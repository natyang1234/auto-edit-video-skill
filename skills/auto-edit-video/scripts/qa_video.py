#!/usr/bin/env python3
"""Run dependency-light delivery QA and create a contact sheet."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def run(command: list[str], timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, timeout=timeout)


def probe(video: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise ValueError("ffprobe is required")
    result = run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration,size:stream=index,codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels",
            "-of",
            "json",
            str(video),
        ]
    )
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or "ffprobe failed")
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    visual = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    duration = float(data.get("format", {}).get("duration") or 0.0)
    return {
        "duration_s": round(duration, 3),
        "size_bytes": int(data.get("format", {}).get("size") or video.stat().st_size),
        "video": visual,
        "audio": audio,
    }


def black_segments(video: Path) -> list[dict[str, float]]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("ffmpeg is required")
    result = run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostats",
            "-i",
            str(video),
            "-vf",
            "blackdetect=d=0.5:pix_th=0.10",
            "-an",
            "-f",
            "null",
            "-",
        ]
    )
    text = result.stderr or ""
    segments: list[dict[str, float]] = []
    pattern = re.compile(
        r"black_start:(?P<start>[0-9.]+)\s+black_end:(?P<end>[0-9.]+)\s+black_duration:(?P<duration>[0-9.]+)"
    )
    for match in pattern.finditer(text):
        segments.append({key: round(float(value), 3) for key, value in match.groupdict().items()})
    return segments


def loudness(video: Path) -> dict[str, float] | None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("ffmpeg is required")
    result = run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostats",
            "-i",
            str(video),
            "-vn",
            "-af",
            "ebur128=peak=true",
            "-f",
            "null",
            "-",
        ]
    )
    text = result.stderr or ""
    integrated = re.findall(r"I:\s*(-?(?:inf|[0-9.]+))\s+LUFS", text)
    true_peak = re.findall(r"Peak:\s*(-?(?:inf|[0-9.]+))\s+dBFS", text)
    if not integrated:
        return None
    payload: dict[str, float] = {}
    if integrated[-1] != "-inf":
        payload["integrated_lufs"] = float(integrated[-1])
    if true_peak and true_peak[-1] != "-inf":
        payload["true_peak_dbfs"] = float(true_peak[-1])
    return payload or None


def contact_sheet(video: Path, output: Path, duration: float) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("ffmpeg is required")
    output.parent.mkdir(parents=True, exist_ok=True)
    interval = max(duration / 9.0, 0.05)
    result = run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video),
            "-vf",
            f"fps=1/{interval:.6f},scale=360:-2,tile=3x3:padding=4:margin=4",
            "-frames:v",
            "1",
            str(output),
        ]
    )
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or "contact-sheet render failed")


def inspect(video: Path, report_path: Path, contact_path: Path) -> tuple[dict[str, Any], bool]:
    media = probe(video)
    failures: list[str] = []
    warnings: list[str] = []
    visual = media["video"] or {}
    audio = media["audio"]
    if not visual:
        failures.append("video stream is missing")
    if media["duration_s"] <= 0:
        failures.append("duration must be positive")
    width = int(visual.get("width") or 0)
    height = int(visual.get("height") or 0)
    if width <= 0 or height <= 0:
        failures.append("invalid video dimensions")
    elif width % 2 or height % 2:
        warnings.append("video dimensions are not even")
    if not audio:
        warnings.append("audio stream is missing")

    blacks = black_segments(video)
    if any(item["duration"] >= 1.0 for item in blacks):
        warnings.append("black segment of at least one second detected")
    levels = loudness(video) if audio else None
    contact_sheet(video, contact_path, media["duration_s"])
    report = {
        "schema_version": 1,
        "generated_at": now_utc(),
        "video": str(video),
        "status": "pass" if not failures else "fail",
        "media": media,
        "black_segments": blacks,
        "loudness": levels,
        "contact_sheet": str(contact_path),
        "failures": failures,
        "warnings": warnings,
        "human_review_required": True,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report, not failures


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", required=True)
    parser.add_argument("--report")
    parser.add_argument("--contact")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    video = Path(args.video).expanduser().resolve()
    if not video.is_file():
        print(f"video not found: {video}", file=sys.stderr)
        return 2
    report = Path(args.report).expanduser().resolve() if args.report else video.parent.parent / "qa/qa-report.json"
    contact = Path(args.contact).expanduser().resolve() if args.contact else video.parent.parent / "qa/final-contact.png"
    try:
        payload, ok = inspect(video, report, contact)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
