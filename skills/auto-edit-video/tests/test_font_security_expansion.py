"""Expansion bombs the 12M outline budget must still bound (2026-10-22 Codex review)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import font_security  # noqa: E402
from font_security import FontSecurityError, validate_font_bytes  # noqa: E402
from test_font_security import build_otf, build_ttf  # noqa: E402

OTF = build_otf()


def build_composite_bomb(*, fan_out: int = 64, depth: int = 3, base_points: int = 3000) -> bytes:
    """A small file whose composites expand to fan_out**depth × base_points outline points."""
    cmap = {0x0020: "space", 0x0041: "A", 0x0042: "B", 0x0043: "C", 0x0044: "D"}
    order = [".notdef", "space", "A", "B", "C", "D"]
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder(order)
    builder.setupCharacterMap(cmap)
    glyphs = {}
    for name in (".notdef", "space"):
        glyphs[name] = TTGlyphPen(None).glyph()
    pen = TTGlyphPen(None)
    pen.moveTo((50, 0))
    for index in range(base_points - 1):
        pen.lineTo((50 + index % 450, (index * 17) % 700))
    pen.closePath()
    glyphs["A"] = pen.glyph()
    chain = ["A", "B", "C", "D"]
    for level in range(1, min(depth, 3) + 1):
        pen = TTGlyphPen(glyphs)  # component lookup only checks membership
        for _ in range(fan_out):
            pen.addComponent(chain[level - 1], (1, 0, 0, 1, 0, 0))
        glyphs[chain[level]] = pen.glyph()
    for name in chain[min(depth, 3) + 1:]:
        glyphs[name] = TTGlyphPen(None).glyph()
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics({name: (600, 0) for name in order})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({"familyName": "Bomb", "styleName": "Regular"})
    builder.setupOS2(fsType=0)
    builder.setupPost()
    from io import BytesIO

    # maxp.maxCompositePoints is a uint16: a real attacker just leaves it small, so do the same
    # instead of letting fontTools recalc (and overflow) the advisory field.
    builder.font.recalcBBoxes = False
    maxp = builder.font["maxp"]
    maxp.maxCompositePoints = 1
    maxp.maxCompositeContours = 1
    maxp.maxComponentElements = min(fan_out, 64)
    maxp.maxComponentDepth = depth
    out = BytesIO()
    builder.save(out)
    return out.getvalue()


from fontTools.misc.psCharStrings import T2CharString  # noqa: E402


class _FakeSubr:
    """A charstring built from a token program (numbers + operator names) via fontTools' encoder."""

    def __init__(self, program: list) -> None:
        self.program = program
        cs = T2CharString(program=program)
        cs.compile()
        self.bytecode = cs.bytecode


