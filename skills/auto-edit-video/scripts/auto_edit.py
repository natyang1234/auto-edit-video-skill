#!/usr/bin/env python3
"""Deterministic project/voice bridge for the auto-edit-video skill."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any


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
    "editor_renderer": SKILL_DIR / "scripts/render_editor_timeline.py",
    "editor_index": SKILL_DIR / "editor/index.html",
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
    "voiceover",
    "timeline_review",
    "render",
    "qa",
)

GATES = ("destructive_edit", "timeline", "final")
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
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


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
    )
    if result.returncode != 0:
        raise ValueError(f"ffprobe failed: {result.stderr.strip() or 'unknown error'}")
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio = next((item for item in streams if item.get("codec_type") == "audio"), {})
    return {
        "duration_s": round(float(data.get("format", {}).get("duration", 0.0)), 3),
        "width": video.get("width"),
        "height": video.get("height"),
        "fps": parse_rate(video.get("r_frame_rate")),
        "video_codec": video.get("codec_name"),
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
        for name in ("ffmpeg", "ffprobe", "python3", "node", "npx", "edge-tts")
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
    core_checks = {
        "ffmpeg": bool(commands["ffmpeg"]),
        "ffprobe": bool(commands["ffprobe"]),
        "python3": bool(commands["python3"]),
        "page_editor": files["editor_server"] and files["editor_index"],
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
        "missing_optional": [name for name, ok in extended_checks.items() if not ok],
        "capabilities": {
            "destructive_edit_review": True,
            "destructive_cut_render": files["cut_renderer"] and bool(commands["ffmpeg"]),
            "programmatic_render_qa": files["video_autopilot_cli"]
            and files["video_autopilot_repo"],
            "bundled_render_qa": files["qa_runner"] and bool(commands["ffmpeg"]),
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
            and files["editor_index"],
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


def cmd_init(args: argparse.Namespace) -> int:
    source = Path(args.input).expanduser().resolve()
    if not source.is_file():
        return die(f"input video not found: {source}")
    project_dir = Path(args.project_dir).expanduser().resolve()
    try:
        media = probe_media(source)
        voice = make_voice_config(args)
        ensure_new_project(project_dir)
    except ValueError as exc:
        return die(str(exc))

    staged = project_dir / "source" / f"original{source.suffix.lower()}"
    try:
        staged.symlink_to(source)
    except OSError:
        shutil.copy2(source, staged)

    target_language = args.target_language or default_target(args.source_language)
    created = now_utc()
    stage_state = {stage: "pending" for stage in STAGES}
    stage_state["ingest"] = "complete"
    if not voice["enabled"]:
        stage_state["voiceover"] = "skipped"

    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_dir.name,
        "created_at": created,
        "updated_at": created,
        "project_dir": str(project_dir),
        "source": {
            "original_path": str(source),
            "staged_path": str(staged.relative_to(project_dir)),
            "immutable": True,
            "size_bytes": source.stat().st_size,
            **media,
        },
        "editing": {
            "preset": args.edit_preset,
            **EDIT_PRESETS[args.edit_preset],
            "delete_earlier_keep_later": True,
            "destructive_review_required": True,
            "retranscribe_after_cut": True,
        },
        "subtitles": {
            "mode": args.subtitle_mode,
            "source_language": args.source_language,
            "target_language": target_language,
            "translation_variant": "zh-Hant" if args.subtitle_mode in {"zh", "bilingual"} else None,
            "style": "rail",
            "emphasis_enabled": args.emphasis != "off",
            "emphasis_density": args.emphasis,
        },
        "visuals": {
            "content_match_required": True,
            "cards": not args.no_cards,
            "related_assets": not args.no_assets,
            "animations": not args.no_animations,
            "density": args.visual_density,
            "provenance_required": True,
        },
        "voiceover": voice,
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
            "edit_review": "review/edit-review.html",
            "timeline_preview": "review/timeline-preview.html",
            "final_video": "renders/final.mp4",
            "qa_report": "qa/qa-report.json",
            "contact_sheet": "qa/final-contact.png",
        },
    }
    write_json(project_dir / "project.json", manifest)
    write_empty_artifacts(project_dir)
    emit(
        {
            "ok": True,
            "manifest": str(project_dir / "project.json"),
            "project_dir": str(project_dir),
            "voiceover": voice,
            "next": "materialize video-profile-context.md, then transcribe locally",
        }
    )
    return 0


def validate_manifest(manifest: dict[str, Any], manifest_path: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    source_path = Path(str(manifest.get("source", {}).get("original_path", ""))).expanduser()
    if not source_path.is_file():
        errors.append(f"source video missing: {source_path}")
    editing = manifest.get("editing", {})
    if not editing.get("destructive_review_required"):
        errors.append("destructive_edit review must remain required")
    if not editing.get("retranscribe_after_cut"):
        errors.append("retranscribe_after_cut must remain enabled")
    render = manifest.get("render", {})
    if render.get("capcut") is not False:
        errors.append("CapCut must remain disabled")

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
            word = {
                "id": f"word-{len(flat_words) + 1:05d}",
                "text": text,
                "start": start,
                "end": end,
                "confidence": round(float(raw_word.get("probability", 0.0)), 4),
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


def cmd_import_whisper(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).expanduser().resolve()
    whisper_path = Path(args.whisper_json).expanduser().resolve()
    try:
        manifest = read_json(manifest_path)
        data = read_json(whisper_path)
    except ValueError as exc:
        return die(str(exc))
    if not isinstance(data.get("segments"), list):
        return die("Whisper JSON must contain a segments array")
    duration_s = float(manifest.get("source", {}).get("duration_s", 0.0))
    transcript, compatibility = whisper_payload(data, duration_s)
    project_dir = manifest_path.parent
    transcript_path = project_dir / "working/transcript_words.json"
    compatibility_path = project_dir / "working/subtitles_words.json"
    write_json(transcript_path, transcript)
    write_json(compatibility_path, compatibility)
    if args.srt:
        srt_path = Path(args.srt).expanduser().resolve()
        if not srt_path.is_file():
            return die(f"SRT not found: {srt_path}")
        shutil.copy2(srt_path, project_dir / "subtitles/source.srt")
    manifest.setdefault("stages", {})["transcribe"] = "complete"
    manifest["stages"]["edit_analysis"] = "pending"
    manifest.setdefault("artifacts", {})["transcript_compatibility"] = (
        "working/subtitles_words.json"
    )
    manifest["transcription"] = {
        "engine": "openai-whisper",
        "model": args.model,
        "language": data.get("language"),
        "word_count": len(transcript["words"]),
        "segment_count": len(transcript["segments"]),
        "source_json": str(whisper_path),
        "imported_at": now_utc(),
    }
    manifest["updated_at"] = now_utc()
    write_json(manifest_path, manifest)
    emit(
        {
            "ok": True,
            "transcript": str(transcript_path),
            "compatibility": str(compatibility_path),
            "words": len(transcript["words"]),
            "segments": len(transcript["segments"]),
        }
    )
    return 0


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

    words = list(transcript.get("words", []))
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
    payload = {
        "schema_version": SCHEMA_VERSION,
        "source": "working/transcript_words.json",
        "generated_at": now_utc(),
        "detector": "deterministic-low-risk-v1",
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
    approvals = manifest.setdefault("approvals", {})
    approval = {
        "approved": True,
        "confirmed_by": args.confirmed_by,
        "at": now_utc(),
        "note": args.note,
    }
    editor_state_path = path.parent / "working/editor_state.json"
    if args.gate in {"timeline", "final"} and editor_state_path.is_file():
        try:
            from editor_server import editor_state_revision

            approval["state_revision"] = editor_state_revision(read_json(editor_state_path))
        except (ImportError, ValueError) as exc:
            return die(f"cannot bind approval to editor state: {exc}")
    approvals[args.gate] = approval
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
    emit(
        {
            "manifest": str(path),
            "project_id": manifest.get("project_id"),
            "stages": manifest.get("stages", {}),
            "approvals": manifest.get("approvals", {}),
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    preflight = sub.add_parser("preflight", help="Check installed editing capabilities")
    preflight.set_defaults(func=cmd_preflight)

    init = sub.add_parser("init", help="Create a non-destructive auto-edit project")
    init.add_argument("--input", required=True)
    init.add_argument("--project-dir", required=True)
    init.add_argument("--source-language", default="auto")
    init.add_argument("--subtitle-mode", choices=SUBTITLE_MODES, default="source")
    init.add_argument("--target-language")
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
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
