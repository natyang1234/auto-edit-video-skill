#!/usr/bin/env python3
"""Render editor_state.json to a reproducible MP4 preview/final or cover PNG."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import qa_video
from typing import Any

from editor_server import (
    PLATFORM_PRESETS,
    editor_state_revision,
    ffprobe_has_visual_stream,
    file_sha256,
    read_json,
    referenced_asset_digests,
)
from graphic_package import ensure_graphic_package
from visual_quality import DESIGN_ROLES, overlays_for_clip, visual_quality_errors


FFMPEG_FULL = Path("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg")
FONT_CANDIDATES = (
    Path("/System/Library/Fonts/PingFang.ttc"),
    Path("/System/Library/Fonts/STHeiti Light.ttc"),
    Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
    Path("/mnt/c/Windows/Fonts/msjh.ttc"),
)


def ffmpeg_path() -> str:
    override = str(os.environ.get("AUTO_EDIT_FFMPEG", "")).strip()
    if override and Path(override).expanduser().is_file():
        return str(Path(override).expanduser())
    if FFMPEG_FULL.is_file():
        return str(FFMPEG_FULL)
    command = shutil.which("ffmpeg")
    if not command:
        raise ValueError("ffmpeg is required")
    return command


def source_has_audible_signal(source: Path, start: float, duration: float) -> bool:
    """Return true only when the selected source range has a usable audio peak."""
    command = [
        ffmpeg_path(),
        "-hide_banner",
        "-nostats",
    ]
    if start > 0:
        command.extend(["-ss", f"{start:.3f}"])
    command.extend(
        [
            "-t",
            f"{duration:.3f}",
            "-i",
            str(source),
            "-vn",
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ]
    )
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    match = re.findall(r"max_volume:\s*(-?(?:inf|[0-9.]+))\s+dB", result.stderr or "")
    if not match or match[-1] == "-inf":
        return False
    try:
        return float(match[-1]) > -70.0
    except ValueError:
        return False


def project_font_binding(
    project_dir: Path | None,
    state: dict[str, Any] | None = None,
    font_asset_id: str | None = None,
    required_text: str = "",
) -> dict[str, Any] | None:
    """Resolve a selected project font at the renderer trust boundary.

    A family is presentation metadata only.  When an asset id is selected we
    ask the provenance registry for the exact receipt-bound bytes and never
    fall back to ``fc-match`` or a local family lookup.
    """
    if font_asset_id is not None and not isinstance(font_asset_id, str):
        raise ValueError("font_asset_id is invalid")
    selected = str(font_asset_id or "").strip()
    if not selected and isinstance(state, dict):
        defaults = state.get("caption_defaults")
        if isinstance(defaults, dict):
            candidate = defaults.get("font_asset_id")
            if candidate is not None and not isinstance(candidate, str):
                raise ValueError("font_asset_id is invalid")
            selected = str(candidate or "").strip()
    if not selected:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", selected):
        raise ValueError("font_asset_id is invalid")
    if project_dir is None:
        raise ValueError("selected project font requires project_dir")
    import asset_registry

    try:
        binding = asset_registry.resolve_project_font(
            Path(project_dir), selected, required_text=required_text,
        )
    except asset_registry.AssetRegistryError as exc:
        raise ValueError(f"project font {selected} is unavailable: {exc}") from exc
    if not isinstance(binding, dict) or not isinstance(binding.get("path"), str):
        raise ValueError(f"project font {selected} returned an invalid binding")
    path = (Path(project_dir) / binding["path"]).resolve()
    if Path(project_dir).resolve() not in path.parents or not path.is_file():
        raise ValueError(f"project font {selected} path is unsafe")
    if not re.fullmatch(r"[0-9a-f]{64}", str(binding.get("sha256") or "")):
        raise ValueError(f"project font {selected} has no verified SHA-256")
    return {**binding, "path": path, "verified": True}


def font_path(
    project_dir: Path | None = None,
    state: dict[str, Any] | None = None,
    font_asset_id: str | None = None,
    required_text: str = "",
) -> Path:
    binding = project_font_binding(project_dir, state, font_asset_id, required_text)
    if binding is not None:
        return Path(binding["path"])
    # Legacy/system behavior deliberately remains available only when no
    # project asset id was selected. It is never represented as verified.
    override = str(os.environ.get("AUTO_EDIT_FONT", "")).strip()
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_file():
            return candidate
        raise ValueError(f"AUTO_EDIT_FONT does not exist: {candidate}")
    match = shutil.which("fc-match")
    if match:
        for family in ("Noto Sans CJK TC:lang=zh-tw", "PingFang TC", "WenQuanYi Zen Hei"):
            result = subprocess.run(
                [match, family, "-f", "%{file}"],
                text=True,
                capture_output=True,
            )
            candidate = Path(result.stdout.strip())
            if result.returncode == 0 and candidate.is_file():
                return candidate
    for candidate in FONT_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise ValueError("no CJK-capable font found")


def even(value: float) -> int:
    rounded = max(2, int(round(value)))
    return rounded if rounded % 2 == 0 else rounded + 1


def preview_dimensions(width: int, height: int) -> tuple[int, int]:
    longest = max(width, height)
    scale = min(1.0, 960.0 / longest)
    return even(width * scale), even(height * scale)


def color(value: Any, fallback: str = "#f7f2e8") -> str:
    raw = str(value or fallback).strip()
    match = re.fullmatch(r"#([0-9a-fA-F]{6})", raw)
    return f"0x{match.group(1)}" if match else f"0x{fallback.removeprefix('#')}"


def filter_path(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def wrap_overlay_text(text: str, width: int, style: dict[str, Any], render_scale: float) -> str:
    font_size = max(16, int(float(style.get("font_size", 52)) * render_scale))
    max_width = max(20.0, min(96.0, float(style.get("max_width", 84))))
    chars = max(5, int(width * (max_width / 100.0) / max(font_size, 1)))
    compact = re.sub(r"\s+", " ", text).strip()
    if not compact:
        return ""
    if re.search(r"[\u3400-\u9fff]", compact):
        return "\n".join(compact[index : index + chars] for index in range(0, len(compact), chars))
    return "\n".join(textwrap.wrap(compact, width=max(chars, 8), break_long_words=False))


# What each style-pack preset asks for, expressed as motion this pipeline can
# apply to a finished card. Presets that animate a card's *contents* — digits
# counting, words arriving one by one, a page turning — cannot be done by
# moving a finished image, so they take the closest entrance instead and say
# so in the render receipt rather than pretending.
MOTION_PRESETS = {
    "slide-in": ("slide-in", True),
    "slide-up": ("slide-up", True),
    "fade-up": ("slide-up", True),
    "pan": ("pan", True),
    "check-pop": ("pop", True),
    "fade": ("fade", True),
    "count-up": ("pop", False),
    "word-cascade": ("fade", False),
    "staggered-reveal": ("fade", False),
    "fill": ("slide-in", False),
    "flip": ("pop", False),
}
MOTION_ANIMATIONS = {"fade", "pop", "slide-up", "slide-in", "pan"}


def motion_for_layer(pack: dict[str, Any], layers: dict[str, Any], layer_id: str) -> str:
    """The animation this card's component asks for."""
    import structured_card_compositor

    layer = next(
        (item for item in layers.get("items", []) if item.get("id") == layer_id), None
    )
    if layer is None:
        return "fade"
    try:
        component = structured_card_compositor.resolve_component(
            pack, str(layer.get("type")), layer.get("component_id")
        )
    except ValueError:
        return "fade"
    preset = (component.get("motion") or {}).get("preset")
    return resolve_motion(preset)[0]


