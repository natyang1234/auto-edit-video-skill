from __future__ import annotations

from collections.abc import Callable
from io import BytesIO
from pathlib import Path
import struct
import sys
import unittest

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables._c_m_a_p import CmapSubtable


SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from font_security import (  # noqa: E402
    ALLOWED_FONT_LICENSES,
    FONT_VALIDATOR_VERSION,
    LIMITS_SHA256,
    MAX_FONT_BYTES,
    MAX_REQUIRED_TEXT_CHARACTERS,
    RECEIPT_VERSION,
    FontSecurityError,
    FontValidationResult,
    validate_font_bytes,
)


def build_ttf(
    *,
    cmap: dict[int, str] | None = None,
    fs_type: int = 0,
    axis_count: int = 0,
    instance_count: int = 0,
    family_name: str = "Phase Two Test",
    outline_points: int = 4,
) -> bytes:
    cmap = cmap or {
        0x0020: "space",
        0x0041: "A",
        0x00C1: "Aacute",
        0x0301: "acutecomb",
        0x4E2D: "uni4E2D",
        0x7E41: "uni7E41",
    }
    glyph_order = [".notdef", *dict.fromkeys(cmap.values())]
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder(glyph_order)
    builder.setupCharacterMap(cmap)
    glyphs = {}
    for name in glyph_order:
        pen = TTGlyphPen(None)
        if name not in {".notdef", "space"}:
            pen.moveTo((50, 0))
            for index in range(outline_points - 1):
                pen.lineTo((50 + index % 450, (index * 17) % 700))
            pen.closePath()
        glyphs[name] = pen.glyph()
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics({name: (600, 0) for name in glyph_order})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable(
        {
            "familyName": family_name,
            "styleName": "Regular",
            "uniqueFontIdentifier": "Phase Two Test Regular 1.0",
            "fullName": "Phase Two Test Regular",
            "psName": "PhaseTwoTest-Regular",
            "version": "Version 1.0",
        }
    )
    if axis_count or instance_count:
        axis_count = max(1, axis_count)
        axes = [
            (f"A{index:03d}", 0.0, 0.0, 1.0, f"Axis {index}")
            for index in range(axis_count)
        ]
        instances = [
            {
                "location": {tag: 0.5 for tag, *_rest in axes},
                "stylename": f"Instance {index}",
            }
            for index in range(instance_count)
        ]
        builder.setupFvar(axes, instances)
    builder.setupOS2(
        sTypoAscender=800,
        sTypoDescender=-200,
        usWinAscent=800,
        usWinDescent=200,
        usWeightClass=400,
        fsType=fs_type,
    )
    builder.setupPost()
    builder.setupMaxp()
    output = BytesIO()
    builder.save(output)
    return output.getvalue()


def build_otf() -> bytes:
    cmap = {0x20: "space", 0x41: "A", 0x4E2D: "uni4E2D"}
    glyph_order = [".notdef", *cmap.values()]
    builder = FontBuilder(1000, isTTF=False)
    builder.setupGlyphOrder(glyph_order)
    builder.setupCharacterMap(cmap)
    charstrings = {}
    for name in glyph_order:
        pen = T2CharStringPen(600, None)
        if name not in {".notdef", "space"}:
            pen.moveTo((50, 0))
            pen.lineTo((500, 0))
            pen.lineTo((500, 700))
            pen.lineTo((50, 700))
            pen.closePath()
        charstrings[name] = pen.getCharString()
    builder.setupCFF(
        "PhaseTwoTest-Regular",
        {"FullName": "Phase Two Test Regular"},
        charstrings,
        {},
    )
    builder.setupHorizontalMetrics({name: (600, 0) for name in glyph_order})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable(
        {
            "familyName": "Phase Two OTF Test",
            "styleName": "Regular",
            "uniqueFontIdentifier": "Phase Two OTF Test Regular 1.0",
            "fullName": "Phase Two OTF Test Regular",
            "psName": "PhaseTwoOTFTest-Regular",
            "version": "Version 1.0",
        }
    )
    builder.setupOS2(
        sTypoAscender=800,
        sTypoDescender=-200,
        usWinAscent=800,
        usWinDescent=200,
        usWeightClass=500,
        fsType=0,
    )
    builder.setupPost()
    output = BytesIO()
    builder.save(output)
    return output.getvalue()


