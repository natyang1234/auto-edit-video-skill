#!/usr/bin/env python3
"""Highlight-scoped visual planning and fail-closed visual-quality contracts."""

from __future__ import annotations

import math
import re
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import contract_registry


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
# Choosing a director is choosing an editing language, not a colour swatch:
# the card copy and the card motion belong to the profile, so switching to
# ``minimal`` cannot leave a classroom kicker like "30 秒重點課" on screen.
DIRECTOR_ROLE_KICKERS = {
    "teacher-punch": dict(ROLE_KICKERS),
    "high-energy": {
        "hook": "先別滑走",
        "concept": "重點在這",
        "rule": "關鍵一句",
        "memory": "記住這招",
        "recap": "三秒收尾",
    },
    "documentary": {
        "hook": "事件開場",
        "concept": "背景是這樣",
        "rule": "爭議焦點",
        "memory": "值得記住",
        "recap": "重點回顧",
    },
    "editorial-clean": {
        "hook": "開場",
        "concept": "重點",
        "rule": "關鍵",
        "memory": "補充",
        "recap": "收尾",
    },
    "minimal": {
        "hook": "現場",
        "concept": "這裡",
        "rule": "重點",
        "memory": "順帶一提",
        "recap": "收工",
    },
    "kinetic-explainer": {
        "hook": "開場動畫",
        "concept": "概念拆解",
        "rule": "核心規則",
        "memory": "記憶點",
        "recap": "快速複習",
    },
}
# Motion intensity mirrors the registry envelope: low-motion profiles get one
# calm entry for every card, high-motion profiles keep the punchy entries.
DIRECTOR_MOTION = {
    "teacher-punch": None,
    "high-energy": {"hook": "pop", "concept": "pop", "rule": "pop", "memory": "pop", "recap": "pop"},
    "documentary": {
        "hook": "fade",
        "concept": "fade",
        "rule": "fade",
        "memory": "fade",
        "recap": "fade",
    },
    "editorial-clean": {
        "hook": "fade",
        "concept": "fade",
        "rule": "fade",
        "memory": "fade",
        "recap": "fade",
    },
    "minimal": {
        "hook": "fade",
        "concept": "fade",
        "rule": "fade",
        "memory": "fade",
        "recap": "fade",
    },
    "kinetic-explainer": {
        "hook": "slide-up",
        "concept": "slide-up",
        "rule": "pop",
        "memory": "slide-up",
        "recap": "pop",
    },
}


def director_role_kickers(director_style: str) -> dict[str, str]:
    """Return one director's card vocabulary, falling back to the default."""
    return dict(DIRECTOR_ROLE_KICKERS.get(director_style) or ROLE_KICKERS)


def director_role_animation(director_style: str, role: str, fallback: str) -> str:
    """Return one director's card entry animation for a designed role."""
    overrides = DIRECTOR_MOTION.get(director_style)
    if not overrides:
        return fallback
    return str(overrides.get(role) or fallback)


def director_overlay_animation(director_style: str, overlay_type: str, fallback: str) -> str:
    """Return one director's entry animation for a plan-derived overlay."""
    role = {"title": "hook", "card": "concept", "animation": "rule"}.get(overlay_type)
    if role is None:
        return fallback
    return director_role_animation(director_style, role, fallback)
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
    animation = director_role_animation(director_style, role, animation)
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
    kickers = director_role_kickers(director_style)
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
                "kicker": kickers[role],
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


_SCENE_SEMANTIC_FIELDS = {
    "eligibility",
    "eligibility_reason",
    "family",
    "role",
    "importance",
    "major_graphic",
    "micro_silent",
    "stage",
    "trigger_role",
}
_SCENE_RESOLVED_FIELDS = _SCENE_SEMANTIC_FIELDS | {
    "motion_window_start_sample",
    "motion_window_end_sample",
    "graphic_roi",
    "presenter_roi",
    "static_fallback",
}
_SCENE_FAMILIES = {
    "title_reveal",
    "staggered_list",
    "analytics_dashboard",
    "count_stat",
    "asset_mosaic",
    "grid_progress",
    "typed_prompt",
}
_INELIGIBLE_REASONS = {
    "missing_transcript_evidence",
    "unsupported_payload",
    "missing_licensed_asset",
    "density_budget",
    "layout_collision",
}
_AUTHORITY_HASH_FIELDS = (
    "visual_plan_revision",
    "visual_plan_sha256",
    "structured_layers_sha256",
    "artifact_index_sha256",
)
_AUTHORITY_SCENE_FIELDS = (
    "id",
    "start",
    "end",
    "kind",
    "family",
    "role",
    "structured_layer_id",
    "structured_layer_hash",
    "artifact_hash",
    "evidence_id",
    "source_literal",
    "assets",
    "graphic_roi",
    "presenter_roi",
    "motion_window_start_sample",
    "motion_window_end_sample",
)


