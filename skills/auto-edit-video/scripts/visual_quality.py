#!/usr/bin/env python3
"""Highlight-scoped visual planning and fail-closed visual-quality contracts."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any


DESIGN_ROLES = ("hook", "concept", "rule", "memory", "recap")
RENDER_VISUAL_EVIDENCE_REL = Path("working/render_visual_evidence")
ROLE_WINDOWS = {
    "hook": (0.00, 0.12),
    "concept": (0.18, 0.31),
    "rule": (0.35, 0.50),
    "memory": (0.58, 0.71),
    "recap": (0.80, 1.00),
}
ROLE_KICKERS = {
    "hook": "30 秒重點課",
    "concept": "先看概念",
    "rule": "核心規則",
    "memory": "記憶技巧",
    "recap": "一秒複習",
}
ROLE_TYPES = {
    "hook": "title",
    "concept": "card",
    "rule": "animation",
    "memory": "card",
    "recap": "animation",
}
ROLE_LAYOUTS = {
    "hook": {"x": 50.0, "y": 50.0, "width": 100.0, "height": 100.0},
    "concept": {"x": 50.0, "y": 55.99, "width": 92.59, "height": 22.40},
    "rule": {"x": 50.0, "y": 56.25, "width": 92.59, "height": 27.08},
    "memory": {"x": 50.0, "y": 15.63, "width": 87.04, "height": 15.10},
    "recap": {"x": 50.0, "y": 41.15, "width": 91.67, "height": 47.92},
}


def _finite_number(value: Any, fallback: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return fallback
    return result if math.isfinite(result) else fallback


def _trim_text(value: Any, limit: int = 42) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip("，,。.!！？?；;：:") + "…"


def _highlight_bounds(highlight: dict[str, Any]) -> tuple[float, float]:
    start = max(0.0, _finite_number(highlight.get("start")))
    end = max(start, _finite_number(highlight.get("end"), start))
    return start, end


def _overlapping_segments(
    transcript: dict[str, Any],
    highlight: dict[str, Any],
) -> list[dict[str, Any]]:
    clip_start, clip_end = _highlight_bounds(highlight)
    # The reading split, when the transcript carries one: whisper may return a
    # single segment for a whole clip, and sampling five cards out of it gives
    # five copies of the same truncated wall of text.
    source = transcript.get("caption_segments") or transcript.get("segments", [])
    return [
        item
        for item in source
        if isinstance(item, dict)
        and str(item.get("text", "")).strip()
        and _finite_number(item.get("end")) > clip_start
        and _finite_number(item.get("start")) < clip_end
    ]


def _sample_segment_text(
    segments: list[dict[str, Any]],
    fraction: float,
    fallback: str,
) -> str:
    if not segments:
        return fallback
    index = min(len(segments) - 1, max(0, round(fraction * (len(segments) - 1))))
    return _trim_text(segments[index].get("text"), 46) or fallback


def _role_style(
    caption_style: dict[str, Any],
    role: str,
    director_style: str,
) -> dict[str, Any]:
    style = dict(caption_style)
    base_size = int(_finite_number(style.get("font_size"), 58))
    palette = {
        "teacher-punch": ("#fdf6e3", "#bf5700", "#171713"),
        "high-energy": ("#fdf6e3", "#ffb000", "#17110d"),
        "documentary": ("#f2eadb", "#d99a52", "#262019"),
        "minimal": ("#f7f3ec", "#d7d0c6", "#221e1a"),
        "editorial-clean": ("#f6f0e5", "#e94f37", "#211d19"),
    }.get(director_style, ("#fdf6e3", "#bf5700", "#171713"))
    color, accent, panel = palette
    role_positions = {
        "hook": (18, "slide-up", max(72, base_size + 12)),
        "concept": (28, "fade", max(64, base_size + 4)),
        "rule": (43, "pop", max(76, base_size + 14)),
        "memory": (22, "slide-up", max(62, base_size + 2)),
        "recap": (27, "pop", max(70, base_size + 10)),
    }
    y, animation, font_size = role_positions[role]
    style.update(
        {
            "font_size": font_size,
            "font_weight": 900,
            "color": accent if role in {"rule", "recap"} else color,
            "emphasis_color": accent,
            "x": 50,
            "y": y,
            "max_width": 84,
            "animation": animation,
            "box": True,
            "box_color": panel,
            "design_theme": "craft",
        }
    )
    return style


def build_highlight_design_overlays(
    transcript: dict[str, Any],
    highlight: dict[str, Any],
    caption_style: dict[str, Any],
    director_style: str,
) -> list[dict[str, Any]]:
    """Create five editable, transcript-grounded cards inside one highlight."""
    clip_start, clip_end = _highlight_bounds(highlight)
    duration = clip_end - clip_start
    if duration <= 0:
        return []
    segments = _overlapping_segments(transcript, highlight)
    # What this cut is called is decided in one place, because deciding it
    # here as well is how the two card paths came to name the same cut
    # differently.
    from editor_server import highlight_card_title  # lazy: import cycle

    title = highlight_card_title(highlight)
    if not title:
        title = _sample_segment_text(segments, 0.0, "本段精華")
    sample_fractions = {
        "hook": 0.0,
        "concept": 0.20,
        "rule": 0.42,
        "memory": 0.66,
        "recap": 1.0,
    }
    highlight_id = str(highlight.get("id") or "highlight")
    overlays: list[dict[str, Any]] = []
    for index, role in enumerate(DESIGN_ROLES, start=1):
        start_fraction, end_fraction = ROLE_WINDOWS[role]
        start = clip_start + duration * start_fraction
        end = clip_start + duration * end_fraction
        if end - start < 0.8:
            end = min(clip_end, start + 0.8)
        text = title if role == "hook" else _sample_segment_text(
            segments,
            sample_fractions[role],
            title,
        )
        overlays.append(
            {
                "id": f"design-{re.sub(r'[^A-Za-z0-9_-]', '-', highlight_id)[:46]}-{index:02d}",
                "type": ROLE_TYPES[role],
                "start": round(start, 3),
                "end": round(min(clip_end, end), 3),
                "text": text,
                "kicker": ROLE_KICKERS[role],
                "detail": _sample_segment_text(
                    segments,
                    min(1.0, sample_fractions[role] + 0.12),
                    text,
                ),
                "emphasis": [],
                "visible": True,
                "locked": False,
                "z_index": 30 + index,
                "style": _role_style(caption_style, role, director_style),
                "layout": dict(ROLE_LAYOUTS[role]),
                "source": "working/highlight_visual_plan.json",
                "provenance": "deterministic local highlight transcript proposal; requires review",
                "highlight_id": highlight_id,
                "design_role": role,
                "review_status": "pending",
            }
        )
    return overlays


def overlay_matches_clip(overlay: dict[str, Any], clip: dict[str, Any] | None) -> bool:
    if not isinstance(overlay, dict) or overlay.get("visible", True) is False:
        return False
    if clip is None:
        return not overlay.get("highlight_id")
    clip_id = str(clip.get("id") or "")
    scoped_id = str(overlay.get("highlight_id") or "")
    if scoped_id and scoped_id != clip_id:
        return False
    clip_start, clip_end = _highlight_bounds(clip)
    start = _finite_number(overlay.get("start"))
    end = _finite_number(overlay.get("end"), start)
    return end > clip_start and start < clip_end


def overlays_for_clip(
    state: dict[str, Any],
    clip: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    return [
        item
        for item in state.get("overlays", [])
        if isinstance(item, dict) and overlay_matches_clip(item, clip)
    ]


def _coverage_ratio(
    overlays: list[dict[str, Any]],
    clip: dict[str, Any],
) -> float:
    clip_start, clip_end = _highlight_bounds(clip)
    duration = clip_end - clip_start
    if duration <= 0:
        return 0.0
    intervals = sorted(
        (
            max(clip_start, _finite_number(item.get("start"))),
            min(clip_end, _finite_number(item.get("end"))),
        )
        for item in overlays
    )
    covered = 0.0
    cursor_start: float | None = None
    cursor_end = 0.0
    for start, end in intervals:
        if end <= start:
            continue
        if cursor_start is None:
            cursor_start, cursor_end = start, end
        elif start <= cursor_end:
            cursor_end = max(cursor_end, end)
        else:
            covered += cursor_end - cursor_start
            cursor_start, cursor_end = start, end
    if cursor_start is not None:
        covered += cursor_end - cursor_start
    return round(min(1.0, covered / duration), 4)


def visual_quality_report(
    state: dict[str, Any],
    manifest: dict[str, Any],
    clip: dict[str, Any] | None,
) -> dict[str, Any]:
    mode = str(state.get("visual_quality_mode") or "basic")
    active = overlays_for_clip(state, clip)
    captions = [item for item in active if item.get("type") == "caption"]
    designed = [item for item in active if item.get("design_role")]
    designed_roles = sorted(
        {
            str(item.get("design_role"))
            for item in designed
            if str(item.get("design_role")) in DESIGN_ROLES
        }
    )
    graphic_packages = [
        item
        for item in active
        if item.get("visual_role") == "graphic-package" or item.get("graphic_package") is True
    ]
    designed_count = len(designed) + sum(
        max(0, int(_finite_number(item.get("design_card_count"), 0)))
        for item in graphic_packages
    )
    designed_types = sorted({str(item.get("type")) for item in designed})
    failures: list[str] = []
    clip_start, clip_end = _highlight_bounds(clip or {})
    clip_duration = clip_end - clip_start
    coverage = _coverage_ratio(designed, clip) if clip is not None else 0.0
    if graphic_packages:
        coverage = max(coverage, 1.0)

    contract_applies = mode == "designed" and clip is not None and clip_duration >= 15.0
    if contract_applies:
        if designed_count < 5:
            failures.append("designed short-form delivery requires at least five in-clip cards")
        missing_roles = [role for role in DESIGN_ROLES if role not in designed_roles]
        if missing_roles and not graphic_packages:
            failures.append(
                "designed delivery is missing required card roles: " + ", ".join(missing_roles)
            )
        if coverage < 0.35:
            failures.append("designed cards must cover at least 35% of the selected highlight")
        if len(designed_types) < 3 and not graphic_packages:
            failures.append("designed delivery requires at least three visual card types")
        source = manifest.get("source", {}) if isinstance(manifest.get("source"), dict) else {}
        canvas = state.get("canvas", {}) if isinstance(state.get("canvas"), dict) else {}
        source_width = _finite_number(source.get("width"))
        source_height = _finite_number(source.get("height"))
        canvas_width = _finite_number(canvas.get("width"))
        canvas_height = _finite_number(canvas.get("height"))
        landscape_to_portrait = (
            source_width > 0
            and source_height > 0
            and canvas_width > 0
            and canvas_height > 0
            and source_width / source_height >= 1.4
            and canvas_width / canvas_height <= 0.8
        )
        if landscape_to_portrait and canvas.get("fit") != "contain" and not graphic_packages:
            failures.append("landscape A-roll on a portrait canvas must use contain or a reviewed graphic package")

    return {
        "schema_version": 1,
        "mode": mode,
        "status": "pass" if not failures else "fail",
        "contract_applies": contract_applies,
        "clip_id": str(clip.get("id")) if isinstance(clip, dict) and clip.get("id") else None,
        "clip_duration_s": round(clip_duration, 3),
        "caption_count": len(captions),
        "designed_card_count": designed_count,
        "designed_roles": designed_roles,
        "designed_types": designed_types,
        "designed_coverage_ratio": coverage,
        "graphic_package_count": len(graphic_packages),
        "failures": failures,
    }


def visual_quality_errors(
    state: dict[str, Any],
    manifest: dict[str, Any],
    clip: dict[str, Any] | None,
) -> list[str]:
    return list(visual_quality_report(state, manifest, clip).get("failures", []))


def _renderer_finite_number(value: Any, field: str, *, minimum: float | None = None) -> float:
    """Validate a JSON number without allowing coercion or non-finite values."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    if minimum is not None and number < minimum:
        raise ValueError(f"{field} must be >= {minimum}")
    return number


