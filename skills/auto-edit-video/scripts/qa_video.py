#!/usr/bin/env python3
"""Run dependency-light delivery QA and create a contact sheet."""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class QaPolicy:
    """Configurable fail thresholds for delivery QA (contracts M7).

    Defaults are fail-closed: a fully black or silent final must never pass.
    Callers may relax individual thresholds explicitly per project.
    """

    max_black_segment_seconds: float = 2.0
    max_black_ratio: float = 0.35
    allow_missing_audio: bool = False
    min_integrated_lufs: float = -45.0
    max_true_peak_dbfs: float = 0.0
    # Integrated loudness is gated: it ignores silent passages, so a final
    # whose narration was truncated still measures fine. Silent coverage
    # catches that; normal pacing leaves well under this share silent.
    max_silent_ratio: float = 0.8

    def __post_init__(self) -> None:
        if not isinstance(self.allow_missing_audio, bool):
            raise ValueError(
                f"QA policy allow_missing_audio must be a bool, got {self.allow_missing_audio!r}"
            )
        # NaN compares false against everything, which would silently disable
        # a threshold; reject non-finite values outright.
        for name in (
            "max_black_segment_seconds",
            "max_black_ratio",
            "min_integrated_lufs",
            "max_true_peak_dbfs",
            "max_silent_ratio",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError(f"QA policy {name} must be a finite number, got {value!r}")
        if (
            self.max_black_segment_seconds < 0
            or self.max_black_ratio < 0
            or self.max_silent_ratio < 0
        ):
            raise ValueError("QA policy coverage thresholds must be non-negative")


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


# Must stay below one frame at the delivery frame rates this tool produces
# (up to 200fps -> 5ms); a floor above the frame duration makes single-frame
# black flicker invisible to blackdetect and lets fragmented black evade the
# coverage gate. Beyond 200fps detection degrades and the gate is unreliable.
BLACK_DETECT_MIN_SECONDS = 0.005
BLACK_DETECT_PIXEL_THRESHOLD = 0.10
# A detected crop smaller than this share of the frame is treated as content
# on a black frame rather than as letterbox padding.
MIN_CONTENT_AREA_RATIO = 0.25
# Luma at or below which cropdetect treats a pixel as padding. This is the
# limited-range equivalent of the black pixel threshold below (16 + 0.10*219),
# so padding drawn in the renderer's near-black tone is recognised.
CROP_DETECT_LIMIT = 38
# Share of a frame that must be dark before the frame counts as black.
# ffmpeg defaults to 0.98, which misses a failed background render that still
# carries a caption box or logo; 0.85 catches those while leaving room for
# legitimate letterboxing (a pillarboxed portrait delivery is ~68% black).
BLACK_DETECT_PICTURE_RATIO = 0.85


def content_crop(video: Path, width: int, height: int) -> dict[str, int] | None:
    """Locate the picture area inside letterbox or pillarbox padding.

    Padding is rendered in a near-black tone (contain fit), so the bars
    themselves read as black pixels and would dominate the coverage
    measurement — a tall canvas can be three-quarters bars before any
    content is considered. Measuring the content area instead keeps the
    gate about the picture. A crop that only covers a small part of the
    frame is NOT padding (it is more likely a caption box on an otherwise
    black frame), so it is ignored and the whole frame is measured.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or width <= 0 or height <= 0:
        return None
    result = run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostats",
            "-i",
            str(video),
            "-vf",
            f"cropdetect=limit={CROP_DETECT_LIMIT}:round=2:reset=0",
            "-an",
            "-f",
            "null",
            "-",
        ]
    )
    matches = re.findall(
        r"crop=(?P<w>\d+):(?P<h>\d+):(?P<x>-?\d+):(?P<y>-?\d+)", result.stderr or ""
    )
    if not matches:
        return None
    crop_w, crop_h, crop_x, crop_y = (int(value) for value in matches[-1])
    if crop_w <= 0 or crop_h <= 0 or crop_x < 0 or crop_y < 0:
        return None
    if crop_w > width or crop_h > height:
        return None
    if crop_w == width and crop_h == height:
        return None
    if (crop_w * crop_h) < (width * height * MIN_CONTENT_AREA_RATIO):
        return None
    return {"width": crop_w, "height": crop_h, "x": crop_x, "y": crop_y}


def black_segments(
    video: Path,
    min_duration: float = BLACK_DETECT_MIN_SECONDS,
    crop: dict[str, int] | None = None,
) -> list[dict[str, float]]:
    """Detect black segments down to roughly two frames.

    The detection floor must stay well below the policy thresholds: coverage
    is summed from detected segments, so segments shorter than the floor are
    invisible and fragmented black frames would otherwise evade the gate.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("ffmpeg is required")
    prefix = (
        f"crop={crop['width']}:{crop['height']}:{crop['x']}:{crop['y']}," if crop else ""
    )
    result = run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostats",
            "-i",
            str(video),
            "-vf",
            f"{prefix}blackdetect=d={min_duration}"
            f":pic_th={BLACK_DETECT_PICTURE_RATIO}"
            f":pix_th={BLACK_DETECT_PIXEL_THRESHOLD}",
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
        segments.append({key: float(value) for key, value in match.groupdict().items()})
    return segments


SILENCE_NOISE_FLOOR_DB = -50
SILENCE_MIN_SECONDS = 0.3
# EBU R128 integrates over 400ms, so shorter clips always measure -70 LUFS;
# they are judged on peak level instead.
LOUDNESS_MIN_MEASURABLE_SECONDS = 0.4


def silent_coverage(video: Path, duration: float) -> dict[str, float] | None:
    """Share of the timeline that is effectively silent.

    Integrated loudness is gated and ignores silence, so a final whose audio
    stops after the first moments still measures a healthy level.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or duration <= 0:
        return None
    result = run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostats",
            "-i",
            str(video),
            "-af",
            f"silencedetect=noise={SILENCE_NOISE_FLOOR_DB}dB:d={SILENCE_MIN_SECONDS}",
            "-vn",
            "-f",
            "null",
            "-",
        ]
    )
    text = result.stderr or ""
    total = sum(
        float(value) for value in re.findall(r"silence_duration:\s*([0-9.]+)", text)
    )
    # An unterminated final silence is reported as a start with no duration.
    starts = [float(value) for value in re.findall(r"silence_start:\s*(-?[0-9.]+)", text)]
    ends = re.findall(r"silence_end:\s*[0-9.]+", text)
    if len(starts) > len(ends):
        total += max(0.0, duration - starts[-1])
    return {"silent_seconds": total, "silent_ratio": min(total / duration, 1.0)}


def peak_level_dbfs(video: Path) -> float | None:
    """Peak sample level, used where integrated loudness cannot be measured."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    result = run(
        [ffmpeg, "-hide_banner", "-nostats", "-i", str(video), "-af", "volumedetect", "-vn", "-f", "null", "-"]
    )
    match = re.search(r"max_volume:\s*(-?[0-9.]+) dB", result.stderr or "")
    return float(match.group(1)) if match else None


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


