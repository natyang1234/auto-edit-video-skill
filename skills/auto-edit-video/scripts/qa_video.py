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


# A project declares what kind of delivery it is making, rather than dialling
# individual thresholds: an open set of numbers is an open set of ways to turn
# the gate off. Every profile here is reviewed and covered by tests.
QA_PROFILES = ("strict", "silent_delivery", "long_pause_delivery")
# Above this share of audible content, a delivery declared silent is not
# silent: it carried sound and then lost it, which is a truncated soundtrack.
SILENT_DELIVERY_MAX_AUDIBLE_RATIO = 0.02


@dataclass(frozen=True)
class QaPolicy:
    """Fail thresholds for delivery QA (contracts M7).

    Defaults are fail-closed: a black, truncated or silent final must never
    pass. A project can declare a profile to allow a delivery that is
    deliberately silent or deliberately paused; nothing relaxes the checks
    that detect a damaged file.
    """

    max_black_segment_seconds: float = 2.0
    max_black_ratio: float = 0.35
    allow_missing_audio: bool = False
    min_integrated_lufs: float = -45.0
    # A clip too short for R128 is judged on sample peak instead, which is a
    # different quantity: real speech here measures around -16 LUFS with
    # peaks near -2 dBFS. Sharing the LUFS number meant tightening the
    # loudness gate silently tightened the peak gate too, and a peak
    # threshold at speech level rejects ordinary quiet talking.
    min_short_clip_peak_dbfs: float = -45.0
    # Full scale itself is clipping: material limited to exactly 0.0 dBTP has
    # been squashed against the ceiling. Delivery targets sit well below.
    max_true_peak_dbfs: float = -0.1
    # Integrated loudness is gated: it ignores silent passages, so a final
    # whose narration was truncated still measures fine. Silent coverage
    # catches that; normal pacing leaves well under this share silent.
    max_silent_ratio: float = 0.8
    # One unbroken silent stretch this large means the audio stopped rather
    # than paused, which total coverage alone lets through. The absolute
    # limit matters because a proportional one lets a long delivery swallow
    # arbitrarily long dead air.
    max_silent_run_ratio: float = 0.45
    max_silent_run_seconds: float = 6.0
    # A brief tail of quiet is normal, so the proportional limit only counts
    # once the stretch reaches this length: a ten second clip closing on a
    # four second call-to-action card stays deliverable.
    min_silent_run_seconds: float = 3.5
    # Floor on how much of the timeline actually carries sound. Without it,
    # silence chopped into runs that each stay under the limits adds up to a
    # near-silent delivery that no other threshold catches.
    min_audible_ratio: float = 0.45
    # Silence that is the point of the delivery rather than a fault. Set only
    # by the silent_delivery profile, which also covers a soundtrack the
    # renderer filled with digital silence because the source had none.
    allow_silent_delivery: bool = False
    profile: str = "strict"
    intent: str = ""

    @classmethod
    def for_profile(cls, profile: str, intent: str = "") -> "QaPolicy":
        if profile not in QA_PROFILES:
            raise ValueError(
                f"unknown QA profile {profile!r}; expected one of {', '.join(QA_PROFILES)}"
            )
        if profile != "strict" and not intent.strip():
            raise ValueError(f"QA profile {profile!r} requires a stated intent")
        if profile == "silent_delivery":
            return cls(
                allow_missing_audio=True,
                allow_silent_delivery=True,
                profile=profile,
                intent=intent,
            )
        if profile == "long_pause_delivery":
            return cls(
                max_silent_run_seconds=20.0,
                max_silent_run_ratio=0.8,
                min_audible_ratio=0.2,
                max_silent_ratio=0.9,
                profile=profile,
                intent=intent,
            )
        return cls(profile=profile, intent=intent)

    def relaxed_fields(self) -> dict[str, Any]:
        """Which thresholds differ from strict, and by how much."""
        strict = QaPolicy()
        return {
            name: {"default": getattr(strict, name), "used": getattr(self, name)}
            for name in (
                "allow_missing_audio",
                "allow_silent_delivery",
                "min_integrated_lufs",
                "min_short_clip_peak_dbfs",
                "max_silent_ratio",
                "max_silent_run_ratio",
                "max_silent_run_seconds",
                "min_silent_run_seconds",
                "min_audible_ratio",
            )
            if getattr(self, name) != getattr(strict, name)
        }

    def __post_init__(self) -> None:
        if self.profile not in QA_PROFILES:
            raise ValueError(f"QA policy profile must be one of {', '.join(QA_PROFILES)}")
        if not isinstance(self.allow_silent_delivery, bool):
            raise ValueError("QA policy allow_silent_delivery must be a bool")
        if not isinstance(self.intent, str):
            raise ValueError("QA policy intent must be a string")
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
            "min_short_clip_peak_dbfs",
            "max_true_peak_dbfs",
            "max_silent_ratio",
            "max_silent_run_ratio",
            "max_silent_run_seconds",
            "min_silent_run_seconds",
            "min_audible_ratio",
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
            or self.max_silent_run_ratio < 0
            or self.max_silent_run_seconds < 0
            or self.min_silent_run_seconds < 0
            or self.min_audible_ratio < 0
        ):
            raise ValueError("QA policy coverage thresholds must be non-negative")


