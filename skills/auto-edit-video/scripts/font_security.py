#!/usr/bin/env python3
"""Strict, deterministic validation for untrusted project-private sfnt fonts.

The validator parses bytes only.  It never rasterizes text, invokes a shaping
engine, executes TrueType instructions, or resolves a system-font fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from io import BytesIO
import json
import math
import struct
from typing import Any
import unicodedata

from fontTools import __version__ as FONTTOOLS_RUNTIME_VERSION
from fontTools.ttLib import TTFont


FONT_VALIDATOR_VERSION = "font-security/1"
POLICY_VERSION = "font-security-policy/3"  # /2: outline points 2M→12M; /3: CFF charstring total 8→20 MiB (2026-10-22)
RECEIPT_VERSION = "font-validation-receipt/1"
PINNED_FONTTOOLS_VERSION = "4.62.1"
ALLOWED_FONT_LICENSES = frozenset({"OFL-1.1", "Apache-2.0", "Ubuntu-font-1.0"})

MAX_FONT_BYTES = 32 * 1024 * 1024
MAX_TABLES = 64
MAX_TABLE_BYTES = 24 * 1024 * 1024
MAX_DIRECTORY_ALIGNMENT_GAP = 3
MAX_INTER_TABLE_GAP = 0
MAX_GLYPHS = 60_000
MAX_CMAP_MAPPINGS = 200_000
MAX_CMAP_SUBTABLES = 32
MAX_NAME_RECORDS = 256
MAX_NAME_STRING_BYTES = 4096
MAX_NAME_TABLE_BYTES = 256 * 1024
MAX_METADATA_CHARACTERS = 256
MAX_AXES = 16
MAX_INSTANCES = 64
MAX_REQUIRED_TEXT_CHARACTERS = 100_000
MAX_POINTS_PER_GLYPH = 10_000
# 2026-10-22 (nat 核可): raised from 2_000_000 so CJK families pass. A Traditional Chinese
# font carries 20k–50k glyphs (Noto Sans TC ≈2.7M points, 台北黑體 ≈4.8M, 內海 ≈9.1M); the
# bound still caps worst-case validator/renderer work, every other limit is unchanged, and
# sources stay pinned (immutable Google Fonts commit over HTTPS, SHA-256 re-verified on use).
MAX_TOTAL_OUTLINE_POINTS = 12_000_000
MAX_CONTOURS_PER_GLYPH = 512
MAX_COMPONENTS_PER_GLYPH = 64
MAX_COMPOSITE_DEPTH = 16
MAX_INSTRUCTION_BYTES = 64 * 1024
MAX_CFF_CHARSTRING_BYTES = 64 * 1024
MAX_CFF_TOTAL_CHARSTRING_BYTES = 20 * 1024 * 1024  # 2026-10-22 (nat 核可): 8→20 MiB so 50k-glyph CJK CFF fonts (昭源環方 ≈18 MB) pass; expansion is bounded by the Type 2 walker, this only caps storage
MAX_CFF_SUBROUTINES = 65_536  # CFF INDEX maximum; CJK CID fonts carry 13k–30k per FD (2026-10-22, was 4096)
# 2026-10-22 (Codex review of the 12M raise): expansion bounds, not just storage bounds.
MAX_CFF_SUBR_DEPTH = 10            # CFF spec nesting limit; cycles are rejected outright
MAX_CFF_EXPANDED_OPS = 100_000     # operators one glyph may execute once every subr call is followed
MAX_CFF_FONT_EXPANDED_OPS = 30_000_000  # … and across the whole font (bounds the analyser itself)
MAX_COMPOSITE_EXPANDED_POINTS = 200_000  # points one composite glyph may reference after expansion
MAX_GLYPH_NAME_CHARACTERS = 128

_LIMITS = {
    "font_bytes": MAX_FONT_BYTES,
    "tables": MAX_TABLES,
    "table_bytes": MAX_TABLE_BYTES,
    "directory_alignment_gap_bytes": MAX_DIRECTORY_ALIGNMENT_GAP,
    "inter_table_gap_bytes": MAX_INTER_TABLE_GAP,
    "glyphs": MAX_GLYPHS,
    "cmap_mappings": MAX_CMAP_MAPPINGS,
    "cmap_subtables": MAX_CMAP_SUBTABLES,
    "name_records": MAX_NAME_RECORDS,
    "name_string_bytes": MAX_NAME_STRING_BYTES,
    "name_table_bytes": MAX_NAME_TABLE_BYTES,
    "metadata_characters": MAX_METADATA_CHARACTERS,
    "axes": MAX_AXES,
    "instances": MAX_INSTANCES,
    "required_text_characters": MAX_REQUIRED_TEXT_CHARACTERS,
    "points_per_glyph": MAX_POINTS_PER_GLYPH,
    "total_outline_points": MAX_TOTAL_OUTLINE_POINTS,
    "contours_per_glyph": MAX_CONTOURS_PER_GLYPH,
    "components_per_glyph": MAX_COMPONENTS_PER_GLYPH,
    "composite_depth": MAX_COMPOSITE_DEPTH,
    "instruction_bytes": MAX_INSTRUCTION_BYTES,
    "cff_charstring_bytes": MAX_CFF_CHARSTRING_BYTES,
    "cff_total_charstring_bytes": MAX_CFF_TOTAL_CHARSTRING_BYTES,
    "cff_subroutines": MAX_CFF_SUBROUTINES,
    "cff_subr_depth": MAX_CFF_SUBR_DEPTH,
    "cff_expanded_ops": MAX_CFF_EXPANDED_OPS,
    "cff_font_expanded_ops": MAX_CFF_FONT_EXPANDED_OPS,
    "composite_expanded_points": MAX_COMPOSITE_EXPANDED_POINTS,
    "glyph_name_characters": MAX_GLYPH_NAME_CHARACTERS,
}


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


LIMITS_SHA256 = hashlib.sha256(_canonical_json(_LIMITS)).hexdigest()

_SFNT_MAGIC = {
    b"\x00\x01\x00\x00": ("ttf", "font/ttf"),
    b"OTTO": ("otf", "font/otf"),
}
_REJECTED_MAGIC = {
    b"ttcf": "FONT_COLLECTION_FORBIDDEN",
    b"wOFF": "FONT_WOFF_FORBIDDEN",
    b"wOF2": "FONT_WOFF2_FORBIDDEN",
}
_DISALLOWED_TABLES = frozenset(
    {
        "SVG ",
        "COLR",
        "CPAL",
        "CBDT",
        "CBLC",
        "sbix",
        "EBDT",
        "EBLC",
        "EBSC",
        "bdat",
        "bloc",
        "CFF2",
    }
)
_COMMON_REQUIRED = frozenset({"head", "hhea", "maxp", "hmtx", "cmap", "name", "OS/2", "post"})
_CONTAINER_REQUIRED = {
    "ttf": frozenset({"glyf", "loca"}),
    "otf": frozenset({"CFF "}),
}
_ALLOWED_FSTYPE_MASK = 0x030E


class FontSecurityError(ValueError):
    """Stable policy error suitable for a service boundary."""

    def __init__(self, code: str, *, missing_codepoints: tuple[str, ...] = ()) -> None:
        self.code = code
        self.missing_codepoints = missing_codepoints
        message = code
        if missing_codepoints:
            message += ":" + ",".join(missing_codepoints)
        super().__init__(message)


@dataclass(frozen=True)
class FontValidationResult:
    container: str
    mime: str
    byte_length: int
    sha256: str
    family: str
    subfamily: str
    style: str
    weight: int
    embedding_fs_type: int
    glyph_count: int
    unicode_codepoints: tuple[int, ...]
    unicode_ranges: tuple[tuple[str, str], ...]
    scripts: tuple[str, ...]
    required_text_nfc: str
    required_glyphs: tuple[dict[str, Any], ...]
    ignored_characters: tuple[dict[str, str], ...]
    receipt: dict[str, Any]


@dataclass(frozen=True)
class _TableRecord:
    tag: str
    checksum: int
    offset: int
    length: int


def _reject(code: str, *, missing: tuple[str, ...] = ()) -> None:
    raise FontSecurityError(code, missing_codepoints=missing)


def _checksum(data: bytes, *, zero_head_adjustment: bool = False) -> int:
    if zero_head_adjustment:
        if len(data) < 12:
            _reject("FONT_HEAD_INVALID")
        data = data[:8] + b"\0\0\0\0" + data[12:]
    padding = (-len(data)) % 4
    if padding:
        data += b"\0" * padding
    return sum(struct.unpack(f">{len(data) // 4}I", data)) & 0xFFFFFFFF


def _read_directory(raw: bytes) -> tuple[str, str, dict[str, _TableRecord]]:
    if not isinstance(raw, bytes):
        _reject("FONT_BYTES_REQUIRED")
    if len(raw) > MAX_FONT_BYTES:
        _reject("FONT_RAW_TOO_LARGE")
    if len(raw) < 12:
        _reject("FONT_TRUNCATED")
    magic = raw[:4]
    if magic in _REJECTED_MAGIC:
        _reject(_REJECTED_MAGIC[magic])
    identity = _SFNT_MAGIC.get(magic)
    if identity is None:
        _reject("FONT_MAGIC_INVALID")

    num_tables, search_range, entry_selector, range_shift = struct.unpack_from(">HHHH", raw, 4)
    if not 0 < num_tables <= MAX_TABLES:
        _reject("FONT_TABLE_COUNT_LIMIT")
    expected_selector = num_tables.bit_length() - 1
    expected_search_range = 16 * (1 << expected_selector)
    if (
        search_range != expected_search_range
        or entry_selector != expected_selector
        or range_shift != 16 * num_tables - expected_search_range
    ):
        _reject("FONT_DIRECTORY_INVALID")
    directory_end = 12 + 16 * num_tables
    if directory_end > len(raw):
        _reject("FONT_TRUNCATED")

    records: dict[str, _TableRecord] = {}
    intervals: list[tuple[int, int, str]] = []
    for index in range(num_tables):
        offset = 12 + 16 * index
        tag_bytes, checksum, table_offset, length = struct.unpack_from(">4sIII", raw, offset)
        if any(byte < 0x20 or byte > 0x7E for byte in tag_bytes):
            _reject("FONT_TABLE_TAG_INVALID")
        tag = tag_bytes.decode("ascii")
        if tag in records:
            _reject("FONT_TABLE_DUPLICATE")
        if length == 0 or length > MAX_TABLE_BYTES:
            _reject("FONT_TABLE_SIZE_LIMIT")
        if table_offset % 4 or table_offset < directory_end:
            _reject("FONT_TABLE_BOUNDS")
        table_end = table_offset + length
        if table_end > len(raw):
            _reject("FONT_TABLE_BOUNDS")
        record = _TableRecord(tag, checksum, table_offset, length)
        records[tag] = record
        padded_end = (table_end + 3) & ~3
        if padded_end > len(raw) or any(raw[table_end:padded_end]):
            _reject("FONT_TABLE_BOUNDS")
        intervals.append((table_offset, padded_end, tag))

    intervals.sort()
    for previous, current in zip(intervals, intervals[1:]):
        if current[0] < previous[1]:
            _reject("FONT_TABLE_OVERLAP")
    prefix_gap = raw[directory_end : intervals[0][0]]
    if any(prefix_gap):
        _reject("FONT_TABLE_GAP_NONZERO")
    if len(prefix_gap) > MAX_DIRECTORY_ALIGNMENT_GAP:
        _reject("FONT_TABLE_GAP")
    for previous, current in zip(intervals, intervals[1:]):
        gap = raw[previous[1] : current[0]]
        if any(gap):
            _reject("FONT_TABLE_GAP_NONZERO")
        if len(gap) > MAX_INTER_TABLE_GAP:
            _reject("FONT_TABLE_GAP")
    if intervals[-1][1] != len(raw):
        _reject("FONT_TRAILING_DATA")

    forbidden = sorted(_DISALLOWED_TABLES.intersection(records))
    if forbidden:
        _reject("FONT_TABLE_FORBIDDEN")
    container, mime = identity
    required = _COMMON_REQUIRED | _CONTAINER_REQUIRED[container]
    if not required.issubset(records):
        _reject("FONT_REQUIRED_TABLE_MISSING")
    if container == "ttf" and "CFF " in records:
        _reject("FONT_OUTLINE_CONTAINER_MISMATCH")
    if container == "otf" and ({"glyf", "loca"} & records.keys()):
        _reject("FONT_OUTLINE_CONTAINER_MISMATCH")

    for record in records.values():
        data = raw[record.offset : record.offset + record.length]
        actual = _checksum(data, zero_head_adjustment=record.tag == "head")
        if actual != record.checksum:
            _reject("FONT_TABLE_CHECKSUM")
    if _checksum(raw) != 0xB1B0AFBA:
        _reject("FONT_FILE_CHECKSUM")
    return container, mime, records


def _validate_name_storage(raw: bytes, record: _TableRecord) -> None:
    data = raw[record.offset : record.offset + record.length]
    if len(data) > MAX_NAME_TABLE_BYTES or len(data) < 6:
        _reject("FONT_NAME_LIMIT")
    name_format, count, string_offset = struct.unpack_from(">HHH", data)
    if name_format not in {0, 1} or count > MAX_NAME_RECORDS:
        _reject("FONT_NAME_LIMIT")
    records_end = 6 + 12 * count
    if records_end > len(data) or string_offset < records_end or string_offset > len(data):
        _reject("FONT_NAME_INVALID")
    for index in range(count):
        _platform, _encoding, _language, _name_id, length, offset = struct.unpack_from(
            ">HHHHHH", data, 6 + 12 * index
        )
        if length > MAX_NAME_STRING_BYTES:
            _reject("FONT_NAME_LIMIT")
        if string_offset + offset + length > len(data):
            _reject("FONT_NAME_INVALID")
    if name_format == 1:
        if records_end + 2 > string_offset:
            _reject("FONT_NAME_INVALID")
        language_count = struct.unpack_from(">H", data, records_end)[0]
        if language_count > MAX_NAME_RECORDS:
            _reject("FONT_NAME_LIMIT")
        language_end = records_end + 2 + 4 * language_count
        if language_end > string_offset:
            _reject("FONT_NAME_INVALID")
        for index in range(language_count):
            length, offset = struct.unpack_from(">HH", data, records_end + 2 + 4 * index)
            if length > MAX_NAME_STRING_BYTES or string_offset + offset + length > len(data):
                _reject("FONT_NAME_INVALID")


def _validate_fvar(raw: bytes, record: _TableRecord | None) -> tuple[int, int]:
    if record is None:
        return 0, 0
    data = raw[record.offset : record.offset + record.length]
    if len(data) < 16:
        _reject("FONT_FVAR_INVALID")
    major, minor, axes_offset, reserved, axis_count, axis_size, instance_count, instance_size = (
        struct.unpack_from(">HHHHHHHH", data)
    )
    if major != 1 or minor != 0 or reserved != 2 or axes_offset < 16:
        _reject("FONT_FVAR_INVALID")
    if not 0 < axis_count <= MAX_AXES:
        _reject("FONT_AXIS_LIMIT")
    if instance_count > MAX_INSTANCES:
        _reject("FONT_INSTANCE_LIMIT")
    if axis_size < 20 or axis_size > 64:
        _reject("FONT_FVAR_INVALID")
    minimum_instance_size = 4 + axis_count * 4
    if instance_size not in {minimum_instance_size, minimum_instance_size + 2}:
        _reject("FONT_FVAR_INVALID")
    required_end = axes_offset + axis_count * axis_size + instance_count * instance_size
    if required_end > len(data):
        _reject("FONT_FVAR_INVALID")
    tags: set[bytes] = set()
    for index in range(axis_count):
        offset = axes_offset + index * axis_size
        tag = data[offset : offset + 4]
        minimum, default, maximum, flags, _name_id = struct.unpack_from(">iiiHH", data, offset + 4)
        if (
            len(tag) != 4
            or any(byte < 0x20 or byte > 0x7E for byte in tag)
            or tag in tags
            or not minimum <= default <= maximum
            or flags & ~0x0001
        ):
            _reject("FONT_FVAR_INVALID")
        tags.add(tag)
    return axis_count, instance_count


def _validate_cmap_storage(raw: bytes, record: _TableRecord) -> None:
    data = raw[record.offset : record.offset + record.length]
    if len(data) < 4:
        _reject("FONT_CMAP_INVALID")
    version, count = struct.unpack_from(">HH", data)
    if version != 0 or not 0 < count <= MAX_CMAP_SUBTABLES or 4 + 8 * count > len(data):
        _reject("FONT_CMAP_INVALID")
    encodings: set[tuple[int, int]] = set()
    has_unicode = False
    for index in range(count):
        platform, encoding, offset = struct.unpack_from(">HHI", data, 4 + 8 * index)
        key = (platform, encoding)
        if key in encodings or offset < 4 + 8 * count or offset + 4 > len(data):
            _reject("FONT_CMAP_INVALID")
        encodings.add(key)
        has_unicode = has_unicode or platform == 0 or (platform == 3 and encoding in {1, 10})
        cmap_format = struct.unpack_from(">H", data, offset)[0]
        if cmap_format in {0, 2, 4, 6}:
            length = struct.unpack_from(">H", data, offset + 2)[0]
        elif cmap_format in {10, 12, 13}:
            if offset + 8 > len(data):
                _reject("FONT_CMAP_INVALID")
            length = struct.unpack_from(">I", data, offset + 4)[0]
        elif cmap_format == 14:
            if offset + 6 > len(data):
                _reject("FONT_CMAP_INVALID")
            length = struct.unpack_from(">I", data, offset + 2)[0]
        else:
            _reject("FONT_CMAP_INVALID")
        if length < 4 or offset + length > len(data):
            _reject("FONT_CMAP_INVALID")
    if not has_unicode:
        _reject("FONT_CMAP_INVALID")


def _validate_maxp_storage(raw: bytes, record: _TableRecord, container: str) -> int:
    data = raw[record.offset : record.offset + record.length]
    minimum_length = 32 if container == "ttf" else 6
    expected_version = 0x00010000 if container == "ttf" else 0x00005000
    if len(data) < minimum_length or struct.unpack_from(">I", data)[0] != expected_version:
        _reject("FONT_MAXP_INVALID")
    glyph_count = struct.unpack_from(">H", data, 4)[0]
    if not 0 < glyph_count <= MAX_GLYPHS:
        _reject("FONT_GLYPH_COUNT_LIMIT")
    return glyph_count


def _clean_name(value: Any, code: str) -> str:
    if not isinstance(value, str):
        _reject(code)
    normalized = unicodedata.normalize("NFC", value).strip()
    if (
        not normalized
        or len(normalized) > MAX_METADATA_CHARACTERS
        or "\ufffd" in normalized
        or any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in normalized)
    ):
        _reject(code)
    return normalized


def _codepoint(value: int) -> str:
    return f"U+{value:04X}" if value <= 0xFFFF else f"U+{value:06X}"


def _ranges(codepoints: tuple[int, ...]) -> tuple[tuple[str, str], ...]:
    if not codepoints:
        return ()
    result: list[tuple[str, str]] = []
    start = previous = codepoints[0]
    for value in codepoints[1:]:
        if value != previous + 1:
            result.append((_codepoint(start), _codepoint(previous)))
            start = value
        previous = value
    result.append((_codepoint(start), _codepoint(previous)))
    return tuple(result)


def _script(value: int) -> str:
    if 0x0041 <= value <= 0x024F or 0x1E00 <= value <= 0x1EFF:
        return "Latin"
    if 0x3400 <= value <= 0x4DBF or 0x4E00 <= value <= 0x9FFF or 0x20000 <= value <= 0x323AF:
        return "CJK"
    if 0x3040 <= value <= 0x309F:
        return "Hiragana"
    if 0x30A0 <= value <= 0x30FF:
        return "Katakana"
    if 0xAC00 <= value <= 0xD7AF:
        return "Hangul"
    if 0x0370 <= value <= 0x03FF:
        return "Greek"
    if 0x0400 <= value <= 0x052F:
        return "Cyrillic"
    if 0x0590 <= value <= 0x05FF:
        return "Hebrew"
    if 0x0600 <= value <= 0x06FF:
        return "Arabic"
    if 0x0900 <= value <= 0x097F:
        return "Devanagari"
    return "Other"


def _is_variation_selector(value: int) -> bool:
    return 0xFE00 <= value <= 0xFE0F or 0xE0100 <= value <= 0xE01EF


def _variation_sequences(
    font: TTFont,
    glyph_names: set[str],
    cmap: dict[int, str],
) -> dict[tuple[int, int], tuple[str, str]]:
    cmap_table = font["cmap"]
    if not 0 < len(cmap_table.tables) <= MAX_CMAP_SUBTABLES:
        _reject("FONT_CMAP_LIMIT")
    total_mappings = 0
    sequences: dict[tuple[int, int], tuple[str, str]] = {}
    for subtable in cmap_table.tables:
        mappings = getattr(subtable, "cmap", None)
        if mappings is not None:
            if not isinstance(mappings, dict):
                _reject("FONT_CMAP_INVALID")
            total_mappings += len(mappings)
            for value, glyph_name in mappings.items():
                if (
                    not isinstance(value, int)
                    or not 0 <= value <= 0x10FFFF
                    or 0xD800 <= value <= 0xDFFF
                    or not isinstance(glyph_name, str)
                    or glyph_name == ".notdef"
                    or glyph_name not in glyph_names
                ):
                    _reject("FONT_CMAP_INVALID")
        if getattr(subtable, "format", None) != 14:
            continue
        uvs_dict = getattr(subtable, "uvsDict", None)
        if not isinstance(uvs_dict, dict):
            _reject("FONT_CMAP_INVALID")
        for selector, entries in uvs_dict.items():
            if not isinstance(selector, int) or not _is_variation_selector(selector):
                _reject("FONT_CMAP_INVALID")
            if not isinstance(entries, list):
                _reject("FONT_CMAP_INVALID")
            total_mappings += len(entries)
            for base, glyph_name in entries:
                if (
                    not isinstance(base, int)
                    or not 0 <= base <= 0x10FFFF
                    or 0xD800 <= base <= 0xDFFF
                ):
                    _reject("FONT_CMAP_INVALID")
                key = (base, selector)
                if key in sequences:
                    _reject("FONT_CMAP_INVALID")
                if glyph_name is None:
                    resolved = cmap.get(base)
                    mapping = "default"
                else:
                    resolved = glyph_name
                    mapping = "nondefault"
                if resolved is None or resolved == ".notdef" or resolved not in glyph_names:
                    _reject("FONT_CMAP_INVALID")
                sequences[key] = (resolved, mapping)
    if total_mappings > MAX_CMAP_MAPPINGS * 4:
        _reject("FONT_CMAP_LIMIT")
    return sequences


def _validate_outlines(font: TTFont, container: str, glyph_order: list[str]) -> None:
    if container == "ttf":
        glyf = font["glyf"]
        graph: dict[str, tuple[str, ...]] = {}
        total_points = 0
        for glyph_name in glyph_order:
            glyph = glyf[glyph_name]
            glyph.expand(glyf)
            program = getattr(glyph, "program", None)
            if program is not None and len(program.getBytecode()) > MAX_INSTRUCTION_BYTES:
                _reject("FONT_GLYPH_COMPLEXITY_LIMIT")
            if glyph.isComposite():
                components = tuple(component.glyphName for component in glyph.components)
                if len(components) > MAX_COMPONENTS_PER_GLYPH:
                    _reject("FONT_GLYPH_COMPLEXITY_LIMIT")
                if any(component not in glyf for component in components):
                    _reject("FONT_GLYPH_INVALID")
                graph[glyph_name] = components
                continue
            contour_count = max(0, int(getattr(glyph, "numberOfContours", 0)))
            coordinates = getattr(glyph, "coordinates", ())
            point_count = len(coordinates)
            if contour_count > MAX_CONTOURS_PER_GLYPH or point_count > MAX_POINTS_PER_GLYPH:
                _reject("FONT_GLYPH_COMPLEXITY_LIMIT")
            total_points += point_count
            if total_points > MAX_TOTAL_OUTLINE_POINTS:
                _reject("FONT_GLYPH_COMPLEXITY_LIMIT")
            graph[glyph_name] = ()

        memo: dict[str, int] = {}
        simple_points: dict[str, int] = {}
        for glyph_name in glyph_order:
            if not graph[glyph_name]:
                simple_points[glyph_name] = len(getattr(glyf[glyph_name], "coordinates", ()))
        expanded_memo: dict[str, int] = {}

        def expanded(glyph_name: str) -> int:
            """Points the renderer touches for this glyph once components are expanded (memoised,
            so a composite that references the same child many times is charged many times)."""
            cached = expanded_memo.get(glyph_name)
            if cached is not None:
                return cached
            children = graph[glyph_name]
            result = simple_points.get(glyph_name, 0) if not children else sum(expanded(c) for c in children)
            if children and result > MAX_COMPOSITE_EXPANDED_POINTS:
                _reject("FONT_GLYPH_COMPLEXITY_LIMIT")
            expanded_memo[glyph_name] = result
            return result

        def depth(glyph_name: str, path: frozenset[str]) -> int:
            cached = memo.get(glyph_name)
            if cached is not None:
                return cached
            if glyph_name in path:
                _reject("FONT_GLYPH_INVALID")
            children = graph[glyph_name]
            if not children:
                result = 0
            else:
                result = 1 + max(depth(child, path | {glyph_name}) for child in children)
            if result > MAX_COMPOSITE_DEPTH:
                _reject("FONT_GLYPH_COMPLEXITY_LIMIT")
            memo[glyph_name] = result
            return result

        total_expanded = 0
        for glyph_name in glyph_order:
            depth(glyph_name, frozenset())  # rejects cycles before expansion is charged
        for glyph_name in glyph_order:
            total_expanded += expanded(glyph_name)
            if total_expanded > MAX_TOTAL_OUTLINE_POINTS:
                _reject("FONT_GLYPH_COMPLEXITY_LIMIT")
        return

    class CountingPen:
        def __init__(self, glyph_names: set[str], running: list[int] | None = None) -> None:
            self.glyph_names = glyph_names
            self.points = 0
            self.components = 0
            self.component_names: list[str] = []  # seac base/accent references (endchar with 4 args)
            self.running = running  # [total so far] shared across glyphs → abort mid-draw, not after

        def _points(self, values: tuple[tuple[float, float], ...]) -> None:
            self.points += len(values)
            if self.points > MAX_POINTS_PER_GLYPH:
                _reject("FONT_GLYPH_COMPLEXITY_LIMIT")
            if self.running is not None and self.running[0] + self.points > MAX_TOTAL_OUTLINE_POINTS:
                _reject("FONT_GLYPH_COMPLEXITY_LIMIT")
            for point in values:
                if (
                    not isinstance(point, tuple)
                    or len(point) != 2
                    or any(
                        not isinstance(value, (int, float))
                        or not math.isfinite(value)
                        or abs(value) > 1_000_000
                        for value in point
                    )
                ):
                    _reject("FONT_GLYPH_INVALID")

        def moveTo(self, point: tuple[float, float]) -> None:  # noqa: N802
            self._points((point,))

        def lineTo(self, point: tuple[float, float]) -> None:  # noqa: N802
            self._points((point,))

        def curveTo(self, *points: tuple[float, float]) -> None:  # noqa: N802
            self._points(points)

        def qCurveTo(self, *points: tuple[float, float]) -> None:  # noqa: N802
            self._points(points)

        def closePath(self) -> None:  # noqa: N802
            return

        def endPath(self) -> None:  # noqa: N802
            return

        def addComponent(  # noqa: N802
            self,
            glyph_name: str,
            transformation: tuple[float, float, float, float, float, float],
        ) -> None:
            self.components += 1
            self.component_names.append(glyph_name)
            if (
                self.components > MAX_COMPONENTS_PER_GLYPH
                or glyph_name not in self.glyph_names
                or len(transformation) != 6
                or any(
                    not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or abs(value) > 1_000_000
                    for value in transformation
                )
            ):
                _reject("FONT_GLYPH_INVALID")

    cff = font["CFF "].cff
    if len(cff.topDictIndex) != 1:
        _reject("FONT_CFF_INVALID")
    top_dict = cff.topDictIndex[0]
    charstrings = top_dict.CharStrings
    if len(charstrings) != len(glyph_order):
        _reject("FONT_CFF_INVALID")
    global_subrs = getattr(top_dict, "GlobalSubrs", ())
    # CID-keyed CFF (every CJK OpenType font) keeps local subrs per FDArray entry and maps
    # glyphs to them through FDSelect; a name-keyed font has one Private dict (2026-10-22).
    fd_array = getattr(top_dict, "FDArray", None)
    if fd_array is not None:
        local_tables = [getattr(getattr(fd, "Private", None), "Subrs", ()) or () for fd in fd_array]
        fd_select = getattr(top_dict, "FDSelect", None)
        if fd_select is None or len(fd_select) != len(glyph_order):
            _reject("FONT_CFF_INVALID")
        fd_of = [int(fd_select[i]) for i in range(len(glyph_order))]
        if any(not 0 <= fd < len(local_tables) for fd in fd_of):
            _reject("FONT_CFF_INVALID")
    else:
        local_tables = [getattr(getattr(top_dict, "Private", None), "Subrs", ()) or ()]
        fd_of = [0] * len(glyph_order)
    if len(global_subrs) > MAX_CFF_SUBROUTINES or any(len(t) > MAX_CFF_SUBROUTINES for t in local_tables):
        _reject("FONT_GLYPH_COMPLEXITY_LIMIT")
    total_bytes = 0
    # Snapshot raw bytecode now: fontTools drops ``.bytecode`` once a charstring/subr has been
    # decompiled by a draw, and the walker must see the same bytes the interpreter will run.
    global_codes = [getattr(sr, "bytecode", None) for sr in global_subrs]
    local_codes = [[getattr(sr, "bytecode", None) for sr in table] for table in local_tables]
    for subroutine in (*global_subrs, *(sr for table in local_tables for sr in table)):
        bytecode = getattr(subroutine, "bytecode", None)
        if not isinstance(bytecode, bytes) or len(bytecode) > MAX_CFF_CHARSTRING_BYTES:
            _reject("FONT_GLYPH_COMPLEXITY_LIMIT")
        total_bytes += len(bytecode)
        if total_bytes > MAX_CFF_TOTAL_CHARSTRING_BYTES:
            _reject("FONT_GLYPH_COMPLEXITY_LIMIT")
    budget = [0]  # operators executed by the bounded walker across the whole font
    running = [0]
    cff_points: dict[str, int] = {}
    cff_graph: dict[str, tuple[str, ...]] = {}
    glyph_names = set(glyph_order)
    for glyph_index, glyph_name in enumerate(glyph_order):
        charstring = charstrings[glyph_name]
        bytecode = getattr(charstring, "bytecode", None)
        if not isinstance(bytecode, bytes) or len(bytecode) > MAX_CFF_CHARSTRING_BYTES:
            _reject("FONT_GLYPH_COMPLEXITY_LIMIT")
        total_bytes += len(bytecode)
        if total_bytes > MAX_CFF_TOTAL_CHARSTRING_BYTES:
            _reject("FONT_GLYPH_COMPLEXITY_LIMIT")
        # Bounded walk first: follow every subr call with the real stem context, depth ≤ spec,
        # no cycles, ≤ MAX_CFF_EXPANDED_OPS per glyph and ≤ MAX_CFF_FONT_EXPANDED_OPS per font —
        # only then is fontTools' interpreter allowed to run, and it aborts the moment the point
        # budget is hit.
        _cff_walk(bytecode, global_codes, local_codes[fd_of[glyph_index]], budget)
        pen = CountingPen(glyph_names, running)
        charstring.draw(pen)
        running[0] += pen.points
        if running[0] > MAX_TOTAL_OUTLINE_POINTS:
            _reject("FONT_GLYPH_COMPLEXITY_LIMIT")
        cff_points[glyph_name] = pen.points
        cff_graph[glyph_name] = tuple(pen.component_names)
    # seac (endchar with 4 operands) references two other glyphs as components; the renderer
    # expands them, so they are charged, depth-limited and cycle-checked exactly like TrueType
    # composites (Codex review 2026-10-22, round 4).
    _charge_component_expansion(glyph_order, cff_points, cff_graph)


def _charge_component_expansion(glyph_order: list[str], points: dict[str, int], graph: dict[str, tuple[str, ...]]) -> int:
    """Expanded outline points across a glyph set whose glyphs may reference other glyphs.
    A reference is billed every point of the referenced glyph (memoised), nesting is capped at
    MAX_COMPOSITE_DEPTH, cycles are rejected, one referencing glyph may expand to at most
    MAX_COMPOSITE_EXPANDED_POINTS and the whole set to MAX_TOTAL_OUTLINE_POINTS."""
    depth_memo: dict[str, int] = {}
    expanded_memo: dict[str, int] = {}

    def depth(name: str, path: frozenset[str]) -> int:
        cached = depth_memo.get(name)
        if cached is not None:
            return cached
        if name in path:
            _reject("FONT_GLYPH_INVALID")
        children = graph.get(name, ())
        if any(child not in graph for child in children):
            _reject("FONT_GLYPH_INVALID")
        result = 0 if not children else 1 + max(depth(child, path | {name}) for child in children)
        if result > MAX_COMPOSITE_DEPTH:
            _reject("FONT_GLYPH_COMPLEXITY_LIMIT")
        depth_memo[name] = result
        return result

    def expanded(name: str) -> int:
        cached = expanded_memo.get(name)
        if cached is not None:
            return cached
        children = graph.get(name, ())
        result = points.get(name, 0) + sum(expanded(child) for child in children)
        if children and result > MAX_COMPOSITE_EXPANDED_POINTS:
            _reject("FONT_GLYPH_COMPLEXITY_LIMIT")
        expanded_memo[name] = result
        return result

    total = 0
    for name in glyph_order:
        depth(name, frozenset())
    for name in glyph_order:
        total += expanded(name)
        if total > MAX_TOTAL_OUTLINE_POINTS:
            _reject("FONT_GLYPH_COMPLEXITY_LIMIT")
    return total


_T2_OPS = {
    1: "hstem", 3: "vstem", 4: "vmoveto", 5: "rlineto", 6: "hlineto", 7: "vlineto", 8: "rrcurveto",
    10: "callsubr", 11: "return", 14: "endchar", 18: "hstemhm", 19: "hintmask", 20: "cntrmask",
    21: "rmoveto", 22: "hmoveto", 23: "vstemhm", 24: "rcurveline", 25: "rlinecurve", 26: "vvcurveto",
    27: "hhcurveto", 29: "callgsubr", 30: "vhcurveto", 31: "hvcurveto",
}


_T2_ESC_FLEX = {34: 7, 35: 13, 36: 9, 37: 11}  # hflex, flex, hflex1, flex1 → fixed operand count


class _T2State:
    __slots__ = ("stack", "stems", "ops", "mask_bytes")

    def __init__(self) -> None:
        self.stack: list[float] = []
        self.stems = 0
        self.ops = 0
        self.mask_bytes: int | None = None  # fixed at the first hintmask/cntrmask, like fontTools


def _cff_walk(bytecode: bytes, global_subrs: list[Any], local_subrs: list[Any], budget: list[int]) -> int:
    """Walk one Type 2 charstring, following subroutine calls with the glyph's own stem context
    (hintmask length depends on it), without drawing anything. ``global_subrs``/``local_subrs``
    are lists of raw bytecode. Rejects recursive subrs, nesting
    deeper than MAX_CFF_SUBR_DEPTH, unknown operators, and any glyph whose expanded operator
    count exceeds MAX_CFF_EXPANDED_OPS; the per-font budget bounds the walker itself."""
    from fontTools.misc.psCharStrings import calcSubrBias

    gbias = calcSubrBias(global_subrs) if len(global_subrs) else 0
    lbias = calcSubrBias(local_subrs) if len(local_subrs) else 0
    state = _T2State()

    def run(code: bytes, depth: int, path: tuple[Any, ...]) -> None:
        """Scan one bytecode to its end (return/endchar do not stop the scan, see below)."""
        i = 0
        n = len(code)
        while i < n:
            b0 = code[i]
            if b0 >= 32 or b0 == 28:
                if b0 == 28:
                    if i + 2 >= n:
                        _reject("FONT_CFF_INVALID")
                    value = int.from_bytes(code[i + 1:i + 3], "big", signed=True); i += 3
                elif b0 <= 246:
                    value = b0 - 139; i += 1
                elif b0 <= 250:
                    if i + 1 >= n:
                        _reject("FONT_CFF_INVALID")
                    value = (b0 - 247) * 256 + code[i + 1] + 108; i += 2
                elif b0 <= 254:
                    if i + 1 >= n:
                        _reject("FONT_CFF_INVALID")
                    value = -(b0 - 251) * 256 - code[i + 1] - 108; i += 2
                else:
                    if i + 4 >= n:
                        _reject("FONT_CFF_INVALID")
                    value = int.from_bytes(code[i + 1:i + 5], "big", signed=True) / 65536.0; i += 5
                state.stack.append(value)
                if len(state.stack) > 513:  # Type 2 argument stack limit (CFF2 raised it; CFF2 is rejected)
                    _reject("FONT_CFF_INVALID")
                continue
            state.ops += 1
            budget[0] += 1
            if state.ops > MAX_CFF_EXPANDED_OPS or budget[0] > MAX_CFF_FONT_EXPANDED_OPS:
                _reject("FONT_GLYPH_COMPLEXITY_LIMIT")
            if b0 == 12:
                if i + 1 >= n:
                    _reject("FONT_CFF_INVALID")
                # Only the flex family is modelled (it consumes every argument). Arithmetic /
                # storage escapes could compute a subr index the walker cannot see, so they are
                # rejected outright rather than approximated (Codex review 2026-10-22).
                expected = _T2_ESC_FLEX.get(code[i + 1])
                if expected is None or len(state.stack) != expected:
                    _reject("FONT_CFF_INVALID")  # flex takes a fixed operand count (Codex round 5 minor)
                state.stack.clear(); i += 2
                continue
            op = _T2_OPS.get(b0)
            if op is None:
                _reject("FONT_CFF_INVALID")
            i += 1
            if op in ("callsubr", "callgsubr"):
                if not state.stack:
                    _reject("FONT_CFF_INVALID")
                index = int(state.stack.pop()) + (lbias if op == "callsubr" else gbias)
                table = local_subrs if op == "callsubr" else global_subrs
                if not 0 <= index < len(table):
                    _reject("FONT_CFF_INVALID")
                key = (op, index)
                if key in path:
                    _reject("FONT_CFF_INVALID")  # recursive subroutine
                if depth + 1 > MAX_CFF_SUBR_DEPTH:
                    _reject("FONT_GLYPH_COMPLEXITY_LIMIT")
                sub = table[index]  # raw bytecode snapshots
                if not isinstance(sub, bytes):
                    _reject("FONT_CFF_INVALID")
                run(sub, depth + 1, path + (key,))
                continue
            if op == "return":
                # fontTools 4.62.1: op_return is a no-op that keeps the operand stack (subrs
                # routinely leave numbers for the caller) and the scan continues to the end of
                # the bytecode, so a call hidden behind return still executes — the walker
                # mirrors both (Codex review 2026-10-22, round 3).
                continue
            if op == "endchar":
                state.stack.clear()  # outline extractor pops everything; scanning continues
                continue
            if op in ("hstem", "vstem", "hstemhm", "vstemhm"):
                state.stems += len(state.stack) // 2  # counted even after a mask, as fontTools does
                state.stack.clear()
                continue
            if op in ("hintmask", "cntrmask"):
                # Type 2 / fontTools: operands before a mask count as an implicit vstem; the mask
                # width is frozen the first time it is non-zero (a zero-width first mask leaves it
                # uninitialised, exactly like fontTools), and later masks keep that width so
                # operands pushed before them can never stretch a mask over a following operator.
                state.stems += len(state.stack) // 2
                state.stack.clear()
                if state.mask_bytes is None:
                    width = (state.stems + 7) // 8
                    if width:
                        state.mask_bytes = width
                else:
                    width = state.mask_bytes
                i += width
                if i > n:
                    _reject("FONT_CFF_INVALID")
                continue
            state.stack.clear()

    run(bytecode, 0, ())
    return state.ops


def _coverage_trace(
    required_text: str,
    cmap: dict[int, str],
    variation_sequences: dict[tuple[int, int], tuple[str, str]],
) -> tuple[
    tuple[dict[str, Any], ...], tuple[dict[str, str], ...]
]:
    glyphs: list[dict[str, Any]] = []
    ignored: list[dict[str, str]] = []
    missing: list[str] = []
    cluster = -1
    join_next = False
    previous_scalar: int | None = None
    for character in required_text:
        value = ord(character)
        category = unicodedata.category(character)
        if character.isspace() or category.startswith("Z"):
            ignored.append({"codepoint": _codepoint(value), "reason": "whitespace"})
            join_next = False
            previous_scalar = None
            continue
        if category in {"Cc", "Cf"}:
            ignored.append({"codepoint": _codepoint(value), "reason": "control-or-format"})
            join_next = value == 0x200D
            previous_scalar = None
            continue
        if category in {"Cs", "Cn"}:
            missing.append(_codepoint(value))
            continue
        if _is_variation_selector(value):
            resolved = variation_sequences.get((previous_scalar, value))
            if resolved is None:
                missing.append(_codepoint(value))
            else:
                glyph_name, mapping = resolved
                glyphs.append(
                    {
                        "cluster": cluster,
                        "codepoint": _codepoint(value),
                        "glyph_name": glyph_name,
                        "variation_selector_for": _codepoint(previous_scalar),
                        "mapping": mapping,
                    }
                )
            previous_scalar = None
            continue
        if not unicodedata.combining(character) and not join_next:
            cluster += 1
        if cluster < 0:
            cluster = 0
        join_next = False
        glyph_name = cmap.get(value)
        if glyph_name is None:
            missing.append(_codepoint(value))
            previous_scalar = value
            continue
        glyphs.append(
            {
                "cluster": cluster,
                "codepoint": _codepoint(value),
                "glyph_name": glyph_name,
            }
        )
        previous_scalar = value
    if missing:
        _reject("FONT_REQUIRED_GLYPH_MISSING", missing=tuple(dict.fromkeys(missing)))
    return tuple(glyphs), tuple(ignored)


def validate_font_bytes(
    raw: bytes,
    required_text: str = "",
    *,
    license_spdx: str | None = None,
    declared_mime: str | None = None,
) -> FontValidationResult:
    """Validate one standalone sfnt TTF/OTF and required NFC text.

    Unicode separators and Cc/Cf controls do not require glyphs and are listed
    in ``ignored_characters``.  Every other assigned scalar must map to a
    non-.notdef glyph.  Variation selectors require an explicit cmap format 14
    sequence and otherwise fail closed.
    """

    if license_spdx is None:
        _reject("FONT_LICENSE_REQUIRED")
    if not isinstance(license_spdx, str) or license_spdx not in ALLOWED_FONT_LICENSES:
        _reject("FONT_LICENSE_NOT_ALLOWED")
    if not isinstance(required_text, str):
        _reject("FONT_REQUIRED_TEXT_INVALID")
    if FONTTOOLS_RUNTIME_VERSION != PINNED_FONTTOOLS_VERSION:
        _reject("FONTTOOLS_VERSION_MISMATCH")
    required_text_nfc = unicodedata.normalize("NFC", required_text)
    if len(required_text_nfc) > MAX_REQUIRED_TEXT_CHARACTERS:
        _reject("FONT_REQUIRED_TEXT_LIMIT")
    container, mime, records = _read_directory(raw)
    if declared_mime is not None and (not isinstance(declared_mime, str) or declared_mime != mime):
        _reject("FONT_MIME_MISMATCH")
    _validate_name_storage(raw, records["name"])
    _validate_cmap_storage(raw, records["cmap"])
    declared_glyph_count = _validate_maxp_storage(raw, records["maxp"], container)
    axis_count, instance_count = _validate_fvar(raw, records.get("fvar"))

    try:
        font = TTFont(
            BytesIO(raw),
            lazy=False,
            recalcBBoxes=False,
            recalcTimestamp=False,
            checkChecksums=0,
            ignoreDecompileErrors=False,
        )
        font.ensureDecompiled(recurse=True)
        glyph_order = font.getGlyphOrder()
        if len(glyph_order) != declared_glyph_count or font["maxp"].numGlyphs != len(glyph_order):
            _reject("FONT_GLYPH_COUNT_LIMIT")
        if (
            not glyph_order
            or glyph_order[0] != ".notdef"
            or len(set(glyph_order)) != len(glyph_order)
            or any(
                not isinstance(name, str)
                or not name
                or len(name) > MAX_GLYPH_NAME_CHARACTERS
                or any(ord(character) < 0x21 or ord(character) > 0x7E for character in name)
                for name in glyph_order
            )
        ):
            _reject("FONT_GLYPH_ORDER_INVALID")
        _validate_outlines(font, container, glyph_order)

        name_table = font["name"]
        name_keys: set[tuple[int, int, int, int]] = set()
        for name_record in name_table.names:
            key = (
                name_record.platformID,
                name_record.platEncID,
                name_record.langID,
                name_record.nameID,
            )
            if key in name_keys:
                _reject("FONT_NAME_INVALID")
            name_keys.add(key)
            decoded = name_record.toUnicode()
            if (
                not isinstance(decoded, str)
                or "\ufffd" in decoded
                or any(unicodedata.category(character) == "Cs" for character in decoded)
            ):
                _reject("FONT_NAME_INVALID")
        family = _clean_name(name_table.getBestFamilyName(), "FONT_FAMILY_INVALID")
        subfamily = _clean_name(name_table.getBestSubFamilyName(), "FONT_SUBFAMILY_INVALID")
        os2 = font["OS/2"]
        weight = os2.usWeightClass
        if not isinstance(weight, int) or not 1 <= weight <= 1000:
            _reject("FONT_WEIGHT_INVALID")
        fs_type = os2.fsType
        if not isinstance(fs_type, int) or fs_type & ~_ALLOWED_FSTYPE_MASK:
            _reject("FONT_EMBEDDING_RESTRICTED")
        low_embedding_bits = fs_type & 0x000E
        if low_embedding_bits not in {0, 0x0004, 0x0008} or fs_type & 0x0200:
            _reject("FONT_EMBEDDING_RESTRICTED")
        italic = bool(getattr(os2, "fsSelection", 0) & 0x0001)
        style = "italic" if italic else "normal"

        cmap = font.getBestCmap()
        if not isinstance(cmap, dict) or not cmap:
            _reject("FONT_CMAP_INVALID")
        glyph_names = set(glyph_order)
        canonical_cmap: dict[int, str] = {}
        for value, glyph_name in cmap.items():
            if (
                not isinstance(value, int)
                or not 0 <= value <= 0x10FFFF
                or 0xD800 <= value <= 0xDFFF
                or not isinstance(glyph_name, str)
                or glyph_name == ".notdef"
                or glyph_name not in glyph_names
            ):
                _reject("FONT_CMAP_INVALID")
            canonical_cmap[value] = glyph_name
        if len(canonical_cmap) > MAX_CMAP_MAPPINGS:
            _reject("FONT_CMAP_LIMIT")
        codepoints = tuple(sorted(canonical_cmap))
        variation_sequences = _variation_sequences(font, glyph_names, canonical_cmap)
        required_glyphs, ignored = _coverage_trace(
            required_text_nfc,
            canonical_cmap,
            variation_sequences,
        )
    except FontSecurityError:
        raise
    except Exception as exc:
        raise FontSecurityError("FONT_PARSER_REJECTED") from exc
    finally:
        if "font" in locals():
            font.close()

    digest = hashlib.sha256(raw).hexdigest()
    scripts = tuple(sorted({_script(value) for value in codepoints}))
    coverage_sha256 = hashlib.sha256(
        b"".join(struct.pack(">I", value) for value in codepoints)
    ).hexdigest()
    required_coverage_sha256 = hashlib.sha256(
        _canonical_json(
            {
                "required_glyphs": required_glyphs,
                "ignored_characters": ignored,
            }
        )
    ).hexdigest()
    receipt = {
        "receipt_version": RECEIPT_VERSION,
        "validator_version": FONT_VALIDATOR_VERSION,
        "policy_version": POLICY_VERSION,
        "fonttools_version": FONTTOOLS_RUNTIME_VERSION,
        "limits_sha256": LIMITS_SHA256,
        "font_sha256": digest,
        "container": container,
        "mime": mime,
        "declared_mime_verified": declared_mime is not None,
        "byte_length": len(raw),
        "license_spdx": license_spdx,
        "axis_count": axis_count,
        "instance_count": instance_count,
        "embedding_fs_type": fs_type,
        "required_text_nfc_sha256": hashlib.sha256(required_text_nfc.encode("utf-8")).hexdigest(),
        "unicode_version": unicodedata.unidata_version,
        "unicode_coverage_count": len(codepoints),
        "unicode_coverage_sha256": coverage_sha256,
        "required_coverage_sha256": required_coverage_sha256,
        "system_fallback_allowed": False,
        "hinting_executed": False,
    }
    return FontValidationResult(
        container=container,
        mime=mime,
        byte_length=len(raw),
        sha256=digest,
        family=family,
        subfamily=subfamily,
        style=style,
        weight=weight,
        embedding_fs_type=fs_type,
        glyph_count=len(glyph_order),
        unicode_codepoints=codepoints,
        unicode_ranges=_ranges(codepoints),
        scripts=scripts,
        required_text_nfc=required_text_nfc,
        required_glyphs=required_glyphs,
        ignored_characters=ignored,
        receipt=receipt,
    )


__all__ = [
    "FONT_VALIDATOR_VERSION",
    "POLICY_VERSION",
    "RECEIPT_VERSION",
    "PINNED_FONTTOOLS_VERSION",
    "LIMITS_SHA256",
    "ALLOWED_FONT_LICENSES",
    "FontSecurityError",
    "FontValidationResult",
    "validate_font_bytes",
]
