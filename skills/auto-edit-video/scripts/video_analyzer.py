#!/usr/bin/env python3
"""Whole-video technical analysis with stage-level checkpoint cache.

Produces ``working/video_analysis.json`` per the Phase 0 contract. Every
stage caches independently under ``working/analysis_cache/`` keyed by
``sha256(source_sha + stage + canonical params + engine versions + upstream
artifact hash)`` so an interrupted analysis resumes instead of restarting
(ANALYSIS_ENGINE.md). Visual-semantic classification is intentionally
``unknown`` here — semantic judgement belongs to the frozen content analysis
artifact, not to deterministic heuristics.
"""
from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

import contract_registry
import vision_ocr

CACHE_REL = Path("working/analysis_cache")
SCENE_FILTER = "select='gt(scene,0.4)',showinfo"
OCR_SAMPLE_INTERVAL_S = 10.0
OCR_DEDUPE_S = 0.5
OCR_MAX_FRAMES = 300
OCR_MAX_PER_MINUTE = 30
OCR_SCALE_LONG_SIDE = 960
OCR_FRAME_TIMEOUT_S = 10.0
OCR_SPAN_LENGTH_S = 2.0


def ffmpeg_path() -> str:
    override = os.environ.get("AUTO_EDIT_FFMPEG")
    if override:
        return override
    fallback = "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"
    if Path(fallback).is_file():
        return fallback
    return "ffmpeg"


def ffprobe_path() -> str:
    ffmpeg = ffmpeg_path()
    candidate = Path(ffmpeg).with_name("ffprobe")
    return str(candidate) if candidate.is_file() else "ffprobe"


@functools.lru_cache(maxsize=1)
def ffmpeg_version() -> str:
    try:
        head = subprocess.run(
            [ffmpeg_path(), "-version"], capture_output=True, text=True, check=True
        ).stdout.splitlines()[0]
    except (OSError, subprocess.CalledProcessError, IndexError):
        return "unknown"
    match = re.search(r"ffmpeg version (\S+)", head)
    return match.group(1) if match else "unknown"


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(path.name + ".tmp")
    scratch.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    scratch.replace(path)


def stage_cached(
    project_dir: Path,
    stage: str,
    key_payload: dict[str, Any],
    compute: Callable[[], Any],
) -> tuple[Any, bool]:
    """Return (payload, cache_hit). Torn/partial cache entries never hit."""
    key = contract_registry.canonical_hash(key_payload)
    cache_path = project_dir / CACHE_REL / f"{stage}.json"
    if cache_path.is_file():
        try:
            entry = json.loads(cache_path.read_text(encoding="utf-8"))
            if (
                entry.get("key") == key
                and contract_registry.canonical_hash(entry.get("payload"))
                == entry.get("output_hash")
            ):
                return entry["payload"], True
        except (ValueError, OSError):
            pass
    payload = compute()
    atomic_write_json(
        cache_path,
        {
            "key": key,
            "output_hash": contract_registry.canonical_hash(payload),
            "payload": payload,
        },
    )
    return payload, False


def probe_source(source: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            ffprobe_path(),
            "-v", "error",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(source),
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise ValueError(f"ffprobe failed: {result.stderr[-500:]}")
    data = json.loads(result.stdout)
    video = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"), None
    )
    if video is None:
        raise ValueError("no video stream in source")
    fps_text = video.get("avg_frame_rate") or "30/1"
    try:
        numerator, _, denominator = fps_text.partition("/")
        fps = float(numerator) / float(denominator or 1)
    except (ValueError, ZeroDivisionError):
        fps = 30.0
    return {
        "duration_s": float(data.get("format", {}).get("duration") or 0.0),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": round(fps, 3),
    }