def qa_policy_args(state: dict[str, Any] | None) -> list[str]:
    """QA flags for a project's declared delivery kind; empty means strict."""
    declared = (state or {}).get("qa_policy")
    if not isinstance(declared, dict):
        return []
    profile = declared.get("profile")
    if profile not in QA_PROFILES or profile == "strict":
        return []
    return ["--qa-profile", str(profile), "--qa-intent", str(declared.get("intent", ""))]


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def run(command: list[str], timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, timeout=timeout)


def _positive_float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) and number > 0 else 0.0


def picture_duration_candidates(streams: list[dict[str, Any]]) -> list[float]:
    """How long the picture runs, read from every video stream present.

    Reading only the first stream lets a short decorative track stand in for
    the delivery; a container carrying several pictures runs as long as its
    longest one.
    """
    candidates: list[float] = []
    for visual in streams:
        candidates.append(_positive_float(visual.get("duration")))
        frames = _positive_float(visual.get("nb_frames"))
        rate = str(visual.get("r_frame_rate") or "")
        if frames and re.fullmatch(r"\d+/\d+", rate):
            numerator, denominator = (float(part) for part in rate.split("/"))
            if numerator > 0 and denominator > 0:
                candidates.append(frames * denominator / numerator)
    return [item for item in candidates if item > 0]


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
            "format=duration,size:stream=index,codec_type,codec_name,width,height,r_frame_rate,nb_frames,duration,sample_rate,channels",
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
    # The container header is the one number nothing else checks, and every
    # ratio hangs off it: a short declared duration silences the gates while
    # the file still plays in full, and a long one (an audio track running
    # past the picture) dilutes dead air. Trust the picture instead, and take
    # the longest credible reading of it.
    visuals = [item for item in streams if item.get("codec_type") == "video"]
    picture = max(picture_duration_candidates(visuals), default=0.0)
    if picture > 0:
        duration = picture
    return {
        "duration_s": round(duration, 3),
        "container_duration_s": round(float(data.get("format", {}).get("duration") or 0.0), 3),
        "video_stream_count": len(visuals),
        "audio_stream_count": len([item for item in streams if item.get("codec_type") == "audio"]),
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
# Share of a frame that must be dark before the frame counts as black.
# ffmpeg defaults to 0.98, which misses a failed background render that still
# carries a caption box or logo; 0.85 catches those while leaving room for
# legitimate letterboxing (a pillarboxed portrait delivery is ~68% black).
BLACK_DETECT_PICTURE_RATIO = 0.85


def picture_analysis(
    video: Path, min_duration: float = BLACK_DETECT_MIN_SECONDS
) -> tuple[list[dict[str, float]], float]:
    """Black segments, plus how much picture actually decoded.

    The decoded length is the only reading of the timeline that does not
    come from the file describing itself. Container headers, stream
    durations and frame counts can all disagree with the picture that
    plays, and every ratio here hangs off that number.

    Detection floor: coverage is summed from detected segments, so
    segments shorter than the floor are invisible and fragmented black
    frames would otherwise evade the gate.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("ffmpeg is required")
    result = run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostats",
            "-progress",
            "pipe:1",
            "-i",
            str(video),
            "-map",
            "0:V:0",
            "-vf",
            f"blackdetect=d={min_duration}"
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
    if result.returncode != 0:
        # A partial decode reports less picture than the file holds, which
        # would understate every ratio measured against it.
        raise ValueError(
            (result.stderr or "").strip().splitlines()[-1]
            if result.stderr
            else "picture analysis failed"
        )
    decoded = 0.0
    # Read the position from the machine-readable progress stream on stdout.
    # Parsing stderr instead would also read the input's filename and
    # metadata tags back: both are attacker supplied (a filename containing
    # a newline puts arbitrary text at the start of a line), and the
    # renderer copies source metadata into deliveries.
    for match in re.finditer(r"^out_time_us=(\d+)$", result.stdout or "", re.MULTILINE):
        decoded = max(decoded, int(match.group(1)) / 1_000_000)
    return segments, decoded


# Silence is judged relative to the delivery's own integrated loudness, not
# against an absolute floor. The renderer normalises every final with
# loudnorm, which lifts a quiet passage's noise floor by tens of dB — an
# absolute threshold sees that boosted hiss as sound and reports a truncated
# soundtrack as healthy. Measured on a normalised final: speech sits at the
# integrated level while boosted room tone sits ~35 LU below it.
AUDIBLE_RELATIVE_LU = 25.0
# Fallback floor for material whose integrated loudness cannot be measured.
AUDIBLE_ABSOLUTE_LUFS = -50.0
# ebur128 reports momentary loudness every 100ms.
MOMENTARY_WINDOW_SECONDS = 0.1
# EBU R128 integrates over 400ms, so shorter clips always measure -70 LUFS;
# they are judged on peak level instead.
LOUDNESS_MIN_MEASURABLE_SECONDS = 0.4
# Silence needs windows either side of the ramp to mean anything, so it is
# only judged from this length up. Shorter deliveries rely on the level gates.
SILENCE_MIN_MEASURABLE_SECONDS = LOUDNESS_MIN_MEASURABLE_SECONDS * 2


def momentary_loudness(video: Path) -> list[float]:
    """Momentary loudness per 100ms window, in LUFS."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return []
    result = run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostats",
            "-i",
            str(video),
            "-map",
            "0:a:0",
            "-af",
            "ebur128",
            "-vn",
            "-f",
            "null",
            "-",
        ]
    )
    values: list[float] = []
    # Digital silence reports "nan", not "-inf". A window that cannot be
    # parsed must count as silent: dropping it would erase that slice of the
    # timeline from the measurement instead of marking it dead.
    for match in re.finditer(r"M:\s*(-?nan|-?inf|-?[0-9.]+)", result.stderr or ""):
        raw = match.group(1)
        try:
            value = float(raw)
        except ValueError:
            value = float("-inf")
        values.append(float("-inf") if math.isnan(value) else value)
    return values