class ExpansionBombTests(unittest.TestCase):
    def reject(self, raw: bytes, code: str) -> None:
        with self.assertRaises(FontSecurityError) as rejected:
            validate_font_bytes(raw, license_spdx="OFL-1.1")
        self.assertEqual(rejected.exception.code, code)

    def test_small_composite_is_fine(self) -> None:
        validate_font_bytes(build_composite_bomb(fan_out=2, depth=1, base_points=20), license_spdx="OFL-1.1")

    def test_truetype_composite_bomb_is_charged_after_expansion(self) -> None:
        # 64 × 64 × 900 = 3.7M referenced points (and > MAX_COMPOSITE_EXPANDED_POINTS for one glyph)
        # from a file of a few KB.
        self.reject(build_composite_bomb(fan_out=64, depth=2, base_points=900), "FONT_GLYPH_COMPLEXITY_LIMIT")

    def test_cff_recursive_subroutine_is_rejected(self) -> None:
        subrs = [_FakeSubr([-107, "callsubr", "return"])]  # local subr 0 calls itself (bias 107)
        glyph = _FakeSubr([-107, "callsubr", "endchar"])
        with self.assertRaises(FontSecurityError) as rejected:
            font_security._cff_walk(glyph.bytecode, [], [x.bytecode for x in subrs], [0])
        self.assertEqual(rejected.exception.code, "FONT_CFF_INVALID")

    def test_cff_subroutine_nesting_deeper_than_spec_is_rejected(self) -> None:
        subrs = [_FakeSubr([(i + 1) - 107, "callsubr", "return"]) for i in range(12)]
        subrs.append(_FakeSubr(["return"]))
        glyph = _FakeSubr([-107, "callsubr", "endchar"])
        with self.assertRaises(FontSecurityError) as rejected:
            font_security._cff_walk(glyph.bytecode, [], [x.bytecode for x in subrs], [0])
        self.assertEqual(rejected.exception.code, "FONT_GLYPH_COMPLEXITY_LIMIT")

    def test_cff_fan_out_bomb_exceeds_expanded_ops(self) -> None:
        # 4 levels × 100 calls = 100**4 operators once followed, from ~1 KB of bytecode.
        subrs = [_FakeSubr([(level + 1) - 107, "callsubr"] * 100 + ["return"]) for level in range(4)]
        subrs.append(_FakeSubr([1, 2, "rlineto", "return"]))
        glyph = _FakeSubr([-107, "callsubr", "endchar"])
        budget = [0]
        with self.assertRaises(FontSecurityError) as rejected:
            font_security._cff_walk(glyph.bytecode, [], [x.bytecode for x in subrs], budget)
        self.assertEqual(rejected.exception.code, "FONT_GLYPH_COMPLEXITY_LIMIT")
        self.assertLessEqual(budget[0], font_security.MAX_CFF_EXPANDED_OPS + 1)  # stopped at the glyph budget

    def test_cff_sane_subroutines_are_followed_and_counted(self) -> None:
        subrs = [_FakeSubr([1 - 107, "callsubr", "return"]), _FakeSubr([10, 20, "rlineto", "return"])]
        glyph = _FakeSubr([0, 0, "rmoveto", 0 - 107, "callsubr", "endchar"])
        ops = font_security._cff_walk(glyph.bytecode, [], [x.bytecode for x in subrs], [0])
        self.assertEqual(ops, 7)  # rmoveto, callsubr, callsubr, rlineto, return, return, endchar

    def test_cff_hintmask_inside_subroutine_uses_caller_stem_count(self) -> None:
        # 2 hstems + 3 vstems declared by the glyph → 5 stems → one mask byte inside the subr.
        subrs = [_FakeSubr(["hintmask", b"\x80", 5, 5, "rlineto", "return"])]
        glyph = _FakeSubr([0, 10, 20, 30, "hstem", 0, 5, 10, 15, 20, 25, "vstem", 0 - 107, "callsubr", "endchar"])
        self.assertGreater(font_security._cff_walk(glyph.bytecode, [], [x.bytecode for x in subrs], [0]), 0)

    def test_second_hintmask_cannot_hide_a_subr_call(self) -> None:
        # Mask width is frozen at the first hintmask (4 stems → 1 byte). Sixteen operands pushed
        # before a second hintmask must not widen it to 2 bytes and swallow the following call.
        subrs = [_FakeSubr([-107, "callsubr", "return"])]  # recursive
        glyph = _FakeSubr([10, 20, "hstem", 30, 40, "vstem", "hintmask", b"\xc0",
                           1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, "hintmask", b"\x80",
                           -107, "callsubr", "endchar"])
        with self.assertRaises(FontSecurityError) as rejected:
            font_security._cff_walk(glyph.bytecode, [], [x.bytecode for x in subrs], [0])
        self.assertEqual(rejected.exception.code, "FONT_CFF_INVALID")

    def test_arithmetic_escape_is_rejected_not_approximated(self) -> None:
        glyph = _FakeSubr([5, 6, "add", -107, "callsubr", "endchar"])
        with self.assertRaises(FontSecurityError) as rejected:
            font_security._cff_walk(glyph.bytecode, [], [_FakeSubr(["return"]).bytecode], [0])
        self.assertEqual(rejected.exception.code, "FONT_CFF_INVALID")

    def test_flex_escape_is_followed(self) -> None:
        glyph = _FakeSubr([0, 0, "rmoveto", 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, "flex", -107, "callsubr", "endchar"])
        ops = font_security._cff_walk(glyph.bytecode, [], [_FakeSubr([1, 1, "rlineto", "return"]).bytecode], [0])
        self.assertEqual(ops, 6)

    def test_whole_cff_font_with_recursive_subr_is_rejected_before_drawing(self) -> None:
        """fontTools itself cannot build a recursive subr (it executes charstrings while saving),
        which is exactly why the validator must catch one: build a benign font, then patch the
        3-byte local subr ``0 hmoveto return`` into ``-107 callsubr return`` (self-recursive) and
        fix the CFF table checksum so only the walker stands between the bytes and fontTools."""
        import struct
        from fontTools.cffLib import SubrsIndex

        cmap = {0x0020: "space", 0x0041: "A"}
        order = [".notdef", "space", "A"]
        builder = FontBuilder(1000, isTTF=False)
        builder.setupGlyphOrder(order)
        builder.setupCharacterMap(cmap)
        charstrings = {
            ".notdef": T2CharString(program=["endchar"]),
            "space": T2CharString(program=["endchar"]),
            # second-mask trick followed by a call into local subr 0
            "A": T2CharString(program=[10, 20, "hstem", 30, 40, "vstem", "hintmask", b"\xc0",
                                       1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, "hintmask", b"\x80",
                                       -107, "callsubr", "endchar"]),
        }
        builder.setupCFF("Bomb", {"FullName": "Bomb"}, charstrings, {})
        private = builder.font["CFF "].cff.topDictIndex[0].Private
        private.Subrs = SubrsIndex()
        private.Subrs.append(T2CharString(program=[0, "hmoveto", "return"]))  # benign, 3 bytes
        builder.setupHorizontalMetrics({name: (600, 0) for name in order})
        builder.setupHorizontalHeader(ascent=800, descent=-200)
        builder.setupNameTable({"familyName": "Bomb", "styleName": "Regular"})
        builder.setupOS2(fsType=0)
        builder.setupPost()
        from io import BytesIO

        out = BytesIO()
        builder.save(out)
        raw = bytearray(out.getvalue())
        benign, evil = bytes([139, 22, 11]), bytes([32, 10, 11])  # 0 hmoveto return → -107 callsubr return
        self.assertEqual(raw.count(benign), 1)
        raw[raw.index(benign):raw.index(benign) + 3] = evil
        num_tables = struct.unpack_from(">H", raw, 4)[0]
        for index in range(num_tables):
            offset = 12 + 16 * index
            tag, _checksum, table_offset, length = struct.unpack_from(">4sIII", raw, offset)
            if tag == b"CFF ":
                struct.pack_into(">I", raw, offset + 4, font_security._checksum(bytes(raw[table_offset:table_offset + length])))
            if tag == b"head":
                head_offset = table_offset
        # whole-file checksum: head.checkSumAdjustment = 0xB1B0AFBA - sum(file with adjustment zeroed)
        struct.pack_into(">I", raw, head_offset + 8, 0)
        adjustment = (0xB1B0AFBA - font_security._checksum(bytes(raw))) & 0xFFFFFFFF
        struct.pack_into(">I", raw, head_offset + 8, adjustment)
        with self.assertRaises(FontSecurityError) as rejected:
            validate_font_bytes(bytes(raw), license_spdx="OFL-1.1", declared_mime="font/otf")
        self.assertEqual(rejected.exception.code, "FONT_CFF_INVALID")

    def test_call_hidden_behind_return_or_endchar_is_still_walked(self) -> None:
        # fontTools keeps executing after return/endchar, so must the walker.
        for tail in ("return", "endchar"):
            subrs = [_FakeSubr([tail, -107, "callsubr"])]  # self-call after the terminator
            glyph = _FakeSubr([0, 0, "rmoveto", -107, "callsubr", "endchar"])
            with self.assertRaises(FontSecurityError) as rejected:
                font_security._cff_walk(glyph.bytecode, [], [x.bytecode for x in subrs], [0])
            self.assertEqual(rejected.exception.code, "FONT_CFF_INVALID", tail)

    def test_zero_width_first_mask_leaves_width_uninitialised_like_fonttools(self) -> None:
        # No stems → first hintmask has 0 bytes; the next one (after 4 operands → 2 stems) is 1 byte.
        glyph = _FakeSubr(["hintmask", b"", 1, 2, 3, 4, "hintmask", b"\x80", 0, 0, "rmoveto", "endchar"])
        self.assertGreater(font_security._cff_walk(glyph.bytecode, [], [], [0]), 0)

    def test_whole_cff_font_with_call_after_return_is_rejected(self) -> None:
        import struct
        from fontTools.cffLib import SubrsIndex
        from io import BytesIO

        order = [".notdef", "space", "A"]
        builder = FontBuilder(1000, isTTF=False)
        builder.setupGlyphOrder(order)
        builder.setupCharacterMap({0x0020: "space", 0x0041: "A"})
        builder.setupCFF("Bomb", {"FullName": "Bomb"}, {
            ".notdef": T2CharString(program=["endchar"]), "space": T2CharString(program=["endchar"]),
            "A": T2CharString(program=[0, 0, "rmoveto", -107, "callsubr", "endchar"])}, {})
        private = builder.font["CFF "].cff.topDictIndex[0].Private
        private.Subrs = SubrsIndex()
        private.Subrs.append(T2CharString(program=[0, "hmoveto", "return"]))  # benign 3 bytes
        builder.setupHorizontalMetrics({name: (600, 0) for name in order})
        builder.setupHorizontalHeader(ascent=800, descent=-200)
        builder.setupNameTable({"familyName": "Bomb", "styleName": "Regular"})
        builder.setupOS2(fsType=0)
        builder.setupPost()
        out = BytesIO(); builder.save(out); raw = bytearray(out.getvalue())
        benign, evil = bytes([139, 22, 11]), bytes([11, 32, 10])  # → "return -107 callsubr"
        self.assertEqual(raw.count(benign), 1)
        raw[raw.index(benign):raw.index(benign) + 3] = evil
        num_tables = struct.unpack_from(">H", raw, 4)[0]
        for index in range(num_tables):
            offset = 12 + 16 * index
            tag, _c, table_offset, length = struct.unpack_from(">4sIII", raw, offset)
            if tag == b"CFF ":
                struct.pack_into(">I", raw, offset + 4, font_security._checksum(bytes(raw[table_offset:table_offset + length])))
            if tag == b"head":
                head_offset = table_offset
        struct.pack_into(">I", raw, head_offset + 8, 0)
        struct.pack_into(">I", raw, head_offset + 8, (0xB1B0AFBA - font_security._checksum(bytes(raw))) & 0xFFFFFFFF)
        with self.assertRaises(FontSecurityError) as rejected:
            validate_font_bytes(bytes(raw), license_spdx="OFL-1.1", declared_mime="font/otf")
        self.assertEqual(rejected.exception.code, "FONT_CFF_INVALID")

    def test_component_expansion_helper_rejects_cycles_and_fan_out(self) -> None:
        charge = font_security._charge_component_expansion
        with self.assertRaises(FontSecurityError) as rejected:
            charge(["A"], {"A": 10}, {"A": ("A",)})  # seac self-reference
        self.assertEqual(rejected.exception.code, "FONT_GLYPH_INVALID")
        with self.assertRaises(FontSecurityError) as rejected:
            charge(["A", "B"], {"A": 10, "B": 5}, {"A": ("B",), "B": ("A",)})  # 2-cycle
        self.assertEqual(rejected.exception.code, "FONT_GLYPH_INVALID")
        # 60,000 seac glyphs each pulling a 9,000-point base: 540M expanded points → rejected
        names = [f"g{i}" for i in range(60_000)]
        points = {"base": 9_000, **{n: 0 for n in names}}
        graph = {"base": (), **{n: ("base", "base") for n in names}}
        with self.assertRaises(FontSecurityError) as rejected:
            charge(["base", *names], points, graph)
        self.assertEqual(rejected.exception.code, "FONT_GLYPH_COMPLEXITY_LIMIT")
        # sane: a few accented glyphs
        self.assertEqual(charge(["a", "grave", "agrave"], {"a": 40, "grave": 8, "agrave": 0}, {"a": (), "grave": (), "agrave": ("a", "grave")}), 96)

    def test_whole_cff_font_with_seac_is_accounted_and_accepted(self) -> None:
        from fontTools.pens.t2CharStringPen import T2CharStringPen
        from io import BytesIO

        order = [".notdef", "space", "a", "grave", "agrave"]
        builder = FontBuilder(1000, isTTF=False)
        builder.setupGlyphOrder(order)
        builder.setupCharacterMap({0x0020: "space", 0x0061: "a", 0x0060: "grave", 0x00E0: "agrave"})

        def box(w: int) -> T2CharString:
            pen = T2CharStringPen(600, None)
            pen.moveTo((50, 0)); pen.lineTo((w, 0)); pen.lineTo((w, 500)); pen.lineTo((50, 500)); pen.closePath()
            return pen.getCharString()

        charstrings = {".notdef": T2CharString(program=["endchar"]), "space": T2CharString(program=["endchar"]),
                       "a": box(400), "grave": box(120),
                       "agrave": T2CharString(program=[0, 600, 97, 193, "endchar"])}  # seac: base 'a', accent 'grave'
        builder.setupCFF("Seac", {"FullName": "Seac"}, charstrings, {})
        builder.setupHorizontalMetrics({name: (600, 0) for name in order})
        builder.setupHorizontalHeader(ascent=800, descent=-200)
        builder.setupNameTable({"familyName": "Seac", "styleName": "Regular"})
        builder.setupOS2(fsType=0)
        builder.setupPost()
        out = BytesIO(); builder.save(out)
        validate_font_bytes(out.getvalue(), "aà", license_spdx="OFL-1.1", declared_mime="font/otf")

    def test_operand_must_precede_subr_call(self) -> None:
        with self.assertRaises(FontSecurityError) as rejected:
            font_security._cff_walk(_FakeSubr(["callsubr", "endchar"]).bytecode, [], [_FakeSubr(["return"]).bytecode], [0])
        self.assertEqual(rejected.exception.code, "FONT_CFF_INVALID")

    def test_cff_total_charstring_cap_is_twenty_mib_and_enforced_at_the_boundary(self) -> None:
        self.assertEqual(font_security.MAX_CFF_TOTAL_CHARSTRING_BYTES, 20 * 1024 * 1024)
        self.assertEqual(font_security._LIMITS["cff_total_charstring_bytes"], 20 * 1024 * 1024)
        # Exercise the same accumulator with the cap scaled down: just-under passes, over rejects.
        total = sum(len(getattr(cs, "bytecode", b"")) for cs in self._otf_charstrings(OTF))
        self.assertGreater(total, 0)
        with mock.patch.object(font_security, "MAX_CFF_TOTAL_CHARSTRING_BYTES", total):
            validate_font_bytes(OTF, license_spdx="OFL-1.1", declared_mime="font/otf")
        with mock.patch.object(font_security, "MAX_CFF_TOTAL_CHARSTRING_BYTES", total - 1):
            self.reject(OTF, "FONT_GLYPH_COMPLEXITY_LIMIT")

    @staticmethod
    def _otf_charstrings(raw: bytes):
        from io import BytesIO
        from fontTools.ttLib import TTFont

        font = TTFont(BytesIO(raw))
        top = font["CFF "].cff.topDictIndex[0]
        subrs = list(getattr(top, "GlobalSubrs", ())) + list(getattr(getattr(top, "Private", None), "Subrs", ()) or ())
        return subrs + [top.CharStrings[n] for n in font.getGlyphOrder()]

    def test_existing_fixtures_still_pass(self) -> None:
        validate_font_bytes(build_ttf(), license_spdx="OFL-1.1")


if __name__ == "__main__":
    unittest.main()
