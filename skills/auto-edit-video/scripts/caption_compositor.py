#!/usr/bin/env python3
"""CoreText caption compositor — the single caption raster truth (Phase 1b).

Renders each caption/emphasis overlay into a tight-bbox RGBA PNG with
CTFramesetter shaping (real line breaking, per-run font fallback) and writes
the ``caption_render_plan`` contract artifact. macOS is the single supported
runtime; the engine version is part of every cache key and receipt.

Font policy (plan v2 B2): text is laid out with the project's resolved font
file plus a sanctioned emoji cascade (Apple Color Emoji). Any OTHER system
fallback CoreText picks is recorded and flagged — final rendering fails
closed on unsanctioned fallbacks.
"""
from __future__ import annotations

import hashlib
import os
import platform
import re
from pathlib import Path
from typing import Any

import caption_engine
import contract_registry

CAPTIONS_REL = Path("working/captions")
RENDER_PLAN_REL = Path("working/caption_render_plan.json")
SANCTIONED_FALLBACK_PS_NAMES = {"AppleColorEmoji"}
# Compatibility name for pre-project-font plans/tests. New glyph runs use the
# actual selected asset id and never emit this placeholder.
PROJECT_FONT_ASSET_ID = "font-project-default"
EMOJI_FONT_ASSET_ID = "font-system-emoji"
# A translation is support, not a second headline.
TRANSLATION_SCALE = 0.62
# ...and it carries a lighter outline in proportion.
TRANSLATION_STROKE = 0.55

_CORETEXT = None


def _load_coretext():
    global _CORETEXT
    if _CORETEXT is not None:
        return _CORETEXT
    if os.environ.get("AUTO_EDIT_DISABLE_CORETEXT") == "1":
        _CORETEXT = False
        return _CORETEXT
    try:
        import CoreText  # type: ignore
        import Foundation  # type: ignore
        import Quartz  # type: ignore
    except Exception:  # pragma: no cover - host-dependent
        _CORETEXT = False
        return _CORETEXT
    _CORETEXT = (CoreText, Foundation, Quartz)
    return _CORETEXT


def compositor_available() -> bool:
    return bool(_load_coretext()) and caption_engine.available()


def engine_descriptor() -> dict[str, str]:
    ok = compositor_available()
    return {
        "name": "macos-coretext",
        "version": f"macos-{platform.mac_ver()[0] or 'unknown'}" if ok else "",
        "status": "present" if ok else "not_configured",
    }


def _font_binding(
    project_dir: Path,
    state: dict[str, Any],
    overlay: dict[str, Any],
) -> dict[str, Any]:
    """Resolve the exact selected bytes used by this caption raster."""
    from render_editor_timeline import project_font_binding  # lazy: import cycle

    style = overlay.get("style") if isinstance(overlay.get("style"), dict) else {}
    binding = project_font_binding(
        project_dir, state, str(style.get("font_asset_id") or "") or None,
        str(overlay.get("text") or ""),
    )
    if binding is not None:
        return binding
    from render_editor_timeline import font_path

    path = font_path()
    return {
        "asset_id": "font-legacy-system",
        "path": path,
        "sha256": font_digest(path),
        "legacy": True,
        "verified": False,
    }


def _font_file(project_dir: Path, state: dict[str, Any] | None = None, overlay: dict[str, Any] | None = None) -> Path:
    """Compatibility helper; project-aware calls retain exact binding truth."""
    if state is not None and overlay is not None:
        return Path(_font_binding(project_dir, state, overlay)["path"])
    from render_editor_timeline import font_path
    return font_path()


_FONT_DIGEST_CACHE: dict[tuple[str, int, int], str] = {}


