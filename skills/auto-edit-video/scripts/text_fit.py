"""Conservative text-fit policy and renderer evidence shape.

This intentionally does not guess from character counts.  Browser-side callers
must attach measured DOM boxes; without them the result remains advisory.
"""
from __future__ import annotations

from typing import Any

SAFE = {"left": 108, "top": 192, "right": 972, "bottom": 1728}


def layout_report(boxes: list[dict[str, Any]], *, subject_box: dict[str, float] | None = None) -> dict[str, Any]:
    """Turn real DOM client rects into evidence; never resize or move content."""
    findings: list[dict[str, Any]] = []
    measured = bool(boxes)
    for box in boxes:
        try:
            x, y, w, h = (float(box[k]) for k in ("x", "y", "width", "height"))
        except (KeyError, TypeError, ValueError):
            findings.append({"kind": "invalid_measurement"}); continue
        if x < SAFE["left"] or y < SAFE["top"] or x + w > SAFE["right"] or y + h > SAFE["bottom"]:
            findings.append({"kind": "safe_area", "id": box.get("id", ""), "rect": {"x": x, "y": y, "width": w, "height": h}})
        if subject_box:
            sx, sy, sw, sh = (float(subject_box[k]) for k in ("x", "y", "width", "height"))
            if max(x, sx) < min(x + w, sx + sw) and max(y, sy) < min(y + h, sy + sh):
                findings.append({"kind": "subject_overlap", "id": box.get("id", "")})
    return {"version": 1, "measurement": "dom" if measured else "missing", "safe_area": SAFE, "boxes": boxes,
            "findings": findings, "status": "warn" if findings or not measured else "pass"}


def measure_values(values: dict[str, Any], *, enabled: bool = True, subject_box: dict[str, float] | None = None) -> dict[str, Any]:
    """Values alone have no layout. Call compose.measure_composition on the real FX."""
    return {"version": 2, "measurement": "unavailable" if enabled else "disabled",
            "status": "warn" if enabled else "skipped", "boxes": [],
            "findings": [{"kind": "composition_required"}] if enabled else []}