def measure_loudness(source: Path) -> float:
    result = subprocess.run(
        [ffmpeg_path(), "-i", str(source), "-af", "ebur128", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    matches = re.findall(r"I:\s*(-?[\d.]+)\s*LUFS", result.stderr)
    return float(matches[-1]) if matches else 0.0


def detect_silences(source: Path) -> list[dict[str, float]]:
    result = subprocess.run(
        [
            ffmpeg_path(), "-i", str(source),
            "-af", "silencedetect=noise=-35dB:d=0.35",
            "-f", "null", "-",
        ],
        capture_output=True, text=True,
    )
    silences: list[dict[str, float]] = []
    start: float | None = None
    for line in result.stderr.splitlines():
        begin = re.search(r"silence_start:\s*(-?[\d.]+)", line)
        if begin:
            start = max(float(begin.group(1)), 0.0)
            continue
        finish = re.search(r"silence_end:\s*(-?[\d.]+)", line)
        if finish and start is not None:
            end = float(finish.group(1))
            if end > start:
                silences.append({"start": round(start, 3), "end": round(end, 3)})
            start = None
    return silences


def detect_shots(source: Path, duration_s: float) -> list[dict[str, Any]]:
    result = subprocess.run(
        [
            ffmpeg_path(), "-i", str(source),
            "-map", "0:v:0", "-vf", SCENE_FILTER, "-f", "null", "-",
        ],
        capture_output=True, text=True,
    )
    boundaries = sorted(
        {
            round(float(match), 3)
            for match in re.findall(r"pts_time:([\d.]+)", result.stderr)
            if 0.0 < float(match) < duration_s
        }
    )
    edges = [0.0, *boundaries, max(duration_s, 0.001)]
    return [
        {"start": edges[i], "end": edges[i + 1], "kind": "unknown"}
        for i in range(len(edges) - 1)
        if edges[i + 1] > edges[i]
    ]


def sample_timestamps(
    duration_s: float,
    shot_starts: list[float],
    interval_s: float = OCR_SAMPLE_INTERVAL_S,
    dedupe_s: float = OCR_DEDUPE_S,
    max_frames: int = OCR_MAX_FRAMES,
    max_per_minute: int = OCR_MAX_PER_MINUTE,
) -> list[float]:
    """OCR frame sampling: shot starts + interval grid, deduped and capped."""
    grid = [float(t) for t in shot_starts]
    tick = 0.0
    while tick < duration_s:
        grid.append(tick)
        tick += interval_s
    chosen: list[float] = []
    for stamp in sorted(t for t in grid if 0.0 <= t < duration_s):
        if chosen and stamp - chosen[-1] < dedupe_s:
            continue
        minute_bucket = int(stamp // 60.0)
        in_minute = sum(1 for t in chosen if int(t // 60.0) == minute_bucket)
        if in_minute >= max_per_minute:
            continue
        chosen.append(round(stamp, 3))
        if len(chosen) >= max_frames:
            break
    return chosen


def extract_frame(source: Path, timestamp: float, destination: Path) -> None:
    result = subprocess.run(
        [
            ffmpeg_path(), "-y",
            "-ss", f"{timestamp:.3f}", "-i", str(source),
            "-frames:v", "1",
            "-vf",
            f"scale='if(gt(iw,ih),{OCR_SCALE_LONG_SIDE},-2)':'if(gt(iw,ih),-2,{OCR_SCALE_LONG_SIDE})'",
            str(destination),
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not destination.is_file():
        raise ValueError(f"frame extraction failed at {timestamp:.3f}s")


def run_ocr(
    source: Path,
    duration_s: float,
    timestamps: list[float],
) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="auto-edit-ocr-") as scratch:
        for timestamp in timestamps:
            frame = Path(scratch) / f"frame-{timestamp:.3f}.png"
            try:
                extract_frame(source, timestamp, frame)
                lines = vision_ocr.recognize_text(frame, timeout_s=OCR_FRAME_TIMEOUT_S)
            except Exception:
                continue
            for line in lines:
                spans.append(
                    {
                        "start": timestamp,
                        "end": round(min(timestamp + OCR_SPAN_LENGTH_S, duration_s), 3),
                        "text": line["text"],
                        "confidence": round(min(max(line["confidence"], 0.0), 1.0), 4),
                    }
                )
    return spans


def transcript_engine(project_dir: Path) -> dict[str, str]:
    words_path = project_dir / "working/transcript_words.json"
    if not words_path.is_file():
        return {"name": "openai-whisper", "version": "", "status": "not_configured"}
    try:
        payload = json.loads(words_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {"name": "openai-whisper", "version": "", "status": "not_configured"}
    return {
        "name": str(payload.get("engine") or "openai-whisper"),
        "version": str(payload.get("model") or payload.get("engine_version") or "unknown"),
        "status": "present",
    }


def analyze(project_dir: Path) -> tuple[dict[str, Any], dict[str, str]]:
    """Run all stages (cache-aware); returns (video_analysis, stage_stats)."""
    project_dir = project_dir.resolve()
    manifest = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    source_rel = str(manifest.get("source", {}).get("staged_path") or "")
    source = project_dir / source_rel
    if not source.is_file():
        raise ValueError(f"staged source missing: {source}")
    source_sha = str(manifest.get("source", {}).get("sha256") or "")
    ff_version = ffmpeg_version()
    stats: dict[str, str] = {}

    def run_stage(stage: str, params: dict[str, Any], compute: Callable[[], Any], upstream: str = "") -> Any:
        payload, hit = stage_cached(
            project_dir,
            stage,
            {
                "source_sha256": source_sha,
                "stage": stage,
                "params": params,
                "engine": {"ffmpeg": ff_version},
                "upstream": upstream,
            },
            compute,
        )
        stats[stage] = "hit" if hit else "computed"
        return payload

    probe = run_stage("probe", {"tool": "ffprobe"}, lambda: probe_source(source))
    duration_s = float(probe["duration_s"])
    loudness = run_stage("loudness", {"filter": "ebur128"}, lambda: measure_loudness(source))
    silences = run_stage(
        "silence",
        {"filter": "silencedetect=noise=-35dB:d=0.35"},
        lambda: detect_silences(source),
    )
    shots = run_stage(
        "shots",
        {"filter": SCENE_FILTER, "stream": "0:v:0"},
        lambda: detect_shots(source, duration_s),
        upstream=contract_registry.canonical_hash(probe),
    )
    ocr_engine = vision_ocr.vision_engine()
    sampling_params = {
        "interval_s": OCR_SAMPLE_INTERVAL_S,
        "dedupe_s": OCR_DEDUPE_S,
        "max_frames": OCR_MAX_FRAMES,
        "max_per_minute": OCR_MAX_PER_MINUTE,
        "scale_long_side": OCR_SCALE_LONG_SIDE,
        "timeout_s": OCR_FRAME_TIMEOUT_S,
        "engine": ocr_engine,
    }
    if ocr_engine["status"] == "present":
        shot_starts = [shot["start"] for shot in shots]
        ocr_spans = run_stage(
            "ocr",
            sampling_params,
            lambda: run_ocr(
                source, duration_s, sample_timestamps(duration_s, shot_starts)
            ),
            upstream=contract_registry.canonical_hash(shots),
        )
    else:
        # Contract: not_configured forbids ocr fields entirely.
        ocr_spans = []
        stats["ocr"] = "not_configured"

    analysis = {
        "schema_version": 1,
        "source_sha256": source_sha,
        "engines": {
            "ffprobe": {"name": "ffprobe", "version": ff_version, "status": "present"},
            "asr": transcript_engine(project_dir),
            "ocr": ocr_engine,
            "shot_detector": {
                "name": "ffmpeg-scene",
                "version": f"{ff_version}|{SCENE_FILTER}|0:v:0",
                "status": "present",
            },
        },
        "duration_s": duration_s,
        "width": int(probe["width"]),
        "height": int(probe["height"]),
        "fps": float(probe["fps"]),
        "loudness_lufs": float(loudness),
        "silences": silences,
        "shots": shots,
        "ocr_spans": ocr_spans,
    }
    analysis["revision"] = contract_registry.canonical_hash(analysis)
    errors = contract_registry.validate_artifact("video_analysis", analysis)
    if errors:
        raise ValueError("video_analysis failed contract validation: " + "; ".join(errors))
    atomic_write_json(project_dir / "working/video_analysis.json", analysis)
    return analysis, stats