def resolve_motion(preset: str | None) -> tuple[str, bool]:
    """The animation to apply, and whether it is what the preset asked for."""
    return MOTION_PRESETS.get(str(preset or ""), ("fade", False))


def motion_values(
    style: dict[str, Any],
    start: float,
    end: float,
    base_y: str,
    offset: float,
) -> tuple[str, str | None]:
    """Return FFmpeg y/alpha expressions matching the live editor motions."""
    animation = str(style.get("animation", "none"))
    if animation not in MOTION_ANIMATIONS:
        return base_y, None
    duration = max(0.01, end - start)
    fade_duration = min(0.18, max(0.01, duration * 0.22))
    fade_in_end = start + fade_duration
    fade_out_start = end - fade_duration
    alpha = (
        f"if(lt(t,{fade_in_end:.3f}),(t-{start:.3f})/{fade_duration:.3f},"
        f"if(gt(t,{fade_out_start:.3f}),({end:.3f}-t)/{fade_duration:.3f},1))"
    )
    if animation == "fade":
        return base_y, alpha
    motion_duration = min(0.22, max(0.01, duration * 0.28))
    motion_end = start + motion_duration
    if animation in {"slide-in", "pan"}:
        # Horizontal motion; the vertical position holds.
        return base_y, alpha
    motion_offset = offset if animation == "slide-up" else offset * 0.35
    y = (
        f"if(lt(t,{motion_end:.3f}),({base_y})+"
        f"({motion_end:.3f}-t)/{motion_duration:.3f}*{motion_offset:.3f},({base_y}))"
    )
    return y, alpha


def text_filter(
    input_label: str,
    output_label: str,
    overlay: dict[str, Any],
    width: int,
    height: int,
    render_scale: float,
    font: Path,
    text_file: Path,
) -> str:
    style = overlay.get("style") or {}
    font_size = max(14, int(float(style.get("font_size", 52)) * render_scale))
    border = max(0, int(float(style.get("stroke_width", 3)) * render_scale))
    x_pct = max(0.0, min(100.0, float(style.get("x", 50))))
    y_pct = max(0.0, min(100.0, float(style.get("y", 78))))
    x = f"{width * x_pct / 100.0:.3f}-text_w/2"
    base_y = f"{height * y_pct / 100.0:.3f}-text_h/2"
    start = max(0.0, float(overlay.get("start", 0.0)))
    end = max(start + 0.01, float(overlay.get("end", start + 0.01)))
    y, alpha = motion_values(style, start, end, base_y, max(12.0, font_size * 0.7))
    enable = f"between(t,{start:.3f},{end:.3f})"
    filters: list[str] = []
    current = input_label
    kind = overlay.get("type")
    if kind in {"title", "card"} or bool(style.get("box")):
        max_width = max(20.0, min(96.0, float(style.get("max_width", 84))))
        box_width = int(width * max_width / 100.0)
        box_height = max(int(font_size * 2.6), int(height * 0.08))
        box_x = int(width * x_pct / 100.0 - box_width / 2)
        box_y = int(height * y_pct / 100.0 - box_height / 2)
        box_color = color(style.get("box_color"), "#201b17")
        box_out = f"{output_label}_box"
        filters.append(
            f"[{current}]drawbox=x={box_x}:y={box_y}:w={box_width}:h={box_height}:"
            f"color={box_color}@0.88:t=fill:enable='{enable}'[{box_out}]"
        )
        current = box_out
    alpha_option = f":alpha='{alpha}'" if alpha else ""
    filters.append(
        f"[{current}]drawtext=fontfile='{filter_path(font)}':"
        f"textfile='{filter_path(text_file)}':expansion=none:"
        f"fontcolor={color(style.get('color'))}:fontsize={font_size}:"
        f"borderw={border}:bordercolor={color(style.get('stroke_color'), '#17130f')}:"
        f"line_spacing={max(2, int(font_size * 0.14))}:x='{x}':y='{y}'"
        f"{alpha_option}:enable='{enable}'[{output_label}]"
    )
    return ";".join(filters)


