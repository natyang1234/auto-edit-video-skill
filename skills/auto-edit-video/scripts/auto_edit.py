#!/usr/bin/env python3
"""Deterministic project/voice bridge for the auto-edit-video skill."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from highlight_planner import (
    DIRECTOR_PROFILES as HIGHLIGHT_DIRECTOR_PROFILES,
    build_highlight_plan,
    validate_highlight_plan,
)
from visual_quality import build_highlight_design_overlays
from template_catalog import cutout_capability


SCHEMA_VERSION = 1
SKILL_DIR = Path(__file__).resolve().parents[1]
HOME = Path.home()


def _path_from_env(name: str, fallback: Path) -> Path:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser() if value else fallback


def _skill_roots() -> list[Path]:
    """Return cross-agent skill roots in deterministic precedence order."""
    roots: list[Path] = []
    extra = os.environ.get("AUTO_EDIT_SKILLS_ROOTS", "")
    if extra:
        roots.extend(Path(item).expanduser() for item in extra.split(os.pathsep) if item)
    roots.extend(
        [
            SKILL_DIR.parent,
            _path_from_env("CODEX_HOME", HOME / ".codex") / "skills",
            HOME / ".codex/skills",
            HOME / ".agents/skills",
            HOME / ".claude/skills",
            _path_from_env("GROK_HOME", HOME / ".grok") / "skills",
            _path_from_env("OPENCLAW_HOME", HOME / ".openclaw") / "skills",
            _path_from_env("OPENCLAW_WORKSPACE", HOME / ".openclaw/workspace") / "skills",
            _path_from_env("HERMES_HOME", HOME / ".hermes") / "skills",
        ]
    )
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root.expanduser())
        if key not in seen:
            unique.append(root.expanduser())
            seen.add(key)
    return unique


def _find_skill_dir(name: str, override_env: str) -> Path:
    override = os.environ.get(override_env, "").strip()
    if override:
        return Path(override).expanduser()
    for root in _skill_roots():
        candidate = root / name
        if (candidate / "SKILL.md").is_file():
            return candidate
    return _skill_roots()[0] / name


VIDEO_AUTOPILOT_DIR = _find_skill_dir(
    "video-autopilot-macos", "VIDEO_AUTOPILOT_SKILL_DIR"
)
CUT_NARRATION_DIR = _find_skill_dir(
    "chengfeng-cut-narration", "CUT_NARRATION_SKILL_DIR"
)
NARRATION_VIDEO_DIR = _find_skill_dir(
    "chengfeng-narration-to-video", "NARRATION_VIDEO_SKILL_DIR"
)
EMBEDDED_CAPTIONS_DIR = _find_skill_dir(
    "embedded-captions", "EMBEDDED_CAPTIONS_SKILL_DIR"
)
TALKING_HEAD_RECUT_DIR = _find_skill_dir(
    "talking-head-recut", "TALKING_HEAD_RECUT_SKILL_DIR"
)
HYPERFRAMES_MEDIA_DIR = _find_skill_dir(
    "hyperframes-media", "HYPERFRAMES_MEDIA_SKILL_DIR"
)

PATHS = {
    "video_autopilot_skill": VIDEO_AUTOPILOT_DIR / "SKILL.md",
    "video_autopilot_cli": VIDEO_AUTOPILOT_DIR / "scripts/vak.py",
    "video_autopilot_repo": Path(
        os.environ.get("VIDEO_AUTOPILOT_HOME", "~/video-autopilot-kit")
    ).expanduser(),
    "cut_skill": CUT_NARRATION_DIR / "SKILL.md",
    "cut_review": CUT_NARRATION_DIR / "scripts/generate_review.js",
    "cut_server": CUT_NARRATION_DIR / "scripts/review_server.js",
    "narration_skill": NARRATION_VIDEO_DIR / "SKILL.md",
    "narration_export": NARRATION_VIDEO_DIR / "scripts/export_final_video.cjs",
    "embedded_captions": EMBEDDED_CAPTIONS_DIR / "SKILL.md",
    "talking_head_recut": TALKING_HEAD_RECUT_DIR / "SKILL.md",
    "hyperframes_media": HYPERFRAMES_MEDIA_DIR / "SKILL.md",
    "hyperframes_audio": HYPERFRAMES_MEDIA_DIR / "scripts/audio.mjs",
    "rumi_voice_system": _path_from_env(
        "RUMI_VOICE_SYSTEM",
        HOME / ".openclaw/workspace/tools/tts_voices.py",
    ),
    "editor_server": SKILL_DIR / "scripts/editor_server.py",
    "studio_server": SKILL_DIR / "scripts/studio_server.py",
    "editor_renderer": SKILL_DIR / "scripts/render_editor_timeline.py",
    "template_catalog": SKILL_DIR / "scripts/template_catalog.py",
    "subject_compositor": SKILL_DIR / "scripts/subject_compositor.py",
    "editor_index": SKILL_DIR / "editor/index.html",
    "studio_index": SKILL_DIR / "editor/import.html",
    "cut_renderer": SKILL_DIR / "scripts/render_cut.py",
    "qa_runner": SKILL_DIR / "scripts/qa_video.py",
}

VOICE_PRESETS: dict[str, dict[str, str]] = {
    "zh-TW": {
        "female": "zh-TW-HsiaoChenNeural",
        "male": "zh-TW-YunJheNeural",
    },
    "zh-CN": {
        "female": "zh-CN-XiaoxiaoNeural",
        "male": "zh-CN-YunxiNeural",
    },
    "en-US": {
        "female": "en-US-AvaMultilingualNeural",
        "male": "en-US-AndrewMultilingualNeural",
    },
    "en-GB": {
        "female": "en-GB-SoniaNeural",
        "male": "en-GB-RyanNeural",
    },
}

RUMI_SYSTEM_PRESETS: dict[str, dict[str, str]] = {
    "zh-TW": {
        "female": "rumi",
        "male": "溫暖磁性男聲旁白",
    },
    "zh-CN": {
        "female": "rumi",
        "male": "溫暖磁性男聲旁白",
    },
    "en-US": {
        "female": "en-US-JennyNeural",
        "male": "en-US-AndrewNeural",
    },
    "en-GB": {
        "female": "en-GB-SoniaNeural",
        "male": "en-GB-RyanNeural",
    },
}

EDIT_PRESETS = {
    "conservative": {
        "silence_threshold_s": 0.45,
        "cut_handle_s": 0.10,
        "remove_fillers": True,
        "remove_stutters": True,
        "remove_repetitions": True,
        "remove_false_starts": True,
    },
    "balanced": {
        "silence_threshold_s": 0.30,
        "cut_handle_s": 0.08,
        "remove_fillers": True,
        "remove_stutters": True,
        "remove_repetitions": True,
        "remove_false_starts": True,
    },
    "aggressive": {
        "silence_threshold_s": 0.20,
        "cut_handle_s": 0.06,
        "remove_fillers": True,
        "remove_stutters": True,
        "remove_repetitions": True,
        "remove_false_starts": True,
    },
}

DURATION_PROFILE_NAMES = ("short", "medium", "long")
DURATION_PROFILES = ("full", "auto", *DURATION_PROFILE_NAMES)
DURATION_PRESETS: dict[str, dict[str, Any]] = {
    "generic-vertical": {
        "label": "Generic vertical social video",
        "aspect_ratio": "9:16",
        "profiles": {
            "short": {"min_seconds": 15, "target_seconds": 30, "max_seconds": 30},
            "medium": {"min_seconds": 45, "target_seconds": 60, "max_seconds": 90},
            "long": {"min_seconds": 120, "target_seconds": 180, "max_seconds": 180},
        },
    },
    "instagram-reels": {
        "label": "Instagram Reels",
        "aspect_ratio": "9:16",
        "profiles": {
            "short": {"min_seconds": 15, "target_seconds": 30, "max_seconds": 30},
            "medium": {"min_seconds": 45, "target_seconds": 60, "max_seconds": 90},
            "long": {"min_seconds": 120, "target_seconds": 180, "max_seconds": 180},
        },
    },
    "youtube-shorts": {
        "label": "YouTube Shorts",
        "aspect_ratio": "9:16",
        "profiles": {
            "short": {"min_seconds": 15, "target_seconds": 30, "max_seconds": 30},
            "medium": {"min_seconds": 45, "target_seconds": 60, "max_seconds": 60},
            "long": {"min_seconds": 90, "target_seconds": 180, "max_seconds": 180},
        },
    },
    "tiktok": {
        "label": "TikTok",
        "aspect_ratio": "9:16",
        "profiles": {
            "short": {"min_seconds": 15, "target_seconds": 30, "max_seconds": 30},
            "medium": {"min_seconds": 45, "target_seconds": 60, "max_seconds": 90},
            "long": {"min_seconds": 120, "target_seconds": 180, "max_seconds": 300},
        },
    },
    "xiaohongshu-portrait": {
        "label": "Xiaohongshu portrait",
        "aspect_ratio": "3:4",
        "profiles": {
            "short": {"min_seconds": 15, "target_seconds": 30, "max_seconds": 30},
            "medium": {"min_seconds": 45, "target_seconds": 60, "max_seconds": 60},
            "long": {"min_seconds": 90, "target_seconds": 120, "max_seconds": 180},
        },
    },
    "xiaohongshu-full": {
        "label": "Xiaohongshu full-screen",
        "aspect_ratio": "9:16",
        "profiles": {
            "short": {"min_seconds": 15, "target_seconds": 30, "max_seconds": 30},
            "medium": {"min_seconds": 45, "target_seconds": 60, "max_seconds": 60},
            "long": {"min_seconds": 90, "target_seconds": 120, "max_seconds": 180},
        },
    },
    "youtube-landscape": {
        "label": "YouTube landscape video",
        "aspect_ratio": "16:9",
        "profiles": {
            "short": {"min_seconds": 60, "target_seconds": 120, "max_seconds": 180},
            "medium": {"min_seconds": 300, "target_seconds": 480, "max_seconds": 600},
            "long": {"min_seconds": 600, "target_seconds": 900, "max_seconds": 1200},
        },
    },
}
PLATFORMS = ("auto", *DURATION_PRESETS)

STAGES = (
    "ingest",
    "transcribe",
    "edit_analysis",
    "edit_review",
    "cut",
    "retranscribe",
    "subtitles",
    "emphasis",
    "visual_plan",
    "highlight_plan",
    "voiceover",
    "timeline_review",
    "render",
    "qa",
)

GATES = ("destructive_edit", "highlight_selection", "timeline", "final")
SUBTITLE_MODES = ("source", "zh", "en", "bilingual", "off")
VOICE_LANGUAGES = tuple(VOICE_PRESETS)
VOICE_PROVIDERS = ("rumi", "edge", "auto", "heygen", "elevenlabs", "kokoro")

FILLER_TOKENS = {
    "嗯",
    "嗯嗯",
    "呃",
    "呃呃",
    "額",
    "啊",
    "阿",
    "欸",
    "誒",
    "那個",
    "這個",
    "就是",
}


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def resolve_output_target(
    platform: str,
    duration_profile: str,
    target_duration: float | None = None,
) -> dict[str, Any]:
    """Resolve user/agent intent into a portable, non-publishing edit target."""
    if platform not in PLATFORMS:
        raise ValueError(f"unsupported platform: {platform}")
    if duration_profile not in DURATION_PROFILES:
        raise ValueError(f"unsupported duration profile: {duration_profile}")

    preset_platform = "generic-vertical" if platform == "auto" else platform
    common: dict[str, Any] = {
        "platform": platform,
        "preset_platform": preset_platform,
        "semantic_completeness_over_exact_duration": True,
        "source_shorter_policy": "keep_actual_length_without_padding",
        "publishing_in_scope": False,
    }

    if target_duration is not None:
        if not math.isfinite(target_duration) or target_duration <= 0:
            raise ValueError("target duration must be a positive finite number")
        target = round(float(target_duration), 3)
        tolerance = round(max(2.0, min(15.0, target * 0.10)), 3)
        return {
            **common,
            "duration_profile": "custom",
            "selection": "explicit_seconds",
            "basis": "user-explicit-seconds",
            "min_seconds": round(max(0.1, target - tolerance), 3),
            "target_seconds": target,
            "max_seconds": round(target + tolerance, 3),
        }

    if duration_profile == "full":
        return {
            **common,
            "duration_profile": "full",
            "selection": "full_cleanup",
            "basis": "source-duration",
            "min_seconds": None,
            "target_seconds": None,
            "max_seconds": None,
        }

    if duration_profile == "auto":
        return {
            **common,
            "duration_profile": "auto",
            "selection": "agent_after_transcript",
            "basis": "editorial-preset-pending",
            "min_seconds": None,
            "target_seconds": None,
            "max_seconds": None,
        }

    profile = DURATION_PRESETS[preset_platform]["profiles"][duration_profile]
    return {
        **common,
        "duration_profile": duration_profile,
        "selection": "user_profile",
        "basis": "auto-edit-video-editorial-preset",
        **profile,
    }


def emit(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def die(message: str, code: int = 2) -> int:
    print(message, file=sys.stderr)
    return code


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object at {path}")
    return value


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def parse_rate(value: str | None) -> float | None:
    if not value or value == "0/0":
        return None
    try:
        if "/" in value:
            numerator, denominator = value.split("/", 1)
            denominator_value = float(denominator)
            return round(float(numerator) / denominator_value, 4) if denominator_value else None
        return round(float(value), 4)
    except (ValueError, ZeroDivisionError):
        return None


def probe_media(path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise ValueError("ffprobe is required")
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=index,codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels",
                "-of",
                "json",
                str(path),
            ],
            text=True,
            capture_output=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("ffprobe timed out while inspecting source media") from exc
    if result.returncode != 0:
        raise ValueError(f"ffprobe failed: {result.stderr.strip() or 'unknown error'}")
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio = next((item for item in streams if item.get("codec_type") == "audio"), {})
    try:
        duration = round(float(data.get("format", {}).get("duration", 0.0)), 3)
    except (TypeError, ValueError) as exc:
        raise ValueError("source media has an invalid duration") from exc
    if not video or not video.get("width") or not video.get("height"):
        raise ValueError("input must contain a valid video stream")
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("source video duration must be positive and finite")
    return {
        "duration_s": duration,
        "width": video.get("width"),
        "height": video.get("height"),
        "fps": parse_rate(video.get("r_frame_rate")),
        "video_codec": video.get("codec_name"),
        "stream_count": len(streams),
        "video_stream_count": sum(1 for item in streams if item.get("codec_type") == "video"),
        "has_audio": bool(audio),
        "audio_codec": audio.get("codec_name"),
        "audio_sample_rate": int(audio["sample_rate"]) if audio.get("sample_rate") else None,
        "audio_channels": audio.get("channels"),
    }


def language_family(language: str | None) -> str | None:
    if not language or language == "auto":
        return None
    if language.lower().startswith("zh"):
        return "zh"
    if language.lower().startswith("en"):
        return "en"
    return language.split("-", 1)[0].lower()


def edge_voice(language: str, gender: str) -> str:
    try:
        return VOICE_PRESETS[language][gender]
    except KeyError as exc:
        raise ValueError(f"no Edge preset for {language}/{gender}") from exc


def rumi_system_voice(language: str, gender: str) -> str:
    try:
        return RUMI_SYSTEM_PRESETS[language][gender]
    except KeyError as exc:
        raise ValueError(f"no Rumi-system preset for {language}/{gender}") from exc


@lru_cache(maxsize=1)
def load_rumi_voice_system() -> Any:
    path = PATHS["rumi_voice_system"]
    if not path.is_file():
        raise ValueError(f"Rumi voice system is missing: {path}")
    spec = importlib.util.spec_from_file_location("auto_edit_rumi_voices", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load Rumi voice system: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def normalized_gender(value: str) -> str:
    return "female" if str(value).lower() in {"女", "f", "female"} else "male"


def rumi_catalog_entries() -> list[dict[str, Any]]:
    module = load_rumi_voice_system()
    defaults = {getattr(module, "DEFAULT_ZH", "rumi"), getattr(module, "DEFAULT_EN", "")}
    entries: list[dict[str, Any]] = []
    for voice_id, (language, gender, description) in module.catalog().items():
        backend = module._engine_of(voice_id)
        locale_match = re.match(r"^([a-z]{2}-[A-Z]{2})-", voice_id)
        entries.append(
            {
                "provider": "rumi" if backend == "fish" else "edge",
                "voice_system": "rumi",
                "backend": backend,
                "language": locale_match.group(1) if locale_match else language,
                "gender": normalized_gender(gender),
                "voice_id": voice_id,
                "description": description,
                "default": voice_id in defaults,
            }
        )
    return entries


def rumi_voice_details(voice_id: str) -> dict[str, Any] | None:
    try:
        return next(
            (entry for entry in rumi_catalog_entries() if entry["voice_id"] == voice_id),
            None,
        )
    except ValueError:
        return None


def rumi_voice_allowed(voice_id: str) -> bool:
    try:
        return bool(load_rumi_voice_system().is_allowed(voice_id))
    except ValueError:
        return False


def rumi_backend(voice_id: str) -> str:
    return str(load_rumi_voice_system()._engine_of(voice_id))


def configured_env_names(path: Path) -> set[str]:
    names: set[str] = set()
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.match(r"^([A-Z_]+)=(.+)$", line.strip())
            if match and match.group(2).strip():
                names.add(match.group(1))
    except OSError:
        pass
    return names


def preflight_payload() -> dict[str, Any]:
    commands = {
        name: shutil.which(name)
        for name in (
            "ffmpeg",
            "ffprobe",
            "python3",
            "whisper",
            "whisper-cli",
            "node",
            "npx",
            "edge-tts",
        )
    }
    files = {name: path.is_file() for name, path in PATHS.items() if name != "video_autopilot_repo"}
    files["video_autopilot_repo"] = PATHS["video_autopilot_repo"].is_dir()
    rumi_env_path = os.environ.get("RUMI_ENV_FILE", "").strip()
    rumi_env_names = (
        configured_env_names(Path(rumi_env_path).expanduser()) if rumi_env_path else set()
    )
    rumi_has_api_key = "FISH_API_KEY" in rumi_env_names or bool(os.environ.get("FISH_API_KEY"))
    rumi_has_reference = "FISH_REFERENCE_ID" in rumi_env_names or bool(
        os.environ.get("FISH_REFERENCE_ID")
    )
    subject_cutout = cutout_capability()
    core_checks = {
        "ffmpeg": bool(commands["ffmpeg"]),
        "ffprobe": bool(commands["ffprobe"]),
        "python3": bool(commands["python3"]),
        "page_editor": files["editor_server"] and files["editor_index"] and files["template_catalog"],
        "studio_import": files["studio_server"] and files["studio_index"],
        "timeline_renderer": files["editor_renderer"],
        "cut_renderer": files["cut_renderer"],
        "qa_runner": files["qa_runner"],
    }
    extended_checks = {
        "creator_profile_bridge": files["video_autopilot_cli"]
        and files["video_autopilot_repo"],
        "external_cut_review": files["cut_review"] and files["cut_server"],
        "narration_export": files["narration_export"],
    }
    ready = all(core_checks.values())
    extended_ready = all(extended_checks.values())
    return {
        "ready": ready,
        "mode": "extended" if extended_ready else "standalone",
        "extended_ready": extended_ready,
        "platform": sys.platform,
        "commands": commands,
        "files": files,
        "skill_roots": [str(path) for path in _skill_roots()],
        "missing_required": [name for name, ok in core_checks.items() if not ok],
        "missing_optional": [name for name, ok in extended_checks.items() if not ok]
        + ([] if subject_cutout.get("available") else ["local_subject_cutout"]),
        "capabilities": {
            "destructive_edit_review": True,
            "destructive_cut_render": files["cut_renderer"] and bool(commands["ffmpeg"]),
            "programmatic_render_qa": files["video_autopilot_cli"]
            and files["video_autopilot_repo"],
            "bundled_render_qa": files["qa_runner"] and bool(commands["ffmpeg"]),
            "local_transcription": bool(commands["whisper"] or commands["whisper-cli"]),
            "visual_cards": files["talking_head_recut"] and bool(commands["node"]),
            "premium_captions": files["embedded_captions"],
            "voice_rumi_system": files["rumi_voice_system"],
            "voice_rumi_default_ready": files["rumi_voice_system"]
            and rumi_has_api_key
            and rumi_has_reference,
            "voice_hyperframes": files["hyperframes_audio"] and bool(commands["node"]),
            "voice_edge": bool(commands["edge-tts"]),
            "page_editor": files["editor_server"]
            and files["editor_renderer"]
            and files["editor_index"]
            and files["template_catalog"],
            "studio_import": files["studio_server"] and files["studio_index"],
            "local_subject_cutout": bool(
                files["subject_compositor"] and subject_cutout.get("available")
            ),
            "subject_cutout_engine": subject_cutout.get("engine"),
            "subject_cutout_reason": subject_cutout.get("reason"),
            "capcut": False,
        },
    }


def cmd_preflight(_args: argparse.Namespace) -> int:
    payload = preflight_payload()
    emit(payload)
    return 0 if payload["ready"] else 2


def empty_gate() -> dict[str, Any]:
    return {"approved": False, "confirmed_by": None, "at": None, "note": None}


def default_target(source_language: str) -> str:
    return "en" if language_family(source_language) == "zh" else "zh-TW"


def make_voice_config(args: argparse.Namespace) -> dict[str, Any]:
    fields = (
        args.voice_language,
        args.voice_gender,
        args.voice_provider,
        args.voice_id,
    )
    enabled = any(value is not None for value in fields)
    if not enabled:
        return disabled_voice_config()
    if not args.voice_language or not args.voice_gender:
        raise ValueError("voiceover requires --voice-language and --voice-gender")
    provider = args.voice_provider or (
        "rumi"
        if language_family(args.voice_language) == "zh"
        and PATHS["rumi_voice_system"].is_file()
        else "edge"
    )
    voice_id = args.voice_id
    if provider == "rumi" and not voice_id:
        voice_id = rumi_system_voice(args.voice_language, args.voice_gender)
    elif provider == "edge" and not voice_id:
        voice_id = edge_voice(args.voice_language, args.voice_gender)
    resolved = bool(voice_id) and provider != "auto"
    if provider == "rumi":
        engine = "rumi-voice-system"
    elif provider == "edge":
        engine = "edge"
    else:
        engine = "hyperframes-media"
    return {
        "enabled": True,
        "mode": args.voice_mode,
        "engine": engine,
        "provider": provider,
        "language": args.voice_language,
        "gender": args.voice_gender,
        "voice_id": voice_id,
        "speed": args.voice_speed,
        "cloud": provider in {"rumi", "edge", "heygen", "elevenlabs", "auto"},
        "selection_status": "resolved" if resolved else "needs_voice_id",
    }


def disabled_voice_config() -> dict[str, Any]:
    return {
        "enabled": False,
        "mode": "off",
        "engine": None,
        "provider": None,
        "language": None,
        "gender": None,
        "voice_id": None,
        "speed": 1.0,
        "cloud": False,
        "selection_status": "disabled",
    }


def ensure_new_project(project_dir: Path) -> None:
    if project_dir.exists() and any(project_dir.iterdir()):
        raise ValueError(f"project directory is not empty: {project_dir}")
    for name in ("source", "working", "review", "subtitles", "voice", "assets", "renders", "qa"):
        (project_dir / name).mkdir(parents=True, exist_ok=True)


def write_empty_artifacts(project_dir: Path) -> None:
    write_json(
        project_dir / "working/edit_candidates.json",
        {"schema_version": SCHEMA_VERSION, "source": "transcript_words.json", "items": []},
    )
    write_json(
        project_dir / "working/edit_decisions.json",
        {"schema_version": SCHEMA_VERSION, "approved_from": None, "items": []},
    )
    write_json(
        project_dir / "working/emphasis_plan.json",
        {"schema_version": SCHEMA_VERSION, "items": []},
    )
    write_json(
        project_dir / "working/visual_plan.json",
        {"schema_version": SCHEMA_VERSION, "items": []},
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def initialize_project(
    source: Path,
    project_dir: Path,
    *,
    source_language: str = "auto",
    subtitle_mode: str = "source",
    target_language: str | None = None,
    platform: str = "auto",
    duration_profile: str = "full",
    target_duration: float | None = None,
    edit_preset: str = "balanced",
    emphasis: str = "balanced",
    visual_density: str = "balanced",
    cards: bool = True,
    related_assets: bool = True,
    animations: bool = True,
    voice: dict[str, Any] | None = None,
    source_mode: str = "copy",
    ingest_method: str = "local_owned_copy",
    original_name: str | None = None,
    browser_last_modified_ms: int | None = None,
    source_sha256: str | None = None,
    project_id: str | None = None,
    manifest_project_dir: Path | None = None,
) -> dict[str, Any]:
    """Create one project around an immutable staged source.

    Browser imports use ``source_mode=move`` from a private incoming file. CLI
    projects default to an owned copy so the page editor never serves an
    out-of-project symlink target.
    """
    source = source.expanduser().resolve()
    project_dir = project_dir.expanduser().absolute()
    if not source.is_file():
        raise ValueError(f"input video not found: {source}")
    if subtitle_mode not in SUBTITLE_MODES:
        raise ValueError(f"unsupported subtitle mode: {subtitle_mode}")
    if edit_preset not in EDIT_PRESETS:
        raise ValueError(f"unsupported edit preset: {edit_preset}")
    if emphasis not in {"off", "sparse", "balanced", "dense"}:
        raise ValueError(f"unsupported emphasis density: {emphasis}")
    if visual_density not in {"sparse", "balanced", "dense"}:
        raise ValueError(f"unsupported visual density: {visual_density}")
    if source_mode not in {"copy", "move"}:
        raise ValueError("source_mode must be copy or move")

    media = probe_media(source)
    source_size = source.stat().st_size
    output_target = resolve_output_target(platform, duration_profile, target_duration)
    ensure_new_project(project_dir)
    staged = project_dir / "source" / f"original{source.suffix.lower()}"
    if source_mode == "move":
        os.replace(source, staged)
        original_path: str | None = None
    else:
        shutil.copy2(source, staged)
        original_path = str(source)
    staged.chmod(0o444)

    checksum = source_sha256 or sha256_file(staged)
    if not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise ValueError("source_sha256 must be a lowercase SHA-256 digest")
    target_language = target_language or default_target(source_language)
    created = now_utc()
    stage_state = {stage: "pending" for stage in STAGES}
    stage_state["ingest"] = "complete"
    voice_config = dict(voice or disabled_voice_config())
    if not voice_config.get("enabled"):
        stage_state["voiceover"] = "skipped"

    final_project_dir = (manifest_project_dir or project_dir).expanduser().absolute()
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id or project_dir.name,
        "created_at": created,
        "updated_at": created,
        "project_dir": str(final_project_dir),
        "source": {
            "original_path": original_path,
            "original_name": original_name or source.name,
            "staged_path": str(staged.relative_to(project_dir)),
            "ingest_method": ingest_method,
            "owned_copy": True,
            "immutable": True,
            "sha256": checksum,
            "size_bytes": source_size,
            "browser_last_modified_ms": browser_last_modified_ms,
            "received_at": created if ingest_method == "browser_upload" else None,
            **media,
        },
        "output_target": output_target,
        "editing": {
            "preset": edit_preset,
            **EDIT_PRESETS[edit_preset],
            "delete_earlier_keep_later": True,
            "destructive_review_required": True,
            "retranscribe_after_cut": True,
        },
        "subtitles": {
            "mode": subtitle_mode,
            "source_language": source_language,
            "target_language": target_language,
            "translation_variant": "zh-Hant" if subtitle_mode in {"zh", "bilingual"} else None,
            "style": "rail",
            "emphasis_enabled": emphasis != "off",
            "emphasis_density": emphasis,
        },
        "visuals": {
            "content_match_required": True,
            "cards": cards,
            "related_assets": related_assets,
            "animations": animations,
            "density": visual_density,
            "provenance_required": True,
        },
        "voiceover": voice_config,
        "render": {
            "primary": "auto-edit-video/page-editor-ffmpeg",
            "cut_review": "auto-edit-video/editor",
            "visual_overlay": "talking-head-recut",
            "premium_caption_optional": "embedded-captions",
            "audio_engine": "hyperframes-media",
            "qa": "auto-edit-video/qa-video",
            "capcut": False,
        },
        "stages": stage_state,
        "approvals": {gate: empty_gate() for gate in GATES},
        "artifacts": {
            "profile_context": "video-profile-context.md",
            "transcript_original": "working/transcript_words.json",
            "edit_candidates": "working/edit_candidates.json",
            "edit_decisions": "working/edit_decisions.json",
            "cut_map": "working/cut_map.json",
            "source_cut": "renders/source_cut.mp4",
            "transcript_final": "working/transcript_final.json",
            "emphasis_plan": "working/emphasis_plan.json",
            "visual_plan": "working/visual_plan.json",
            "highlight_plan": "working/highlight_plan.json",
            "edit_review": "review/edit-review.html",
            "timeline_preview": "review/timeline-preview.html",
            "final_video": "renders/final.mp4",
            "qa_report": "qa/qa-report.json",
            "contact_sheet": "qa/final-contact.png",
        },
    }
    write_json(project_dir / "project.json", manifest)
    write_empty_artifacts(project_dir)
    return manifest


def cmd_init(args: argparse.Namespace) -> int:
    source = Path(args.input).expanduser().resolve()
    project_dir = Path(args.project_dir).expanduser().resolve()
    try:
        voice = make_voice_config(args)
        manifest = initialize_project(
            source,
            project_dir,
            source_language=args.source_language,
            subtitle_mode=args.subtitle_mode,
            target_language=args.target_language,
            platform=args.platform,
            duration_profile=args.duration_profile,
            target_duration=args.target_duration,
            edit_preset=args.edit_preset,
            emphasis=args.emphasis,
            visual_density=args.visual_density,
            cards=not args.no_cards,
            related_assets=not args.no_assets,
            animations=not args.no_animations,
            voice=voice,
            source_mode="copy",
            ingest_method="local_owned_copy",
        )
    except ValueError as exc:
        return die(str(exc))
    emit(
        {
            "ok": True,
            "manifest": str(project_dir / "project.json"),
            "project_dir": str(project_dir),
            "voiceover": voice,
            "output_target": manifest["output_target"],
            "next": "materialize video-profile-context.md, then transcribe locally",
        }
    )
    return 0


def cmd_duration_presets(args: argparse.Namespace) -> int:
    emit(
        {
            "profiles": list(DURATION_PROFILE_NAMES),
            "special_modes": ["full", "auto", "custom seconds via --target-duration"],
            "default_without_highlight_request": "full",
            "auto_rule": "select the smallest profile that preserves a complete idea after transcription",
            "presets": DURATION_PRESETS,
            "notes": [
                "These are editorial targets, not a promise of current publishing eligibility.",
                "Re-check platform rules before publishing; publishing is outside this skill phase.",
                "Never pad, stretch, or speed up a short source merely to hit a target.",
            ],
        }
    )
    return 0


def cmd_set_target(args: argparse.Namespace) -> int:
    path = Path(args.manifest).expanduser().resolve()
    try:
        manifest = read_json(path)
        existing = manifest.get("output_target", {})
        if not isinstance(existing, dict):
            existing = {}
        platform = args.platform or existing.get("platform") or "auto"
        duration_profile = (
            args.duration_profile or existing.get("duration_profile") or "auto"
        )
        if duration_profile == "custom":
            if args.target_duration is None:
                raise ValueError(
                    "set-target needs --target-duration to replace a custom target"
                )
            duration_profile = "auto"
        output_target = resolve_output_target(
            platform,
            duration_profile,
            args.target_duration,
        )
    except ValueError as exc:
        return die(str(exc))
    manifest["output_target"] = output_target
    manifest["updated_at"] = now_utc()
    write_json(path, manifest)
    emit({"ok": True, "manifest": str(path), "output_target": output_target})
    return 0


def validate_manifest(manifest: dict[str, Any], manifest_path: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    source = manifest.get("source", {})
    original_value = source.get("original_path") if isinstance(source, dict) else None
    if original_value:
        source_path = Path(str(original_value)).expanduser()
        if not source_path.is_file():
            errors.append(f"source video missing: {source_path}")
    staged_value = str(source.get("staged_path", "")) if isinstance(source, dict) else ""
    staged_path = (manifest_path.parent / staged_value).absolute()
    project_root = manifest_path.parent.absolute()
    if not staged_value or (project_root not in staged_path.parents and staged_path != project_root):
        errors.append("staged source must stay inside the project directory")
    elif not staged_path.is_file():
        errors.append(f"staged source missing: {staged_path}")
    elif source.get("owned_copy") and staged_path.is_symlink():
        errors.append("owned staged source must not be a symlink")
    checksum = source.get("sha256") if isinstance(source, dict) else None
    if checksum is not None and not re.fullmatch(r"[0-9a-f]{64}", str(checksum)):
        errors.append("source sha256 must be a lowercase SHA-256 digest")
    editing = manifest.get("editing", {})
    if not editing.get("destructive_review_required"):
        errors.append("destructive_edit review must remain required")
    if not editing.get("retranscribe_after_cut"):
        errors.append("retranscribe_after_cut must remain enabled")
    render = manifest.get("render", {})
    if render.get("capcut") is not False:
        errors.append("CapCut must remain disabled")

    output_target = manifest.get("output_target")
    if output_target is None:
        warnings.append("output_target is absent; legacy project defaults to full cleanup")
    elif not isinstance(output_target, dict):
        errors.append("output_target must be an object")
    else:
        platform = output_target.get("platform")
        preset_platform = output_target.get("preset_platform")
        duration_profile = output_target.get("duration_profile")
        if platform not in PLATFORMS:
            errors.append(f"invalid output target platform: {platform}")
        if preset_platform not in DURATION_PRESETS:
            errors.append(f"invalid output target preset platform: {preset_platform}")
        if duration_profile not in (*DURATION_PROFILES, "custom"):
            errors.append(f"invalid duration profile: {duration_profile}")
        if output_target.get("publishing_in_scope") is not False:
            errors.append("publishing must remain outside the current skill phase")
        seconds = [
            output_target.get("min_seconds"),
            output_target.get("target_seconds"),
            output_target.get("max_seconds"),
        ]
        numeric_seconds = [value for value in seconds if isinstance(value, (int, float))]
        if len(numeric_seconds) not in {0, 3}:
            errors.append("duration bounds must be all numeric or all null")
        elif numeric_seconds and not (
            0 < float(numeric_seconds[0])
            <= float(numeric_seconds[1])
            <= float(numeric_seconds[2])
        ):
            errors.append("duration bounds must satisfy 0 < min <= target <= max")

    subtitles = manifest.get("subtitles", {})
    mode = subtitles.get("mode")
    if mode not in SUBTITLE_MODES:
        errors.append(f"invalid subtitle mode: {mode}")
    source_language = subtitles.get("source_language")
    target_language = subtitles.get("target_language")
    if mode == "bilingual":
        if source_language == "auto":
            warnings.append("detect source language before translating bilingual subtitles")
        elif language_family(source_language) == language_family(target_language):
            errors.append("bilingual source and target languages must differ")

    voice = manifest.get("voiceover", {})
    if voice.get("enabled"):
        provider = voice.get("provider")
        if provider not in VOICE_PROVIDERS:
            errors.append(f"invalid voice provider: {provider}")
        if voice.get("language") not in VOICE_LANGUAGES:
            errors.append(f"invalid voice language: {voice.get('language')}")
        if voice.get("gender") not in {"female", "male"}:
            errors.append("voice gender must be female or male")
        speed = voice.get("speed")
        if not isinstance(speed, (int, float)) or not 0.7 <= float(speed) <= 1.5:
            errors.append("voice speed must be between 0.7 and 1.5")
        if not voice.get("voice_id"):
            errors.append("pin a provider-specific voice_id before synthesis")
        if provider == "auto":
            errors.append("provider=auto cannot guarantee selected gender; resolve and pin provider")
        if provider == "edge" and not shutil.which("edge-tts"):
            errors.append("edge-tts is not installed")
        if provider == "rumi":
            if not PATHS["rumi_voice_system"].is_file():
                errors.append("Rumi voice system is missing")
            elif not rumi_voice_allowed(str(voice.get("voice_id", ""))):
                errors.append("Rumi voice_id is not in the shared safe catalog")
            else:
                details = rumi_voice_details(str(voice.get("voice_id")))
                if details and details["gender"] != voice.get("gender"):
                    errors.append(
                        "selected Rumi-system voice does not match the requested gender"
                    )
                if (
                    isinstance(speed, (int, float))
                    and rumi_backend(str(voice.get("voice_id"))) == "fish"
                    and float(speed) != 1.0
                ):
                    warnings.append("Fish voice speed will be applied after synthesis with ffmpeg")
        if provider not in {"rumi", "edge"} and not PATHS["hyperframes_audio"].is_file():
            errors.append("hyperframes-media audio engine is missing")
        if voice.get("cloud"):
            warnings.append("voiceover sends narration text to a cloud service; require consent")

    project_dir = Path(str(manifest.get("project_dir", manifest_path.parent))).expanduser()
    if project_dir.resolve() != manifest_path.parent.resolve():
        warnings.append("project_dir differs from the manifest directory")
    for name in ("source", "working", "review", "subtitles", "voice", "assets", "renders", "qa"):
        if not (manifest_path.parent / name).is_dir():
            errors.append(f"missing project directory: {name}")
    return errors, warnings


def cmd_validate(args: argparse.Namespace) -> int:
    path = Path(args.manifest).expanduser().resolve()
    try:
        manifest = read_json(path)
    except ValueError as exc:
        return die(str(exc))
    errors, warnings = validate_manifest(manifest, path)
    emit({"valid": not errors, "errors": errors, "warnings": warnings, "manifest": str(path)})
    return 0 if not errors else 2


def parse_edge_voices(output: str) -> list[dict[str, str]]:
    voices: list[dict[str, str]] = []
    for line in output.splitlines():
        match = re.match(r"^([a-z]{2}-[A-Z]{2}(?:-[\w-]+)?Neural)\s+(Female|Male)\b", line)
        if not match:
            continue
        voice_id, gender = match.groups()
        locale_parts = voice_id.split("-")
        voices.append(
            {
                "provider": "edge",
                "language": "-".join(locale_parts[:2]),
                "gender": gender.lower(),
                "voice_id": voice_id,
            }
        )
    return voices


def cmd_voices(args: argparse.Namespace) -> int:
    warnings: list[str] = []
    try:
        voices = rumi_catalog_entries()
    except ValueError as exc:
        warnings.append(str(exc))
        voices = [
            {
                "provider": "edge",
                "voice_system": "standalone",
                "backend": "edge",
                "language": language,
                "gender": gender,
                "voice_id": voice,
                "description": "built-in Edge fallback",
                "default": False,
            }
            for language, gender_map in VOICE_PRESETS.items()
            for gender, voice in gender_map.items()
        ]
    if args.live:
        command = shutil.which("edge-tts")
        if not command:
            warnings.append("edge-tts is not installed; showing shared catalog")
        else:
            try:
                result = subprocess.run(
                    [command, "--list-voices"],
                    text=True,
                    capture_output=True,
                    timeout=45,
                )
                live = parse_edge_voices(result.stdout) if result.returncode == 0 else []
                if live:
                    voices = [item for item in voices if item["backend"] != "edge"] + [
                        {
                            **item,
                            "voice_system": "standalone",
                            "backend": "edge",
                            "description": "live Edge catalog",
                            "default": False,
                        }
                        for item in live
                    ]
                else:
                    warnings.append(
                        result.stderr.strip() or "live Edge catalog unavailable; showing shared catalog"
                    )
            except subprocess.TimeoutExpired:
                warnings.append("live Edge catalog timed out; showing shared catalog")
    if args.language:
        family = language_family(args.language)
        voices = [
            item
            for item in voices
            if item["language"] == args.language
            or (item["provider"] == "rumi" and item["language"] == family)
        ]
    if args.gender:
        voices = [item for item in voices if item["gender"] == args.gender]
    if args.provider:
        voices = [item for item in voices if item["provider"] == args.provider]
    emit(
        {
            "defaults": {
                "zh": {
                    "provider": "rumi" if PATHS["rumi_voice_system"].is_file() else "edge",
                    "voice_id": "safe catalog selection"
                    if PATHS["rumi_voice_system"].is_file()
                    else "locale/gender preset",
                },
                "en": {"provider": "edge", "voice_id": "locale/gender preset"},
            },
            "voices": voices,
            "warning": "; ".join(warnings) if warnings else None,
        }
    )
    return 0


def split_tts_lines(text: str, max_chars: int = 420) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    result: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph) <= max_chars:
            result.append(paragraph)
            continue
        sentences = [
            part.strip()
            for part in re.split(r"(?<=[。！？!?\.])\s*", paragraph)
            if part.strip()
        ]
        buffer = ""
        for sentence in sentences or [paragraph]:
            if buffer and len(buffer) + len(sentence) + 1 > max_chars:
                result.append(buffer)
                buffer = sentence
            else:
                buffer = f"{buffer} {sentence}".strip()
        if buffer:
            result.append(buffer)
    return result


def normalized_token(text: str) -> str:
    return re.sub(r"[\s，。！？、,.!?：:；;\-—…]+", "", text).lower()


def whisper_payload(data: dict[str, Any], duration_s: float) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    segments: list[dict[str, Any]] = []
    flat_words: list[dict[str, Any]] = []
    for segment_index, raw_segment in enumerate(data.get("segments", []), start=1):
        raw_words = raw_segment.get("words", [])
        words: list[dict[str, Any]] = []
        for raw_word in raw_words:
            text = str(raw_word.get("word", "")).strip()
            if not text:
                continue
            start = round(float(raw_word.get("start", raw_segment.get("start", 0.0))), 3)
            end = round(float(raw_word.get("end", raw_segment.get("end", start))), 3)
            probability = raw_word.get("probability")
            confidence = None
            if isinstance(probability, (int, float)) and math.isfinite(float(probability)):
                confidence = round(float(probability), 4)
            word = {
                "id": f"word-{len(flat_words) + 1:05d}",
                "text": text,
                "start": start,
                "end": end,
                "confidence": confidence,
                "segment_id": f"segment-{segment_index:04d}",
            }
            words.append(word)
            flat_words.append(word)
        segments.append(
            {
                "id": f"segment-{segment_index:04d}",
                "start": round(float(raw_segment.get("start", 0.0)), 3),
                "end": round(float(raw_segment.get("end", 0.0)), 3),
                "text": str(raw_segment.get("text", "")).strip(),
                "words": words,
            }
        )

    compatibility: list[dict[str, Any]] = []
    cursor = 0.0
    for word in flat_words:
        if word["start"] - cursor >= 0.08:
            compatibility.append(
                {
                    "id": f"gap-{len(compatibility) + 1:05d}",
                    "text": "",
                    "start": round(cursor, 3),
                    "end": word["start"],
                    "isGap": True,
                    "reason": "whisper word gap",
                }
            )
        compatibility.append(
            {
                "id": word["id"],
                "text": word["text"],
                "start": word["start"],
                "end": word["end"],
                "isGap": False,
                "confidence": word["confidence"],
            }
        )
        cursor = max(cursor, float(word["end"]))
    if duration_s - cursor >= 0.08:
        compatibility.append(
            {
                "id": f"gap-{len(compatibility) + 1:05d}",
                "text": "",
                "start": round(cursor, 3),
                "end": round(duration_s, 3),
                "isGap": True,
                "reason": "untranscribed tail",
            }
        )

    transcript = {
        "schema_version": SCHEMA_VERSION,
        "engine": "openai-whisper",
        "language": data.get("language"),
        "duration_s": round(duration_s, 3),
        "text": str(data.get("text", "")).strip(),
        "segments": segments,
        "words": flat_words,
    }
    return transcript, compatibility


def invalidate_approvals(
    manifest: dict[str, Any],
    gates: tuple[str, ...],
    note: str,
) -> list[str]:
    invalidated: list[str] = []
    approvals = manifest.setdefault("approvals", {})
    for gate in gates:
        previous = approvals.get(gate)
        if isinstance(previous, dict) and previous.get("approved"):
            invalidated.append(gate)
        approvals[gate] = {
            "approved": False,
            "confirmed_by": None,
            "at": None,
            "note": note,
            "invalidated_at": now_utc(),
        }
    return invalidated


def sync_transcript_to_editor(project_dir: Path) -> int:
    state_path = project_dir / "working/editor_state.json"
    if not state_path.is_file():
        return 0
    state = read_json(state_path)
    transcript = read_json(project_dir / "working/transcript_words.json")
    manifest = read_json(project_dir / "project.json")
    try:
        from editor_server import (
            DIRECTOR_PRESETS,
            caption_effect_spans,
            editor_state_revision,
            effect_keywords_for_caption,
        )
    except ImportError as exc:
        raise ValueError(f"cannot load editor transcript bridge: {exc}") from exc
    director_id = str(state.get("director_style") or "teacher-punch")
    director = DIRECTOR_PRESETS.get(director_id, DIRECTOR_PRESETS["teacher-punch"])
    caption_style = dict(state.get("caption_defaults") or director["caption"])
    emphasis_plan = read_json(project_dir / "working/emphasis_plan.json")
    preserved = [
        overlay
        for overlay in state.get("overlays", [])
        if not (
            isinstance(overlay, dict)
            and overlay.get("type") == "caption"
            and overlay.get("source") == "working/transcript_words.json"
        )
    ]
    captions: list[dict[str, Any]] = []
    for index, segment in enumerate(transcript.get("segments", []), start=1):
        text = str(segment.get("text", "")).strip()
        if not text:
            continue
        segment_start = round(float(segment.get("start", 0.0)), 3)
        segment_end = round(float(segment.get("end", 0.0)), 3)
        effects = caption_effect_spans(
            emphasis_plan,
            text,
            segment_start,
            segment_end,
            str(caption_style.get("emphasis_color") or "#ffd447"),
            effect_keywords_for_caption(state, segment_start, segment_end),
        )
        captions.append(
            {
                "id": f"caption-{index:04d}",
                "type": "caption",
                "start": segment_start,
                "end": segment_end,
                "text": text,
                "emphasis": [item["text"] for item in effects],
                "effect_spans": effects,
                "visible": True,
                "locked": False,
                "z_index": 20,
                "style": dict(caption_style),
                "source": "working/transcript_words.json",
                "provenance": "local-whisper draft; requires transcript review",
            }
        )
    state["overlays"] = captions + preserved
    state["source_sha256"] = manifest.get("source", {}).get("sha256")
    state.setdefault("review", {})["selected_overlay_id"] = captions[0]["id"] if captions else (preserved[0].get("id") if preserved else None)
    state["updated_at"] = now_utc()
    state["revision"] = editor_state_revision(state)
    write_json(state_path, state)
    return len(captions)


def sync_highlight_plan_to_editor(project_dir: Path, plan: dict[str, Any]) -> int:
    state_path = project_dir / "working/editor_state.json"
    if not state_path.is_file():
        return 0
    state = read_json(state_path)
    manifest = read_json(project_dir / "project.json")
    try:
        from editor_server import DIRECTOR_PRESETS, editor_state_revision
    except ImportError as exc:
        raise ValueError(f"cannot load editor highlight bridge: {exc}") from exc
    highlights = [
        {
            "id": str(item["id"]),
            "plan_item_id": str(item["id"]),
            "start": item["start"],
            "end": item["end"],
            "title": str(item["title"]),
            "review_status": str(item["review_status"]),
            "score": item["score"],
            "source": "working/highlight_plan.json",
        }
        for item in plan.get("items", [])[:10]
    ]
    state["highlights"] = highlights
    state["highlight_plan_revision"] = plan.get("plan_revision")
    state["source_sha256"] = manifest.get("source", {}).get("sha256")
    state["active_highlight_id"] = highlights[0]["id"] if highlights else None
    configuration = plan.get("configuration") if isinstance(plan.get("configuration"), dict) else {}
    director_id = str(configuration.get("director_profile", "teacher-punch"))
    if director_id in DIRECTOR_PRESETS:
        caption_style = dict(DIRECTOR_PRESETS[director_id]["caption"])
        state["director_style"] = director_id
        state["caption_defaults"] = caption_style
        generated_sources = {"working/transcript_words.json"}
        for overlay in state.get("overlays", []):
            if not isinstance(overlay, dict) or overlay.get("source") not in generated_sources:
                continue
            if overlay.get("type") != "caption":
                continue
            overlay["style"] = {**dict(overlay.get("style") or {}), **caption_style}
        preserved = [
            overlay
            for overlay in state.get("overlays", [])
            if not (
                isinstance(overlay, dict)
                and overlay.get("type") != "caption"
                and overlay.get("source")
                in {
                    "working/emphasis_plan.json",
                    "working/visual_plan.json",
                    "working/highlight_visual_plan.json",
                }
            )
        ]
        transcript = read_json(project_dir / "working/transcript_words.json")
        design_overlays: list[dict[str, Any]] = []
        for highlight in highlights:
            design_overlays.extend(
                build_highlight_design_overlays(
                    transcript,
                    highlight,
                    caption_style,
                    director_id,
                )
            )
        state["overlays"] = preserved + design_overlays
        state.setdefault("canvas", {})["fit"] = "contain"
        state["visual_quality_mode"] = "designed"
        state["graphic_package_style"] = "craft-stack"
        write_json(
            project_dir / "working/highlight_visual_plan.json",
            {
                "schema_version": 1,
                "generator": "highlight-scoped-designed-cards-v1",
                "highlight_plan_revision": plan.get("plan_revision"),
                "items": design_overlays,
            },
        )
    state["editing_brief"] = str(configuration.get("editing_brief", ""))[:2000]
    state["updated_at"] = now_utc()
    state["revision"] = editor_state_revision(state)
    write_json(state_path, state)
    return len(highlights)


def import_whisper_artifacts(
    manifest_path: Path,
    whisper_path: Path,
    *,
    srt_path: Path | None,
    model: str,
) -> dict[str, Any]:
    manifest = read_json(manifest_path)
    data = read_json(whisper_path)
    if not isinstance(data.get("segments"), list):
        raise ValueError("Whisper JSON must contain a segments array")
    duration_s = float(manifest.get("source", {}).get("duration_s", 0.0))
    transcript, compatibility = whisper_payload(data, duration_s)
    project_dir = manifest_path.parent
    transcript_path = project_dir / "working/transcript_words.json"
    compatibility_path = project_dir / "working/subtitles_words.json"
    write_json(transcript_path, transcript)
    write_json(compatibility_path, compatibility)
    if srt_path is not None:
        srt_path = srt_path.expanduser().resolve()
        if not srt_path.is_file():
            raise ValueError(f"SRT not found: {srt_path}")
        shutil.copy2(srt_path, project_dir / "subtitles/source.srt")
    manifest.setdefault("stages", {})["transcribe"] = "complete"
    manifest["stages"]["edit_analysis"] = "pending"
    manifest.setdefault("artifacts", {})["transcript_compatibility"] = (
        "working/subtitles_words.json"
    )
    manifest["transcription"] = {
        "engine": "openai-whisper",
        "model": model,
        "language": data.get("language"),
        "word_count": len(transcript["words"]),
        "segment_count": len(transcript["segments"]),
        "source_json": str(whisper_path),
        "imported_at": now_utc(),
    }
    manifest["updated_at"] = now_utc()
    invalidate_approvals(
        manifest,
        ("highlight_selection", "timeline", "final"),
        "Invalidated because the source transcript changed",
    )
    write_json(manifest_path, manifest)
    synced = sync_transcript_to_editor(project_dir)
    return {
        "ok": True,
        "transcript": str(transcript_path),
        "compatibility": str(compatibility_path),
        "words": len(transcript["words"]),
        "segments": len(transcript["segments"]),
        "synced_editor_captions": synced,
    }


def cmd_import_whisper(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).expanduser().resolve()
    whisper_path = Path(args.whisper_json).expanduser().resolve()
    try:
        payload = import_whisper_artifacts(
            manifest_path,
            whisper_path,
            srt_path=Path(args.srt) if args.srt else None,
            model=args.model,
        )
    except ValueError as exc:
        return die(str(exc))
    emit(payload)
    return 0


def project_staged_source(manifest_path: Path, manifest: dict[str, Any]) -> Path:
    project_dir = manifest_path.parent.resolve()
    relative = Path(str(manifest.get("source", {}).get("staged_path", "")))
    if relative.is_absolute():
        raise ValueError("staged source must be project-relative")
    entry = project_dir / relative
    if entry.is_symlink():
        raise ValueError("staged source must be an owned regular file")
    source = entry.resolve()
    if project_dir not in source.parents or not source.is_file():
        raise ValueError("staged source is missing or outside the project")
    return source


def cmd_transcribe_local(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).expanduser().resolve()
    try:
        manifest = read_json(manifest_path)
        source = project_staged_source(manifest_path, manifest)
    except ValueError as exc:
        return die(str(exc))
    whisper = os.environ.get("WHISPER_BIN", "").strip() or shutil.which("whisper")
    if not whisper:
        return die("local Whisper CLI is not installed")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", args.model):
        return die("Whisper model name is invalid")
    run_dir = manifest_path.parent / "working/whisper-local" / uuid.uuid4().hex
    run_dir.mkdir(parents=True, exist_ok=False)
    language = str(manifest.get("subtitles", {}).get("source_language", "auto"))
    language_map = {"zh-TW": "zh", "zh-CN": "zh", "en-US": "en", "en-GB": "en"}
    command = [
        whisper,
        str(source),
        "--model",
        args.model,
        "--output_dir",
        str(run_dir),
        "--output_format",
        "all",
        "--word_timestamps",
        "True",
        "--verbose",
        "False",
        "--fp16",
        "False",
    ]
    if language in language_map:
        command.extend(["--language", language_map[language]])
    manifest.setdefault("stages", {})["transcribe"] = "in_progress"
    manifest["updated_at"] = now_utc()
    write_json(manifest_path, manifest)
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=args.timeout,
        )
    except subprocess.TimeoutExpired:
        manifest = read_json(manifest_path)
        manifest.setdefault("stages", {})["transcribe"] = "failed"
        manifest["transcription"] = {
            "engine": "openai-whisper",
            "model": args.model,
            "status": "failed",
            "error_code": "timeout",
            "updated_at": now_utc(),
        }
        write_json(manifest_path, manifest)
        return die("local Whisper transcription timed out")
    if result.returncode != 0:
        manifest = read_json(manifest_path)
        manifest.setdefault("stages", {})["transcribe"] = "failed"
        manifest["transcription"] = {
            "engine": "openai-whisper",
            "model": args.model,
            "status": "failed",
            "error_code": "whisper_failed",
            "updated_at": now_utc(),
        }
        write_json(manifest_path, manifest)
        return die("local Whisper transcription failed")
    whisper_json = run_dir / f"{source.stem}.json"
    whisper_srt = run_dir / f"{source.stem}.srt"
    try:
        payload = import_whisper_artifacts(
            manifest_path,
            whisper_json,
            srt_path=whisper_srt if whisper_srt.is_file() else None,
            model=args.model,
        )
    except ValueError as exc:
        return die(str(exc))
    payload["local"] = True
    emit(payload)
    return 0


def cmd_plan_highlights(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).expanduser().resolve()
    project_dir = manifest_path.parent
    try:
        manifest = read_json(manifest_path)
        transcript = read_json(project_dir / "working/transcript_words.json")
    except ValueError:
        try:
            manifest = read_json(manifest_path)
        except ValueError as exc:
            return die(str(exc))
        transcript = {"schema_version": 1, "text": "", "segments": [], "words": []}
    try:
        plan = build_highlight_plan(
            transcript,
            manifest,
            director_profile=args.director,
            requested_count=args.count,
            editing_brief=args.brief or "",
        )
    except ValueError as exc:
        return die(str(exc))
    errors = validate_highlight_plan(
        plan,
        float(manifest.get("source", {}).get("duration_s", 0.0)),
    )
    if errors:
        return die("; ".join(errors))
    output = project_dir / "working/highlight_plan.json"
    write_json(output, plan)
    previous_revision = manifest.get("highlight_planning", {}).get("plan_revision")
    manifest.setdefault("artifacts", {})["highlight_plan"] = "working/highlight_plan.json"
    manifest.setdefault("stages", {})["highlight_plan"] = plan["status"]
    manifest["highlight_planning"] = {
        "generator": plan["generator"],
        "director_profile": args.director,
        "requested_count": args.count,
        "plan_revision": plan["plan_revision"],
        "status": plan["status"],
        "updated_at": now_utc(),
    }
    if previous_revision != plan["plan_revision"]:
        invalidate_approvals(
            manifest,
            ("highlight_selection", "timeline", "final"),
            "Invalidated because the highlight plan changed",
        )
        manifest["approvals"]["highlight_selection"]["plan_revision"] = plan["plan_revision"]
    manifest["updated_at"] = now_utc()
    write_json(manifest_path, manifest)
    synced = sync_highlight_plan_to_editor(project_dir, plan)
    emit(
        {
            "ok": plan["status"] == "needs_review",
            "status": plan["status"],
            "plan": str(output),
            "plan_revision": plan["plan_revision"],
            "items": len(plan["items"]),
            "synced_editor_highlights": synced,
            "warnings": plan["warnings"],
        }
    )
    return 0 if plan["status"] == "needs_review" else 3


def new_edit_candidate(
    candidates: list[dict[str, Any]],
    kind: str,
    start: float,
    end: float,
    text: str,
    risk: str,
    reason: str,
    word_ids: list[str],
    default_action: str = "delete",
) -> None:
    candidates.append(
        {
            "id": f"edit-{len(candidates) + 1:04d}",
            "type": kind,
            "start": round(start, 3),
            "end": round(end, 3),
            "text": text,
            "risk": risk,
            "reason": reason,
            "word_ids": word_ids,
            "default_action": default_action,
            "review_status": "pending",
        }
    )


def analyze_edit_candidates(
    transcript: dict[str, Any],
    compatibility: list[dict[str, Any]],
    silence_threshold_s: float,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    words = [item for item in transcript.get("words", []) if isinstance(item, dict)]
    if not words:
        return candidates
    for item in compatibility:
        if item.get("isGap") and float(item["end"]) - float(item["start"]) >= silence_threshold_s:
            reason = str(item.get("reason") or "pause above preset threshold")
            new_edit_candidate(
                candidates,
                "silence",
                float(item["start"]),
                float(item["end"]),
                "",
                "low",
                reason,
                [str(item.get("id"))],
            )

    for index, word in enumerate(words):
        token = normalized_token(str(word.get("text", "")))
        if token in FILLER_TOKENS:
            new_edit_candidate(
                candidates,
                "filler",
                float(word["start"]),
                float(word["end"]),
                str(word["text"]),
                "low",
                "standalone hesitation; keep if it carries discourse meaning",
                [str(word["id"])],
            )
        if index + 1 >= len(words) or not token:
            continue
        next_word = words[index + 1]
        next_token = normalized_token(str(next_word.get("text", "")))
        gap = float(next_word["start"]) - float(word["end"])
        if token == next_token and len(token) <= 4 and gap <= 0.35:
            new_edit_candidate(
                candidates,
                "stutter",
                float(word["start"]),
                float(word["end"]),
                str(word["text"]),
                "low",
                "immediate duplicate; delete earlier and keep later",
                [str(word["id"])],
            )
    return sorted(candidates, key=lambda item: (item["start"], item["end"], item["id"]))


def cmd_analyze_edits(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).expanduser().resolve()
    project_dir = manifest_path.parent
    try:
        manifest = read_json(manifest_path)
        transcript = read_json(project_dir / "working/transcript_words.json")
        raw_compatibility = json.loads(
            (project_dir / "working/subtitles_words.json").read_text(encoding="utf-8")
        )
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
        return die(str(exc))
    if not isinstance(raw_compatibility, list):
        return die("working/subtitles_words.json must be a JSON array")
    threshold = float(manifest.get("editing", {}).get("silence_threshold_s", 0.3))
    candidates = analyze_edit_candidates(transcript, raw_compatibility, threshold)
    has_word_timestamps = bool(transcript.get("words"))
    payload = {
        "schema_version": SCHEMA_VERSION,
        "source": "working/transcript_words.json",
        "generated_at": now_utc(),
        "detector": "deterministic-low-risk-v1",
        "warning": None
        if has_word_timestamps
        else "word timestamps unavailable; destructive edit proposals were skipped",
        "items": candidates,
    }
    decisions = {
        "schema_version": SCHEMA_VERSION,
        "approved_from": None,
        "items": [
            {
                "candidate_id": item["id"],
                "action": item["default_action"],
                "review_status": "pending",
            }
            for item in candidates
        ],
    }
    write_json(project_dir / "working/edit_candidates.json", payload)
    write_json(project_dir / "working/edit_decisions.json", decisions)
    auto_selected: list[int] = []
    selected_ids = {word_id for item in candidates for word_id in item.get("word_ids", [])}
    for index, item in enumerate(raw_compatibility):
        if str(item.get("id")) in selected_ids:
            auto_selected.append(index)
    write_json(project_dir / "working/auto_selected.json", auto_selected)
    manifest.setdefault("stages", {})["edit_analysis"] = "complete"
    manifest["stages"]["edit_review"] = "needs_review"
    manifest["updated_at"] = now_utc()
    write_json(manifest_path, manifest)
    counts: dict[str, int] = {}
    for item in candidates:
        counts[item["type"]] = counts.get(item["type"], 0) + 1
    emit(
        {
            "ok": True,
            "candidates": len(candidates),
            "counts": counts,
            "review_required": True,
            "warning": payload["warning"],
            "output": str(project_dir / "working/edit_candidates.json"),
        }
    )
    return 0


EMPHASIS_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\d+(?:\.\d+)?\s*(?:%|％|倍|年|個|个|只|種|种|秒|分鐘|分钟)?"), "numeric claim"),
    (re.compile(r"(?:最大|最小|最多|最少|第一|唯一|關鍵|关键|重點|重点|注意|一定|必須|必须)"), "priority phrase"),
    (re.compile(r"(?:差別|差别|不同|相比|比較|比较|不是|而是|一樣|一样|不好|不能)"), "contrast or conclusion"),
)


def trim_plan_text(text: str, limit: int = 26) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    return compact if len(compact) <= limit else compact[:limit].rstrip() + "…"


def phrase_timing(segment: dict[str, Any], phrase: str) -> tuple[float, float]:
    text = str(segment.get("text", ""))
    start = float(segment.get("start", 0.0))
    end = max(start + 0.05, float(segment.get("end", start + 0.5)))
    index = max(0, text.find(phrase))
    denominator = max(1, len(text))
    phrase_start = start + (index / denominator) * (end - start)
    phrase_end = phrase_start + max(0.35, (len(phrase) / denominator) * (end - start))
    return round(phrase_start, 3), round(min(end, phrase_end), 3)


def build_emphasis_plan(transcript: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    last_start = -10.0
    for segment in transcript.get("segments", []):
        text = str(segment.get("text", "")).strip()
        if not text:
            continue
        match: re.Match[str] | None = None
        reason = ""
        for pattern, candidate_reason in EMPHASIS_PATTERNS:
            match = pattern.search(text)
            if match:
                reason = candidate_reason
                break
        if not match:
            continue
        phrase = match.group(0).strip()
        start, end = phrase_timing(segment, phrase)
        if start - last_start < 1.2:
            continue
        items.append(
            {
                "id": f"em-{len(items) + 1:04d}",
                "start": start,
                "end": end,
                "text": phrase,
                "reason": reason,
                "treatment": "accent-pop",
                "scope": "phrase",
                "transcript_evidence": text,
                "provenance": "deterministic local transcript proposal",
                "review_status": "pending",
            }
        )
        last_start = start
        if len(items) >= 12:
            break
    return items


def build_visual_plan(transcript: dict[str, Any], duration_s: float) -> list[dict[str, Any]]:
    segments = [item for item in transcript.get("segments", []) if str(item.get("text", "")).strip()]
    if not segments or duration_s <= 0:
        return []
    items: list[dict[str, Any]] = []
    first = segments[0]
    first_start = max(0.0, float(first.get("start", 0.0)))
    first_end = min(duration_s, max(first_start + 0.4, first_start + 2.2))
    items.append(
        {
            "id": "visual-0001",
            "start": round(first_start, 3),
            "end": round(first_end, 3),
            "type": "title_card",
            "text": trim_plan_text(str(first.get("text", "")), 22),
            "purpose": "opening hook from the first spoken idea",
            "transcript_evidence": str(first.get("text", "")).strip(),
            "source": None,
            "provenance": "generated local text card from unverified transcript",
            "fallback": "keep subtitle only",
            "review_status": "pending",
        }
    )
    last_start = first_start
    for segment in segments[1:]:
        text = str(segment.get("text", "")).strip()
        start = max(0.0, float(segment.get("start", 0.0)))
        if start - last_start < 3.0:
            continue
        numeric = re.search(r"\d+(?:\.\d+)?\s*(?:%|％|倍|年|個|个|只|種|种)?", text)
        contrast = re.search(r"差別|差别|不同|相比|比較|比较|不是|而是|一樣|一样", text)
        if not numeric and not contrast:
            continue
        card_type = "data_card" if numeric else "animation"
        card_text = numeric.group(0).strip() if numeric else trim_plan_text(text, 24)
        end = min(duration_s, max(start + 0.8, float(segment.get("end", start + 1.8))))
        items.append(
            {
                "id": f"visual-{len(items) + 1:04d}",
                "start": round(start, 3),
                "end": round(end, 3),
                "type": card_type,
                "text": card_text,
                "purpose": "surface a numeric claim" if numeric else "explain a comparison",
                "transcript_evidence": text,
                "source": None,
                "provenance": "generated local text card from unverified transcript",
                "fallback": "keep subtitle only",
                "review_status": "pending",
            }
        )
        last_start = start
        if len(items) >= 5:
            break
    return items


def sync_plans_to_editor(project_dir: Path, duration_s: float) -> int:
    state_path = project_dir / "working/editor_state.json"
    if not state_path.is_file():
        return 0
    state = read_json(state_path)
    try:
        from editor_server import artifact_plan_overlays, editor_state_revision
    except ImportError as exc:
        raise ValueError(f"cannot load editor plan bridge: {exc}") from exc
    sources = {"working/emphasis_plan.json", "working/visual_plan.json"}
    retained = [
        item
        for item in state.get("overlays", [])
        if str(item.get("source", "")) not in sources
    ]
    generated = artifact_plan_overlays(
        project_dir,
        dict(state.get("caption_defaults") or {}),
        duration_s,
    )
    state["overlays"] = retained + generated
    state["updated_at"] = now_utc()
    state["revision"] = editor_state_revision(state)
    write_json(state_path, state)
    return len(generated)


def cmd_plan_overlays(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).expanduser().resolve()
    project_dir = manifest_path.parent
    try:
        manifest = read_json(manifest_path)
        transcript = read_json(project_dir / "working/transcript_words.json")
    except ValueError as exc:
        return die(str(exc))
    if not isinstance(transcript.get("segments"), list):
        return die("working/transcript_words.json must contain a segments array")
    duration_s = float(manifest.get("source", {}).get("duration_s", 0.0))
    emphasis = build_emphasis_plan(transcript)
    visuals = build_visual_plan(transcript, duration_s)
    write_json(
        project_dir / "working/emphasis_plan.json",
        {
            "schema_version": SCHEMA_VERSION,
            "generator": "deterministic-local-emphasis-v1",
            "generated_at": now_utc(),
            "items": emphasis,
        },
    )
    write_json(
        project_dir / "working/visual_plan.json",
        {
            "schema_version": SCHEMA_VERSION,
            "generator": "deterministic-local-card-v1",
            "generated_at": now_utc(),
            "items": visuals,
        },
    )
    try:
        synced = sync_plans_to_editor(project_dir, duration_s)
    except ValueError as exc:
        return die(str(exc))
    stages = manifest.setdefault("stages", {})
    stages["emphasis"] = "needs_review" if emphasis else "skipped"
    stages["visual_plan"] = "needs_review" if visuals else "skipped"
    stages["timeline_review"] = "needs_review"
    invalidated: list[str] = []
    for gate in ("timeline", "final"):
        approval = manifest.setdefault("approvals", {}).get(gate)
        if isinstance(approval, dict) and approval.get("approved"):
            manifest["approvals"][gate] = {
                "approved": False,
                "confirmed_by": None,
                "at": None,
                "note": "Invalidated because a new overlay plan was generated",
                "invalidated_at": now_utc(),
            }
            invalidated.append(gate)
    manifest["updated_at"] = now_utc()
    write_json(manifest_path, manifest)
    emit(
        {
            "ok": True,
            "emphasis_items": len(emphasis),
            "visual_items": len(visuals),
            "synced_editor_overlays": synced,
            "invalidated_gates": invalidated,
            "review_required": True,
        }
    )
    return 0


def cmd_audio_request(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).expanduser().resolve()
    script_path = Path(args.script).expanduser().resolve()
    try:
        manifest = read_json(manifest_path)
        text = script_path.read_text(encoding="utf-8").strip()
    except (ValueError, FileNotFoundError) as exc:
        return die(str(exc))
    if not text:
        return die("narration script is empty")
    voice = manifest.get("voiceover", {})
    if not voice.get("enabled"):
        return die("voiceover is disabled in the manifest")
    if voice.get("provider") in {"rumi", "edge"}:
        return die(
            "Rumi/Edge use their dedicated synthesize command; "
            "audio-request is for the HyperFrames engine"
        )
    if not voice.get("voice_id"):
        return die("pin a provider-specific voice_id first")
    lines = split_tts_lines(text)
    request = {
        "provider": voice["provider"],
        "voice": voice["voice_id"],
        "lang": language_family(voice["language"]),
        "speed": voice["speed"],
        "lines": [
            {"id": f"voice-{index:03d}", "text": line}
            for index, line in enumerate(lines, start=1)
        ],
        "bgm": {"mode": "none"},
    }
    output = (
        Path(args.output).expanduser().resolve()
        if args.output
        else manifest_path.parent / "voice/audio_request.json"
    )
    write_json(output, request)
    emit({"ok": True, "output": str(output), "line_count": len(lines), "request": request})
    return 0


def edge_rate(speed: float) -> str:
    percent = round((speed - 1.0) * 100)
    return f"{percent:+d}%"


def manifest_media_path(path: Path, project_dir: Path) -> str:
    try:
        return str(path.relative_to(project_dir))
    except ValueError:
        return str(path)


def cmd_synthesize_rumi(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).expanduser().resolve()
    script_path = Path(args.script).expanduser().resolve()
    try:
        manifest = read_json(manifest_path)
        text = script_path.read_text(encoding="utf-8").strip()
    except (ValueError, FileNotFoundError) as exc:
        return die(str(exc))
    voice = manifest.get("voiceover", {})
    if not voice.get("enabled") or voice.get("provider") != "rumi":
        return die("manifest voiceover provider must be rumi")
    if not args.allow_cloud and not args.dry_run:
        return die(
            "Rumi voice system sends script text to Fish Audio or Edge; "
            "re-run with --allow-cloud after consent"
        )
    if not text:
        return die("narration script is empty")
    voice_id = str(voice.get("voice_id", ""))
    if not rumi_voice_allowed(voice_id):
        return die("voice_id is not allowed by the shared Rumi voice catalog")
    voice_system = PATHS["rumi_voice_system"]
    if not voice_system.is_file():
        return die(f"Rumi voice system is missing: {voice_system}")

    output_dir = manifest_path.parent / "voice"
    output_dir.mkdir(parents=True, exist_ok=True)
    narration_txt = output_dir / "narration.txt"
    if narration_txt.resolve() != script_path:
        narration_txt.write_text(text + "\n", encoding="utf-8")
    media = Path(args.output).expanduser().resolve() if args.output else output_dir / "narration.mp3"
    media.parent.mkdir(parents=True, exist_ok=True)
    speed = float(voice["speed"])
    backend = rumi_backend(voice_id)
    apply_post_speed = backend == "fish" and speed != 1.0
    synth_target = output_dir / "narration.rumi-source.mp3" if apply_post_speed else media
    cmd = [
        sys.executable,
        str(voice_system),
        "--text",
        text,
        "--voice",
        voice_id,
        "--out",
        str(synth_target),
        "--rate",
        edge_rate(speed),
    ]
    if args.dry_run:
        safe_cmd = list(cmd)
        safe_cmd[safe_cmd.index("--text") + 1] = "<narration text redacted>"
        emit(
            {
                "ok": True,
                "dry_run": True,
                "provider": "rumi",
                "backend": backend,
                "voice_id": voice_id,
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "command": safe_cmd,
                "post_speed": speed if apply_post_speed else None,
            }
        )
        return 0

    try:
        result = subprocess.run(cmd, text=True, capture_output=True, timeout=180)
    except subprocess.TimeoutExpired:
        return die("Rumi voice synthesis timed out")
    if result.returncode != 0 or not synth_target.is_file():
        return die(result.stderr.strip() or "Rumi voice synthesis failed")

    if apply_post_speed:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return die("ffmpeg is required to apply Fish voice speed")
        retime = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(synth_target),
                "-filter:a",
                f"atempo={speed:.4f}",
                "-vn",
                str(media),
            ],
            text=True,
            capture_output=True,
        )
        if retime.returncode != 0 or not media.is_file():
            return die(retime.stderr.strip() or "ffmpeg voice speed adjustment failed")
        synth_target.unlink(missing_ok=True)

    try:
        duration = probe_media(media)["duration_s"]
    except ValueError:
        duration = None
    meta = {
        "provider": "rumi",
        "backend": backend,
        "voice_id": voice_id,
        "language": voice["language"],
        "gender": voice["gender"],
        "speed": speed,
        "cloud": True,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "media": manifest_media_path(media, manifest_path.parent),
        "duration_s": duration,
        "requires_word_retranscription": True,
        "created_at": now_utc(),
    }
    write_json(output_dir / "voice_meta.json", meta)
    emit({"ok": True, "media": str(media), "meta": meta})
    return 0


def cmd_synthesize_edge(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).expanduser().resolve()
    script_path = Path(args.script).expanduser().resolve()
    try:
        manifest = read_json(manifest_path)
        text = script_path.read_text(encoding="utf-8").strip()
    except (ValueError, FileNotFoundError) as exc:
        return die(str(exc))
    voice = manifest.get("voiceover", {})
    if not voice.get("enabled") or voice.get("provider") != "edge":
        return die("manifest voiceover provider must be edge")
    if not args.allow_cloud and not args.dry_run:
        return die("Edge TTS sends script text to Microsoft; re-run with --allow-cloud after consent")
    command = shutil.which("edge-tts")
    if not command:
        return die("edge-tts is not installed")
    if not text:
        return die("narration script is empty")
    output_dir = manifest_path.parent / "voice"
    output_dir.mkdir(parents=True, exist_ok=True)
    narration_txt = output_dir / "narration.txt"
    if narration_txt.resolve() != script_path:
        narration_txt.write_text(text + "\n", encoding="utf-8")
    media = Path(args.output).expanduser().resolve() if args.output else output_dir / "narration.mp3"
    subtitles = media.with_suffix(".vtt")
    cmd = [
        command,
        "--file",
        str(narration_txt),
        "--voice",
        str(voice["voice_id"]),
        "--rate",
        edge_rate(float(voice["speed"])),
        "--write-media",
        str(media),
        "--write-subtitles",
        str(subtitles),
    ]
    if args.dry_run:
        emit({"ok": True, "dry_run": True, "command": cmd})
        return 0
    media.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(cmd, text=True, capture_output=True)
    if result.returncode != 0 or not media.is_file():
        return die(result.stderr.strip() or "edge-tts failed")
    try:
        duration = probe_media(media)["duration_s"]
    except ValueError:
        ffprobe = shutil.which("ffprobe")
        duration_result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(media)],
            text=True,
            capture_output=True,
        ) if ffprobe else None
        duration = round(float(duration_result.stdout.strip()), 3) if duration_result and duration_result.returncode == 0 else None
    meta = {
        "provider": "edge",
        "voice_id": voice["voice_id"],
        "language": voice["language"],
        "gender": voice["gender"],
        "speed": voice["speed"],
        "cloud": True,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "media": manifest_media_path(media, manifest_path.parent),
        "subtitles": manifest_media_path(subtitles, manifest_path.parent)
        if subtitles.is_file()
        else None,
        "duration_s": duration,
        "requires_word_retranscription": True,
        "created_at": now_utc(),
    }
    write_json(output_dir / "voice_meta.json", meta)
    emit({"ok": True, "media": str(media), "subtitles": str(subtitles), "meta": meta})
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    path = Path(args.manifest).expanduser().resolve()
    try:
        manifest = read_json(path)
    except ValueError as exc:
        return die(str(exc))
    try:
        from editor_server import approval_prerequisite_errors, gate_revision
    except ImportError as exc:
        return die(f"cannot load approval contract: {exc}")
    editor_state_path = path.parent / "working/editor_state.json"
    try:
        state = read_json(editor_state_path) if editor_state_path.is_file() else {}
        current_revision = gate_revision(path.parent, args.gate, state)
    except ValueError as exc:
        return die(str(exc))
    if args.expected_revision != current_revision:
        return die(
            f"approval revision is stale; current revision is {current_revision}"
        )
    errors = approval_prerequisite_errors(
        path.parent,
        manifest,
        state,
        args.gate,
    )
    if errors:
        return die("; ".join(errors))
    approvals = manifest.setdefault("approvals", {})
    approval = {
        "approved": True,
        "confirmed_by": args.confirmed_by,
        "at": now_utc(),
        "note": args.note,
        "state_revision": current_revision,
        "revision_kind": args.gate,
    }
    if args.gate == "highlight_selection":
        approval["plan_revision"] = state.get("highlight_plan_revision")
    approvals[args.gate] = approval
    stages = manifest.setdefault("stages", {})
    if args.gate == "destructive_edit":
        stages["edit_review"] = "complete"
    elif args.gate == "highlight_selection":
        stages["highlight_plan"] = "complete"
    elif args.gate == "timeline":
        stages["timeline_review"] = "complete"
    elif args.gate == "final":
        stages["edit_review"] = "complete"
        stages["cut"] = "skipped"
        stages["retranscribe"] = "skipped"
        if state.get("highlights"):
            stages["highlight_plan"] = "complete"
        stages["timeline_review"] = "complete"
        overlay_types = {
            str(item.get("type"))
            for item in state.get("overlays", [])
            if isinstance(item, dict) and item.get("visible", True)
        }
        if "caption" in overlay_types:
            stages["subtitles"] = "complete"
        if "emphasis" in overlay_types:
            stages["emphasis"] = "complete"
        if overlay_types & {"title", "card", "image", "gif", "video", "animation"}:
            stages["visual_plan"] = "complete"
        stages["render"] = "complete"
        stages["qa"] = "complete"
    manifest["updated_at"] = now_utc()
    write_json(path, manifest)
    emit({"ok": True, "gate": args.gate, "approval": approvals[args.gate]})
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    path = Path(args.manifest).expanduser().resolve()
    try:
        manifest = read_json(path)
    except ValueError as exc:
        return die(str(exc))
    approval_revisions: dict[str, str] = {}
    try:
        from editor_server import (
            GATES as EDITOR_GATES,
            approval_is_current,
            approval_revisions as current_approval_revisions,
        )

        state_path = path.parent / "working/editor_state.json"
        state = read_json(state_path) if state_path.is_file() else {}
        approval_revisions = current_approval_revisions(path.parent, state)
        approval_current = {
            gate: approval_is_current(path.parent, manifest, gate, state)
            for gate in sorted(EDITOR_GATES)
        }
    except (ImportError, ValueError):
        approval_revisions = {}
        approval_current = {}
    emit(
        {
            "manifest": str(path),
            "project_id": manifest.get("project_id"),
            "stages": manifest.get("stages", {}),
            "approvals": manifest.get("approvals", {}),
            "approval_revisions": approval_revisions,
            "approval_current": approval_current,
            "output_target": manifest.get("output_target", {}),
            "voiceover": manifest.get("voiceover", {}),
        }
    )
    return 0


def cmd_editor(args: argparse.Namespace) -> int:
    project_dir = Path(args.project_dir).expanduser().resolve()
    if not (project_dir / "project.json").is_file():
        return die(f"project.json not found under {project_dir}")
    server = PATHS["editor_server"]
    if not server.is_file():
        return die(f"editor server is missing: {server}")
    command = [
        sys.executable,
        str(server),
        "--project-dir",
        str(project_dir),
        "--host",
        args.host,
        "--port",
        str(args.port),
    ]
    if args.open_browser:
        command.append("--open")
    if args.allow_remote:
        command.append("--allow-remote")
    try:
        return subprocess.call(command)
    except KeyboardInterrupt:
        return 130


def cmd_studio(args: argparse.Namespace) -> int:
    projects_root = Path(args.projects_root).expanduser().resolve()
    server = PATHS["studio_server"]
    if not server.is_file():
        return die(f"Studio server is missing: {server}")
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        return die("Studio import server is loopback-only")
    projects_root.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(server),
        "--projects-root",
        str(projects_root),
        "--host",
        args.host,
        "--port",
        str(args.port),
    ]
    if args.open_browser:
        command.append("--open")
    try:
        return subprocess.call(command)
    except KeyboardInterrupt:
        return 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    preflight = sub.add_parser("preflight", help="Check installed editing capabilities")
    preflight.set_defaults(func=cmd_preflight)

    presets = sub.add_parser(
        "duration-presets",
        help="List platform-aware short, medium, and long editorial targets",
    )
    presets.set_defaults(func=cmd_duration_presets)

    init = sub.add_parser("init", help="Create a non-destructive auto-edit project")
    init.add_argument("--input", required=True)
    init.add_argument("--project-dir", required=True)
    init.add_argument("--source-language", default="auto")
    init.add_argument("--subtitle-mode", choices=SUBTITLE_MODES, default="source")
    init.add_argument("--target-language")
    init.add_argument("--platform", choices=PLATFORMS, default="auto")
    init.add_argument("--duration-profile", choices=DURATION_PROFILES, default="full")
    init.add_argument(
        "--target-duration",
        type=float,
        help="Approximate custom target in seconds; overrides the duration profile",
    )
    init.add_argument("--edit-preset", choices=tuple(EDIT_PRESETS), default="balanced")
    init.add_argument("--emphasis", choices=("off", "sparse", "balanced", "dense"), default="balanced")
    init.add_argument("--visual-density", choices=("sparse", "balanced", "dense"), default="balanced")
    init.add_argument("--no-cards", action="store_true")
    init.add_argument("--no-assets", action="store_true")
    init.add_argument("--no-animations", action="store_true")
    init.add_argument("--voice-language", choices=VOICE_LANGUAGES)
    init.add_argument("--voice-gender", choices=("female", "male"))
    init.add_argument("--voice-provider", choices=VOICE_PROVIDERS)
    init.add_argument("--voice-id")
    init.add_argument("--voice-speed", type=float, default=1.0)
    init.add_argument("--voice-mode", choices=("replace", "add"), default="replace")
    init.set_defaults(func=cmd_init)

    target = sub.add_parser(
        "set-target",
        help="Resolve or change the platform and duration target after transcription",
    )
    target.add_argument("--manifest", required=True)
    target.add_argument("--platform", choices=PLATFORMS)
    target.add_argument("--duration-profile", choices=DURATION_PROFILES)
    target.add_argument(
        "--target-duration",
        type=float,
        help="Approximate custom target in seconds; overrides the duration profile",
    )
    target.set_defaults(func=cmd_set_target)

    validate = sub.add_parser("validate", help="Validate project invariants")
    validate.add_argument("--manifest", required=True)
    validate.set_defaults(func=cmd_validate)

    whisper_import = sub.add_parser(
        "import-whisper",
        help="Normalize local Whisper JSON into the project artifact contract",
    )
    whisper_import.add_argument("--manifest", required=True)
    whisper_import.add_argument("--whisper-json", required=True)
    whisper_import.add_argument("--srt")
    whisper_import.add_argument("--model", default="unknown")
    whisper_import.set_defaults(func=cmd_import_whisper)

    transcribe = sub.add_parser(
        "transcribe-local",
        help="Run the installed local Whisper CLI and import timed transcript artifacts",
    )
    transcribe.add_argument("--manifest", required=True)
    transcribe.add_argument("--model", default="base")
    transcribe.add_argument("--timeout", type=int, default=21600)
    transcribe.set_defaults(func=cmd_transcribe_local)

    analyze = sub.add_parser(
        "analyze-edits",
        help="Propose low-risk silence/filler/stutter edits for human review",
    )
    analyze.add_argument("--manifest", required=True)
    analyze.set_defaults(func=cmd_analyze_edits)

    plans = sub.add_parser(
        "plan-overlays",
        help="Propose transcript-grounded emphasis, title, and data/contrast cards",
    )
    plans.add_argument("--manifest", required=True)
    plans.set_defaults(func=cmd_plan_overlays)

    highlights = sub.add_parser(
        "plan-highlights",
        help="Create 1-10 deterministic transcript-grounded highlight proposals",
    )
    highlights.add_argument("--manifest", required=True)
    highlights.add_argument(
        "--director",
        choices=tuple(HIGHLIGHT_DIRECTOR_PROFILES),
        default="teacher-punch",
    )
    highlights.add_argument("--count", type=int, default=10)
    highlights.add_argument("--brief", default="")
    highlights.set_defaults(func=cmd_plan_highlights)

    voices = sub.add_parser("voices", help="List Rumi/Fish and Edge voice options")
    voices.add_argument("--language", choices=VOICE_LANGUAGES)
    voices.add_argument("--gender", choices=("female", "male"))
    voices.add_argument("--provider", choices=("rumi", "edge"))
    voices.add_argument("--live", action="store_true", help="Refresh the online Edge voice catalog")
    voices.set_defaults(func=cmd_voices)

    audio = sub.add_parser("audio-request", help="Adapt narration text to HyperFrames audio_request.json")
    audio.add_argument("--manifest", required=True)
    audio.add_argument("--script", required=True)
    audio.add_argument("--output")
    audio.set_defaults(func=cmd_audio_request)

    rumi = sub.add_parser(
        "synthesize-rumi",
        help="Generate through the shared Rumi voice system (Fish default, Edge fallback)",
    )
    rumi.add_argument("--manifest", required=True)
    rumi.add_argument("--script", required=True)
    rumi.add_argument("--output")
    rumi.add_argument("--allow-cloud", action="store_true")
    rumi.add_argument("--dry-run", action="store_true")
    rumi.set_defaults(func=cmd_synthesize_rumi)

    edge = sub.add_parser("synthesize-edge", help="Generate selected Edge voiceover and VTT")
    edge.add_argument("--manifest", required=True)
    edge.add_argument("--script", required=True)
    edge.add_argument("--output")
    edge.add_argument("--allow-cloud", action="store_true")
    edge.add_argument("--dry-run", action="store_true")
    edge.set_defaults(func=cmd_synthesize_edge)

    approve = sub.add_parser("approve", help="Record an explicit human gate approval")
    approve.add_argument("--manifest", required=True)
    approve.add_argument("--gate", choices=GATES, required=True)
    approve.add_argument("--expected-revision", required=True)
    approve.add_argument("--confirmed-by", required=True)
    approve.add_argument("--note")
    approve.set_defaults(func=cmd_approve)

    status = sub.add_parser("status", help="Show stages, approvals, and voice selection")
    status.add_argument("--manifest", required=True)
    status.set_defaults(func=cmd_status)

    editor = sub.add_parser("editor", help="Launch the local live-preview page editor")
    editor.add_argument("--project-dir", required=True)
    editor.add_argument("--host", default="127.0.0.1")
    editor.add_argument("--port", type=int, default=8765)
    editor.add_argument("--open", action="store_true", dest="open_browser")
    editor.add_argument("--allow-remote", action="store_true")
    editor.set_defaults(func=cmd_editor)

    studio = sub.add_parser("studio", help="Launch the loopback-only new-project importer")
    studio.add_argument("--projects-root", required=True)
    studio.add_argument("--host", default="127.0.0.1")
    studio.add_argument("--port", type=int, default=8765)
    studio.add_argument("--open", action="store_true", dest="open_browser")
    studio.set_defaults(func=cmd_studio)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