def _renderer_identifier(
    item: dict[str, Any], field: str, item_index: int, *, required: bool = False
) -> str | None:
    value = item.get(field)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value:
        requirement = "required " if required else ""
        raise ValueError(f"items[{item_index}].{field} must be a non-empty {requirement}string")
    return value


def _renderer_motion_requested(value: Any, field: str) -> bool:
    # The renderer records the requested motion preset as a string (an empty
    # preset means no request); callers may also provide an explicit boolean.
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return bool(value)
    raise ValueError(f"{field} must be a motion preset string or boolean")


def _renderer_longest_no_change_gap(
    intervals: list[tuple[float, float]], duration: float
) -> float:
    """Return the largest uncovered interval after clipping and unioning beats."""
    if duration <= 0:
        return 0.0
    clipped = sorted(
        (
            max(0.0, start),
            min(duration, end),
        )
        for start, end in intervals
        if min(duration, end) > max(0.0, start)
    )
    if not clipped:
        return round(duration, 6)

    longest = 0.0
    cursor = 0.0
    for start, end in clipped:
        if start > cursor:
            longest = max(longest, start - cursor)
        cursor = max(cursor, end)
    longest = max(longest, duration - cursor)
    return round(longest, 6)


def rendered_visual_quality_report(evidence: dict[str, Any]) -> dict[str, Any]:
    """Aggregate deterministic visual-quality metrics from renderer evidence.

    This is intentionally separate from ``visual_quality_report``: the latter
    validates an editable timeline, while this report describes what the
    renderer actually delivered and is suitable for a final QA receipt.
    """
    if not isinstance(evidence, dict):
        raise ValueError("renderer evidence must be an object")
    if (
        isinstance(evidence.get("schema_version"), bool)
        or not isinstance(evidence.get("schema_version"), int)
        or evidence.get("schema_version") != 1
    ):
        raise ValueError("renderer evidence schema_version must be 1")

    duration = _renderer_finite_number(evidence.get("duration_s"), "duration_s", minimum=0.0)
    motion_intensity = evidence.get("motion_intensity")
    if motion_intensity not in {"low", "medium", "high"}:
        raise ValueError("motion_intensity must be low, medium, or high")
    items = evidence.get("items")
    if not isinstance(items, list):
        raise ValueError("items must be a list")
    expected_visual_beat_count = evidence.get("expected_visual_beat_count")
    if (
        isinstance(expected_visual_beat_count, bool)
        or not isinstance(expected_visual_beat_count, int)
        or expected_visual_beat_count < 0
    ):
        raise ValueError("expected_visual_beat_count must be a non-negative integer")

    component_ids: set[str] = set()
    skin_ids: set[str] = set()
    font_sizes: list[float] = []
    intervals: list[tuple[float, float]] = []
    requested_motion_count = 0
    faithful_motion_count = 0
    fallback_motion_count = 0
    unfaithful_motion: list[tuple[str, str]] = []

    for item_index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"items[{item_index}] must be an object")
        item_id = _renderer_identifier(item, "id", item_index, required=True)
        _renderer_identifier(item, "kind", item_index, required=True)
        start = _renderer_finite_number(item.get("start"), f"items[{item_index}].start")
        end = _renderer_finite_number(item.get("end"), f"items[{item_index}].end")
        if end < start:
            raise ValueError(f"items[{item_index}].end must be >= start")
        intervals.append((start, end))

        component_id = _renderer_identifier(item, "component_id", item_index)
        if component_id is not None:
            component_ids.add(component_id)
        style_pack_id = _renderer_identifier(item, "style_pack_id", item_index)
        if style_pack_id is not None:
            skin_ids.add(style_pack_id)

        font_evidence_required = item.get("font_evidence_required")
        if not isinstance(font_evidence_required, bool):
            raise ValueError(
                f"items[{item_index}].font_evidence_required must be a boolean"
            )
        if "minimum_primary_font_px" in item:
            raw_font_size = item.get("minimum_primary_font_px")
            if raw_font_size is not None:
                font_sizes.append(
                    _renderer_finite_number(
                        raw_font_size,
                        f"items[{item_index}].minimum_primary_font_px",
                        minimum=0.0,
                    )
                )
        if font_evidence_required and item.get("minimum_primary_font_px") is None:
            raise ValueError(
                f"items[{item_index}].minimum_primary_font_px is required"
            )

        motion = item.get("motion")
        if motion is None:
            if motion_intensity == "high":
                requested_motion_count += 1
                fallback_motion_count += 1
                unfaithful_motion.append((str(item_id), "motion evidence missing"))
            continue
        if not isinstance(motion, dict):
            raise ValueError(f"items[{item_index}].motion must be an object")
        for field in ("requested", "delivered", "faithful", "status"):
            if field not in motion:
                raise ValueError(f"items[{item_index}].motion.{field} is required")
        requested = _renderer_motion_requested(
            motion.get("requested"), f"items[{item_index}].motion.requested"
        )
        delivered = motion.get("delivered")
        if not isinstance(delivered, str):
            raise ValueError(f"items[{item_index}].motion.delivered must be a string")
        faithful = motion.get("faithful")
        if not isinstance(faithful, bool):
            raise ValueError(f"items[{item_index}].motion.faithful must be a boolean")
        status = motion.get("status")
        if not isinstance(status, str) or not status:
            raise ValueError(f"items[{item_index}].motion.status must be a non-empty string")
        if "reason" in motion and motion.get("reason") is not None and not isinstance(
            motion.get("reason"), str
        ):
            raise ValueError(f"items[{item_index}].motion.reason must be a string")
        normalized_status = status.strip().lower()
        normalized_delivery = delivered.strip().lower()
        if motion_intensity == "high" and (
            normalized_status == "fallback"
            or (faithful and normalized_delivery in {"", "none", "static"})
        ):
            faithful = False
        if not requested:
            if motion_intensity == "high":
                requested_motion_count += 1
                fallback_motion_count += 1
                unfaithful_motion.append((str(item_id), "motion evidence missing"))
            continue
        requested_motion_count += 1
        if faithful:
            faithful_motion_count += 1
        if status == "fallback" or not faithful:
            fallback_motion_count += 1
        if not faithful:
            reason = motion.get("reason") or "requested motion was not delivered faithfully"
            unfaithful_motion.append((str(item_id), str(reason)))

    minimum_font = min(font_sizes) if font_sizes else None
    failures: list[str] = []
    warnings: list[str] = []
    visual_beat_count = len(items)
    if visual_beat_count != expected_visual_beat_count:
        failures.append(
            f"renderer delivered {visual_beat_count} visual beats but expected "
            f"{expected_visual_beat_count}"
        )
    if visual_beat_count == 0:
        warnings.append("renderer evidence contains no visual beats")
    if visual_beat_count and minimum_font is not None and minimum_font < 32.0:
        failures.append(
            f"minimum primary font size {minimum_font:g}px is below the 32px floor"
        )
    for item_id, reason in unfaithful_motion:
        message = f"requested motion for {item_id} is not faithful: {reason}"
        if motion_intensity == "high":
            failures.append(message)
        else:
            warnings.append(message)

    ratio = (
        round(faithful_motion_count / requested_motion_count, 6)
        if requested_motion_count
        else None
    )
    return {
        "schema_version": 1,
        "source": "renderer_evidence",
        "status": "fail" if failures else "pass",
        "duration_s": duration,
        "minimum_primary_font_px": minimum_font,
        "expected_visual_beat_count": expected_visual_beat_count,
        "visual_beat_count": visual_beat_count,
        "component_ids": sorted(component_ids),
        "component_count": len(component_ids),
        "skin_ids": sorted(skin_ids),
        "skin_count": len(skin_ids),
        "longest_no_change_gap_s": _renderer_longest_no_change_gap(intervals, duration),
        "motion_requested_count": requested_motion_count,
        "motion_faithful_count": faithful_motion_count,
        "motion_fallback_count": fallback_motion_count,
        "motion_faithful_ratio": ratio,
        "failures": failures,
        "warnings": warnings,
    }


def rendered_visual_evidence_path(project_dir: Path, render_id: str) -> Path:
    """Project-owned receipt path for one frozen render identity."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", render_id):
        raise ValueError("render visual evidence identity is invalid")
    return project_dir / RENDER_VISUAL_EVIDENCE_REL / f"{render_id}.json"