def image_filter(
    input_label: str,
    output_label: str,
    asset_label: str,
    overlay: dict[str, Any],
    width: int,
    height: int,
) -> str:
    style = overlay.get("style") or {}
    asset_width = even(width * max(0.5, min(100.0, float(style.get("width", 32)))) / 100.0)
    x_pct = max(0.0, min(100.0, float(style.get("x", 50))))
    y_pct = max(0.0, min(100.0, float(style.get("y", 50))))
    x = f"{width * x_pct / 100.0:.3f}-overlay_w/2"
    base_y = f"{height * y_pct / 100.0:.3f}-overlay_h/2"
    start = max(0.0, float(overlay.get("start", 0.0)))
    end = max(start + 0.01, float(overlay.get("end", start + 0.01)))
    y, _alpha = motion_values(style, start, end, base_y, max(12.0, height * 0.04))
    duration = max(0.01, end - start)
    animation_name = str(style.get("animation", "none"))
    if animation_name == "slide-in":
        travel = max(24.0, width * 0.05)
        enter = min(0.28, max(0.05, duration * 0.3))
        x = (
            f"if(lt(t,{start + enter:.3f}),({x})-"
            f"({start + enter:.3f}-t)/{enter:.3f}*{travel:.3f},({x}))"
        )
    elif animation_name == "pan":
        # A slow drift across the whole hold, which is what a carousel does.
        drift = max(16.0, width * 0.03)
        x = f"({x})+({drift:.3f}*min(1,max(0,(t-{start:.3f})/{duration:.3f}))-{drift / 2:.3f})"
    fade_duration = min(0.18, max(0.01, duration * 0.22))
    animation = str(style.get("animation", "none"))
    scaled = f"{output_label}_asset"
    asset_filters = (
        f"[{asset_label}]scale={asset_width}:-2,format=rgba,"
        f"setpts=PTS-STARTPTS+{start:.3f}/TB"
    )
    if animation in MOTION_ANIMATIONS:
        asset_filters += (
            f",fade=t=in:st={start:.3f}:d={fade_duration:.3f}:alpha=1"
            f",fade=t=out:st={max(start, end - fade_duration):.3f}:"
            f"d={fade_duration:.3f}:alpha=1"
        )
    if animation == "pop":
        pop_duration = min(0.22, max(0.05, duration * 0.28))
        asset_filters += (
            ",scale=eval=frame:"
            f"w='iw*(0.86+0.14*min(1,max(0,(t-{start:.3f})/{pop_duration:.3f})))':h=-2"
        )
    return (
        f"{asset_filters}[{scaled}];"
        f"[{input_label}][{scaled}]overlay=x='{x}':y='{y}':"
        f"enable='between(t,{start:.3f},{end:.3f})':eof_action=pass:shortest=0[{output_label}]"
    )


CAPTION_TYPES = {"caption", "emphasis"}


def _compositor_available_cached() -> bool:
    import caption_compositor

    return caption_compositor.compositor_available()


def is_plain_caption(overlay: dict[str, Any]) -> bool:
    return (
        isinstance(overlay, dict)
        and overlay.get("type") in CAPTION_TYPES
        and not overlay.get("design_role")
    )


def strip_caption_overlays(state: dict[str, Any]) -> dict[str, Any]:
    """Designed-route state without plain captions — the compositor is the
    single caption truth, so the graphic package must not bake them again."""
    return {
        **state,
        "overlays": [
            overlay
            for overlay in state.get("overlays", [])
            if not is_plain_caption(overlay)
        ],
    }


def captionized_overlays(
    overlays: list[dict[str, Any]],
    caption_plan: dict[str, Any],
    width: int,
) -> list[dict[str, Any]]:
    """Swap plain caption overlays for their compositor PNG artifacts.

    Runs after post-cut mapping, so windows/timing stay untouched; a caption
    split across a removed region shares one PNG across both windows.
    """
    artifact_by_id = {
        item["caption_item_id"]: item["artifact"] for item in caption_plan.get("items", [])
    }
    converted: list[dict[str, Any]] = []
    for overlay in overlays:
        if not is_plain_caption(overlay):
            converted.append(overlay)
            continue
        artifact = artifact_by_id.get(str(overlay.get("id")))
        if artifact is None:
            continue  # empty/hidden captions have no raster
        style = overlay.get("style") or {}
        converted.append(
            {
                "id": overlay.get("id"),
                "type": "image",
                "source": artifact["rgba_path"],
                "start": overlay.get("start"),
                "end": overlay.get("end"),
                "visible": True,
                "z_index": overlay.get("z_index", 0),
                "style": {
                    "width": max(5.0, min(100.0, artifact["width"] / max(width, 1) * 100.0)),
                    "x": float(style.get("x", 50)),
                    "y": float(style.get("y", 78)),
                    "animation": str(style.get("animation", "none")),
                },
            }
        )
    return converted


