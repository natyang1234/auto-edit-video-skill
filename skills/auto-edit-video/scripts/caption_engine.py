#!/usr/bin/env python3
"""Caption grapheme/boundary authority — macOS runtime (GRAPHEME_CLUSTER_V1).

The server is the ONLY boundary authority: browsers send raw UTF-16 offsets
and get them snapped here. macOS is the single supported segmentation
runtime (nat-approved 2026-08-04); its version is part of the engine
identity so an OS upgrade invalidates downstream artifacts instead of
silently changing boundaries.

Offsets throughout the caption contract are UTF-16 code units (browser
``selectionStart`` semantics). Python string indices are code points, so use
the conversion helpers here whenever slicing.
"""
from __future__ import annotations

import os
import platform

_FOUNDATION = None


def _load_foundation():
    global _FOUNDATION
    if _FOUNDATION is not None:
        return _FOUNDATION
    if os.environ.get("AUTO_EDIT_DISABLE_CORETEXT") == "1":
        _FOUNDATION = False
        return _FOUNDATION
    try:
        import Foundation  # type: ignore
    except Exception:  # pragma: no cover - host-dependent
        _FOUNDATION = False
        return _FOUNDATION
    _FOUNDATION = Foundation
    return _FOUNDATION


def available() -> bool:
    return bool(_load_foundation())


def engine_descriptor() -> dict[str, str]:
    ok = available()
    return {
        "name": "macos-nsstring-egc",
        "version": f"macos-{platform.mac_ver()[0] or 'unknown'}" if ok else "",
        "status": "present" if ok else "not_configured",
    }


# ---------------------------------------------------------------------------
# UTF-16 offset helpers (contract offsets ⇄ Python code-point indices)
# ---------------------------------------------------------------------------

def utf16_length(text: str) -> int:
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def codepoint_index_for_utf16(text: str, utf16_offset: int) -> int:
    """Code-point index for a UTF-16 offset; raises inside a surrogate pair."""
    if utf16_offset < 0:
        raise ValueError("negative UTF-16 offset")
    units = 0
    for index, ch in enumerate(text):
        if units == utf16_offset:
            return index
        width = 2 if ord(ch) > 0xFFFF else 1
        if units + width > utf16_offset:
            raise ValueError(f"UTF-16 offset {utf16_offset} splits a surrogate pair")
        units += width
    if units == utf16_offset:
        return len(text)
    raise ValueError(f"UTF-16 offset {utf16_offset} exceeds text length {units}")


def slice_utf16(text: str, start: int, end: int) -> str:
    return text[codepoint_index_for_utf16(text, start):codepoint_index_for_utf16(text, end)]


# ---------------------------------------------------------------------------
# Boundary map and snapping
# ---------------------------------------------------------------------------

def boundary_map(text: str) -> list[list[int]]:
    """Grapheme clusters as UTF-16 [start, end) pairs, via NSString EGC."""
    foundation = _load_foundation()
    if not foundation:
        raise RuntimeError("macOS caption engine is not available")
    ns_text = foundation.NSString.stringWithString_(text)
    length = ns_text.length()
    clusters: list[list[int]] = []
    location = 0
    while location < length:
        cluster_range = ns_text.rangeOfComposedCharacterSequenceAtIndex_(location)
        clusters.append([cluster_range.location, cluster_range.location + cluster_range.length])
        location = cluster_range.location + cluster_range.length
    return clusters


def snap_span(text: str, start: int, end: int) -> tuple[int, int] | None:
    """Snap raw UTF-16 offsets outward to the nearest cluster boundaries.

    Returns None when the span cannot be represented (empty after clamping).
    """
    clusters = boundary_map(text)
    if not clusters:
        return None
    total = clusters[-1][1]
    start = max(0, min(int(start), total))
    end = max(0, min(int(end), total))
    if end < start:
        start, end = end, start
    snapped_start = 0
    for cluster_start, cluster_end in clusters:
        if cluster_start <= start < cluster_end:
            snapped_start = cluster_start
            break
        if cluster_start >= start:
            snapped_start = cluster_start
            break
    else:
        snapped_start = total
    snapped_end = total
    for cluster_start, cluster_end in clusters:
        if cluster_start < end <= cluster_end:
            snapped_end = cluster_end
            break
    if end == 0:
        snapped_end = 0
    if snapped_end <= snapped_start:
        return None
    return snapped_start, snapped_end


def span_on_boundaries(text: str, start: int, end: int) -> bool:
    boundaries: set[int] = {0}
    for cluster_start, cluster_end in boundary_map(text):
        boundaries.add(cluster_start)
        boundaries.add(cluster_end)
    return start in boundaries and end in boundaries and end > start