def with_uvs(raw: bytes, selector: int, base: int, glyph_name: str | None = None) -> bytes:
    font = TTFont(BytesIO(raw), lazy=False)
    subtable = CmapSubtable.newSubtable(14)
    subtable.platformID = 0
    subtable.platEncID = 5
    subtable.language = 0
    subtable.cmap = {}
    subtable.uvsDict = {selector: [(base, glyph_name)]}
    font["cmap"].tables.append(subtable)
    output = BytesIO()
    font.save(output)
    font.close()
    return output.getvalue()


def sfnt_checksum(data: bytes, *, head: bool = False) -> int:
    if head:
        data = data[:8] + b"\0\0\0\0" + data[12:]
    data += b"\0" * ((-len(data)) % 4)
    return sum(struct.unpack(f">{len(data) // 4}I", data)) & 0xFFFFFFFF


def mutate_table(raw: bytes, tag: bytes, mutation: Callable[[bytearray, int, int], None]) -> bytes:
    changed = bytearray(raw)
    head_entry = directory_entry(raw, b"head")
    head_offset = struct.unpack_from(">I", changed, head_entry + 8)[0]
    struct.pack_into(">I", changed, head_offset + 8, 0)
    target_entry = directory_entry(raw, tag)
    target_offset, target_length = struct.unpack_from(">II", changed, target_entry + 8)
    mutation(changed, target_offset, target_length)
    count = struct.unpack_from(">H", changed, 4)[0]
    for index in range(count):
        entry = 12 + 16 * index
        table_tag = bytes(changed[entry : entry + 4])
        offset, length = struct.unpack_from(">II", changed, entry + 8)
        checksum = sfnt_checksum(bytes(changed[offset : offset + length]), head=table_tag == b"head")
        struct.pack_into(">I", changed, entry + 4, checksum)
    adjustment = (0xB1B0AFBA - sfnt_checksum(bytes(changed))) & 0xFFFFFFFF
    struct.pack_into(">I", changed, head_offset + 8, adjustment)
    return bytes(changed)


def insert_gap_before_table(raw: bytes, target_tag: bytes, payload: bytes) -> bytes:
    """Insert checksum-valid bytes outside all tables and move later offsets."""

    if not payload or len(payload) % 4:
        raise AssertionError("test gap must be non-empty and four-byte aligned")
    target_entry = directory_entry(raw, target_tag)
    target_offset = struct.unpack_from(">I", raw, target_entry + 8)[0]
    changed = bytearray(raw[:target_offset] + payload + raw[target_offset:])
    count = struct.unpack_from(">H", raw, 4)[0]
    for index in range(count):
        entry = 12 + 16 * index
        table_offset = struct.unpack_from(">I", raw, entry + 8)[0]
        if table_offset >= target_offset:
            struct.pack_into(">I", changed, entry + 8, table_offset + len(payload))
    head_entry = directory_entry(raw, b"head")
    head_offset = struct.unpack_from(">I", changed, head_entry + 8)[0]
    struct.pack_into(">I", changed, head_offset + 8, 0)
    adjustment = (0xB1B0AFBA - sfnt_checksum(bytes(changed))) & 0xFFFFFFFF
    struct.pack_into(">I", changed, head_offset + 8, adjustment)
    return bytes(changed)


def directory_entry(raw: bytes, tag: bytes) -> int:
    count = struct.unpack_from(">H", raw, 4)[0]
    for index in range(count):
        entry = 12 + 16 * index
        if raw[entry : entry + 4] == tag:
            return entry
    raise AssertionError(f"missing table {tag!r}")


