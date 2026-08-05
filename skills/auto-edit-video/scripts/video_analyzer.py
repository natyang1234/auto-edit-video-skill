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
# Burned-in subtitle detection, in Vision's normalised bottom-left space.
BURNED_IN_BAND_TOP = 0.30       # caption rows live in the bottom 30%
BURNED_IN_MAX_OFF_CENTER = 0.22  # and stay near the horizontal centre
BURNED_IN_MIN_FRAME_SHARE = 0.5  # present in at least half the samples
BURNED_IN_MIN_FRAMES = 3         # never conclude from one or two frames
BURNED_IN_MAX_BAND_SPREAD = 0.05  # anchored height, unlike scenery text
BURNED_IN_MAX_EVIDENCE = 12
BURNED_IN_SAMPLE_COUNT = 12    # detection samples on its own schedule


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


@functools.lru_cache(maxsize=4)
def _tool_identity(executable: str) -> str:
    """version + build-config digest — cache keys must see build changes."""
    try:
        output = subprocess.run(
            [executable, "-version"], capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    head = output.splitlines()[0] if output else "unknown"
    match = re.search(r"version (\S+)", head)
    version = match.group(1) if match else "unknown"
    import hashlib

    return f"{version}+build-{hashlib.sha256(output.encode('utf-8')).hexdigest()[:12]}"


def ffmpeg_version() -> str:
    return _tool_identity(ffmpeg_path())


def ffprobe_version() -> str:
    return _tool_identity(ffprobe_path())


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
        capture_output=True, text=True, timeout=OCR_FRAME_TIMEOUT_S,
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


def burned_in_verdict(frames: dict[float, list[dict[str, Any]]]) -> dict[str, Any]:
    """Pure verdict over {timestamp: OCR lines with boxes}.

    A subtitle is text that sits low, sits centred, and sits at the SAME
    height every time; shop signs and menu boards drift as the camera moves,
    so the tight vertical clustering is what separates them.
    """
    if not frames:
        return {"status": "not_configured", "frames_sampled": 0, "frames_with_band_text": 0}

    evidence: list[dict[str, Any]] = []
    for timestamp in sorted(frames):
        best: dict[str, Any] | None = None
        for line in frames[timestamp]:
            box = line.get("box")
            if not isinstance(box, dict):
                continue
            center_y = float(box["y"]) + float(box["height"]) / 2.0
            center_x = float(box["x"]) + float(box["width"]) / 2.0
            if center_y > BURNED_IN_BAND_TOP:
                continue
            if abs(center_x - 0.5) > BURNED_IN_MAX_OFF_CENTER:
                continue
            # Lowest qualifying line is the caption row.
            if best is None or center_y < best["center_y"]:
                best = {
                    "start": round(float(timestamp), 3),
                    "text": str(line["text"]),
                    "center_y": round(center_y, 5),
                }
        if best is not None:
            evidence.append(best)

    sampled = len(frames)
    hits = len(evidence)
    result: dict[str, Any] = {
        "status": "absent",
        "frames_sampled": sampled,
        "frames_with_band_text": hits,
        "band_center_y": None,
        "band_spread": None,
        "evidence": evidence[:BURNED_IN_MAX_EVIDENCE],
    }
    if hits < BURNED_IN_MIN_FRAMES or hits < sampled * BURNED_IN_MIN_FRAME_SHARE:
        return result

    centers = [item["center_y"] for item in evidence]
    mean = sum(centers) / len(centers)
    spread = (sum((value - mean) ** 2 for value in centers) / len(centers)) ** 0.5
    result["band_center_y"] = round(mean, 5)
    result["band_spread"] = round(spread, 5)
    if spread <= BURNED_IN_MAX_BAND_SPREAD:
        result["status"] = "detected"
    return result


def detect_burned_in_captions(source: Path, duration_s: float) -> dict[str, Any]:
    """Sample the clip on its own schedule and rule on burned-in subtitles.

    Deliberately does NOT reuse the OCR stage's frames: that stage samples on
    shot boundaries, which on a short single-shot clip yields two frames —
    too few to tell an anchored caption row from a coincidence.
    """
    if duration_s <= 0:
        return {"status": "not_configured", "frames_sampled": 0, "frames_with_band_text": 0}
    count = BURNED_IN_SAMPLE_COUNT
    # Evenly spaced, inset from both ends so titles/end cards do not dominate.
    timestamps = [
        round(duration_s * (index + 0.5) / count, 3) for index in range(count)
    ]
    frames: dict[float, list[dict[str, Any]]] = {}
    with tempfile.TemporaryDirectory(prefix="auto-edit-burnin-") as scratch:
        for timestamp in timestamps:
            frame = Path(scratch) / f"burnin-{timestamp:.3f}.png"
            try:
                extract_frame(source, timestamp, frame)
                lines = vision_ocr.recognize_text(frame, timeout_s=OCR_FRAME_TIMEOUT_S)
            except Exception:
                continue
            frames[timestamp] = lines
    return burned_in_verdict(frames)


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

    def run_stage(
        stage: str,
        params: dict[str, Any],
        compute: Callable[[], Any],
        upstream: str = "",
        engine: dict[str, str] | None = None,
    ) -> Any:
        payload, hit = stage_cached(
            project_dir,
            stage,
            {
                "source_sha256": source_sha,
                "stage": stage,
                "params": params,
                "engine": engine if engine is not None else {"ffmpeg": ff_version},
                "upstream": upstream,
            },
            compute,
        )
        stats[stage] = "hit" if hit else "computed"
        return payload

    probe = run_stage(
        "probe",
        {"tool": "ffprobe"},
        lambda: probe_source(source),
        engine={"ffprobe": ffprobe_version()},
    )
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
    asr_engine = transcript_engine(project_dir)
    transcript_path = project_dir / "working/transcript_words.json"
    transcript_state = {
        "status": asr_engine["status"],
        "transcript_sha256": (
            contract_registry.canonical_hash(
                json.loads(transcript_path.read_text(encoding="utf-8"))
            )
            if asr_engine["status"] == "present"
            else None
        ),
    }
    run_stage(
        "asr",
        {"artifact": "working/transcript_words.json"},
        lambda: transcript_state,
        engine=asr_engine,
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
        "recognition_languages": ["zh-Hant", "en-US"],
        "recognition_level": "accurate",
        "language_correction": False,
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
            "asr": asr_engine,
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
        "burned_in_captions": run_stage(
            "burned_in_captions",
            {"samples": BURNED_IN_SAMPLE_COUNT, "band_top": BURNED_IN_BAND_TOP},
            lambda: detect_burned_in_captions(source, duration_s),
            engine=vision_ocr.vision_engine(),
        ),
    }
    analysis["revision"] = contract_registry.canonical_hash(analysis)
    errors = contract_registry.validate_artifact("video_analysis", analysis)
    if errors:
        raise ValueError("video_analysis failed contract validation: " + "; ".join(errors))
    atomic_write_json(project_dir / "working/video_analysis.json", analysis)
    return analysis, stats
