#!/usr/bin/env python3
"""Render editor_state.json to a reproducible MP4 preview/final or cover PNG."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import textwrap
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import qa_video
import contract_registry
import delivery_envelope
import sfx_delivery
from typing import Any

from editor_server import (
    DIRECTOR_PRESETS,
    PLATFORM_PRESETS,
    atomic_write_json,
    audio_event_edits_hash,
    editor_base_state_revision,
    editor_state_revision,
    ffprobe_has_visual_stream,
    file_sha256,
    platform_safe_area,
    read_json,
    referenced_asset_digests,
    resolve_audio_event_source,
    resolve_studio_audio_plan,
)
from graphic_package import ensure_graphic_package
from visual_quality import (
    DESIGN_ROLES,
    overlays_for_clip,
    rendered_visual_evidence_path,
    rendered_visual_quality_report,
    visual_quality_errors,
)


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

def animated_card_overlay_source(
    project_dir: Path,
    pack: dict[str, Any],
    layers: dict[str, Any],
    layer_id: str,
    artifact: dict[str, Any],
    duration_s: float,
    fps: int,
    render_scale: float = 1.0,
) -> str | None:
    """A transparent .mov of the card's content animation, or None.

    None means the static PNG with its entrance approximation ships instead —
    the preset is an entrance anyway, the animator is unavailable, or the
    render failed. A delivery is never blocked on a browser.
    """
    import card_animator
    import structured_card_compositor

    layer = next(
        (item for item in layers.get("items", []) if item.get("id") == layer_id), None
    )
    if layer is None:
        return None
    try:
        component = structured_card_compositor.resolve_component(
            pack, str(layer.get("type")), layer.get("component_id")
        )
    except ValueError:
        return None
    preset = str((component.get("motion") or {}).get("preset") or "")
    if preset not in card_animator.CONTENT_PRESETS:
        return None
    if not card_animator.available():
        print(
            json.dumps({"card_animation_fallback": {
                "layer": layer_id, "preset": preset,
                "reason": "card animator unavailable",
            }}, ensure_ascii=False),
            file=sys.stderr,
        )
        return None
    try:
        rendered = card_animator.render_card_animation(
            project_dir, layer, pack, preset,
            int(artifact.get("width") or 0), int(artifact.get("height") or 0),
            duration_s, fps, render_scale,
        )
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print(
            json.dumps({"card_animation_fallback": {
                "layer": layer_id, "preset": preset,
                "reason": str(exc)[:200],
            }}, ensure_ascii=False),
            file=sys.stderr,
        )
        return None
    return rendered.relative_to(project_dir).as_posix()


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


def card_visual_evidence(
    pack: dict[str, Any],
    layers: dict[str, Any],
    layer_id: str,
    render_scale: float,
    animated_source: str | None,
) -> dict[str, Any]:
    """Describe what a structured card asked for and what reached FFmpeg."""
    import structured_card_compositor

    layer = next(
        (item for item in layers.get("items", []) if item.get("id") == layer_id),
        None,
    )
    if layer is None:
        raise ValueError(f"structured layer {layer_id!r} is missing from visual evidence")
    component = structured_card_compositor.resolve_component(
        pack, str(layer.get("type")), layer.get("component_id")
    )
    preset = str((component.get("motion") or {}).get("preset") or "")
    delivered, native_faithful = resolve_motion(preset)
    if animated_source:
        delivered = preset
        faithful = True
        status = "rendered"
    else:
        faithful = native_faithful
        status = "native" if native_faithful else "fallback"
    layer_type = str(layer.get("type") or "")
    floor = structured_card_compositor.primary_font_floor(layer_type)
    evidence = {
        "kind": layer_type,
        "component_id": str(component.get("id") or "") or None,
        "style_pack_id": str(pack.get("id") or "") or None,
        "font_evidence_required": True,
        "minimum_primary_font_px": (
            round(floor * render_scale, 3)
        ),
        "motion": {
            "requested": preset,
            "delivered": delivered,
            "faithful": faithful,
            "status": status,
        },
    }
    if layer_type == "title":
        payload = layer.get("payload")
        if isinstance(payload, dict):
            title_kind = payload.get("title_kind")
            if isinstance(title_kind, str) and title_kind:
                evidence["title_kind"] = title_kind
            evidence_id = payload.get("evidence_id")
            source_literal = payload.get("source_literal")
            if isinstance(evidence_id, str) and evidence_id:
                evidence["evidence_id"] = evidence_id
            if isinstance(source_literal, str) and source_literal:
                evidence["source_literal"] = source_literal
    elif layer_type == "mosaic":
        payload = layer.get("payload")
        assets = payload.get("assets") if isinstance(payload, dict) else None
        if not isinstance(assets, list) or not 2 <= len(assets) <= 4:
            raise ValueError("mosaic renderer evidence requires two to four assets")
        evidence["evidence_id"] = payload.get("evidence_id")
        evidence["source_literal"] = payload.get("source_literal")
        evidence["assets"] = [
            {
                "asset_id": item.get("asset_id"),
                "path": item.get("path"),
                "sha256": item.get("sha256"),
                "evidence_id": item.get("evidence_id"),
                "source_literal": item.get("source_literal"),
            }
            for item in assets
            if isinstance(item, dict)
        ]
    return evidence


SCENE_PLAN_FIELDS = (
    "eligibility",
    "eligibility_reason",
    "family",
    "role",
    "importance",
    "major_graphic",
    "micro_silent",
    "stage",
    "trigger_role",
)


@dataclass(frozen=True, slots=True)
class FrozenVisualAuthority:
    """Stable scene inputs and bytes held across render and publication."""

    public: dict[str, Any]
    layers: dict[str, Any]
    plan: dict[str, Any]
    artifacts: dict[str, Any]
    snapshots: tuple[delivery_envelope.FileSnapshot, ...]
    motion_graphics: dict[str, dict[str, Any]] = field(
        default_factory=dict, repr=False, compare=False
    )

    def revalidate(self) -> None:
        for snapshot in self.snapshots:
            delivery_envelope.revalidate_file_snapshot(snapshot)

    def bind_motion_graphic(
        self,
        project_dir: Path,
        scene_id: str,
        artifact: dict[str, Any],
        overlay: dict[str, Any],
        animated_source: str | None,
        canvas_width: int,
        canvas_height: int,
    ) -> str:
        """Bind one rendered overlay to frozen bytes and an exact placement model."""
        existing = self.motion_graphics.get(scene_id)
        if existing is not None:
            return str(existing["source_path"])
        authority_item = next(
            (item for item in self.public["items"] if item.get("id") == scene_id), None
        )
        if authority_item is None or authority_item.get("artifact_hash") != artifact.get(
            "artifact_hash"
        ):
            raise ValueError("motion graphic does not match frozen scene authority")
        artifact_path = Path(str(artifact.get("artifact_id") or ""))
        if animated_source is None:
            source_path = artifact_path
            source_sha256 = str(artifact.get("artifact_hash") or "")
            source_kind = "image"
        else:
            snapshot = delivery_envelope.snapshot_project_file(
                project_dir,
                animated_source,
                label=f"animated motion graphic {scene_id}",
                capture_bytes=True,
            )
            if snapshot.payload is None:
                raise ValueError("animated motion graphic bytes were not captured")
            safe_scene = re.sub(r"[^A-Za-z0-9_-]", "_", scene_id)
            source_path = artifact_path.parent / (
                f"motion-{safe_scene}-{snapshot.sha256}.mov"
            )
            _write_frozen_payload(source_path, snapshot.payload)
            source_sha256 = snapshot.sha256
            source_kind = "video"
            object.__setattr__(self, "snapshots", (*self.snapshots, snapshot))
        style = overlay.get("style") if isinstance(overlay.get("style"), dict) else {}
        binding = {
            "artifact_sha256": str(artifact.get("artifact_hash") or ""),
            "source_path": str(source_path),
            "source_sha256": source_sha256,
            "source_kind": source_kind,
            "canvas_width": canvas_width,
            "canvas_height": canvas_height,
            "source_start_sample": sfx_delivery.seconds_to_samples(overlay["start"]),
            "source_end_sample": sfx_delivery.seconds_to_samples(overlay["end"]),
            "placement": {
                "width_percent": float(style.get("width", 84.0)),
                "x_percent": float(style.get("x", 50.0)),
                "y_percent": float(style.get("y", 50.0)),
                "animation": str(style.get("animation", "none")),
            },
        }
        public_binding = {key: value for key, value in binding.items() if key != "source_path"}
        authority_item["motion_attribution"] = public_binding
        self.public["authority_hash"] = contract_registry.canonical_hash(
            {key: value for key, value in self.public.items() if key != "authority_hash"}
        )
        self.motion_graphics[scene_id] = binding
        return str(source_path)

    def bind_motion_base(
        self,
        snapshot: delivery_envelope.FileSnapshot,
        path: Path,
        canvas_width: int,
        canvas_height: int,
        fps: int,
    ) -> dict[str, Any]:
        """Bind the private same-graph base visual used by independent QA."""
        binding = {
            "base_path": str(path),
            "base_sha256": snapshot.sha256,
            "canvas_width": canvas_width,
            "canvas_height": canvas_height,
            "fps": fps,
        }
        self.public["motion_input"] = {
            key: value for key, value in binding.items() if key != "base_path"
        }
        self.public["authority_hash"] = contract_registry.canonical_hash(
            {key: value for key, value in self.public.items() if key != "authority_hash"}
        )
        object.__setattr__(self, "snapshots", (*self.snapshots, snapshot))
        return binding


def _write_frozen_payload(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while freezing structured artifact")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sanitize_private_motion_receipts(
    staged_evidence: Path, staged_report: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Remove QA-only motion paths without hiding a receipt replacement."""
    staged_visual_payload = read_json(staged_evidence, {}) or {}
    report_payload = read_json(staged_report, {}) or {}
    embedded_visual = report_payload.get("visual_delivery")
    if not isinstance(embedded_visual, dict) or contract_registry.canonical_hash(
        embedded_visual
    ) != contract_registry.canonical_hash(staged_visual_payload):
        raise RuntimeError("QA visual receipt differs from staged visual evidence")
    if not isinstance(staged_visual_payload.get("raw_evidence"), dict):
        raise RuntimeError("authority-bound visual evidence has no private QA inputs")
    staged_visual_payload.pop("raw_evidence")
    report_payload["visual_delivery"] = staged_visual_payload
    atomic_write_json(staged_evidence, staged_visual_payload)
    atomic_write_json(staged_report, report_payload)
    return staged_visual_payload, report_payload