def _renderer_roi(value: Any, field: str) -> dict[str, float]:
    if not isinstance(value, dict) or set(value) != {"x", "y", "width", "height"}:
        raise ValueError(f"{field} must be an exact normalized ROI")
    roi = {
        key: _renderer_finite_number(value.get(key), f"{field}.{key}", minimum=0.0)
        for key in ("x", "y", "width", "height")
    }
    if roi["width"] <= 0 or roi["height"] <= 0:
        raise ValueError(f"{field} width and height must be positive")
    if roi["x"] + roi["width"] > 1.0 or roi["y"] + roi["height"] > 1.0:
        raise ValueError(f"{field} must remain inside the normalized frame")
    return roi


def _final_sample(value: float) -> int:
    return int(
        (Decimal(str(value)) * Decimal(48000)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def _renderer_scene_failures(
    item: dict[str, Any], item_index: int, start: float, end: float
) -> list[str]:
    if not _SCENE_RESOLVED_FIELDS.intersection(item):
        return []
    path = f"items[{item_index}]"
    missing = sorted(_SCENE_RESOLVED_FIELDS.difference(item))
    if missing:
        raise ValueError(f"{path} scene receipt is missing fields: {missing}")
    if item.get("eligibility") not in {"eligible", "ineligible"}:
        raise ValueError(f"{path}.eligibility is invalid")
    reason = item.get("eligibility_reason")
    if item.get("eligibility") == "eligible" and reason is not None:
        raise ValueError(f"{path}.eligibility_reason must be null when eligible")
    if item.get("eligibility") == "ineligible" and reason not in _INELIGIBLE_REASONS:
        raise ValueError(f"{path}.eligibility_reason is invalid")
    if item.get("family") not in _SCENE_FAMILIES:
        raise ValueError(f"{path}.family is invalid")
    if item.get("importance") not in {"low", "medium", "high"}:
        raise ValueError(f"{path}.importance is invalid")
    for field in ("major_graphic", "micro_silent", "static_fallback"):
        if not isinstance(item.get(field), bool):
            raise ValueError(f"{path}.{field} must be a boolean")
    if item.get("stage") not in {"full_screen_graphic", "split_graphic_presenter"}:
        raise ValueError(f"{path}.stage is invalid")
    if item.get("trigger_role") not in {
        None,
        "title_enter",
        "scene_transition",
        "row_reveal",
        "count_complete",
        "chart_complete",
        "grid_complete",
        "typing",
    }:
        raise ValueError(f"{path}.trigger_role is invalid")

    motion_start = item.get("motion_window_start_sample")
    motion_end = item.get("motion_window_end_sample")
    if (
        type(motion_start) is not int
        or type(motion_end) is not int
        or motion_start < _final_sample(start)
        or motion_end > _final_sample(end)
        or motion_end <= motion_start
    ):
        raise ValueError(f"{path}.motion_window must be positive and inside the scene")
    graphic = _renderer_roi(item.get("graphic_roi"), f"{path}.graphic_roi")
    presenter = None
    if item.get("presenter_roi") is not None:
        presenter = _renderer_roi(item.get("presenter_roi"), f"{path}.presenter_roi")
    if item.get("stage") == "split_graphic_presenter":
        if presenter is None:
            raise ValueError(f"{path}.presenter_roi is required for split stage")
        overlap_width = max(
            0.0,
            min(graphic["x"] + graphic["width"], presenter["x"] + presenter["width"])
            - max(graphic["x"], presenter["x"]),
        )
        overlap_height = max(
            0.0,
            min(graphic["y"] + graphic["height"], presenter["y"] + presenter["height"])
            - max(graphic["y"], presenter["y"]),
        )
        if overlap_width * overlap_height > 1e-9:
            raise ValueError(f"{path} split-stage ROIs overlap")

    motion = item.get("motion")
    if not isinstance(motion, dict):
        raise ValueError(f"{path}.motion is required for a resolved scene")
    observed_fallback = (
        motion.get("status") == "fallback" or motion.get("faithful") is not True
    )
    if item.get("static_fallback") is not observed_fallback:
        raise ValueError(f"{path}.static_fallback disagrees with renderer motion")

    failures: list[str] = []
    if item.get("role") == "section_title" and (
        item.get("eligibility") != "eligible"
        or item.get("family") != "title_reveal"
        or item.get("importance") != "high"
        or item.get("major_graphic") is not True
        or item.get("micro_silent") is not False
        or item.get("stage") != "split_graphic_presenter"
        or item.get("trigger_role") != "scene_transition"
    ):
        failures.append(f"{path} section-title scene contract is inconsistent")
    if item.get("role") == "opening_title" and (
        item.get("eligibility") != "eligible"
        or item.get("family") != "title_reveal"
        or item.get("importance") != "high"
        or item.get("major_graphic") is not True
        or item.get("micro_silent") is not False
        or item.get("stage") != "full_screen_graphic"
        or item.get("trigger_role") != "title_enter"
    ):
        failures.append(f"{path} opening-title scene contract is inconsistent")
    if item.get("role") == "metric_emphasis" and (
        item.get("eligibility") != "eligible"
        or item.get("family") != "count_stat"
        or item.get("importance") != "high"
        or item.get("major_graphic") is not True
        or item.get("micro_silent") is not False
        or item.get("stage") != "split_graphic_presenter"
        or item.get("trigger_role") != "count_complete"
    ):
        failures.append(f"{path} metric-emphasis scene contract is inconsistent")
    if item.get("role") == "list_explanation" and (
        item.get("eligibility") != "eligible"
        or item.get("family") != "staggered_list"
        or item.get("importance") != "medium"
        or item.get("major_graphic") is not True
        or item.get("micro_silent") is not False
        or item.get("stage") != "split_graphic_presenter"
        or item.get("trigger_role") != "row_reveal"
    ):
        failures.append(f"{path} list-explanation scene contract is inconsistent")
    if item.get("role") == "data_explanation" and (
        item.get("eligibility") != "eligible"
        or item.get("family") != "analytics_dashboard"
        or item.get("importance") != "high"
        or item.get("major_graphic") is not True
        or item.get("micro_silent") is not False
        or item.get("stage") != "split_graphic_presenter"
        or item.get("trigger_role") != "chart_complete"
    ):
        failures.append(f"{path} data-explanation scene contract is inconsistent")
    if item.get("role") == "prompt_command" and (
        item.get("eligibility") != "eligible"
        or item.get("family") != "typed_prompt"
        or item.get("importance") != "medium"
        or item.get("major_graphic") is not True
        or item.get("micro_silent") is not False
        or item.get("stage") != "split_graphic_presenter"
        or item.get("trigger_role") != "typing"
    ):
        failures.append(f"{path} prompt-command scene contract is inconsistent")
    if item.get("role") == "workflow_progress" and (
        item.get("eligibility") != "eligible"
        or item.get("family") != "grid_progress"
        or item.get("importance") != "medium"
        or item.get("major_graphic") is not True
        or item.get("micro_silent") is not False
        or item.get("stage") != "split_graphic_presenter"
        or item.get("trigger_role") != "grid_complete"
    ):
        failures.append(f"{path} workflow-progress scene contract is inconsistent")
    if item.get("role") == "asset_showcase" and (
        item.get("eligibility") != "eligible"
        or item.get("family") != "asset_mosaic"
        or item.get("importance") != "high"
        or item.get("major_graphic") is not True
        or item.get("micro_silent") is not False
        or item.get("stage") != "split_graphic_presenter"
        or item.get("trigger_role") != "scene_transition"
    ):
        failures.append(f"{path} asset-showcase scene contract is inconsistent")
    if item.get("major_graphic") is True:
        if graphic["width"] * graphic["height"] < 0.15:
            failures.append(f"{path} major graphic ROI covers less than 15% of frame")
        if end - start < 0.8:
            failures.append(f"{path} major graphic lasts less than 0.8 seconds")
    if item.get("eligibility") == "eligible" and item.get("static_fallback") is True:
        failures.append(f"{path} eligible scene was delivered as a static fallback")
    return failures


def _visual_authority_failures(
    evidence_items: list[dict[str, Any]],
    breathing_intervals: Any,
    motion_input: Any,
    motion_attribution: Any,
    authority: dict[str, Any],
) -> list[str]:
    if (
        type(authority) is not dict
        or type(authority.get("schema_version")) is not int
        or authority.get("schema_version") != 1
        or authority.get("source") != "frozen_visual_authority"
    ):
        raise ValueError("visual authority schema/source is invalid")
    for field in _AUTHORITY_HASH_FIELDS:
        value = authority.get(field)
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError(f"visual authority {field} must be lowercase SHA-256")
    declared_hash = authority.get("authority_hash")
    if not isinstance(declared_hash, str) or re.fullmatch(
        r"[0-9a-f]{64}", declared_hash
    ) is None:
        raise ValueError("visual authority hash must be lowercase SHA-256")
    hash_material = {key: value for key, value in authority.items() if key != "authority_hash"}
    if contract_registry.canonical_hash(hash_material) != declared_hash:
        raise ValueError("visual authority hash does not match its canonical payload")
    expected_items = authority.get("items")
    if not isinstance(expected_items, list):
        raise ValueError("visual authority items must be a list")
    expected_breathing = authority.get("a_roll_breathing_intervals")
    if not isinstance(expected_breathing, list):
        raise ValueError("visual authority A-roll breathing intervals must be a list")

    def index(items: list[dict[str, Any]], label: str) -> tuple[dict[str, dict[str, Any]], list[str]]:
        by_id: dict[str, dict[str, Any]] = {}
        duplicates: list[str] = []
        for position, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValueError(f"{label}[{position}] must be an object")
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id:
                raise ValueError(f"{label}[{position}].id is required")
            if item_id in by_id:
                duplicates.append(item_id)
            else:
                by_id[item_id] = item
        return by_id, duplicates

    expected_by_id, expected_duplicates = index(expected_items, "visual authority items")
    observed_by_id, observed_duplicates = index(evidence_items, "renderer evidence items")
    failures = [
        f"visual authority has duplicate scene id {item_id}"
        for item_id in expected_duplicates
    ]
    failures.extend(
        f"renderer evidence has duplicate scene id {item_id}"
        for item_id in observed_duplicates
    )
    if breathing_intervals != expected_breathing:
        failures.append(
            "renderer A-roll breathing intervals do not match frozen authority"
        )
    expected_motion_attribution = {
        str(item["id"]): item["motion_attribution"]
        for item in expected_items
        if "motion_attribution" in item
    }
    if expected_motion_attribution:
        expected_motion_input = authority.get("motion_input")
        if not isinstance(expected_motion_input, dict):
            raise ValueError("visual authority motion_input must be an object")
        if not isinstance(motion_input, dict):
            raise ValueError("renderer motion_input must be an object")
        if (
            {key: value for key, value in motion_input.items() if key != "base_path"}
            != expected_motion_input
        ):
            failures.append("renderer motion input does not match frozen authority")
        if not isinstance(motion_attribution, dict):
            raise ValueError("renderer motion_attribution must be an object")
        if motion_attribution != expected_motion_attribution:
            failures.append("renderer motion attribution does not match frozen authority")
    for item_id in sorted(expected_by_id.keys() - observed_by_id.keys()):
        failures.append(f"renderer evidence is missing authority scene {item_id}")
    for item_id in sorted(observed_by_id.keys() - expected_by_id.keys()):
        failures.append(f"renderer evidence has extra scene {item_id}")
    for item_id in sorted(expected_by_id.keys() & observed_by_id.keys()):
        expected = expected_by_id[item_id]
        observed = observed_by_id[item_id]
        missing = [field for field in _AUTHORITY_SCENE_FIELDS if field not in expected]
        if missing:
            raise ValueError(
                f"visual authority scene {item_id} is missing fields: {missing}"
            )
        for field in _AUTHORITY_SCENE_FIELDS:
            if observed.get(field) != expected[field]:
                failures.append(
                    f"renderer scene {item_id} {field} does not match frozen authority"
                )
    return failures


def _motion_probe_failures(
    evidence_items: list[dict[str, Any]], motion_probes: Any, motion_input: Any
) -> list[str]:
    """Validate exact per-scene final-pixel motion observations."""
    if not isinstance(motion_probes, dict):
        raise ValueError("motion_probes must be an object")
    major_items = {
        str(item.get("id") or ""): item
        for item in evidence_items
        if item.get("major_graphic") is True
    }
    if set(motion_probes) != set(major_items):
        raise ValueError("motion_probes must exactly cover every major graphic")
    if not major_items:
        return []
    if not isinstance(motion_input, dict):
        raise ValueError("motion_input must be an object")
    fps = motion_input.get("fps")
    if type(fps) is not int or fps <= 0 or 48_000 % fps:
        raise ValueError("motion_input.fps must divide the evidence sample rate")
    frame_samples = 48_000 // fps
    failures: list[str] = []
    for scene_id, item in major_items.items():
        probe = motion_probes.get(scene_id)
        if not isinstance(probe, dict) or set(probe) != {
            "sample_positions",
            "graphic_roi",
            "candidate_matches",
            "pairs",
            "detected",
        }:
            raise ValueError(f"motion probe {scene_id} has an invalid shape")
        start = item.get("motion_window_start_sample")
        end = item.get("motion_window_end_sample")
        if type(start) is not int or type(end) is not int or end <= start:
            raise ValueError(f"motion probe {scene_id} has no valid scene window")
        first_frame = (start + frame_samples - 1) // frame_samples
        last_frame = end // frame_samples
        if last_frame - first_frame + 1 < 3:
            raise ValueError(
                f"motion probe {scene_id} window contains fewer than three frames"
            )
        expected_positions = []
        for fraction in (0.1, 0.5, 0.9):
            target = start + int((end - start) * fraction)
            frame_index = (target + frame_samples // 2) // frame_samples
            frame_index = max(first_frame, min(last_frame, frame_index))
            position = frame_index * frame_samples
            if expected_positions and position <= expected_positions[-1]:
                position = expected_positions[-1] + frame_samples
            expected_positions.append(position)
        if expected_positions[-1] > last_frame * frame_samples:
            expected_positions = [
                (last_frame - 2 + index) * frame_samples for index in range(3)
            ]
        if probe.get("sample_positions") != expected_positions:
            raise ValueError(f"motion probe {scene_id} sampled the wrong positions")
        if probe.get("graphic_roi") != item.get("graphic_roi"):
            raise ValueError(f"motion probe {scene_id} sampled the wrong ROI")
        candidate_matches = probe.get("candidate_matches")
        if not isinstance(candidate_matches, list) or len(candidate_matches) != 3:
            raise ValueError(
                f"motion probe {scene_id} must contain three candidate matches"
            )
        for match_index, (match, expected_sample) in enumerate(
            zip(candidate_matches, expected_positions, strict=True)
        ):
            if not isinstance(match, dict) or set(match) != {
                "sample",
                "ssim",
                "matched",
            }:
                raise ValueError(
                    f"motion probe {scene_id} candidate_matches[{match_index}] "
                    "has an invalid shape"
                )
            if match.get("sample") != expected_sample:
                raise ValueError(
                    f"motion probe {scene_id} candidate_matches[{match_index}] "
                    "sample is invalid"
                )
            _renderer_finite_number(
                match.get("ssim"),
                f"motion probe {scene_id} candidate_matches[{match_index}].ssim",
            )
            if not isinstance(match.get("matched"), bool):
                raise ValueError(
                    f"motion probe {scene_id} candidate_matches[{match_index}].matched "
                    "is invalid"
                )
        pairs = probe.get("pairs")
        if not isinstance(pairs, list) or len(pairs) != 3:
            raise ValueError(f"motion probe {scene_id} must contain three pairs")
        for pair_index, pair in enumerate(pairs):
            if not isinstance(pair, dict) or set(pair) != {
                "left_sample",
                "right_sample",
                "ssim",
                "changed_pixel_fraction",
                "detected",
            }:
                raise ValueError(
                    f"motion probe {scene_id} pairs[{pair_index}] has an invalid shape"
                )
            for field in ("left_sample", "right_sample"):
                if type(pair.get(field)) is not int or pair[field] < 0:
                    raise ValueError(
                        f"motion probe {scene_id} pairs[{pair_index}].{field} is invalid"
                    )
            for field in ("ssim", "changed_pixel_fraction"):
                _renderer_finite_number(
                    pair.get(field),
                    f"motion probe {scene_id} pairs[{pair_index}].{field}",
                )
            if not isinstance(pair.get("detected"), bool):
                raise ValueError(
                    f"motion probe {scene_id} pairs[{pair_index}].detected is invalid"
                )
        if not isinstance(probe.get("detected"), bool):
            raise ValueError(f"motion probe {scene_id}.detected must be a boolean")
        if not all(match["matched"] for match in candidate_matches):
            failures.append(
                f"major graphic {scene_id} does not match frozen composite states"
            )
        if probe["detected"] is not True:
            failures.append(
                f"major graphic {scene_id} has no detected final-pixel motion"
            )
    return failures


def rendered_visual_quality_report(
    evidence: dict[str, Any], authority: dict[str, Any] | None = None
) -> dict[str, Any]:
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
    scene_failures: list[str] = []

    for item_index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"items[{item_index}] must be an object")
        item_id = _renderer_identifier(item, "id", item_index, required=True)
        kind = _renderer_identifier(item, "kind", item_index, required=True)
        start = _renderer_finite_number(item.get("start"), f"items[{item_index}].start")
        end = _renderer_finite_number(item.get("end"), f"items[{item_index}].end")
        if end < start:
            raise ValueError(f"items[{item_index}].end must be >= start")
        intervals.append((start, end))
        scene_failures.extend(
            _renderer_scene_failures(item, item_index, start, end)
        )

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
        if kind == "mosaic":
            if component_id != "asset-mosaic":
                raise ValueError(
                    f"items[{item_index}].component_id must be asset-mosaic for mosaic"
                )
            for field in ("structured_layer_hash", "artifact_hash"):
                value = item.get(field)
                if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                    raise ValueError(f"items[{item_index}].{field} must be lowercase SHA-256")
            evidence_id = _renderer_identifier(
                item, "evidence_id", item_index, required=True
            )
            source_literal = _renderer_identifier(
                item, "source_literal", item_index, required=True
            )
            assets = item.get("assets")
            if not isinstance(assets, list) or not 2 <= len(assets) <= 4:
                raise ValueError(
                    f"items[{item_index}].assets must contain two to four snapshots"
                )
            seen_assets: set[tuple[str, str]] = set()
            for asset_index, asset in enumerate(assets):
                asset_path = f"items[{item_index}].assets[{asset_index}]"
                if not isinstance(asset, dict) or set(asset) != {
                    "asset_id", "path", "sha256", "evidence_id", "source_literal"
                }:
                    raise ValueError(f"{asset_path} must be an exact frozen descriptor")
                asset_id = asset.get("asset_id")
                relative = asset.get("path")
                digest = asset.get("sha256")
                if not isinstance(asset_id, str) or not asset_id:
                    raise ValueError(f"{asset_path}.asset_id is required")
                if (
                    not isinstance(relative, str)
                    or not relative
                    or relative.startswith("/")
                    or "\\" in relative
                    or any(part in {"", ".", ".."} for part in relative.split("/"))
                ):
                    raise ValueError(f"{asset_path}.path must be normalized project-relative")
                if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                    raise ValueError(f"{asset_path}.sha256 must be lowercase SHA-256")
                if asset.get("evidence_id") != evidence_id or asset.get(
                    "source_literal"
                ) != source_literal:
                    raise ValueError(f"{asset_path} transcript binding is inconsistent")
                identity = (asset_id, relative)
                if identity in seen_assets:
                    raise ValueError(f"{asset_path} duplicates an earlier snapshot")
                seen_assets.add(identity)

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
    failures: list[str] = list(scene_failures)
    if authority is not None:
        breathing_intervals = evidence.get("a_roll_breathing_intervals")
        failures.extend(
            _visual_authority_failures(
                items,
                breathing_intervals,
                evidence.get("motion_input"),
                evidence.get("motion_attribution"),
                authority,
            )
        )
        failures.extend(
            _motion_probe_failures(
                items,
                evidence.get("motion_probes"),
                evidence.get("motion_input"),
            )
        )
        import visual_scene_coverage

        scene_coverage = visual_scene_coverage.evaluate_scene_coverage(
            duration,
            breathing_intervals,
            [item for item in items if item.get("major_graphic") is True],
        )
        failures.extend(scene_coverage["failures"])
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
    report = {
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
        # Downstream motion/audio QA must recompute trigger identity and final
        # timing from item-level renderer observations. Aggregate counts alone
        # cannot prove which title or animation was actually delivered.
        "items": items,
        "failures": failures,
        "warnings": warnings,
    }
    if authority is not None:
        report["a_roll_breathing_intervals"] = breathing_intervals
        report["scene_coverage"] = scene_coverage
        report["motion_probes"] = evidence["motion_probes"]
        report["authority"] = authority
        report["authority_hash"] = authority["authority_hash"]
        report["raw_evidence"] = evidence
    return report


def rendered_visual_evidence_path(project_dir: Path, render_id: str) -> Path:
    """Project-owned receipt path for one frozen render identity."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", render_id):
        raise ValueError("render visual evidence identity is invalid")
    return project_dir / RENDER_VISUAL_EVIDENCE_REL / f"{render_id}.json"
