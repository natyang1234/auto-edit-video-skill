#!/usr/bin/env python3
"""Fail-closed SVG security core.

Only canonical SVG bytes produced here may be handed to a rasterizer.  Browser
and timeline callers must use the validated PNG result, never either SVG form.
This module intentionally uses only the Python standard library.
"""

from __future__ import annotations

import binascii
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import struct
import subprocess
import tempfile
import time
from typing import Any, Callable, Mapping
import unicodedata
import xml.etree.ElementTree as ET
import zlib


POLICY_VERSION = "svg-threat-model/2"
SANITIZER_VERSION = "svg-security/1"
SVG_NAMESPACE = "http://www.w3.org/2000/svg"

MAX_RAW_BYTES = 2 * 1024 * 1024
MAX_DEPTH = 32
MAX_ELEMENTS = 5_000
MAX_ATTRIBUTES = 20_000
MAX_TEXT = 64 * 1024
MAX_PATH_BYTES = 1024 * 1024
MAX_PATH_COMMANDS = 20_000
MAX_COORDINATE = Decimal("1000000")
MAX_RASTER_SIDE = 4096
MAX_RASTER_PIXELS = 16 * 1024 * 1024
MAX_PNG_BYTES = 64 * 1024 * 1024
MAX_REFERENCE_DEPTH = 8

RESVG_MANIFEST_ENV = "AUTO_EDIT_VIDEO_RESVG_MANIFEST"
DEFAULT_RESVG_MANIFEST_PATH = Path(
    os.path.expanduser("~/.config/auto-edit-video/resvg-manifest.json")
).absolute()
BUNDLED_SANDBOX_PROFILE = (
    Path(__file__).resolve().parents[1] / "contracts/policies/RESVG_SANDBOX.sb"
).resolve()
BUNDLED_SANDBOX_PROFILE_SHA256 = (
    "fdeafff37a24abd03bc8f4069c88c8366e6cebbfd693874da9fd18b3cbae1420"
)
try:
    BUNDLED_SANDBOX_PROFILE_BYTES = BUNDLED_SANDBOX_PROFILE.read_bytes()
except OSError:
    BUNDLED_SANDBOX_PROFILE_BYTES = b""

SANDBOX_EXECUTABLE = Path("/usr/bin/sandbox-exec")
RASTER_TIMEOUT_SECONDS = 5.0
MAX_RASTER_LOG_BYTES = 64 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
# Python cannot safely install per-child RLIMIT_AS from a threaded macOS
# service without preexec_fn.  The adapter therefore makes no memory-cap claim.
RASTER_MEMORY_LIMIT_ENFORCED = False

_LIMITS = {
    "raw_bytes": MAX_RAW_BYTES,
    "depth": MAX_DEPTH,
    "elements": MAX_ELEMENTS,
    "attributes": MAX_ATTRIBUTES,
    "text": MAX_TEXT,
    "path_bytes": MAX_PATH_BYTES,
    "path_commands": MAX_PATH_COMMANDS,
    "raster_side": MAX_RASTER_SIDE,
    "raster_pixels": MAX_RASTER_PIXELS,
    "reference_depth": MAX_REFERENCE_DEPTH,
}


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


LIMITS_SHA256 = hashlib.sha256(_canonical_json(_LIMITS)).hexdigest()


