#!/usr/bin/env python3
"""Deterministic project/voice bridge for the auto-edit-video skill."""

from __future__ import annotations

import argparse
from difflib import SequenceMatcher
import hashlib
import importlib.util
import json
import math
import mimetypes
import os
import re
import shlex
import shutil
import subprocess
import sys
import uuid

import text_joining
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from contextual_semantic_calibration import (
    ollama_json_model_call,
    propose_contextual_corrections,
    validate_contextual_proposals,
)
from highlight_planner import (
    DIRECTOR_PROFILES as HIGHLIGHT_DIRECTOR_PROFILES,
    build_highlight_plan,
    validate_highlight_plan,
)
from visual_quality import build_highlight_design_overlays
from template_catalog import cutout_capability
from traditional_chinese import (
    ORTHOGRAPHY_VARIANT,
    normalize_whisper_orthography,
    should_normalize_taiwan_traditional,
)


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
SOURCE_LANGUAGES = ("auto", "zh-TW", "zh-CN", "en-US", "en-GB", "zh-en")
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
        # "About thirty seconds" is a length to aim for, not a length to
        # hit: at ten percent a thirty-second target refuses anything under
        # twenty-seven, and a good forty-second point gets thrown away for
        # being the wrong length. Aim for the target, accept what reads.
        tolerance = round(max(5.0, min(30.0, target * 0.45)), 3)
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


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, round(float(seconds) * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{millis:03d}"


def write_transcript_srt(path: Path, transcript: dict[str, Any]) -> None:
    source_segments = transcript.get("caption_segments") or transcript.get("segments", [])
    blocks = []
    for index, segment in enumerate(source_segments, start=1):
        text = str(segment.get("text", "")).strip()
        if not text:
            continue
        blocks.append(
            "\n".join(
                [
                    str(index),
                    f"{srt_timestamp(segment['start'])} --> {srt_timestamp(segment['end'])}",
                    text,
                ]
            )
        )
    write_text_atomic(path, "\n\n".join(blocks) + ("\n" if blocks else ""))


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
    if language.lower() == "zh-en":
        return "mixed"
    if language.lower().startswith("zh"):
        return "zh"
    if language.lower().startswith("en"):
        return "en"
    return language.split("-", 1)[0].lower()


def normalize_transcription_glossary(values: Any) -> list[str]:
    if values is None:
        return []
    raw_values = [values] if isinstance(values, str) else list(values)
    terms: list[str] = []
    seen: set[str] = set()
    for raw_value in raw_values:
        for raw_term in re.split(r"[,;；\n]+", str(raw_value)):
            term = re.sub(r"\s+", " ", raw_term).strip()
            if not term:
                continue
            if len(term) > 80:
                raise ValueError("transcription glossary terms must be 80 characters or fewer")
            key = term.casefold()
            if key in seen:
                continue
            seen.add(key)
            terms.append(term)
    if len(terms) > 64 or sum(len(term) for term in terms) > 1200:
        raise ValueError("transcription glossary is too large")
    return terms


def normalize_transcription_calibrations(values: Any) -> list[dict[str, Any]]:
    """Normalize explicit, auditable ASR alias rules.

    Rules use ``canonical=alias|alias``.  Canonical text may be longer or
    shorter than the ASR alias; applying a rule preserves the matched source
    time span and reuses the original Whisper word boundaries where possible.
    """
    if values is None:
        return []
    raw_values = [values] if isinstance(values, (str, dict)) else list(values)
    raw_rules: list[Any] = []
    for raw_value in raw_values:
        if isinstance(raw_value, dict):
            raw_rules.append(raw_value)
        else:
            raw_rules.extend(
                item for item in re.split(r"[;；\n]+", str(raw_value)) if item.strip()
            )
    rules: list[dict[str, Any]] = []
    seen_aliases: dict[str, str] = {}
    total_aliases = 0
    total_characters = 0
    for raw_rule in raw_rules:
        if isinstance(raw_rule, dict):
            canonical = re.sub(r"\s+", " ", str(raw_rule.get("canonical", ""))).strip()
            raw_aliases = raw_rule.get("aliases", [])
            aliases_source = [raw_aliases] if isinstance(raw_aliases, str) else list(raw_aliases or [])
            raw_scope_start = raw_rule.get("start")
            raw_scope_end = raw_rule.get("end")
        else:
            if "=" not in str(raw_rule):
                raise ValueError(
                    "transcription calibration rules must use canonical=alias|alias"
                )
            canonical, aliases_text = str(raw_rule).split("=", 1)
            canonical = re.sub(r"\s+", " ", canonical).strip()
            aliases_source = aliases_text.split("|")
            raw_scope_start = None
            raw_scope_end = None
        if not canonical or len(canonical) > 80:
            raise ValueError(
                "transcription calibration canonical terms must be 1-80 characters"
            )
        aliases: list[str] = []
        local_seen: set[str] = set()
        for raw_alias in aliases_source:
            alias = re.sub(r"\s+", " ", str(raw_alias)).strip()
            if not alias or len(alias) > 80:
                raise ValueError(
                    "transcription calibration aliases must be 1-80 characters"
                )
            alias_key = alias.casefold()
            if alias_key == canonical.casefold() or alias_key in local_seen:
                continue
            prior = seen_aliases.get(alias_key)
            if prior is not None and prior.casefold() != canonical.casefold():
                raise ValueError(
                    f"transcription calibration alias has conflicting targets: {alias}"
                )
            seen_aliases[alias_key] = canonical
            local_seen.add(alias_key)
            aliases.append(alias)
        if not aliases:
            continue
        scope: dict[str, float] = {}
        for name, raw_value in (("start", raw_scope_start), ("end", raw_scope_end)):
            if raw_value is None:
                continue
            if isinstance(raw_value, bool):
                raise ValueError("transcription calibration time scopes must be finite numbers")
            try:
                value = float(raw_value)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "transcription calibration time scopes must be finite numbers"
                ) from exc
            if not math.isfinite(value) or value < 0:
                raise ValueError(
                    "transcription calibration time scopes must be finite non-negative numbers"
                )
            scope[name] = round(value, 3)
        if "start" in scope and "end" in scope and scope["end"] <= scope["start"]:
            raise ValueError("transcription calibration end must be after start")
        rules.append({"canonical": canonical, "aliases": aliases, **scope})
        total_aliases += len(aliases)
        total_characters += len(canonical) + sum(len(alias) for alias in aliases)
    if len(rules) > 64 or total_aliases > 256 or total_characters > 2400:
        raise ValueError("transcription calibration rules are too large")
    return rules


