#!/usr/bin/env python3
"""Highlight-scoped visual planning and fail-closed visual-quality contracts."""

from __future__ import annotations

import math
import re
from typing import Any


DESIGN_ROLES = ("hook", "concept", "rule", "memory", "recap")
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
    title = _trim_text(highlight.get("title"), 48)
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