def state_segments(state: dict[str, Any], source_duration: float) -> list[tuple[float, float]]:
    """Ordered source segments from a v2 state; v1/absent means full source."""
    raw = state.get("segments")
    if not isinstance(raw, list) or not raw:
        return [(0.0, max(source_duration, 0.001))]
    segments: list[tuple[float, float]] = []
    for entry in raw:
        try:
            start = float(entry.get("source_start"))
            end = float(entry.get("source_end"))
        except (TypeError, ValueError) as exc:
            raise ValueError("segment timing must be numeric") from exc
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise ValueError("segment needs 0 <= source_start < source_end")
        if end > source_duration + 0.05:
            raise ValueError("segment extends past the source duration")
        segments.append((start, end))
    return segments


def effective_segments(
    segments: list[tuple[float, float]],
    clip_start: float,
    clip_end: float,
) -> list[tuple[float, float]]:
    """Intersect the timeline segments with a requested clip range, in order."""
    out: list[tuple[float, float]] = []
    for start, end in segments:
        clipped_start = max(start, clip_start)
        clipped_end = min(end, clip_end)
        if clipped_end > clipped_start + 1e-6:
            out.append((clipped_start, clipped_end))
    if not out:
        raise ValueError("the requested clip range does not intersect any timeline segment")
    return out


def map_source_range_to_post_cut(
    segments: list[tuple[float, float]],
    range_start: float,
    range_end: float,
) -> list[tuple[float, float]]:
    """Project a source-time range onto the post-cut axis.

    An overlay may intersect several segments (e.g. it spans a removed
    silence); each intersection becomes its own post-cut window. Ranges that
    fall entirely inside removed material yield no windows.
    """
    windows: list[tuple[float, float]] = []
    offset = 0.0
    for start, end in segments:
        hit_start = max(range_start, start)
        hit_end = min(range_end, end)
        if hit_end > hit_start + 1e-6:
            windows.append((offset + hit_start - start, offset + hit_end - start))
        offset += end - start
    return windows


