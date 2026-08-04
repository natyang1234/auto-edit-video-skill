#!/usr/bin/env python3
"""Strict, fail-closed SVG sanitizer for the auto-edit asset pipeline.

The sanitizer never trusts SVG markup.  Raw SVG is *never* injected into a
browser DOM; this module turns caller-supplied bytes into a canonical, minimal
SVG tree or refuses the whole document.  Callers must still rasterize the
canonical output before it may enter the render timeline (see
``contracts/policies/SVG_THREAT_MODEL.md``).

Design choices that keep this fail-closed:

* Parsing uses :mod:`xml.parsers.expat` with DOCTYPE, entity declarations,
  external entities and processing instructions all rejected outright, so
  billion-laughs and external-DTD payloads never expand.
* Elements are an allowlist; anything else rejects the *entire* document rather
  than being silently stripped.
* Attributes are checked by rule: no ``on*`` handlers, ``href``/``xlink:href``
  restricted to local ``#fragment`` targets, and every ``url(...)`` (in any
  attribute or ``<style>`` body) must be a local fragment.
* References are resolved: unknown ids, duplicate ids and reference cycles all
  reject.
* Byte, node, depth, attribute and viewBox bounds cap DoS payloads.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any
import xml.parsers.expat as expat
from xml.sax.saxutils import escape, quoteattr

SANITIZER_VERSION = "svg-sanitizer/1"
POLICY_VERSION = "svg-threat-model/1"

SVG_NAMESPACE = "http://www.w3.org/2000/svg"

MAX_BYTES = 2 * 1024 * 1024
MAX_NODES = 10_000
MAX_DEPTH = 64
MAX_ATTR_VALUE = 100_000
MAX_VIEWBOX_DIM = 100_000.0

# Element allowlist mirrors SVG_THREAT_MODEL.md rule 2 (local names only).
ELEMENT_ALLOWLIST = frozenset(
    {
        "svg",
        "g",
        "path",
        "rect",
        "circle",
        "ellipse",
        "line",
        "polyline",
        "polygon",
        "text",
        "tspan",
        "defs",
        "linearGradient",
        "radialGradient",
        "stop",
        "clipPath",
        "mask",
        "use",
        "style",
        "title",
        "desc",
    }
)

_URL_REF = re.compile(r"url\(\s*(['\"]?)([^)'\"]*)\1\s*\)", re.IGNORECASE)
_CSS_IMPORT = re.compile(r"@import", re.IGNORECASE)
_UNSAFE_SCHEME = re.compile(r"(?:javascript|data|vbscript)\s*:", re.IGNORECASE)


class SvgRejected(Exception):
    """Raised when an SVG document is refused.  ``reason`` is a stable code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SanitizeResult:
    """A sanitized, canonical SVG plus its receipt (never the raw input)."""

    canonical_svg: str
    canonical_hash: str
    receipt: dict[str, Any]


class _StopParse(Exception):
    """Internal signal carrying a rejection reason out of an expat handler."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Node:
    __slots__ = ("tag", "attrs", "content", "node_id")

    def __init__(self, tag: str, attrs: dict[str, str], node_id: str | None) -> None:
        self.tag = tag
        self.attrs = attrs
        self.content: list[Any] = []  # str | _Node in document order
        self.node_id = node_id


def _local_name(name: str) -> str:
    return name.rsplit(":", 1)[-1] if ":" in name else name


def _check_url_refs(value: str) -> None:
    if _CSS_IMPORT.search(value):
        raise _StopParse("css-import")
    for match in _URL_REF.finditer(value):
        target = match.group(2).strip()
        if not target.startswith("#"):
            raise _StopParse("external-url")


def _check_viewbox(value: str) -> None:
    parts = value.replace(",", " ").split()
    if len(parts) != 4:
        raise _StopParse("oversized-viewbox")
    try:
        numbers = [float(part) for part in parts]
    except ValueError as exc:
        raise _StopParse("oversized-viewbox") from exc
    for number in numbers:
        if abs(number) > MAX_VIEWBOX_DIM:
            raise _StopParse("oversized-viewbox")


def _validate_attributes(local_tag: str, attrs: dict[str, str]) -> str | None:
    """Enforce attribute rules; return this element's id if it declares one."""
    node_id: str | None = None
    for raw_name, value in attrs.items():
        name = raw_name.casefold()
        local = _local_name(name)
        if len(value) > MAX_ATTR_VALUE:
            raise _StopParse("attribute-length")
        if local.startswith("on"):
            raise _StopParse(f"event-handler-attribute:{local}")
        if local in {"href", "xlink:href"} or local == "href":
            if not value.startswith("#") or len(value) < 2:
                raise _StopParse("external-reference:href")
        elif _UNSAFE_SCHEME.search(value):
            raise _StopParse("unsafe-scheme")
        _check_url_refs(value)
        if local == "viewbox" and local_tag == "svg":
            _check_viewbox(value)
        if local == "id":
            node_id = value
    return node_id


def _referenced_ids(node: _Node) -> set[str]:
    refs: set[str] = set()
    for raw_name, value in node.attrs.items():
        local = _local_name(raw_name.casefold())
        if local == "href" and value.startswith("#"):
            refs.add(value[1:])
        for match in _URL_REF.finditer(value):
            target = match.group(2).strip()
            if target.startswith("#") and len(target) > 1:
                refs.add(target[1:])
    return refs