def font_digest(path: Path) -> str:
    """SHA-256 of a (possibly large) font file, cached by (path,mtime,size)."""
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _FONT_DIGEST_CACHE.get(key)
    if cached is None:
        cached = _file_sha256(path)
        _FONT_DIGEST_CACHE.clear()
        _FONT_DIGEST_CACHE[key] = cached
    return cached


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _make_base_font(ct, size: float, font_file: Path):
    descriptors = ct.CTFontManagerCreateFontDescriptorsFromURL(
        __import__("Foundation").NSURL.fileURLWithPath_(str(font_file))
    )
    if not descriptors:
        raise ValueError(f"font file yields no descriptors: {font_file}")
    emoji_descriptor = ct.CTFontDescriptorCreateWithNameAndSize("Apple Color Emoji", size)
    base_descriptor = ct.CTFontDescriptorCreateCopyWithAttributes(
        descriptors[0], {ct.kCTFontCascadeListAttribute: [emoji_descriptor]}
    )
    return ct.CTFontCreateWithFontDescriptor(base_descriptor, size, None)


def _cg_color(quartz, hex_color: str, alpha: float = 1.0):
    value = hex_color.lstrip("#")
    red, green, blue = (int(value[i : i + 2], 16) / 255.0 for i in (0, 2, 4))
    return quartz.CGColorCreateGenericRGB(red, green, blue, alpha)


_LATIN = re.compile(r"[0-9A-Za-z@#$%&'’\-.]")
# Punctuation that must not start a line, and punctuation that must not end one.
_NO_LINE_START = "，,。.、！!？?：:；;）)」』】》〉…”’%％"
_NO_LINE_END = "（(「『【《〈“‘"
# A line holding only a couple of characters reads as a mistake, not a line.
MIN_TAIL_CHARS = 3
# Shrinking a little beats wrapping badly, but only a little.
MIN_AUTOFIT_SCALE = 0.82
AUTOFIT_STEP = 0.04


def _breakable(text: str, index: int) -> bool:
    """Can a line end just before ``index``?"""
    if index <= 0 or index >= len(text):
        return False
    before, after = text[index - 1], text[index]
    if after in _NO_LINE_START or before in _NO_LINE_END:
        return False
    if before == " ":
        return True
    # Never split a Latin word or a number down the middle; Chinese may break
    # between any two characters, which is why the framesetter alone leaves
    # words like 小門就是 cut in half.
    if _LATIN.match(before) and _LATIN.match(after):
        return False
    return True


def wrap_lines(text: str, measure, max_width: float) -> list[str] | None:
    """Break one line into balanced lines, or None if it cannot be done well.

    Greedy filling packs the first line and leaves the remainder stranded —
    that is where the single dangling character comes from. This balances
    the lines instead, and refuses a break that would strand one.
    """
    if measure(text) <= max_width:
        return [text]
    positions = [index for index in range(1, len(text)) if _breakable(text, index)]
    if not positions:
        return None

    best: tuple[float, list[str]] | None = None
    for split in positions:
        head, tail = text[:split].rstrip(), text[split:].lstrip()
        if not head or not tail:
            continue
        if len(head) < MIN_TAIL_CHARS or len(tail) < MIN_TAIL_CHARS:
            continue
        head_width, tail_width = measure(head), measure(tail)
        if head_width > max_width:
            continue
        if tail_width > max_width:
            rest = wrap_lines(tail, measure, max_width)
            if rest is None:
                continue
            score = abs(head_width - max_width) + 1000 * len(rest)
            candidate = [head, *rest]
        else:
            score = abs(head_width - tail_width)
            candidate = [head, tail]
        if best is None or score < best[0]:
            best = (score, candidate)
    return best[1] if best else None


def fit_caption_text(text: str, measure_at, font_size: float, max_width: float):
    """(lines, font size). Shrink a little before accepting a second line.

    A caption that drops one point to stay on one line reads better than one
    that keeps its size and wraps with two characters left over.
    """
    paragraphs = text.split("\n")
    size = font_size
    while size >= font_size * MIN_AUTOFIT_SCALE:
        measure = measure_at(size)
        if all(measure(part) <= max_width for part in paragraphs):
            return paragraphs, size
        size *= 1.0 - AUTOFIT_STEP

    measure = measure_at(font_size)
    wrapped: list[str] = []
    for part in paragraphs:
        lines = wrap_lines(part, measure, max_width)
        # Nothing splits well: leave it to the framesetter rather than
        # forcing a break somewhere worse.
        wrapped.extend(lines if lines is not None else [part])
    return wrapped, font_size