def build_render_command(
    project_dir: Path,
    state: dict[str, Any],
    manifest: dict[str, Any],
    output: Path,
    quality: str,
    clip: dict[str, Any] | None = None,
    visual_source: Path | None = None,
) -> list[str]:
    canvas = state.get("canvas") or {}
    target_width = int(canvas.get("width", 1080))
    target_height = int(canvas.get("height", 1920))
    if quality == "preview":
        width, height = preview_dimensions(target_width, target_height)
    else:
        width, height = even(target_width), even(target_height)
    render_scale = width / max(target_width, 1)
    source_duration = float(manifest.get("source", {}).get("duration_s", 0.0))
    clip_start = 0.0
    clip_end = source_duration
    if clip is not None:
        try:
            clip_start = float(clip.get("start"))
            clip_end = float(clip.get("end"))
        except (TypeError, ValueError) as exc:
            raise ValueError("render clip timing must be numeric") from exc
        if (
            not math.isfinite(clip_start)
            or not math.isfinite(clip_end)
            or clip_start < 0
            or clip_end <= clip_start
            or clip_end > source_duration + 0.05
        ):
            raise ValueError("render clip timing is outside the source")
    timeline_segments = state_segments(state, source_duration)
    segments = effective_segments(timeline_segments, clip_start, clip_end)
    multi_segment = len(segments) > 1
    if multi_segment and visual_source is not None:
        raise ValueError(
            "designed graphic package rendering does not support multi-segment "
            "timelines yet; flatten the timeline or use basic mode"
        )
    if multi_segment:
        duration = sum(end - start for start, end in segments)
    else:
        clip_start, clip_end = segments[0]
        duration = clip_end - clip_start
    source_rel = str(manifest.get("source", {}).get("staged_path", ""))
    source = project_dir / source_rel
    if not source.is_file():
        raise ValueError(f"source media missing: {source}")
    fit = canvas.get("fit", "cover")
    if visual_source is not None:
        graphics_root = (project_dir / "working/graphic_packages").resolve()
        visual_entry = visual_source.expanduser()
        if visual_entry.is_symlink():
            raise ValueError("designed graphic package must be a project-owned regular file")
        visual_source = visual_entry.resolve()
        if graphics_root not in visual_source.parents or not visual_source.is_file():
            raise ValueError("designed graphic package must stay under working/graphic_packages")
        base_filter = f"scale={width}:{height},setsar=1,setpts=PTS-STARTPTS"
    elif fit == "contain":
        base_filter = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x171512,setsar=1,setpts=PTS-STARTPTS"
        )
    else:
        base_filter = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,setpts=PTS-STARTPTS"
        )

    command = [ffmpeg_path(), "-y"]
    if not multi_segment and clip_start > 0:
        command.extend(["-ss", f"{clip_start:.3f}"])
    command.extend(["-i", str(source)])
    visual_input_index: int | None = None
    if visual_source is not None:
        visual_input_index = 1
        command.extend(["-i", str(visual_source)])
    overlays: list[dict[str, Any]] = []
    for source_overlay in state.get("overlays", []):
        if not isinstance(source_overlay, dict) or not source_overlay.get("visible", True):
            continue
        scoped_highlight = str(source_overlay.get("highlight_id") or "")
        clip_id = str(clip.get("id") or "") if clip is not None else ""
        if scoped_highlight and scoped_highlight != clip_id:
            continue
        if visual_source is not None and source_overlay.get("design_role"):
            # design-role cards are baked by the graphic package
            continue
        if (
            visual_source is not None
            and source_overlay.get("type") in {"caption", "emphasis"}
            and not _compositor_available_cached()
        ):
            # Legacy designed path only: without the compositor the package
            # bakes captions itself, so skip them here (route table).
            continue
        raw_start = float(source_overlay.get("start", 0.0))
        raw_end = float(source_overlay.get("end", 0.0))
        # Overlay times are stored on the source axis; project them onto the
        # post-cut axis. Crossing a removed region yields one window per
        # surviving intersection; fully removed overlays disappear.
        for window_start, window_end in map_source_range_to_post_cut(
            segments, raw_start, raw_end
        ):
            overlay = dict(source_overlay)
            overlay["style"] = dict(source_overlay.get("style") or {})
            overlay["start"] = window_start
            overlay["end"] = window_end
            overlays.append(overlay)
    overlays.sort(key=lambda item: (int(item.get("z_index", 0)), float(item.get("start", 0.0))))
    from editor_server import load_layer_bundle

    layers_bundle, visual_plan_v2 = load_layer_bundle(project_dir)
    if layers_bundle.get("items"):
        import structured_card_compositor

        if not structured_card_compositor.compositor_available():
            if quality == "final":
                raise ValueError(
                    "structured layers need the static card compositor, which is "
                    "unavailable on this host; final would silently lose content"
                )
        else:
            selection = state.get("style_pack") or {}
            if selection.get("project_default") or selection.get("per_highlight"):
                resolved_pack = structured_card_compositor.load_default_pack()
            else:
                # No pack selected: cards render with the compositor's own
                # fallback tokens; an empty pack keeps the hash distinct so
                # selecting a pack later verifiably re-renders.
                resolved_pack = {"id": "none", "tokens": {}}
            artifacts_index = structured_card_compositor.build_structured_artifacts(
                project_dir, state, layers_bundle, resolved_pack, render_scale,
            )
            key = structured_card_compositor.canvas_key(canvas, render_scale)
            artifact_by_layer = {
                item["layer_id"]: item
                for item in artifacts_index.get("items", [])
                if item.get("canvas") == key
            }
            for plan_item in visual_plan_v2.get("items", []):
                layer_ref = plan_item.get("structured_layer_id")
                asset_ref = plan_item.get("selected_asset")
                windows = map_source_range_to_post_cut(
                    segments,
                    float(plan_item.get("start", 0.0)),
                    float(plan_item.get("end", 0.0)),
                )
                for window_start, window_end in windows:
                    if layer_ref and layer_ref in artifact_by_layer:
                        artifact = artifact_by_layer[layer_ref]
                        overlays.append(
                            {
                                "id": plan_item.get("id"),
                                "type": "image",
                                "source": artifact["artifact_id"],
                                "start": window_start,
                                "end": window_end,
                                "visible": True,
                                "z_index": 5,
                                "style": {
                                    "width": 84.0,
                                    "x": 50,
                                    "y": 46,
                                    # The style pack says how this component
                                    # should arrive; every card fading in
                                    # regardless was the pack going unread.
                                    "animation": motion_for_layer(
                                        resolved_pack, layers_bundle, layer_ref
                                    ),
                                },
                            }
                        )
                    elif asset_ref:
                        overlays.append(
                            {
                                "id": plan_item.get("id"),
                                "type": "image",
                                "source": str(asset_ref),
                                "start": window_start,
                                "end": window_end,
                                "visible": True,
                                "z_index": 4,
                                "style": {"width": 60.0, "x": 50, "y": 42,
                                          "animation": "fade"},
                            }
                        )

    if any(is_plain_caption(overlay) for overlay in overlays):
        import caption_compositor

        if caption_compositor.compositor_available():
            caption_plan = caption_compositor.build_render_plan(
                project_dir, state, render_scale
            )
            disallowed = caption_plan.get("receipt", {}).get("disallowed_fallbacks") or []
            if disallowed and quality == "final":
                raise ValueError(
                    "captions use unsanctioned system font fallbacks "
                    f"({', '.join(disallowed)}); add the glyph coverage to the "
                    "project font or mark the caption for review"
                )
            overlays = captionized_overlays(overlays, caption_plan, width)

    asset_inputs: dict[str, int] = {}
    for overlay in overlays:
        if overlay.get("type") not in {"image", "gif", "video"}:
            continue
        source_rel = str(overlay.get("source", ""))
        asset = (project_dir / source_rel).resolve()
        if project_dir.resolve() not in asset.parents or not asset.is_file():
            raise ValueError(f"asset missing or outside project: {source_rel}")
        key = str(asset)
        if key in asset_inputs:
            continue
        suffix = asset.suffix.lower()
        if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
            command.extend(["-loop", "1", "-framerate", str(canvas.get("fps", 30)), "-i", key])
        elif suffix == ".gif":
            command.extend(["-ignore_loop", "0", "-stream_loop", "-1", "-i", key])
        else:
            command.extend(["-stream_loop", "-1", "-i", key])
        asset_inputs[key] = len(asset_inputs) + (2 if visual_input_index is not None else 1)

    render_text_dir = project_dir / "working/render_text"
    render_text_dir.mkdir(parents=True, exist_ok=True)
    fps_value = int(canvas.get("fps", 30))
    if multi_segment:
        # Unified-timeline prelude (contracts/policies/UNIFIED_TIMELINE.md):
        # normalise to CFR and a common timebase BEFORE trimming so concat
        # cannot drift on VFR or odd-timebase sources, then cut and rejoin.
        segment_count = len(segments)
        prelude = [
            f"[0:v]fps={fps_value},settb=AVTB,split={segment_count}"
            + "".join(f"[vin{i}]" for i in range(segment_count))
        ]
        for i, (segment_start, segment_end) in enumerate(segments):
            prelude.append(
                f"[vin{i}]trim=start={segment_start:.6f}:end={segment_end:.6f},"
                f"setpts=PTS-STARTPTS[vseg{i}]"
            )
        prelude.append(
            "".join(f"[vseg{i}]" for i in range(segment_count))
            + f"concat=n={segment_count}:v=1:a=0[vcat]"
        )
        filters = prelude + [f"[vcat]{base_filter}[v0]"]
    else:
        filters = [
            f"[{visual_input_index if visual_input_index is not None else 0}:v]{base_filter}[v0]"
        ]
    current = "v0"
    for index, overlay in enumerate(overlays, start=1):
        output_label = f"v{index}"
        kind = overlay.get("type")
        if kind in {"image", "gif", "video"}:
            asset = str((project_dir / str(overlay.get("source", ""))).resolve())
            filters.append(
                image_filter(current, output_label, f"{asset_inputs[asset]}:v", overlay, width, height)
            )
        else:
            text = wrap_overlay_text(str(overlay.get("text", "")), width, overlay.get("style") or {}, render_scale)
            if not text:
                continue
            safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(overlay.get("id", index)))
            text_file = render_text_dir / f"{safe_id}.txt"
            text_file.write_text(text, encoding="utf-8")
            overlay_style = overlay.get("style") if isinstance(overlay.get("style"), dict) else {}
            font = font_path(
                project_dir,
                state,
                str(overlay_style.get("font_asset_id") or "") or None,
                str(overlay.get("text") or ""),
            )
            filters.append(
                text_filter(
                    current,
                    output_label,
                    overlay,
                    width,
                    height,
                    render_scale,
                    font,
                    text_file,
                )
            )
        current = output_label

    output.parent.mkdir(parents=True, exist_ok=True)
    has_audio_stream = manifest.get("source", {}).get("has_audio") is not False
    # Probe the covered SOURCE span; with reorder the first/last segment give
    # a negative span, so use min/max over all segments (Codex review).
    probe_start = min(start for start, _end in segments)
    probe_span = max(end for _start, end in segments) - probe_start
    normalize_audio = has_audio_stream and source_has_audible_signal(
        source, probe_start, probe_span
    )
    if multi_segment:
        if has_audio_stream:
            segment_count = len(segments)
            filters.append(
                "[0:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"asplit={segment_count}"
                + "".join(f"[ain{i}]" for i in range(segment_count))
            )
            for i, (segment_start, segment_end) in enumerate(segments):
                filters.append(
                    f"[ain{i}]atrim=start={segment_start:.6f}:end={segment_end:.6f},"
                    f"asetpts=PTS-STARTPTS[aseg{i}]"
                )
            filters.append(
                "".join(f"[aseg{i}]" for i in range(segment_count))
                + f"concat=n={segment_count}:v=0:a=1[acat]"
            )
            if normalize_audio:
                filters.append("[acat]loudnorm=I=-16:TP=-1.5:LRA=11[aout]")
            else:
                filters.append("[acat]anull[aout]")
        else:
            # Sourceless audio: emit a silent bed of exactly the post-cut
            # duration so every variant has a uniform stream layout (plan B2).
            filters.append(
                f"anullsrc=r=48000:cl=stereo,atrim=0:{duration:.6f},"
                "asetpts=PTS-STARTPTS[aout]"
            )
        command.extend(["-filter_complex", ";".join(filters), "-map", f"[{current}]"])
        command.extend(["-map", "[aout]"])
    else:
        command.extend(
            [
                "-filter_complex",
                ";".join(filters),
                "-map",
                f"[{current}]",
                "-map",
                "0:a?",
            ]
        )
        if normalize_audio:
            command.extend(
                ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000"]
            )
        command.extend(["-t", f"{duration:.3f}"])
    command.extend(
        [
            "-r",
            str(int(canvas.get("fps", 30))),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast" if quality == "preview" else "medium",
            "-crf",
            "24" if quality == "preview" else "18",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
    return command


def load_render_snapshot(
    project_dir: Path,
    snapshot_path: Path,
    quality: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    root = project_dir.resolve()
    snapshot_root = (root / "working/render_snapshots").resolve()
    entry = snapshot_path.expanduser()
    if not entry.is_absolute():
        entry = root / entry
    if entry.is_symlink():
        raise ValueError("render snapshot must be an owned regular file")
    resolved = entry.resolve()
    if snapshot_root not in resolved.parents or not resolved.is_file():
        raise ValueError("render snapshot must be under working/render_snapshots")
    snapshot = read_json(resolved, {}) or {}
    if snapshot.get("schema_version") != 1 or snapshot.get("quality") != quality:
        raise ValueError("render snapshot schema or quality is invalid")
    state = snapshot.get("state") if isinstance(snapshot.get("state"), dict) else {}
    manifest = snapshot.get("manifest") if isinstance(snapshot.get("manifest"), dict) else {}
    revision = editor_state_revision(state)
    if snapshot.get("state_revision") != revision:
        raise ValueError("render snapshot state revision does not match its payload")
    if snapshot.get("project_id") != manifest.get("project_id"):
        raise ValueError("render snapshot project identity does not match")
    actual_assets = referenced_asset_digests(root, state)
    if actual_assets != (state.get("asset_digests") or {}):
        raise ValueError("render snapshot asset digest does not match current owned assets")
    source_rel = str(manifest.get("source", {}).get("staged_path", ""))
    source_entry = root / source_rel
    if source_entry.is_symlink():
        raise ValueError("render source must be an owned regular file")
    source = source_entry.resolve()
    if root not in source.parents or not source.is_file():
        raise ValueError("render source is missing or outside the project")
    declared_source_sha = str(manifest.get("source", {}).get("sha256") or "")
    if declared_source_sha:
        if state.get("source_sha256") != declared_source_sha:
            raise ValueError("render snapshot source digest contract is inconsistent")
        if file_sha256(source) != declared_source_sha:
            raise ValueError("render source changed after import")
    clip = snapshot.get("clip") if isinstance(snapshot.get("clip"), dict) else None
    if clip is not None:
        matching = next(
            (
                item
                for item in state.get("highlights", [])
                if isinstance(item, dict) and str(item.get("id")) == str(clip.get("id"))
            ),
            None,
        )
        if matching is None or any(
            matching.get(key) != clip.get(key)
            for key in ("plan_item_id", "start", "end", "title", "review_status")
        ):
            raise ValueError("render clip does not match the frozen editor state")
    if quality == "final":
        authorization = (
            snapshot.get("authorization")
            if isinstance(snapshot.get("authorization"), dict)
            else {}
        )
        revisions = (
            snapshot.get("approval_revisions")
            if isinstance(snapshot.get("approval_revisions"), dict)
            else {}
        )
        required = ["destructive_edit", "timeline"]
        if state.get("highlights"):
            required.append("highlight_selection")
        for gate in required:
            approval = authorization.get(gate, {})
            if (
                not isinstance(approval, dict)
                or not approval.get("approved")
                or approval.get("state_revision") != revisions.get(gate)
            ):
                raise ValueError(f"current {gate} revision must be approved before final render")
        if clip is not None and clip.get("review_status") != "approved":
            raise ValueError("final render clip must be approved")
    return manifest, state, clip


def render_project(
    project_dir: Path,
    output: Path,
    quality: str,
    snapshot_path: Path | None = None,
    variant_id: str | None = None,
) -> None:
    if snapshot_path is None:
        manifest = read_json(project_dir / "project.json", {}) or {}
        state = read_json(project_dir / "working/editor_state.json", {}) or {}
        if state.get("schema_version") == 1:
            raise SystemExit(
                "editor_state schema_version 1 must be migrated first; open the "
                "editor page once to run the v1→v2 migration"
            )
        clip = None
        if variant_id:
            from editor_server import (
                build_variant_snapshot,
                rights_gate_errors,
                variant_approval_is_current,
                variant_state_for,
            )

            if quality == "final":
                if not variant_approval_is_current(
                    project_dir, manifest, "timeline", state, variant_id
                ):
                    raise ValueError(
                        f"variant {variant_id}: current timeline snapshot must be "
                        "approved before final render"
                    )
                rights_errors = rights_gate_errors(project_dir, state)
                if rights_errors:
                    raise ValueError("rights gate: " + "; ".join(rights_errors))
                build_variant_snapshot(project_dir, state, variant_id)
            state = variant_state_for(state, variant_id)
        elif quality == "final":
            approval = manifest.get("approvals", {}).get("timeline", {})
            from editor_server import gate_revision, rights_gate_errors

            if (
                not approval.get("approved")
                or approval.get("state_revision")
                != gate_revision(project_dir, "timeline", state)
            ):
                raise ValueError("current timeline revision must be approved before final render")
            rights_errors = rights_gate_errors(project_dir, state)
            if rights_errors:
                raise ValueError("rights gate: " + "; ".join(rights_errors))
    else:
        manifest, state, clip = load_render_snapshot(project_dir, snapshot_path, quality)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.parent / f".{output.stem}.{uuid.uuid4().hex}.part.mp4"
    try:
        visual_source: Path | None = None
        if clip is not None and state.get("visual_quality_mode") == "designed":
            quality_errors = visual_quality_errors(state, manifest, clip)
            if quality_errors:
                raise ValueError("visual-quality contract failed: " + "; ".join(quality_errors))
            roles = {
                str(item.get("design_role"))
                for item in overlays_for_clip(state, clip)
                if item.get("design_role")
            }
            if set(DESIGN_ROLES).issubset(roles):
                import caption_compositor

                package_state = (
                    strip_caption_overlays(state)
                    if caption_compositor.compositor_available()
                    else state
                )
                visual_source = ensure_graphic_package(
                    project_dir, package_state, manifest, clip
                )
        if quality == "final" and visual_source is None:
            import caption_compositor

            if not caption_compositor.compositor_available():
                # Fail-closed gate (route table, plan v2): without the
                # compositor the drawtext route would silently drop
                # per-character styling.
                spans_present = any(
                    overlay.get("effect_spans")
                    for overlay in state.get("overlays", [])
                    if isinstance(overlay, dict) and overlay.get("visible", True)
                )
                if spans_present:
                    raise ValueError(
                        "per-character effect spans need the caption compositor, "
                        "which is unavailable on this host; remove the effects or "
                        "install the macOS CoreText stack"
                    )
        command = build_render_command(
            project_dir,
            state,
            manifest,
            temporary,
            quality,
            clip,
            visual_source,
        )
        try:
            result = subprocess.run(
                command,
                text=True,
                capture_output=True,
                timeout=2 * 60 * 60,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("ffmpeg render timed out") from exc
        if result.returncode != 0 or not temporary.is_file() or not ffprobe_has_visual_stream(temporary):
            raise RuntimeError((result.stderr or result.stdout or "ffmpeg render failed")[-5000:])
        if variant_id and quality == "final":
            # QA runs on the temporary output; only a passing QA publishes
            # the file + receipt together (no receipt-less final on disk).
            receipt = qa_variant_output(project_dir, temporary, variant_id, state)
            os.replace(temporary, output)
            finalize_variant_delivery_receipt(project_dir, receipt, output, variant_id)
        else:
            os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def qa_variant_output(
    project_dir: Path, candidate: Path, variant_id: str, state: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Run QA on the still-unpublished output; raise before anything lands."""
    qa_dir = project_dir / "qa"
    qa_dir.mkdir(exist_ok=True)
    report_path = qa_dir / f"variant-{variant_id}.json"
    contact_path = qa_dir / f"variant-{variant_id}-contact.png"
    qa_result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("qa_video.py")),
            "--video", str(candidate),
            "--report", str(report_path),
            "--contact", str(contact_path),
            *qa_video.qa_policy_args(state),
        ],
        text=True,
        capture_output=True,
    )
    if qa_result.returncode != 0:
        report_path.unlink(missing_ok=True)
        contact_path.unlink(missing_ok=True)
        raise RuntimeError(
            "variant QA failed; final output was NOT published: "
            + (qa_result.stderr or qa_result.stdout)[-2000:]
        )
    return {
        "report": report_path.relative_to(project_dir).as_posix(),
        "report_sha256": file_sha256(report_path),
        "contact_sheet": contact_path.relative_to(project_dir).as_posix(),
        "contact_sheet_sha256": file_sha256(contact_path),
        "output_sha256": file_sha256(candidate),
    }


def finalize_variant_delivery_receipt(
    project_dir: Path,
    qa_receipt: dict[str, Any],
    output: Path,
    variant_id: str,
) -> None:
    """Per-variant delivery receipt (plan v2 B4): no single-slot clobbering."""
    from editor_server import (
        VARIANT_DELIVERY_REL,
        VARIANT_SNAPSHOTS_REL,
        atomic_write_json,
        read_json as server_read_json,
    )

    snapshot = server_read_json(
        project_dir / VARIANT_SNAPSHOTS_REL / f"{variant_id}.json", {}
    )
    receipt = {
        "schema_version": 1,
        "variant_id": variant_id,
        "quality": "final",
        "snapshot_hash": (snapshot or {}).get("snapshot_hash"),
        "status": "pass",
        "output": output.relative_to(project_dir).as_posix(),
        **qa_receipt,
    }
    delivery_dir = project_dir / VARIANT_DELIVERY_REL
    delivery_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(delivery_dir / f"{variant_id}.json", receipt)


def render_cover(
    project_dir: Path,
    output: Path,
    platform_id: str,
    timestamp: float,
    title: str,
) -> None:
    manifest = read_json(project_dir / "project.json", {}) or {}
    state = read_json(project_dir / "working/editor_state.json", {}) or {}
    preset = PLATFORM_PRESETS[platform_id]
    width = even(int(preset["cover_width"]))
    height = even(int(preset["cover_height"]))
    source = project_dir / str(manifest.get("source", {}).get("staged_path", ""))
    if not source.is_file():
        raise ValueError(f"source media missing: {source}")
    style = dict(state.get("caption_defaults") or {})
    style.update(
        {
            "font_size": max(34, int(width * 0.065)),
            "x": 50,
            "y": 68,
            "max_width": 84,
            "box": True,
            "box_color": "#201b17",
        }
    )
    text_dir = project_dir / "working/render_text"
    text_dir.mkdir(parents=True, exist_ok=True)
    text_file = text_dir / "cover.txt"
    text_file.write_text(wrap_overlay_text(title, width, style, 1.0), encoding="utf-8")
    overlay = {
        "type": "title",
        "start": 0,
        "end": 1,
        "text": title,
        "style": style,
    }
    filters = [
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1[v0]"
    ]
    if title.strip():
        filters.append(
            text_filter(
                "v0", "v1", overlay, width, height, 1.0,
                font_path(project_dir, state, required_text=title), text_file,
            )
        )
        final_label = "v1"
    else:
        final_label = "v0"
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg_path(),
        "-y",
        "-ss",
        f"{max(0.0, timestamp):.3f}",
        "-i",
        str(source),
        "-filter_complex",
        ";".join(filters),
        "-map",
        f"[{final_label}]",
        "-frames:v",
        "1",
        "-update",
        "1",
        str(output),
    ]
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode != 0 or not output.is_file():
        raise RuntimeError((result.stderr or result.stdout or "cover render failed")[-5000:])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--quality", choices=("preview", "final"), default="preview")
    parser.add_argument("--snapshot", help="Frozen render snapshot under working/render_snapshots")
    parser.add_argument("--variant", help="Render a specific output variant (per-variant approvals)")
    parser.add_argument("--cover", action="store_true")
    parser.add_argument("--platform", choices=tuple(PLATFORM_PRESETS), default="instagram-reels")
    parser.add_argument("--cover-time", type=float, default=0.0)
    parser.add_argument("--cover-text", default="")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    project_dir = Path(args.project_dir).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    try:
        if args.cover:
            render_cover(project_dir, output, args.platform, args.cover_time, args.cover_text)
        else:
            render_project(
                project_dir,
                output,
                args.quality,
                Path(args.snapshot) if args.snapshot else None,
                args.variant or None,
            )
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, "output": str(output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
