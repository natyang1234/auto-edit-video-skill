#!/usr/bin/env python3
"""Static structured-card compositor — Phase 1c ``static_fallback`` capability.

Renders title / stat / chart(bar,line) / dynamic_list / mosaic layers into tight RGBA
PNGs with CoreText + CoreGraphics, driven by the style-pack tokens. Every
render lands in the external receipt index ``structured_layer_artifacts.json``
(Phase 0 contract) keyed by layer/pack/mode/canvas identities, so a change to
any input makes downstream artifacts verifiably stale.

Chart contract (plan v2): kinds are bar|line only; datum order = input
order; bars grow from a zero baseline in both directions; labels shrink to a
minimum before the render FAILS CLOSED rather than truncating silently.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import caption_compositor
import contract_registry
from caption_compositor import _cg_color, _file_sha256, _load_coretext

CARDS_REL = Path("working/structured_cards")
ARTIFACTS_REL = Path("working/structured_layer_artifacts.json")
# v2: type sizes raised to caption scale — a card smaller than the
# captions beside it reads as fine print (nat, watching a delivery).
# v3: the title refuses to shrink below its floor; a fallback title
# holding a whole transcript sentence is trimmed instead.
COMPILER_VERSION = "static-card-compositor-v7"
MIN_LABEL_PT = 11.0
# Cards are watched at phone size beside 52--68px captions.  The old list
# ramp started at 32 design pixels and could shrink to 11, which made a 540p
# preview render body copy at just 16 (or fewer) output pixels.  Keep primary
# card copy near caption scale and refuse a shrink into fine print.
LIST_BODY_PT = 44.0
MIN_LIST_BODY_PT = 36.0
LIST_ROW_PT = 62.0
TITLE_PT = 58.0
STAT_VALUE_PT = 64.0
STAT_LABEL_PT = 34.0
MIN_STAT_LABEL_PT = 32.0
METADATA_PT = 26.0
MIN_METADATA_PT = 22.0
CHART_LABEL_PT = 32.0
MIN_CHART_LABEL_PT = 32.0
CHART_LABEL_ZONE_PT = 46.0
KICKER_PT = 24.0
SUBTITLE_PT = 32.0
PRIMARY_FONT_FLOORS_PT = {
    "title": 42.0,
    "stat": 32.0,
    "chart": 32.0,
    "dynamic_list": 36.0,
    "quote": 32.0,
    "question": 36.0,
    "comparison": 32.0,
    "term": 32.0,
    "note": 32.0,
    "chip": 32.0,
    "statement": 32.0,
    "mosaic": 32.0,
}


def primary_font_floor(layer_type: str) -> float:
    """Minimum output-space primary copy at render scale 1."""
    try:
        return PRIMARY_FONT_FLOORS_PT[layer_type]
    except KeyError as exc:
        raise ValueError(f"unsupported structured layer type: {layer_type}") from exc


def compositor_available() -> bool:
    return caption_compositor.compositor_available()


def capability_status() -> dict[str, str]:
    ok = compositor_available()
    return {
        "name": "structured-card-static",
        "version": COMPILER_VERSION if ok else "",
        "status": "static_fallback" if ok else "not_configured",
    }


def _pack_token(pack: dict[str, Any], group: str, key: str, fallback: str) -> str:
    tokens = pack.get("tokens", {}).get(group, {})
    value = tokens.get(key)
    return str(value) if value else fallback


def _pack_number(
    pack: dict[str, Any], group: str, key: str, fallback: float,
    minimum: float, maximum: float,
) -> float:
    value = pack.get("tokens", {}).get(group, {}).get(key, fallback)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    number = float(value)
    if not math.isfinite(number):
        return fallback
    return max(minimum, min(maximum, number))


def surface_style(pack: dict[str, Any], render_scale: float) -> dict[str, Any]:
    """Shared card-surface geometry for the static and animated renderers."""
    return {
        "radius": _pack_number(pack, "spacing", "card_radius", 14, 0, 80)
        * render_scale,
        "border_width": _pack_number(pack, "spacing", "border_width", 0, 0, 12)
        * render_scale,
        "panel_alpha": _pack_number(pack, "spacing", "panel_alpha", 0.92, 0, 1),
        "border": _pack_token(pack, "palette", "border", "#000000"),
    }


def _make_font(ct, size: float):
    from render_editor_timeline import font_path

    return caption_compositor._make_base_font(ct, size, font_path())


def _attributed(foundation, ct, quartz, text: str, size: float, color: str):
    attributed = foundation.NSMutableAttributedString.alloc().initWithString_(text)
    attributed.addAttributes_range_(
        {
            ct.kCTFontAttributeName: _make_font(ct, size),
            ct.kCTForegroundColorAttributeName: _cg_color(quartz, color),
        },
        foundation.NSMakeRange(0, attributed.length()),
    )
    return attributed


def _line_width(ct, attributed) -> float:
    line = ct.CTLineCreateWithAttributedString(attributed)
    width, _a, _d, _l = ct.CTLineGetTypographicBounds(line, None, None, None)
    return float(width)


def _fit_text(
    foundation, ct, quartz, text, size, color, max_width, render_scale,
    minimum_pt: float = MIN_LABEL_PT,
):
    """Shrink to fit; below the minimum point size the card fails closed."""
    current = size
    while current >= minimum_pt * render_scale:
        attributed = _attributed(foundation, ct, quartz, text, current, color)
        if _line_width(ct, attributed) <= max_width:
            return attributed, current
        current *= 0.9
    raise ValueError(
        f"label does not fit at the minimum size: {text!r} "
        "(shorten the copy or widen the layer)"
    )


def _fit_text_lines(
    foundation, ct, quartz, text, size, color, max_width, render_scale,
    minimum_pt: float, max_lines: int,
):
    """Keep readable type and wrap before considering a smaller size."""
    current = size
    while current >= minimum_pt * render_scale:
        def measure(candidate: str) -> float:
            return _line_width(
                ct, _attributed(foundation, ct, quartz, candidate, current, color)
            )

        lines = caption_compositor.wrap_lines(str(text), measure, max_width)
        if lines and len(lines) <= max_lines:
            return [
                _attributed(foundation, ct, quartz, line, current, color)
                for line in lines
            ], current
        current *= 0.9
    raise ValueError(
        f"label does not fit in {max_lines} readable lines: {text!r} "
        "(shorten the copy or widen the layer)"
    )


def _draw_line(ct, quartz, context, attributed, x: float, y: float) -> None:
    quartz.CGContextSetTextPosition(context, x, y)
    ct.CTLineDraw(ct.CTLineCreateWithAttributedString(attributed), context)


def _begin_card(
    quartz, width: int, height: int, panel_color: str, *,
    radius: float = 14, panel_alpha: float = 0.92,
    border_color: str = "#000000", border_width: float = 0,
):
    color_space = quartz.CGColorSpaceCreateDeviceRGB()
    context = quartz.CGBitmapContextCreate(
        None, width, height, 8, width * 4, color_space,
        quartz.kCGImageAlphaPremultipliedLast,
    )
    if context is None:
        raise RuntimeError("could not create card bitmap context")
    quartz.CGContextSetFillColorWithColor(
        context, _cg_color(quartz, panel_color, panel_alpha)
    )
    path = quartz.CGPathCreateWithRoundedRect(
        quartz.CGRectMake(0, 0, width, height), radius, radius, None
    )
    quartz.CGContextAddPath(context, path)
    quartz.CGContextFillPath(context)
    if border_width > 0:
        quartz.CGContextSetStrokeColorWithColor(
            context, _cg_color(quartz, border_color)
        )
        quartz.CGContextSetLineWidth(context, border_width)
        quartz.CGContextAddPath(context, path)
        quartz.CGContextStrokePath(context)
    return context


def _finish_card(foundation, quartz, context, project_dir: Path, layer_id: str) -> tuple[str, str, int, int]:
    image = quartz.CGBitmapContextCreateImage(context)
    cards_dir = project_dir / CARDS_REL
    cards_dir.mkdir(parents=True, exist_ok=True)
    scratch = cards_dir / f".rendering-{layer_id}.png"
    destination = quartz.CGImageDestinationCreateWithURL(
        foundation.NSURL.fileURLWithPath_(str(scratch)), "public.png", 1, None
    )
    quartz.CGImageDestinationAddImage(destination, image, None)
    if not quartz.CGImageDestinationFinalize(destination):
        raise RuntimeError("card PNG finalize failed")
    digest = _file_sha256(scratch)
    final_path = cards_dir / f"{layer_id}-{digest[:16]}.png"
    scratch.replace(final_path)
    return (
        final_path.relative_to(project_dir).as_posix(),
        digest,
        int(quartz.CGBitmapContextGetWidth(context)),
        int(quartz.CGBitmapContextGetHeight(context)),
    )


# What data an item carries decides its type; which component renders it
# decides how it looks. A component only fits the types whose payload it can
# actually draw.
COMPONENTS_BY_TYPE = {
    "title": ("prompt_card", "kinetic_title", "title_lockup"),
    "stat": ("hero_stat", "progress"),
    "chart": ("dashboard",),
    "dynamic_list": ("dynamic_list", "warning_checklist", "carousel_grid", "calendar_reveal"),
    "note": ("note_card",),
    "chip": ("chip",),
    "statement": ("statement_card",),
    "quote": ("pull_quote",),
    "question": ("question_card",),
    "comparison": ("versus",),
    "term": ("definition",),
    "mosaic": ("asset_mosaic",),
}
DEFAULT_COMPONENT = {
    "title": "prompt_card",
    "stat": "hero_stat",
    "chart": "dashboard",
    "dynamic_list": "dynamic_list",
    "note": "note_card",
    "chip": "chip",
    "statement": "statement_card",
    "quote": "pull_quote",
    "question": "question_card",
    "comparison": "versus",
    "term": "definition",
    "mosaic": "asset_mosaic",
}


def _decode_snapshot_image(foundation, quartz, snapshot: dict[str, Any]):
    payload = snapshot.get("bytes") if isinstance(snapshot, dict) else None
    image_format = snapshot.get("format") if isinstance(snapshot, dict) else None
    if not isinstance(payload, bytes) or not payload or image_format not in {"png", "jpeg"}:
        raise ValueError("approved image resolver returned an invalid snapshot")
    data = foundation.NSData.dataWithBytes_length_(payload, len(payload))
    source = quartz.CGImageSourceCreateWithData(data, None)
    if source is None or quartz.CGImageSourceGetCount(source) != 1:
        raise ValueError("approved still image could not be decoded exactly once")
    image = quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
    if image is None:
        raise ValueError("approved still image decode failed")
    return image


def _draw_aspect_fill_image(quartz, context, image, x, y, width, height, radius):
    image_width = float(quartz.CGImageGetWidth(image))
    image_height = float(quartz.CGImageGetHeight(image))
    if image_width <= 0 or image_height <= 0:
        raise ValueError("approved still image dimensions are invalid")
    scale = max(width / image_width, height / image_height)
    drawn_width = image_width * scale
    drawn_height = image_height * scale
    drawn_x = x + (width - drawn_width) / 2
    drawn_y = y + (height - drawn_height) / 2
    quartz.CGContextSaveGState(context)
    clip = quartz.CGPathCreateWithRoundedRect(
        quartz.CGRectMake(x, y, width, height), radius, radius, None
    )
    quartz.CGContextAddPath(context, clip)
    quartz.CGContextClip(context)
    quartz.CGContextDrawImage(
        context,
        quartz.CGRectMake(drawn_x, drawn_y, drawn_width, drawn_height),
        image,
    )
    quartz.CGContextRestoreGState(context)


def resolve_component(pack: dict[str, Any], layer_type: str, component_id: str | None) -> dict[str, Any]:
    """The pack component that renders this item, or an error if it cannot."""
    components = {str(item.get("id")): item for item in pack.get("components", [])}
    allowed = COMPONENTS_BY_TYPE.get(layer_type, ())
    if not component_id:
        wanted = DEFAULT_COMPONENT.get(layer_type)
        chosen = next(
            (item for item in components.values() if item.get("kind") == wanted), None
        )
        if chosen is not None:
            return chosen
        if components:
            raise ValueError(f"style pack has no default component for {layer_type!r}")
        # A pack that declares no components at all predates them (the "none"
        # selection passes one). Render as before rather than refusing.
        return {"id": f"builtin-{wanted}", "kind": wanted, "layout": "left-stack"}
    chosen = components.get(component_id)
    if chosen is None:
        raise ValueError(f"style pack has no component {component_id!r}")
    if chosen.get("kind") not in allowed:
        raise ValueError(
            f"component {component_id!r} ({chosen.get('kind')}) cannot render a "
            f"{layer_type!r} item"
        )
    return chosen


def _layout_origin(layout: str, card_width: float, text_width: float, pad: float) -> float:
    """Where a line starts, from the component's declared layout."""
    if layout in {"center", "full-bleed"}:
        return max(pad, (card_width - text_width) / 2)
    return pad