def _parse(text: str) -> _Node:
    parser = expat.ParserCreate()
    parser.buffer_text = True

    stack: list[_Node] = []
    root: _Node | None = None
    counters = {"nodes": 0}

    def _reject_doctype(*_args: Any) -> None:
        raise _StopParse("doctype-forbidden")

    def _reject_entity(*_args: Any) -> None:
        raise _StopParse("doctype-forbidden")

    def _reject_pi(*_args: Any) -> None:
        raise _StopParse("processing-instruction-forbidden")

    def _reject_external(*_args: Any) -> int:
        raise _StopParse("doctype-forbidden")

    def _start(name: str, attrs: dict[str, str]) -> None:
        nonlocal root
        counters["nodes"] += 1
        if counters["nodes"] > MAX_NODES:
            raise _StopParse("node-limit")
        if len(stack) >= MAX_DEPTH:
            raise _StopParse("depth-limit")
        local = _local_name(name)
        if not stack and local != "svg":
            raise _StopParse("root-not-svg")
        if local not in ELEMENT_ALLOWLIST:
            raise _StopParse(f"disallowed-element:{local}")
        node_id = _validate_attributes(local, attrs)
        node = _Node(local, dict(attrs), node_id)
        if stack:
            stack[-1].content.append(node)
        else:
            root = node
        stack.append(node)

    def _end(_name: str) -> None:
        stack.pop()

    def _chars(data: str) -> None:
        if not stack:
            return
        collapsed = " ".join(data.split())
        if collapsed:
            if stack[-1].tag == "style":
                _check_url_refs(collapsed)
            stack[-1].content.append(collapsed)

    parser.StartDoctypeDeclHandler = _reject_doctype
    parser.EntityDeclHandler = _reject_entity
    parser.UnparsedEntityDeclHandler = _reject_entity
    parser.ExternalEntityRefHandler = _reject_external
    parser.ProcessingInstructionHandler = _reject_pi
    parser.StartElementHandler = _start
    parser.EndElementHandler = _end
    parser.CharacterDataHandler = _chars

    try:
        parser.Parse(text, True)
    except _StopParse as exc:
        raise SvgRejected(exc.reason) from exc
    except expat.ExpatError as exc:
        raise SvgRejected("parse-error") from exc

    if root is None:
        raise SvgRejected("empty-document")
    return root


def _resolve_references(root: _Node) -> None:
    ids: set[str] = set()
    all_refs: set[str] = set()
    edges: dict[str, set[str]] = {}

    def walk(node: _Node, enclosing_id: str | None) -> None:
        if node.node_id is not None:
            if node.node_id in ids:
                raise SvgRejected("duplicate-id")
            ids.add(node.node_id)
        owner = node.node_id or enclosing_id
        refs = _referenced_ids(node)
        all_refs.update(refs)
        if owner is not None and refs:
            edges.setdefault(owner, set()).update(refs)
        for child in node.content:
            if isinstance(child, _Node):
                walk(child, owner)

    walk(root, None)

    unresolved = all_refs - ids
    if unresolved:
        raise SvgRejected("unresolved-reference")

    # Detect reference cycles among ids (use/url(#..) chains).
    state: dict[str, int] = {}  # 0=visiting, 1=done

    def has_cycle(node_id: str) -> bool:
        state[node_id] = 0
        for target in edges.get(node_id, ()):  # only ids that exist reach here
            marker = state.get(target)
            if marker == 0:
                return True
            if marker is None and target in edges and has_cycle(target):
                return True
        state[node_id] = 1
        return False

    for node_id in edges:
        if state.get(node_id) is None and has_cycle(node_id):
            raise SvgRejected("cyclic-reference")


def _serialize(node: _Node) -> str:
    attrs = node.attrs
    if node.tag == "svg" and not any(
        _local_name(name.casefold()) == "xmlns" or name.casefold() == "xmlns"
        for name in attrs
    ):
        attrs = {"xmlns": SVG_NAMESPACE, **attrs}
    rendered_attrs = "".join(
        f" {name}={quoteattr(attrs[name])}" for name in sorted(attrs)
    )
    if not node.content:
        return f"<{node.tag}{rendered_attrs}/>"
    inner = "".join(
        _serialize(child) if isinstance(child, _Node) else escape(child)
        for child in node.content
    )
    return f"<{node.tag}{rendered_attrs}>{inner}</{node.tag}>"


def _count_nodes(node: _Node) -> int:
    total = 1
    for child in node.content:
        if isinstance(child, _Node):
            total += _count_nodes(child)
    return total


def sanitize_svg(raw: str | bytes) -> SanitizeResult:
    """Sanitize *raw* SVG, returning a canonical form + receipt or rejecting.

    Raises :class:`SvgRejected` (with a stable ``reason``) for any unsafe or
    malformed document.  The returned :class:`SanitizeResult` never contains the
    raw input, only the re-serialized canonical tree.
    """
    if isinstance(raw, bytes):
        if len(raw) > MAX_BYTES:
            raise SvgRejected("byte-limit")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SvgRejected("parse-error") from exc
    elif isinstance(raw, str):
        text = raw
        if len(text.encode("utf-8")) > MAX_BYTES:
            raise SvgRejected("byte-limit")
    else:  # pragma: no cover - defensive
        raise SvgRejected("parse-error")

    if not text.strip():
        raise SvgRejected("empty-document")

    root = _parse(text)
    _resolve_references(root)

    canonical_svg = _serialize(root)
    canonical_bytes = canonical_svg.encode("utf-8")
    canonical_hash = hashlib.sha256(canonical_bytes).hexdigest()
    receipt = {
        "sanitizer_version": SANITIZER_VERSION,
        "policy_version": POLICY_VERSION,
        "canonical_sha256": canonical_hash,
        "byte_length": len(canonical_bytes),
        "node_count": _count_nodes(root),
    }
    return SanitizeResult(
        canonical_svg=canonical_svg,
        canonical_hash=canonical_hash,
        receipt=receipt,
    )


__all__ = [
    "POLICY_VERSION",
    "SANITIZER_VERSION",
    "SanitizeResult",
    "SvgRejected",
    "sanitize_svg",
]