def silent_coverage(
    video: Path, duration: float, integrated: float | None = None
) -> dict[str, float] | None:
    """Share of the timeline that carries no audible sound.

    Integrated loudness is gated and ignores silent passages, so a final whose
    audio stops after the opening still measures a healthy level. The
    threshold is relative to the delivery's own level: the renderer
    normalises every final, lifting a quiet passage's noise floor by tens of
    dB, which an absolute threshold reads as sound.
    """
    if duration <= 0:
        return None
    # Whether silence can be judged depends on how long the delivery is, not
    # on how much of it the meter managed to read: an audio track that stops
    # early yields few windows, and treating that as "unmeasurable" would
    # skip the gate on exactly the deliveries it exists for.
    if duration < SILENCE_MIN_MEASURABLE_SECONDS:
        return None
    windows = momentary_loudness(video)
    # Momentary loudness integrates over 400ms, so the opening windows always
    # read near-silent while the measurement fills; counting them invents a
    # leading silent run and swamps a short delivery.
    ramp = round(LOUDNESS_MIN_MEASURABLE_SECONDS / MOMENTARY_WINDOW_SECONDS)
    # Audio running past the picture is not part of the delivery and must not
    # dilute the dead air inside it.
    limit = ramp + max(0, round((duration - LOUDNESS_MIN_MEASURABLE_SECONDS) / MOMENTARY_WINDOW_SECONDS))
    windows = windows[ramp:limit]
    threshold = (
        integrated - AUDIBLE_RELATIVE_LU
        if integrated is not None
        else AUDIBLE_ABSOLUTE_LUFS
    )
    runs: list[float] = []
    current = 0.0
    for value in windows:
        if value < threshold:
            current += MOMENTARY_WINDOW_SECONDS
        elif current:
            runs.append(current)
            current = 0.0
    measured = len(windows) * MOMENTARY_WINDOW_SECONDS
    span = max(0.0, duration - LOUDNESS_MIN_MEASURABLE_SECONDS)
    # Time the measurement never reached is dead air, not absent time: an
    # audio stream shorter than the video stops the meter early, and judging
    # ratios against what was measured would hide the gap entirely.
    uncovered = max(0.0, span - measured)
    current += uncovered
    if current:
        runs.append(current)
    total = sum(runs)
    longest = max(runs, default=0.0)
    silent_ratio = min(total / span, 1.0) if span > 0 else 0.0
    return {
        "silent_seconds": total,
        "silent_ratio": silent_ratio,
        "longest_silent_seconds": longest,
        "longest_silent_ratio": min(longest / span, 1.0) if span > 0 else 0.0,
        "audible_ratio": max(0.0, 1.0 - silent_ratio),
        "audible_threshold_lufs": threshold,
        "measured_seconds": measured,
        "unmeasured_seconds": uncovered,
    }