class SvgSecurityError(ValueError):
    """Stable, non-reflective policy failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SanitizeResult:
    canonical_svg: bytes
    raw_sha256: str
    sanitized_sha256: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class PNGValidationResult:
    width: int
    height: int
    color_type: int
    byte_length: int
    png_sha256: str


@dataclass(frozen=True)
class RasterizerPreflight:
    available: bool
    checks_ok: bool
    code: str
    identity: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RasterizedPNG:
    png_bytes: bytes
    png_sha256: str
    width: int
    height: int
    metadata: dict[str, Any]


@dataclass
class _Reference:
    attribute: str
    target: str
    expected: frozenset[str]


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    references: list[_Reference]
    children: list["_Node"] = field(default_factory=list)
    text: str = ""


_ELEMENTS = frozenset(
    {
        "svg", "g", "defs", "path", "rect", "circle", "ellipse", "line",
        "polyline", "polygon", "linearGradient", "radialGradient", "stop",
        "clipPath", "title", "desc",
    }
)
_COMMON = frozenset(
    {
        "id", "transform", "color", "opacity", "fill", "fill-opacity",
        "fill-rule", "clip-rule", "stroke", "stroke-width", "stroke-opacity",
        "stroke-linecap", "stroke-linejoin", "stroke-miterlimit",
        "stroke-dasharray", "stroke-dashoffset", "clip-path",
    }
)
_ATTRS: dict[str, frozenset[str]] = {
    "svg": _COMMON | {"viewBox", "width", "height", "preserveAspectRatio"},
    "g": _COMMON,
    "defs": frozenset({"id"}),
    "path": _COMMON | {"d"},
    "rect": _COMMON | {"x", "y", "width", "height", "rx", "ry"},
    "circle": _COMMON | {"cx", "cy", "r"},
    "ellipse": _COMMON | {"cx", "cy", "rx", "ry"},
    "line": _COMMON | {"x1", "y1", "x2", "y2"},
    "polyline": _COMMON | {"points"},
    "polygon": _COMMON | {"points"},
    "linearGradient": frozenset(
        {"id", "x1", "y1", "x2", "y2", "gradientUnits", "gradientTransform", "spreadMethod"}
    ),
    "radialGradient": frozenset(
        {"id", "cx", "cy", "r", "fx", "fy", "fr", "gradientUnits", "gradientTransform", "spreadMethod"}
    ),
    "stop": frozenset({"id", "offset", "stop-color", "stop-opacity"}),
    "clipPath": frozenset({"id", "clipPathUnits", "transform", "clip-path"}),
    "title": frozenset(),
    "desc": frozenset(),
}

_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}\Z")
_LOCAL_URL = re.compile(r"url\(#([A-Za-z_][A-Za-z0-9_.-]{0,63})\)\Z")
_HEX = re.compile(r"#([0-9A-Fa-f]{3,8})\Z")
_PATH_COMMAND = frozenset("MmLlHhVvCcSsQqTtAaZz")
_PATH_ARITY = {
    "M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4,
    "Q": 4, "T": 2, "A": 7, "Z": 0,
}
_NAMED_COLORS = {
    "black": "#000000", "white": "#ffffff", "red": "#ff0000",
    "green": "#008000", "blue": "#0000ff",
}
_ATTR_ORDER = [
    "id", "viewBox", "width", "height", "preserveAspectRatio", "color",
    "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry",
    "fx", "fy", "fr", "points", "d", "transform", "gradientUnits",
    "gradientTransform", "spreadMethod", "offset", "opacity", "fill",
    "fill-opacity", "fill-rule", "clip-rule", "stroke", "stroke-width",
    "stroke-opacity", "stroke-linecap", "stroke-linejoin", "stroke-miterlimit",
    "stroke-dasharray", "stroke-dashoffset", "clip-path", "clipPathUnits",
    "stop-color", "stop-opacity",
]
_ATTR_RANK = {name: index for index, name in enumerate(_ATTR_ORDER)}


def _reject(code: str) -> None:
    raise SvgSecurityError(code)


def _decimal(token: str, *, nonnegative: bool = False) -> Decimal:
    if _NUMBER.fullmatch(token) is None:
        _reject("SVG_NUMBER_INVALID")
    try:
        value = Decimal(token)
    except InvalidOperation:
        _reject("SVG_NUMBER_INVALID")
    tup = value.as_tuple()
    if not value.is_finite() or len(tup.digits) > 15 or tup.exponent < -9:
        _reject("SVG_NUMBER_INVALID")
    if abs(value) > MAX_COORDINATE or (nonnegative and value < 0):
        _reject("SVG_GEOMETRY_LIMIT")
    return Decimal(0) if value == 0 else value


def _number_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0", "+0"} else text


def _number_tokens(value: str) -> list[str]:
    tokens: list[str] = []
    cursor = 0
    for match in _NUMBER.finditer(value):
        gap = value[cursor : match.start()]
        if cursor == 0:
            valid_gap = not gap or gap.isspace()
        else:
            valid_gap = (
                not gap and match.group(0)[0] in "+-"
            ) or re.fullmatch(r"\s*(?:,\s*)?", gap) is not None
        if not valid_gap or re.search(r",\s*,", gap):
            _reject("SVG_NUMBER_INVALID")
        tokens.append(match.group(0))
        cursor = match.end()
    if not tokens or (value[cursor:] and not value[cursor:].isspace()):
        _reject("SVG_NUMBER_INVALID")
    return tokens


def _number_list(value: str, *, count: int | None = None, nonnegative: bool = False) -> list[Decimal]:
    values = [_decimal(token, nonnegative=nonnegative) for token in _number_tokens(value)]
    if count is not None and len(values) != count:
        _reject("SVG_NUMBER_INVALID")
    return values


def _canon_number(value: str, *, nonnegative: bool = False) -> str:
    return _number_text(_decimal(value.strip(), nonnegative=nonnegative))


def _canon_number_or_percent(value: str, *, bounded: bool = False) -> str:
    stripped = value.strip()
    percent = stripped.endswith("%")
    number = _decimal(stripped[:-1] if percent else stripped)
    if bounded:
        upper = Decimal(100) if percent else Decimal(1)
        if number < 0 or number > upper:
            _reject("SVG_GEOMETRY_LIMIT")
    return _number_text(number) + ("%" if percent else "")


def _canon_length(value: str, *, positive: bool = False) -> str:
    stripped = value.strip()
    if stripped.endswith("px"):
        stripped = stripped[:-2]
    number = _decimal(stripped, nonnegative=True)
    if positive and number <= 0:
        _reject("SVG_GEOMETRY_LIMIT")
    return _number_text(number)


def _canon_color(value: str, *, allow_none: bool = True) -> str:
    stripped = value.strip()
    if stripped == "currentColor":
        return stripped
    lower = stripped.casefold()
    if allow_none and lower == "none":
        return "none"
    if lower in _NAMED_COLORS:
        return _NAMED_COLORS[lower]
    match = _HEX.fullmatch(stripped)
    if match is None or len(match.group(1)) not in {3, 4, 6, 8}:
        _reject("SVG_PAINT_INVALID")
    digits = match.group(1).lower()
    if len(digits) in {3, 4}:
        digits = "".join(char * 2 for char in digits)
    return "#" + digits


def _canon_opacity(value: str) -> str:
    number = _decimal(value.strip())
    if number < 0 or number > 1:
        _reject("SVG_GEOMETRY_LIMIT")
    return _number_text(number)


def _canon_transform(value: str) -> str:
    cursor = 0
    rendered: list[str] = []
    operations = 0
    cumulative = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

    def multiply(
        first: tuple[float, float, float, float, float, float],
        second: tuple[float, float, float, float, float, float],
    ) -> tuple[float, float, float, float, float, float]:
        a1, b1, c1, d1, e1, f1 = first
        a2, b2, c2, d2, e2, f2 = second
        return (
            a1 * a2 + c1 * b2,
            b1 * a2 + d1 * b2,
            a1 * c2 + c1 * d2,
            b1 * c2 + d1 * d2,
            a1 * e2 + c1 * f2 + e1,
            b1 * e2 + d1 * f2 + f1,
        )

    while cursor < len(value):
        separator = re.match(r"[\s,]*", value[cursor:])
        assert separator is not None
        cursor += separator.end()
        if cursor == len(value):
            break
        name_match = re.match(r"(matrix|translate|scale|rotate|skewX|skewY)\s*\(", value[cursor:])
        if name_match is None:
            _reject("SVG_TRANSFORM_INVALID")
        name = name_match.group(1)
        cursor += name_match.end()
        close = value.find(")", cursor)
        if close < 0:
            _reject("SVG_TRANSFORM_INVALID")
        numbers = _number_list(value[cursor:close])
        arities = {
            "matrix": {6}, "translate": {1, 2}, "scale": {1, 2},
            "rotate": {1, 3}, "skewX": {1}, "skewY": {1},
        }
        if len(numbers) not in arities[name]:
            _reject("SVG_TRANSFORM_INVALID")
        if name in {"skewX", "skewY"}:
            tangent = math.tan(math.radians(float(numbers[0])))
            if not math.isfinite(tangent) or abs(tangent) > 1_000_000:
                _reject("SVG_GEOMETRY_LIMIT")
        floats = [float(item) for item in numbers]
        if name == "matrix":
            operation = tuple(floats)
        elif name == "translate":
            operation = (1.0, 0.0, 0.0, 1.0, floats[0], floats[1] if len(floats) == 2 else 0.0)
        elif name == "scale":
            operation = (floats[0], 0.0, 0.0, floats[-1], 0.0, 0.0)
        elif name == "rotate":
            cosine = math.cos(math.radians(floats[0]))
            sine = math.sin(math.radians(floats[0]))
            rotation = (cosine, sine, -sine, cosine, 0.0, 0.0)
            if len(floats) == 3:
                to_center = (1.0, 0.0, 0.0, 1.0, floats[1], floats[2])
                from_center = (1.0, 0.0, 0.0, 1.0, -floats[1], -floats[2])
                operation = multiply(multiply(to_center, rotation), from_center)
            else:
                operation = rotation
        elif name == "skewX":
            operation = (1.0, 0.0, tangent, 1.0, 0.0, 0.0)
        else:
            operation = (1.0, tangent, 0.0, 1.0, 0.0, 0.0)
        cumulative = multiply(cumulative, operation)  # type: ignore[arg-type]
        if any(not math.isfinite(item) or abs(item) > 1_000_000 for item in cumulative):
            _reject("SVG_GEOMETRY_LIMIT")
        operations += 1
        if operations > 32:
            _reject("SVG_GEOMETRY_LIMIT")
        rendered.append(f"{name}({' '.join(_number_text(item) for item in numbers)})")
        cursor = close + 1
    if not rendered:
        _reject("SVG_TRANSFORM_INVALID")
    return " ".join(rendered)


def _canon_path(value: str, metrics: dict[str, int]) -> str:
    encoded = len(value.encode("utf-8"))
    metrics["path_bytes"] += encoded
    if metrics["path_bytes"] > MAX_PATH_BYTES or encoded > 256 * 1024:
        _reject("SVG_PATH_DATA_LIMIT")
    tokens: list[str] = []
    cursor = 0
    while cursor < len(value):
        if value[cursor].isspace() or value[cursor] == ",":
            start = cursor
            comma_count = 0
            while cursor < len(value) and (value[cursor].isspace() or value[cursor] == ","):
                comma_count += value[cursor] == ","
                cursor += 1
            if comma_count > 1:
                _reject("SVG_PATH_INVALID")
            if start == 0 and comma_count:
                _reject("SVG_PATH_INVALID")
            continue
        char = value[cursor]
        if char.isalpha():
            if char not in _PATH_COMMAND:
                _reject("SVG_PATH_INVALID")
            tokens.append(char)
            cursor += 1
            continue
        match = _NUMBER.match(value, cursor)
        if match is None:
            _reject("SVG_PATH_INVALID")
        tokens.append(match.group(0))
        cursor = match.end()
    if not tokens:
        _reject("SVG_PATH_INVALID")

    groups: list[str] = []
    index = 0
    while index < len(tokens):
        command = tokens[index]
        if command not in _PATH_COMMAND:
            _reject("SVG_PATH_INVALID")
        index += 1
        raw_numbers: list[str] = []
        while index < len(tokens) and tokens[index] not in _PATH_COMMAND:
            raw_numbers.append(tokens[index])
            index += 1
        arity = _PATH_ARITY[command.upper()]
        if arity == 0:
            if raw_numbers:
                _reject("SVG_PATH_INVALID")
            segments = 1
            canonical_numbers: list[str] = []
        else:
            if not raw_numbers or len(raw_numbers) % arity:
                _reject("SVG_PATH_INVALID")
            canonical_numbers = []
            for position, token in enumerate(raw_numbers):
                number = _decimal(token)
                if command.upper() == "A" and position % 7 in {3, 4} and number not in {0, 1}:
                    _reject("SVG_PATH_INVALID")
                canonical_numbers.append(_number_text(number))
            segments = len(raw_numbers) // arity
        metrics["path_commands"] += segments
        if metrics["path_commands"] > MAX_PATH_COMMANDS:
            _reject("SVG_PATH_COMMAND_LIMIT")
        groups.append(command + (" ".join(canonical_numbers) if canonical_numbers else ""))
    return "".join(groups)


def _tag_name(raw: str) -> tuple[str | None, str]:
    if raw.startswith("{"):
        namespace, local = raw[1:].split("}", 1)
        return namespace, local
    return None, raw


def _attribute_name(raw: str) -> tuple[str | None, str]:
    return _tag_name(raw)


def _validate_attrs(tag: str, raw_attrs: Mapping[str, str], metrics: dict[str, int]) -> tuple[dict[str, str], list[_Reference]]:
    attrs: dict[str, str] = {}
    refs: list[_Reference] = []
    for raw_name, value in raw_attrs.items():
        namespace, name = _attribute_name(raw_name)
        lowered = name.casefold()
        if lowered.startswith("on"):
            _reject("SVG_EVENT_HANDLER")
        if lowered == "href":
            _reject("SVG_URL_FORBIDDEN")
        if lowered in {"style", "class"}:
            _reject("SVG_CSS_FORBIDDEN")
        if namespace is not None:
            _reject("SVG_NAMESPACE_FORBIDDEN")
        if name not in _ATTRS[tag]:
            _reject("SVG_ATTRIBUTE_FORBIDDEN")
        if len(value.encode("utf-8")) > (MAX_PATH_BYTES if name == "d" else 8192):
            _reject("SVG_ATTRIBUTE_VALUE_LIMIT")

        if name == "id":
            if _ID.fullmatch(value) is None:
                _reject("SVG_ID_INVALID")
            canonical = value
        elif name == "d":
            canonical = _canon_path(value, metrics)
        elif name == "viewBox":
            numbers = _number_list(value, count=4)
            if numbers[2] <= 0 or numbers[3] <= 0:
                _reject("SVG_GEOMETRY_LIMIT")
            ratio = numbers[2] / numbers[3]
            if ratio < Decimal("0.001") or ratio > Decimal("1000"):
                _reject("SVG_GEOMETRY_LIMIT")
            canonical = " ".join(_number_text(item) for item in numbers)
        elif name in {"width", "height"}:
            canonical = _canon_length(value, positive=True)
            if tag == "svg" and Decimal(canonical) > MAX_RASTER_SIDE:
                _reject("SVG_GEOMETRY_LIMIT")
        elif name in {"r", "rx", "ry", "fr", "stroke-width", "stroke-miterlimit"}:
            canonical = _canon_number(value, nonnegative=True)
        elif name in {"x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "fx", "fy", "stroke-dashoffset"}:
            canonical = _canon_number_or_percent(value) if tag in {"linearGradient", "radialGradient"} else _canon_number(value)
        elif name == "points":
            numbers = _number_list(value)
            if len(numbers) % 2 or len(numbers) // 2 > 4096:
                _reject("SVG_GEOMETRY_LIMIT")
            canonical = " ".join(_number_text(item) for item in numbers)
        elif name in {"transform", "gradientTransform"}:
            canonical = _canon_transform(value)
        elif name in {"opacity", "fill-opacity", "stroke-opacity", "stop-opacity"}:
            canonical = _canon_opacity(value)
        elif name == "offset":
            canonical = _canon_number_or_percent(value, bounded=True)
        elif name in {"fill", "stroke"}:
            match = _LOCAL_URL.fullmatch(value)
            if match:
                canonical = f"url(#{match.group(1)})"
                refs.append(_Reference(name, match.group(1), frozenset({"linearGradient", "radialGradient"})))
            else:
                if "url" in value.casefold() or ":" in value:
                    _reject("SVG_URL_FORBIDDEN")
                canonical = _canon_color(value)
        elif name == "clip-path":
            match = _LOCAL_URL.fullmatch(value)
            if match is None:
                _reject("SVG_URL_FORBIDDEN")
            canonical = f"url(#{match.group(1)})"
            refs.append(_Reference(name, match.group(1), frozenset({"clipPath"})))
        elif name in {"color", "stop-color"}:
            canonical = _canon_color(value, allow_none=False)
        elif name == "stroke-dasharray":
            if value.strip().casefold() == "none":
                canonical = "none"
            else:
                numbers = _number_list(value, nonnegative=True)
                if len(numbers) > 64:
                    _reject("SVG_GEOMETRY_LIMIT")
                canonical = " ".join(_number_text(item) for item in numbers)
        elif name in {"fill-rule", "clip-rule"}:
            if value not in {"nonzero", "evenodd"}:
                _reject("SVG_ATTRIBUTE_VALUE_INVALID")
            canonical = value
        elif name == "stroke-linecap":
            if value not in {"butt", "round", "square"}:
                _reject("SVG_ATTRIBUTE_VALUE_INVALID")
            canonical = value
        elif name == "stroke-linejoin":
            if value not in {"miter", "round", "bevel"}:
                _reject("SVG_ATTRIBUTE_VALUE_INVALID")
            canonical = value
        elif name == "gradientUnits":
            if value not in {"objectBoundingBox", "userSpaceOnUse"}:
                _reject("SVG_ATTRIBUTE_VALUE_INVALID")
            canonical = value
        elif name == "clipPathUnits":
            if value not in {"userSpaceOnUse", "objectBoundingBox"}:
                _reject("SVG_ATTRIBUTE_VALUE_INVALID")
            canonical = value
        elif name == "spreadMethod":
            if value not in {"pad", "reflect", "repeat"}:
                _reject("SVG_ATTRIBUTE_VALUE_INVALID")
            canonical = value
        elif name == "preserveAspectRatio":
            if re.fullmatch(r"(?:none|x(?:Min|Mid|Max)Y(?:Min|Mid|Max))(?: (?:meet|slice))?", value) is None:
                _reject("SVG_ATTRIBUTE_VALUE_INVALID")
            canonical = value
        else:
            _reject("SVG_ATTRIBUTE_FORBIDDEN")
        attrs[name] = canonical
    return attrs, refs


def _lexical_gate(raw: bytes) -> str:
    if not raw:
        _reject("SVG_EMPTY")
    if len(raw) > MAX_RAW_BYTES:
        _reject("SVG_RAW_TOO_LARGE")
    if raw.startswith((b"\x1f\x8b", b"PK\x03\x04")):
        _reject("SVG_CONTAINER_FORBIDDEN")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _reject("SVG_ENCODING_INVALID")
    if text.startswith("\ufeff"):
        text = text[1:]
    if "\x00" in text or any(ord(char) < 32 and char not in "\t\n\r" for char in text):
        _reject("SVG_ENCODING_INVALID")
    declaration = re.match(
        r"\A<\?xml\s+version=(['\"])(?:1\.0|1\.1)\1(?:\s+encoding=(['\"])UTF-8\2)?(?:\s+standalone=(['\"])(?:yes|no)\3)?\s*\?>",
        text,
        flags=re.IGNORECASE,
    )
    scan = text[declaration.end():] if declaration else text
    upper = scan.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        _reject("SVG_DTD_FORBIDDEN")
    if "<!--" in scan or "<![CDATA[" in upper or "<!" in scan:
        _reject("SVG_XML_DECLARATION_FORBIDDEN")
    if "<?" in scan:
        _reject("SVG_PROCESSING_INSTRUCTION")
    return text


def _parse_svg(text: str) -> tuple[_Node, dict[str, int]]:
    parser = ET.XMLPullParser(events=("start", "end"))
    stack: list[tuple[_Node, ET.Element]] = []
    root: _Node | None = None
    namespace_mode: str | None | object = object()
    metrics = {"elements": 0, "attributes": 0, "text": 0, "path_bytes": 0, "path_commands": 0}

    try:
        for start in range(0, len(text), 16 * 1024):
            parser.feed(text[start : start + 16 * 1024])
            for event, element in parser.read_events():
                namespace, tag = _tag_name(element.tag)
                if event == "start":
                    metrics["elements"] += 1
                    metrics["attributes"] += len(element.attrib)
                    if metrics["elements"] > MAX_ELEMENTS:
                        _reject("SVG_ELEMENT_LIMIT")
                    if metrics["attributes"] > MAX_ATTRIBUTES:
                        _reject("SVG_ATTRIBUTE_LIMIT")
                    if len(stack) + 1 > MAX_DEPTH:
                        _reject("SVG_DEPTH_LIMIT")
                    if not stack:
                        if tag != "svg":
                            _reject("SVG_ROOT_INVALID")
                        if namespace not in {None, SVG_NAMESPACE}:
                            _reject("SVG_NAMESPACE_FORBIDDEN")
                        namespace_mode = namespace
                    elif namespace != namespace_mode:
                        _reject("SVG_NAMESPACE_FORBIDDEN")
                    if tag not in _ELEMENTS:
                        _reject("SVG_ELEMENT_FORBIDDEN")
                    attrs, refs = _validate_attrs(tag, element.attrib, metrics)
                    node = _Node(tag, attrs, refs)
                    if stack:
                        stack[-1][0].children.append(node)
                    else:
                        root = node
                    stack.append((node, element))
                else:
                    if not stack:
                        _reject("SVG_XML_PARSE")
                    node, source = stack.pop()
                    if source is not element:
                        _reject("SVG_XML_PARSE")
                    own_text = element.text or ""
                    tail = element.tail or ""
                    if node.tag in {"title", "desc"}:
                        if node.children:
                            _reject("SVG_ELEMENT_FORBIDDEN")
                        normalized = unicodedata.normalize("NFC", " ".join(own_text.split()))
                        if len(normalized) > 1024:
                            _reject("SVG_TEXT_LIMIT")
                        node.text = normalized
                    elif own_text.strip():
                        _reject("SVG_TEXT_FORBIDDEN")
                    if tail.strip():
                        _reject("SVG_TEXT_FORBIDDEN")
                    metrics["text"] += len(own_text) + len(tail)
                    if metrics["text"] > MAX_TEXT:
                        _reject("SVG_TEXT_LIMIT")
                    element.clear()
        parser.close()
        for event, _element in parser.read_events():
            if event:
                _reject("SVG_XML_PARSE")
    except SvgSecurityError:
        raise
    except (ET.ParseError, ValueError, UnicodeError):
        _reject("SVG_XML_PARSE")
    if root is None or stack:
        _reject("SVG_XML_PARSE")
    if "viewBox" not in root.attrs:
        width = root.attrs.get("width", "300")
        height = root.attrs.get("height", "150")
        root.attrs["viewBox"] = f"0 0 {width} {height}"
    root.attrs.setdefault("color", "#000000")
    return root, metrics


def _validate_references(root: _Node) -> tuple[int, int]:
    by_id: dict[str, _Node] = {}
    order: list[_Node] = []

    def collect(node: _Node) -> None:
        order.append(node)
        node_id = node.attrs.get("id")
        if node_id is not None:
            if node_id in by_id:
                _reject("SVG_ID_DUPLICATE")
            by_id[node_id] = node
        for child in node.children:
            collect(child)

    collect(root)
    edges: dict[str, set[str]] = {}
    reference_count = 0

    def link(node: _Node, owner: str) -> None:
        nonlocal reference_count
        node_id = node.attrs.get("id")
        current_owner = node_id or owner
        for reference in node.references:
            reference_count += 1
            target = by_id.get(reference.target)
            if target is None:
                _reject("SVG_REFERENCE_UNRESOLVED")
            if target.tag not in reference.expected:
                _reject("SVG_REFERENCE_TARGET")
            edges.setdefault(current_owner, set()).add(reference.target)
        for child in node.children:
            link(child, current_owner)

    link(root, "@root")
    visiting: set[str] = set()
    longest_cache: dict[str, int] = {}

    def longest_chain(identity: str) -> int:
        if identity in visiting:
            _reject("SVG_REFERENCE_CYCLE")
        if identity in longest_cache:
            return longest_cache[identity]
        visiting.add(identity)
        longest = 0
        for target in edges.get(identity, set()):
            longest = max(longest, 1 + longest_chain(target))
        visiting.remove(identity)
        longest_cache[identity] = longest
        return longest

    for identity in tuple(edges):
        if longest_chain(identity) > MAX_REFERENCE_DEPTH:
            _reject("SVG_REFERENCE_DEPTH")

    renamed = {old: f"s{index:04d}" for index, old in enumerate(by_id, start=1)}
    for node in order:
        old_id = node.attrs.get("id")
        if old_id is not None:
            node.attrs["id"] = renamed[old_id]
        for reference in node.references:
            node.attrs[reference.attribute] = f"url(#{renamed[reference.target]})"
    return len(by_id), reference_count


def _serialize(node: _Node, *, root: bool = False) -> str:
    attributes: list[tuple[str, str]] = []
    if root:
        attributes.append(("xmlns", SVG_NAMESPACE))
    attributes.extend(
        sorted(node.attrs.items(), key=lambda item: (_ATTR_RANK.get(item[0], 10_000), item[0]))
    )
    rendered = "".join(
        f' {name}="{html.escape(value, quote=True)}"' for name, value in attributes
    )
    body = html.escape(node.text, quote=False) + "".join(_serialize(child) for child in node.children)
    return f"<{node.tag}{rendered}>{body}</{node.tag}>" if body else f"<{node.tag}{rendered}/>"


def _validate_dimensions(width: int, height: int) -> None:
    if (
        isinstance(width, bool) or isinstance(height, bool)
        or not isinstance(width, int) or not isinstance(height, int)
        or not 1 <= width <= MAX_RASTER_SIDE or not 1 <= height <= MAX_RASTER_SIDE
        or width * height > MAX_RASTER_PIXELS
    ):
        _reject("SVG_RASTER_DIMENSIONS")


def sanitize_svg_bytes(raw: bytes, *, requested_width: int, requested_height: int) -> SanitizeResult:
    """Parse hostile SVG bytes and return a newly-built canonical tree."""
    _validate_dimensions(requested_width, requested_height)
    if not isinstance(raw, bytes):
        _reject("SVG_INPUT_TYPE")
    text = _lexical_gate(raw)
    root, metrics = _parse_svg(text)
    id_count, reference_count = _validate_references(root)
    canonical = _serialize(root, root=True).encode("utf-8")
    raw_hash = hashlib.sha256(raw).hexdigest()
    sanitized_hash = hashlib.sha256(canonical).hexdigest()
    cache_key = hashlib.sha256(
        _canonical_json(
            {
                "raw_sha256": raw_hash,
                "policy_version": POLICY_VERSION,
                "sanitizer_version": SANITIZER_VERSION,
                "limits_sha256": LIMITS_SHA256,
            }
        )
    ).hexdigest()
    metadata: dict[str, Any] = {
        "policy_version": POLICY_VERSION,
        "sanitizer_version": SANITIZER_VERSION,
        "limits_sha256": LIMITS_SHA256,
        "sanitize_cache_key_sha256": cache_key,
        "raw_sha256": raw_hash,
        "sanitized_sha256": sanitized_hash,
        "requested_width": requested_width,
        "requested_height": requested_height,
        "canonical_bytes": len(canonical),
        "id_count": id_count,
        "reference_count": reference_count,
        **metrics,
    }
    return SanitizeResult(canonical, raw_hash, sanitized_hash, metadata)


def validate_png_bytes(payload: bytes, *, expected_width: int, expected_height: int) -> PNGValidationResult:
    """Validate a non-interlaced 8-bit RGB/RGBA PNG and bounded scanlines."""
    _validate_dimensions(expected_width, expected_height)
    if not isinstance(payload, bytes) or len(payload) > MAX_PNG_BYTES:
        _reject("PNG_SIZE_LIMIT")
    signature = b"\x89PNG\r\n\x1a\n"
    if not payload.startswith(signature):
        _reject("PNG_SIGNATURE")
    cursor = len(signature)
    chunks: list[tuple[bytes, bytes]] = []
    ended = False
    while cursor < len(payload):
        if cursor + 12 > len(payload):
            _reject("PNG_CHUNK_FRAMING")
        length = struct.unpack(">I", payload[cursor : cursor + 4])[0]
        kind = payload[cursor + 4 : cursor + 8]
        end = cursor + 12 + length
        if end > len(payload) or len(kind) != 4 or not all(65 <= byte <= 90 or 97 <= byte <= 122 for byte in kind):
            _reject("PNG_CHUNK_FRAMING")
        data = payload[cursor + 8 : cursor + 8 + length]
        actual_crc = struct.unpack(">I", payload[cursor + 8 + length : end])[0]
        expected_crc = binascii.crc32(kind)
        expected_crc = binascii.crc32(data, expected_crc) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            _reject("PNG_CRC")
        chunks.append((kind, data))
        cursor = end
        if kind == b"IEND":
            ended = True
            break
    if not ended:
        _reject("PNG_CHUNK_ORDER")
    if cursor != len(payload):
        _reject("PNG_TRAILING_DATA")
    if not chunks or chunks[0][0] != b"IHDR" or len(chunks[0][1]) != 13:
        _reject("PNG_CHUNK_ORDER")
    if chunks[-1] != (b"IEND", b"") or sum(kind == b"IHDR" for kind, _ in chunks) != 1:
        _reject("PNG_CHUNK_ORDER")

    idat: list[bytes] = []
    idat_closed = False
    width = height = color_type = 0
    for index, (kind, data) in enumerate(chunks):
        critical = not bool(kind[0] & 0x20)
        if kind == b"IHDR":
            if index != 0:
                _reject("PNG_CHUNK_ORDER")
            width, height, bit_depth, color_type, compression, filter_method, interlace = struct.unpack(">IIBBBBB", data)
            if (width, height) != (expected_width, expected_height):
                _reject("PNG_DIMENSIONS")
            if bit_depth != 8 or color_type not in {2, 6} or compression or filter_method or interlace:
                _reject("PNG_IHDR_POLICY")
        elif kind == b"IDAT":
            if idat_closed:
                _reject("PNG_CHUNK_ORDER")
            idat.append(data)
        elif kind == b"IEND":
            idat_closed = True
        elif critical:
            _reject("PNG_CRITICAL_CHUNK")
        elif kind not in {b"sRGB", b"gAMA", b"pHYs"}:
            _reject("PNG_ANCILLARY_CHUNK")
        elif idat:
            idat_closed = True
    if not idat:
        _reject("PNG_CHUNK_ORDER")

    channels = 3 if color_type == 2 else 4
    row_size = 1 + channels * width
    expected_size = row_size * height
    inflater = zlib.decompressobj()
    inflated = bytearray()
    try:
        for compressed in idat:
            pending = compressed
            while pending:
                maximum = max(1, expected_size + 1 - len(inflated))
                inflated.extend(inflater.decompress(pending, maximum))
                if len(inflated) > expected_size:
                    _reject("PNG_INFLATE_SIZE")
                pending = inflater.unconsumed_tail
                if pending and len(inflated) >= expected_size:
                    _reject("PNG_INFLATE_SIZE")
        inflated.extend(inflater.flush(max(1, expected_size + 1 - len(inflated))))
    except zlib.error:
        _reject("PNG_ZLIB")
    if len(inflated) != expected_size or not inflater.eof or inflater.unused_data or inflater.unconsumed_tail:
        _reject("PNG_INFLATE_SIZE")
    for row in range(height):
        if inflated[row * row_size] not in {0, 1, 2, 3, 4}:
            _reject("PNG_FILTER")
    digest = hashlib.sha256(payload).hexdigest()
    return PNGValidationResult(width, height, color_type, len(payload), digest)


Probe = Callable[[Mapping[str, Any]], tuple[str, bytes]]
Runner = Callable[..., Any]

_MANIFEST_REQUIRED = frozenset(
    {
        "schema_version", "executable_path", "executable_sha256", "version",
        "sandbox_executable_path", "sandbox_executable_sha256",
        "sandbox_profile_path", "sandbox_profile_sha256",
    }
)


class _BoundedProcessError(RuntimeError):
    pass


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            process.kill()
        except ProcessLookupError:
            pass


def _run_bounded_process(
    argv: list[str],
    *,
    cwd: str,
    env: Mapping[str, str],
    timeout: float,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Run fixed argv while bounding time, logs, and any expected PNG file.

    No ``preexec_fn`` is used: it is unsafe in the threaded Studio process on
    macOS.  Output and log caps are actively supervised and always rechecked
    before bytes cross back into the caller.
    """
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
        start_new_session=True,
    )
    assert process.stdout is not None and process.stderr is not None
    selected = selectors.DefaultSelector()
    selected.register(process.stdout, selectors.EVENT_READ, "stdout")
    selected.register(process.stderr, selectors.EVENT_READ, "stderr")
    logs = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout
    try:
        while selected.get_map() or process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_process_group(process)
                raise TimeoutError
            if output_path is not None:
                try:
                    if output_path.stat().st_size > MAX_PNG_BYTES:
                        _kill_process_group(process)
                        raise _BoundedProcessError("output limit")
                except FileNotFoundError:
                    pass
            for key, _event in selected.select(min(0.05, remaining)):
                chunk = os.read(key.fileobj.fileno(), 8192)
                if not chunk:
                    selected.unregister(key.fileobj)
                    continue
                buffer = logs[key.data]
                if len(buffer) + len(chunk) > MAX_RASTER_LOG_BYTES:
                    _kill_process_group(process)
                    raise _BoundedProcessError("log limit")
                buffer.extend(chunk)
        returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as exc:
        _kill_process_group(process)
        raise TimeoutError from exc
    finally:
        selected.close()
        if process.poll() is None:
            _kill_process_group(process)
        process.wait()
        process.stdout.close()
        process.stderr.close()
    if output_path is not None:
        try:
            if output_path.stat().st_size > MAX_PNG_BYTES:
                raise _BoundedProcessError("output limit")
        except FileNotFoundError:
            pass
    return {
        "returncode": returncode,
        "stdout": bytes(logs["stdout"]),
        "stderr": bytes(logs["stderr"]),
    }