def apply_transcription_calibrations(
    data: dict[str, Any],
    calibrations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply exact aliases while preserving each matched source time span."""
    candidates = sorted(
        (
            (
                alias,
                str(rule["canonical"]),
                rule.get("start"),
                rule.get("end"),
            )
            for rule in calibrations
            for alias in rule.get("aliases", [])
        ),
        key=lambda item: (-len(item[0]), item[0].casefold()),
    )
    corrections: list[dict[str, Any]] = []
    for segment_index, segment in enumerate(data.get("segments", [])):
        raw_words = segment.get("words")
        if not isinstance(raw_words, list) or not raw_words:
            continue
        original_tokens = [str(item.get("word", "")) for item in raw_words]
        combined = "".join(original_tokens)
        if not combined:
            continue
        boundaries: list[tuple[int, int]] = []
        cursor = 0
        for token in original_tokens:
            boundaries.append((cursor, cursor + len(token)))
            cursor += len(token)
        occupied: set[int] = set()
        replacements: list[dict[str, Any]] = []
        for alias, canonical, scope_start, scope_end in candidates:
            for match in re.finditer(re.escape(alias), combined, flags=re.IGNORECASE):
                start, end = match.span()
                if any(index in occupied for index in range(start, end)):
                    continue
                matched = combined[start:end]
                if matched.casefold() == canonical.casefold():
                    continue
                word_indexes = [
                    index
                    for index, (word_start, word_end) in enumerate(boundaries)
                    if word_start < end and word_end > start
                ]
                first_word = raw_words[word_indexes[0]] if word_indexes else {}
                last_word = raw_words[word_indexes[-1]] if word_indexes else first_word
                match_start = float(first_word.get("start", segment.get("start", 0.0)))
                match_end = float(last_word.get("end", segment.get("end", match_start)))
                if scope_start is not None and match_end <= float(scope_start):
                    continue
                if scope_end is not None and match_start >= float(scope_end):
                    continue
                occupied.update(range(start, end))
                replacement = {
                    "segment_id": segment.get("id", segment_index),
                    "from": matched,
                    "to": canonical,
                    "start": match_start,
                    "end": match_end,
                    "source_word_count": len(word_indexes),
                    "timing_mode": (
                        "word_boundaries_preserved"
                        if len(matched) == len(canonical)
                        else "source_span_preserved"
                    ),
                    "character_start": start,
                    "character_end": end,
                    "word_indexes": word_indexes,
                }
                replacements.append(replacement)
                corrections.append(
                    {
                        key: value
                        for key, value in replacement.items()
                        if key not in {"character_start", "character_end", "word_indexes"}
                    }
                )
        if not occupied:
            continue

        output_tokens = ["" for _item in raw_words]

        def append_original(start: int, end: int) -> None:
            for index, (word_start, word_end) in enumerate(boundaries):
                if word_end <= start:
                    continue
                if word_start >= end:
                    break
                slice_start = max(start, word_start)
                slice_end = min(end, word_end)
                if slice_start < slice_end:
                    output_tokens[index] += combined[slice_start:slice_end]

        def replacement_chunks(text: str, count: int) -> list[str]:
            if count <= 1:
                return [text]
            if len(text) >= count:
                return [
                    text[index * len(text) // count : (index + 1) * len(text) // count]
                    for index in range(count)
                ]
            chunks = ["" for _index in range(count)]
            if len(text) == 1:
                chunks[0] = text
                return chunks
            positions = [
                round(index * (count - 1) / (len(text) - 1))
                for index in range(len(text))
            ]
            for character, position in zip(text, positions):
                chunks[position] += character
            return chunks

        cursor = 0
        for replacement in sorted(replacements, key=lambda item: item["character_start"]):
            start = int(replacement["character_start"])
            end = int(replacement["character_end"])
            append_original(cursor, start)
            word_indexes = list(replacement["word_indexes"])
            chunks = replacement_chunks(str(replacement["to"]), len(word_indexes))
            for word_index, chunk in zip(word_indexes, chunks):
                output_tokens[word_index] += chunk
            cursor = end
        append_original(cursor, len(combined))

        for raw_word, corrected_token in zip(raw_words, output_tokens):
            raw_word["word"] = corrected_token
        segment["text"] = join_caption_words(
            [{"text": raw_word.get("word", "")} for raw_word in raw_words]
        )
    if corrections:
        data["text"] = " ".join(
            str(segment.get("text", "")).strip()
            for segment in data.get("segments", [])
            if str(segment.get("text", "")).strip()
        )
    return corrections


TRANSCRIPTION_PROMPT_MAX_CHARS = 180
TRANSCRIPTION_PROMPT_MAX_TERMS = 12


def compact_transcription_prompt_terms(glossary: list[str]) -> list[str]:
    candidates: list[tuple[int, str, int]] = []
    for index, term in enumerate(glossary):
        latin_words = re.findall(r"[A-Za-z]+", term)
        if not latin_words or len(latin_words) > 5 or len(term) > 36:
            continue
        candidates.append((index, term, len(latin_words)))
    candidates.sort(key=lambda item: (item[2], len(item[1]), item[0]))
    return [
        term
        for _index, term, _word_count in candidates[:TRANSCRIPTION_PROMPT_MAX_TERMS]
    ]


def transcription_initial_prompt(source_language: str, glossary: list[str]) -> str | None:
    chinese_or_auto = source_language in {"auto", "zh-TW", "zh-CN", "zh-en"}
    if not chinese_or_auto and not glossary:
        return None
    if chinese_or_auto:
        # The prompt steers spelling and vocabulary, so declaring the variant
        # here prevents mis-hearings the project would otherwise have to
        # detect and repair afterwards. zh-TW asks for Taiwanese wording;
        # anything else only asks for verbatim Chinese-English handling.
        if source_language in {"zh-TW", "zh-en"}:
            prompt = "繁體中文，台灣用語。中英逐字稿。英文請保留拼寫，不要中文音譯。"
        else:
            prompt = "中英逐字稿。英文請保留拼寫，不要中文音譯。"
        glossary_prefix = "英文詞彙："
    else:
        prompt = "Verbatim transcript. Preserve original spelling."
        glossary_prefix = " Terms: "
    prompt_terms = compact_transcription_prompt_terms(glossary)
    selected: list[str] = []
    for term in prompt_terms:
        candidate_terms = selected + [term]
        candidate = prompt + glossary_prefix + ", ".join(candidate_terms) + "。"
        if len(candidate) > TRANSCRIPTION_PROMPT_MAX_CHARS:
            continue
        selected = candidate_terms
    if selected:
        prompt += glossary_prefix + ", ".join(selected) + "。"
    return prompt


LOW_CONFIDENCE_GLOSSARY_ALIASES = {
    "it": {"ed"},
}


def apply_glossary_corrections(
    data: dict[str, Any],
    glossary: list[str],
) -> list[dict[str, Any]]:
    glossary_tokens = [
        (term, re.findall(r"[A-Za-z]+", term))
        for term in glossary
        if re.search(r"[A-Za-z]", term)
    ]
    glossary_tokens.sort(key=lambda item: (-len(item[1]), -len(item[0])))
    exact_glossary_phrases = {
        " ".join(words).casefold()
        for _term, words in glossary_tokens
        if words
    }
    corrections: list[dict[str, Any]] = []
    for segment in data.get("segments", []):
        raw_words = segment.get("words")
        if not isinstance(raw_words, list) or not raw_words:
            continue
        parsed: list[tuple[str, str, str] | None] = []
        for raw_word in raw_words:
            raw_text = str(raw_word.get("word", ""))
            match = re.fullmatch(r"(\s*)([A-Za-z]+)([^A-Za-z]*)", raw_text)
            parsed.append(match.groups() if match else None)
        occupied: set[int] = set()
        for canonical, canonical_words in glossary_tokens:
            canonical_size = len(canonical_words)
            if canonical_size == 0:
                continue
            canonical_text = " ".join(canonical_words)
            for start in range(len(parsed)):
                if start in occupied or parsed[start] is None:
                    continue
                best: tuple[float, int, str] | None = None
                if canonical_size == 1:
                    window_sizes = range(1, min(len(parsed) - start, 4) + 1)
                else:
                    allowed_sizes = [canonical_size, canonical_size + 1]
                    if any(word.casefold() in {"a", "an", "the"} for word in canonical_words):
                        allowed_sizes.append(canonical_size - 1)
                    window_sizes = [
                        size
                        for size in allowed_sizes
                        if size > 0 and start + size <= len(parsed)
                    ]
                for window_size in window_sizes:
                    end = start + window_size
                    indexes = range(start, end)
                    if any(index in occupied or parsed[index] is None for index in indexes):
                        continue
                    if any((parsed[index] or ("", "", ""))[2] for index in range(start, end - 1)):
                        continue
                    if (
                        canonical_size > 1
                        and window_size == canonical_size + 1
                        and (parsed[start + 1] or ("", "", ""))[0]
                    ):
                        continue
                    candidate = (parsed[start] or ("", "", ""))[1]
                    for index in range(start + 1, end):
                        prefix, core, _suffix = parsed[index] or ("", "", "")
                        candidate += (" " if prefix else "") + core
                    candidate_key = re.sub(r"\s+", " ", candidate).strip().casefold()
                    canonical_key = canonical_text.casefold()
                    if (
                        candidate_key in exact_glossary_phrases
                        and candidate_key != canonical_key
                    ):
                        continue
                    if canonical_size == 1 and len(canonical_text) < 4:
                        confidence = raw_words[start].get(
                            "probability", raw_words[start].get("confidence")
                        )
                        alias_match = candidate_key in LOW_CONFIDENCE_GLOSSARY_ALIASES.get(
                            canonical_key, set()
                        )
                        forward_context = "".join(
                            str(raw_words[index].get("word", ""))
                            for index in range(start, min(len(raw_words), end + 4))
                        )
                        local_grammar_context = (
                            canonical_key == "it"
                            and re.search(
                                r"虛\s*主\s*詞|to\s*V",
                                forward_context,
                                flags=re.IGNORECASE,
                            )
                            is not None
                        )
                        trusted_alias = alias_match and (
                            (
                                isinstance(confidence, (int, float))
                                and float(confidence) < 0.35
                            )
                            or local_grammar_context
                        )
                        score = (
                            1.0
                            if candidate_key == canonical_key or trusted_alias
                            else 0.0
                        )
                        threshold = 1.0
                    else:
                        score = SequenceMatcher(
                            None,
                            candidate.casefold(),
                            canonical_text.casefold(),
                        ).ratio()
                        if canonical_size == 1:
                            threshold = 0.86
                        elif canonical_size == 2:
                            threshold = 0.84
                        else:
                            threshold = 0.90
                    if score >= threshold and (best is None or score > best[0]):
                        best = (score, end, candidate)
                if best is None:
                    continue
                _score, end, candidate = best
                window_size = end - start
                split_first_word = (
                    canonical_size > 1
                    and window_size == canonical_size + 1
                    and not (parsed[start + 1] or ("", "", ""))[0]
                )
                if split_first_word:
                    assignments = [canonical_words[0], ""] + canonical_words[1:]
                elif canonical_size >= window_size:
                    assignments = canonical_words[: window_size - 1] + [
                        " ".join(canonical_words[window_size - 1 :])
                    ]
                else:
                    assignments = canonical_words + [""] * (window_size - canonical_size)
                last_nonempty = max(
                    index for index, assignment in enumerate(assignments) if assignment
                )
                final_suffix = (parsed[end - 1] or ("", "", ""))[2]
                final_end = raw_words[end - 1].get("end")
                for offset, assignment in enumerate(assignments):
                    index = start + offset
                    prefix = (parsed[index] or ("", "", ""))[0]
                    suffix = final_suffix if offset == last_nonempty else ""
                    raw_words[index]["word"] = prefix + assignment + suffix if assignment else ""
                    if split_first_word and offset == 0:
                        raw_words[index]["end"] = raw_words[index + 1].get("end")
                    if offset == last_nonempty:
                        raw_words[index]["end"] = final_end
                    occupied.add(index)
                changed = candidate.casefold() != canonical_text.casefold() or window_size != canonical_size
                if changed:
                    corrections.append(
                        {
                            "from": candidate,
                            "to": canonical_text,
                            "start": raw_words[start].get("start"),
                            "end": final_end,
                        }
                    )
        segment["text"] = join_caption_words(
            [{"text": raw_word.get("word", "")} for raw_word in raw_words]
        )
    if corrections:
        data["text"] = " ".join(
            str(segment.get("text", "")).strip()
            for segment in data.get("segments", [])
            if str(segment.get("text", "")).strip()
        )
    return corrections


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
    transcription_glossary: list[str] | None = None,
    transcription_calibrations: list[Any] | None = None,
    contextual_semantic_calibration: bool = False,
    semantic_model: str = "qwen2.5:7b",
    subtitle_mode: str = "source",
    source_has_burned_in: str = "auto",
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
    if source_language not in SOURCE_LANGUAGES:
        raise ValueError(f"unsupported source language: {source_language}")
    if edit_preset not in EDIT_PRESETS:
        raise ValueError(f"unsupported edit preset: {edit_preset}")
    if emphasis not in {"off", "sparse", "balanced", "dense"}:
        raise ValueError(f"unsupported emphasis density: {emphasis}")
    if visual_density not in {"sparse", "balanced", "dense"}:
        raise ValueError(f"unsupported visual density: {visual_density}")
    if source_mode not in {"copy", "move"}:
        raise ValueError("source_mode must be copy or move")
    if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,120}", semantic_model):
        raise ValueError("semantic calibration model name is invalid")

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
    glossary = normalize_transcription_glossary(transcription_glossary)
    calibrations = normalize_transcription_calibrations(transcription_calibrations)
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
            # auto = believe the analysis stage; yes/no override it.
            "source_has_burned_in": source_has_burned_in,
            "source_language": source_language,
            "target_language": target_language,
            "glossary": glossary,
            "calibrations": calibrations,
            "contextual_calibrations": [],
            "contextual_semantic_calibration": {
                "enabled": bool(contextual_semantic_calibration),
                "provider": "ollama",
                "model": semantic_model,
                "minimum_confidence": 0.92,
                "context_radius": 2,
            },
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


FOLDER_KIND_BY_EXT = {
    **{ext: "video" for ext in (".mp4", ".mov", ".m4v", ".webm", ".mkv")},
    **{ext: "image" for ext in (".png", ".jpg", ".jpeg", ".webp")},
    ".gif": "gif",
    **{ext: "audio" for ext in (".mp3", ".wav", ".m4a", ".aac", ".flac")},
    **{ext: "font" for ext in (".ttf", ".otf", ".woff2")},
    **{ext: "subtitle" for ext in (".srt", ".vtt", ".ass")},
    **{ext: "document" for ext in (".md", ".txt", ".pdf")},
}
FOLDER_ROLE_BY_KIND = {
    "video": "broll",
    "image": "asset",
    "gif": "asset",
    "audio": "asset",
    "font": "font",
    "subtitle": "transcript",
    "document": "ignored",
    "other": "ignored",
}
FOLDER_IMPORT_MAX_FILES = 5000


def cmd_build_evidence_index(args: argparse.Namespace) -> int:
    """Tool-side evidence authority: derive citable evidence from transcript."""
    try:
        import narrative_engine
    except ImportError as exc:
        return die(f"cannot load narrative engine: {exc}")
    try:
        evidence_map = narrative_engine.build_evidence_index(
            Path(args.project_dir).expanduser().resolve()
        )
    except ValueError as exc:
        return die(str(exc))
    print(
        json.dumps(
            {
                "ok": True,
                "items": len(evidence_map["items"]),
                "revision": evidence_map["revision"],
            },
            ensure_ascii=False,
        )
    )
    return 0


def cmd_freeze_content_analysis(args: argparse.Namespace) -> int:
    """Validate and freeze an agent-authored content analysis draft."""
    try:
        import narrative_engine
    except ImportError as exc:
        return die(f"cannot load narrative engine: {exc}")
    draft_path = Path(args.input).expanduser()
    try:
        draft = read_json(draft_path)
        analysis = narrative_engine.freeze_content_analysis(
            Path(args.project_dir).expanduser().resolve(),
            draft,
            engine_id=args.engine_id,
            prompt_policy_version=args.prompt_policy_version,
            generated_at=now_utc(),
        )
    except ValueError as exc:
        return die(str(exc))
    print(json.dumps({"ok": True, "revision": analysis["revision"]}, ensure_ascii=False))
    return 0


def cmd_plan_narrative(args: argparse.Namespace) -> int:
    """Deterministic formula routing + low-risk narrative plan generation."""
    try:
        import narrative_engine
    except ImportError as exc:
        return die(f"cannot load narrative engine: {exc}")
    project_dir = Path(args.project_dir).expanduser().resolve()
    try:
        structure = narrative_engine.route_formulas(project_dir)
        plan = narrative_engine.build_narrative_plan(project_dir)
    except ValueError as exc:
        return die(str(exc))
    print(
        json.dumps(
            {
                "ok": True,
                "selected": structure["selected"],
                "plan_hash": plan["plan_hash"],
                "segments": len(plan["segments"]),
                "risk": plan["risk"],
                "warnings": plan["warnings"] + [
                    w for c in structure["candidates"] for w in c["warnings"]
                ],
                "next": "auto_edit.py apply-narrative-plan --project-dir ... "
                        "--plan working/narrative_edit_plan.json",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_reanchor_narrative(args: argparse.Namespace) -> int:
    """Re-anchor evidence against a rough-cut re-transcription.

    Workflow: render the rough cut, transcribe it (e.g. transcribe-local on
    the output, or an external whisper run), then pass that word-timed
    transcript here; the plan's reanchor status becomes anchored/stale/failed.
    """
    try:
        import narrative_engine
    except ImportError as exc:
        return die(f"cannot load narrative engine: {exc}")
    try:
        transcript = read_json(Path(args.transcript).expanduser())
        plan = narrative_engine.reanchor(
            Path(args.project_dir).expanduser().resolve(), transcript
        )
    except ValueError as exc:
        return die(str(exc))
    print(
        json.dumps(
            {"ok": True, "reanchor": plan["reanchor"], "plan_hash": plan["plan_hash"]},
            ensure_ascii=False,
        )
    )
    return 0


def cmd_analyze_video(args: argparse.Namespace) -> int:
    """Whole-video technical analysis with stage-level checkpoint cache."""
    try:
        import video_analyzer
    except ImportError as exc:
        return die(f"cannot load video analyzer: {exc}")
    project_dir = Path(args.project_dir).expanduser().resolve()
    if not (project_dir / "project.json").is_file():
        return die(f"project manifest not found under {project_dir}")
    try:
        analysis, stats = video_analyzer.analyze(project_dir)
    except ValueError as exc:
        return die(str(exc))
    print(
        json.dumps(
            {
                "ok": True,
                "revision": analysis["revision"],
                "duration_s": analysis["duration_s"],
                "shots": len(analysis["shots"]),
                "silences": len(analysis["silences"]),
                "ocr_spans": len(analysis["ocr_spans"]),
                "ocr_status": analysis["engines"]["ocr"]["status"],
                "stages": stats,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_ingest_folder(args: argparse.Namespace) -> int:
    """Folder-first ingest: inventory, main-video pick, owned copy, assets."""
    try:
        import contract_registry
    except ImportError as exc:
        return die(f"cannot load contract registry: {exc}")
    folder = Path(args.folder).expanduser().resolve()
    if not folder.is_dir():
        return die(f"input folder not found: {folder}")
    project_dir = Path(args.project_dir).expanduser()
    files = sorted(
        path
        for path in folder.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and not any(
            part.startswith(".") for part in path.relative_to(folder).parts
        )
    )
    if not files:
        return die("the folder contains no importable files")
    if len(files) > FOLDER_IMPORT_MAX_FILES:
        return die(
            f"folder has {len(files)} files; the import limit is "
            f"{FOLDER_IMPORT_MAX_FILES}"
        )
    entries: list[dict[str, Any]] = []
    videos: list[Path] = []
    warnings: list[str] = []
    for path in files:
        kind = FOLDER_KIND_BY_EXT.get(path.suffix.lower(), "other")
        if kind == "video":
            videos.append(path)
        entries.append(
            {
                "path": path.relative_to(folder).as_posix(),
                "kind": kind,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
                "mime": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                "role": FOLDER_ROLE_BY_KIND.get(kind, "ignored"),
            }
        )
    if args.main:
        main_video = Path(args.main).expanduser().resolve()
        if folder not in main_video.parents or FOLDER_KIND_BY_EXT.get(
            main_video.suffix.lower()
        ) != "video":
            return die("--main must point at a video inside the input folder")
    else:
        if not videos:
            return die("no video file found in the folder; pass --main explicitly")
        named = [
            candidate for candidate in videos
            if candidate.stem.lower() == "main" and candidate.parent == folder
        ]
        if len(named) > 1:
            return die(
                "more than one file is named main: "
                + ", ".join(sorted(candidate.name for candidate in named))
                + "; pass --main to say which one"
            )
        if named:
            main_video = named[0]
        elif len(videos) > 1:
            # Picking the longest used to stand in for asking. It reads as a
            # rule until the day the B-roll runs longer than the talking, and
            # then the wrong video is cut with nothing said about it.
            return die(
                f"the folder has {len(videos)} videos and none is named main: "
                + ", ".join(sorted(candidate.name for candidate in videos)[:6])
                + "; name one main.<ext> or pass --main"
            )
        else:
            main_video = videos[0]
        try:
            probe_media(main_video)
        except (ValueError, RuntimeError) as exc:
            return die(f"the main video could not be read: {main_video.name}: {exc}")
    main_rel = main_video.relative_to(folder).as_posix()
    for entry in entries:
        if entry["path"] == main_rel:
            entry["role"] = "main_video"
    try:
        manifest = initialize_project(
            main_video,
            project_dir,
            source_language=args.source_language,
            source_mode="copy",
            ingest_method="folder_import",
        )
    except ValueError as exc:
        return die(str(exc))
    project_dir = project_dir.expanduser().absolute()
    imported_dir = project_dir / "assets/imported"
    copied = 0
    provenance_items: list[dict[str, Any]] = []
    for entry in entries:
        if entry["role"] in {"main_video", "ignored"}:
            continue
        source_path = folder / entry["path"]
        # An extension is a claim about content, and the renderer believes it.
        # A text file called cover.png reaches the frame as a picture that
        # cannot be decoded, and the failure surfaces minutes later inside
        # ffmpeg rather than here, where the file is still in someone's hand.
        if entry["kind"] in {"video", "image"}:
            # A still has no duration, so it is asked the question a still can
            # answer: does a picture of positive size decode out of it. Asking
            # only whether a video stream is declared is not enough — ffprobe
            # reports a 0x0 png stream for a text file called cover.png.
            from editor_server import ffprobe_visual_dimensions

            readable = ffprobe_visual_dimensions(source_path) is not None
            if readable and entry["kind"] == "video":
                try:
                    probe_media(source_path)
                except (ValueError, RuntimeError):
                    readable = False
            if not readable:
                warnings.append(
                    f"{entry['path']} is named like a {entry['kind']} but nothing "
                    "could read a picture out of it; left out"
                )
                entry["role"] = "ignored"
                continue
        # 16-hex content prefix keeps names readable while making a
        # case-insensitive-filesystem collision imply identical content
        # (which is then a legitimate dedupe, not silent loss).
        destination = imported_dir / f"{entry['sha256'][:16]}-{source_path.name}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            shutil.copy2(source_path, destination)
            copied += 1
        provenance_items.append(
            {
                "asset_id": f"asset-{entry['sha256'][:16]}",
                "path": destination.relative_to(project_dir).as_posix(),
                "sha256": entry["sha256"],
                "origin": "folder-import",
                "provider_id": None,
                "source_url": None,
                "license": {
                    "spdx": "user-owned-pending",
                    "attribution_required": False,
                    "attribution_text": "",
                    "verified_at": now_utc(),
                },
                "review_status": "pending",
            }
        )
    seen_assets: set[str] = set()
    provenance_items = [
        item
        for item in provenance_items
        if not (item["asset_id"] in seen_assets or seen_assets.add(item["asset_id"]))
    ]
    provenance = {"schema_version": 1, "items": provenance_items}
    errors = contract_registry.validate_artifact("asset_provenance", provenance)
    if errors:
        return die("asset provenance failed contract validation: " + "; ".join(errors))
    write_json(project_dir / "working/asset_provenance.json", provenance)
    inventory = {
        "schema_version": 1,
        "project_id": manifest["project_id"],
        "scanned_at": now_utc(),
        "root_display_name": folder.name,
        "files": entries,
        "main_video_path": main_rel,
        "warnings": warnings,
    }
    errors = contract_registry.validate_artifact("folder_inventory", inventory)
    if errors:
        return die("folder inventory failed contract validation: " + "; ".join(errors))
    write_json(project_dir / "working/folder_inventory.json", inventory)
    print(
        json.dumps(
            {
                "ok": True,
                "project_dir": str(project_dir),
                "main_video": main_rel,
                "files": len(entries),
                "assets_copied": copied,
                "warnings": warnings,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_apply_narrative_plan(args: argparse.Namespace) -> int:
    """Apply a narrative_edit_plan to the editor state's unified segments."""
    try:
        import contract_registry
        from editor_server import (
            STATE_REL,
            atomic_write_json,
            default_editor_state,
            editor_state_revision,
            migrate_editor_state_v1_to_v2,
            validate_editor_state,
        )
    except ImportError as exc:
        return die(f"cannot load timeline contract: {exc}")
    project_dir = Path(args.project_dir).expanduser().resolve()
    manifest_path = project_dir / "project.json"
    if not manifest_path.is_file():
        return die(f"project manifest not found: {manifest_path}")
    manifest = read_json(manifest_path)
    plan = read_json(Path(args.plan).expanduser())
    if args.draft:
        plan.setdefault("schema_version", 1)
        plan.setdefault("source_sha256", str(manifest.get("source", {}).get("sha256") or ""))
        plan.setdefault("warnings", [])
        segments = plan.get("segments") or []
        starts = [segment.get("source_start") for segment in segments]
        reorder = starts != sorted(starts)
        plan.setdefault("reorder", reorder)
        plan.setdefault("risk", "high" if reorder else "low")
        plan.setdefault(
            "reanchor", {"status": "stale", "transcript_revision": "0" * 64}
        )
        plan["plan_hash"] = contract_registry.canonical_hash(
            {key: value for key, value in plan.items() if key != "plan_hash"}
        )
    errors = contract_registry.validate_artifact("narrative_edit_plan", plan)
    if errors:
        return die("narrative plan failed contract validation: " + "; ".join(errors))
    if plan.get("reorder") and not args.confirm_high_risk:
        return die(
            "this plan changes the source order (high-risk); re-run with "
            "--confirm-high-risk after reviewing it"
        )
    state_path = project_dir / STATE_REL
    if state_path.is_file():
        state = read_json(state_path)
    else:
        state = default_editor_state(project_dir, manifest)
    state, _migrated = migrate_editor_state_v1_to_v2(project_dir, manifest, state)
    state["segments"] = [
        {
            "id": str(segment["id"]),
            "source_start": float(segment["source_start"]),
            "source_end": float(segment["source_end"]),
            "origin": "narrative",
        }
        for segment in plan["segments"]
    ]
    duration = float(manifest.get("source", {}).get("duration_s") or 0.0)
    state_errors = validate_editor_state(state, duration)
    if state_errors:
        return die("resulting editor state is invalid: " + "; ".join(state_errors))
    state["updated_at"] = now_utc()
    state["revision"] = editor_state_revision(state)
    atomic_write_json(state_path, state)
    write_json(project_dir / "working/narrative_edit_plan.json", plan)
    post_cut = sum(
        float(segment["source_end"]) - float(segment["source_start"])
        for segment in plan["segments"]
    )
    print(
        json.dumps(
            {
                "ok": True,
                "segments": len(plan["segments"]),
                "post_cut_duration_s": round(post_cut, 3),
                "reorder": bool(plan.get("reorder")),
                "state_revision": state["revision"],
                "note": "existing approvals are stale until re-confirmed",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    source = Path(args.input).expanduser().resolve()
    project_dir = Path(args.project_dir).expanduser().resolve()
    try:
        voice = make_voice_config(args)
        manifest = initialize_project(
            source,
            project_dir,
            source_language=args.source_language,
            transcription_glossary=args.transcription_glossary,
            transcription_calibrations=args.transcription_calibration,
            contextual_semantic_calibration=args.contextual_semantic_calibration,
            semantic_model=args.semantic_model,
            subtitle_mode=args.subtitle_mode,
            source_has_burned_in=args.source_has_burned_in,
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
    contextual_config = subtitles.get("contextual_semantic_calibration", {})
    if contextual_config and not isinstance(contextual_config, dict):
        errors.append("contextual semantic calibration config must be an object")
    elif isinstance(contextual_config, dict):
        enabled = contextual_config.get("enabled", False)
        if not isinstance(enabled, bool):
            errors.append("contextual semantic calibration enabled must be boolean")
        if contextual_config.get("provider", "ollama") != "ollama":
            errors.append("contextual semantic calibration provider must be ollama")
        semantic_model = str(contextual_config.get("model", "qwen2.5:7b"))
        if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,120}", semantic_model):
            errors.append("contextual semantic calibration model name is invalid")
        minimum_confidence = contextual_config.get("minimum_confidence", 0.92)
        if (
            isinstance(minimum_confidence, bool)
            or not isinstance(minimum_confidence, (int, float))
            or not 0.5 <= float(minimum_confidence) <= 1
        ):
            errors.append("contextual semantic confidence must be between 0.5 and 1")
    raw_contextual_rules = subtitles.get("contextual_calibrations", [])
    if not isinstance(raw_contextual_rules, list) or len(raw_contextual_rules) > 512:
        errors.append("contextual semantic calibration rules must be an array of at most 512")
    else:
        try:
            for raw_rule in raw_contextual_rules:
                normalize_transcription_calibrations([raw_rule])
        except ValueError as exc:
            errors.append(str(exc))

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


def caption_display_units(text: str) -> int:
    return sum(2 if ord(character) > 127 else 1 for character in text)


def join_caption_words(words: list[dict[str, Any]]) -> str:
    """Caption text for these words, by the shared spacing rule."""
    return text_joining.join_tokens(word.get("text", "") for word in words)


def readable_caption_segments(
    segments: list[dict[str, Any]],
    words: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not words:
        return [dict(segment) for segment in segments]
    captions: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []

    def flush() -> None:
        if not current:
            return
        captions.append(
            {
                "id": f"caption-segment-{len(captions) + 1:04d}",
                "start": round(float(current[0]["start"]), 3),
                "end": round(float(current[-1]["end"]), 3),
                "text": join_caption_words(current),
                "word_ids": [str(item["id"]) for item in current],
            }
        )
        current.clear()

    # Where the recogniser itself ended a segment is a boundary worth
    # keeping: some engines return phrase-level segments without punctuation,
    # and splitting those again on length alone cuts mid-phrase.
    #
    # Which segment a word belongs to is recorded on the word. Deciding it by
    # comparing timestamps instead means any word that happens to end on the
    # same rounded second as some segment elsewhere in the video ends a line
    # — on an eight-minute lesson that fired 442 times and left captions one
    # character long.
    def segment_of(word: dict[str, Any]) -> str:
        return str(word.get("segment_id") or "")

    for word in words:
        if current and segment_of(current[-1]) and segment_of(current[-1]) != segment_of(word):
            flush()
        if current:
            gap = float(word["start"]) - float(current[-1]["end"])
            candidate = current + [word]
            candidate_duration = float(word["end"]) - float(current[0]["start"])
            candidate_units = caption_display_units(join_caption_words(candidate))
            if gap >= 0.7 or candidate_duration > 5.5 or candidate_units > 56:
                flush()
        current.append(word)
        text = join_caption_words(current)
        duration = float(current[-1]["end"]) - float(current[0]["start"])
        if re.search(r"[。！？!?]\s*$", text) and duration >= 0.8:
            flush()
        elif re.search(r"[，,；;：:]\s*$", text) and duration >= 2.4:
            flush()
    flush()
    return captions


def build_transcript_review(
    transcript: dict[str, Any],
    source_language_mode: str,
    glossary: list[str],
    semantic_calibration: dict[str, Any],
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    transcript_text = normalized_token(str(transcript.get("text", "")))
    glossary_words = {
        word.casefold()
        for term in glossary
        for word in re.findall(r"[A-Za-z]+", term)
    }
    for term in glossary:
        if normalized_token(term) not in transcript_text:
            issues.append(
                {
                    "code": "missing_glossary_term",
                    "severity": "warning",
                    "term": term,
                    "message": f"Glossary term was not found in the transcript: {term}",
                }
            )
    for word in transcript.get("words", []):
        confidence = word.get("confidence")
        text = str(word.get("text", ""))
        latin_words = [item.casefold() for item in re.findall(r"[A-Za-z]+", text)]
        unknown_latin_words = [
            item
            for item in latin_words
            if item not in glossary_words and item not in {"ok", "so"}
        ]
        if (
            isinstance(confidence, (int, float))
            and float(confidence) < 0.35
            and unknown_latin_words
        ):
            issues.append(
                {
                    "code": "low_confidence_latin_token",
                    "severity": "warning",
                    "text": text,
                    "start": word.get("start"),
                    "end": word.get("end"),
                    "confidence": confidence,
                    "message": "Low-confidence Latin token may be an English code-switch error",
                }
            )
    if source_language_mode == "zh-en" and not glossary:
        issues.append(
            {
                "code": "mixed_language_without_glossary",
                "severity": "notice",
                "message": "Mixed Chinese/English mode has no project terminology glossary",
            }
        )
    chinese_or_auto = source_language_mode in {"auto", "zh-TW", "zh-CN", "zh-en"}
    semantic_status = str(semantic_calibration.get("status", "not_configured"))
    if issues:
        risk_status = "warning"
    elif chinese_or_auto and semantic_status == "not_configured":
        risk_status = "semantic_review_required"
    elif chinese_or_auto:
        risk_status = "review_required"
    else:
        risk_status = "clear"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "needs_review",
        "risk_status": risk_status,
        "human_review_required": True,
        "source_language_mode": source_language_mode,
        "glossary": glossary,
        "mechanical_issue_count": len(issues),
        "issue_count": len(issues),
        "issues": issues,
        "semantic_calibration": semantic_calibration,
        "generated_at": now_utc(),
    }


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

    caption_segments = readable_caption_segments(segments, flat_words)
    transcript = {
        "schema_version": SCHEMA_VERSION,
        # Provenance follows the engine that produced this, not the format it
        # was written in: a wrong label here is how a transcript gets trusted
        # for the wrong reasons later.
        "engine": str(data.get("engine") or "openai-whisper"),
        "language": data.get("language"),
        "duration_s": round(duration_s, 3),
        "text": str(data.get("text", "")).strip(),
        "segments": segments,
        "caption_segments": caption_segments,
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
            caption_render_decision,
            editor_state_revision,
            effect_keywords_for_caption,
        )
    except ImportError as exc:
        raise ValueError(f"cannot load editor transcript bridge: {exc}") from exc
    # Same decision as the bootstrap path, made by the same function: a
    # project that skips captions must not have them reappear on re-sync.
    render_captions, caption_reason = caption_render_decision(project_dir, manifest)
    # Both places that build caption overlays attach translations, or the
    # second line appears and disappears depending on which one ran last.
    from editor_server import caption_translations
    translations = caption_translations(project_dir)
    director_id = str(state.get("director_style") or "teacher-punch")
    director = DIRECTOR_PRESETS.get(director_id, DIRECTOR_PRESETS["teacher-punch"])
    caption_style = dict(state.get("caption_defaults") or director["caption"])
    emphasis_plan = read_json(project_dir / "working/emphasis_plan.json")
    semantic_review_path = project_dir / "working/transcript_semantic_review.json"
    semantic_review = (
        read_json(semantic_review_path) if semantic_review_path.is_file() else {}
    )
    pending_by_unit: dict[str, list[dict[str, Any]]] = {}
    for item in semantic_review.get("pending", []):
        if not isinstance(item, dict):
            continue
        unit_id = str(item.get("unit_id", ""))
        if not unit_id:
            continue
        pending_by_unit.setdefault(unit_id, []).append(
            {
                "source": str(item.get("source", "")),
                "replacement": str(item.get("replacement", "")),
                "reason": str(item.get("reason", ""))[:500],
                "confidence": item.get("confidence"),
                "verifier_confidence": item.get("verifier_confidence"),
                "pending_reason": str(item.get("pending_reason", "")),
            }
        )
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
    source_segments = transcript.get("caption_segments") or transcript.get("segments", [])
    if not render_captions:
        source_segments = []
    state["caption_generation"] = {
        "enabled": render_captions,
        "reason": caption_reason,
    }
    for index, segment in enumerate(source_segments, start=1):
        text = str(segment.get("text", "")).strip()
        if not text:
            continue
        segment_start = round(float(segment.get("start", 0.0)), 3)
        segment_end = round(float(segment.get("end", 0.0)), 3)
        unit_id = str(segment.get("id", ""))
        pending_candidates = pending_by_unit.get(unit_id, [])
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
                **({"translation": translations[text]} if text in translations else {}),
                "semantic_review": (
                    {
                        "status": "pending",
                        "unit_id": unit_id,
                        "candidates": pending_candidates,
                    }
                    if pending_candidates
                    else {"status": "checked", "unit_id": unit_id, "candidates": []}
                ),
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
            "editorial": item.get("editorial"),
            "review_status": str(item["review_status"]),
            "score": item["score"],
            "source": "working/highlight_plan.json",
        }
        for item in plan.get("items", [])[:10]
    ]
    current_highlight_ids = {item["id"] for item in highlights}
    for overlay in state.get("overlays", []):
        if not isinstance(overlay, dict):
            continue
        scoped_id = str(overlay.get("highlight_id") or "")
        if not scoped_id or scoped_id in current_highlight_ids:
            continue
        try:
            overlay_start = float(overlay.get("start"))
            overlay_end = float(overlay.get("end"))
        except (TypeError, ValueError):
            overlay.pop("highlight_id", None)
            continue
        best_highlight: dict[str, Any] | None = None
        best_overlap = 0.0
        for highlight in highlights:
            overlap = max(
                0.0,
                min(overlay_end, float(highlight["end"]))
                - max(overlay_start, float(highlight["start"])),
            )
            if overlap > best_overlap:
                best_highlight = highlight
                best_overlap = overlap
        if best_highlight is None:
            overlay.pop("highlight_id", None)
        else:
            overlay["highlight_id"] = best_highlight["id"]
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
    project_dir = manifest_path.parent
    subtitles = manifest.get("subtitles", {})
    if not isinstance(subtitles, dict):
        raise ValueError("manifest subtitles must be an object")
    source_language_mode = str(subtitles.get("source_language", "auto"))
    glossary = normalize_transcription_glossary(
        subtitles.get("glossary", [])
    )
    calibrations = normalize_transcription_calibrations(
        subtitles.get("calibrations", [])
    )
    contextual_rules: list[dict[str, Any]] = []
    raw_contextual_rules = subtitles.get("contextual_calibrations", [])
    if not isinstance(raw_contextual_rules, list):
        raise ValueError("contextual semantic calibration rules must be an array")
    if len(raw_contextual_rules) > 512:
        raise ValueError("too many contextual semantic calibration rules")
    for raw_rule in raw_contextual_rules:
        contextual_rules.extend(normalize_transcription_calibrations([raw_rule]))
    data = read_json(whisper_path)
    if not isinstance(data.get("segments"), list):
        raise ValueError("Whisper JSON must contain a segments array")
    semantic_corrections = apply_transcription_calibrations(data, calibrations)
    glossary_corrections = apply_glossary_corrections(data, glossary)
    orthography_enabled = should_normalize_taiwan_traditional(
        manifest,
        data.get("language"),
    )
    orthography = (
        normalize_whisper_orthography(data)
        if orthography_enabled
        else {
            "variant": None,
            "configuration": None,
            "backend": None,
            "changed_strings": 0,
            "changed_characters": 0,
        }
    )
    contextual_corrections = apply_transcription_calibrations(
        data,
        contextual_rules,
    )
    duration_s = float(manifest.get("source", {}).get("duration_s", 0.0))
    transcript, compatibility = whisper_payload(data, duration_s)
    transcript["orthography_variant"] = orthography["variant"]
    transcript_path = project_dir / "working/transcript_words.json"
    compatibility_path = project_dir / "working/subtitles_words.json"
    review_path = project_dir / "working/transcript_review.json"
    calibration_path = project_dir / "working/transcript_calibration.json"
    orthography_path = project_dir / "working/transcript_orthography.json"
    contextual_review_path = project_dir / "working/transcript_semantic_review.json"
    contextual_config = subtitles.get("contextual_semantic_calibration", {})
    if not isinstance(contextual_config, dict):
        contextual_config = {}
    if contextual_review_path.is_file():
        contextual_review = read_json(contextual_review_path)
    else:
        contextual_review = {
            "schema_version": SCHEMA_VERSION,
            "status": (
                "pending"
                if contextual_config.get("enabled")
                else "not_configured"
            ),
            "coverage_status": "not_started",
            "reviewed_unit_count": 0,
            "total_unit_count": len(transcript.get("caption_segments", [])),
            "accepted_count": 0,
            "pending_count": 0,
            "rejected_count": 0,
            "rules": raw_contextual_rules,
            "human_review_required": True,
            "generated_at": now_utc(),
        }
    if contextual_review.get("coverage_status") in {None, "not_started"}:
        contextual_review["reviewed_unit_count"] = 0
        contextual_review["total_unit_count"] = len(
            transcript.get("caption_segments", [])
        )
    contextual_review["applied_correction_count"] = len(contextual_corrections)
    contextual_review["active_rule_count"] = len(contextual_rules)
    if contextual_config.get("enabled") or contextual_rules or contextual_review_path.is_file():
        write_json(contextual_review_path, contextual_review)
    semantic_calibration = {
        "status": "applied_needs_review" if calibrations else "not_configured",
        "rule_count": len(calibrations),
        "correction_count": len(semantic_corrections),
        "human_review_required": True,
        "artifact": "working/transcript_calibration.json",
    }
    write_json(
        calibration_path,
        {
            "schema_version": SCHEMA_VERSION,
            **semantic_calibration,
            "rules": calibrations,
            "corrections": semantic_corrections,
            "generated_at": now_utc(),
        },
    )
    write_json(
        orthography_path,
        {
            "schema_version": SCHEMA_VERSION,
            "status": "applied" if orthography_enabled else "not_requested",
            **orthography,
            "source_language_mode": source_language_mode,
            "generated_at": now_utc(),
        },
    )
    write_json(transcript_path, transcript)
    write_json(compatibility_path, compatibility)
    transcript_review = build_transcript_review(
        transcript,
        source_language_mode,
        glossary,
        semantic_calibration,
    )
    transcript_review["contextual_semantic_calibration"] = {
        "status": contextual_review.get("status", "not_configured"),
        "coverage_status": contextual_review.get("coverage_status", "not_started"),
        "reviewed_unit_count": int(contextual_review.get("reviewed_unit_count", 0)),
        "total_unit_count": int(
            contextual_review.get(
                "total_unit_count",
                len(transcript.get("caption_segments", [])),
            )
        ),
        "accepted_count": int(contextual_review.get("accepted_count", 0)),
        "pending_count": int(contextual_review.get("pending_count", 0)),
        "rejected_count": int(contextual_review.get("rejected_count", 0)),
        "applied_correction_count": len(contextual_corrections),
        "human_review_required": True,
        "artifact": "working/transcript_semantic_review.json",
    }
    write_json(review_path, transcript_review)
    if srt_path is not None:
        srt_path = srt_path.expanduser().resolve()
        if not srt_path.is_file():
            raise ValueError(f"SRT not found: {srt_path}")
    write_transcript_srt(project_dir / "subtitles/source.srt", transcript)
    manifest.setdefault("stages", {})["transcribe"] = "complete"
    manifest["stages"]["edit_analysis"] = "pending"
    manifest.setdefault("artifacts", {})["transcript_compatibility"] = (
        "working/subtitles_words.json"
    )
    manifest["artifacts"]["transcript_review"] = "working/transcript_review.json"
    manifest["artifacts"]["transcript_calibration"] = (
        "working/transcript_calibration.json"
    )
    manifest["artifacts"]["transcript_orthography"] = (
        "working/transcript_orthography.json"
    )
    manifest["artifacts"]["transcript_semantic_review"] = (
        "working/transcript_semantic_review.json"
    )
    if orthography_enabled:
        manifest.setdefault("subtitles", {})["source_variant"] = ORTHOGRAPHY_VARIANT
    manifest["transcription"] = {
        "engine": "openai-whisper",
        "model": model,
        "language": data.get("language"),
        "source_language_mode": source_language_mode,
        "code_switching": source_language_mode == "zh-en",
        "glossary": glossary,
        "semantic_calibration_status": semantic_calibration["status"],
        "semantic_calibration_rule_count": len(calibrations),
        "semantic_calibration_correction_count": len(semantic_corrections),
        "contextual_semantic_status": contextual_review.get(
            "status",
            "not_configured",
        ),
        "contextual_semantic_coverage_status": contextual_review.get(
            "coverage_status",
            "not_started",
        ),
        "contextual_semantic_reviewed_units": int(
            contextual_review.get("reviewed_unit_count", 0)
        ),
        "contextual_semantic_total_units": int(
            contextual_review.get(
                "total_unit_count",
                len(transcript.get("caption_segments", [])),
            )
        ),
        "contextual_semantic_rule_count": len(contextual_rules),
        "contextual_semantic_correction_count": len(contextual_corrections),
        "contextual_semantic_pending_count": int(
            contextual_review.get("pending_count", 0)
        ),
        "orthography_variant": orthography["variant"],
        "orthography_backend": orthography["backend"],
        "orthography_conversion_count": orthography["changed_strings"],
        "review_status": transcript_review["status"],
        "review_issue_count": transcript_review["issue_count"],
        "glossary_correction_count": len(glossary_corrections),
        "word_count": len(transcript["words"]),
        "segment_count": len(transcript["segments"]),
        "caption_count": len(transcript["caption_segments"]),
        "source_json": str(whisper_path),
        "imported_at": now_utc(),
    }
    manifest["updated_at"] = now_utc()
    invalidate_approvals(
        manifest,
        ("destructive_edit", "highlight_selection", "timeline", "final"),
        "Invalidated because the source transcript changed",
    )
    write_json(manifest_path, manifest)
    synced = sync_transcript_to_editor(project_dir)
    return {
        "ok": True,
        "transcript": str(transcript_path),
        "compatibility": str(compatibility_path),
        "transcript_review": str(review_path),
        "transcript_review_issues": transcript_review["issue_count"],
        "semantic_calibration_status": semantic_calibration["status"],
        "semantic_calibration_corrections": len(semantic_corrections),
        "contextual_semantic_status": contextual_review.get(
            "status",
            "not_configured",
        ),
        "contextual_semantic_coverage_status": contextual_review.get(
            "coverage_status",
            "not_started",
        ),
        "contextual_semantic_corrections": len(contextual_corrections),
        "contextual_semantic_pending": int(contextual_review.get("pending_count", 0)),
        "glossary_corrections": len(glossary_corrections),
        "orthography_variant": orthography["variant"],
        "orthography_conversions": orthography["changed_strings"],
        "words": len(transcript["words"]),
        "segments": len(transcript["segments"]),
        "synced_editor_captions": synced,
        "code_switching": source_language_mode == "zh-en",
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


def contextual_semantic_baseline(
    manifest_path: Path,
    manifest: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    """Rebuild the pre-contextual transcript from immutable Whisper output."""

    transcription = manifest.get("transcription", {})
    if not isinstance(transcription, dict):
        raise ValueError("manifest transcription must be an object")
    raw_source = str(transcription.get("source_json", "")).strip()
    if not raw_source:
        raise ValueError("transcribe the video before semantic calibration")
    whisper_path = Path(raw_source).expanduser()
    if not whisper_path.is_absolute():
        whisper_path = manifest_path.parent / whisper_path
    whisper_path = whisper_path.resolve()
    data = read_json(whisper_path)
    if not isinstance(data.get("segments"), list):
        raise ValueError("Whisper JSON must contain a segments array")
    subtitles = manifest.get("subtitles", {})
    if not isinstance(subtitles, dict):
        raise ValueError("manifest subtitles must be an object")
    calibrations = normalize_transcription_calibrations(
        subtitles.get("calibrations", [])
    )
    glossary = normalize_transcription_glossary(subtitles.get("glossary", []))
    apply_transcription_calibrations(data, calibrations)
    apply_glossary_corrections(data, glossary)
    if should_normalize_taiwan_traditional(manifest, data.get("language")):
        normalize_whisper_orthography(data)
    duration_s = float(manifest.get("source", {}).get("duration_s", 0.0))
    transcript, _compatibility = whisper_payload(data, duration_s)
    return whisper_path, transcript


def cmd_semantic_calibrate(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).expanduser().resolve()
    try:
        manifest = read_json(manifest_path)
        subtitles = manifest.get("subtitles", {})
        if not isinstance(subtitles, dict):
            raise ValueError("manifest subtitles must be an object")
        glossary = normalize_transcription_glossary(subtitles.get("glossary", []))
        whisper_path, transcript = contextual_semantic_baseline(
            manifest_path,
            manifest,
        )
        review_path = manifest_path.parent / "working/transcript_semantic_review.json"
        previous_review = read_json(review_path) if review_path.is_file() else {}
        if args.proposals_json:
            proposal_path = Path(args.proposals_json).expanduser().resolve()
            proposal_payload = read_json(proposal_path)
            provider_used = "proposal_file"
        else:
            def write_semantic_progress(progress: dict[str, Any]) -> None:
                write_json(
                    review_path,
                    {
                        "schema_version": SCHEMA_VERSION,
                        "status": "running",
                        "coverage_status": "partial",
                        "provider": args.provider,
                        "model": args.model,
                        "context_radius": 2,
                        "document_context_unit_count": progress[
                            "total_unit_count"
                        ],
                        "reviewed_unit_count": progress["reviewed_unit_count"],
                        "total_unit_count": progress["total_unit_count"],
                        "candidate_count": progress["candidate_count"],
                        "model_error_count": progress["model_error_count"],
                        "human_review_required": True,
                        "updated_at": now_utc(),
                    },
                )

            proposal_payload = propose_contextual_corrections(
                transcript,
                glossary=glossary,
                model_call=lambda prompt, stage: ollama_json_model_call(
                    prompt,
                    stage,
                    model=args.model,
                    timeout=args.timeout,
                ),
                batch_size=args.batch_size,
                progress_callback=write_semantic_progress,
            )
            provider_used = args.provider

        current_items = proposal_payload.get("items", [])
        if not isinstance(current_items, list):
            current_items = []
        previous_accepted = previous_review.get("accepted", [])
        if not isinstance(previous_accepted, list):
            previous_accepted = []
        previous_pending = previous_review.get("pending", [])
        if not isinstance(previous_pending, list):
            previous_pending = []
        current_keys = {
            (
                str(item.get("unit_id", "")),
                str(item.get("source", "")),
                str(item.get("replacement", "")),
            )
            for item in current_items
            if isinstance(item, dict)
        }
        preservable = previous_accepted + (
            previous_pending if args.proposals_json else []
        )
        preserved = [
            item
            for item in preservable
            if isinstance(item, dict)
            and (
                str(item.get("unit_id", "")),
                str(item.get("source", "")),
                str(item.get("replacement", "")),
            )
            not in current_keys
        ]
        validation_payload = {
            **proposal_payload,
            "items": current_items + preserved,
        }
        result = validate_contextual_proposals(
            transcript,
            validation_payload,
            glossary=glossary,
            minimum_confidence=args.minimum_confidence,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return die(str(exc))

    coverage_complete = result["coverage_status"] == "complete"
    status = "complete_needs_review" if coverage_complete else "partial_needs_review"
    errors = proposal_payload.get("errors", [])
    if not isinstance(errors, list):
        errors = []
    generated_at = now_utc()
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "coverage_status": result["coverage_status"],
        "provider": provider_used,
        "model": args.model,
        "context_radius": 2,
        "document_context_unit_count": result["total_unit_count"],
        "minimum_confidence": result["minimum_confidence"],
        "reviewed_unit_ids": result["reviewed_unit_ids"],
        "reviewed_unit_count": result["reviewed_unit_count"],
        "total_unit_count": result["total_unit_count"],
        "accepted_count": result["applied_count"],
        "pending_count": result["pending_count"],
        "rejected_count": result["rejected_count"],
        "accepted": result["accepted"],
        "pending": result["pending"],
        "rejected": result["rejected"],
        "rules": result["rules"],
        "model_errors": errors,
        "human_review_required": True,
        "generated_at": generated_at,
    }
    subtitles["contextual_calibrations"] = result["rules"]
    contextual_config = subtitles.get("contextual_semantic_calibration", {})
    if not isinstance(contextual_config, dict):
        contextual_config = {}
    subtitles["contextual_semantic_calibration"] = {
        **contextual_config,
        "enabled": True,
        "provider": args.provider,
        "model": args.model,
        "minimum_confidence": result["minimum_confidence"],
        "context_radius": 2,
        "last_status": status,
        "last_run_at": generated_at,
    }
    manifest["subtitles"] = subtitles
    manifest.setdefault("artifacts", {})["transcript_semantic_review"] = (
        "working/transcript_semantic_review.json"
    )
    manifest.setdefault("stages", {})["edit_analysis"] = "pending"
    manifest["updated_at"] = generated_at
    write_json(review_path, artifact)
    write_json(manifest_path, manifest)

    try:
        imported = import_whisper_artifacts(
            manifest_path,
            whisper_path,
            srt_path=None,
            model=str(manifest.get("transcription", {}).get("model", "unknown")),
        )
    except ValueError as exc:
        return die(str(exc))
    refreshed_artifact = read_json(review_path)
    emit(
        {
            "ok": coverage_complete,
            "manifest": str(manifest_path),
            "artifact": str(review_path),
            "status": status,
            "coverage_status": result["coverage_status"],
            "reviewed_units": result["reviewed_unit_count"],
            "total_units": result["total_unit_count"],
            "accepted": result["applied_count"],
            "applied_corrections": refreshed_artifact.get(
                "applied_correction_count",
                imported.get("contextual_semantic_corrections", 0),
            ),
            "pending": result["pending_count"],
            "rejected": result["rejected_count"],
            "model_errors": len(errors),
        }
    )
    return 0 if coverage_complete else 3


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


def transcribe_with_breeze(
    manifest_path: Path, source: Path, args: argparse.Namespace
) -> int | None:
    """Transcribe with the Taiwan-tuned recogniser, or None to fall back.

    General checkpoints mishear Taiwanese place names, brands and homophones,
    and those words end up on screen. Returning None when the runtime is
    absent keeps a project transcribable on a machine that has not installed
    it, rather than failing outright.
    """
    import breeze_asr

    ok, reason = breeze_asr.available()
    if not ok:
        if args.model == "breeze":
            die(f"Breeze was requested but is unavailable: {reason}")
            return 1
        return None
    run_dir = manifest_path.parent / "working/whisper-local" / uuid.uuid4().hex
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        payload = breeze_asr.transcribe(source)
    except Exception as exc:  # the runtime is a separate process; it can fail many ways
        manifest = read_json(manifest_path)
        manifest.setdefault("stages", {})["transcribe"] = "failed"
        manifest["transcription"] = {
            "engine": "breeze-asr-25",
            "model": breeze_asr.MODEL_REPO,
            "status": "failed",
            "error_code": "breeze_failed",
            "updated_at": now_utc(),
        }
        write_json(manifest_path, manifest)
        return die(f"Breeze transcription failed: {exc}")
    transcript_json = run_dir / f"{source.stem}.json"
    write_json(transcript_json, payload)
    try:
        imported = import_whisper_artifacts(
            manifest_path, transcript_json, srt_path=None, model=breeze_asr.MODEL_REPO
        )
    except ValueError as exc:
        return die(str(exc))
    imported["local"] = True
    imported["engine"] = "breeze-asr-25"
    emit(imported)
    return 0


def cmd_transcribe_local(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).expanduser().resolve()
    try:
        manifest = read_json(manifest_path)
        source = project_staged_source(manifest_path, manifest)
    except ValueError as exc:
        return die(str(exc))
    language = str(manifest.get("subtitles", {}).get("source_language", "auto"))
    if language in {"zh-TW", "zh-en"} and args.model in {"auto", "breeze"}:
        result = transcribe_with_breeze(manifest_path, source, args)
        if result is not None:
            return result
    if args.model in {"auto", "breeze"}:
        # Either this is not a Taiwanese project or the tuned runtime is not
        # installed; whisper needs an actual size to load.
        args.model = "large-v3"
    whisper = os.environ.get("WHISPER_BIN", "").strip() or shutil.which("whisper")
    if not whisper:
        return die("local Whisper CLI is not installed")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", args.model):
        return die("Whisper model name is invalid")
    run_dir = manifest_path.parent / "working/whisper-local" / uuid.uuid4().hex
    run_dir.mkdir(parents=True, exist_ok=False)
    language = str(manifest.get("subtitles", {}).get("source_language", "auto"))
    glossary = normalize_transcription_glossary(
        manifest.get("subtitles", {}).get("glossary", [])
    )
    initial_prompt = transcription_initial_prompt(language, glossary)
    language_map = {
        "zh-TW": "zh",
        "zh-CN": "zh",
        "zh-en": "zh",
        "en-US": "en",
        "en-GB": "en",
    }
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
    if initial_prompt:
        command.extend(["--initial_prompt", initial_prompt])
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


def apply_editorial_selection(
    plan: dict[str, Any],
    transcript: dict[str, Any],
    manifest: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Replace scored windows with a model's cuts, or keep the scored ones.

    A model that is unreachable, slow, or talking nonsense must not cost the
    project its highlights: the deterministic plan is already built and stays
    exactly as it was, with the reason recorded in the plan's warnings.
    """
    import editorial_planner

    bounds = (plan.get("configuration") or {}).get("duration_bounds_s") or {}
    try:
        items, warnings = editorial_planner.plan_editorial_highlights(
            transcript,
            duration_s=float(manifest.get("source", {}).get("duration_s", 0.0)),
            count=int(args.count),
            min_duration=float(bounds.get("min") or 8.0),
            max_duration=float(bounds.get("max") or 90.0),
            brief=str(getattr(args, "brief", "") or ""),
            provider=tuple(shlex.split(args.editorial_provider))
            if getattr(args, "editorial_provider", "")
            else None,
            timeout_s=int(getattr(args, "editorial_timeout", editorial_planner.DEFAULT_TIMEOUT_S)),
        )
    except (editorial_planner.EditorialUnavailable, ValueError) as exc:
        plan.setdefault("warnings", []).append(
            f"editorial selection unavailable, kept the scored plan: {exc}"
        )
        return plan
    plan["items"] = items
    plan["generator"] = editorial_planner.GENERATOR
    plan["status"] = "needs_review" if items else plan.get("status", "needs_transcript")
    plan.setdefault("warnings", []).extend(warnings)
    plan["plan_revision"] = highlight_planner_hash(plan)
    return plan


def highlight_planner_hash(plan: dict[str, Any]) -> str:
    from highlight_planner import canonical_hash

    return canonical_hash(
        {key: value for key, value in plan.items() if key not in {"generated_at", "plan_revision"}}
    )


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
    if getattr(args, "editorial", False):
        plan = apply_editorial_selection(plan, transcript, manifest, args)
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


def cmd_add_card(args: argparse.Namespace) -> int:
    """Place one card by hand. It outranks anything a model proposes."""
    import card_plan

    manifest_path = Path(args.manifest).expanduser().resolve()
    project_dir = manifest_path.parent
    try:
        manifest = read_json(manifest_path)
    except ValueError as exc:
        return die(str(exc))
    payload: dict[str, Any] = {}
    for name in ("icon", "meta", "body", "text", "lead"):
        value = getattr(args, name, "")
        if value:
            payload[name] = value
    if getattr(args, "waveform", False):
        payload["waveform"] = True
    if args.title:
        payload["title"] = args.title
    if args.subtitle:
        payload["subtitle"] = args.subtitle
    if args.kicker:
        payload["kicker"] = args.kicker
    if args.value:
        payload["value"] = args.value
        payload.setdefault("label", args.title or args.value)
    if not payload:
        return die("a card needs content: --title, --value or --text")
    try:
        plan, notes = card_plan.add(
            project_dir,
            str(manifest.get("source", {}).get("sha256") or ""),
            start=float(args.at),
            end=float(args.at) + float(args.seconds),
            kind=args.kind,
            payload=payload,
            note=args.note or "",
        )
    except ValueError as exc:
        return die(str(exc))
    emit(
        {
            "ok": True,
            "cards": len(plan["items"]),
            "plan": str(project_dir / card_plan.CARD_PLAN_REL),
            "notes": notes,
        }
    )
    return 0



def _step(label: str) -> None:
    """Progress goes to stderr so stdout stays one JSON document."""
    print(f"… {label}", file=sys.stderr, flush=True)


def _args_for(command: str, *argv: str) -> argparse.Namespace:
    """Build a sub-command's arguments through its own parser.

    Hand-assembling a Namespace means restating every default the parser
    already declares, and missing one shows up as an AttributeError deep
    inside the command. Let argparse fill them.
    """
    return build_parser().parse_args([command, *argv])


def materialise_clip(
    project_dir: Path,
    manifest: dict[str, Any],
    highlight: dict[str, Any],
    *,
    fit: str,
    cards: bool,
    trim_pauses: bool = True,
) -> dict[str, Any]:
    """Editor state for one highlight, with its visuals planned.

    Every step here already exists as a function used by the interactive
    editor; this calls those rather than restating them, because the one
    thing this codebase reliably gets wrong is the same decision written
    twice.
    """
    import visual_director
    from editor_server import (
        active_editorial_title,
        default_editor_state,
        publish_layer_bundle,
        read_json,
    )
    from video_analyzer import atomic_write_json

    state = default_editor_state(project_dir, manifest)
    base = state["segments"][0]
    clip_start = float(highlight["start"])
    clip_end = float(highlight["end"])
    # Dead air inside the clip is dropped, splitting the timeline around each
    # pause. The renderer was built for that shape from the start — captions
    # and cards split their windows across removed regions — and the editor's
    # analyzer already proposes the pauses; `cut` just never used either.
    pieces = [(clip_start, clip_end)]
    if trim_pauses:
        proposals = read_json(
            project_dir / "working/edit_candidates.json", {"items": []}
        ) or {"items": []}
        deletions = silence_deletions(
            proposals.get("items", []), clip_start, clip_end
        )
        if deletions:
            pieces = window_minus_deletions(clip_start, clip_end, deletions)
            removed = sum(e - s for s, e in deletions)
            print(
                json.dumps({"pauses_removed": {
                    "count": len(deletions), "seconds": round(removed, 2),
                }}, ensure_ascii=False),
                file=sys.stderr,
            )
    state["segments"] = [
        dict(base, id=f"{base.get('id', 'segment')}-p{index}" if index else base.get("id"),
             source_start=round(piece_start, 3), source_end=round(piece_end, 3))
        for index, (piece_start, piece_end) in enumerate(pieces)
    ]
    state["canvas"]["fit"] = fit
    state["active_highlight_id"] = str(highlight["id"])
    atomic_write_json(project_dir / "working/editor_state.json", state)

    if cards:
        evidence = read_json(project_dir / "working/evidence_map.json", None)
        if isinstance(evidence, dict) and evidence.get("items"):
            planned = visual_director.plan_visuals(
                # Planned over the whole clip, not the first surviving piece:
                # evidence lives on the source axis, and the renderer maps
                # every plan item across the removed pauses itself.
                planning_segments(
                    dict(base, source_start=clip_start, source_end=clip_end)
                ),
                evidence["items"],
                editorial_title=active_editorial_title(state),
            )
            errors = visual_director.validate(planned)
            if errors:
                raise ValueError("; ".join(errors[:3]))
            publish_layer_bundle(
                project_dir, planned["structured_layers"], planned["visual_plan"]
            )
    return state


# How much of a clip one card is allowed to speak for. The director decides a
# beat per segment, so handing it the clip as a single segment let it decide
# exactly once — and, since the first segment is the opening, that decision
# was always the opening title. Every other kind of card was unreachable.
PLANNING_SEGMENT_SECONDS = 8.0


def planning_segments(segment: dict[str, Any]) -> list[dict[str, Any]]:
    """The clip cut into windows for the director to read, not to render.

    The timeline still holds one segment; this is only what the director is
    shown, so it can say "a definition here, prose there" instead of one
    verdict for the whole clip. Windows land inside the clip, so every plan
    item still maps onto the timeline unchanged.
    """
    start = float(segment.get("source_start", 0.0))
    end = float(segment.get("source_end", 0.0))
    span = end - start
    if span <= PLANNING_SEGMENT_SECONDS * 1.5:
        return [dict(segment)]
    count = max(2, int(round(span / PLANNING_SEGMENT_SECONDS)))
    step = span / count
    return [
        dict(
            segment,
            id=f"{segment.get('id', 'segment')}-w{index}",
            source_start=round(start + step * index, 3),
            source_end=round(start + step * (index + 1), 3),
        )
        for index in range(count)
    ]


def framing_for(requested: str, manifest: dict[str, Any]) -> str:
    """contain or cover, deciding for the caller when asked to.

    Filling a vertical frame from landscape footage means throwing away more
    than half the width, and on a lesson that width is the blackboard.
    Keeping the whole picture with bars loses nothing; the bars also give
    the captions somewhere to sit that is not on top of the speaker.
    """
    if requested in {"contain", "cover"}:
        return requested
    source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    target = manifest.get("output_target") if isinstance(manifest.get("output_target"), dict) else {}
    try:
        source_ratio = float(source.get("width") or 0) / float(source.get("height") or 0)
    except (TypeError, ValueError, ZeroDivisionError):
        return "cover"
    from editor_server import PLATFORM_PRESETS  # lazy: import cycle

    preset = PLATFORM_PRESETS.get(str(target.get("platform") or "")) or {}
    try:
        target_ratio = float(preset["width"]) / float(preset["height"])
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        # Unknown platform: assume the vertical frame these are cut for.
        target_ratio = 9 / 16
    # Wider than the frame it is going into: cropping would discard content.
    return "contain" if source_ratio > target_ratio * 1.15 else "cover"


# Terms worth keeping between projects: the same brand names and the same
# mis-hearings come back every time, and retyping them on every run is the
# same as not having them.
TERMS_FILE = HOME / ".auto-edit/terms.json"


def saved_terms(path: Path = TERMS_FILE) -> tuple[list[str], list[str]]:
    """The kept glossary and corrections, as (glossary, fix) raw entries.

    A missing file means nothing was kept. A file that is there but cannot be
    read is an error, not an empty list: silently ignoring a term list the
    user wrote is exactly how a mis-heard word reaches a card anyway.
    """
    if not path.is_file():
        return [], []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path} is not readable JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must hold an object with 'glossary' and 'fix'")
    glossary = payload.get("glossary", [])
    fix = payload.get("fix", [])
    for name, value in (("glossary", glossary), ("fix", fix)):
        if not isinstance(value, list) or any(
            not isinstance(entry, str) for entry in value
        ):
            raise ValueError(f"{path}: '{name}' must be a list of strings")
    return list(glossary), list(fix)


def cut_target_seconds(requested: float, source_duration: float) -> float | None:
    """The length to aim each clip at, or None to leave length alone.

    A target is for choosing moments out of something longer than itself.
    When the source is no more than the target there is nothing to choose
    from — the whole thing is already the clip — and imposing a window only
    turns something complete into something cut short.
    """
    try:
        wanted = float(requested)
        available = float(source_duration)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(wanted) or wanted <= 0:
        return None
    if math.isfinite(available) and 0 < available <= wanted:
        return None
    return wanted


# Trimming a pause flush against the words around it clips consonant onsets
# and breath tails; this much of every pause is left in place on each side.
PAUSE_BREATH_S = 0.12
# What remains of a pause after the breathing room must still be worth a cut:
# shorter than this reads as a stutter in the picture, not as tightening.
MIN_PAUSE_CUT_S = 0.25
# A surviving scrap of timeline shorter than this is a flash frame.
MIN_SEGMENT_S = 0.2


def silence_deletions(
    candidates: list[dict[str, Any]], start: float, end: float
) -> list[tuple[float, float]]:
    """Which stretches of this window to drop, from the proposed edits.

    Only silence. Fillers and stutters carry words, and cutting a word's
    audio while its caption still shows it desynchronises the two — those
    stay proposals for a person. Silence has nothing in it to disagree with.
    """
    cuts: list[tuple[float, float]] = []
    for item in candidates:
        if item.get("type") != "silence" or item.get("risk") != "low":
            continue
        cut_start = max(float(item.get("start", 0.0)), start) + PAUSE_BREATH_S
        cut_end = min(float(item.get("end", 0.0)), end) - PAUSE_BREATH_S
        if cut_end - cut_start >= MIN_PAUSE_CUT_S:
            cuts.append((round(cut_start, 3), round(cut_end, 3)))
    cuts.sort()
    merged: list[tuple[float, float]] = []
    for cut in cuts:
        # Two cuts with less than a segment's worth of speech between them
        # would leave a flash frame; they are one cut that happened to be
        # proposed in two pieces.
        if merged and cut[0] - merged[-1][1] < MIN_SEGMENT_S:
            merged[-1] = (merged[-1][0], max(merged[-1][1], cut[1]))
        else:
            merged.append(cut)
    return merged


def window_minus_deletions(
    start: float, end: float, deletions: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    """The pieces of [start, end] that survive the cuts, in order."""
    pieces: list[tuple[float, float]] = []
    cursor = start
    for cut_start, cut_end in deletions:
        if cut_start > cursor:
            pieces.append((cursor, min(cut_start, end)))
        cursor = max(cursor, cut_end)
        if cursor >= end:
            break
    if cursor < end:
        pieces.append((cursor, end))
    return [(s, e) for s, e in pieces if e - s >= MIN_SEGMENT_S] or [(start, end)]


def clip_qa_command(
    project_dir: Path, manifest: dict[str, Any], output: Path
) -> list[str]:
    """The delivery gate call for one finished clip.

    Built here rather than inline so it can be checked without rendering a
    video first: the arguments are the part that drifts, and the render that
    surrounds them takes minutes, which is how a broken call reached a real
    run with the suite green.
    """
    import qa_video as _qa

    state_path = project_dir / "working/editor_state.json"
    clip_state = read_json(state_path) if state_path.is_file() else {}
    return [
        sys.executable,
        str(Path(__file__).with_name("qa_video.py")),
        "--video", str(output),
        "--report", str(project_dir / f"qa/{output.stem}.json"),
        # Landscape sources are delivered whole, inside letterbox bars that
        # are dark by construction. The gate is told where the picture is, or
        # it condemns any clip whose own picture is dark.
        *_qa.qa_policy_args(clip_state, manifest),
    ]


def cmd_cut(args: argparse.Namespace) -> int:
    """One command: a long video in, finished clips out."""
    import subprocess as _subprocess

    folder = Path(args.folder).expanduser().resolve() if args.folder else None
    source = Path(args.input).expanduser().resolve() if args.input else None
    if folder is None and source is None:
        return die("give it something to cut: --input a video, or --folder a folder")
    if folder is not None and not folder.is_dir():
        return die(f"no such folder: {folder}")
    if source is not None and not source.is_file():
        return die(f"no such video: {source}")
    out_dir = Path(args.out).expanduser().resolve()
    project_dir = Path(args.project_dir).expanduser().resolve() if args.project_dir \
        else out_dir / ".project"
    manifest_path = project_dir / "project.json"

    if not manifest_path.is_file() and folder is not None:
        # A folder brings its own pictures and B-roll, and says which file is
        # the talking. Handing the picked video to init instead would throw
        # the rest of the folder away.
        _step("reading the folder")
        ingest_argv = [
            "--folder", str(folder), "--project-dir", str(project_dir),
            "--source-language", args.language,
        ]
        if args.main:
            ingest_argv += ["--main", args.main]
        if cmd_ingest_folder(_args_for("ingest-folder", *ingest_argv)):
            return 2
    elif not manifest_path.is_file():
        _step("preparing the project")
        init_argv = [
            "--input", str(source), "--project-dir", str(project_dir),
            "--source-language", args.language, "--platform", args.platform,
            "--source-has-burned-in", args.burned_in,
        ]
        code = cmd_init(_args_for("init", *init_argv))
        if code:
            return code

    # Where the clip length and the platform are decided, for both routes in.
    # They were decided on one of them: a folder went to ingest-folder, which
    # takes neither, so --seconds and --platform did nothing at all there and
    # said so only in a warning buried in the output.
    target = cut_target_seconds(
        args.seconds,
        float((read_json(manifest_path).get("source") or {}).get("duration_s") or 0.0),
    )
    target_argv = ["--manifest", str(manifest_path), "--platform", args.platform]
    if target is not None:
        target_argv += ["--target-duration", str(target)]
    if cmd_set_target(_args_for("set-target", *target_argv)):
        return 2

    # What the recogniser gets wrong, in the two shapes it gets wrong.
    #
    # A glossary keeps an English term from being spelled in pieces — "c ig
    # ar" back into "cigar". It only takes terms with Latin letters in them,
    # so it cannot touch a Chinese mis-hearing at all.
    #
    # A correction is the general one: heard this, write that, with the
    # timing of what was heard. That is what fixes 句型 arriving as 巨型 or
    # 菸 as "yan".
    #
    # `init` has taken both since the beginning; `cut` offered neither, so on
    # the one command this tool is driven by there was no way to supply any —
    # and a mis-heard word now reaches a card, where it is far more visible
    # than in a caption.
    try:
        kept_glossary, kept_fix = saved_terms()
    except ValueError as exc:
        return die(str(exc))
    if kept_glossary or kept_fix or args.glossary or args.fix:
        try:
            terms = normalize_transcription_glossary(kept_glossary + args.glossary)
            fixes = normalize_transcription_calibrations(kept_fix + args.fix)
        except ValueError as exc:
            return die(str(exc))
        manifest = read_json(manifest_path)
        subtitles = manifest.get("subtitles")
        if not isinstance(subtitles, dict):
            return die("manifest subtitles must be an object")
        if terms:
            subtitles["glossary"] = terms
        if fixes:
            subtitles["calibrations"] = fixes
        manifest["updated_at"] = now_utc()
        write_json(manifest_path, manifest)
        aliases = sum(len(rule["aliases"]) for rule in fixes)
        source = (
            f" ({TERMS_FILE.name} + command line)"
            if (kept_glossary or kept_fix) and (args.glossary or args.fix)
            else f" (from {TERMS_FILE.name})" if kept_glossary or kept_fix else ""
        )
        _step(
            f"keeping the spelling of {len(terms)} term(s) and correcting "
            f"{aliases} mis-hearing(s){source}"
        )

    _step("looking at the picture and the sound")
    if cmd_analyze_video(_args_for("analyze-video", "--project-dir", str(project_dir))):
        return 2
    _step("listening to what is said")
    if cmd_transcribe_local(_args_for(
        "transcribe-local", "--manifest", str(manifest_path), "--model", args.model
    )):
        return 2
    if not args.keep_pauses:
        _step("finding the dead air")
        if cmd_analyze_edits(_args_for("analyze-edits", "--manifest", str(manifest_path))):
            return 2
    _step("indexing what can be quoted")
    if cmd_build_evidence_index(
        _args_for("build-evidence-index", "--project-dir", str(project_dir))
    ):
        return 2

    _step(f"choosing {args.clips} moment(s) worth cutting")
    highlight_argv = [
        "--manifest", str(manifest_path), "--director", args.director,
        "--count", str(args.clips), "--brief", args.brief,
        "--editorial-timeout", str(args.timeout),
    ]
    if not args.no_editorial:
        highlight_argv.append("--editorial")
    if cmd_plan_highlights(_args_for("plan-highlights", *highlight_argv)):
        return 2

    problems: list[str] = []
    if args.translate:
        from editor_server import caption_render_decision

        render_captions, caption_reason = caption_render_decision(
            project_dir, read_json(manifest_path)
        )
        if not render_captions:
            # Nothing will carry a second line, so translating would only
            # imply otherwise. Said out loud rather than delivered around.
            problems.append(
                f"--translate {args.translate} had nothing to translate: "
                f"captions are off for this source ({caption_reason})"
            )
        else:
            _step(f"translating the captions into {args.translate}")
            translated = _subprocess.run(
                [sys.executable,
                 str(Path(__file__).with_name("caption_translator.py")),
                 "--project-dir", str(project_dir),
                 "--language", args.translate],
                check=False, capture_output=True, text=True,
            )
            if translated.returncode != 0:
                # The clips are still worth delivering with one caption line,
                # but a translation that quietly did not happen looks exactly
                # like one nobody asked for — the step said "translating" and
                # the delivery must not imply it succeeded.
                problems.append(
                    "captions were not translated: "
                    + (translated.stderr or translated.stdout or "").strip()[-300:]
                )

    manifest = read_json(manifest_path)
    plan = read_json(project_dir / "working/highlight_plan.json")
    highlights = plan.get("items", [])
    if not highlights:
        return die("nothing in this video came out as a clip worth cutting")

    chosen_framing = framing_for(args.framing, manifest)
    _step(
        "keeping the whole picture with bars"
        if chosen_framing == "contain"
        else "filling the frame and following the speaker"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    renderer = str(Path(__file__).with_name("render_editor_timeline.py"))
    made: list[dict[str, Any]] = []

    for highlight in highlights:
        editorial = highlight.get("editorial") or {}
        title = str(editorial.get("title") or highlight.get("title") or "clip")
        # An editorial title is already short; a transcript extract is a
        # sentence, and a sentence makes an unusable filename.
        name = re.sub(r"[^\w一-鿿 ]+", "_", title).strip()[:24].strip() or "clip"
        output = out_dir / f"{int(highlight.get('rank', 0)):02d}_{name}.mp4"
        _step(f"cutting 「{title}」")
        try:
            materialise_clip(
                project_dir, manifest, highlight,
                fit=chosen_framing, cards=not args.no_cards,
                trim_pauses=not args.keep_pauses,
            )
        except ValueError as exc:
            problems.append(f"{title}: {exc}")
            continue
        if args.cards_from_model:
            _subprocess.run(
                [sys.executable, str(Path(__file__).with_name("card_director.py")),
                 "--project-dir", str(project_dir)],
                check=False, capture_output=True,
            )
        # A stale artifact index would reuse cards drawn for the previous clip.
        (project_dir / "working/structured_layer_artifacts.json").unlink(missing_ok=True)
        result = _subprocess.run(
            [sys.executable, renderer, "--project-dir", str(project_dir),
             "--output", str(output), "--quality", args.quality],
            check=False, capture_output=True, text=True,
        )
        if result.returncode != 0:
            problems.append(f"{title}: {(result.stderr or '').strip()[-200:]}")
            continue
        # Every clip goes through the delivery gate. Handing back a black or
        # silent file because nobody thought to check is the failure this
        # gate exists for, and until now `cut` was the one path that skipped
        # it — QA was something a person remembered to run afterwards.
        verdict = _subprocess.run(
            clip_qa_command(project_dir, manifest, output),
            check=False, capture_output=True, text=True,
        )
        status = "unknown"
        try:
            status = str(json.loads(verdict.stdout or "{}").get("status") or "unknown")
        except ValueError:
            pass
        if status != "pass":
            problems.append(f"{title}: delivery QA said {status}")
        made.append({"title": title, "file": str(output), "qa": status,
                     "seconds": round(float(highlight["end"]) - float(highlight["start"]), 2)})

    emit({
        "ok": bool(made),
        "clips": made,
        "out": str(out_dir),
        "project": str(project_dir),
        "problems": problems,
    })
    return 0 if made else 2


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
    init.add_argument("--source-language", choices=SOURCE_LANGUAGES, default="auto")
    init.add_argument(
        "--transcription-glossary",
        action="append",
        default=[],
        help="Comma/semicolon-separated terms whose spelling Whisper should preserve",
    )
    init.add_argument(
        "--transcription-calibration",
        action="append",
        default=[],
        help="Semicolon-separated exact ASR aliases using canonical=alias|alias",
    )
    init.add_argument(
        "--contextual-semantic-calibration",
        action="store_true",
        help="Run a local whole-transcript context pass after Whisper",
    )
    init.add_argument("--semantic-model", default="qwen2.5:7b")
    init.add_argument("--subtitle-mode", choices=SUBTITLE_MODES, default="source")
    init.add_argument(
        "--source-has-burned-in", choices=("auto", "yes", "no"), default="auto",
        help="does the footage already carry subtitles? auto = let analysis decide",
    )
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

    ingest = sub.add_parser(
        "ingest-folder",
        help="Folder-first import: inventory, main-video pick and owned copy",
    )
    ingest.add_argument("--folder", required=True)
    ingest.add_argument("--project-dir", required=True)
    ingest.add_argument("--main", help="Override the main-video pick (path inside the folder)")
    ingest.add_argument("--source-language", choices=SOURCE_LANGUAGES, default="auto")
    ingest.set_defaults(func=cmd_ingest_folder)

    analyze_video = sub.add_parser(
        "analyze-video",
        help="Whole-video technical analysis (probe/loudness/silence/shots/OCR) with resume cache",
    )
    analyze_video.add_argument("--project-dir", required=True)
    analyze_video.set_defaults(func=cmd_analyze_video)

    evidence = sub.add_parser(
        "build-evidence-index",
        help="Derive the citable evidence index from the word-timed transcript",
    )
    evidence.add_argument("--project-dir", required=True)
    evidence.set_defaults(func=cmd_build_evidence_index)

    freeze = sub.add_parser(
        "freeze-content-analysis",
        help="Validate an agent-authored content analysis and freeze it",
    )
    freeze.add_argument("--project-dir", required=True)
    freeze.add_argument("--input", required=True)
    freeze.add_argument("--engine-id", default="claude-agent")
    freeze.add_argument("--prompt-policy-version", default="content-analysis-guide-v1")
    freeze.set_defaults(func=cmd_freeze_content_analysis)

    plan_narrative = sub.add_parser(
        "plan-narrative",
        help="Run the deterministic formula router and emit a low-risk narrative plan",
    )
    plan_narrative.add_argument("--project-dir", required=True)
    plan_narrative.set_defaults(func=cmd_plan_narrative)

    reanchor = sub.add_parser(
        "reanchor-narrative",
        help="Re-anchor evidence literals against a rough-cut re-transcription",
    )
    reanchor.add_argument("--project-dir", required=True)
    reanchor.add_argument("--transcript", required=True,
                          help="word-timed transcript JSON of the rendered rough cut")
    reanchor.set_defaults(func=cmd_reanchor_narrative)

    narrative = sub.add_parser(
        "apply-narrative-plan",
        help="Apply a narrative_edit_plan.json to the unified segments timeline",
    )
    narrative.add_argument("--project-dir", required=True)
    narrative.add_argument("--plan", required=True)
    narrative.add_argument(
        "--draft",
        action="store_true",
        help="Fill plan_hash/reorder/risk defaults for a hand-written plan",
    )
    narrative.add_argument(
        "--confirm-high-risk",
        action="store_true",
        help="Required when the plan changes the source order",
    )
    narrative.set_defaults(func=cmd_apply_narrative_plan)

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
    # auto picks the Taiwan-tuned recogniser for zh-TW and zh-en projects and
    # Whisper otherwise. Measured on a 17s Mandarin ad: base heard the brand
    # name as an unrelated phrase, large-v3 fixed that but still missed a
    # metro station and read "book a table" as "positioning"; the Taiwan-tuned
    # model got all of them. Name a Whisper size here to force one.
    transcribe.add_argument("--model", default="auto")
    transcribe.add_argument("--timeout", type=int, default=21600)
    transcribe.set_defaults(func=cmd_transcribe_local)

    semantic = sub.add_parser(
        "semantic-calibrate",
        help="Review every caption with surrounding transcript context using local Ollama",
    )
    semantic.add_argument("--manifest", required=True)
    semantic.add_argument("--provider", choices=("ollama",), default="ollama")
    semantic.add_argument("--model", default="qwen2.5:7b")
    semantic.add_argument("--proposals-json")
    semantic.add_argument("--minimum-confidence", type=float, default=0.92)
    semantic.add_argument("--batch-size", type=int, default=10)
    semantic.add_argument("--timeout", type=int, default=300)
    semantic.set_defaults(func=cmd_semantic_calibrate)

    analyze = sub.add_parser(
        "analyze-edits",
        help="Propose low-risk silence/filler/stutter edits for human review",
    )
    analyze.add_argument("--manifest", required=True)
    analyze.set_defaults(func=cmd_analyze_edits)

    cut = sub.add_parser(
        "cut",
        help="One command: a long video in, finished clips out",
    )
    cut.add_argument("--input", default="", help="the video to cut")
    cut.add_argument("--folder", default="", help="a folder to cut from, with its assets")
    cut.add_argument("--main", default="", help="which file in the folder is the video")
    cut.add_argument("--out", required=True, help="where the clips go")
    cut.add_argument("--project-dir", default="", help="defaults to <out>/.project")
    cut.add_argument("--clips", type=int, default=3, help="how many clips to cut")
    cut.add_argument("--seconds", type=float, default=30.0, help="roughly how long each")
    cut.add_argument(
        "--keep-pauses", action="store_true",
        help="leave dead air in place instead of cutting it out",
    )
    cut.add_argument(
        "--glossary",
        action="append",
        default=[],
        help="English terms to keep spelled whole, comma separated "
             "(e.g. --glossary 'cigar,cigarette')",
    )
    cut.add_argument(
        "--fix",
        action="append",
        default=[],
        help="what the recogniser mis-hears, as 正確=誤聽|誤聽, semicolons "
             "between rules (e.g. --fix '句型=巨型;菸=yan')",
    )
    cut.add_argument("--language", default="zh-TW")
    cut.add_argument("--model", default="auto")
    cut.add_argument("--platform", choices=PLATFORMS, default="instagram-reels")
    cut.add_argument(
        "--director",
        choices=("teacher-punch", "high-energy", "documentary", "minimal", "editorial-clean"),
        default="high-energy",
    )
    cut.add_argument("--brief", default="", help="what you want out of it")
    cut.add_argument("--translate", default="", help="add a second caption line, e.g. en")
    cut.add_argument(
        "--framing", choices=("auto", "contain", "cover"), default="auto",
        help="auto keeps the whole picture when the source is wider than the target",
    )
    cut.add_argument("--quality", choices=("preview", "final"), default="preview")
    cut.add_argument("--timeout", type=int, default=900)
    cut.add_argument(
        "--cards-from-model", action="store_true",
        help="also let a model propose cards through the clip",
    )
    cut.add_argument("--no-cards", action="store_true", help="no cards at all")
    cut.add_argument(
        "--no-editorial", action="store_true",
        help="pick moments by score instead of asking a model",
    )
    cut.add_argument(
        "--burned-in", choices=("auto", "yes", "no"), default="auto",
        help="does the footage already carry subtitles?",
    )
    cut.set_defaults(func=cmd_cut)

    cards = sub.add_parser(
        "add-card",
        help="Place a card at a moment by hand; it outranks proposed cards",
    )
    cards.add_argument("--manifest", required=True)
    cards.add_argument("--at", type=float, required=True, help="source seconds")
    cards.add_argument("--seconds", type=float, default=3.0, help="time on screen")
    cards.add_argument(
        "--kind",
        choices=("title", "stat", "note", "chip", "statement"),
        default="title",
    )
    cards.add_argument("--title", default="")
    cards.add_argument("--subtitle", default="")
    cards.add_argument("--kicker", default="")
    cards.add_argument("--value", default="", help="the figure, for a stat card")
    cards.add_argument("--icon", default="", help="emoji shown before a note's title")
    cards.add_argument("--meta", default="", help="the small right-hand label on a note")
    cards.add_argument("--body", default="", help="a second line under a note's title")
    cards.add_argument(
        "--waveform", action="store_true", help="draw a recording strip on a note"
    )
    cards.add_argument("--text", default="", help="the line on a chip or statement")
    cards.add_argument("--lead", default="", help="the figure a statement counts off")
    cards.add_argument("--note", default="", help="why this card is here")
    cards.set_defaults(func=cmd_add_card)

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
    highlights.add_argument(
        "--editorial", action="store_true",
        help="let a model choose and name the cuts; falls back to the scored plan",
    )
    highlights.add_argument(
        "--editorial-provider", default="",
        help="command that answers the selection prompt (default: openclaw agent-7)",
    )
    highlights.add_argument("--editorial-timeout", type=int, default=600)
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