def render_card(
    project_dir: Path,
    layer: dict[str, Any],
    pack: dict[str, Any],
    canvas: dict[str, Any],
    render_scale: float,
) -> tuple[str, str, int, int]:
    modules = _load_coretext()
    if not modules:
        raise RuntimeError("structured card compositor is not available")
    ct, foundation, quartz = modules
    payload = layer.get("payload") or {}
    layer_type = layer.get("type")
    component = resolve_component(pack, str(layer_type), layer.get("component_id"))
    layout = str(component.get("layout") or "left-stack")
    scale = render_scale
    canvas_width = int(canvas.get("width", 1080)) * scale
    canvas_height = int(canvas.get("height", 1920)) * scale
    card_width = int(canvas_width * 0.84)
    pad = int(28 * scale)
    ink = _pack_token(pack, "palette", "ink", "#E6EDF3")
    accent = _pack_token(pack, "palette", "accent", "#E5484D")
    panel = _pack_token(pack, "palette", "panel", "#161B22")
    muted = "#9AA4AF"
    card_surface = surface_style(pack, scale)

    def begin_card(width: int, height: int, color: str):
        return _begin_card(
            quartz, width, height, color,
            radius=card_surface["radius"],
            panel_alpha=card_surface["panel_alpha"],
            border_color=card_surface["border"],
            border_width=card_surface["border_width"],
        )

    if layer_type == "title":
        # Cards share the screen with 52-68px captions; a title smaller
        # than a caption reads as fine print. nat, watching a delivery:
        # 「圖卡字太小了，要大一點才能看得見」.
        #
        # And shrinking must not undo that. When the editorial model is
        # unavailable the fallback title is a transcript sentence, and
        # fitting all of it shrank the card right back to fine print — the
        # size ruling broken on the fallback path. Below the floor the words
        # are cut instead: a nameplate is a label, not the transcript.
        title_text = str(payload.get("title") or "")
        title, title_size = _fit_text(
            foundation, ct, quartz, title_text,
            TITLE_PT * scale, ink, card_width - pad * 2, scale,
        )
        floor = 42 * scale
        while title_size < floor and len(title_text) > 4:
            import text_joining

            title_text = text_joining.trim_to_width(
                title_text, text_joining.display_width(title_text) - 2
            )
            title, title_size = _fit_text(
                foundation, ct, quartz, title_text,
                TITLE_PT * scale, ink, card_width - pad * 2, scale,
            )
        kicker_text = str(payload.get("kicker") or "")
        subtitle_text = str(payload.get("subtitle") or "")
        kicker = None
        kicker_size = 0.0
        if kicker_text:
            kicker, kicker_size = _fit_text(
                foundation, ct, quartz, kicker_text, KICKER_PT * scale,
                accent, card_width - pad * 2, scale, MIN_METADATA_PT,
            )
        subtitle = None
        subtitle_size = 0.0
        if subtitle_text:
            subtitle, subtitle_size = _fit_text(
                foundation, ct, quartz, subtitle_text, SUBTITLE_PT * scale,
                muted, card_width - pad * 2, scale, MIN_STAT_LABEL_PT,
            )
        # A hook fills the screen and is read as a statement; a lower third is
        # a band along the bottom. The payload has said which since the
        # director started emitting these, and only the pack's default was
        # being consulted — so a hook was drawn as a band with its text pushed
        # into the left corner of a slab sized for a sentence it did not have.
        if str(payload.get("title_kind") or "") == "full-screen-hook":
            layout = "center"
            if not kicker_text and not subtitle_text:
                card_width = int(
                    min(card_width, _line_width(ct, title) + pad * 2)
                )
        height = int(
            pad * 2 + title_size * 1.4
            + (kicker_size * 1.25 + 8 * scale if kicker is not None else 0)
            + (subtitle_size * 1.35 + 8 * scale if subtitle is not None else 0)
        )
        context = begin_card(card_width, height, panel)
        cursor = height - pad
        if kicker is not None:
            cursor -= kicker_size
            _draw_line(ct, quartz, context, kicker, pad, cursor)
            cursor -= 8 * scale
        cursor -= title_size
        _draw_line(
            ct, quartz, context, title,
            _layout_origin(layout, card_width, _line_width(ct, title), pad), cursor,
        )
        if component.get("kind") == "title_lockup":
            # A lower third reads as a band, so it carries a rule under the text.
            quartz.CGContextSetFillColorWithColor(context, _cg_color(quartz, accent))
            quartz.CGContextFillRect(
                context, quartz.CGRectMake(pad, cursor - 10 * scale, card_width - pad * 2, 2 * scale)
            )
        if subtitle is not None:
            cursor -= subtitle_size * 1.35
            _draw_line(ct, quartz, context, subtitle, pad, cursor)
    elif layer_type == "stat":
        value_text = str(payload.get("value"))
        value, value_size = _fit_text(
            foundation, ct, quartz, value_text,
            STAT_VALUE_PT * scale, accent, card_width - pad * 2, scale,
            primary_font_floor("stat"),
        )
        label, label_size = _fit_text(
            foundation, ct, quartz, str(payload.get("label") or ""),
            STAT_LABEL_PT * scale, ink, card_width - pad * 2, scale,
            MIN_STAT_LABEL_PT,
        )
        source_text = str(payload.get("source_literal") or "")
        source = None
        source_size = 0.0
        if source_text:
            source, source_size = _fit_text(
                foundation, ct, quartz, f"「{source_text}」",
                METADATA_PT * scale, muted, card_width - pad * 2, scale,
                MIN_METADATA_PT,
            )
        height = int(
            pad * 2 + value_size * 1.2 + label_size * 1.35
            + (source_size * 1.35 if source is not None else 0)
        )
        context = begin_card(card_width, height, panel)
        cursor = height - pad - value_size
        _draw_line(
            ct, quartz, context, value,
            _layout_origin(layout, card_width, _line_width(ct, value), pad), cursor,
        )
        if component.get("kind") == "progress":
            ratio = payload.get("ratio")
            if not isinstance(ratio, (int, float)) or isinstance(ratio, bool):
                raise ValueError("progress component requires a numeric payload ratio")
            filled = max(0.0, min(1.0, float(ratio)))
            track = card_width - pad * 2
            quartz.CGContextSetFillColorWithColor(context, _cg_color(quartz, muted))
            quartz.CGContextFillRect(context, quartz.CGRectMake(pad, pad, track, 6 * scale))
            quartz.CGContextSetFillColorWithColor(context, _cg_color(quartz, accent))
            quartz.CGContextFillRect(
                context, quartz.CGRectMake(pad, pad, track * filled, 6 * scale)
            )
        cursor -= label_size * 1.25
        _draw_line(ct, quartz, context, label, pad, cursor)
        if source is not None:
            cursor -= source_size * 1.35
            _draw_line(ct, quartz, context, source, pad, cursor)
    elif layer_type == "mosaic":
        import asset_registry

        title_text = str(payload.get("title") or "").strip()
        assets = payload.get("assets")
        if not title_text or not isinstance(assets, list) or not 2 <= len(assets) <= 4:
            raise ValueError("mosaic requires a title and two to four frozen assets")
        title, title_size = _fit_text(
            foundation,
            ct,
            quartz,
            title_text,
            36 * scale,
            ink,
            card_width - pad * 2,
            scale,
            primary_font_floor("mosaic"),
        )
        gap = 12 * scale
        rows = math.ceil(len(assets) / 2)
        cell_width = (card_width - pad * 2 - gap) / 2
        # Split-stage scenes declare the upper 10%--40% as their graphic ROI.
        # Keep the complete card inside that same 30% band at every supported
        # canvas size instead of letting fixed-height cells escape above it.
        fixed_height = (
            pad * 2 + title_size * 1.35 + 14 * scale + (rows - 1) * gap
        )
        available_cell_height = (canvas_height * 0.30 - fixed_height) / rows
        cell_height = min(220 * scale, available_cell_height)
        if cell_height < 48 * scale:
            raise ValueError("mosaic cells cannot fit the declared graphic stage")
        grid_height = rows * cell_height + (rows - 1) * gap
        height = int(pad * 2 + title_size * 1.35 + 14 * scale + grid_height)
        context = begin_card(card_width, height, panel)
        title_y = height - pad - title_size
        _draw_line(ct, quartz, context, title, pad, title_y)
        grid_top = title_y - 14 * scale
        for index, descriptor in enumerate(assets):
            if not isinstance(descriptor, dict):
                raise ValueError("mosaic asset descriptor must be an object")
            snapshot = asset_registry.resolve_approved_image_snapshot(
                project_dir, descriptor
            )
            image = _decode_snapshot_image(foundation, quartz, snapshot)
            row, column = divmod(index, 2)
            x = pad + column * (cell_width + gap)
            y = grid_top - (row + 1) * cell_height - row * gap
            _draw_aspect_fill_image(
                quartz,
                context,
                image,
                x,
                y,
                cell_width,
                cell_height,
                10 * scale,
            )
    elif layer_type == "quote":
        # A pulled quote is the line, set large, with the marks that say it is
        # somebody's words. Sized to the text so a short line does not sit in
        # a slab built for a sentence it does not have.
        text = f"「{str(payload.get('quote') or '').strip()}」"
        body, body_size = _fit_text(
            foundation, ct, quartz, text, 38 * scale, ink,
            card_width - pad * 2, scale, primary_font_floor("quote"),
        )
        card_width = int(min(card_width, _line_width(ct, body) + pad * 2 + 6 * scale))
        rule = int(4 * scale)
        height = int(pad * 2 + body_size * 1.4)
        context = begin_card(card_width, height, panel)
        # A bar down the leading edge, the way a print pull-quote is marked.
        quartz.CGContextSetFillColorWithColor(context, _cg_color(quartz, accent))
        quartz.CGContextFillRect(
            context, quartz.CGRectMake(0, 0, rule, height)
        )
        _draw_line(
            ct, quartz, context, body,
            _layout_origin(layout, card_width, _line_width(ct, body), pad),
            int((height - body_size) / 2),
        )
    elif layer_type == "question":
        # A question is asked, so it gets the mark that asks it and nothing
        # else. Bigger than a quote: it is meant to stop the scroll.
        asked = str(payload.get("question") or "").strip()
        if not asked.endswith(("?", "？")):
            asked += "？"
        body, body_size = _fit_text(
            foundation, ct, quartz, asked, 46 * scale, ink,
            card_width - pad * 2, scale, primary_font_floor("question"),
        )
        card_width = int(min(card_width, _line_width(ct, body) + pad * 2))
        height = int(pad * 2 + body_size * 1.4)
        context = begin_card(card_width, height, panel)
        _draw_line(
            ct, quartz, context, body,
            _layout_origin("center", card_width, _line_width(ct, body), pad),
            int((height - body_size) / 2),
        )
    elif layer_type == "comparison":
        # Two things held apart by a rule, so the eye reads them as a pair
        # rather than as one sentence that happens to have two nouns in it.
        left_text = str(payload.get("left") or "").strip()
        right_text = str(payload.get("right") or "").strip()
        column = (card_width - pad * 3) / 2
        left, left_size = _fit_text(
            foundation, ct, quartz, left_text, 36 * scale, ink, column, scale,
            primary_font_floor("comparison"),
        )
        right, right_size = _fit_text(
            foundation, ct, quartz, right_text, 36 * scale, accent, column, scale,
            primary_font_floor("comparison"),
        )
        row = max(left_size, right_size)
        # Both columns take the width of the wider side, and the card takes
        # what those need. Holding the full card width put two short words at
        # opposite ends of a slab of empty panel.
        column = max(
            _line_width(ct, left), _line_width(ct, right), 120 * scale
        )
        card_width = int(min(card_width, column * 2 + pad * 3))
        height = int(pad * 2 + row * 1.4)
        context = begin_card(card_width, height, panel)
        baseline = int((height - row) / 2)
        _draw_line(
            ct, quartz, context, left,
            int(pad + max(0, (column - _line_width(ct, left)) / 2)), baseline,
        )
        _draw_line(
            ct, quartz, context, right,
            int(pad * 2 + column + max(0, (column - _line_width(ct, right)) / 2)),
            baseline,
        )
        quartz.CGContextSetFillColorWithColor(context, _cg_color(quartz, muted))
        quartz.CGContextFillRect(
            context,
            quartz.CGRectMake(
                int(card_width / 2 - scale), int(pad * 0.6),
                max(1, int(2 * scale)), height - int(pad * 1.2),
            ),
        )
    elif layer_type == "term":
        # The term is the headline and its meaning sits under it, because that
        # is the order the sentence said them in.
        term_text = str(payload.get("term") or "").strip()
        meaning_text = str(payload.get("meaning") or "").strip()
        term, term_size = _fit_text(
            foundation, ct, quartz, term_text, 40 * scale, accent,
            card_width - pad * 2, scale, primary_font_floor("term"),
        )
        meaning, meaning_size = _fit_text(
            foundation, ct, quartz, meaning_text, 34 * scale, ink,
            card_width - pad * 2, scale, primary_font_floor("term"),
        )
        card_width = int(min(
            card_width,
            max(_line_width(ct, term), _line_width(ct, meaning)) + pad * 2,
        ))
        height = int(pad * 2 + term_size * 1.3 + meaning_size * 1.6)
        context = begin_card(card_width, height, panel)
        cursor = height - pad - term_size
        _draw_line(
            ct, quartz, context, term,
            _layout_origin(layout, card_width, _line_width(ct, term), pad), cursor,
        )
        cursor -= int(meaning_size * 1.5)
        _draw_line(
            ct, quartz, context, meaning,
            _layout_origin(layout, card_width, _line_width(ct, meaning), pad), cursor,
        )
    elif layer_type == "chart":
        datums = payload.get("datums") or []
        chart_kind = payload.get("chart_kind")
        plot_height = int(150 * scale)
        label_zone = int(CHART_LABEL_ZONE_PT * scale)
        height = pad * 2 + plot_height + label_zone
        context = begin_card(card_width, height, panel)
        plot_left = pad
        plot_right = card_width - pad
        plot_bottom = pad + label_zone
        values = [float(d.get("value", 0.0)) for d in datums]
        span = max(abs(v) for v in values) or 1.0
        baseline = plot_bottom + (plot_height / 2 if min(values) < 0 else 0)
        unit = (plot_height / (2 if min(values) < 0 else 1)) / span
        slot = (plot_right - plot_left) / max(len(datums), 1)
        quartz.CGContextSetFillColorWithColor(context, _cg_color(quartz, accent))
        points = []
        for index, value in enumerate(values):
            x0 = plot_left + index * slot
            if chart_kind == "bar":
                bar_height = value * unit
                rect = quartz.CGRectMake(
                    x0 + slot * 0.15, min(baseline, baseline + bar_height),
                    slot * 0.7, abs(bar_height),
                )
                quartz.CGContextFillRect(context, rect)
            else:
                points.append((x0 + slot / 2, baseline + value * unit))
            label, _ = _fit_text(
                foundation, ct, quartz, str(datums[index].get("label") or ""),
                CHART_LABEL_PT * scale, ink, slot * 0.9, scale,
                MIN_CHART_LABEL_PT,
            )
            _draw_line(ct, quartz, context, label, x0 + slot * 0.08, pad * 0.6)
        if chart_kind == "line" and len(points) >= 2:
            quartz.CGContextSetStrokeColorWithColor(context, _cg_color(quartz, accent))
            quartz.CGContextSetLineWidth(context, max(2.0, 3.0 * scale))
            quartz.CGContextBeginPath(context)
            quartz.CGContextMoveToPoint(context, *points[0])
            for point in points[1:]:
                quartz.CGContextAddLineToPoint(context, *point)
            quartz.CGContextStrokePath(context)
    elif layer_type == "dynamic_list":
        entries = payload.get("items") or []
        row = int(LIST_ROW_PT * scale)
        kind = component.get("kind")
        columns = 2 if kind in {"carousel_grid", "calendar_reveal"} else 1
        column_width = (card_width - pad * 2) / columns
        per_column = max(1, (len(entries) + columns - 1) // columns)
        prepared: list[tuple[int, list[Any], float]] = []
        column_heights = [0 for _ in range(columns)]
        for index, entry in enumerate(entries):
            if kind == "warning_checklist":
                prefix = "⚠ " if entry.get("severity") == "warning" else "✓ "
            elif columns > 1:
                prefix = ""
            else:
                prefix = f"{index + 1}. "
            lines, line_size = _fit_text_lines(
                foundation, ct, quartz,
                f"{prefix}{entry.get('text', '')}",
                LIST_BODY_PT * scale, ink, column_width - pad * 0.5, scale,
                MIN_LIST_BODY_PT, 2,
            )
            column = min(columns - 1, index // per_column)
            prepared.append((column, lines, line_size))
            column_heights[column] += row * len(lines)
        height = pad * 2 + max([row, *column_heights])
        context = begin_card(card_width, height, panel)
        cursors = [height - pad for _ in range(columns)]
        for column, lines, line_size in prepared:
            for line in lines:
                cursors[column] -= line_size
                _draw_line(
                    ct, quartz, context, line,
                    pad + column * column_width, cursors[column],
                )
                cursors[column] -= row - line_size
    elif layer_type == "note":
        # A small widget quoting something the speaker referred to: an icon,
        # what it is, and when. Light surface — the footage these sit on is
        # daylight and classrooms, where a dark slab reads as a hole.
        surface = _pack_token(pack, "palette", "surface", "#FBF7F0")
        surface_ink = _pack_token(pack, "palette", "surface_ink", "#2A2622")
        surface_muted = _pack_token(pack, "palette", "surface_muted", "#8C8378")
        accent_ink = _pack_token(pack, "palette", "surface_accent", "#D2571E")
        icon = str(payload.get("icon") or "").strip()
        head = f"{icon} {payload.get('title') or ''}".strip()
        meta_text = str(payload.get("meta") or "").strip()
        title, title_size = _fit_text(
            foundation, ct, quartz, head, 34 * scale, surface_ink,
            card_width - pad * 3, scale, primary_font_floor("note"),
        )
        meta = None
        meta_size = 0.0
        if meta_text:
            meta, meta_size = _fit_text(
                foundation, ct, quartz, meta_text, 32 * scale, accent_ink,
                card_width / 3, scale, primary_font_floor("note"),
            )
        body_text = str(payload.get("body") or "").strip()
        body = None
        body_size = 0.0
        if body_text:
            body, body_size = _fit_text(
                foundation, ct, quartz, body_text, 32 * scale, surface_muted,
                card_width - pad * 2, scale, primary_font_floor("note"),
            )
        wave = bool(payload.get("waveform"))
        height = int(
            pad * 2 + title_size * 1.4
            + (30 * scale if wave else 0)
            + (body_size * 1.35 if body else 0)
        )
        context = begin_card(card_width, height, surface)
        cursor = height - pad - title_size
        _draw_line(ct, quartz, context, title, pad, cursor)
        if meta is not None:
            _draw_line(
                ct, quartz, context, meta,
                card_width - pad - _line_width(ct, meta), cursor,
            )
        if wave:
            # A recording is legible as one from its waveform; the shape is
            # decoration, so it is drawn, not sampled from audio that this
            # card may not even refer to.
            cursor -= 24 * scale
            quartz.CGContextSetFillColorWithColor(context, _cg_color(quartz, accent_ink))
            bar = max(2.0, 3 * scale)
            gap = bar * 1.9
            count = int((card_width - pad * 2) / gap)
            for index in range(count):
                # Speech is uneven; bars of one height read as a progress
                # bar, not a recording. Two out-of-step cycles give the
                # irregularity without depending on any audio.
                swing = abs(((index * 5) % 13) - 6) / 6.0
                ripple = abs(((index * 3) % 7) - 3) / 3.0
                tall = 3 + 15 * (0.35 + 0.65 * swing) * (0.55 + 0.45 * ripple)
                bar_height = tall * scale
                quartz.CGContextFillRect(
                    context,
                    quartz.CGRectMake(
                        pad + index * gap, cursor + (18 * scale - bar_height) / 2,
                        bar, bar_height,
                    ),
                )
        if body is not None:
            cursor -= body_size * 1.25
            _draw_line(ct, quartz, context, body, pad, cursor)
    elif layer_type == "chip":
        # One short line, sized to itself. A chip that stretches to a fixed
        # width stops reading as a chip.
        accent = _pack_token(pack, "palette", "surface_accent", "#D2571E")
        chip_ink = _pack_token(pack, "palette", "surface", "#FBF7F0")
        text_value = str(payload.get("text") or "").strip()
        if not text_value:
            raise ValueError("a chip card needs text")
        line, line_size = _fit_text(
            foundation, ct, quartz, text_value, 34 * scale, chip_ink,
            card_width - pad * 2, scale, primary_font_floor("chip"),
        )
        chip_pad = int(18 * scale)
        card_width = int(min(card_width, _line_width(ct, line) + chip_pad * 2))
        height = int(line_size * 1.9)
        context = begin_card(card_width, height, accent)
        _draw_line(
            ct, quartz, context, line,
            (card_width - _line_width(ct, line)) / 2, (height - line_size) / 2 + line_size * 0.18,
        )
    elif layer_type == "statement":
        # A number the piece is counting off, and what that step is.
        surface = _pack_token(pack, "palette", "surface", "#FBF7F0")
        surface_ink = _pack_token(pack, "palette", "surface_ink", "#2A2622")
        accent_ink = _pack_token(pack, "palette", "surface_accent", "#D2571E")
        lead_text = str(payload.get("lead") or "").strip()
        text_value = str(payload.get("text") or "").strip()
        if not text_value:
            raise ValueError("a statement card needs text")
        lead = None
        if lead_text:
            lead, _ = _fit_text(
                foundation, ct, quartz, lead_text, 46 * scale, accent_ink,
                card_width / 3, scale, primary_font_floor("statement"),
            )
        lead_width = (_line_width(ct, lead) + 14 * scale) if lead is not None else 0.0
        body, body_size = _fit_text(
            foundation, ct, quartz, text_value, 34 * scale, surface_ink,
            card_width - pad * 2 - lead_width, scale,
            primary_font_floor("statement"),
        )
        content = lead_width + _line_width(ct, body)
        card_width = int(min(card_width, content + pad * 2))
        height = int(pad * 1.6 + body_size * 1.5)
        context = begin_card(card_width, height, surface)
        origin = max(pad, (card_width - content) / 2)
        baseline = (height - body_size) / 2 + body_size * 0.18
        if lead is not None:
            _draw_line(ct, quartz, context, lead, origin, baseline)
        _draw_line(ct, quartz, context, body, origin + lead_width, baseline)
    else:
        raise ValueError(f"unsupported structured layer type: {layer_type}")
    return _finish_card(foundation, quartz, context, project_dir, str(layer.get("id")))


def canvas_key(canvas: dict[str, Any], render_scale: float) -> str:
    return f"{canvas.get('width')}x{canvas.get('height')}@{round(float(render_scale), 4)}"


def build_structured_artifacts(
    project_dir: Path,
    state: dict[str, Any],
    layers: dict[str, Any],
    pack: dict[str, Any],
    render_scale: float = 1.0,
) -> dict[str, Any]:
    """Render all layers (cache-aware) and publish the receipt index."""
    if not compositor_available():
        raise RuntimeError("structured card compositor is not available on this host")
    canvas = state.get("canvas") or {}
    key = canvas_key(canvas, render_scale)
    pack_hash = contract_registry.canonical_hash(pack)
    mode_hash = contract_registry.canonical_hash(
        {"director_style": state.get("director_style"), "capability": capability_status()}
    )
    motion_hash = contract_registry.canonical_hash({"motion": "static_fallback"})
    index_path = project_dir / ARTIFACTS_REL
    existing_items: dict[tuple[str, str], dict[str, Any]] = {}
    if index_path.is_file():
        try:
            stored = contract_registry.load_artifact_text(index_path.read_text("utf-8"))
            for item in stored.get("items", []):
                existing_items[(item.get("layer_id"), item.get("canvas"))] = item
        except (ValueError, OSError):
            pass
    items: list[dict[str, Any]] = []
    for layer in layers.get("items", []):
        layer_hash = contract_registry.canonical_hash(layer)
        cache_key = (str(layer.get("id")), key)
        cached = existing_items.get(cache_key)
        if (
            cached
            # The drawing code is an input too: the type-size bump shipped
            # while every existing card sailed on through this cache.
            and cached.get("compiler_version") == COMPILER_VERSION
            and cached.get("structured_layer_hash") == layer_hash
            and cached.get("style_pack_hash") == pack_hash
            and cached.get("mode_hash") == mode_hash
            and (project_dir / cached.get("artifact_id", "")).is_file()
            and _file_sha256(project_dir / cached["artifact_id"])
            == cached.get("artifact_hash")
        ):
            items.append(cached)
            continue
        rel_path, digest, card_width, card_height = render_card(
            project_dir, layer, pack, canvas, render_scale
        )
        items.append(
            {
                "structured_layer_hash": layer_hash,
                "evidence_revision": layer.get("evidence_revision"),
                "mode_hash": mode_hash,
                "style_pack_hash": pack_hash,
                "resolved_motion_plan_hash": motion_hash,
                "compiler_version": COMPILER_VERSION,
                "artifact_id": rel_path,
                "artifact_hash": digest,
                "layer_id": str(layer.get("id")),
                "canvas": key,
                # The size it was actually drawn at. Without it the renderer
                # has to assume one, and a card fitted to its text gets
                # stretched back out to that assumption.
                "width": int(card_width),
                "height": int(card_height),
            }
        )
    # keep other-canvas entries (variants render per canvas)
    for (layer_id, other_key), item in existing_items.items():
        if other_key != key and any(
            layer.get("id") == layer_id for layer in layers.get("items", [])
        ):
            items.append(item)
    index = {"schema_version": 1, "items": items}
    errors = contract_registry.validate_artifact("structured_layer_artifacts", index)
    if errors:
        raise ValueError(
            "structured layer artifacts failed contract validation: " + "; ".join(errors)
        )
    scratch = index_path.with_name(index_path.name + ".tmp")
    scratch.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", "utf-8")
    scratch.replace(index_path)
    return index

# The registry is deliberately explicit.  A pack id coming from editor state
# must resolve to one checked-in contract instance; silently falling back to
# the default would make a selection look accepted while rendering another
# design.
STYLE_PACK_REGISTRY: dict[str, Path] = {
    "dark-data-presenter": (
        Path(__file__).resolve().parent.parent
        / "contracts/instances/style_pack__dark_data_presenter.json"
    ),
    "kinetic-social": (
        Path(__file__).resolve().parent.parent
        / "contracts/instances/style_pack__kinetic_social.json"
    ),
    "editorial-paper": (
        Path(__file__).resolve().parent.parent
        / "contracts/instances/style_pack__editorial_paper.json"
    ),
}
_PACK_CACHE: dict[str, dict[str, Any]] = {}


def style_pack_ids() -> tuple[str, ...]:
    """Return the public, ordered ids accepted by the style-pack registry."""
    return tuple(STYLE_PACK_REGISTRY)


def load_style_pack(pack_id: str) -> dict[str, Any]:
    """Load one checked-in style pack, failing closed on every bad selection."""
    if not isinstance(pack_id, str) or pack_id not in STYLE_PACK_REGISTRY:
        raise ValueError(f"unknown style pack: {pack_id}")
    cached = _PACK_CACHE.get(pack_id)
    if cached is not None:
        return cached
    pack_path = STYLE_PACK_REGISTRY[pack_id]
    try:
        pack = contract_registry.load_artifact_text(pack_path.read_text("utf-8"))
    except OSError as exc:
        raise RuntimeError(f"style pack {pack_id!r} is unavailable") from exc
    errors = contract_registry.validate_artifact("style_pack", pack)
    if errors:
        raise RuntimeError(
            f"style pack {pack_id!r} failed contract validation: "
            + "; ".join(errors)
        )
    if pack.get("id") != pack_id:
        raise RuntimeError(
            f"style pack registry entry {pack_id!r} contains {pack.get('id')!r}"
        )
    _PACK_CACHE[pack_id] = pack
    return pack


def load_default_pack() -> dict[str, Any]:
    """Backward-compatible alias for the original dark-data-presenter pack."""
    return load_style_pack("dark-data-presenter")