def _manifest_path(path: str | os.PathLike[str] | None) -> Path | None:
    if path is None:
        override = os.environ.get(RESVG_MANIFEST_ENV)
        path = override if override is not None else DEFAULT_RESVG_MANIFEST_PATH
    candidate = Path(path)
    if not candidate.is_absolute():
        return None
    return candidate


def load_resvg_manifest(
    path: str | os.PathLike[str] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Load a strict, owner-only machine manifest without following symlinks."""
    candidate = _manifest_path(path)
    if candidate is None:
        return None, "RASTERIZER_MANIFEST_INVALID"
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except FileNotFoundError:
        return None, "RASTERIZER_MANIFEST_MISSING"
    except OSError:
        return None, "RASTERIZER_MANIFEST_UNSAFE"
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size <= 0
            or info.st_size > MAX_MANIFEST_BYTES
        ):
            return None, "RASTERIZER_MANIFEST_UNSAFE"
        chunks = bytearray()
        while len(chunks) <= MAX_MANIFEST_BYTES:
            chunk = os.read(descriptor, min(8192, MAX_MANIFEST_BYTES + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        if len(chunks) > MAX_MANIFEST_BYTES:
            return None, "RASTERIZER_MANIFEST_UNSAFE"
    finally:
        os.close(descriptor)
    try:
        manifest = json.loads(
            bytes(chunks).decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_pairs,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None, "RASTERIZER_MANIFEST_INVALID"
    if type(manifest) is not dict or set(manifest) != _MANIFEST_REQUIRED:
        return None, "RASTERIZER_MANIFEST_INVALID"
    if (
        type(manifest.get("schema_version")) is not int
        or manifest["schema_version"] != 1
        or type(manifest.get("version")) is not str
    ):
        return None, "RASTERIZER_MANIFEST_INVALID"
    profile_path = manifest.get("sandbox_profile_path")
    if (
        not isinstance(profile_path, str)
        or not os.path.isabs(profile_path)
        or manifest.get("sandbox_profile_sha256") != BUNDLED_SANDBOX_PROFILE_SHA256
        or hashlib.sha256(BUNDLED_SANDBOX_PROFILE_BYTES).hexdigest()
        != BUNDLED_SANDBOX_PROFILE_SHA256
    ):
        return None, "RASTERIZER_MANIFEST_INVALID"
    try:
        configured_profile = Path(profile_path)
        profile_info = configured_profile.lstat()
        if (
            stat.S_ISLNK(profile_info.st_mode)
            or not stat.S_ISREG(profile_info.st_mode)
            or profile_info.st_uid != os.geteuid()
            or stat.S_IMODE(profile_info.st_mode) != 0o600
            or str(configured_profile.resolve(strict=True)) != profile_path
        ):
            return None, "RASTERIZER_MANIFEST_UNSAFE"
        profile_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        profile_fd = os.open(configured_profile, profile_flags)
        try:
            opened_profile_info = os.fstat(profile_fd)
            if (
                not stat.S_ISREG(opened_profile_info.st_mode)
                or opened_profile_info.st_uid != os.geteuid()
                or stat.S_IMODE(opened_profile_info.st_mode) != 0o600
                or opened_profile_info.st_size != len(BUNDLED_SANDBOX_PROFILE_BYTES)
            ):
                return None, "RASTERIZER_MANIFEST_UNSAFE"
            profile_bytes = bytearray()
            while len(profile_bytes) <= len(BUNDLED_SANDBOX_PROFILE_BYTES):
                chunk = os.read(profile_fd, 8192)
                if not chunk:
                    break
                profile_bytes.extend(chunk)
        finally:
            os.close(profile_fd)
        if bytes(profile_bytes) != BUNDLED_SANDBOX_PROFILE_BYTES:
            return None, "RASTERIZER_MANIFEST_INVALID"
    except OSError:
        return None, "RASTERIZER_MANIFEST_UNSAFE"
    if manifest.get("sandbox_executable_path") != str(SANDBOX_EXECUTABLE):
        return None, "RASTERIZER_MANIFEST_INVALID"
    return manifest, "OK"


def _reject_duplicate_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    del value
    raise ValueError("invalid JSON constant")


class ResvgRasterizer:
    """Manifest-gated rasterizer seam.

    There is intentionally no permissive binary discovery. Injected hooks are
    test-only and can pass checks, but never report production availability.
    """

    _PRODUCTION_TOKEN = object()

    def __init__(
        self,
        manifest: Mapping[str, Any] | None,
        *,
        probe: Probe | None = None,
        runner: Runner | None = None,
        _production_token: object | None = None,
        _load_error: str | None = None,
    ) -> None:
        self._manifest = dict(manifest) if manifest is not None else None
        self._probe = probe
        self._runner = runner
        self._production = _production_token is self._PRODUCTION_TOKEN
        self._load_error = _load_error

    @classmethod
    def from_machine_manifest(
        cls, path: str | os.PathLike[str] | None = None
    ) -> "ResvgRasterizer":
        manifest, code = load_resvg_manifest(path)
        return cls(
            manifest,
            _production_token=cls._PRODUCTION_TOKEN,
            _load_error=None if code == "OK" else code,
        )

    @classmethod
    def _production_for_configure(cls, manifest: Mapping[str, Any]) -> "ResvgRasterizer":
        """Private configure-time verifier; never selected by service injection."""
        return cls(manifest, _production_token=cls._PRODUCTION_TOKEN)

    @staticmethod
    def _safe_file(path_value: Any, expected_hash: Any, *, executable: bool) -> tuple[Path, str] | RasterizerPreflight:
        if not isinstance(path_value, str) or not os.path.isabs(path_value):
            return RasterizerPreflight(False, False, "RASTERIZER_MANIFEST_INVALID")
        path = Path(path_value)
        try:
            info = path.lstat()
        except OSError:
            return RasterizerPreflight(False, False, "RASTERIZER_EXECUTABLE_MISSING" if executable else "RASTERIZER_SANDBOX_FAILED")
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o022
            or (executable and not os.access(path, os.X_OK))
        ):
            return RasterizerPreflight(False, False, "RASTERIZER_EXECUTABLE_UNSAFE" if executable else "RASTERIZER_SANDBOX_FAILED")
        if not isinstance(expected_hash, str) or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None:
            return RasterizerPreflight(False, False, "RASTERIZER_MANIFEST_INVALID")
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return RasterizerPreflight(False, False, "RASTERIZER_SANDBOX_FAILED")
        if digest != expected_hash:
            return RasterizerPreflight(False, False, "RASTERIZER_HASH_MISMATCH")
        return path, digest

    def preflight(self) -> RasterizerPreflight:
        manifest = self._manifest
        if manifest is None:
            return RasterizerPreflight(
                False, False, self._load_error or "RASTERIZER_MANIFEST_MISSING"
            )
        if (
            set(manifest) != _MANIFEST_REQUIRED
            or type(manifest.get("schema_version")) is not int
            or manifest["schema_version"] != 1
            or type(manifest.get("version")) is not str
            or not manifest["version"]
            or len(manifest["version"]) > 200
            or any(ord(char) < 32 or ord(char) == 127 for char in manifest["version"])
        ):
            return RasterizerPreflight(False, False, "RASTERIZER_MANIFEST_INVALID")
        executable = self._safe_file(manifest["executable_path"], manifest["executable_sha256"], executable=True)
        if isinstance(executable, RasterizerPreflight):
            return executable
        sandbox = self._safe_file(manifest["sandbox_executable_path"], manifest["sandbox_executable_sha256"], executable=True)
        if isinstance(sandbox, RasterizerPreflight):
            return RasterizerPreflight(False, False, "RASTERIZER_SANDBOX_FAILED")
        profile = self._safe_file(manifest["sandbox_profile_path"], manifest["sandbox_profile_sha256"], executable=False)
        if isinstance(profile, RasterizerPreflight):
            return RasterizerPreflight(False, False, "RASTERIZER_SANDBOX_FAILED")
        try:
            profile_bytes = profile[0].read_bytes()
        except OSError:
            return RasterizerPreflight(False, False, "RASTERIZER_SANDBOX_FAILED")
        if b"(deny default)" not in profile_bytes or b"(allow network" in profile_bytes:
            return RasterizerPreflight(False, False, "RASTERIZER_SANDBOX_FAILED")
        if self._production:
            if (
                manifest["sandbox_executable_path"] != str(SANDBOX_EXECUTABLE)
                or manifest["sandbox_profile_sha256"] != BUNDLED_SANDBOX_PROFILE_SHA256
                or profile_bytes != BUNDLED_SANDBOX_PROFILE_BYTES
                or hashlib.sha256(profile_bytes).hexdigest()
                != BUNDLED_SANDBOX_PROFILE_SHA256
                or b"(deny network*)" not in profile_bytes
                or str(Path(manifest["executable_path"]).resolve(strict=True))
                != manifest["executable_path"]
            ):
                return RasterizerPreflight(False, False, "RASTERIZER_SANDBOX_FAILED")
            try:
                profile_info = Path(manifest["sandbox_profile_path"]).lstat()
            except OSError:
                return RasterizerPreflight(False, False, "RASTERIZER_SANDBOX_FAILED")
            if (
                profile_info.st_uid != os.geteuid()
                or stat.S_IMODE(profile_info.st_mode) != 0o600
            ):
                return RasterizerPreflight(False, False, "RASTERIZER_SANDBOX_FAILED")
            probe = self._production_probe
        elif self._probe is None:
            return RasterizerPreflight(False, False, "RASTERIZER_PROBE_UNAVAILABLE")
        else:
            probe = self._probe
        try:
            version, smoke_png = probe(manifest)
            if version != manifest["version"]:
                return RasterizerPreflight(False, False, "RASTERIZER_VERSION_MISMATCH")
            validate_png_bytes(smoke_png, expected_width=1, expected_height=1)
        except SvgSecurityError:
            return RasterizerPreflight(False, False, "RASTERIZER_SANDBOX_FAILED")
        except Exception:
            return RasterizerPreflight(False, False, "RASTERIZER_SANDBOX_FAILED")
        identity = {
            "version": str(manifest["version"]),
            "executable_sha256": executable[1],
            "sandbox_executable_sha256": sandbox[1],
            "sandbox_profile_sha256": profile[1],
        }
        if not self._production:
            return RasterizerPreflight(False, True, "RASTERIZER_TEST_BACKEND", identity)
        return RasterizerPreflight(True, True, "OK", identity)

    @staticmethod
    def _sandbox_argv(manifest: Mapping[str, Any], workdir: Path) -> list[str]:
        return [
            str(manifest["sandbox_executable_path"]),
            "-D", f"WORK_DIR={workdir}",
            "-D", f"RESVG_EXECUTABLE={manifest['executable_path']}",
            "-f", str(manifest["sandbox_profile_path"]),
            str(manifest["executable_path"]),
        ]

    @staticmethod
    def _fixed_env(workdir: Path) -> dict[str, str]:
        return {
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
            "TMPDIR": str(workdir),
            "HOME": str(workdir),
        }

    @classmethod
    def _production_probe(cls, manifest: Mapping[str, Any]) -> tuple[str, bytes]:
        with tempfile.TemporaryDirectory(prefix="auto-edit-svg-probe-") as directory:
            root = Path(directory).resolve(strict=True)
            os.chmod(root, 0o700)
            version_result = _run_bounded_process(
                cls._sandbox_argv(manifest, root) + ["--version"],
                cwd=str(root),
                env=cls._fixed_env(root),
                timeout=RASTER_TIMEOUT_SECONDS,
            )
            if version_result.get("returncode") != 0:
                raise _BoundedProcessError("version probe failed")
            stderr = version_result.get("stderr", b"")
            stdout = version_result.get("stdout", b"")
            if stderr or not isinstance(stdout, bytes):
                raise _BoundedProcessError("version probe output")
            try:
                version = stdout.decode("ascii").strip()
            except UnicodeDecodeError as exc:
                raise _BoundedProcessError("version encoding") from exc
            input_path = root / "input.svg"
            output_path = root / "output.png"
            input_path.write_bytes(
                b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1" width="1" height="1"><rect width="1" height="1" fill="#000000"/></svg>'
            )
            os.chmod(input_path, 0o600)
            result = _run_bounded_process(
                cls._raster_argv(manifest, root, input_path, output_path, 1, 1),
                cwd=str(root),
                env=cls._fixed_env(root),
                timeout=RASTER_TIMEOUT_SECONDS,
                output_path=output_path,
            )
            if result.get("returncode") != 0 or result.get("stdout") or result.get("stderr"):
                raise _BoundedProcessError("smoke raster failed")
            return version, output_path.read_bytes()

    @classmethod
    def _raster_argv(
        cls,
        manifest: Mapping[str, Any],
        root: Path,
        input_path: Path,
        output_path: Path,
        width: int,
        height: int,
    ) -> list[str]:
        return cls._sandbox_argv(manifest, root) + [
            "--quiet",
            "--skip-system-fonts",
            "--resources-dir", str(root),
            "--width", str(width),
            "--height", str(height),
            str(input_path),
            str(output_path),
        ]

    def rasterize(self, sanitized: SanitizeResult) -> RasterizedPNG:
        if not isinstance(sanitized, SanitizeResult):
            _reject("RASTERIZER_INPUT_INVALID")
        checked = self.preflight()
        if not checked.checks_ok or (not self._production and self._runner is None):
            _reject("RASTERIZER_UNAVAILABLE")
        width = sanitized.metadata["requested_width"]
        height = sanitized.metadata["requested_height"]
        with tempfile.TemporaryDirectory(prefix="auto-edit-svg-") as directory:
            root = Path(directory).resolve(strict=True)
            os.chmod(root, 0o700)
            input_path = root / "input.svg"
            output_path = root / "output.png"
            input_path.write_bytes(sanitized.canonical_svg)
            os.chmod(input_path, 0o600)
            manifest = self._manifest or {}
            argv = self._raster_argv(
                manifest, root, input_path, output_path, width, height
            )
            env = self._fixed_env(root)
            try:
                if self._production:
                    completed = _run_bounded_process(
                        argv,
                        cwd=str(root),
                        env=env,
                        timeout=RASTER_TIMEOUT_SECONDS,
                        output_path=output_path,
                    )
                elif self._runner is not None:
                    completed = self._runner(
                        argv,
                        cwd=str(root),
                        env=env,
                        timeout=RASTER_TIMEOUT_SECONDS,
                        limits={
                            "file_bytes": MAX_PNG_BYTES,
                            "log_bytes": MAX_RASTER_LOG_BYTES,
                        },
                    )
                else:
                    _reject("RASTERIZER_UNAVAILABLE")
            except TimeoutError:
                _reject("RASTERIZER_TIMEOUT")
            except _BoundedProcessError:
                _reject("RASTERIZER_RESOURCE_LIMIT")
            except Exception:
                _reject("RASTERIZER_FAILED")
            if not isinstance(completed, Mapping) or completed.get("returncode") != 0:
                _reject("RASTERIZER_FAILED")
            for key in ("stdout", "stderr"):
                value = completed.get(key, b"")
                if not isinstance(value, bytes) or len(value) > 64 * 1024:
                    _reject("RASTERIZER_LOG_LIMIT")
            try:
                info = output_path.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > MAX_PNG_BYTES:
                    _reject("RASTERIZER_OUTPUT_UNSAFE")
                payload = output_path.read_bytes()
            except SvgSecurityError:
                raise
            except OSError:
                _reject("RASTERIZER_OUTPUT_MISSING")
            png = validate_png_bytes(payload, expected_width=width, expected_height=height)
            metadata = {
                **checked.identity,
                "sandboxed": True,
                "timeout_seconds": RASTER_TIMEOUT_SECONDS,
                "memory_limit_enforced": RASTER_MEMORY_LIMIT_ENFORCED,
                "output_limit_bytes": MAX_PNG_BYTES,
                "log_limit_bytes": MAX_RASTER_LOG_BYTES,
            }
            return RasterizedPNG(payload, png.png_sha256, width, height, metadata)


def sanitize_and_rasterize(
    raw: bytes, *, requested_width: int, requested_height: int, rasterizer: ResvgRasterizer
) -> tuple[SanitizeResult, RasterizedPNG]:
    sanitized = sanitize_svg_bytes(
        raw, requested_width=requested_width, requested_height=requested_height
    )
    return sanitized, rasterizer.rasterize(sanitized)


__all__ = [
    "LIMITS_SHA256", "POLICY_VERSION", "SANITIZER_VERSION", "PNGValidationResult",
    "RasterizedPNG", "RasterizerPreflight", "ResvgRasterizer", "SanitizeResult",
    "SvgSecurityError", "sanitize_and_rasterize", "sanitize_svg_bytes", "validate_png_bytes",
]
