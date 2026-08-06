"""What ends up on top of what, once everything has been placed.

Placement already avoids the speaker: the tracker finds a head and the card
goes above it. Nothing checked the result. Two cards can hold the same
moment, a card can sit on the caption, and a caption that wrapped to a third
line can slide under the platform's own controls — all of which render
without complaint and are only visible to someone watching the file.

Rects are fractions of the canvas and anchored at their centre, the way the
renderer's own overlay placement is expressed, so nothing has to be converted
twice to be compared.
"""
from __future__ import annotations

import math
from typing import Any, Iterable

# Two overlays almost always share a few pixels at their edges. What matters
# is one covering another: this is the share of the smaller overlay that has
# to disappear behind the larger before it counts as a collision.
COLLISION_SHARE = 0.15
# Overlapping windows shorter than this are the crossfade between one card
# leaving and the next arriving, not two cards holding the screen together.
MIN_SHARED_SECONDS = 0.35
# Rects are built from pixel sizes and a centre rounded to two decimals, so
# an overlay placed deliberately on a boundary can land either side of it.
EDGE_TOLERANCE = 0.005


def rect(placement: dict[str, Any]) -> tuple[float, float, float, float] | None:
    """Left, top, right, bottom as fractions, or None when unmeasured.

    None is not "no problem": it is a placement whose drawn size nobody
    recorded, and every caller has to say so rather than skip it quietly.
    """
    try:
        centre_x = float(placement["x"])
        centre_y = float(placement["y"])
        width = float(placement["width"])
        height = float(placement["height"])
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (centre_x, centre_y, width, height)):
        return None
    if width <= 0 or height <= 0:
        return None
    return (
        centre_x - width / 2.0,
        centre_y - height / 2.0,
        centre_x + width / 2.0,
        centre_y + height / 2.0,
    )


def shared_seconds(first: dict[str, Any], second: dict[str, Any]) -> float:
    try:
        start = max(float(first["start"]), float(second["start"]))
        end = min(float(first["end"]), float(second["end"]))
    except (KeyError, TypeError, ValueError):
        return 0.0
    return max(0.0, end - start)


def covered_share(first: tuple[float, ...], second: tuple[float, ...]) -> float:
    """How much of the smaller rect the other one covers."""
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    if right <= left or bottom <= top:
        return 0.0
    overlap = (right - left) * (bottom - top)
    areas = [
        (first[2] - first[0]) * (first[3] - first[1]),
        (second[2] - second[0]) * (second[3] - second[1]),
    ]
    smaller = min(areas)
    return overlap / smaller if smaller > 0 else 0.0


def label(placement: dict[str, Any]) -> str:
    kind = str(placement.get("kind") or "overlay")
    name = str(placement.get("id") or "")
    return f"{kind} {name}".strip()


def find_collisions(placements: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pairs that hold the same moment and the same part of the frame."""
    items = list(placements)
    findings: list[dict[str, Any]] = []
    for index, first in enumerate(items):
        first_rect = rect(first)
        if first_rect is None:
            continue
        for second in items[index + 1:]:
            second_rect = rect(second)
            if second_rect is None:
                continue
            together = shared_seconds(first, second)
            if together < MIN_SHARED_SECONDS:
                continue
            share = covered_share(first_rect, second_rect)
            if share < COLLISION_SHARE:
                continue
            findings.append(
                {
                    "kind": "collision",
                    "between": [label(first), label(second)],
                    "covered_share": round(share, 4),
                    "shared_seconds": round(together, 3),
                    "detail": (
                        f"{label(first)} and {label(second)} share "
                        f"{together:.2f}s with {share:.0%} of the smaller one covered"
                    ),
                }
            )
    return findings


def find_off_frame(placements: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anything reaching past the edge of the canvas, which is text cut off."""
    findings: list[dict[str, Any]] = []
    for placement in placements:
        bounds = rect(placement)
        if bounds is None:
            continue
        left, top, right, bottom = bounds
        if (
            left < -EDGE_TOLERANCE
            or top < -EDGE_TOLERANCE
            or right > 1 + EDGE_TOLERANCE
            or bottom > 1 + EDGE_TOLERANCE
        ):
            findings.append(
                {
                    "kind": "off_frame",
                    "overlay": label(placement),
                    "detail": (
                        f"{label(placement)} reaches outside the frame "
                        f"(left {left:.3f}, top {top:.3f}, "
                        f"right {right:.3f}, bottom {bottom:.3f})"
                    ),
                }
            )
    return findings


def find_safe_area_intrusions(
    placements: Iterable[dict[str, Any]], safe: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Anything under the platform's own controls.

    The margins come from the platform preset registry, in percent. Without
    them nothing is reported — an empty list here means "not checked", which
    is why the caller records whether margins were available at all.
    """
    if not isinstance(safe, dict):
        return []
    try:
        limits = {
            "top": float(safe.get("top", 0)) / 100.0,
            "bottom": 1.0 - float(safe.get("bottom", 0)) / 100.0,
            "left": float(safe.get("left", 0)) / 100.0,
            "right": 1.0 - float(safe.get("right", 0)) / 100.0,
        }
    except (TypeError, ValueError):
        return []
    findings: list[dict[str, Any]] = []
    for placement in placements:
        bounds = rect(placement)
        if bounds is None:
            continue
        left, top, right, bottom = bounds
        sides = []
        # The same slack the frame edge gets, for the same reason: rects come
        # from pixel sizes and a centre rounded to two decimals, so a card
        # placed exactly on the margin lands a hair either side of it.
        if top < limits["top"] - EDGE_TOLERANCE:
            sides.append("top")
        if bottom > limits["bottom"] + EDGE_TOLERANCE:
            sides.append("bottom")
        if left < limits["left"] - EDGE_TOLERANCE:
            sides.append("left")
        if right > limits["right"] + EDGE_TOLERANCE:
            sides.append("right")
        if sides:
            findings.append(
                {
                    "kind": "safe_area",
                    "overlay": label(placement),
                    "sides": sides,
                    "detail": (
                        f"{label(placement)} reaches into the "
                        f"{', '.join(sides)} margin the platform reserves "
                        "for its own controls"
                    ),
                }
            )
    return findings


def review(
    placements: Iterable[dict[str, Any]], safe: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Everything the geometry can be asked, plus what it could not answer."""
    items = list(placements)
    unmeasured = [label(item) for item in items if rect(item) is None]
    return {
        "checked": len(items) - len(unmeasured),
        # Named, not counted: an overlay nobody measured is an overlay nobody
        # checked, and a report that only counts them reads like coverage.
        "unmeasured": unmeasured,
        "safe_area_available": isinstance(safe, dict) and bool(safe),
        "collisions": find_collisions(items),
        "off_frame": find_off_frame(items),
        "safe_area": find_safe_area_intrusions(items, safe),
    }


def blocking(findings: dict[str, Any]) -> list[str]:
    """The findings that mean the frame is wrong, not merely tight.

    Two overlays on top of each other, or text running off the canvas, are
    defects in what was drawn. Reaching into a platform's reserved margin is
    a judgement about where it will be posted, so it is reported and not
    raised: a delivery that never goes to Reels is not broken by it.
    """
    return [item["detail"] for item in findings["collisions"] + findings["off_frame"]]
