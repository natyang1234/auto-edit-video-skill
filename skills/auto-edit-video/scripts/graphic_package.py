#!/usr/bin/env python3
"""Compile reviewed highlight cards into a local talking-head graphic package."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import uuid
import copy
from pathlib import Path
from typing import Any

from visual_quality import DESIGN_ROLES, overlays_for_clip
from template_catalog import (
    DEFAULT_VIDEO_TEMPLATE_ID,
    VIDEO_TEMPLATES,
    cutout_capability,
    default_video_template_state,
    template_readiness_errors,
)


TEMPLATE_VERSION = "multi-template-v1"
ROLE_INDEX = {role: index for index, role in enumerate(DESIGN_ROLES)}
DEFAULT_CARD_BOUNDS = {
    "hook": (0, 0, 1080, 1920),
    "concept": (40, 860, 1000, 430),
    "rule": (40, 820, 1000, 520),
    "memory": (70, 155, 940, 290),
    "recap": (45, 330, 990, 920),
}
ALLOWED_EFFECTS = {"pop", "highlight", "underline"}


def _safe_text(value: Any, limit: int = 120) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _bounded_number(value: Any, fallback: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if not minimum <= number <= maximum:
        return fallback
    return number


def _safe_color(value: Any, fallback: str) -> str:
    color = str(value or "").strip().lower()
    return color if re.fullmatch(r"#[0-9a-f]{6}", color) else fallback


def _layout_bounds(item: dict[str, Any], role: str) -> dict[str, int]:
    layout = item.get("layout") if isinstance(item.get("layout"), dict) else None
    if not layout:
        x, y, width, height = DEFAULT_CARD_BOUNDS[role]
        return {"x": x, "y": y, "width": width, "height": height}
    width_pct = _bounded_number(layout.get("width"), 90.0, 1.0, 100.0)
    height_pct = _bounded_number(layout.get("height"), 24.0, 1.0, 100.0)
    center_x = _bounded_number(layout.get("x"), 50.0, 0.0, 100.0) * 10.8
    center_y = _bounded_number(layout.get("y"), 50.0, 0.0, 100.0) * 19.2
    width = round(width_pct * 10.8)
    height = round(height_pct * 19.2)
    return {
        "x": round(center_x - width / 2),
        "y": round(center_y - height / 2),
        "width": width,
        "height": height,
    }


def package_cards(state: dict[str, Any], clip: dict[str, Any]) -> list[dict[str, Any]]:
    clip_start = float(clip.get("start", 0.0))
    clip_end = float(clip.get("end", clip_start))
    duration = clip_end - clip_start
    if duration <= 0:
        raise ValueError("graphic package clip must have a positive duration")
    candidates = [
        item
        for item in overlays_for_clip(state, clip)
        if str(item.get("design_role") or "") in ROLE_INDEX
    ]
    candidates.sort(key=lambda item: ROLE_INDEX[str(item.get("design_role"))])
    by_role = {str(item.get("design_role")): item for item in candidates}
    missing = [role for role in DESIGN_ROLES if role not in by_role]
    if missing:
        raise ValueError("graphic package requires reviewed cards for: " + ", ".join(missing))
    cards: list[dict[str, Any]] = []
    for index, role in enumerate(DESIGN_ROLES, start=1):
        item = by_role[role]
        start = max(0.0, float(item.get("start", clip_start)) - clip_start)
        end = min(duration, float(item.get("end", clip_end)) - clip_start)
        if end <= start:
            raise ValueError(f"graphic package card {role} is outside the selected clip")
        cards.append(
            {
                "id": f"card-{index:02d}",
                "role": role,
                "start": round(start, 4),
                "end": round(end, 4),
                "text": _safe_text(item.get("text"), 90),
                "kicker": _safe_text(item.get("kicker"), 32),
                "detail": _safe_text(item.get("detail"), 90),
                "source_overlay_id": str(item.get("id")),
                "bounds": _layout_bounds(item, role),
            }
        )
    return cards


def _normalized_effect_spans(
    text: str,
    raw_spans: Any,
    legacy_phrases: Any,
    default_color: str,
    whole_text_effect: bool = False,
) -> list[dict[str, Any]]:
    candidates = raw_spans if isinstance(raw_spans, list) else []
    normalized: list[dict[str, Any]] = []
    for index, span in enumerate(candidates[:50], start=1):
        if not isinstance(span, dict):
            continue
        start = span.get("start_char")
        end = span.get("end_char")
        if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int):
            continue
        if start < 0 or end <= start or end > len(text) or text[start:end] != str(span.get("text") or ""):
            continue
        raw_style = span.get("style") if isinstance(span.get("style"), dict) else {}
        effect = str(raw_style.get("effect") or "pop")
        if effect not in ALLOWED_EFFECTS:
            effect = "pop"
        normalized.append(
            {
                "id": re.sub(r"[^A-Za-z0-9_-]", "-", str(span.get("id") or f"fx-{index}"))[:80],
                "text": text[start:end],
                "start_char": start,
                "end_char": end,
                "style": {
                    "effect": effect,
                    "color": _safe_color(raw_style.get("color"), default_color),
                    "font_scale": _bounded_number(raw_style.get("font_scale"), 1.18, 0.5, 3.0),
                },
            }
        )
    if not normalized and isinstance(legacy_phrases, list):
        cursor = 0
        for index, phrase_value in enumerate(legacy_phrases[:50], start=1):
            phrase = str(phrase_value or "")
            start = text.find(phrase, cursor) if phrase else -1
            if start < 0:
                continue
            end = start + len(phrase)
            normalized.append(
                {
                    "id": f"legacy-fx-{index}",
                    "text": phrase,
                    "start_char": start,
                    "end_char": end,
                    "style": {"effect": "pop", "color": default_color, "font_scale": 1.18},
                }
            )
            cursor = end
    if not normalized and whole_text_effect and text:
        normalized.append(
            {
                "id": "whole-text-fx",
                "text": text,
                "start_char": 0,
                "end_char": len(text),
                "style": {"effect": "pop", "color": default_color, "font_scale": 1.18},
            }
        )
    normalized.sort(key=lambda item: (item["start_char"], item["end_char"]))
    result: list[dict[str, Any]] = []
    cursor = -1
    for span in normalized:
        if span["start_char"] < cursor:
            continue
        result.append(span)
        cursor = span["end_char"]
    return result


def _caption_replacement_intervals(state: dict[str, Any], clip: dict[str, Any]) -> list[tuple[float, float]]:
    intervals: list[tuple[float, float]] = []
    for item in overlays_for_clip(state, clip):
        if item.get("design_role") != "hook":
            continue
        bounds = _layout_bounds(item, "hook")
        if bounds["width"] < 1026 or bounds["height"] < 1728:
            continue
        intervals.append((float(item.get("start", 0.0)), float(item.get("end", 0.0))))
    return sorted(intervals)


def _subtract_intervals(
    start: float,
    end: float,
    blocked: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    visible = [(start, end)]
    for block_start, block_end in blocked:
        next_visible: list[tuple[float, float]] = []
        for item_start, item_end in visible:
            if block_end <= item_start or block_start >= item_end:
                next_visible.append((item_start, item_end))
                continue
            if block_start > item_start:
                next_visible.append((item_start, min(block_start, item_end)))
            if block_end < item_end:
                next_visible.append((max(block_end, item_start), item_end))
        visible = next_visible
    return [(item_start, item_end) for item_start, item_end in visible if item_end - item_start >= 0.05]


def package_captions(state: dict[str, Any], clip: dict[str, Any]) -> list[dict[str, Any]]:
    """Compile editable caption/effect layers into the same HTML used for final pixels."""
    clip_start = float(clip.get("start", 0.0))
    clip_end = float(clip.get("end", clip_start))
    duration = clip_end - clip_start
    if duration <= 0:
        raise ValueError("graphic package clip must have a positive duration")
    candidates = [
        item
        for item in overlays_for_clip(state, clip)
        if item.get("type") in {"caption", "emphasis"} and not item.get("design_role")
    ]
    candidates.sort(key=lambda item: (float(item.get("start", 0.0)), int(item.get("z_index", 0))))
    captions: list[dict[str, Any]] = []
    blocked = _caption_replacement_intervals(state, clip)
    for item in candidates:
        absolute_start = max(clip_start, float(item.get("start", clip_start)))
        absolute_end = min(clip_end, float(item.get("end", clip_end)))
        text = str(item.get("text") or "").replace("\r", "").strip()[:1000]
        if not text or absolute_end <= absolute_start:
            continue
        raw_style = item.get("style") if isinstance(item.get("style"), dict) else {}
        emphasis_color = _safe_color(raw_style.get("emphasis_color"), "#ffd447")
        font_family = str(raw_style.get("font_family") or "PingFang TC")
        if font_family not in {"PingFang TC", "Songti TC", "Avenir Next", "Arial Unicode MS", "LXGW WenKai TC", "Inter"}:
            font_family = "PingFang TC"
        style = {
            "x": _bounded_number(raw_style.get("x"), 50.0, -100.0, 200.0),
            "y": _bounded_number(raw_style.get("y"), 76.0, -100.0, 200.0),
            "max_width": _bounded_number(raw_style.get("max_width"), 84.0, 1.0, 200.0),
            "font_family": font_family,
            "font_size": _bounded_number(raw_style.get("font_size"), 58.0, 8.0, 500.0),
            "font_weight": round(_bounded_number(raw_style.get("font_weight"), 800.0, 100.0, 1000.0)),
            "color": _safe_color(raw_style.get("color"), "#f7f2e8"),
            "emphasis_color": emphasis_color,
            "stroke_color": _safe_color(raw_style.get("stroke_color"), "#17130f"),
            "stroke_width": _bounded_number(raw_style.get("stroke_width"), 4.0, 0.0, 50.0),
            "animation": str(raw_style.get("animation") or "none")
            if str(raw_style.get("animation") or "none") in {"none", "fade", "pop", "slide-up"}
            else "none",
            "box": bool(raw_style.get("box", False)),
            "box_color": _safe_color(raw_style.get("box_color"), "#17130f"),
        }
        effects = _normalized_effect_spans(
            text,
            item.get("effect_spans"),
            item.get("emphasis"),
            emphasis_color,
            item.get("type") == "emphasis",
        )
        for segment_start, segment_end in _subtract_intervals(absolute_start, absolute_end, blocked):
            captions.append(
                {
                    "id": f"caption-{len(captions) + 1:03d}",
                    "source_overlay_id": str(item.get("id") or ""),
                    "start": round(segment_start - clip_start, 4),
                    "end": round(segment_end - clip_start, 4),
                    "text": text,
                    "style": style,
                    "effect_spans": effects,
                }
            )
    return captions


def _card_fragment(card: dict[str, Any]) -> str:
    card_id = str(card["id"])
    role = str(card["role"])
    kicker = html.escape(str(card.get("kicker") or "重點"), quote=True)
    text = html.escape(str(card.get("text") or "本段重點"), quote=True)
    detail = html.escape(str(card.get("detail") or text), quote=True)
    if role == "hook":
        return f'''<div class="card" data-card-id="{card_id}">
  <style>
    .card[data-card-id="{card_id}"] .root {{ width:100%;height:100%;position:relative;overflow:hidden;background:transparent;color:#171713;font-family:"Inter","LXGW WenKai TC",sans-serif; }}
    .card[data-card-id="{card_id}"] .sheet {{ position:absolute;left:54px;right:54px;top:330px;height:890px;padding:64px 58px;border-radius:34px;background:#fdf6e3;border:5px solid #171713;box-shadow:0 34px 80px rgba(0,0,0,.36); }}
    .card[data-card-id="{card_id}"] .sheet::after {{ content:"";position:absolute;inset:18px;border:2px dashed rgba(23,23,19,.36);border-radius:20px;pointer-events:none; }}
    .card[data-card-id="{card_id}"] .kicker {{ position:relative;display:inline-block;padding:12px 22px 10px;color:#fdf6e3;background:#bf5700;border-radius:999px;font-size:34px;font-weight:800;letter-spacing:.08em; }}
    .card[data-card-id="{card_id}"] .title {{ position:relative;margin:78px 0 24px;font-size:104px;line-height:1.03;letter-spacing:-.045em;font-weight:900; }}
    .card[data-card-id="{card_id}"] .marker {{ display:inline;position:relative;z-index:1; }}
    .card[data-card-id="{card_id}"] .marker::before {{ content:"";position:absolute;left:-8px;right:-8px;top:55%;bottom:2%;z-index:-1;background:#ffe066;transform:rotate(-1.5deg); }}
    .card[data-card-id="{card_id}"] .detail {{ position:relative;margin:42px 0 0;color:#5c584b;font:700 46px/1.35 "LXGW WenKai TC",sans-serif; }}
    .card[data-card-id="{card_id}"] .arrow {{ position:absolute;right:70px;bottom:60px;color:#2557a7;font:700 62px/1 "Caveat","LXGW WenKai TC",cursive;transform:rotate(-5deg); }}
  </style>
  <div class="root"><section class="sheet" id="{card_id}-sheet"><span class="kicker" id="{card_id}-kicker">{kicker}</span><h1 class="title" id="{card_id}-title"><span class="marker">{text}</span></h1><p class="detail" id="{card_id}-detail">{detail}</p><span class="arrow" id="{card_id}-arrow">look here ↗</span></section></div>
</div>'''
    if role == "concept":
        return f'''<div class="card" data-card-id="{card_id}">
  <style>
    .card[data-card-id="{card_id}"] .root {{ width:100%;height:100%;position:relative;overflow:hidden;padding:42px 52px;color:#171713;background:#fdf6e3;border:5px solid #171713;border-radius:30px;font-family:"Inter","LXGW WenKai TC",sans-serif; }}
    .card[data-card-id="{card_id}"] .kicker {{ color:#2557a7;font:700 34px/1 "Caveat","LXGW WenKai TC",cursive;letter-spacing:.06em; }}
    .card[data-card-id="{card_id}"] .title {{ margin:30px 0 0;font-size:72px;line-height:1.12;font-weight:900;letter-spacing:-.03em; }}
    .card[data-card-id="{card_id}"] .detail {{ margin:28px 0 0;padding-top:22px;border-top:3px dashed rgba(23,23,19,.38);color:#5c584b;font:700 38px/1.25 "LXGW WenKai TC",sans-serif; }}
  </style>
  <div class="root"><div class="kicker" id="{card_id}-kicker">✎ {kicker}</div><h2 class="title" id="{card_id}-title">{text}</h2><p class="detail" id="{card_id}-detail">{detail}</p></div>
</div>'''
    if role == "rule":
        return f'''<div class="card" data-card-id="{card_id}">
  <style>
    .card[data-card-id="{card_id}"] .root {{ width:100%;height:100%;position:relative;overflow:hidden;padding:42px 52px;color:#171713;background:#fdf6e3;border:5px solid #171713;border-radius:30px;font-family:"Inter","LXGW WenKai TC",sans-serif; }}
    .card[data-card-id="{card_id}"] .kicker {{ color:#bf5700;font:700 34px/1 "Caveat","LXGW WenKai TC",cursive; }}
    .card[data-card-id="{card_id}"] .rule {{ margin-top:34px;padding:18px 24px 24px;background:#ffe066;transform:rotate(-1deg);font-size:82px;line-height:1.08;font-weight:900;letter-spacing:-.035em; }}
    .card[data-card-id="{card_id}"] .detail {{ margin:34px 0 0;color:#5c584b;font:700 40px/1.3 "LXGW WenKai TC",sans-serif; }}
    .card[data-card-id="{card_id}"] .underline {{ width:0;height:10px;margin-top:24px;border-radius:999px;background:#2557a7; }}
  </style>
  <div class="root"><div class="kicker" id="{card_id}-kicker">✎ {kicker}</div><div class="rule" id="{card_id}-title">{text}</div><p class="detail" id="{card_id}-detail">{detail}</p><div class="underline" id="{card_id}-line"></div></div>
</div>'''
    if role == "memory":
        return f'''<div class="card" data-card-id="{card_id}">
  <style>
    .card[data-card-id="{card_id}"] .root {{ width:100%;height:100%;position:relative;overflow:hidden;padding:34px 42px;color:#fdf6e3;background:rgba(15,32,25,.94);border:4px solid #ffe066;border-radius:28px;box-shadow:0 26px 70px rgba(0,0,0,.38);font-family:"Inter","LXGW WenKai TC",sans-serif; }}
    .card[data-card-id="{card_id}"] .kicker {{ color:#ffe066;font:700 30px/1 "Caveat","LXGW WenKai TC",cursive; }}
    .card[data-card-id="{card_id}"] .title {{ margin:24px 120px 0 0;font:900 60px/1.18 "LXGW WenKai TC",sans-serif;letter-spacing:-.02em; }}
    .card[data-card-id="{card_id}"] .badge {{ position:absolute;right:34px;top:30px;width:104px;height:104px;display:grid;place-items:center;color:#171713;background:#ffe066;border-radius:50%;font:900 50px/1 "Inter",sans-serif; }}
  </style>
  <div class="root" id="{card_id}-root"><div class="kicker" id="{card_id}-kicker">{kicker}</div><h2 class="title" id="{card_id}-title">{text}</h2><div class="badge" id="{card_id}-badge">✓</div></div>
</div>'''
    return f'''<div class="card" data-card-id="{card_id}">
  <style>
    .card[data-card-id="{card_id}"] .root {{ width:100%;height:100%;position:relative;overflow:hidden;padding:62px 54px;color:#171713;background:#fdf6e3;border:5px solid #171713;border-radius:34px;font-family:"Inter","LXGW WenKai TC",sans-serif; }}
    .card[data-card-id="{card_id}"] .kicker {{ display:inline-block;padding:10px 22px;color:#fdf6e3;background:#2557a7;border-radius:999px;font:800 32px/1.1 "LXGW WenKai TC",sans-serif; }}
    .card[data-card-id="{card_id}"] .title {{ margin:44px 0 28px;font:900 74px/1.08 "LXGW WenKai TC",sans-serif; }}
    .card[data-card-id="{card_id}"] .summary {{ margin-top:42px;min-height:300px;padding:44px 38px;border:4px solid #171713;border-radius:24px;box-shadow:10px 10px 0 #171713;font:900 62px/1.25 "LXGW WenKai TC",sans-serif; }}
    .card[data-card-id="{card_id}"] .detail {{ margin:48px 0 0;color:#5c584b;font:700 38px/1.25 "LXGW WenKai TC",sans-serif;text-align:center; }}
  </style>
  <div class="root"><span class="kicker" id="{card_id}-kicker">{kicker}</span><h2 class="title" id="{card_id}-title">本段重點</h2><div class="summary" id="{card_id}-summary">{text}</div><p class="detail" id="{card_id}-detail">{detail}</p></div>
</div>'''


def _card_bounds(card: dict[str, Any]) -> tuple[int, int, int, int]:
    bounds = card.get("bounds") if isinstance(card.get("bounds"), dict) else {}
    role = str(card["role"])
    fallback = DEFAULT_CARD_BOUNDS[role]
    return tuple(int(bounds.get(key, fallback[index])) for index, key in enumerate(("x", "y", "width", "height")))


def _caption_text_fragment(caption: dict[str, Any]) -> str:
    text = str(caption.get("text") or "")
    spans = caption.get("effect_spans") if isinstance(caption.get("effect_spans"), list) else []
    fragments: list[str] = []
    cursor = 0
    for span in spans:
        start = int(span["start_char"])
        end = int(span["end_char"])
        if start < cursor or end > len(text):
            continue
        fragments.append(html.escape(text[cursor:start]))
        effect_style = span.get("style") if isinstance(span.get("style"), dict) else {}
        effect = str(effect_style.get("effect") or "pop")
        color = _safe_color(effect_style.get("color"), "#ffd447")
        scale = _bounded_number(effect_style.get("font_scale"), 1.18, 0.5, 3.0)
        pop_from = 0.72 / scale
        fragments.append(
            f'<span class="effect-word effect-{effect}" data-effect="{effect}" '
            f'style="--effect-color:{color};--effect-scale:{scale:.3f};'
            f'--effect-font-size:{scale:.3f}em;--effect-pop-from:{pop_from:.4f}">'
            f'{html.escape(text[start:end])}</span>'
        )
        cursor = end
    fragments.append(html.escape(text[cursor:]))
    return "".join(fragments)


def _caption_fragment(caption: dict[str, Any]) -> str:
    style = caption["style"]
    box_class = " has-box" if style.get("box") else ""
    animation = str(style.get("animation") or "none")
    return (
        f'<div id="{caption["id"]}-host" class="caption-host clip motion-{animation}{box_class}" '
        f'data-caption-id="{caption["id"]}" data-start="{float(caption["start"]):.4f}" '
        f'data-duration="{float(caption["end"]) - float(caption["start"]):.4f}" data-track-index="3" '
        f'style="left:{float(style["x"]):.3f}%;top:{float(style["y"]):.3f}%;max-width:{float(style["max_width"]):.3f}%;'
        f'--caption-size:{float(style["font_size"]):.3f}px;--caption-weight:{int(style["font_weight"])};'
        f'--caption-color:{style["color"]};--caption-emphasis:{style["emphasis_color"]};'
        f'--caption-stroke:{style["stroke_color"]};--caption-stroke-width:{float(style["stroke_width"]):.3f}px;'
        f'--caption-box:{style["box_color"]};--caption-font:\'{html.escape(str(style["font_family"]), quote=True)}\';'
        f'visibility:hidden;opacity:0;">'
        f'<p class="caption-line">{_caption_text_fragment(caption)}</p></div>'
    )


def _lifecycle_js(card: dict[str, Any]) -> str:
    card_id = card["id"]
    start = float(card["start"])
    end = float(card["end"])
    role = card["role"]
    lines = [f'lifecycle("{card_id}", {start:.4f}, {end:.4f});']
    if role == "hook":
        lines.extend(
            [
                f"tl.fromTo('#{card_id}-sheet', {{opacity:0,y:90}}, {{opacity:1,y:0,duration:.4667,ease:'power3.out'}}, {start + .0667:.4f});",
                f"tl.fromTo('#{card_id}-kicker', {{opacity:0,scale:.6}}, {{opacity:1,scale:1,duration:.3667,ease:'back.out(1.6)'}}, {start + .2667:.4f});",
                f"tl.fromTo('#{card_id}-title', {{opacity:0,x:-80}}, {{opacity:1,x:0,duration:.4667,ease:'power2.out'}}, {start + .2:.4f});",
                f"tl.fromTo('#{card_id}-detail', {{opacity:0}}, {{opacity:1,duration:.3667,ease:'power2.out'}}, {start + .6667:.4f});",
            ]
        )
    elif role == "rule":
        lines.extend(
            [
                f"tl.fromTo('#{card_id}-title', {{opacity:0,scale:.72}}, {{opacity:1,scale:1,duration:.4667,ease:'back.out(1.6)'}}, {start + .2:.4f});",
                f"tl.fromTo('#{card_id}-detail', {{opacity:0}}, {{opacity:1,duration:.4,ease:'power2.out'}}, {start + .75:.4f});",
                f"tl.fromTo('#{card_id}-line', {{width:0}}, {{width:850,duration:.5,ease:'power2.out'}}, {start + 1.0:.4f});",
            ]
        )
    else:
        lines.extend(
            [
                f"tl.fromTo('#{card_id}-kicker', {{opacity:0}}, {{opacity:1,duration:.3,ease:'power2.out'}}, {start + .1:.4f});",
                f"tl.fromTo('#{card_id}-title', {{clipPath:'inset(0 100% 0 0)'}}, {{clipPath:'inset(0 0 0 0)',duration:.5,ease:'power2.inOut'}}, {start + .25:.4f});",
            ]
        )
    return "\n          ".join(lines)


def _caption_lifecycle_js(caption: dict[str, Any]) -> str:
    caption_id = str(caption["id"])
    start = float(caption["start"])
    end = float(caption["end"])
    animation = str(caption.get("style", {}).get("animation") or "none")
    return f'captionLifecycle("{caption_id}", {start:.4f}, {end:.4f}, "{animation}");'


def _resolved_template_state(value: dict[str, Any] | None) -> dict[str, Any]:
    template_id = str(value.get("id") or "") if isinstance(value, dict) else ""
    if template_id not in VIDEO_TEMPLATES:
        template_id = DEFAULT_VIDEO_TEMPLATE_ID
    resolved = default_video_template_state(template_id)
    if isinstance(value, dict):
        for section in ("frame", "subject", "background"):
            if isinstance(value.get(section), dict):
                resolved[section].update(copy.deepcopy(value[section]))
    return resolved


def _template_frame_bounds(template_state: dict[str, Any]) -> tuple[int, int, int, int]:
    template = VIDEO_TEMPLATES[str(template_state["id"])]
    if template["subject_mode"] == "cutout":
        return (0, 0, 1080, 1920)
    frame = template_state["frame"]
    width = round(float(frame["width"]) * 10.8)
    height = round(float(frame["height"]) * 19.2)
    left = round(float(frame["x"]) * 10.8 - width / 2)
    top = round(float(frame["y"]) * 19.2 - height / 2)
    return (left, top, width, height)


def _template_camera_transitions(template_id: str, cards: list[dict[str, Any]]) -> str:
    if template_id == "dynamic-craft":
        hook, concept, rule, _memory, recap = cards
        return f'''
          tl.to('#video-wrap', {{x:0,y:0,scale:1,duration:.5667,ease:'power2.inOut'}}, {max(0.0, float(hook['end']) - .3):.4f});
          tl.to('#video-wrap', {{x:0,y:-315,scale:1,duration:.5667,ease:'power2.inOut'}}, {max(0.0, float(concept['start']) - .25):.4f});
          tl.to('#video-wrap', {{x:0,y:0,scale:1,duration:.5667,ease:'power2.inOut'}}, {max(0.0, float(concept['end']) - .25):.4f});
          tl.to('#video-wrap', {{x:0,y:-335,scale:1,duration:.5667,ease:'power2.inOut'}}, {max(0.0, float(rule['start']) - .25):.4f});
          tl.to('#video-wrap', {{x:0,y:0,scale:1,duration:.5667,ease:'power2.inOut'}}, {max(0.0, float(rule['end']) - .25):.4f});
          tl.to('#video-wrap', {{x:660,y:-400,scale:.34,duration:.5667,ease:'power2.inOut'}}, {max(0.0, float(recap['start']) - .3):.4f});'''
    if template_id == "dynamic-punch":
        _hook, concept, rule, memory, recap = cards
        return f'''
          tl.to('#video-wrap', {{scale:1.06,duration:.30,ease:'power2.out'}}, {max(0.0, float(concept['start'])):.4f});
          tl.to('#video-wrap', {{scale:1,duration:.34,ease:'power2.inOut'}}, {max(0.0, float(concept['end']) - .34):.4f});
          tl.to('#video-wrap', {{scale:1.06,duration:.30,ease:'power2.out'}}, {max(0.0, float(rule['start'])):.4f});
          tl.to('#video-wrap', {{scale:1,duration:.34,ease:'power2.inOut'}}, {max(0.0, float(rule['end']) - .34):.4f});
          tl.to('#video-wrap', {{scale:1.04,duration:.30,ease:'power2.out'}}, {max(0.0, float(memory['start'])):.4f});
          tl.to('#video-wrap', {{scale:1,duration:.34,ease:'power2.inOut'}}, {max(0.0, float(recap['start']) - .34):.4f});'''
    return ""


def build_composition_html(
    cards: list[dict[str, Any]],
    duration: float,
    director_style: str,
    brand_label: str = "AUTO EDIT",
    captions: list[dict[str, Any]] | None = None,
    template_state: dict[str, Any] | None = None,
) -> str:
    if len(cards) != 5:
        raise ValueError("graphic package composition requires exactly five cards")
    resolved_template = _resolved_template_state(template_state)
    template_id = str(resolved_template["id"])
    template = VIDEO_TEMPLATES[template_id]
    hosts: list[str] = []
    washes: list[str] = []
    wash_cards: list[dict[str, Any]] = []
    show_scene_washes = template_id == "dynamic-craft"
    for card in cards:
        x, y, width, height = _card_bounds(card)
        hosts.append(
            f'''<div id="{card['id']}-host" class="card-host clip" data-card-id="{card['id']}" data-start="{float(card['start']):.4f}" data-duration="{float(card['end']) - float(card['start']):.4f}" data-track-index="2" style="left:{x}px;top:{y}px;width:{width}px;height:{height}px;visibility:hidden;opacity:0;">{_card_fragment(card)}</div>'''
        )
        if card["role"] != "hook" and show_scene_washes:
            wash = {
                "concept": "#f3e6ca",
                "rule": "#b44732",
                "memory": "#9b5c24",
                "recap": "#eee0be",
            }[str(card["role"])]
            washes.append(
                f'''<div id="wash-{card['id']}" class="scene-wash clip" data-start="{float(card['start']):.4f}" data-duration="{float(card['end']) - float(card['start']):.4f}" data-track-index="0" style="background:{wash};"></div>'''
            )
            wash_cards.append(card)
    lifecycle = "\n          ".join(_lifecycle_js(card) for card in cards)
    caption_items = captions or []
    caption_hosts = "".join(_caption_fragment(caption) for caption in caption_items)
    caption_lifecycle = "\n          ".join(_caption_lifecycle_js(caption) for caption in caption_items)
    hard_washes = "\n          ".join(
        f'hardWash("wash-{card["id"]}", {float(card["start"]):.4f}, {float(card["end"]):.4f});'
        for card in wash_cards
    )
    transitions = _template_camera_transitions(template_id, cards)
    frame_left, frame_top, frame_width, frame_height = _template_frame_bounds(resolved_template)
    frame_fit = str(resolved_template["frame"].get("fit") or "cover")
    decorated = template_id in {"dynamic-craft", "fixed-stage", "fixed-stack"}
    initial_transform = "transform:translate(660px,-400px) scale(.34);" if template_id == "dynamic-craft" else ""
    wrapper_chrome = (
        '<div class="photo"><video id="bg-video" class="clip" src="input-video.mp4" muted playsinline preload="auto" '
        f'data-start="0" data-duration="{duration:.4f}" data-track-index="1"></video></div>'
        f'<div class="photo-label">{html.escape(_safe_text(brand_label, 28) or "AUTO EDIT")} · 精華片段</div><div class="washi"></div>'
        if decorated
        else '<video id="bg-video" class="clip" src="input-video.mp4" muted playsinline preload="auto" '
        f'data-start="0" data-duration="{duration:.4f}" data-track-index="1"></video>'
    )
    label = _safe_text(brand_label, 28) or "AUTO EDIT"
    desk = {
        "teacher-punch": "LEARNING LAB",
        "high-energy": "QUICK CLASS",
        "documentary": "CONTEXT DESK",
        "minimal": "POV NOTES",
        "editorial-clean": "EDIT NOTES",
    }.get(director_style, "LEARNING LAB")
    brand = f"{label.upper()} · {desk}"
    return f'''<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"/><style>
@font-face{{font-family:"Caveat";src:url("fonts/Caveat-400-latin.woff2") format("woff2");font-weight:400;font-display:block}}
@font-face{{font-family:"Caveat";src:url("fonts/Caveat-700-latin.woff2") format("woff2");font-weight:700;font-display:block}}
@font-face{{font-family:"Inter";src:url("fonts/Inter-400-latin.woff2") format("woff2");font-weight:400;font-display:block}}
@font-face{{font-family:"Inter";src:url("fonts/Inter-700-latin.woff2") format("woff2");font-weight:700 900;font-display:block}}
@font-face{{font-family:"LXGW WenKai TC";src:url("fonts/LXGWWenKaiTC-400-latin.woff2") format("woff2");font-weight:400 900;font-display:block}}
:root{{--bg:#f6efe1;--text:#2d2d2d;--accent-0:#2557a7;--accent-1:#d62728;--accent-2:#2d6a4f;--accent-3:#bf5700;--accent-4:#e9b54a}}
*{{box-sizing:border-box}}html,body{{margin:0;width:100%;height:100%;overflow:hidden;background:#101913;font-family:"Inter","Caveat","LXGW WenKai TC",sans-serif}}
#stage{{position:relative;width:1080px;height:1920px;overflow:hidden;background:radial-gradient(circle at 15% 8%,rgba(255,224,102,.12),transparent 22%),linear-gradient(180deg,#17251d 0%,#0f1712 100%)}}
#stage::before{{content:"";position:absolute;inset:0;opacity:.15;pointer-events:none;background-image:linear-gradient(rgba(255,255,255,.09) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.09) 1px,transparent 1px);background-size:54px 54px}}
.brandline{{position:absolute;left:58px;top:70px;color:#fdf6e3;z-index:0;font:800 30px/1 "Inter","LXGW WenKai TC",sans-serif;letter-spacing:.12em}}.brandline::after{{content:"";display:block;width:170px;height:7px;margin-top:18px;background:#e9b54a;border-radius:999px}}
.ghost-word{{position:absolute;right:-24px;top:174px;color:rgba(253,246,227,.38);font:900 118px/1 "Inter",sans-serif;letter-spacing:-.05em;transform:rotate(90deg);transform-origin:right top}}
.scene-wash{{position:absolute;inset:0;z-index:0;visibility:hidden}}.video-wrapper{{position:absolute;left:{frame_left}px;top:{frame_top}px;width:{frame_width}px;height:{frame_height}px;overflow:hidden;z-index:1;background:#000;{initial_transform}transform-origin:center center;box-shadow:0 30px 70px rgba(0,0,0,.36)}}
.video-wrapper .photo{{position:absolute;left:18px;right:18px;top:18px;bottom:70px;overflow:hidden;background:#000}}.video-wrapper>video,.video-wrapper .photo video{{width:100%;height:100%;object-fit:{frame_fit}}}.video-wrapper .photo-label{{position:absolute;left:0;right:0;bottom:18px;text-align:center;color:#25231f;font:700 28px/1 "Caveat","LXGW WenKai TC",cursive}}.video-wrapper .washi{{position:absolute;top:-10px;left:18%;width:34%;height:32px;background:rgba(183,211,231,.9);box-shadow:0 4px 8px rgba(0,0,0,.18)}}
.card-host{{position:absolute;overflow:hidden;pointer-events:none;z-index:2}}.card-host .card{{position:relative;width:100%;height:100%;overflow:hidden}}
.caption-host{{position:absolute;z-index:4;width:max-content;transform:translate(-50%,-50%);pointer-events:none;text-align:center;font-family:var(--caption-font),"LXGW WenKai TC",sans-serif}}
.caption-line{{display:inline;margin:0;color:var(--caption-color);font-size:var(--caption-size);font-weight:var(--caption-weight);line-height:1.14;letter-spacing:-.02em;white-space:pre-wrap;overflow-wrap:anywhere;-webkit-text-stroke:var(--caption-stroke-width) var(--caption-stroke);paint-order:stroke fill;filter:drop-shadow(0 6px 10px rgba(0,0,0,.42))}}
.caption-host.has-box .caption-line{{padding:.24em .42em;background:color-mix(in srgb,var(--caption-box) 88%,transparent);border-radius:.22em;box-decoration-break:clone;-webkit-box-decoration-break:clone}}
.effect-word{{position:relative;display:inline-block;color:var(--effect-color,var(--caption-emphasis));font-size:var(--effect-font-size,1em);transform-origin:center bottom}}
.effect-highlight{{z-index:0;padding:0 .06em;color:#171713;-webkit-text-stroke:0}}
.effect-highlight::before{{content:"";position:absolute;z-index:-1;left:-.05em;right:-.05em;top:.48em;bottom:-.02em;background:var(--effect-color);transform:rotate(-1.5deg)}}
.effect-underline::after{{content:"";position:absolute;left:0;right:0;bottom:-.08em;height:.10em;border-radius:999px;background:var(--effect-color)}}
</style></head><body><div id="stage" data-composition-id="talking-head-recut" data-template-id="{template_id}" data-camera-motion="{template['camera_motion']}" data-subject-mode="{template['subject_mode']}" data-start="0" data-duration="{duration:.4f}" data-fps="30" data-width="1080" data-height="1920"><div class="brandline">{html.escape(brand)}</div><div class="ghost-word">LESSON</div>{''.join(washes)}<div class="video-wrapper" id="video-wrap">{wrapper_chrome}</div>{''.join(hosts)}{caption_hosts}<script src="vendor/gsap.min.js"></script><script>(function(){{const tl=window.gsap.timeline({{paused:true}});function lifecycle(id,start,end){{const sel='.card-host[data-card-id="'+id+'"]';tl.set(sel,{{visibility:'visible'}},start);tl.fromTo(sel,{{opacity:0}},{{opacity:1,duration:.2667,ease:'power2.out'}},start);tl.to(sel,{{opacity:0,duration:.2667,ease:'power2.in'}},Math.max(start,end-.2667));tl.set(sel,{{visibility:'hidden'}},end)}}function hardWash(id,start,end){{const sel='#'+id;tl.set(sel,{{visibility:'visible',opacity:1}},start);tl.set(sel,{{visibility:'hidden'}},end)}}function captionLifecycle(id,start,end,motion){{const sel='#'+id+'-host';const from=motion==='slide-up'?{{opacity:0,y:34}}:motion==='pop'?{{opacity:0,scale:.86}}:{{opacity:0}};tl.set(sel,{{visibility:'visible'}},start);tl.fromTo(sel,from,{{opacity:1,y:0,scale:1,duration:.2667,ease:'power3.out'}},start);tl.fromTo(sel+' .effect-pop',{{opacity:.65,scale:function(i,target){{return Number(getComputedStyle(target).getPropertyValue('--effect-pop-from'))||.61}}}},{{opacity:1,scale:1,duration:.2667,ease:'power3.out',stagger:.04}},start+.10);tl.to(sel,{{opacity:0,duration:.20,ease:'power2.in'}},Math.max(start,end-.20));tl.set(sel,{{visibility:'hidden'}},end)}}{lifecycle}{caption_lifecycle}{hard_washes}{transitions}window.__timelines=window.__timelines||{{}};window.__timelines["talking-head-recut"]=tl;}})();</script></div></body></html>'''


def _source_brand(manifest: dict[str, Any]) -> str:
    source = manifest.get("source", {}) if isinstance(manifest.get("source"), dict) else {}
    original_name = str(source.get("original_name") or "").strip()
    if not original_name:
        return "AUTO EDIT"
    label = Path(original_name).stem
    label = re.sub(r"[_-]?\[[^\]]+\]$", "", label)
    label = re.sub(r"[_-]+", " ", label).strip()
    if not label or label.lower() in {"original", "source", "video"}:
        return "AUTO EDIT"
    return _safe_text(label, 28)


def _talking_head_skill_dir() -> Path:
    candidates = [
        Path(os.environ.get("TALKING_HEAD_RECUT_SKILL_DIR", "")),
        Path.home() / ".codex/skills/talking-head-recut",
        Path.home() / ".agents/skills/talking-head-recut",
        Path.home() / ".claude/skills/talking-head-recut",
    ]
    for candidate in candidates:
        if str(candidate) and (candidate / "assets/vendor/gsap.min.js").is_file():
            return candidate.resolve()
    raise ValueError("talking-head-recut assets are required for designed graphic packages")


def _hyperframes_command() -> list[str]:
    configured = os.environ.get("HYPERFRAMES_BIN")
    if configured:
        executable = Path(configured).expanduser()
        if executable.is_file():
            return [str(executable)]
    installed = shutil.which("hyperframes")
    if installed:
        return [installed]
    node = shutil.which("node")
    cached = sorted(
        (Path.home() / ".npm/_npx").glob("*/node_modules/hyperframes/dist/cli.js"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if node and cached:
        return [node, str(cached[0])]
    raise ValueError("an installed HyperFrames CLI is required for designed graphic packages")


def _has_video(path: Path) -> bool:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe or not path.is_file():
        return False
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
            text=True,
            capture_output=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0 and result.stdout.strip() == "video"


def _signature(
    state: dict[str, Any],
    manifest: dict[str, Any],
    clip: dict[str, Any],
) -> str:
    payload = {
        "template": TEMPLATE_VERSION,
        "source_sha256": manifest.get("source", {}).get("sha256"),
        "source_size": manifest.get("source", {}).get("size_bytes"),
        "brand": _source_brand(manifest),
        "clip": clip,
        "director_style": state.get("director_style"),
        "video_template": _resolved_template_state(
            state.get("video_template") if isinstance(state.get("video_template"), dict) else None
        ),
        "asset_digests": state.get("asset_digests"),
        "cards": package_cards(state, clip),
        "captions": package_captions(state, clip),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _owned_background_asset(root: Path, template_state: dict[str, Any]) -> Path | None:
    source = template_state.get("background", {}).get("source")
    if not source:
        return None
    relative = Path(str(source))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("template background must be a project-owned asset")
    entry = root / relative
    if entry.is_symlink():
        raise ValueError("template background must not be a symlink")
    asset = entry.resolve()
    assets_root = (root / "assets").resolve()
    if assets_root not in asset.parents or not asset.is_file():
        raise ValueError("template background is missing or outside assets/")
    return asset


def _stage_input_video(
    *,
    root: Path,
    source: Path,
    output: Path,
    clip_start: float,
    duration: float,
    template_state: dict[str, Any],
    ffmpeg: str,
) -> str:
    template = VIDEO_TEMPLATES[str(template_state["id"])]
    if template["subject_mode"] != "cutout":
        command = [
            ffmpeg,
            "-y",
            "-ss",
            f"{clip_start:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(source),
            "-an",
            "-c:v",
            "libx264",
            "-crf",
            "18",
            "-g",
            "30",
            "-keyint_min",
            "30",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output),
        ]
        timeout = 30 * 60
        timeout_message = "graphic source staging timed out"
        engine = "source"
    else:
        readiness = template_readiness_errors(template_state)
        if readiness:
            raise ValueError("; ".join(readiness))
        capability = cutout_capability()
        if not capability.get("available"):
            raise ValueError(str(capability.get("reason") or "local subject cutout is unavailable"))
        background = template_state["background"]
        subject = template_state["subject"]
        background_asset = _owned_background_asset(root, template_state)
        try:
            working_size = int(os.environ.get("AUTO_EDIT_CUTOUT_WORKING_SIZE", "640"))
        except ValueError:
            working_size = 640
        command = [
            str(capability["python"]),
            str(Path(__file__).with_name("subject_compositor.py")),
            "--source",
            str(source),
            "--output",
            str(output),
            "--start",
            f"{clip_start:.4f}",
            "--duration",
            f"{duration:.4f}",
            "--fps",
            "30",
            "--width",
            "1080",
            "--height",
            "1920",
            "--working-size",
            str(max(320, min(960, working_size))),
            "--model-home",
            str(Path(str(capability["model_path"])).parent),
            "--background-mode",
            str(template["background_mode"]),
            "--background-color",
            str(background["color"]),
            "--background-fit",
            str(background["fit"]),
            "--background-blur",
            str(background["blur"]),
            "--subject-x",
            str(subject["x"]),
            "--subject-y",
            str(subject["y"]),
            "--subject-scale",
            str(subject["scale"]),
            "--feather",
            str(subject["feather"]),
            "--mask-stride",
            str(subject["mask_stride"]),
        ]
        if background_asset is not None:
            command.extend(["--background-source", str(background_asset)])
        timeout = 4 * 60 * 60
        timeout_message = "local subject cutout timed out"
        engine = "rembg-local"
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(timeout_message) from exc
    if result.returncode != 0 or not _has_video(output):
        detail = result.stderr or result.stdout or "graphic source staging failed"
        raise RuntimeError(detail[-5000:])
    return engine


def ensure_graphic_package(
    project_dir: Path,
    state: dict[str, Any],
    manifest: dict[str, Any],
    clip: dict[str, Any],
) -> Path:
    """Render or reuse the deterministic project-owned package for one clip."""
    root = project_dir.resolve()
    signature = _signature(state, manifest, clip)
    packages_root = root / "working/graphic_packages"
    package_dir = packages_root / signature[:20]
    output = package_dir / "output.mp4"
    receipt = package_dir / "package.json"
    if _has_video(output) and receipt.is_file():
        try:
            payload = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        actual = hashlib.sha256(output.read_bytes()).hexdigest()
        if payload.get("signature") == signature and payload.get("output_sha256") == actual:
            return output

    source_rel = str(manifest.get("source", {}).get("staged_path") or "")
    source = (root / source_rel).resolve()
    if root not in source.parents or not source.is_file():
        raise ValueError("graphic package source is missing or outside the project")
    clip_start = float(clip.get("start", 0.0))
    clip_end = float(clip.get("end", clip_start))
    duration = clip_end - clip_start
    if duration <= 0:
        raise ValueError("graphic package clip duration must be positive")

    skill_dir = _talking_head_skill_dir()
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("ffmpeg is required for designed graphic packages")
    packages_root.mkdir(parents=True, exist_ok=True)
    temporary = packages_root / f".{signature[:20]}.building-{uuid.uuid4().hex}"
    public = temporary / "public"
    cards_dir = public / "cards"
    fonts_dir = public / "fonts"
    vendor_dir = public / "vendor"
    cards_dir.mkdir(parents=True, exist_ok=True)
    fonts_dir.mkdir(parents=True, exist_ok=True)
    vendor_dir.mkdir(parents=True, exist_ok=True)
    cards = package_cards(state, clip)
    captions = package_captions(state, clip)
    template_state = _resolved_template_state(
        state.get("video_template") if isinstance(state.get("video_template"), dict) else None
    )
    try:
        for font in (skill_dir / "assets/fonts").glob("*.woff2"):
            shutil.copy2(font, fonts_dir / font.name)
        shutil.copy2(skill_dir / "assets/vendor/gsap.min.js", vendor_dir / "gsap.min.js")
        source_engine = _stage_input_video(
            root=root,
            source=source,
            output=public / "input-video.mp4",
            clip_start=clip_start,
            duration=duration,
            template_state=template_state,
            ffmpeg=ffmpeg,
        )
        for card in cards:
            (cards_dir / f"{card['id']}.html").write_text(_card_fragment(card) + "\n", encoding="utf-8")
        frame_left, frame_top, frame_width, frame_height = _template_frame_bounds(template_state)
        storyboard = {
            "schemaVersion": 3,
            "composition": {
                "fps": 30,
                "width": 1080,
                "height": 1920,
                "durationSeconds": round(duration, 4),
                "layout": "portrait",
                "themeId": "craft",
                "seed": 42,
            },
            "videoTrack": {
                "sourcePath": "input-video.mp4",
                "startSec": 0,
                "endSec": round(duration, 4),
                "bounds": {
                    "x": frame_left,
                    "y": frame_top,
                    "width": frame_width,
                    "height": frame_height,
                },
                "templateId": template_state["id"],
            },
            "subtitles": {"enabled": bool(captions), "items": captions},
            "cards": cards,
        }
        (temporary / "storyboard.json").write_text(
            json.dumps(storyboard, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (public / "index.html").write_text(
            build_composition_html(
                cards,
                duration,
                str(state.get("director_style") or "teacher-punch"),
                _source_brand(manifest),
                captions,
                template_state,
            ),
            encoding="utf-8",
        )
        try:
            render_result = subprocess.run(
                _hyperframes_command()
                + ["render", "public", "--skill=talking-head-recut", "-o", "output.mp4", "--fps", "30"],
                cwd=temporary,
                env={**os.environ, "PRODUCER_BROWSER_GPU_MODE": "hardware"},
                text=True,
                capture_output=True,
                timeout=2 * 60 * 60,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("graphic package render timed out") from exc
        built_output = temporary / "output.mp4"
        if render_result.returncode != 0 or not _has_video(built_output):
            raise RuntimeError((render_result.stderr or render_result.stdout or "graphic package render failed")[-5000:])
        output_sha = hashlib.sha256(built_output.read_bytes()).hexdigest()
        (temporary / "package.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "template_version": TEMPLATE_VERSION,
                    "template_id": template_state["id"],
                    "source_engine": source_engine,
                    "signature": signature,
                    "clip_id": clip.get("id"),
                    "card_count": len(cards),
                    "caption_count": len(captions),
                    "output": "output.mp4",
                    "output_sha256": output_sha,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        if package_dir.exists():
            shutil.rmtree(package_dir)
        os.replace(temporary, package_dir)
        return package_dir / "output.mp4"
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