def peak_level_dbfs(video: Path) -> float | None:
    """Peak sample level, used where integrated loudness cannot be measured."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    result = run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostats",
            "-i",
            str(video),
            "-map",
            "0:a:0",
            "-af",
            "volumedetect",
            "-vn",
            "-f",
            "null",
            "-",
        ]
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
    # Which stream plays is a player's choice, and the measurements here,
    # ffprobe's stream order and a player's selection rule need not agree —
    # so a second stream can carry the picture that ships while a decoy is
    # the one inspected. A delivery from this pipeline holds one of each.
    if media["video_stream_count"] > 1 or media["audio_stream_count"] > 1:
        failures.append(
            f"delivery carries {media['video_stream_count']} video and "
            f"{media['audio_stream_count']} audio streams; which one plays is ambiguous"
        )
    if media["duration_s"] <= 0:
        failures.append("duration must be positive")
    width = int(visual.get("width") or 0)
    height = int(visual.get("height") or 0)
    if width <= 0 or height <= 0:
        failures.append("invalid video dimensions")
    elif width % 2 or height % 2:
        warnings.append("video dimensions are not even")
    if not audio:
        if policy.allow_missing_audio or policy.allow_silent_delivery:
            warnings.append("audio stream is missing")
        else:
            failures.append("audio stream is missing")

    blacks, decoded = picture_analysis(video)
    # The declared timeline is unreliable in both directions: a short claim
    # puts the delivery below the length at which anything is judged, and a
    # long one (a second video stream, an audio track outliving the picture)
    # thins every ratio. What decoded is what plays, and it is the same pass
    # the black segments came from, so ratios stay measured against their own
    # source rather than against a number from somewhere else.
    declared = media["duration_s"]
    if decoded > 0:
        media["duration_s"] = round(decoded, 3)
    media["decoded_seconds"] = round(decoded, 3)
    # Anchoring to the decode keeps every ratio self-consistent, which is
    # exactly why a delivery that stops early would otherwise look like a
    # clean shorter one: ffmpeg reports success on a truncated file. The two
    # readings sat side by side in the report without ever being compared.
    tolerance = max(0.5, declared * 0.02)
    if declared > 0 and decoded > 0 and abs(declared - decoded) > tolerance:
        failures.append(
            f"only {decoded:.2f}s of picture decoded from a {declared:.2f}s delivery "
            f"(truncated or damaged)"
        )
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
    short_clip = 0 < media["duration_s"] < LOUDNESS_MIN_MEASURABLE_SECONDS
    silence = (
        silent_coverage(video, media["duration_s"], (levels or {}).get("integrated_lufs"))
        if audio and not short_clip
        else None
    )
    # A silent delivery is silent throughout. If sound was there and stopped,
    # the ordinary dropout rules still apply — that is damage, not intent.
    declared_silent = policy.allow_silent_delivery and (
        silence is None or silence["audible_ratio"] <= SILENT_DELIVERY_MAX_AUDIBLE_RATIO
    )
    if (
        audio
        and not declared_silent
        and media["duration_s"] >= SILENCE_MIN_MEASURABLE_SECONDS
        and silence is None
    ):
        failures.append("audio could not be measured for silence")
    if audio and silence and not declared_silent:
        if silence["silent_ratio"] >= policy.max_silent_ratio:
            failures.append(
                f"audio is silent for {silence['silent_ratio']:.1%} of the video, at or above "
                f"the {policy.max_silent_ratio:.1%} fail threshold (truncated or missing audio)"
            )
        elif silence["longest_silent_seconds"] >= policy.max_silent_run_seconds or (
            silence["longest_silent_ratio"] >= policy.max_silent_run_ratio
            and silence["longest_silent_seconds"] >= policy.min_silent_run_seconds
        ):
            failures.append(
                f"audio is silent for an unbroken {silence['longest_silent_seconds']:.1f}s "
                f"({silence['longest_silent_ratio']:.1%} of the video), at or above the "
                f"{policy.max_silent_run_ratio:.1%} / {policy.max_silent_run_seconds:.1f}s "
                f"fail thresholds (audio stopped)"
            )
        elif silence["audible_ratio"] < policy.min_audible_ratio:
            failures.append(
                f"only {silence['audible_ratio']:.1%} of the video carries sound, below the "
                f"{policy.min_audible_ratio:.1%} minimum (audio is mostly missing)"
            )
    if audio:
        integrated = (levels or {}).get("integrated_lufs")
        true_peak = (levels or {}).get("true_peak_dbfs")
        if declared_silent:
            pass
        elif short_clip:
            # Too short for R128; judge level on the sample peak so brief
            # clips are neither falsely failed nor waved through.
            peak = peak_level_dbfs(video)
            if peak is None or peak < policy.min_short_clip_peak_dbfs:
                failures.append(
                    "clip is too short to measure loudness and its peak level is silent"
                )
        elif integrated is None:
            failures.append(
                "integrated loudness could not be measured (silent or unreadable audio)"
            )
        elif integrated < policy.min_integrated_lufs:
            failures.append(
                f"integrated loudness {integrated:.1f} LUFS is below the "
                f"{policy.min_integrated_lufs:.1f} LUFS fail threshold (near-silent audio)"
            )
        # Clipping applies at every duration: a short clip is still a delivery.
        if true_peak is None:
            if not short_clip and not declared_silent and integrated is not None:
                failures.append("true peak could not be measured (silent or unreadable audio)")
        elif true_peak > policy.max_true_peak_dbfs:
            failures.append(
                f"true peak {true_peak:.1f} dBFS is above the "
                f"{policy.max_true_peak_dbfs:.1f} dBFS fail threshold (clipping)"
            )
    contact_sheet(video, contact_path, media["duration_s"])
    report = {
        "schema_version": 2,
        "generated_at": now_utc(),
        "profile": policy.profile,
        "intent": policy.intent,
        "relaxed_fields": policy.relaxed_fields(),
        "video": str(video),
        "status": "pass" if not failures else "fail",
        "media": media,
        "black_segments": blacks,
        "black_detection": {
            "min_segment_seconds": BLACK_DETECT_MIN_SECONDS,
            "pixel_threshold": BLACK_DETECT_PIXEL_THRESHOLD,
            "picture_ratio_threshold": BLACK_DETECT_PICTURE_RATIO,
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
        "--qa-profile",
        choices=QA_PROFILES,
        help="declare the kind of delivery; anything but strict needs --qa-intent",
    )
    parser.add_argument(
        "--qa-intent",
        default="",
        help="why this delivery is deliberately silent or paused",
    )
    parser.add_argument(
        "--max-silent-ratio",
        type=float,
        default=defaults.max_silent_ratio,
        help="fail when audio is silent for at least this fraction of the duration",
    )
    parser.add_argument(
        "--max-silent-run-ratio",
        type=float,
        default=defaults.max_silent_run_ratio,
        help="fail when one unbroken silence covers at least this fraction of the duration",
    )
    parser.add_argument(
        "--max-silent-run-seconds",
        type=float,
        default=defaults.max_silent_run_seconds,
        help="fail when one unbroken silence lasts at least this many seconds",
    )
    parser.add_argument(
        "--min-silent-run-seconds",
        type=float,
        default=defaults.min_silent_run_seconds,
        help="shortest silence the proportional run limit applies to",
    )
    parser.add_argument(
        "--min-audible-ratio",
        type=float,
        default=defaults.min_audible_ratio,
        help="fail when less than this fraction of the duration carries sound",
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
    if args.qa_profile:
        # A declared profile is a reviewed set; individual dials do not apply.
        return QaPolicy.for_profile(args.qa_profile, args.qa_intent)
    return QaPolicy(
        max_black_segment_seconds=args.max_black_segment_seconds,
        max_black_ratio=args.max_black_ratio,
        allow_missing_audio=args.allow_missing_audio,
        max_silent_ratio=args.max_silent_ratio,
        max_silent_run_ratio=args.max_silent_run_ratio,
        max_silent_run_seconds=args.max_silent_run_seconds,
        min_silent_run_seconds=args.min_silent_run_seconds,
        min_audible_ratio=args.min_audible_ratio,
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