def inspect(
    video: Path,
    report_path: Path,
    contact_path: Path,
    policy: QaPolicy | None = None,
) -> tuple[dict[str, Any], bool]:
    policy = policy or QaPolicy()
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
        if policy.allow_missing_audio:
            warnings.append("audio stream is missing")
        else:
            failures.append("audio stream is missing")

    crop = content_crop(video, width, height)
    blacks = black_segments(video, crop=crop)
    longest_black = max((item["duration"] for item in blacks), default=0.0)
    if longest_black >= policy.max_black_segment_seconds:
        failures.append(
            f"black segment of {longest_black:.3f}s reaches the "
            f"{policy.max_black_segment_seconds:.3f}s fail threshold"
        )
    elif longest_black >= 1.0:
        warnings.append("black segment of at least one second detected")
    if media["duration_s"] > 0:
        black_ratio = sum(item["duration"] for item in blacks) / media["duration_s"]
        if black_ratio >= policy.max_black_ratio:
            failures.append(
                f"black frames cover {black_ratio:.1%} of the video, at or above the "
                f"{policy.max_black_ratio:.1%} fail threshold"
            )
    levels = loudness(video) if audio else None
    silence = silent_coverage(video, media["duration_s"]) if audio else None
    short_clip = 0 < media["duration_s"] < LOUDNESS_MIN_MEASURABLE_SECONDS
    if audio and silence and silence["silent_ratio"] >= policy.max_silent_ratio:
        failures.append(
            f"audio is silent for {silence['silent_ratio']:.1%} of the video, at or above "
            f"the {policy.max_silent_ratio:.1%} fail threshold (truncated or missing audio)"
        )
    if audio and short_clip:
        # Too short for R128; judge on peak level so brief clips are neither
        # falsely failed nor waved through.
        peak = peak_level_dbfs(video)
        if peak is None or peak < policy.min_integrated_lufs:
            failures.append(
                "clip is too short to measure loudness and its peak level is silent"
            )
    elif audio:
        integrated = (levels or {}).get("integrated_lufs")
        if integrated is None:
            failures.append(
                "integrated loudness could not be measured (silent or unreadable audio)"
            )
        elif integrated < policy.min_integrated_lufs:
            failures.append(
                f"integrated loudness {integrated:.1f} LUFS is below the "
                f"{policy.min_integrated_lufs:.1f} LUFS fail threshold (near-silent audio)"
            )
        true_peak = (levels or {}).get("true_peak_dbfs")
        if true_peak is None:
            if integrated is not None:
                failures.append("true peak could not be measured (silent or unreadable audio)")
        elif true_peak > policy.max_true_peak_dbfs:
            failures.append(
                f"true peak {true_peak:.1f} dBFS is above the "
                f"{policy.max_true_peak_dbfs:.1f} dBFS fail threshold (clipping)"
            )
    contact_sheet(video, contact_path, media["duration_s"])
    report = {
        "schema_version": 1,
        "generated_at": now_utc(),
        "video": str(video),
        "status": "pass" if not failures else "fail",
        "media": media,
        "black_segments": blacks,
        "black_detection": {
            "min_segment_seconds": BLACK_DETECT_MIN_SECONDS,
            "pixel_threshold": BLACK_DETECT_PIXEL_THRESHOLD,
            "picture_ratio_threshold": BLACK_DETECT_PICTURE_RATIO,
            "measured_crop": crop,
        },
        "loudness": levels,
        "silence": silence,
        "policy": asdict(policy),
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
    defaults = QaPolicy()
    parser.add_argument("--video", required=True)
    parser.add_argument("--report")
    parser.add_argument("--contact")
    parser.add_argument(
        "--max-black-segment-seconds",
        type=float,
        default=defaults.max_black_segment_seconds,
        help="fail when any black segment lasts at least this many seconds",
    )
    parser.add_argument(
        "--max-black-ratio",
        type=float,
        default=defaults.max_black_ratio,
        help="fail when black frames cover at least this fraction of the duration",
    )
    parser.add_argument(
        "--allow-missing-audio",
        action="store_true",
        help="downgrade a missing audio stream from failure to warning",
    )
    parser.add_argument(
        "--max-silent-ratio",
        type=float,
        default=defaults.max_silent_ratio,
        help="fail when audio is silent for at least this fraction of the duration",
    )
    parser.add_argument(
        "--min-integrated-lufs",
        type=float,
        default=defaults.min_integrated_lufs,
        help="fail when integrated loudness is below this LUFS value",
    )
    parser.add_argument(
        "--max-true-peak-dbfs",
        type=float,
        default=defaults.max_true_peak_dbfs,
        help="fail when true peak is above this dBFS value",
    )
    return parser


def policy_from_args(args: argparse.Namespace) -> QaPolicy:
    return QaPolicy(
        max_black_segment_seconds=args.max_black_segment_seconds,
        max_black_ratio=args.max_black_ratio,
        allow_missing_audio=args.allow_missing_audio,
        max_silent_ratio=args.max_silent_ratio,
        min_integrated_lufs=args.min_integrated_lufs,
        max_true_peak_dbfs=args.max_true_peak_dbfs,
    )


def main() -> int:
    args = build_parser().parse_args()
    video = Path(args.video).expanduser().resolve()
    if not video.is_file():
        print(f"video not found: {video}", file=sys.stderr)
        return 2
    report = Path(args.report).expanduser().resolve() if args.report else video.parent.parent / "qa/qa-report.json"
    contact = Path(args.contact).expanduser().resolve() if args.contact else video.parent.parent / "qa/final-contact.png"
    try:
        payload, ok = inspect(video, report, contact, policy=policy_from_args(args))
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