def adopt_fresh_motion_receipt(
    staged_evidence: Path,
    staged_report: Path,
    expected_staged_sha256: str,
) -> None:
    """CAS-replace renderer probe numbers with independently recomputed QA values."""
    if (
        staged_evidence.is_symlink()
        or not staged_evidence.is_file()
        or file_sha256(staged_evidence) != expected_staged_sha256
    ):
        raise RuntimeError("staged visual evidence changed during QA")
    if staged_report.is_symlink() or not staged_report.is_file():
        raise RuntimeError("staged QA report is not a regular file")
    supplied = read_json(staged_evidence, {}) or {}
    report = read_json(staged_report, {}) or {}
    fresh = report.get("visual_delivery")
    if not isinstance(fresh, dict):
        raise RuntimeError("QA report has no fresh visual delivery receipt")

    def stable(payload: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(payload)
        result.pop("motion_probes", None)
        raw = result.get("raw_evidence")
        if isinstance(raw, dict):
            raw.pop("motion_probes", None)
        return result

    if contract_registry.canonical_hash(stable(supplied)) != contract_registry.canonical_hash(
        stable(fresh)
    ):
        raise RuntimeError("fresh QA visual receipt changes stable renderer evidence")
    atomic_write_json(staged_evidence, fresh)


def freeze_visual_authority(
    project_dir: Path,
    state: dict[str, Any],
    manifest: dict[str, Any],
    clip: dict[str, Any] | None,
    stage: delivery_envelope.StagingAttempt,
) -> FrozenVisualAuthority:
    """Freeze canonical plan, layers, composed bytes, and licensed mosaic inputs."""
    layers, layers_snapshot = delivery_envelope.snapshot_owned_json(
        project_dir,
        "working/structured_layers.json",
        label="structured visual layers",
    )
    plan, plan_snapshot = delivery_envelope.snapshot_owned_json(
        project_dir,
        "working/visual_plan_v2.json",
        label="visual plan v2",
    )
    for name, payload in (("structured_layer", layers), ("visual_plan", plan)):
        errors = contract_registry.validate_artifact(name, payload)
        if errors:
            raise ValueError(f"{name} failed contract validation: " + "; ".join(errors))
    items = plan.get("items")
    revision = plan.get("revision")
    if not isinstance(items, list) or revision != contract_registry.canonical_hash(items):
        raise ValueError("visual plan revision does not match its frozen items")

    canvas = state.get("canvas") or {}
    target_width = int(canvas.get("width", 1080))
    render_scale = even(target_width) / max(target_width, 1)
    pack = style_pack_for_clip(state, clip)
    import structured_card_compositor

    artifact_index = structured_card_compositor.build_structured_artifacts(
        project_dir, state, layers, pack, render_scale
    )
    index_payload, index_snapshot = delivery_envelope.snapshot_owned_json(
        project_dir,
        structured_card_compositor.ARTIFACTS_REL.as_posix(),
        label="structured artifact index",
    )
    if index_payload != artifact_index:
        raise ValueError("structured artifact index changed after composition")
    key = structured_card_compositor.canvas_key(canvas, render_scale)
    frozen_dir = Path(stage) / "frozen_visual_artifacts"
    frozen_dir.mkdir(mode=0o700)
    artifact_by_layer: dict[str, Any] = {}
    snapshots: list[delivery_envelope.FileSnapshot] = [
        plan_snapshot,
        layers_snapshot,
        index_snapshot,
    ]
    for ordinal, item in enumerate(artifact_index.get("items", [])):
        if item.get("canvas") != key:
            continue
        relative = item.get("artifact_id")
        if not isinstance(relative, str):
            raise ValueError("structured artifact has no project-relative identity")
        snapshot = delivery_envelope.snapshot_project_file(
            project_dir,
            relative,
            label=f"structured artifact {item.get('layer_id')}",
            capture_bytes=True,
        )
        if snapshot.payload is None or snapshot.sha256 != item.get("artifact_hash"):
            raise ValueError("structured artifact bytes do not match the frozen index")
        frozen_path = frozen_dir / f"{ordinal:04d}-{snapshot.sha256}.png"
        _write_frozen_payload(frozen_path, snapshot.payload)
        frozen = dict(item)
        frozen["artifact_id"] = str(frozen_path)
        artifact_by_layer[str(item.get("layer_id") or "")] = frozen
        snapshots.append(snapshot)

    layer_by_id = {
        str(item.get("id")): item
        for item in layers.get("items", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    source_duration = float(manifest.get("source", {}).get("duration_s", 0.0))
    clip_start = float(clip.get("start", 0.0)) if clip is not None else 0.0
    clip_end = float(clip.get("end", source_duration)) if clip is not None else source_duration
    segments = effective_segments(state_segments(state, source_duration), clip_start, clip_end)
    breathing_intervals = a_roll_breathing_intervals(items, segments)
    authority_items: list[dict[str, Any]] = []
    mosaic_descriptors: list[dict[str, Any]] = []
    for plan_item in items:
        if not isinstance(plan_item, dict):
            raise ValueError("visual plan item must be an object")
        layer_id = plan_item.get("structured_layer_id")
        if not isinstance(layer_id, str) or not layer_id:
            continue
        layer = layer_by_id.get(layer_id)
        artifact = artifact_by_layer.get(layer_id)
        if layer is None or artifact is None:
            raise ValueError(f"visual plan layer has no frozen artifact: {layer_id}")
        windows = map_source_range_to_post_cut(
            segments, float(plan_item.get("start", 0.0)), float(plan_item.get("end", 0.0))
        )
        if len(windows) != 1:
            raise ValueError("frozen structured scene must map to exactly one final window")
        start, end = windows[0]
        evidence = card_visual_evidence(pack, layers, layer_id, render_scale, None)
        resolved = resolved_scene_evidence(plan_item, evidence, start, end)
        authority_items.append(
            {
                "id": str(plan_item.get("id") or ""),
                "start": round(start, 3),
                "end": round(end, 3),
                "kind": str(layer.get("type") or ""),
                "family": resolved.get("family"),
                "role": resolved.get("role"),
                "structured_layer_id": layer_id,
                "structured_layer_hash": artifact.get("structured_layer_hash"),
                "artifact_hash": artifact.get("artifact_hash"),
                "evidence_id": evidence.get("evidence_id"),
                "source_literal": evidence.get("source_literal"),
                "assets": evidence.get("assets"),
                "graphic_roi": resolved.get("graphic_roi"),
                "presenter_roi": resolved.get("presenter_roi"),
                "motion_window_start_sample": resolved.get("motion_window_start_sample"),
                "motion_window_end_sample": resolved.get("motion_window_end_sample"),
            }
        )
        if layer.get("type") == "mosaic":
            payload = layer.get("payload")
            mosaic_descriptors.extend(
                item for item in (payload.get("assets") if isinstance(payload, dict) else [])
                if isinstance(item, dict)
            )
    if mosaic_descriptors:
        registry, registry_snapshot = delivery_envelope.snapshot_owned_json(
            project_dir, "assets/provenance.json", label="mosaic provenance registry"
        )
        delivery_envelope.validate_mosaic_registry_snapshot(
            registry, mosaic_descriptors
        )
        snapshots.append(registry_snapshot)
        for descriptor in mosaic_descriptors:
            relative = descriptor.get("path")
            if not isinstance(relative, str):
                raise ValueError("mosaic descriptor path is invalid")
            snapshot = delivery_envelope.snapshot_project_file(
                project_dir, relative, label=f"mosaic asset {descriptor.get('asset_id')}"
            )
            if snapshot.sha256 != descriptor.get("sha256"):
                raise ValueError("mosaic asset changed after approved composition")
            snapshots.append(snapshot)

    public = {
        "schema_version": 1,
        "source": "frozen_visual_authority",
        "visual_plan_revision": revision,
        "visual_plan_sha256": plan_snapshot.sha256,
        "structured_layers_sha256": layers_snapshot.sha256,
        "artifact_index_sha256": index_snapshot.sha256,
        "a_roll_breathing_intervals": breathing_intervals,
        "items": authority_items,
    }
    public["authority_hash"] = contract_registry.canonical_hash(public)
    return FrozenVisualAuthority(
        public=public,
        layers=layers,
        plan=plan,
        artifacts=artifact_by_layer,
        snapshots=tuple(snapshots),
    )


def resolved_scene_evidence(
    plan_item: dict[str, Any],
    card_evidence: dict[str, Any],
    final_start: float,
    final_end: float,
) -> dict[str, Any]:
    """Freeze one planned scene in the final timeline domain.

    Semantic choices stay planner-owned.  This renderer seam only resolves
    the final sample window and the stage ROIs that it will use to draw the
    already-selected scene.
    """
    present = [field for field in SCENE_PLAN_FIELDS if field in plan_item]
    if not present:
        return {}
    missing = [field for field in SCENE_PLAN_FIELDS if field not in plan_item]
    if missing:
        raise ValueError(f"scene plan is missing fields: {missing}")
    if (
        isinstance(final_start, bool)
        or isinstance(final_end, bool)
        or not isinstance(final_start, (int, float))
        or not isinstance(final_end, (int, float))
        or not math.isfinite(float(final_start))
        or not math.isfinite(float(final_end))
        or final_start < 0
        or final_end <= final_start
    ):
        raise ValueError("resolved scene final window is invalid")
    if plan_item.get("role") == "section_title" and (
        plan_item.get("eligibility") != "eligible"
        or plan_item.get("eligibility_reason") is not None
        or plan_item.get("family") != "title_reveal"
        or plan_item.get("importance") != "high"
        or plan_item.get("major_graphic") is not True
        or plan_item.get("micro_silent") is not False
        or plan_item.get("stage") != "split_graphic_presenter"
        or plan_item.get("trigger_role") != "scene_transition"
    ):
        raise ValueError("section-title scene contract is inconsistent")
    if plan_item.get("role") == "opening_title" and (
        plan_item.get("eligibility") != "eligible"
        or plan_item.get("eligibility_reason") is not None
        or plan_item.get("family") != "title_reveal"
        or plan_item.get("importance") != "high"
        or plan_item.get("major_graphic") is not True
        or plan_item.get("micro_silent") is not False
        or plan_item.get("stage") != "full_screen_graphic"
        or plan_item.get("trigger_role") != "title_enter"
    ):
        raise ValueError("opening-title scene contract is inconsistent")
    if plan_item.get("role") == "metric_emphasis" and (
        plan_item.get("eligibility") != "eligible"
        or plan_item.get("eligibility_reason") is not None
        or plan_item.get("family") != "count_stat"
        or plan_item.get("importance") != "high"
        or plan_item.get("major_graphic") is not True
        or plan_item.get("micro_silent") is not False
        or plan_item.get("stage") != "split_graphic_presenter"
        or plan_item.get("trigger_role") != "count_complete"
    ):
        raise ValueError("metric-emphasis scene contract is inconsistent")
    if plan_item.get("role") == "list_explanation" and (
        plan_item.get("eligibility") != "eligible"
        or plan_item.get("eligibility_reason") is not None
        or plan_item.get("family") != "staggered_list"
        or plan_item.get("importance") != "medium"
        or plan_item.get("major_graphic") is not True
        or plan_item.get("micro_silent") is not False
        or plan_item.get("stage") != "split_graphic_presenter"
        or plan_item.get("trigger_role") != "row_reveal"
    ):
        raise ValueError("list-explanation scene contract is inconsistent")
    if plan_item.get("role") == "data_explanation" and (
        plan_item.get("eligibility") != "eligible"
        or plan_item.get("eligibility_reason") is not None
        or plan_item.get("family") != "analytics_dashboard"
        or plan_item.get("importance") != "high"
        or plan_item.get("major_graphic") is not True
        or plan_item.get("micro_silent") is not False
        or plan_item.get("stage") != "split_graphic_presenter"
        or plan_item.get("trigger_role") != "chart_complete"
    ):
        raise ValueError("data-explanation scene contract is inconsistent")
    if plan_item.get("role") == "prompt_command" and (
        plan_item.get("eligibility") != "eligible"
        or plan_item.get("eligibility_reason") is not None
        or plan_item.get("family") != "typed_prompt"
        or plan_item.get("importance") != "medium"
        or plan_item.get("major_graphic") is not True
        or plan_item.get("micro_silent") is not False
        or plan_item.get("stage") != "split_graphic_presenter"
        or plan_item.get("trigger_role") != "typing"
    ):
        raise ValueError("prompt-command scene contract is inconsistent")
    if plan_item.get("role") == "workflow_progress" and (
        plan_item.get("eligibility") != "eligible"
        or plan_item.get("eligibility_reason") is not None
        or plan_item.get("family") != "grid_progress"
        or plan_item.get("importance") != "medium"
        or plan_item.get("major_graphic") is not True
        or plan_item.get("micro_silent") is not False
        or plan_item.get("stage") != "split_graphic_presenter"
        or plan_item.get("trigger_role") != "grid_complete"
    ):
        raise ValueError("workflow-progress scene contract is inconsistent")
    if plan_item.get("role") == "asset_showcase" and (
        plan_item.get("eligibility") != "eligible"
        or plan_item.get("eligibility_reason") is not None
        or plan_item.get("family") != "asset_mosaic"
        or plan_item.get("importance") != "high"
        or plan_item.get("major_graphic") is not True
        or plan_item.get("micro_silent") is not False
        or plan_item.get("stage") != "split_graphic_presenter"
        or plan_item.get("trigger_role") != "scene_transition"
    ):
        raise ValueError("asset-showcase scene contract is inconsistent")

    motion = card_evidence.get("motion")
    if not isinstance(motion, dict):
        raise ValueError("resolved scene needs renderer motion evidence")
    duration = float(final_end) - float(final_start)
    requested = str(motion.get("requested") or "")
    if requested in {"slide-up", "slide-in", "pop", "pop-in"}:
        motion_duration = min(0.22, max(0.01, duration * 0.28))
    elif requested == "fade":
        motion_duration = min(0.18, max(0.01, duration * 0.22))
    else:
        # Content-native animations own their motion over the declared scene.
        motion_duration = duration
    motion_end = min(float(final_end), float(final_start) + motion_duration)

    if plan_item.get("stage") == "split_graphic_presenter":
        graphic_roi = {"x": 0.08, "y": 0.1, "width": 0.84, "height": 0.3}
        presenter_roi = {"x": 0.0, "y": 0.42, "width": 1.0, "height": 0.58}
    else:
        graphic_roi = {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}
        presenter_roi = None

    return {
        **{field: plan_item[field] for field in SCENE_PLAN_FIELDS},
        "motion_window_start_sample": sfx_delivery.seconds_to_samples(
            f"{float(final_start):.9f}"
        ),
        "motion_window_end_sample": sfx_delivery.seconds_to_samples(
            f"{motion_end:.9f}"
        ),
        "graphic_roi": graphic_roi,
        "presenter_roi": presenter_roi,
        "static_fallback": (
            motion.get("status") == "fallback" or motion.get("faithful") is not True
        ),
    }


def overlay_visual_evidence(
    overlay: dict[str, Any],
    render_scale: float,
    source: str = "editor_overlay",
) -> dict[str, Any]:
    """Describe a non-card visual after its timing was mapped to output time."""
    style = overlay.get("style") if isinstance(overlay.get("style"), dict) else {}
    requested = str(style.get("animation") or "")
    motion: dict[str, Any] | None = None
    if requested and requested != "none":
        faithful = requested in MOTION_ANIMATIONS
        motion = {
            "requested": requested,
            "delivered": requested if faithful else "none",
            "faithful": faithful,
            "status": "native" if faithful else "fallback",
        }
    kind = str(overlay.get("type") or "")
    font_size = None
    font_evidence_required = kind not in {"image", "gif", "video"}
    if font_evidence_required:
        raw_size = style.get("font_size", 52)
        if isinstance(raw_size, bool) or not isinstance(raw_size, (int, float)):
            raise ValueError("text overlay font_size must be a finite number")
        value = float(raw_size) * render_scale
        if not math.isfinite(value):
            raise ValueError("text overlay font_size must be a finite number")
        # Keep this identical to text_filter(): the evidence records the size
        # FFmpeg receives after output scaling and its emergency minimum.
        font_size = float(max(14, int(value)))
    return {
        "id": str(overlay.get("id") or ""),
        # SFX planning consumes final-domain timing.  Do not first throw away
        # sub-millisecond state precision in renderer evidence.
        "start": float(overlay.get("start", 0.0)),
        "end": float(overlay.get("end", 0.0)),
        "kind": kind,
        "component_id": None,
        "style_pack_id": None,
        "font_evidence_required": font_evidence_required,
        "minimum_primary_font_px": font_size,
        "source": source,
        "motion": motion,
    }


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


def placements_of(
    overlays: list[dict[str, Any]], width: int, height: int
) -> list[dict[str, Any]]:
    """What each overlay will occupy, in fractions of the canvas.

    Built in one pass over the finished list rather than recorded at each of
    the three places overlays are created, because those three have drifted
    from each other before and would again.

    Height is only known where a compositor measured it. An overlay drawn
    from a picture on disk is listed without one, which the checker reports
    as unmeasured rather than passing over in silence.
    """
    placements: list[dict[str, Any]] = []
    for overlay in overlays:
        if not overlay.get("visible", True):
            continue
        style = overlay.get("style") or {}
        drawn = overlay.get("drawn") or {}
        entry: dict[str, Any] = {
            "id": str(overlay.get("id") or ""),
            "kind": str(overlay.get("type") or "overlay"),
            "start": float(overlay.get("start", 0.0)),
            "end": float(overlay.get("end", 0.0)),
            "x": float(style.get("x", 50)) / 100.0,
            "y": float(style.get("y", 50)) / 100.0,
        }
        try:
            drawn_width = float(drawn.get("width") or 0)
            drawn_height = float(drawn.get("height") or 0)
        except (TypeError, ValueError):
            drawn_width = drawn_height = 0.0
        if drawn_width > 0 and drawn_height > 0 and width > 0 and height > 0:
            # The overlay is scaled to its style width; the height follows the
            # asset's own proportions, which is what ffmpeg's -2 computes.
            scaled_width = float(style.get("width", 0)) / 100.0
            if scaled_width > 0:
                drawn_scale = scaled_width * width / drawn_width
                # A caption's raster is wider than its text by a transparent
                # margin. Measuring the raster reports a caption whose words
                # sit inside a boundary as crossing it.
                try:
                    padding = max(0.0, float(drawn.get("padding") or 0)) * 2
                except (TypeError, ValueError):
                    padding = 0.0
                ink_width = max(1.0, drawn_width - padding)
                ink_height = max(1.0, drawn_height - padding)
                entry["width"] = ink_width * drawn_scale / width
                entry["height"] = ink_height * drawn_scale / height
        placements.append(entry)
    return placements


def captionized_overlays(
    overlays: list[dict[str, Any]],
    caption_plan: dict[str, Any],
    width: int,
    height: int = 0,
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
                # The raster carries a transparent margin for the stroke; the
                # ink is that much narrower, and the ink is what a viewer
                # sees covered or clipped.
                "drawn": {
                    "width": artifact["width"],
                    "height": artifact["height"],
                    "padding": artifact.get("padding", 0),
                },
                # The compositor's output is always type "image" — this is
                # the only trace left of whether it was a caption or an
                # emphasis block, which a safe-area clamp still needs to
                # tell from an unrelated card or asset overlay.
                "caption_kind": overlay.get("type"),
                "style": {
                    "width": max(5.0, min(100.0, artifact["width"] / max(width, 1) * 100.0)),
                    "x": float(style.get("x", 50)),
                    # The declared y positions the spoken line. A translation
                    # makes the raster taller below it; centring the whole
                    # block at y pushed the spoken line up into the picture,
                    # so the centre moves down by half the added height.
                    "y": float(style.get("y", 78)) + (
                        (artifact["height"] - artifact["spoken_height"]) / 2.0
                        / height * 100.0
                        if height > 0 and artifact.get("spoken_height")
                        else 0.0
                    ),
                    "animation": str(style.get("animation", "none")),
                },
            }
        )
    return converted


def constrain_caption_wrap_to_safe_area(
    overlays: list[dict[str, Any]], safe: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Narrow a caption's wrap width to what the platform actually leaves clear.

    A caption wraps to the director's own max_width, chosen for how a
    caption looks and unaware of any platform. Reaching past the platform's
    left or right margin is not a style choice once this frame is actually
    going there, so the tighter of the two wins.
    """
    if not isinstance(safe, dict) or not safe:
        return overlays
    try:
        left = float(safe.get("left", 0))
        safe_width = 100.0 - left - float(safe.get("right", 0))
    except (TypeError, ValueError):
        return overlays
    if safe_width <= 0:
        return overlays
    # The narrowed block must sit on the safe column's own centre. Where
    # the platform's left and right margins differ (TikTok: 8 and 14),
    # leaving x at the frame's centre still runs the block past the
    # tighter margin even though its width now fits between them.
    safe_center = left + safe_width / 2.0
    adjusted: list[dict[str, Any]] = []
    changed = False
    for overlay in overlays:
        style = overlay.get("style") if is_plain_caption(overlay) else None
        if not isinstance(style, dict):
            adjusted.append(overlay)
            continue
        try:
            current = float(style.get("max_width", 84))
        except (TypeError, ValueError):
            adjusted.append(overlay)
            continue
        if current <= safe_width:
            adjusted.append(overlay)
            continue
        changed = True
        moved = dict(overlay)
        moved["style"] = {**style, "max_width": safe_width, "x": safe_center}
        adjusted.append(moved)
    return adjusted if changed else overlays


def clamp_captions_into_safe_area(
    overlays: list[dict[str, Any]],
    safe: dict[str, Any] | None,
    width: int,
    height: int,
) -> list[dict[str, Any]]:
    """Move a caption's block clear of the platform's own reserved margin.

    A translation grows the block downward from the spoken line without
    knowing where the platform's own controls sit. This only ever moves a
    caption up, and only as far as needed; one that still cannot fit is left
    where it was, so visual_collision reports a real, remaining problem
    instead of one already hidden here.
    """
    if not isinstance(safe, dict) or not safe or width <= 0 or height <= 0:
        return overlays
    try:
        limit_bottom = 1.0 - float(safe.get("bottom", 0)) / 100.0
    except (TypeError, ValueError):
        return overlays
    placements = {
        placement["id"]: placement for placement in placements_of(overlays, width, height)
    }
    adjusted: list[dict[str, Any]] = []
    changed = False
    for overlay in overlays:
        overlay_id = str(overlay.get("id") or "")
        placement = placements.get(overlay_id)
        style = overlay.get("style")
        if (
            overlay.get("caption_kind") not in CAPTION_TYPES
            or placement is None
            or "height" not in placement
            or not isinstance(style, dict)
            or not isinstance(style.get("y"), (int, float))
        ):
            adjusted.append(overlay)
            continue
        overflow = (placement["y"] + placement["height"] / 2.0) - limit_bottom
        if overflow <= 0.0:
            adjusted.append(overlay)
            continue
        changed = True
        moved = dict(overlay)
        moved["style"] = {**style, "y": float(style["y"]) - overflow * 100.0}
        adjusted.append(moved)
    return adjusted if changed else overlays


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


def a_roll_breathing_intervals(
    plan_items: list[dict[str, Any]],
    segments: list[tuple[float, float]],
) -> list[dict[str, Any]]:
    """Resolve explicit A-roll breathing beats onto the final timeline."""
    intervals: list[dict[str, Any]] = []
    for item_index, item in enumerate(plan_items):
        if not isinstance(item, dict):
            raise ValueError(f"visual plan items[{item_index}] must be an object")
        if item.get("beat") != "a_roll_breathing":
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise ValueError("A-roll breathing beat must have a non-empty id")
        windows = map_source_range_to_post_cut(
            segments,
            float(item.get("start", 0.0)),
            float(item.get("end", 0.0)),
        )
        for window_index, (start, end) in enumerate(windows):
            interval_id = (
                item_id
                if len(windows) == 1
                else f"{item_id}-part-{window_index + 1}"
            )
            intervals.append(
                {
                    "id": interval_id,
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "role": "a_roll_breathing",
                    "major_graphic": False,
                }
            )
    return intervals


def style_pack_for_clip(
    state: dict[str, Any], clip: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve the render pack for one clip, or the explicit ``none`` fallback."""
    import structured_card_compositor

    selection = state.get("style_pack")
    if selection is None:
        return {"id": "none", "tokens": {}}
    if not isinstance(selection, dict):
        raise ValueError("style_pack must be an object")
    per_highlight = selection.get("per_highlight")
    if per_highlight is not None and not isinstance(per_highlight, dict):
        raise ValueError("style_pack.per_highlight must be an object")
    clip_id = str((clip or {}).get("id") or "")
    pack_id = (
        (per_highlight or {}).get(clip_id) if clip_id else None
    ) or selection.get("project_default")
    if not pack_id:
        return {"id": "none", "tokens": {}}
    return structured_card_compositor.load_style_pack(pack_id)


def resolve_active_highlight(
    state: dict[str, Any],
) -> dict[str, Any] | None:
    """Resolve the direct-render clip without guessing on malformed state."""
    active_id = state.get("active_highlight_id")
    if active_id is None:
        return None
    if not isinstance(active_id, str):
        raise ValueError("active_highlight_id must be a non-empty string")
    if not active_id.strip():
        raise ValueError("active_highlight_id must be a non-empty string")
    highlights = state.get("highlights")
    if not isinstance(highlights, list):
        raise ValueError("active_highlight_id must reference exactly one highlight")
    matches = [
        item
        for item in highlights
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and item.get("id")
        and item.get("id") == active_id
    ]
    if len(matches) != 1:
        if not matches:
            raise ValueError("active_highlight_id must reference exactly one highlight")
        raise ValueError("active_highlight_id references duplicate highlights")
    return matches[0]


def build_render_command(
    project_dir: Path,
    state: dict[str, Any],
    manifest: dict[str, Any],
    output: Path,
    quality: str,
    clip: dict[str, Any] | None = None,
    visual_source: Path | None = None,
    visual_evidence: dict[str, Any] | None = None,
    sfx_stem: Path | None = None,
    dialogue_priority_dialogue: Path | None = None,
    dialogue_priority_sfx: Path | None = None,
    visual_authority: FrozenVisualAuthority | None = None,
    motion_base_output: Path | None = None,
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
    evidence_items: list[dict[str, Any]] = []
    motion_graphics_for_probe: dict[str, dict[str, Any]] = {}
    expected_visual_beat_count = 0
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
        crop_x = "(iw-ow)/2"
        if not multi_segment and state.get("subject_tracking", True):
            crop_x = tracked_or_centered_crop_x(
                source, manifest, clip_start, clip_end, width, height
            )
        base_filter = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}:{crop_x}:(ih-oh)/2,setsar=1,setpts=PTS-STARTPTS"
        )

    command = [ffmpeg_path(), "-y"]
    if not multi_segment and clip_start > 0:
        command.extend(["-ss", f"{clip_start:.3f}"])
    command.extend(["-i", str(source)])
    visual_input_index: int | None = None
    if visual_source is not None:
        visual_input_index = 1
        command.extend(["-i", str(visual_source)])
    # Decided before any overlay is read, because it changes which of them
    # are drawn as well as which layer bundle is used.
    if visual_authority is not None:
        layers_bundle = visual_authority.layers
        visual_plan_v2 = visual_authority.plan
    else:
        from editor_server import load_layer_bundle

        layers_bundle, visual_plan_v2 = load_layer_bundle(project_dir)
        planned_cards = card_plan_bundle(project_dir)
        if planned_cards is not None:
            layers_bundle, visual_plan_v2 = planned_cards
    canonical_plan_adopted = bool(visual_plan_v2.get("items"))
    breathing_intervals = a_roll_breathing_intervals(
        visual_plan_v2.get("items", []), segments
    )
    overlays: list[dict[str, Any]] = []
    import caption_delivery

    delivery_items = caption_delivery.render_item_map(state)
    for source_overlay in state.get("overlays", []):
        if not isinstance(source_overlay, dict) or not source_overlay.get("visible", True):
            continue
        scoped_highlight = str(source_overlay.get("highlight_id") or "")
        clip_id = str(clip.get("id") or "") if clip is not None else ""
        if scoped_highlight and scoped_highlight != clip_id:
            continue
        if visual_source is not None and source_overlay.get("design_role"):
            # design-role cards are baked by the graphic package
            for window_start, window_end in map_source_range_to_post_cut(
                segments,
                float(source_overlay.get("start", 0.0)),
                float(source_overlay.get("end", 0.0)),
            ):
                baked = dict(source_overlay)
                baked["start"] = window_start
                baked["end"] = window_end
                expected_visual_beat_count += 1
                evidence_items.append(
                    overlay_visual_evidence(
                        baked, render_scale, source="graphic_package"
                    )
                )
            continue
        if canonical_plan_adopted and source_overlay.get("design_role"):
            # The card plan answers "which cards" on its own. These were put
            # here by the highlight-deck and legacy overlay paths, which the
            # plan replaces; drawing both puts two cards on screen at once —
            # the very thing the plan's contract refuses to hold.
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
            if overlay.get("type") == "caption" and delivery_items:
                source_id = str(source_overlay.get("caption_source_id") or "")
                key = (
                    source_id,
                    int(round(window_start * 1_000_000)),
                    int(round(window_end * 1_000_000)),
                )
                delivered = delivery_items.get(key)
                if delivered is None:
                    raise caption_delivery.CaptionDeliveryError(
                        "caption_binding_missing",
                        f"renderer instance {source_id}@{key[1]}-{key[2]}",
                    )
                overlay["id"] = delivered["caption_instance_id"]
                overlay["caption_instance_id"] = delivered["caption_instance_id"]
                overlay["translation"] = delivered["translated_text"]
            overlays.append(overlay)
            if overlay.get("type") not in {"caption", "emphasis"}:
                expected_visual_beat_count += 1
                evidence_items.append(
                    overlay_visual_evidence(overlay, render_scale)
                )
    overlays.sort(key=lambda item: (int(item.get("z_index", 0)), float(item.get("start", 0.0))))
    # A card plan, once a project has one, is the whole answer to "which
    # cards". Cards used to arrive down three unrelated paths; letting the
    # plan and one of those paths both contribute would restore exactly the
    # drift the plan exists to remove. The design-role overlays those other
    # paths left in the editor state are skipped above on the same flag.
    # Composing the cards and placing the plan's beats are separate jobs, and
    # nesting the second inside the first meant a plan of nothing but
    # pictures was skipped whole: no picture drawn, no error, a render that
    # reported success. Cards are composed only when there are cards; every
    # plan item is placed either way.
    artifact_by_layer: dict[str, Any] = {}
    resolved_pack = style_pack_for_clip(state, clip)
    if visual_authority is not None:
        artifact_by_layer = visual_authority.artifacts
    elif layers_bundle.get("items"):
        import structured_card_compositor

        if not structured_card_compositor.compositor_available():
            if quality == "final":
                raise ValueError(
                    "structured layers need the static card compositor, which is "
                    "unavailable on this host; final would silently lose content"
                )
        else:
            artifacts_index = structured_card_compositor.build_structured_artifacts(
                project_dir, state, layers_bundle, resolved_pack, render_scale,
            )
            key = structured_card_compositor.canvas_key(canvas, render_scale)
            artifact_by_layer = {
                item["layer_id"]: item
                for item in artifacts_index.get("items", [])
                if item.get("canvas") == key
            }
    dropped_beats: list[str] = []
    for plan_item in visual_plan_v2.get("items", []):
        layer_ref = plan_item.get("structured_layer_id")
        asset_ref = plan_item.get("selected_asset")
        windows = map_source_range_to_post_cut(
            segments,
            float(plan_item.get("start", 0.0)),
            float(plan_item.get("end", 0.0)),
        )
        placed_before = len(overlays)
        for window_start, window_end in windows:
            if layer_ref and layer_ref in artifact_by_layer:
                artifact = artifact_by_layer[layer_ref]
                if plan_item.get("stage") == "split_graphic_presenter":
                    card_y = 25.0
                else:
                    card_y = card_y_for_window(
                        source, plan_item, artifact, height,
                        caption_top_fraction(state),
                        # The band the platform keeps for its own controls; a
                        # card that clears the speaker can still land behind them.
                        float((platform_safe_area(state) or {}).get("top", 0) or 0)
                        / 100.0,
                    )
                # A content-animating preset gets the real thing when the
                # animator can deliver it: the same card, rebuilt as a
                # transparent clip whose digits count and whose rows arrive.
                animated_source = animated_card_overlay_source(
                    project_dir, resolved_pack, layers_bundle, layer_ref,
                    artifact, window_end - window_start,
                    int(canvas.get("fps", 30)),
                    render_scale,
                )
                card_evidence = card_visual_evidence(
                    resolved_pack,
                    layers_bundle,
                    str(layer_ref),
                    render_scale,
                    animated_source,
                )
                card_evidence["structured_layer_hash"] = artifact.get(
                    "structured_layer_hash"
                )
                card_evidence["structured_layer_id"] = str(layer_ref)
                card_evidence["artifact_hash"] = artifact.get("artifact_hash")
                card_evidence.update(
                    {
                        "id": str(plan_item.get("id") or ""),
                        "start": round(window_start, 3),
                        "end": round(window_end, 3),
                        "source": "structured_card",
                    }
                )
                card_evidence.update(
                    resolved_scene_evidence(
                        plan_item, card_evidence, window_start, window_end
                    )
                )
                card_overlay = {
                        "id": plan_item.get("id"),
                        "type": "video" if animated_source else "image",
                        "source": animated_source or artifact["artifact_id"],
                        "start": window_start,
                        "end": window_end,
                        "visible": True,
                        "z_index": 5,
                        "motion_major": True,
                        # The size it was composed at, kept so the finished
                        # frame can be checked for cards sitting on each
                        # other. ffmpeg derives the drawn height from the
                        # asset, so nothing downstream knows it otherwise.
                        "drawn": {
                            "width": artifact.get("width"),
                            "height": artifact.get("height"),
                        },
                        "style": {
                            # The card was composed at this canvas, so
                            # draw it at the size it was drawn. Forcing
                            # every card to one width re-stretched a
                            # card that had just been fitted to its
                            # text, and upscaled its glyph edges.
                            "width": max(
                                5.0,
                                min(
                                    100.0,
                                    float(artifact.get("width") or 0)
                                    / max(width, 1) * 100.0,
                                ),
                            ) if artifact.get("width") else 84.0,
                            "x": 50,
                            "y": card_y,
                            # The style pack says how this component
                            # should arrive; every card fading in
                            # regardless was the pack going unread. An
                            # animated card keeps its component motion inside
                            # the clip and adds the existing cross-hold pan so
                            # final pixels retain measurable motion after an
                            # early content entrance has completed.
                            "animation": "pan" if animated_source else
                            motion_for_layer(
                                resolved_pack, layers_bundle, layer_ref
                            ),
                        },
                    }
                if visual_authority is not None:
                    frozen_source = visual_authority.bind_motion_graphic(
                        project_dir,
                        str(plan_item.get("id") or ""),
                        artifact,
                        card_overlay,
                        animated_source,
                        width,
                        height,
                    )
                    card_overlay["source"] = frozen_source
                    authority_item = next(
                        item["motion_attribution"]
                        for item in visual_authority.public["items"]
                        if item.get("id") == str(plan_item.get("id") or "")
                    )
                    if not isinstance(authority_item, dict):
                        raise ValueError("motion attribution binding is invalid")
                    motion_graphics_for_probe[str(plan_item.get("id") or "")] = dict(
                        visual_authority.motion_graphics[str(plan_item.get("id") or "")]
                    )
                else:
                    legacy_source = Path(
                        str(animated_source or artifact["artifact_id"])
                    )
                    if not legacy_source.is_absolute():
                        legacy_source = project_dir / legacy_source
                    motion_graphics_for_probe[str(plan_item.get("id") or "")] = {
                        "artifact_sha256": str(artifact.get("artifact_hash") or ""),
                        "source_path": str(legacy_source.resolve()),
                        "source_sha256": file_sha256(legacy_source),
                        "source_kind": "video" if animated_source else "image",
                        "canvas_width": width,
                        "canvas_height": height,
                        "source_start_sample": sfx_delivery.seconds_to_samples(
                            window_start
                        ),
                        "source_end_sample": sfx_delivery.seconds_to_samples(
                            window_end
                        ),
                        "placement": {
                            "width_percent": float(card_overlay["style"]["width"]),
                            "x_percent": float(card_overlay["style"]["x"]),
                            "y_percent": float(card_overlay["style"]["y"]),
                            "animation": str(card_overlay["style"]["animation"]),
                        },
                    }
                expected_visual_beat_count += 1
                evidence_items.append(card_evidence)
                overlays.append(card_overlay)
            elif asset_ref:
                asset_overlay = {
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
                overlays.append(asset_overlay)
                expected_visual_beat_count += 1
                evidence_items.append(
                    overlay_visual_evidence(
                        asset_overlay, render_scale, source="planned_asset"
                    )
                )

        # A beat whose window fell inside removed material legitimately
        # draws nothing. A beat with a window that still drew nothing was
        # dropped, and a dropped beat looks exactly like a plan that never
        # asked for it — which is how a picture went missing twice tonight
        # from a render that reported success.
        # Only a beat that asked for something can have lost it. A beat naming
        # neither a card nor an asset is the director saying "keep the picture
        # as it is", which is most of them; reading that as a dropped visual
        # made every plan with a plain beat in it fail to render.
        if windows and (layer_ref or asset_ref) and len(overlays) == placed_before:
            dropped_beats.append(
                f"{plan_item.get('beat', '?')} at "
                f"{float(plan_item.get('start', 0.0)):.2f}s ("
                + (
                    f"no composed card for layer {layer_ref}"
                    if layer_ref
                    else f"asset {asset_ref} was not drawn"
                )
                + ")"
            )
    if dropped_beats:
        raise ValueError(
            "the plan asked for visuals that did not reach the frame: "
            + "; ".join(dropped_beats[:5])
        )

    safe_area = platform_safe_area(state)
    if any(is_plain_caption(overlay) for overlay in overlays):
        import caption_compositor

        if caption_compositor.compositor_available():
            overlays = constrain_caption_wrap_to_safe_area(overlays, safe_area)
            render_caption_state = dict(state)
            render_caption_state["overlays"] = overlays
            caption_plan = caption_compositor.build_render_plan(
                project_dir, render_caption_state, render_scale
            )
            disallowed = caption_plan.get("receipt", {}).get("disallowed_fallbacks") or []
            if disallowed and quality == "final":
                raise ValueError(
                    "captions use unsanctioned system font fallbacks "
                    f"({', '.join(disallowed)}); add the glyph coverage to the "
                    "project font or mark the caption for review"
                )
            overlays = captionized_overlays(overlays, caption_plan, width, height)
            overlays = clamp_captions_into_safe_area(overlays, safe_area, width, height)

    # Placement already tries to avoid the speaker. Nothing looked at the
    # result: two cards could hold the same moment, a card could sit on the
    # caption, and either renders without complaint. Now the frame is
    # inspected before it is drawn, and what could not be inspected is named.
    import visual_collision

    placements = placements_of(overlays, width, height)
    collision_review = visual_collision.review(placements, safe_area)
    atomic_write_json(project_dir / "working/overlay_placements.json", {
        "schema_version": 1,
        "canvas": {"width": width, "height": height},
        "placements": placements,
        "review": collision_review,
    })
    print(
        json.dumps({"visual_review": {
            key: collision_review[key]
            for key in ("checked", "unmeasured", "collisions", "off_frame", "safe_area")
        }}, ensure_ascii=False),
        file=sys.stderr,
    )
    # Collisions and off-frame overlays are not gated on final. `cut` — the
    # one command this is normally driven by — renders preview, and two
    # cards on top of each other is exactly as wrong there. Both shipped
    # projects report none, so nothing that works today starts failing.
    # The platform's own reserved margin is only a defect once this frame
    # is actually going to that platform, which is what quality == "final"
    # means here.
    problems = visual_collision.blocking(
        collision_review, block_safe_area=quality == "final"
    )
    if problems:
        raise ValueError(
            "the frame has overlays sitting on each other or running off it: "
            + "; ".join(problems[:5])
        )

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

    sfx_input_index: int | None = None
    dialogue_priority_paths = (
        dialogue_priority_dialogue,
        dialogue_priority_sfx,
    )
    if any(path is not None for path in dialogue_priority_paths) and not all(
        path is not None for path in dialogue_priority_paths
    ):
        raise ValueError("dialogue-priority evidence paths must be all-or-none")
    if any(path is not None for path in dialogue_priority_paths) and sfx_stem is None:
        raise ValueError("dialogue-priority evidence requires an SFX stem")
    evidence_mode = all(path is not None for path in dialogue_priority_paths)
    if sfx_stem is not None:
        if not sfx_stem.is_file():
            raise ValueError("staged SFX stem is missing")
        sfx_input_index = len(asset_inputs) + (2 if visual_input_index is not None else 1)
        command.extend(["-i", str(sfx_stem)])

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
    ordered_overlays = overlays
    first_major_index: int | None = None
    if motion_base_output is not None:
        non_major = [item for item in overlays if item.get("motion_major") is not True]
        major = [item for item in overlays if item.get("motion_major") is True]
        if not major:
            raise ValueError("motion base sidecar requires a structured major graphic")
        ordered_overlays = [*non_major, *major]
        first_major_index = len(non_major) + 1
    motion_base_label: str | None = None
    for index, overlay in enumerate(ordered_overlays, start=1):
        if first_major_index == index:
            motion_base_label = "motion_base_visual"
            candidate_label = f"{current}_with_major"
            filters.append(
                f"[{current}]split=2[{candidate_label}][{motion_base_label}]"
            )
            current = candidate_label
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
    if sfx_stem is not None:
        if multi_segment:
            raise ValueError("Phase 0d SFX only supports single-cut timelines")
        if not has_audio_stream or sfx_input_index is None:
            raise ValueError("kinetic SFX delivery requires dialogue audio")
        filters.append("[0:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                       f"atrim=0:{duration:.6f},asetpts=PTS-STARTPTS[adialogue]")
        filters.append(f"[{sfx_input_index}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,asetpts=PTS-STARTPTS[asfx]")
        filters.append(
            "[adialogue]asplit=3[adialogue_mix][adialogue_key][adialogue_evidence_raw]"
            if evidence_mode else
            "[adialogue]asplit=2[adialogue_mix][adialogue_key]"
        )
        filters.append(
            "[asfx][adialogue_key]sidechaincompress="
            "threshold=0.2:ratio=1.2:attack=5:release=250:makeup=1:"
            "link=maximum:detection=rms[asfx_ducked]"
        )
        if evidence_mode:
            evidence_sample_count = sfx_delivery.seconds_to_samples(f"{duration:.3f}")
            filters.append(
                f"[adialogue_evidence_raw]apad=whole_len={evidence_sample_count},"
                f"atrim=end_sample={evidence_sample_count}[adialogue_evidence]"
            )
            filters.append(
                f"[asfx_ducked]apad=whole_len={evidence_sample_count},"
                f"atrim=end_sample={evidence_sample_count}[asfx_ducked_bounded]"
            )
            filters.append(
                "[asfx_ducked_bounded]asplit=2"
                "[asfx_ducked_mix][asfx_ducked_evidence]"
            )
        filters.append(
            "[adialogue_mix]"
            + ("[asfx_ducked_mix]" if evidence_mode else "[asfx_ducked]")
            + "amix=inputs=2:normalize=0,"
            "loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[aout]"
        )
        command.extend(["-filter_complex", ";".join(filters)])
        if evidence_mode:
            for label, evidence_path in (
                ("adialogue_evidence", dialogue_priority_dialogue),
                ("asfx_ducked_evidence", dialogue_priority_sfx),
            ):
                assert evidence_path is not None
                evidence_path.parent.mkdir(parents=True, exist_ok=True)
                command.extend([
                    "-map", f"[{label}]", "-c:a", "pcm_s24le", "-ar", "48000",
                    "-ac", "2", "-f", "wav", str(evidence_path),
                ])
        command.extend([
            "-map", f"[{current}]", "-map", "[aout]", "-t", f"{duration:.3f}"
        ])
    elif multi_segment:
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
    if motion_base_output is not None:
        if motion_base_label is None:
            raise ValueError("motion base sidecar was not connected to the render graph")
        motion_base_output.parent.mkdir(parents=True, exist_ok=True)
        command.extend(
            [
                "-map",
                f"[{motion_base_label}]",
                "-an",
                "-t",
                f"{duration:.3f}",
                "-r",
                str(int(canvas.get("fps", 30))),
                "-c:v",
                "ffv1",
                "-pix_fmt",
                "yuv420p",
                str(motion_base_output),
            ]
        )
    if visual_evidence is not None:
        director = DIRECTOR_PRESETS.get(str(state.get("director_style") or ""), {})
        motion_intensity = (
            str(director.get("motion_intensity") or "low")
            if isinstance(director, dict)
            else "low"
        )
        visual_evidence.clear()
        visual_evidence.update(
            {
                "schema_version": 1,
                "source": "renderer_evidence_raw",
                "duration_s": round(duration, 3),
                "motion_intensity": motion_intensity,
                "expected_visual_beat_count": expected_visual_beat_count,
                "visual_beat_count": len(evidence_items),
                "a_roll_breathing_intervals": breathing_intervals,
                "items": evidence_items,
            }
        )
        if motion_graphics_for_probe:
            visual_evidence["frozen_graphics"] = motion_graphics_for_probe
            visual_evidence["motion_attribution"] = {
                scene_id: {
                    key: value for key, value in binding.items() if key != "source_path"
                }
                for scene_id, binding in motion_graphics_for_probe.items()
            }
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
    *,
    defer_delivery_handoff: bool = False,
) -> delivery_envelope.DeferredPublication | None:
    direct_final = quality == "final" and snapshot_path is None and variant_id is None
    if defer_delivery_handoff and not direct_final:
        raise ValueError("deferred delivery handoff requires a direct final render")
    render_id: str | None = None
    if snapshot_path is None:
        manifest = read_json(project_dir / "project.json", {}) or {}
        state = read_json(project_dir / "working/editor_state.json", {}) or {}
        if state.get("schema_version") == 1:
            raise SystemExit(
                "editor_state schema_version 1 must be migrated first; open the "
                "editor page once to run the v1→v2 migration"
            )
        clip = None
        if not variant_id:
            clip = resolve_active_highlight(state)
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
            if quality == "final":
                render_id = f"variant-{variant_id}"
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
            render_id = direct_final_render_id(state, output)
    else:
        manifest, state, clip = load_render_snapshot(project_dir, snapshot_path, quality)
        snapshot_payload = read_json(snapshot_path, {}) or {}
        render_id = str(snapshot_payload.get("render_id") or "") or None
    direct_stage: delivery_envelope.StagingAttempt | None = None
    caption_v2_artifact: dict[str, Any] | None = None
    staged_sfx: tuple[Path, Path, Path] | None = None
    sfx_bindings: tuple[str, str] | None = None
    dialogue_priority_paths: tuple[Path, Path] | None = None
    visual_authority: FrozenVisualAuthority | None = None
    motion_base_output: Path | None = None
    temporary: Path | None = None
    if direct_final:
        if render_id is None:
            raise RuntimeError("direct final render has no delivery identity")
        # Required caption delivery is reloaded and matched against the live
        # transcript/timeline before begin_staging creates any private or
        # public delivery state.
        import caption_delivery

        caption_v2_artifact, state = caption_delivery.validate_for_render(
            project_dir, state, manifest
        )
        if caption_v2_artifact is not None:
            import caption_compositor

            if not caption_compositor.compositor_available():
                raise caption_delivery.CaptionDeliveryError(
                    "caption_binding_missing",
                    "required translated captions need the caption compositor",
                )
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.parent / f".{output.stem}.{uuid.uuid4().hex}.part.mp4"
    published = False
    publication_attempted = False
    deferred_publication: delivery_envelope.DeferredPublication | None = None
    try:
        if direct_final:
            if render_id is None:
                raise RuntimeError("direct final render has no delivery identity")
            direct_stage = delivery_envelope.begin_staging(
                project_dir,
                render_id,
                expected_output=output,
            )
            if state.get("director_style") == "kinetic-explainer":
                visual_authority = freeze_visual_authority(
                    project_dir, state, manifest, clip, direct_stage
                )
                motion_base_output = direct_stage / "motion_base_visual.mkv"
            temporary = direct_stage / delivery_envelope.STAGE_FILENAMES["output"]
        if temporary is None:
            raise RuntimeError("render output staging was not initialized")
        visual_source: Path | None = None
        if clip is not None and state.get("visual_quality_mode") == "designed":
            quality_errors = visual_quality_errors(state, manifest, clip)
            if quality_errors:
                raise ValueError("visual-quality contract failed: " + "; ".join(quality_errors))
            if visual_authority is not None:
                scene_plan = visual_authority.plan
            else:
                from editor_server import load_layer_bundle

                _scene_layers, scene_plan = load_layer_bundle(project_dir)
            canonical_scene_plan = bool(scene_plan.get("items"))
            roles = {
                str(item.get("design_role"))
                for item in overlays_for_clip(state, clip)
                if item.get("design_role")
            }
            if not canonical_scene_plan and set(DESIGN_ROLES).issubset(roles):
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
        raw_visual_evidence: dict[str, Any] | None = (
            {} if quality == "final" and render_id is not None else None
        )
        command = build_render_command(
            project_dir,
            state,
            manifest,
            temporary,
            quality,
            clip,
            visual_source,
            visual_evidence=raw_visual_evidence,
            visual_authority=visual_authority,
            motion_base_output=motion_base_output,
        )
        if direct_final and state.get("director_style") == "kinetic-explainer":
            if direct_stage is None or render_id is None or raw_visual_evidence is None:
                raise RuntimeError("kinetic SFX staging lacks direct delivery identity")
            staged_sfx = stage_phase0d_sfx(
                project_dir, state, render_id, direct_stage, raw_visual_evidence
            )
            dialogue_priority_paths = (
                direct_stage / "dialogue_priority_dialogue.wav",
                direct_stage / "dialogue_priority_sfx.wav",
            )
            sfx_bindings = (
                editor_state_revision(state),
                editor_base_state_revision(state),
                sfx_delivery.effective_cut_map_sha256(project_dir, state),
            )
            command = build_render_command(
                project_dir, state, manifest, temporary, quality, clip, visual_source,
                visual_evidence=raw_visual_evidence, sfx_stem=staged_sfx[2],
                dialogue_priority_dialogue=dialogue_priority_paths[0],
                dialogue_priority_sfx=dialogue_priority_paths[1],
                visual_authority=visual_authority,
                motion_base_output=motion_base_output,
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
        if raw_visual_evidence is not None and render_id is not None:
            if visual_authority is not None:
                visual_authority.revalidate()
                if motion_base_output is None:
                    raise RuntimeError("frozen visual authority has no motion base output")
                base_relative = motion_base_output.relative_to(project_dir).as_posix()
                base_snapshot = delivery_envelope.snapshot_project_file(
                    project_dir,
                    base_relative,
                    label="private motion base visual",
                )
                canvas = state.get("canvas") or {}
                raw_visual_evidence["motion_input"] = visual_authority.bind_motion_base(
                    base_snapshot,
                    motion_base_output,
                    even(float(canvas.get("width", 1080))),
                    even(float(canvas.get("height", 1920))),
                    int(canvas.get("fps", 30)),
                )
                import visual_motion_probe

                motion_probes = visual_motion_probe.measure_declared_motion(
                    temporary,
                    raw_visual_evidence,
                )
                raw_visual_evidence["motion_probes"] = motion_probes
            evidence_output = (
                direct_stage / delivery_envelope.STAGE_FILENAMES["visual_evidence"]
                if direct_stage is not None
                else rendered_visual_evidence_path(project_dir, render_id)
            )
            atomic_write_json(
                evidence_output,
                rendered_visual_quality_report(
                    raw_visual_evidence,
                    visual_authority.public if visual_authority is not None else None,
                ),
            )
        if variant_id and quality == "final":
            # QA runs on the temporary output; only a passing QA publishes
            # the file + receipt together (no receipt-less final on disk).
            if render_id is None:
                raise RuntimeError("variant final render has no visual evidence identity")
            receipt = qa_variant_output(
                project_dir,
                temporary,
                variant_id,
                rendered_visual_evidence_path(project_dir, render_id),
                state,
            )
            os.replace(temporary, output)
            finalize_variant_delivery_receipt(project_dir, receipt, output, variant_id)
        elif direct_final:
            if render_id is None or direct_stage is None:
                raise RuntimeError("direct final render has no visual evidence identity")
            staged_evidence = direct_stage / delivery_envelope.STAGE_FILENAMES["visual_evidence"]
            pre_qa_visual_sha256 = file_sha256(staged_evidence)
            if staged_sfx is not None:
                if sfx_bindings is None:
                    raise RuntimeError("kinetic SFX has no freshness bindings")
                current_sfx_state, timeline_revision, cut_hash = fresh_sfx_bindings(
                    project_dir, *sfx_bindings,
                )
                expected_studio_edits_hash = audio_event_edits_hash(
                    current_sfx_state.get("audio_event_edits")
                    if isinstance(current_sfx_state.get("audio_event_edits"), dict)
                    else None
                )
            else:
                timeline_revision = cut_hash = None
                expected_studio_edits_hash = None
            qa_direct_final_output(
                project_dir,
                temporary,
                render_id,
                staged_evidence,
                state,
                report_path=direct_stage / delivery_envelope.STAGE_FILENAMES["qa_report"],
                contact_path=direct_stage / delivery_envelope.STAGE_FILENAMES["contact_sheet"],
                audio_event_plan=staged_sfx[0] if staged_sfx else None,
                audio_catalog=staged_sfx[1] if staged_sfx else None,
                sfx_stem=staged_sfx[2] if staged_sfx else None,
                dialogue_priority_dialogue=(
                    dialogue_priority_paths[0] if dialogue_priority_paths else None
                ),
                dialogue_priority_sfx=(
                    dialogue_priority_paths[1] if dialogue_priority_paths else None
                ),
                expected_timeline_revision=timeline_revision,
                expected_cut_map_sha256=cut_hash,
                expected_studio_edits_sha256=expected_studio_edits_hash,
            )
            if visual_authority is not None:
                visual_authority.revalidate()
            staged_report = direct_stage / delivery_envelope.STAGE_FILENAMES["qa_report"]
            if visual_authority is not None:
                adopt_fresh_motion_receipt(
                    staged_evidence,
                    staged_report,
                    pre_qa_visual_sha256,
                )
                _staged_visual_payload, report_payload = (
                    sanitize_private_motion_receipts(staged_evidence, staged_report)
                )
            else:
                report_payload = read_json(staged_report, {}) or {}
            report_payload["video"] = str(output.expanduser().resolve())
            atomic_write_json(staged_report, report_payload)
            staged_sources = {
                "output": temporary,
                "qa_report": staged_report,
                "contact_sheet": direct_stage / delivery_envelope.STAGE_FILENAMES["contact_sheet"],
                "visual_evidence": staged_evidence,
                "motion_evidence": staged_evidence,
            }
            if staged_sfx is not None:
                if sfx_bindings is None:
                    raise RuntimeError("kinetic SFX has no freshness bindings")
                current_sfx_state, timeline_revision, cut_hash = fresh_sfx_bindings(
                    project_dir, *sfx_bindings,
                )
                expected_studio_edits_hash = audio_event_edits_hash(
                    current_sfx_state.get("audio_event_edits")
                    if isinstance(current_sfx_state.get("audio_event_edits"), dict)
                    else None
                )
                verification = sfx_delivery.verify_delivery(
                    staged_sfx[0], staged_sfx[1], staged_sfx[2],
                    read_json(staged_evidence, {}) or {},
                    expected_timeline_revision=timeline_revision,
                    expected_cut_map_sha256=cut_hash,
                    candidate_path=temporary,
                    dialogue_priority_dialogue_path=dialogue_priority_paths[0],
                    dialogue_priority_sfx_path=dialogue_priority_paths[1],
                    expected_studio_edits_sha256=expected_studio_edits_hash,
                )
                if verification.get("source") != "independent_sfx_evidence" or verification.get("status") != "pass":
                    raise RuntimeError("staged SFX verification did not pass before publication")
                assert_sfx_candidate_binding(verification, report_payload, temporary)
                staged_sources.update({
                    "audio_event_plan": staged_sfx[0],
                    "audio_catalog": staged_sfx[1],
                    "sfx_stem": staged_sfx[2],
                })
            if caption_v2_artifact is not None:
                canonical_caption = project_dir / caption_delivery.CAPTION_REL
                expected_caption_sha = str(
                    state.get("_caption_delivery_v2", {}).get("artifact_sha256") or ""
                )
                if (
                    canonical_caption.is_symlink()
                    or not canonical_caption.is_file()
                    or file_sha256(canonical_caption) != expected_caption_sha
                ):
                    raise caption_delivery.CaptionDeliveryError(
                        "caption_binding_missing", "caption artifact changed during render"
                    )
                staged_caption = direct_stage / delivery_envelope.STAGE_FILENAMES["caption_v2"]
                shutil.copyfile(canonical_caption, staged_caption)
                try:
                    import contract_registry

                    staged_caption_bytes = staged_caption.read_bytes()
                    staged_caption_payload = contract_registry.load_artifact_text(
                        staged_caption_bytes.decode("utf-8")
                    )
                except (OSError, UnicodeDecodeError, ValueError) as exc:
                    raise caption_delivery.CaptionDeliveryError(
                        "caption_binding_missing", "staged caption artifact is invalid"
                    ) from exc
                if (
                    staged_caption.is_symlink()
                    or hashlib.sha256(staged_caption_bytes).hexdigest()
                    != expected_caption_sha
                    or staged_caption_payload != caption_v2_artifact
                ):
                    raise caption_delivery.CaptionDeliveryError(
                        "caption_binding_missing",
                        "staged caption artifact differs from approved bytes",
                    )
                staged_sources["caption_v2"] = staged_caption
            prepared = delivery_envelope.build_prepared_envelope(
                project_dir,
                render_id,
                output,
                state,
                staged_sources,
                renderer_script=Path(__file__).resolve(),
                ffmpeg_executable=Path(ffmpeg_path()).expanduser().resolve(),
                visual_authority=(
                    visual_authority.public if visual_authority is not None else None
                ),
            )
            delivery_envelope.write_prepared_envelope(direct_stage, prepared)
            # The prepared envelope is persisted but still private. Re-read the
            # live bindings once more in the final publication window; neither
            # the plan nor the just-written envelope can attest to current
            # editor/cut state. Host/power-loss ordering is outside this contract.
            if staged_sfx is not None:
                if sfx_bindings is None:
                    raise RuntimeError("kinetic SFX has no freshness bindings")
                current_sfx_state, timeline_revision, cut_hash = fresh_sfx_bindings(
                    project_dir, *sfx_bindings,
                )
                expected_studio_edits_hash = audio_event_edits_hash(
                    current_sfx_state.get("audio_event_edits")
                    if isinstance(current_sfx_state.get("audio_event_edits"), dict)
                    else None
                )
                verification = sfx_delivery.verify_delivery(
                    staged_sfx[0], staged_sfx[1], staged_sfx[2],
                    read_json(staged_evidence, {}) or {},
                    expected_timeline_revision=timeline_revision,
                    expected_cut_map_sha256=cut_hash,
                    candidate_path=temporary,
                    dialogue_priority_dialogue_path=dialogue_priority_paths[0],
                    dialogue_priority_sfx_path=dialogue_priority_paths[1],
                    expected_studio_edits_sha256=expected_studio_edits_hash,
                )
                if verification.get("source") != "independent_sfx_evidence" or verification.get("status") != "pass":
                    raise RuntimeError("staged SFX verification did not pass before publication")
                assert_sfx_candidate_binding(verification, report_payload, temporary)
                # Verification can be expensive.  Re-read the independent
                # editor authority after it completes so a state/edit change
                # during QA cannot ride the already-passing receipt into the
                # publication call.
                final_sfx_state, final_timeline_revision, final_cut_hash = (
                    fresh_sfx_bindings(project_dir, *sfx_bindings)
                )
                if (
                    final_timeline_revision != timeline_revision
                    or final_cut_hash != cut_hash
                    or audio_event_edits_hash(
                        final_sfx_state.get("audio_event_edits")
                        if isinstance(final_sfx_state.get("audio_event_edits"), dict)
                        else None
                    )
                    != expected_studio_edits_hash
                ):
                    raise RuntimeError("Studio SFX authority changed after final verification")
            publication_attempted = True
            publication = delivery_envelope.publish_direct_delivery(
                project_dir,
                direct_stage,
                staged_sources=staged_sources,
                expected_output=output,
                defer_commit=defer_delivery_handoff,
                revalidate_authority=(
                    visual_authority.revalidate if visual_authority is not None else None
                ),
            )
            if defer_delivery_handoff:
                if not isinstance(publication, delivery_envelope.DeferredPublication):
                    raise RuntimeError("renderer did not retain deferred publication authority")
                deferred_publication = publication
            published = True
        else:
            os.replace(temporary, output)
    finally:
        if publication_attempted:
            if deferred_publication is None and temporary is not None:
                temporary.unlink(missing_ok=True)
        else:
            primary_error = sys.exc_info()[1]
            cleanup_errors: list[tuple[str, Exception]] = []
            try:
                if deferred_publication is None and temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError as exc:
                        cleanup_errors.append(("temporary output cleanup", exc))
            finally:
                if (
                    direct_final
                    and render_id is not None
                    and direct_stage is not None
                    and not published
                ):
                    try:
                        delivery_envelope.discard_staging(
                            project_dir,
                            render_id,
                            authority=direct_stage,
                        )
                    except Exception as exc:
                        cleanup_errors.append(("staging discard", exc))
            if cleanup_errors:
                if primary_error is not None:
                    add_note = getattr(primary_error, "add_note", None)
                    if add_note is not None:
                        for label, cleanup_error in cleanup_errors:
                            add_note(f"{label} also failed: {cleanup_error}")
                else:
                    label, cleanup_error = cleanup_errors[-1]
                    add_note = getattr(cleanup_error, "add_note", None)
                    if add_note is not None:
                        for other_label, other_error in cleanup_errors[:-1]:
                            add_note(f"{other_label} also failed: {other_error}")
                        add_note(f"render cleanup failed during {label}")
                    raise cleanup_error
    return deferred_publication


def direct_final_render_id(state: dict[str, Any], output: Path) -> str:
    """Deterministic project-local identity for one direct final destination."""
    material = (
        editor_state_revision(state)
        + "\0"
        + str(output.expanduser().resolve())
    ).encode("utf-8")
    return "direct-final-" + hashlib.sha256(material).hexdigest()[:20]


def stage_phase0d_sfx(
    project_dir: Path,
    state: dict[str, Any],
    render_id: str,
    stage: delivery_envelope.StagingAttempt,
    visual_evidence: dict[str, Any],
) -> tuple[Path, Path, Path]:
    """Stage core-owned multi-event artifacts against live final bindings."""
    timeline_revision = editor_base_state_revision(state)
    cut_hash = sfx_delivery.effective_cut_map_sha256(project_dir, state)
    staged = sfx_delivery.stage_multi_event_delivery(
        Path(stage), visual_evidence, timeline_revision, cut_hash,
    )
    if not isinstance(staged, tuple) or len(staged) != 3:
        raise ValueError("core SFX staging returned invalid artifact bindings")
    plan_path, catalog_path, stem = (Path(value) for value in staged)
    expected = (
        stage / delivery_envelope.STAGE_FILENAMES["audio_event_plan"],
        stage / delivery_envelope.STAGE_FILENAMES["audio_catalog"],
        stage / delivery_envelope.STAGE_FILENAMES["sfx_stem"],
    )
    if (plan_path, catalog_path, stem) != expected or not all(path.is_file() for path in expected):
        raise ValueError("core SFX staging did not produce canonical private artifacts")
    edits = state.get("audio_event_edits")
    if isinstance(edits, dict):
        _source_render_id, source_plan, _source_hash = resolve_audio_event_source(
            project_dir, state
        )
        base_plan = read_json(plan_path, None)
        if not isinstance(base_plan, dict):
            raise ValueError("core SFX base plan is unreadable")

        def planning_authority(plan: dict[str, Any]) -> dict[str, Any]:
            excluded = {
                "timeline_revision",
                "sfx_stem_sha256",
                "sfx_stem_decoded_pcm_sha256",
                "studio_edits",
                "studio_edits_sha256",
            }
            return {key: value for key, value in plan.items() if key not in excluded}

        if (
            sfx_delivery._canonical_hash(planning_authority(base_plan))
            != sfx_delivery._canonical_hash(planning_authority(source_plan))
        ):
            raise ValueError("current renderer audio planning authority differs from Studio source")
        resolved = resolve_studio_audio_plan(base_plan, edits)
        asset_paths = {
            asset_id: Path(stage) / sfx_delivery.STARTER_ASSET_FILENAMES[asset_id]
            for asset_id in sfx_delivery.STARTER_ASSET_IDS
        }
        decoded = sfx_delivery._write_multi_event_stem(
            stem,
            total_samples=resolved["sfx_stem_sample_count"],
            events=resolved["events"],
            asset_paths=asset_paths,
        )
        stem_bytes = stem.read_bytes()
        resolved["sfx_stem_sha256"] = sfx_delivery.sha256_bytes(stem_bytes)
        resolved["sfx_stem_decoded_pcm_sha256"] = sfx_delivery.sha256_bytes(decoded.pcm)
        plan_errors = contract_registry.validate_artifact("audio_event_plan", resolved)
        if plan_errors:
            raise ValueError(f"Studio-resolved SFX plan is invalid: {plan_errors[0]}")
        atomic_write_json(plan_path, resolved)
    return expected


def fresh_sfx_bindings(
    project_dir: Path,
    expected_editor_state_revision: str,
    expected_timeline_revision: str,
    expected_cut_map_sha256: str,
) -> tuple[dict[str, Any], str, str]:
    """Re-read current editor state; never let an earlier plan prove freshness."""
    current = read_json(project_dir / "working/editor_state.json", {}) or {}
    if not isinstance(current, dict):
        raise ValueError("editor state is unreadable before SFX publication")
    state_revision = editor_state_revision(current)
    timeline_revision = editor_base_state_revision(current)
    cut_hash = sfx_delivery.effective_cut_map_sha256(project_dir, current)
    if (
        state_revision != expected_editor_state_revision
        or timeline_revision != expected_timeline_revision
        or cut_hash != expected_cut_map_sha256
    ):
        raise ValueError("editor state, base timeline, or cut map changed after SFX staging")
    if isinstance(current.get("audio_event_edits"), dict):
        _render_id, source_plan, _source_hash = resolve_audio_event_source(
            project_dir, current
        )
        resolve_studio_audio_plan(source_plan, current["audio_event_edits"])
    return current, timeline_revision, cut_hash


def assert_sfx_candidate_binding(
    verification: dict[str, Any], report_payload: dict[str, Any], candidate: Path,
) -> None:
    """Bind every SFX receipt to the exact still-private candidate bytes."""
    candidate_hash = file_sha256(candidate)
    if verification.get("candidate_output_sha256") != candidate_hash:
        raise RuntimeError("SFX verification candidate output hash does not match live candidate")
    report_sfx = report_payload.get("sfx_delivery")
    if not isinstance(report_sfx, dict) or report_sfx.get("candidate_output_sha256") != candidate_hash:
        raise RuntimeError("SFX QA report candidate output hash does not match live candidate")
    try:
        qa_video.validate_sfx_report(report_sfx)
    except ValueError as exc:
        raise RuntimeError("SFX QA report shape is invalid at publication") from exc
    if report_sfx != verification:
        raise RuntimeError("SFX QA report does not match fresh private evidence verification")


def qa_unpublished_output(
    project_dir: Path,
    candidate: Path,
    report_stem: str,
    visual_evidence_path: Path,
    delivery_label: str,
    state: dict[str, Any] | None = None,
    report_path: Path | None = None,
    contact_path: Path | None = None,
    audio_event_plan: Path | None = None,
    audio_catalog: Path | None = None,
    sfx_stem: Path | None = None,
    expected_timeline_revision: str | None = None,
    expected_cut_map_sha256: str | None = None,
    expected_studio_edits_sha256: str | None = None,
    dialogue_priority_dialogue: Path | None = None,
    dialogue_priority_sfx: Path | None = None,
) -> dict[str, Any]:
    """Run QA on a still-unpublished final; raise before anything lands."""
    qa_dir = project_dir / "qa"
    qa_dir.mkdir(exist_ok=True)
    report_path = report_path or qa_dir / f"{report_stem}.json"
    contact_path = contact_path or qa_dir / f"{report_stem}-contact.png"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    contact_path.parent.mkdir(parents=True, exist_ok=True)
    sfx_paths = (audio_event_plan, audio_catalog, sfx_stem)
    if any(path is not None for path in sfx_paths) and not all(path is not None for path in sfx_paths):
        raise ValueError("SFX QA artifacts must be all-or-none")
    sfx_args: list[str] = []
    if all(path is not None for path in sfx_paths):
        if not expected_timeline_revision or not expected_cut_map_sha256:
            raise ValueError("SFX QA requires fresh timeline and cut bindings")
        sfx_args = [
            "--audio-event-plan", str(audio_event_plan),
            "--audio-catalog", str(audio_catalog),
            "--sfx-stem", str(sfx_stem),
            "--expected-timeline-revision", expected_timeline_revision,
            "--expected-cut-map-sha256", expected_cut_map_sha256,
        ]
        if expected_studio_edits_sha256 is not None:
            sfx_args.extend([
                "--expected-studio-edits-sha256",
                expected_studio_edits_sha256,
            ])
        priority_paths = (dialogue_priority_dialogue, dialogue_priority_sfx)
        if any(path is not None for path in priority_paths) and not all(
            path is not None for path in priority_paths
        ):
            raise ValueError("dialogue-priority QA evidence must be all-or-none")
        if all(path is not None for path in priority_paths):
            sfx_args.extend([
                "--dialogue-priority-dialogue", str(dialogue_priority_dialogue),
                "--dialogue-priority-sfx", str(dialogue_priority_sfx),
            ])
    qa_result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("qa_video.py")),
            "--video", str(candidate),
            "--report", str(report_path),
            "--contact", str(contact_path),
            "--visual-evidence", str(visual_evidence_path),
            *sfx_args,
            *qa_video.qa_policy_args(
                state, read_json(project_dir / "project.json", {}) or {}
            ),
        ],
        text=True,
        capture_output=True,
    )
    if qa_result.returncode != 0:
        report_path.unlink(missing_ok=True)
        contact_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"{delivery_label} QA failed; final output was NOT published: "
            + (qa_result.stderr or qa_result.stdout)[-2000:]
        )
    report = read_json(report_path, {}) or {}
    visual_delivery = report.get("visual_delivery")
    if (
        not isinstance(visual_delivery, dict)
        or visual_delivery.get("source") != "renderer_evidence"
        or visual_delivery.get("status") != "pass"
    ):
        report_path.unlink(missing_ok=True)
        contact_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"{delivery_label} QA did not bind passing renderer visual evidence; "
            "final output was NOT published"
        )
    if sfx_args:
        sfx_delivery_report = report.get("sfx_delivery")
        if (
            not isinstance(sfx_delivery_report, dict)
            or sfx_delivery_report.get("source") != "independent_sfx_evidence"
            or sfx_delivery_report.get("status") != "pass"
        ):
            report_path.unlink(missing_ok=True)
            contact_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"{delivery_label} QA did not bind passing SFX delivery; "
                "final output was NOT published"
            )
    return {
        "report": report_path.relative_to(project_dir).as_posix(),
        "report_sha256": file_sha256(report_path),
        "contact_sheet": contact_path.relative_to(project_dir).as_posix(),
        "contact_sheet_sha256": file_sha256(contact_path),
        "output_sha256": file_sha256(candidate),
        "visual_delivery": visual_delivery,
    }


