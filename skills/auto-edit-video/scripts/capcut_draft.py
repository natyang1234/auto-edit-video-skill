#!/usr/bin/env python3
"""Generate a CapCut (international) draft project from an auto-edit timeline.

Schema constraints (violating any of these makes CapCut ignore or reject the draft):
- Top-level skeleton must match a CapCut-9.x-created draft (``version`` 360000);
  the embedded skeleton below was captured from a real Mac CapCut 9.1 empty draft
  with device/draft identity fields blanked.
- All timestamps are microseconds. ``target_timerange`` values on a track must be
  contiguous from 0 with no overlap.
- Mac CapCut reads ``draft_info.json`` and keeps a byte-identical ``template-2.tmp``
  mirror; Windows reads ``draft_content.json``. All three are written identically.
- Segment/material shapes follow the community-verified pyJianYingDraft (MIT)
  export format, which targets the same draft lineage.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SKELETON_JSON = r'''{"canvas_config":{"background":null,"height":1080,"ratio":"original","width":1920},"color_space":-1,"config":{"adjust_max_index":1,"attachment_info":[],"combination_max_index":1,"export_range":null,"extract_audio_last_index":1,"lyrics_recognition_id":"","lyrics_sync":true,"lyrics_taskinfo":[],"maintrack_adsorb":true,"material_save_mode":0,"multi_language_current":"none","multi_language_list":[],"multi_language_main":"none","multi_language_mode":"none","original_sound_last_index":1,"record_audio_last_index":1,"sticker_max_index":1,"subtitle_keywords_config":null,"subtitle_recognition_id":"","subtitle_sync":true,"subtitle_taskinfo":[],"system_font_list":[],"use_float_render":false,"video_mute":false,"voice_change_sync":false,"zoom_info_params":null},"cover":null,"create_time":0,"draft_type":"video","duration":0,"extra_info":null,"fps":30.0,"free_render_index_mode_on":false,"function_assistant_info":{"audio_noise_segid_list":[],"auto_adjust":false,"auto_adjust_fixed":false,"auto_adjust_fixed_value":50.0,"auto_adjust_segid_list":[],"auto_caption":false,"auto_caption_segid_list":[],"auto_caption_template_id":"","caption_opt":false,"caption_opt_segid_list":[],"color_correction":false,"color_correction_fixed":false,"color_correction_fixed_value":50.0,"color_correction_segid_list":[],"deflicker_segid_list":[],"enhance_quality":false,"enhance_quality_fixed":false,"enhance_quality_segid_list":[],"enhance_voice_segid_list":[],"enhande_voice":false,"enhande_voice_fixed":false,"eye_correction":false,"eye_correction_segid_list":[],"fixed_rec_applied":false,"fps":{"den":1,"num":0},"normalize_loudness":false,"normalize_loudness_audio_denoise_segid_list":[],"normalize_loudness_fixed":false,"normalize_loudness_segid_list":[],"retouch":false,"retouch_fixed":false,"retouch_segid_list":[],"smart_rec_applied":false,"smart_segid_list":[],"smooth_slow_motion":false,"smooth_slow_motion_fixed":false,"video_noise_segid_list":[]},"group_container":null,"id":"","is_drop_frame_timecode":false,"keyframe_graph_list":[],"keyframes":{"adjusts":[],"audios":[],"effects":[],"filters":[],"handwrites":[],"stickers":[],"texts":[],"videos":[]},"last_modified_platform":{"app_id":359289,"app_source":"cc","app_version":"9.2.8-beta4","device_id":"","hard_disk_id":"","mac_address":"","os":"mac","os_version":"26.5"},"lyrics_effects":[],"materials":{"ai_text_effects":[],"ai_translates":[],"audio_balances":[],"audio_effects":[],"audio_fades":[],"audio_pannings":[],"audio_pitch_shifts":[],"audio_track_indexes":[],"audios":[],"beats":[],"canvases":[],"chromas":[],"color_curves":[],"common_mask":[],"digital_human_model_dressing":[],"digital_humans":[],"drafts":[],"effects":[],"flowers":[],"green_screens":[],"handwrites":[],"hsl":[],"hsl_curves":[],"images":[],"log_color_wheels":[],"loudnesses":[],"manual_beautys":[],"manual_deformations":[],"material_animations":[],"material_colors":[],"multi_language_refs":[],"placeholder_infos":[],"placeholders":[],"plugin_effects":[],"primary_color_wheels":[],"realtime_denoises":[],"shapes":[],"smart_crops":[],"smart_relights":[],"sound_channel_mappings":[],"speeds":[],"stickers":[],"tail_leaders":[],"text_templates":[],"texts":[],"time_marks":[],"transitions":[],"video_effects":[],"video_radius":[],"video_shadows":[],"video_strokes":[],"video_trackings":[],"videos":[],"vocal_beautifys":[],"vocal_separations":[]},"mixed_track_mode_on":false,"mutable_config":null,"name":"","new_version":"183.0.0","path":"","platform":{"app_id":359289,"app_source":"cc","app_version":"9.2.8-beta4","device_id":"","hard_disk_id":"","mac_address":"","os":"mac","os_version":"26.5"},"relationships":[],"render_index_track_mode_on":true,"retouch_cover":null,"smart_ads_info":{"draft_url":"","page_from":"","routine":""},"source":"default","static_cover_image_path":"","time_marks":null,"tracks":[],"uneven_animation_template_info":{"composition":"","content":"","order":"","sub_template_info_list":[]},"update_time":0,"version":360000}'''

US = 1_000_000
# CapCut ignores sub-5ms slivers; also used as the floor for caption pieces.
MIN_PIECE_S = 0.12
# JianYing/CapCut subtitle convention: near the bottom of the frame.
SUBTITLE_TRANSFORM_Y = -0.8


class CapcutDraftError(RuntimeError):
    pass


def _uid() -> str:
    return uuid.uuid4().hex


def _uuid_upper() -> str:
    return str(uuid.uuid4()).upper()


def to_us(seconds: float) -> int:
    return int(round(seconds * US))


def default_draft_root() -> Path:
    return Path.home() / "Movies/CapCut/User Data/Projects/com.lveditor.draft"


def capcut_is_running() -> bool:
    probe = subprocess.run(["pgrep", "-x", "CapCut"], capture_output=True)
    return probe.returncode == 0


def probe_media(path: Path) -> dict[str, Any]:
    command = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", str(path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except FileNotFoundError as exc:
        raise CapcutDraftError("ffprobe is required but not installed") from exc
    if result.returncode != 0:
        raise CapcutDraftError(f"ffprobe failed for {path}: {result.stderr.strip()[:300]}")
    payload = json.loads(result.stdout or "{}")
    video = next(
        (s for s in payload.get("streams", []) if s.get("codec_type") == "video"),
        None,
    )
    if video is None:
        raise CapcutDraftError(f"no video stream in {path}")
    duration_s = float(payload.get("format", {}).get("duration") or video.get("duration") or 0.0)
    if duration_s <= 0:
        raise CapcutDraftError(f"could not determine duration of {path}")
    rate = video.get("avg_frame_rate") or "30/1"
    try:
        num, den = rate.split("/")
        fps = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        fps = 30.0
    return {
        "duration_us": to_us(duration_s),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": round(fps, 2) if fps > 0 else 30.0,
    }


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str


_SRT_TIME = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)


def parse_srt(text: str) -> list[Cue]:
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", text.strip().replace("\r\n", "\n")):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        match = None
        body_start = 0
        for index, line in enumerate(lines[:2]):
            match = _SRT_TIME.search(line)
            if match:
                body_start = index + 1
                break
        if not match:
            continue
        groups = match.groups()
        h1, m1, s1 = int(groups[0]), int(groups[1]), int(groups[2])
        h2, m2, s2 = int(groups[4]), int(groups[5]), int(groups[6])
        if m1 > 59 or s1 > 59 or m2 > 59 or s2 > 59:
            continue
        # 1-2 digit millisecond fields are decimal fractions ("5" == 500ms), not 5ms.
        ms1 = int(groups[3].ljust(3, "0"))
        ms2 = int(groups[7].ljust(3, "0"))
        start = h1 * 3600 + m1 * 60 + s1 + ms1 / 1000
        end = h2 * 3600 + m2 * 60 + s2 + ms2 / 1000
        body = " ".join(lines[body_start:]).strip()
        if body and end > start:
            cues.append(Cue(start, end, body))
    return cues


def remap_cues(cues: list[Cue], segments: list[tuple[float, float]]) -> list[Cue]:
    """Map source-time cues onto the cut output timeline.

    A cue crossing a deleted region yields one piece per kept intersection;
    pieces shorter than MIN_PIECE_S are dropped so flash-frames never ship.
    """
    remapped: list[Cue] = []
    offset = 0.0
    for seg_start, seg_end in segments:
        seg_len = seg_end - seg_start
        for cue in cues:
            piece_start = max(cue.start, seg_start)
            piece_end = min(cue.end, seg_end)
            if piece_end - piece_start >= MIN_PIECE_S:
                remapped.append(
                    Cue(
                        offset + (piece_start - seg_start),
                        offset + (piece_end - seg_start),
                        cue.text,
                    )
                )
        offset += seg_len
    remapped.sort(key=lambda c: c.start)
    return remapped


def _base_segment(material_id: str, target_start_us: int, target_dur_us: int) -> dict[str, Any]:
    return {
        "enable_adjust": True,
        "enable_color_correct_adjust": False,
        "enable_color_curves": True,
        "enable_color_match_adjust": False,
        "enable_color_wheels": True,
        "enable_lut": True,
        "enable_smart_color_adjust": False,
        "last_nonzero_volume": 1.0,
        "reverse": False,
        "track_attribute": 0,
        "track_render_index": 0,
        "visible": True,
        "id": _uid(),
        "material_id": material_id,
        "target_timerange": {"start": target_start_us, "duration": target_dur_us},
        "common_keyframes": [],
        "keyframe_refs": [],
    }


def _clip_settings(transform_y: float = 0.0) -> dict[str, Any]:
    return {
        "alpha": 1.0,
        "flip": {"horizontal": False, "vertical": False},
        "rotation": 0.0,
        "scale": {"x": 1.0, "y": 1.0},
        "transform": {"x": 0.0, "y": transform_y},
    }


def _speed_material() -> dict[str, Any]:
    return {"curve_speed": None, "id": _uid(), "mode": 0, "speed": 1.0, "type": "speed"}


def build_video_material(path: Path, probe: dict[str, Any]) -> dict[str, Any]:
    return {
        "audio_fade": None,
        "category_id": "",
        "category_name": "local",
        "check_flag": 63487,
        "crop": {
            "lower_left_x": 0.0, "lower_left_y": 1.0,
            "lower_right_x": 1.0, "lower_right_y": 1.0,
            "upper_left_x": 0.0, "upper_left_y": 0.0,
            "upper_right_x": 1.0, "upper_right_y": 0.0,
        },
        "crop_ratio": "free",
        "crop_scale": 1.0,
        "duration": probe["duration_us"],
        "height": probe["height"],
        "id": _uid(),
        "local_material_id": "",
        "material_id": "",
        "material_name": path.name,
        "media_path": "",
        "path": str(path),
        "type": "video",
        "width": probe["width"],
    }


def build_video_segment(
    material_id: str,
    speed_id: str,
    source_start_us: int,
    duration_us: int,
    target_start_us: int,
) -> dict[str, Any]:
    segment = _base_segment(material_id, target_start_us, duration_us)
    segment.update(
        {
            "source_timerange": {"start": source_start_us, "duration": duration_us},
            "speed": 1.0,
            "volume": 1.0,
            "extra_material_refs": [speed_id],
            "is_tone_modify": False,
            "clip": _clip_settings(),
            "uniform_scale": {"on": True, "value": 1.0},
            "hdr_settings": {"intensity": 1.0, "mode": 1, "nits": 1000},
        }
    )
    return segment


def build_text_material(text: str) -> dict[str, Any]:
    content = {
        "styles": [
            {
                "fill": {
                    "alpha": 1.0,
                    "content": {
                        "render_type": "solid",
                        "solid": {"alpha": 1.0, "color": [1.0, 1.0, 1.0]},
                    },
                },
                "range": [0, len(text)],
                "size": 8.0,
                "bold": False,
                "italic": False,
                "underline": False,
                "strokes": [],
            }
        ],
        "text": text,
    }
    return {
        "id": _uid(),
        "content": json.dumps(content, ensure_ascii=False),
        "typesetting": 0,
        "alignment": 1,
        "letter_spacing": 0.0,
        "line_spacing": 0.02,
        "line_feed": 1,
        "line_max_width": 0.82,
        "force_apply_line_max_width": False,
        "check_flag": 7,
        "type": "subtitle",
        "global_alpha": 1.0,
    }


def build_text_segment(material_id: str, target_start_us: int, duration_us: int) -> dict[str, Any]:
    segment = _base_segment(material_id, target_start_us, duration_us)
    segment.update(
        {
            "source_timerange": None,
            "speed": 1.0,
            "volume": 1.0,
            "extra_material_refs": [],
            "is_tone_modify": False,
            "clip": _clip_settings(transform_y=SUBTITLE_TRANSFORM_Y),
            "uniform_scale": {"on": True, "value": 1.0},
        }
    )
    return segment


def _track(track_type: str, segments: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "attribute": 0,
        "flag": 0,
        "id": _uid(),
        "is_default_name": True,
        "name": "",
        "segments": segments,
        "type": track_type,
    }


def build_draft(
    source_path: Path,
    probe: dict[str, Any],
    segments_s: list[tuple[float, float]],
    cues: list[Cue],
    canvas: tuple[int, int] | None = None,
    draft_name: str = "",
) -> dict[str, Any]:
    if not segments_s:
        raise CapcutDraftError("timeline has no segments")
    for start, end in segments_s:
        if end <= start or start < 0:
            raise CapcutDraftError(f"invalid segment ({start}, {end})")

    draft = json.loads(_SKELETON_JSON)
    draft["id"] = _uuid_upper()
    draft["name"] = draft_name

    material = build_video_material(source_path, probe)
    speeds: list[dict[str, Any]] = []
    video_segments: list[dict[str, Any]] = []
    cursor_us = 0
    for start_s, end_s in segments_s:
        duration_us = to_us(end_s - start_s)
        if duration_us <= 0:
            continue
        speed = _speed_material()
        speeds.append(speed)
        video_segments.append(
            build_video_segment(material["id"], speed["id"], to_us(start_s), duration_us, cursor_us)
        )
        cursor_us += duration_us

    text_materials: list[dict[str, Any]] = []
    text_segments: list[dict[str, Any]] = []
    for cue in remap_cues(cues, segments_s):
        text_material = build_text_material(cue.text)
        text_materials.append(text_material)
        start_us = to_us(cue.start)
        end_us = min(to_us(cue.end), cursor_us)
        if end_us - start_us < to_us(MIN_PIECE_S):
            text_materials.pop()
            continue
        text_segments.append(build_text_segment(text_material["id"], start_us, end_us - start_us))

    materials = draft.setdefault("materials", {})
    materials["videos"] = [material]
    materials["speeds"] = speeds
    materials["texts"] = text_materials

    tracks = [_track("video", video_segments)]
    if text_segments:
        tracks.append(_track("text", text_segments))
    draft["tracks"] = tracks
    draft["duration"] = cursor_us
    draft["fps"] = probe.get("fps") or 30.0

    if canvas:
        width, height = canvas
    else:
        width, height = probe["width"], probe["height"]
    ratio = "9:16" if width < height else "1:1" if width == height else "16:9"
    draft["canvas_config"] = {
        "background": None, "height": height, "ratio": ratio, "width": width,
    }
    return draft


def _meta_info(draft_dir: Path, draft: dict[str, Any]) -> dict[str, Any]:
    return {
        "draft_cover": "draft_cover.jpg",
        "draft_fold_path": str(draft_dir),
        "draft_id": _uuid_upper(),
        "draft_materials": [{"type": t, "value": []} for t in (0, 1, 2, 3, 6)],
        "draft_materials_copied_info": [],
        "draft_name": draft_dir.name,
        "draft_need_rename_folder": False,
        "draft_new_version": "",
        "draft_removable_storage_device": "",
        "draft_root_path": str(draft_dir.parent),
        "draft_segment_extra_info": [],
        "draft_type": "",
        "tm_duration": draft.get("duration", 0),
    }


def _validate_draft_name(name: str) -> None:
    # The draft name becomes a directory component under the CapCut root; anything
    # that can traverse or hide must be rejected before any filesystem operation.
    if not name or name in {".", ".."}:
        raise CapcutDraftError("draft name must not be empty")
    if name.startswith("."):
        raise CapcutDraftError("draft name must not start with '.' (invisible to CapCut)")
    if "/" in name or "\\" in name or "\x00" in name:
        raise CapcutDraftError(f"draft name must be a plain folder name, got {name!r}")


def _root_needs_running_check(root: Path) -> bool:
    resolved = root.resolve()
    for watched in (Path.home() / "Movies/CapCut", Path.home() / "Movies/JianyingPro"):
        if resolved == watched or watched in resolved.parents or resolved.is_relative_to(watched):
            return True
    return False


def write_draft(
    draft: dict[str, Any],
    name: str,
    draft_root: Path | None = None,
    force: bool = False,
    copy_media: bool = True,
) -> Path:
    _validate_draft_name(name)
    root = (draft_root or default_draft_root()).expanduser()
    if not root.is_dir():
        raise CapcutDraftError(
            f"CapCut draft root not found: {root} (launch CapCut once to create it, "
            "or pass an explicit draft root)"
        )
    # Editing inside CapCut's own tree while it runs loses the write to auto-save.
    if _root_needs_running_check(root) and capcut_is_running():
        raise CapcutDraftError("CapCut is running — quit it first so auto-save cannot overwrite the draft")
    target = root / name
    if target.resolve().parent != root.resolve():
        raise CapcutDraftError(f"draft name escapes the draft root: {name!r}")
    if target.exists():
        if not force:
            raise CapcutDraftError(f"draft '{name}' already exists at {target} (use --force to replace)")
        # Dot-folders are invisible to CapCut's project list (cf. its own .recycle_bin).
        trash = root / ".pre-export-backups"
        trash.mkdir(exist_ok=True)
        backup = trash / f"{name}-{_uid()[:8]}"
        target.rename(backup)
    target.mkdir(parents=True)

    draft = json.loads(json.dumps(draft))
    draft["name"] = name
    draft["path"] = str(target)
    # Sandboxed CapCut cannot read paths outside its own tree without a
    # user-granted bookmark; media must live inside the draft folder.
    if copy_media:
        media_dir = target / "Resources" / "local"
        media_dir.mkdir(parents=True, exist_ok=True)
        for video in draft.get("materials", {}).get("videos", []):
            original = Path(str(video.get("path") or ""))
            if not original.is_file():
                raise CapcutDraftError(f"source media missing: {original}")
            local_name = f"{_uid()[:12]}{original.suffix.lower()}"
            local_path = media_dir / local_name
            shutil.copy2(original, local_path)
            video["path"] = str(local_path)
    blob = json.dumps(draft, ensure_ascii=False, separators=(",", ":"))
    for filename in ("draft_info.json", "draft_content.json", "template-2.tmp"):
        (target / filename).write_text(blob, encoding="utf-8")
    meta = _meta_info(target, draft)
    (target / "draft_meta_info.json").write_text(
        json.dumps(meta, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    (target / "Resources").mkdir(exist_ok=True)
    return target
