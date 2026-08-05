#!/usr/bin/env python3
"""Static structured-card compositor — Phase 1c ``static_fallback`` capability.

Renders title / stat / chart(bar,line) / dynamic_list layers into tight RGBA
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
from pathlib import Path
from typing import Any

import caption_compositor
import contract_registry
from caption_compositor import _cg_color, _file_sha256, _load_coretext

CARDS_REL = Path("working/structured_cards")
ARTIFACTS_REL = Path("working/structured_layer_artifacts.json")
COMPILER_VERSION = "static-card-compositor-v1"
MIN_LABEL_PT = 11.0


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


def _fit_text(foundation, ct, quartz, text, size, color, max_width, render_scale):
    """Shrink to fit; below the minimum point size the card fails closed."""
    current = size
    while current >= MIN_LABEL_PT * render_scale:
        attributed = _attributed(foundation, ct, quartz, text, current, color)
        if _line_width(ct, attributed) <= max_width:
            return attributed, current
        current *= 0.9
    raise ValueError(
        f"label does not fit at the minimum size: {text!r} "
        "(shorten the copy or widen the layer)"
    )


def _draw_line(ct, quartz, context, attributed, x: float, y: float) -> None:
    quartz.CGContextSetTextPosition(context, x, y)
    ct.CTLineDraw(ct.CTLineCreateWithAttributedString(attributed), context)


def _begin_card(quartz, width: int, height: int, panel_color: str):
    color_space = quartz.CGColorSpaceCreateDeviceRGB()
    context = quartz.CGBitmapContextCreate(
        None, width, height, 8, width * 4, color_space,
        quartz.kCGImageAlphaPremultipliedLast,
    )
    if context is None:
        raise RuntimeError("could not create card bitmap context")
    quartz.CGContextSetFillColorWithColor(context, _cg_color(quartz, panel_color, 0.92))
    path = quartz.CGPathCreateWithRoundedRect(
        quartz.CGRectMake(0, 0, width, height), 14, 14, None
    )
    quartz.CGContextAddPath(context, path)
    quartz.CGContextFillPath(context)
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
}
DEFAULT_COMPONENT = {
    "title": "prompt_card",
    "stat": "hero_stat",
    "chart": "dashboard",
    "dynamic_list": "dynamic_list",
    "note": "note_card",
    "chip": "chip",
    "statement": "statement_card",
}


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
    card_width = int(canvas_width * 0.84)
    pad = int(28 * scale)
    ink = _pack_token(pack, "palette", "ink", "#E6EDF3")
    accent = _pack_token(pack, "palette", "accent", "#E5484D")
    panel = _pack_token(pack, "palette", "panel", "#161B22")
    muted = "#9AA4AF"

    if layer_type == "title":
        title, title_size = _fit_text(
            foundation, ct, quartz, str(payload.get("title") or ""),
            44 * scale, ink, card_width - pad * 2, scale,
        )
        kicker_text = str(payload.get("kicker") or "")
        subtitle_text = str(payload.get("subtitle") or "")
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
        height = int(pad * 2 + title_size * 1.4 + (22 * scale if kicker_text else 0)
                     + (26 * scale if subtitle_text else 0))
        context = _begin_card(quartz, card_width, height, panel)
        cursor = height - pad
        if kicker_text:
            kicker, _ = _fit_text(foundation, ct, quartz, kicker_text, 16 * scale,
                                  accent, card_width - pad * 2, scale)
            cursor -= 16 * scale
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
        if subtitle_text:
            subtitle, _ = _fit_text(foundation, ct, quartz, subtitle_text, 18 * scale,
                                    muted, card_width - pad * 2, scale)
            cursor -= 26 * scale
            _draw_line(ct, quartz, context, subtitle, pad, cursor)
    elif layer_type == "stat":
        value_text = str(payload.get("value"))
        value, value_size = _fit_text(foundation, ct, quartz, value_text,
                                      64 * scale, accent, card_width - pad * 2, scale)
        label, _ = _fit_text(foundation, ct, quartz, str(payload.get("label") or ""),
                             20 * scale, ink, card_width - pad * 2, scale)
        source_text = str(payload.get("source_literal") or "")
        height = int(pad * 2 + value_size * 1.2 + 30 * scale + (20 * scale if source_text else 0))
        context = _begin_card(quartz, card_width, height, panel)
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
        cursor -= 26 * scale
        _draw_line(ct, quartz, context, label, pad, cursor)
        if source_text:
            source, _ = _fit_text(foundation, ct, quartz, f"「{source_text}」",
                                  13 * scale, muted, card_width - pad * 2, scale)
            cursor -= 20 * scale
            _draw_line(ct, quartz, context, source, pad, cursor)
    elif layer_type == "chart":
        datums = payload.get("datums") or []
        chart_kind = payload.get("chart_kind")
        plot_height = int(150 * scale)
        label_zone = int(26 * scale)
        height = pad * 2 + plot_height + label_zone
        context = _begin_card(quartz, card_width, height, panel)
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
                13 * scale, ink, slot * 0.9, scale,
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
        row = int(30 * scale)
        wrapped = 2 if component.get("kind") in {"carousel_grid", "calendar_reveal"} else 1
        rows = (len(entries) + wrapped - 1) // wrapped
        height = pad * 2 + row * max(rows, 1)
        context = _begin_card(quartz, card_width, height, panel)
        kind = component.get("kind")
        columns = 2 if kind in {"carousel_grid", "calendar_reveal"} else 1
        column_width = (card_width - pad * 2) / columns
        for index, entry in enumerate(entries):
            if kind == "warning_checklist":
                prefix = "⚠ " if entry.get("severity") == "warning" else "✓ "
            elif columns > 1:
                prefix = ""
            else:
                prefix = f"{index + 1}. "
            line, _ = _fit_text(
                foundation, ct, quartz,
                f"{prefix}{entry.get('text', '')}",
                18 * scale, ink, column_width - pad * 0.5, scale,
            )
            column, position = divmod(index, max(1, (len(entries) + columns - 1) // columns)) \
                if columns > 1 else (0, index)
            _draw_line(
                ct, quartz, context, line,
                pad + column * column_width,
                height - pad - row * position - 20 * scale,
            )
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
            foundation, ct, quartz, head, 26 * scale, surface_ink,
            card_width - pad * 3, scale,
        )
        meta = None
        if meta_text:
            meta, _ = _fit_text(
                foundation, ct, quartz, meta_text, 22 * scale, accent_ink,
                card_width / 3, scale,
            )
        body_text = str(payload.get("body") or "").strip()
        body = None
        if body_text:
            body, _ = _fit_text(
                foundation, ct, quartz, body_text, 18 * scale, surface_muted,
                card_width - pad * 2, scale,
            )
        wave = bool(payload.get("waveform"))
        height = int(
            pad * 2 + title_size * 1.4
            + (30 * scale if wave else 0)
            + (26 * scale if body else 0)
        )
        context = _begin_card(quartz, card_width, height, surface)
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
            cursor -= 26 * scale
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
            foundation, ct, quartz, text_value, 26 * scale, chip_ink,
            card_width - pad * 2, scale,
        )
        chip_pad = int(18 * scale)
        card_width = int(min(card_width, _line_width(ct, line) + chip_pad * 2))
        height = int(line_size * 1.9)
        context = _begin_card(quartz, card_width, height, accent)
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
                card_width / 3, scale,
            )
        lead_width = (_line_width(ct, lead) + 14 * scale) if lead is not None else 0.0
        body, body_size = _fit_text(
            foundation, ct, quartz, text_value, 34 * scale, surface_ink,
            card_width - pad * 2 - lead_width, scale,
        )
        content = lead_width + _line_width(ct, body)
        card_width = int(min(card_width, content + pad * 2))
        height = int(pad * 1.6 + body_size * 1.5)
        context = _begin_card(quartz, card_width, height, surface)
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

_PACK_CACHE: dict[str, Any] | None = None


def load_default_pack() -> dict[str, Any]:
    """Style pack registry — fail closed like the platform/director loaders."""
    global _PACK_CACHE
    if _PACK_CACHE is not None:
        return _PACK_CACHE
    pack_path = (
        Path(__file__).resolve().parent.parent
        / "contracts/instances/style_pack__dark_data_presenter.json"
    )
    pack = contract_registry.load_artifact_text(pack_path.read_text("utf-8"))
    errors = contract_registry.validate_artifact("style_pack", pack)
    if errors:
        raise RuntimeError("style pack registry invalid: " + "; ".join(errors))
    _PACK_CACHE = pack
    return pack