def qa_variant_output(
    project_dir: Path,
    candidate: Path,
    variant_id: str,
    visual_evidence_path: Path,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return qa_unpublished_output(
        project_dir,
        candidate,
        f"variant-{variant_id}",
        visual_evidence_path,
        "variant",
        state,
    )


def qa_direct_final_output(
    project_dir: Path,
    candidate: Path,
    render_id: str,
    visual_evidence_path: Path,
    state: dict[str, Any] | None = None,
    report_path: Path | None = None,
    contact_path: Path | None = None,
    audio_event_plan: Path | None = None,
    audio_catalog: Path | None = None,
    sfx_stem: Path | None = None,
    expected_timeline_revision: str | None = None,
    expected_cut_map_sha256: str | None = None,
    expected_studio_edits_sha256: str | None = None,
    dialogue_priority_dialogue: Path | None = None,
    dialogue_priority_sfx: Path | None = None,
) -> dict[str, Any]:
    return qa_unpublished_output(
        project_dir,
        candidate,
        render_id,
        visual_evidence_path,
        "direct final",
        state,
        report_path=report_path,
        contact_path=contact_path,
        audio_event_plan=audio_event_plan,
        audio_catalog=audio_catalog,
        sfx_stem=sfx_stem,
        dialogue_priority_dialogue=dialogue_priority_dialogue,
        dialogue_priority_sfx=dialogue_priority_sfx,
        expected_timeline_revision=expected_timeline_revision,
        expected_cut_map_sha256=expected_cut_map_sha256,
        expected_studio_edits_sha256=expected_studio_edits_sha256,
    )


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


DEFAULT_CARD_Y = 46.0


def card_plan_bundle(
    project_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """The card plan expressed as a layer bundle, or None if there is no plan.

    A plan that exists but cannot be drawn is an error, not a silent fall
    back to whatever the old paths would have produced: the author asked for
    those cards and would otherwise get different ones with no warning.
    """
    import card_plan

    path = project_dir / card_plan.CARD_PLAN_REL
    if not path.is_file():
        return None
    # load() recovers to an empty plan so a caller can start adding cards to
    # a damaged file. Here that recovery would be indistinguishable from a
    # deliberately empty plan, and the author would silently get no cards.
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"card plan at {path} could not be read: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError(
            f"card plan at {path} is schema_version "
            f"{raw.get('schema_version') if isinstance(raw, dict) else '?'}, "
            "which this renderer does not understand"
        )
    plan = card_plan.load(project_dir, "")
    if not plan.get("items"):
        return {"schema_version": 1, "items": []}, {
            "schema_version": 1,
            "revision": plan.get("revision", ""),
            "highlight_plan_revision": plan.get("revision", ""),
            "items": [],
        }
    return card_plan.to_layer_bundle(plan)


def caption_top_fraction(state: dict[str, Any]) -> float:
    """Where the caption band starts, as a fraction from the top."""
    defaults = state.get("caption_defaults")
    y = 76.0
    if isinstance(defaults, dict):
        try:
            y = float(defaults.get("y", y))
        except (TypeError, ValueError):
            pass
    # Captions are anchored by their centre and can wrap to a second line.
    return max(0.0, min(1.0, (y - 8.0) / 100.0))


def card_y_for_window(
    source: Path,
    plan_item: dict[str, Any],
    artifact: dict[str, Any],
    canvas_height: int,
    caption_top: float,
    reserved_top: float = 0.0,
) -> float:
    """Put the card where the speaker is not.

    A fixed mid-frame position lands on whoever is talking. The tracker
    already knows where they are, so ask it, and keep the fixed position for
    every case where it cannot answer.
    """
    try:
        import subject_tracker
    except ImportError:
        return DEFAULT_CARD_Y
    card_height = float(artifact.get("height") or 0.0)
    if card_height <= 0 or canvas_height <= 0:
        return DEFAULT_CARD_Y
    try:
        head_top = subject_tracker.subject_head_top(
            source,
            float(plan_item.get("start", 0.0)),
            float(plan_item.get("end", 0.0)),
        )
        placement, reason = subject_tracker.card_y_percent(
            head_top,
            card_height_fraction=card_height / canvas_height,
            caption_top=caption_top,
            default=DEFAULT_CARD_Y,
            reserved_top=reserved_top,
        )
    except Exception:
        return DEFAULT_CARD_Y
    print(
        json.dumps({"card_placement": {"y": placement, "reason": reason}},
                   ensure_ascii=False),
        file=sys.stderr,
    )
    return placement


def tracked_or_centered_crop_x(
    source: Path,
    manifest: dict[str, Any],
    clip_start: float,
    clip_end: float,
    width: int,
    height: int,
) -> str:
    """Where the crop window sits over time — following the subject if found.

    Falls back to centring for every reason it can fail: no source
    dimensions, no subject, tracking unavailable. A vertical crop that
    guesses centre is the old behaviour, not a broken render.
    """
    try:
        import subject_tracker
    except ImportError:
        return "(iw-ow)/2"
    source_info = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    try:
        src_w = float(source_info.get("width") or 0.0)
        src_h = float(source_info.get("height") or 0.0)
    except (TypeError, ValueError):
        return "(iw-ow)/2"
    if src_w <= 0 or src_h <= 0:
        return "(iw-ow)/2"
    # cover scales by whichever axis needs the most, then crops the excess.
    scale = max(width / src_w, height / src_h)
    scaled_width = src_w * scale
    try:
        expression, report = subject_tracker.tracked_crop_x(
            source, clip_start, clip_end,
            scaled_width=scaled_width, window_width=float(width),
        )
    except Exception:
        # Tracking is an enhancement; never let it cost the render.
        return "(iw-ow)/2"
    if expression is None:
        return "(iw-ow)/2"
    print(
        json.dumps({"subject_tracking": report}, ensure_ascii=False),
        file=sys.stderr,
    )
    # Commas separate filters in a filtergraph, so every comma inside an
    # expression has to be escaped or the graph splits mid-argument.
    return expression.replace(",", "\\,")


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