def render_caption_png(
    project_dir: Path,
    overlay: dict[str, Any],
    canvas: dict[str, Any],
    render_scale: float,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Shape + rasterise one caption overlay; returns the plan item dict."""
    modules = _load_coretext()
    if not modules:
        raise RuntimeError("caption compositor is not available")
    ct, foundation, quartz = modules
    pack, pack_source = selected_pack(state or {}, overlay)
    style, style_sources = resolve_caption_style(overlay, pack, pack_source)
    text = str(overlay.get("text") or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise ValueError("caption text is empty")
    # A translation rides under the spoken line, in the same frame so the
    # two share one set of line-breaking rules. Effect spans index the
    # spoken text, so it has to stay at the front and keep its offsets.
    translation = str(overlay.get("translation") or "").strip()
    translation_range: tuple[int, int] | None = None
    if translation:
        translation_range = (len(text) + 1, len(text) + 1 + len(translation))
        text = f"{text}\n{translation}"
    canvas_width = int(canvas.get("width", 1080))
    font_size = max(14.0, float(style.get("font_size", 52)) * render_scale)
    max_width = max(20.0, min(96.0, float(style.get("max_width", 84))))
    frame_width = canvas_width * render_scale * (max_width / 100.0)
    font_binding = _font_binding(project_dir, state, overlay)
    font_file = Path(font_binding["path"])

    def measure_at(size: float):
        probe_font = _make_base_font(ct, size, font_file)

        def measure(value: str) -> float:
            if not value:
                return 0.0
            probe = foundation.NSMutableAttributedString.alloc().initWithString_(value)
            probe.addAttributes_range_(
                {ct.kCTFontAttributeName: probe_font},
                foundation.NSMakeRange(0, probe.length()),
            )
            line = ct.CTLineCreateWithAttributedString(probe)
            width, _a, _d, _l = ct.CTLineGetTypographicBounds(line, None, None, None)
            return float(width)

        return measure

    # Decide the breaks here rather than letting the framesetter fill greedily:
    # it packs the first line and strands whatever is left, which is how a
    # caption ends up with one character on a line of its own.
    lines, font_size = fit_caption_text(text, measure_at, font_size, frame_width)
    text = "\n".join(lines)
    if translation_range is not None and translation:
        # The breaks moved the text, so the range has to be found again.
        marker = "\n".join(
            wrap_lines(translation, measure_at(font_size), frame_width) or [translation]
        )
        offset = text.rfind(marker)
        translation_range = (offset, offset + len(marker)) if offset >= 0 else None
    base_font = _make_base_font(ct, font_size, font_file)

    stroke_width = float(style.get("stroke_width", 3)) * render_scale
    stroke_color = _cg_color(quartz, str(style.get("stroke_color") or "#17130F"))
    fill_color = _cg_color(quartz, str(style.get("color") or "#F7F2E8"))

    max_span_scale = 1.0

    def build_attributed(pass_kind: str):
        """One attributed string per drawing pass.

        CoreText centres a stroke on the glyph outline, so a stroke wide
        enough to read against video eats inward until it swallows the fill
        of thin CJK strokes entirely.  Draw the outline first at double
        width, then lay the fill over its inner half: what survives is a
        true outside-only outline.
        """
        built = foundation.NSMutableAttributedString.alloc().initWithString_(text)
        built_range = foundation.NSMakeRange(0, built.length())
        attrs = {ct.kCTFontAttributeName: base_font}
        if pass_kind == "stroke":
            # Positive stroke width = stroke only, no fill.
            attrs[ct.kCTStrokeWidthAttributeName] = (
                stroke_width * 2.0 / font_size * 100.0
            )
            attrs[ct.kCTStrokeColorAttributeName] = stroke_color
            attrs[ct.kCTForegroundColorAttributeName] = stroke_color
        else:
            attrs[ct.kCTForegroundColorAttributeName] = fill_color
        built.addAttributes_range_(attrs, built_range)

        if translation_range is not None:
            # Smaller and quieter: it supports the spoken line rather than
            # competing with it. It keeps the same outline, because it sits
            # on the same moving picture and needs the same legibility.
            start, end = translation_range
            sub = {
                ct.kCTFontAttributeName: ct.CTFontCreateCopyWithAttributes(
                    base_font, font_size * TRANSLATION_SCALE, None, None
                )
            }
            if pass_kind == "fill":
                sub[ct.kCTForegroundColorAttributeName] = _cg_color(
                    quartz, str(style.get("translation_color") or "#DCD6CA")
                )
            else:
                # Stroke width is a share of each run's own size, so the same
                # share on a smaller line reads as equally bold — two
                # headlines instead of a line and its support.
                sub[ct.kCTStrokeWidthAttributeName] = (
                    stroke_width * 2.0 * TRANSLATION_STROKE
                    / (font_size * TRANSLATION_SCALE) * 100.0
                )
            built.addAttributes_range_(sub, foundation.NSMakeRange(start, end - start))

        nonlocal max_span_scale
        for span in overlay.get("effect_spans") or []:
            span_style = span.get("style") or {}
            start = int(span.get("start_char", 0))
            end = int(span.get("end_char", 0))
            if end <= start or end > built.length():
                continue
            span_range = foundation.NSMakeRange(start, end - start)
            scale = max(0.5, min(3.0, float(span_style.get("font_scale", 1.0))))
            max_span_scale = max(max_span_scale, scale)
            effect_kind = str(span_style.get("effect") or "pop")
            span_attrs: dict[Any, Any] = {}
            if pass_kind == "fill" and effect_kind != "highlight":
                # Highlight = backdrop marker: keep the base text colour and
                # paint a translucent pill behind the span (drawn below).
                span_attrs[ct.kCTForegroundColorAttributeName] = _cg_color(
                    quartz, str(span_style.get("color") or "#FF5533")
                )
            if scale != 1.0:
                span_attrs[ct.kCTFontAttributeName] = ct.CTFontCreateCopyWithAttributes(
                    base_font, font_size * scale, None, None
                )
            if pass_kind == "fill" and span_style.get("effect") == "underline":
                span_attrs[ct.kCTUnderlineStyleAttributeName] = ct.kCTUnderlineStyleSingle
            if span_attrs:
                built.addAttributes_range_(span_attrs, span_range)
        return built

    # Layout is identical across passes: stroke width does not change glyph
    # advances, so the fill lands exactly on top of its own outline.
    attributed = build_attributed("fill")
    framesetter = ct.CTFramesetterCreateWithAttributedString(attributed)
    constraint = quartz.CGSizeMake(frame_width, 100000.0)
    fitted, _ = ct.CTFramesetterSuggestFrameSizeWithConstraints(
        framesetter, foundation.NSMakeRange(0, 0), None, constraint, None
    )
    padding = int(max(8.0, stroke_width * 2.0, font_size * (max_span_scale - 1.0) + 4.0))
    width = int(fitted.width) + padding * 2
    height = int(fitted.height) + padding * 2
    width += width % 2
    height += height % 2

    color_space = quartz.CGColorSpaceCreateDeviceRGB()
    context = quartz.CGBitmapContextCreate(
        None, width, height, 8, width * 4, color_space,
        quartz.kCGImageAlphaPremultipliedLast,
    )
    if context is None:
        raise RuntimeError("could not create bitmap context")
    if bool(style.get("box")):
        quartz.CGContextSetFillColorWithColor(
            context, _cg_color(quartz, str(style.get("box_color") or "#201B17"), 0.82)
        )
        quartz.CGContextFillRect(context, quartz.CGRectMake(0, 0, width, height))
    path = quartz.CGPathCreateMutable()
    quartz.CGPathAddRect(
        path, None,
        quartz.CGRectMake(padding, padding, fitted.width, fitted.height),
    )
    frame = ct.CTFramesetterCreateFrame(framesetter, foundation.NSMakeRange(0, 0), path, None)

    highlight_spans = [
        (int(span.get("start_char", 0)), int(span.get("end_char", 0)), span.get("style") or {})
        for span in overlay.get("effect_spans") or []
        if (span.get("style") or {}).get("effect") == "highlight"
        and int(span.get("end_char", 0)) > int(span.get("start_char", 0))
    ]
    if highlight_spans:
        lines = ct.CTFrameGetLines(frame)
        origins = ct.CTFrameGetLineOrigins(
            frame, foundation.NSMakeRange(0, len(lines)), None
        )
        for line, origin in zip(lines, origins):
            line_range = ct.CTLineGetStringRange(line)
            _width, ascent, descent, _leading = ct.CTLineGetTypographicBounds(
                line, None, None, None
            )
            for span_start, span_end, span_style in highlight_spans:
                clipped_start = max(span_start, line_range.location)
                clipped_end = min(span_end, line_range.location + line_range.length)
                if clipped_end <= clipped_start:
                    continue

                def line_offset(index: int) -> float:
                    result = ct.CTLineGetOffsetForStringIndex(line, index, None)
                    return float(result[0] if isinstance(result, tuple) else result)

                x_start = line_offset(clipped_start)
                x_end = line_offset(clipped_end)
                quartz.CGContextSetFillColorWithColor(
                    context,
                    _cg_color(quartz, str(span_style.get("color") or "#F5A623"), 0.45),
                )
                quartz.CGContextFillRect(
                    context,
                    quartz.CGRectMake(
                        padding + origin.x + min(x_start, x_end),
                        padding + origin.y - descent - 2,
                        abs(x_end - x_start),
                        ascent + descent + 4,
                    ),
                )

    if stroke_width > 0:
        stroke_frame = ct.CTFramesetterCreateFrame(
            ct.CTFramesetterCreateWithAttributedString(build_attributed("stroke")),
            foundation.NSMakeRange(0, 0),
            path,
            None,
        )
        ct.CTFrameDraw(stroke_frame, context)
    ct.CTFrameDraw(frame, context)

    # Glyph-run font accounting (plan v2 B2): map every run back to a
    # declared asset or flag it.
    project_ps_names = set()
    ps_name = ct.CTFontCopyPostScriptName(base_font)
    if ps_name:
        project_ps_names.add(str(ps_name))
    clusters = caption_engine.boundary_map(text)

    def cluster_index_for(utf16_offset: int) -> int:
        for index, (cluster_start, cluster_end) in enumerate(clusters):
            if cluster_start <= utf16_offset < cluster_end:
                return index
        return max(0, len(clusters) - 1)

    glyph_runs: list[dict[str, Any]] = []
    disallowed_fallbacks: list[str] = []
    lines = ct.CTFrameGetLines(frame)
    for line in lines:
        for run in ct.CTLineGetGlyphRuns(line):
            attributes = ct.CTRunGetAttributes(run)
            run_font = attributes.get(ct.kCTFontAttributeName)
            run_ps = str(ct.CTFontCopyPostScriptName(run_font)) if run_font else "unknown"
            run_range = ct.CTRunGetStringRange(run)
            if run_ps in project_ps_names:
                asset_id = str(font_binding["asset_id"])
            elif run_ps in SANCTIONED_FALLBACK_PS_NAMES:
                asset_id = EMOJI_FONT_ASSET_ID
            else:
                asset_id = f"font-unsanctioned-{run_ps}"
                disallowed_fallbacks.append(run_ps)
            glyph_runs.append(
                {
                    "font_asset_id": asset_id,
                    "cluster_start": cluster_index_for(run_range.location),
                    "cluster_end": cluster_index_for(
                        max(run_range.location, run_range.location + run_range.length - 1)
                    )
                    + 1,
                }
            )

    image = quartz.CGBitmapContextCreateImage(context)
    captions_dir = project_dir / CAPTIONS_REL
    captions_dir.mkdir(parents=True, exist_ok=True)
    scratch = captions_dir / f".rendering-{overlay.get('id')}.png"
    url = foundation.NSURL.fileURLWithPath_(str(scratch))
    destination = quartz.CGImageDestinationCreateWithURL(url, "public.png", 1, None)
    if destination is None:
        raise RuntimeError("could not create PNG destination")
    quartz.CGImageDestinationAddImage(destination, image, None)
    if not quartz.CGImageDestinationFinalize(destination):
        raise RuntimeError("PNG finalize failed")
    artifact_hash = _file_sha256(scratch)
    final_path = captions_dir / f"{overlay.get('id')}-{artifact_hash[:16]}.png"
    scratch.replace(final_path)

    return {
        "caption_item_id": str(overlay.get("id")),
        "style_sources": style_sources,
        "clusters": clusters,
        "glyph_runs": glyph_runs,
        "artifact": {
            "rgba_path": final_path.relative_to(project_dir).as_posix(),
            "artifact_hash": artifact_hash,
            "width": width,
            "height": height,
        },
        "x_padding": padding,
        "x_disallowed_fallbacks": sorted(set(disallowed_fallbacks)),
        "x_font_binding": font_binding,
    }


def caption_overlays(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        overlay
        for overlay in state.get("overlays", [])
        if isinstance(overlay, dict)
        and overlay.get("type") in {"caption", "emphasis"}
        and overlay.get("visible", True)
        and not overlay.get("design_role")
        and str(overlay.get("text") or "").strip()
    ]


def caption_content_revision(
    state: dict[str, Any], canvas: dict[str, Any], render_scale: float, project_dir: Path | None = None,
) -> str:
    font_identity: dict[str, Any] = {}
    if project_dir is not None:
        for overlay in caption_overlays(state):
            try:
                binding = _font_binding(project_dir, state, overlay)
                font_identity[str(overlay.get("id"))] = {
                    "asset_id": binding.get("asset_id"), "sha256": binding.get("sha256"),
                }
            except (OSError, ValueError):
                font_identity[str(overlay.get("id"))] = "unresolved"
    else:
        try:
            font_identity["legacy"] = font_digest(_font_file(None))
        except (OSError, ValueError):
            font_identity["legacy"] = "unresolved"
    pack_identity: dict[str, Any] = {}
    for overlay in caption_overlays(state):
        try:
            pack, pack_source = selected_pack(state, overlay)
        except ValueError:
            pack, pack_source = None, "invalid"
        pack_identity[str(overlay.get("id"))] = {
            "source": pack_source,
            "defaults": pack_caption_defaults(pack) if pack else {},
        }
    payload = {
        "engine": engine_descriptor(),
        "font_sha256": font_identity,
        "style_packs": pack_identity,
        "canvas": {"width": canvas.get("width"), "height": canvas.get("height")},
        "render_scale": round(float(render_scale), 4),
        "items": [
            {
                "id": overlay.get("id"),
                "text": overlay.get("text"),
                "style": overlay.get("style"),
                "effect_spans": overlay.get("effect_spans"),
            }
            for overlay in caption_overlays(state)
        ],
    }
    return contract_registry.canonical_hash(payload)


def build_render_plan(
    project_dir: Path,
    state: dict[str, Any],
    render_scale: float = 1.0,
) -> dict[str, Any]:
    """Render every caption overlay (cache-aware) and write the plan artifact."""
    if not compositor_available():
        raise RuntimeError("caption compositor is not available on this host")
    canvas = state.get("canvas") or {}
    content_revision = caption_content_revision(state, canvas, render_scale, project_dir)
    plan_path = project_dir / RENDER_PLAN_REL
    if plan_path.is_file():
        try:
            existing = contract_registry.load_artifact_text(plan_path.read_text("utf-8"))
            if existing.get("caption_revision") == content_revision and all(
                (project_dir / item["artifact"]["rgba_path"]).is_file()
                and _file_sha256(project_dir / item["artifact"]["rgba_path"])
                == item["artifact"]["artifact_hash"]
                for item in existing.get("items", [])
            ):
                return existing
        except (ValueError, OSError, KeyError):
            pass
    items = [
        render_caption_png(project_dir, overlay, canvas, render_scale, state)
        for overlay in caption_overlays(state)
    ]
    disallowed = sorted({name for item in items for name in item.pop("x_disallowed_fallbacks")})
    font_receipts: dict[str, dict[str, Any]] = {}
    for item in items:
        item.pop("x_padding", None)
        binding = item.pop("x_font_binding", {})
        asset_id = str(binding.get("asset_id") or "font-legacy-system")
        path = binding.get("path")
        font_receipts[asset_id] = {
            "path": str(path), "sha256": str(binding.get("sha256") or ""),
        }
    plan = {
        "schema_version": 1,
        "caption_revision": content_revision,
        "grapheme_contract": "grapheme_cluster_v1",
        "items": items,
        "receipt": {
            "shaping_engine": "macos-coretext",
            "shaping_engine_version": engine_descriptor()["version"],
            "fonts": {
                **font_receipts,
                EMOJI_FONT_ASSET_ID: {"path": "system:Apple Color Emoji", "sha256": ""},
            },
            "disallowed_fallbacks": disallowed,
        },
    }
    plan["revision"] = contract_registry.canonical_hash(plan)
    errors = contract_registry.validate_artifact("caption_render_plan", plan)
    if errors:
        raise ValueError("caption render plan failed contract validation: " + "; ".join(errors))
    scratch = plan_path.with_name(plan_path.name + ".tmp")
    scratch.write_text(
        __import__("json").dumps(plan, ensure_ascii=False, indent=2) + "\n", "utf-8"
    )
    scratch.replace(plan_path)
    return plan

SYSTEM_CAPTION_DEFAULTS = {"color": "#F7F2E8", "stroke_color": "#17130F"}
PACK_CAPTION_KEYS = ("color", "stroke_color")


def pack_caption_defaults(pack: dict[str, Any]) -> dict[str, str]:
    palette = pack.get("tokens", {}).get("palette", {})
    defaults: dict[str, str] = {}
    if palette.get("ink"):
        defaults["color"] = str(palette["ink"])
    if palette.get("bg"):
        defaults["stroke_color"] = str(palette["bg"])
    return defaults


def resolve_caption_style(
    overlay: dict[str, Any],
    pack: dict[str, Any] | None,
    pack_source: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Precedence (plan v2): manual key > pack default > system default.

    Variant overrides slot in above manual at render time (P3). Returns the
    resolved style plus a per-key source map for the render receipt.
    """
    manual = overlay.get("style") or {}
    resolved = dict(manual)
    sources = {key: "manual" for key in manual}
    pack_defaults = pack_caption_defaults(pack) if pack else {}
    for key in PACK_CAPTION_KEYS:
        if key in manual and manual.get(key):
            continue
        if key in pack_defaults:
            resolved[key] = pack_defaults[key]
            sources[key] = pack_source
        elif key in SYSTEM_CAPTION_DEFAULTS:
            resolved[key] = SYSTEM_CAPTION_DEFAULTS[key]
            sources[key] = "system"
    return resolved, sources


def selected_pack(state: dict[str, Any], overlay: dict[str, Any]):
    """Per-highlight pack beats the project default; None when unset."""
    import structured_card_compositor

    selection = state.get("style_pack") or {}
    highlight = str(overlay.get("highlight_id") or "")
    pack_id = (
        (selection.get("per_highlight") or {}).get(highlight)
        or selection.get("project_default")
    )
    if not pack_id:
        return None, "system"
    pack = structured_card_compositor.load_default_pack()
    if pack.get("id") != pack_id:
        raise ValueError(f"unknown style pack: {pack_id}")
    scope = "pack-highlight" if (selection.get("per_highlight") or {}).get(highlight) else "pack-project"
    return pack, f"{scope}:{pack_id}"