class HappyPathTests(unittest.TestCase):
    def test_valid_ttf_returns_hash_bound_canonical_metadata_and_coverage(self) -> None:
        raw = build_ttf()

        result = validate_font_bytes(
            raw,
            required_text="繁中 A\u0301\n",
            license_spdx="OFL-1.1",
        )

        self.assertIsInstance(result, FontValidationResult)
        self.assertEqual(result.container, "ttf")
        self.assertEqual(result.mime, "font/ttf")
        self.assertEqual(result.family, "Phase Two Test")
        self.assertEqual(result.subfamily, "Regular")
        self.assertEqual(result.style, "normal")
        self.assertEqual(result.weight, 400)
        self.assertEqual(result.required_text_nfc, "繁中 Á\n")
        self.assertEqual(result.receipt["receipt_version"], RECEIPT_VERSION)
        self.assertEqual(result.receipt["validator_version"], FONT_VALIDATOR_VERSION)
        self.assertEqual(result.receipt["font_sha256"], result.sha256)
        self.assertEqual(result.receipt["license_spdx"], "OFL-1.1")
        self.assertEqual(result.receipt["limits_sha256"], LIMITS_SHA256)
        self.assertEqual(
            result.receipt["unicode_coverage_count"],
            len(result.unicode_codepoints),
        )
        self.assertRegex(result.receipt["unicode_coverage_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(result.receipt["required_coverage_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            [item["codepoint"] for item in result.required_glyphs],
            ["U+7E41", "U+4E2D", "U+00C1"],
        )
        self.assertIn("CJK", result.scripts)
        self.assertIn("Latin", result.scripts)

    def test_valid_static_otf_is_accepted_without_rasterizing(self) -> None:
        result = validate_font_bytes(
            build_otf(),
            required_text="A中",
            license_spdx="Apache-2.0",
        )
        self.assertEqual((result.container, result.mime), ("otf", "font/otf"))
        self.assertEqual((result.family, result.weight), ("Phase Two OTF Test", 500))
        self.assertFalse(result.receipt["hinting_executed"])

    def test_declared_mime_must_match_magic_and_receipt_is_deterministic(self) -> None:
        raw = build_ttf()
        first = validate_font_bytes(
            raw,
            license_spdx="Ubuntu-font-1.0",
            declared_mime="font/ttf",
        )
        second = validate_font_bytes(
            raw,
            license_spdx="Ubuntu-font-1.0",
            declared_mime="font/ttf",
        )
        self.assertEqual(first, second)
        with self.assertRaises(FontSecurityError) as rejected:
            validate_font_bytes(
                raw,
                license_spdx="Ubuntu-font-1.0",
                declared_mime="font/otf",
            )
        self.assertEqual(rejected.exception.code, "FONT_MIME_MISMATCH")

    def test_license_is_exact_allowlist_and_required(self) -> None:
        raw = build_ttf()
        for license_spdx in sorted(ALLOWED_FONT_LICENSES):
            with self.subTest(license=license_spdx):
                result = validate_font_bytes(raw, license_spdx=license_spdx)
                self.assertEqual(result.receipt["license_spdx"], license_spdx)
        for license_spdx, code in (
            (None, "FONT_LICENSE_REQUIRED"),
            ("ofl-1.1", "FONT_LICENSE_NOT_ALLOWED"),
            ("CC0-1.0", "FONT_LICENSE_NOT_ALLOWED"),
        ):
            with self.subTest(license=license_spdx):
                with self.assertRaises(FontSecurityError) as rejected:
                    validate_font_bytes(raw, license_spdx=license_spdx)  # type: ignore[arg-type]
                self.assertEqual(rejected.exception.code, code)


class SfntEnvelopeTests(unittest.TestCase):
    def reject(self, raw: bytes, code: str) -> None:
        with self.assertRaises(FontSecurityError) as rejected:
            validate_font_bytes(raw, license_spdx="OFL-1.1")
        self.assertEqual(rejected.exception.code, code)

    def test_rejects_non_sfnt_and_unsupported_containers(self) -> None:
        raw = build_ttf()
        for magic, code in (
            (b"BAD!", "FONT_MAGIC_INVALID"),
            (b"ttcf", "FONT_COLLECTION_FORBIDDEN"),
            (b"wOFF", "FONT_WOFF_FORBIDDEN"),
            (b"wOF2", "FONT_WOFF2_FORBIDDEN"),
        ):
            with self.subTest(magic=magic):
                self.reject(magic + raw[4:], code)
        self.reject(raw[:11], "FONT_TRUNCATED")

        corrupted = bytearray(raw)
        head_entry = directory_entry(raw, b"head")
        head_offset = struct.unpack_from(">I", raw, head_entry + 8)[0]
        corrupted[head_offset] ^= 1
        self.reject(bytes(corrupted), "FONT_TABLE_CHECKSUM")

    def test_rejects_duplicate_overlapping_and_out_of_bounds_tables(self) -> None:
        raw = build_ttf()
        first_entry = 12
        second_entry = 28

        duplicate = bytearray(raw)
        duplicate[second_entry : second_entry + 4] = duplicate[first_entry : first_entry + 4]
        self.reject(bytes(duplicate), "FONT_TABLE_DUPLICATE")

        overlapping = bytearray(raw)
        first_offset = struct.unpack_from(">I", raw, first_entry + 8)[0]
        struct.pack_into(">I", overlapping, second_entry + 8, first_offset)
        self.reject(bytes(overlapping), "FONT_TABLE_OVERLAP")

        out_of_bounds = bytearray(raw)
        struct.pack_into(">I", out_of_bounds, first_entry + 8, len(raw) + 4)
        self.reject(bytes(out_of_bounds), "FONT_TABLE_BOUNDS")

    def test_rejects_checksum_valid_opaque_bytes_in_every_sfnt_gap(self) -> None:
        raw = build_ttf()
        count = struct.unpack_from(">H", raw, 4)[0]
        by_offset = sorted(
            (
                struct.unpack_from(">I", raw, 12 + 16 * index + 8)[0],
                raw[12 + 16 * index : 12 + 16 * index + 4],
            )
            for index in range(count)
        )
        first_tag = by_offset[0][1]
        last_tag = by_offset[-1][1]
        cases = (
            (first_tag, b"PWN!", "FONT_TABLE_GAP_NONZERO"),
            (last_tag, b"PWN!", "FONT_TABLE_GAP_NONZERO"),
            (first_tag, b"\0" * 8, "FONT_TABLE_GAP"),
            (last_tag, b"\0" * 8, "FONT_TABLE_GAP"),
        )
        for target_tag, payload, code in cases:
            with self.subTest(target=target_tag, payload=payload[:4], code=code):
                hostile = insert_gap_before_table(raw, target_tag, payload)
                self.assertEqual(sfnt_checksum(hostile), 0xB1B0AFBA)
                self.reject(hostile, code)

    def test_rejects_raw_table_count_and_disallowed_table_bombs(self) -> None:
        self.reject(b"\0" * (MAX_FONT_BYTES + 1), "FONT_RAW_TOO_LARGE")

        raw = build_ttf()
        excessive_tables = bytearray(raw)
        struct.pack_into(">H", excessive_tables, 4, 65)
        self.reject(bytes(excessive_tables), "FONT_TABLE_COUNT_LIMIT")

        post_entry = directory_entry(raw, b"post")
        for table_tag in (
            b"SVG ", b"COLR", b"CPAL", b"CBDT", b"CBLC", b"sbix",
            b"EBDT", b"EBLC", b"EBSC", b"bdat", b"bloc", b"CFF2",
        ):
            with self.subTest(table=table_tag):
                disallowed = bytearray(raw)
                disallowed[post_entry : post_entry + 4] = table_tag
                self.reject(bytes(disallowed), "FONT_TABLE_FORBIDDEN")

    def test_rejects_variation_axis_bomb_before_deep_parsing(self) -> None:
        self.reject(build_ttf(axis_count=17), "FONT_AXIS_LIMIT")
        self.reject(build_ttf(instance_count=65), "FONT_INSTANCE_LIMIT")

    def test_rejects_glyph_outline_point_bomb(self) -> None:
        self.reject(build_ttf(outline_points=10_001), "FONT_GLYPH_COMPLEXITY_LIMIT")

    def test_broken_name_cmap_and_glyph_tables_fail_closed(self) -> None:
        raw = build_ttf()
        broken_name = mutate_table(
            raw,
            b"name",
            lambda data, offset, length: struct.pack_into(">H", data, offset + 4, length + 100),
        )
        broken_cmap = mutate_table(
            raw,
            b"cmap",
            lambda data, offset, _length: struct.pack_into(">H", data, offset, 1),
        )
        broken_glyph = mutate_table(
            raw,
            b"loca",
            lambda data, offset, length: struct.pack_into(">H", data, offset + length - 2, 0xFFFF),
        )
        excessive_names = mutate_table(
            raw,
            b"name",
            lambda data, offset, _length: struct.pack_into(">H", data, offset + 2, 257),
        )
        excessive_glyphs = mutate_table(
            raw,
            b"maxp",
            lambda data, offset, _length: struct.pack_into(">H", data, offset + 4, 60_001),
        )
        for payload, code in (
            (broken_name, "FONT_NAME_INVALID"),
            (broken_cmap, "FONT_CMAP_INVALID"),
            (broken_glyph, "FONT_PARSER_REJECTED"),
            (excessive_names, "FONT_NAME_LIMIT"),
            (excessive_glyphs, "FONT_GLYPH_COUNT_LIMIT"),
        ):
            with self.subTest(code=code):
                self.reject(payload, code)

    def test_rejects_name_string_bomb(self) -> None:
        self.reject(build_ttf(family_name="N" * 3000), "FONT_NAME_LIMIT")


class CoverageAndRightsTests(unittest.TestCase):
    def test_explicit_variation_selector_sequence_is_traced(self) -> None:
        raw = with_uvs(build_ttf(), 0xFE0F, 0x41)

        result = validate_font_bytes(
            raw,
            required_text="A\ufe0f",
            license_spdx="OFL-1.1",
        )

        self.assertEqual(
            result.required_glyphs,
            (
                {"cluster": 0, "codepoint": "U+0041", "glyph_name": "A"},
                {
                    "cluster": 0,
                    "codepoint": "U+FE0F",
                    "glyph_name": "A",
                    "variation_selector_for": "U+0041",
                    "mapping": "default",
                },
            ),
        )

    def test_missing_variation_and_mixed_cjk_glyphs_fail_closed(self) -> None:
        raw = build_ttf()
        for text, missing in (
            ("A\ufe0f", ("U+FE0F",)),
            ("繁中文B", ("U+6587", "U+0042")),
        ):
            with self.subTest(text=text):
                with self.assertRaises(FontSecurityError) as rejected:
                    validate_font_bytes(raw, required_text=text, license_spdx="OFL-1.1")
                self.assertEqual(rejected.exception.code, "FONT_REQUIRED_GLYPH_MISSING")
                self.assertEqual(rejected.exception.missing_codepoints, missing)

    def test_whitespace_and_controls_are_ignored_but_traceable(self) -> None:
        result = validate_font_bytes(
            build_ttf(),
            required_text=" A\t\nA\u200dA ",
            license_spdx="OFL-1.1",
        )
        self.assertEqual(len(result.required_glyphs), 3)
        self.assertEqual(result.required_glyphs[-1]["cluster"], 1)
        self.assertEqual(
            {item["reason"] for item in result.ignored_characters},
            {"whitespace", "control-or-format"},
        )

    def test_embedding_restrictions_fail_closed(self) -> None:
        for fs_type in (0x0002, 0x0200, 0x000C, 0x8000):
            with self.subTest(fs_type=fs_type):
                with self.assertRaises(FontSecurityError) as rejected:
                    validate_font_bytes(build_ttf(fs_type=fs_type), license_spdx="OFL-1.1")
                self.assertEqual(rejected.exception.code, "FONT_EMBEDDING_RESTRICTED")

    def test_required_text_type_and_size_are_bounded(self) -> None:
        raw = build_ttf()
        for text, code in (
            (object(), "FONT_REQUIRED_TEXT_INVALID"),
            ("A" * (MAX_REQUIRED_TEXT_CHARACTERS + 1), "FONT_REQUIRED_TEXT_LIMIT"),
        ):
            with self.subTest(code=code):
                with self.assertRaises(FontSecurityError) as rejected:
                    validate_font_bytes(  # type: ignore[arg-type]
                        raw,
                        required_text=text,
                        license_spdx="OFL-1.1",
                    )
                self.assertEqual(rejected.exception.code, code)


if __name__ == "__main__":
    unittest.main()
