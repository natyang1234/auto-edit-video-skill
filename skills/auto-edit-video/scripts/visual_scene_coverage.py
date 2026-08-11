#!/usr/bin/env python3
"""Validate major-scene pacing and explicit A-roll breathing intervals."""
from __future__ import annotations

import math
from typing import Any


LONG_OUTPUT_SECONDS = 45.0
BREATHING_SHARE_MIN = 0.25
BREATHING_SHARE_MAX = 0.55
BREATHING_INTERVAL_MIN_SECONDS = 2.0
BREATHING_INTERVAL_MIN_COUNT = 2
UNEXPLAINED_GAP_MAX_SECONDS = 12.0


class VisualSceneCoverageError(ValueError):
    """Scene coverage evidence is malformed and cannot be trusted."""


def _finite(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VisualSceneCoverageError(f"{path} must be a finite number")
    resolved = float(value)
    if not math.isfinite(resolved):
        raise VisualSceneCoverageError(f"{path} must be a finite number")
    return resolved


def _bounded_interval(
    item: Any,
    path: str,
    duration: float,
    *,
    breathing: bool,
) -> tuple[str, float, float]:
    if not isinstance(item, dict):
        raise VisualSceneCoverageError(f"{path} must be an object")
    identifier = item.get("id")
    if not isinstance(identifier, str) or not identifier:
        raise VisualSceneCoverageError(f"{path}.id must be non-empty")
    start = _finite(item.get("start"), f"{path}.start")
    end = _finite(item.get("end"), f"{path}.end")
    if start < 0 or end <= start or end > duration:
        raise VisualSceneCoverageError(f"{path} must be positive and inside duration")
    if breathing:
        if set(item) != {"id", "start", "end", "role", "major_graphic"}:
            raise VisualSceneCoverageError(
                f"{path} must be an exact A-roll breathing interval"
            )
        if item.get("role") != "a_roll_breathing" or item.get("major_graphic") is not False:
            raise VisualSceneCoverageError(
                f"{path} must be labeled a_roll_breathing with no major graphic"
            )
    elif item.get("major_graphic") is not True:
        raise VisualSceneCoverageError(f"{path} must be a delivered major graphic")
    return identifier, start, end


def _overlap(first: tuple[float, float], second: tuple[float, float]) -> bool:
    return max(first[0], second[0]) < min(first[1], second[1])


def _uncovered_gaps(
    duration: float,
    intervals: list[tuple[float, float]],
) -> list[dict[str, float]]:
    merged: list[list[float]] = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    gaps: list[dict[str, float]] = []
    cursor = 0.0
    for start, end in merged:
        if start - cursor > UNEXPLAINED_GAP_MAX_SECONDS:
            gaps.append(
                {
                    "start": round(cursor, 6),
                    "end": round(start, 6),
                    "duration": round(start - cursor, 6),
                }
            )
        cursor = max(cursor, end)
    if duration - cursor > UNEXPLAINED_GAP_MAX_SECONDS:
        gaps.append(
            {
                "start": round(cursor, 6),
                "end": round(duration, 6),
                "duration": round(duration - cursor, 6),
            }
        )
    return gaps


def evaluate_scene_coverage(
    duration_s: Any,
    breathing_intervals: Any,
    major_scene_items: Any,
) -> dict[str, Any]:
    """Return a fail-closed pacing report over final-domain seconds."""
    duration = _finite(duration_s, "duration_s")
    if duration <= 0:
        raise VisualSceneCoverageError("duration_s must be positive")
    if not isinstance(breathing_intervals, list):
        raise VisualSceneCoverageError("breathing_intervals must be a list")
    if not isinstance(major_scene_items, list):
        raise VisualSceneCoverageError("major_scene_items must be a list")

    seen_ids: set[str] = set()
    breathing: list[tuple[float, float]] = []
    for index, item in enumerate(breathing_intervals):
        identifier, start, end = _bounded_interval(
            item, f"breathing_intervals[{index}]", duration, breathing=True
        )
        if identifier in seen_ids:
            raise VisualSceneCoverageError("scene coverage IDs must be unique")
        seen_ids.add(identifier)
        breathing.append((start, end))

    major: list[tuple[float, float]] = []
    for index, item in enumerate(major_scene_items):
        identifier, start, end = _bounded_interval(
            item, f"major_scene_items[{index}]", duration, breathing=False
        )
        if identifier in seen_ids:
            raise VisualSceneCoverageError("scene coverage IDs must be unique")
        seen_ids.add(identifier)
        major.append((start, end))

    failures: list[str] = []
    for breathing_index, breathing_interval in enumerate(breathing):
        if any(_overlap(breathing_interval, major_interval) for major_interval in major):
            failures.append(
                f"breathing_intervals[{breathing_index}] overlaps a delivered major graphic"
            )
    for index, interval in enumerate(breathing):
        if any(_overlap(interval, other) for other in breathing[index + 1 :]):
            failures.append(f"breathing_intervals[{index}] overlaps another breathing interval")

    breathing_seconds = sum(end - start for start, end in breathing)
    breathing_share = breathing_seconds / duration
    qualifying_count = sum(
        end - start >= BREATHING_INTERVAL_MIN_SECONDS for start, end in breathing
    )
    if duration > LONG_OUTPUT_SECONDS:
        if not BREATHING_SHARE_MIN <= breathing_share <= BREATHING_SHARE_MAX:
            failures.append("long output A-roll breathing share must stay between 25% and 55%")
        if qualifying_count < BREATHING_INTERVAL_MIN_COUNT:
            failures.append("long output needs at least two A-roll breathing intervals of 2s")

    gaps = _uncovered_gaps(duration, breathing + major)
    if gaps:
        failures.append("visual timeline has an unexplained gap over 12 seconds")
    return {
        "status": "pass" if not failures else "fail",
        "duration_s": round(duration, 6),
        "breathing_seconds": round(breathing_seconds, 6),
        "breathing_share": round(breathing_share, 6),
        "qualifying_breathing_interval_count": qualifying_count,
        "uncovered_gaps_over_12s": gaps,
        "failures": failures,
    }
