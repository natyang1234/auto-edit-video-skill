#!/usr/bin/env python3
"""Deterministic, local-only Phase 0d SFX delivery primitives.

All final-timeline timing is stored as integer 48kHz samples.  This module
intentionally has no renderer or provider dependency so validation can be
repeated by the delivery and QA paths.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import os
import re
import shutil
import stat
import struct
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

SAMPLE_RATE = 48000
CHANNELS = 2
SAMPLE_WIDTH = 3
WINDOW_SAMPLES = 240  # 5ms
ALIGNMENT_TOLERANCE = 3840
# Final candidate cue evidence must meet this signed correlation threshold.
# Schema-v2 first binds one pipeline lag using cue-excluded private dialogue,
# then checks every cue only at that independent lag.
CANDIDATE_CORRELATION_THRESHOLD = 0.30
_CANDIDATE_MAX_PIPELINE_LAG_SAMPLES = 512
_PIPELINE_LAG_DIALOGUE_STRIDE = 67
_PIPELINE_LAG_MIN_DIALOGUE_SAMPLES = 512
# Candidate sample-count tolerance against the staged final-domain stem is
# DIRECTIONAL, because the codec artefact it absorbs only has one sign.
#
# Surplus (candidate longer than planned): AAC framing commonly contributes
# up to 512 decoded samples of end padding at 48 kHz; 1024 bounds that with
# margin.  Nothing else in the pipeline can lengthen the mix, so anything
# beyond this is a different render and fails closed.
#
# Deficit (candidate shorter than planned): the AAC encoder ends the stream
# on a frame boundary and drops the trailing partial frame rather than
# padding it, so a real final decodes short.  Phase 4 measured 3.1k-3.3k
# samples short on every real kinetic cut; four AAC frames (4 x 1024 = 4096)
# bounds that with one frame of margin.  Ruling of 2026-08-13: the symmetric
# 1024 window was rejecting genuine deliveries, and widening it symmetrically
# would have loosened the surplus side for no codec reason.
CANDIDATE_SAMPLE_COUNT_TOLERANCE = 1024
CANDIDATE_SAMPLE_COUNT_TOLERANCE_TRAILING = 4096


def candidate_sample_count_within_tolerance(delta: int) -> bool:
    """Signed codec tolerance for candidate-vs-planned sample counts.

    `delta` is observed minus expected.  Deliverer and QA both decide with
    this one predicate so the two sides can never drift apart.
    """
    if delta >= 0:
        return delta <= CANDIDATE_SAMPLE_COUNT_TOLERANCE
    return -delta <= CANDIDATE_SAMPLE_COUNT_TOLERANCE_TRAILING
DIALOGUE_PRIORITY_WINDOW_SAMPLES = 12000
DIALOGUE_PRIORITY_THRESHOLD_DBFS = -45.0
DIALOGUE_PRIORITY_REQUIRED_REDUCTION_DB = 6.0
DIALOGUE_PRIORITY_SILENCE_DBFS = -120.0
DIALOGUE_PRIORITY_AUTHORITY = (
    "same-render pre-final-loudnorm dialogue and post-sidechain pre-amix SFX"
)

STARTER_PACK_ID = "phase1-local-procedural"
STARTER_ASSET_IDS = (
    "soft-ui-tick-v1",
    "short-pop-v1",
    "short-whoosh-v1",
    "soft-impact-v1",
    "short-riser-v1",
    "typing-tick-v1",
    "completion-chime-v1",
)
STARTER_ASSET_ROLES = {
    "soft-ui-tick-v1": "title_enter",
    "short-pop-v1": "row_reveal",
    "short-whoosh-v1": "transition",
    "soft-impact-v1": "grid_fill",
    "short-riser-v1": "count_tick",
    "typing-tick-v1": "typing",
    "completion-chime-v1": "complete",
}
_DENSITY_ROLE_ASSETS = {
    "title_enter": "soft-ui-tick-v1",
    "transition": "short-whoosh-v1",
    "row_reveal": "short-pop-v1",
    "count_tick": "short-riser-v1",
    "grid_fill": "soft-impact-v1",
    "typing": "typing-tick-v1",
    "complete": "completion-chime-v1",
}
_DENSITY_PRIORITY = {
    "title_enter": 0,
    "transition": 0,
    "grid_fill": 1,
    "count_tick": 1,
    "complete": 1,
    "row_reveal": 2,
    "typing": 3,
}
_DENSITY_MIN_ONSET_SAMPLES = 5760  # 120 ms at 48 kHz
_DENSITY_MAX_OVERLAP = 2
_DENSITY_MAX_CUES_PER_MINUTE = 40
# Keep the Phase 0d filename stable because existing renderer/integration
# callers open it directly.  New assets are deliberately explicit rather
# than derived from a lossy role or a filesystem glob.
STARTER_ASSET_FILENAMES = {
    "soft-ui-tick-v1": "generated-soft-ui-tick.wav",
    "short-pop-v1": "generated-short-pop-v1.wav",
    "short-whoosh-v1": "generated-short-whoosh-v1.wav",
    "soft-impact-v1": "generated-soft-impact-v1.wav",
    "short-riser-v1": "generated-short-riser-v1.wav",
    "typing-tick-v1": "generated-typing-tick-v1.wav",
    "completion-chime-v1": "generated-completion-chime-v1.wav",
}
_STARTER_ASSET_SPECS = {
    "short-pop-v1": {
        "name": "short_pop",
        "recipe": "sine_pop_exponential_decay",
        "kind": "pop",
        "audible_seconds": 0.10,
    },
    "short-whoosh-v1": {
        "name": "short_whoosh",
        "recipe": "swept_sine_fade",
        "kind": "whoosh",
        "audible_seconds": 0.24,
    },
    "soft-impact-v1": {
        "name": "soft_impact",
        "recipe": "low_tone_exponential_decay",
        "kind": "impact",
        "audible_seconds": 0.14,
    },
    "short-riser-v1": {
        "name": "short_riser",
        "recipe": "swept_sine_rise",
        "kind": "riser",
        "audible_seconds": 0.36,
    },
    "typing-tick-v1": {
        "name": "typing_tick",
        "recipe": "short_sine_exponential_decay",
        "kind": "typing",
        "audible_seconds": 0.08,
    },
    "completion-chime-v1": {
        "name": "completion_chime",
        "recipe": "two_note_exponential_decay",
        "kind": "chime",
        "audible_seconds": 0.42,
    },
}


class SfxDeliveryError(ValueError):
    """An SFX artifact is malformed or cannot prove final-domain binding."""


@dataclass(frozen=True)
class DecodedWav:
    sample_rate: int
    channels: int
    sample_width: int
    pcm: bytes
    samples: list[tuple[float, float]]


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def _cue_template_correlation(
    output_samples: list[tuple[float, float]],
    stem_samples: list[tuple[float, float]],
    event_start_sample: int,
    duration_samples: int,
    *,
    max_lag_samples: int = 512,
) -> tuple[float, int] | None:
    """Find a lagged normalized correlation against the deterministic cue."""
    if type(event_start_sample) is not int or type(duration_samples) is not int:
        return None
    if duration_samples <= 0 or event_start_sample < 0:
        return None
    template_start = event_start_sample
    template_end = min(len(stem_samples), template_start + duration_samples)
    template = [
        (stem_samples[index][0] + stem_samples[index][1]) / 2.0
        for index in range(template_start, template_end)
    ]
    if not template:
        return None
    peak = max(abs(value) for value in template)
    if peak <= 0:
        return None
    # Ignore padded silence while retaining the deterministic attack/decay.
    active = [index for index, value in enumerate(template) if abs(value) >= peak * 0.01]
    if len(active) < 16:
        return None
    # The cue is a deterministic 120 ms attack/decay.  Sample every eighth
    # active point for the lag search instead of running a Python-level
    # 5,000-sample dot product at every codec offset.  Keeping the attack and
    # tail makes this shape-specific evidence robust while bounded.
    if len(active) > 512:
        last_active = active[-1]
        active = active[::8]
        if active[-1] != last_active:
            active.append(last_active)
    template_values = [template[index] for index in active]
    template_count = len(template_values)
    template_sum = sum(template_values)
    template_square_sum = sum(value * value for value in template_values)
    template_energy = template_square_sum - template_sum * template_sum / template_count
    if template_energy <= 0:
        return None
    best: tuple[float, int] | None = None
    for lag in range(-max_lag_samples, max_lag_samples + 1):
        output_sum = 0.0
        output_square_sum = 0.0
        dot_sum = 0.0
        count = 0
        for template_index, template_value in zip(active, template_values):
            output_index = event_start_sample + lag + template_index
            if 0 <= output_index < len(output_samples):
                output_value = (output_samples[output_index][0] + output_samples[output_index][1]) / 2.0
                output_sum += output_value
                output_square_sum += output_value * output_value
                dot_sum += output_value * template_value
                count += 1
        if count < max(16, len(active) // 2):
            continue
        output_energy = output_square_sum - output_sum * output_sum / count
        if count == template_count:
            template_energy_lag = template_energy
            covariance = dot_sum - output_sum * template_sum / count
        else:
            # Only candidate clips shorter than the cue reach this branch;
            # recompute the valid template moments without changing the
            # acceptance rule for ordinary final-length candidates.
            valid_sum = 0.0
            valid_square_sum = 0.0
            valid_dot = 0.0
            valid_count = 0
            for template_index, template_value in zip(active, template_values):
                output_index = event_start_sample + lag + template_index
                if 0 <= output_index < len(output_samples):
                    output_value = (output_samples[output_index][0] + output_samples[output_index][1]) / 2.0
                    valid_sum += template_value
                    valid_square_sum += template_value * template_value
                    valid_dot += output_value * template_value
                    valid_count += 1
            if valid_count != count:
                continue
            template_energy_lag = valid_square_sum - valid_sum * valid_sum / count
            covariance = valid_dot - output_sum * valid_sum / count
        if output_energy <= 0 or template_energy_lag <= 0:
            continue
        correlation = covariance / math.sqrt(output_energy * template_energy_lag)
        # A polarity-inverted cue is not proof of the baked positive-gain
        # stem.  Keep the signed Pearson coefficient rather than allowing an
        # anti-correlation to satisfy the publish threshold.
        candidate = (correlation, lag)
        if best is None or candidate[0] > best[0]:
            best = candidate
    return best


def _partial_correlation(
    output_values: list[float],
    template_values: list[float],
    dialogue_values: list[float],
) -> float | None:
    """Correlate cue and candidate after regressing out private dialogue."""
    count = len(output_values)
    if count < 2 or len(template_values) != count or len(dialogue_values) != count:
        return None
    if not all(
        math.isfinite(value)
        for values in (output_values, template_values, dialogue_values)
        for value in values
    ):
        return None
    output_mean = sum(output_values) / count
    template_mean = sum(template_values) / count
    dialogue_mean = sum(dialogue_values) / count
    if not all(math.isfinite(value) for value in (output_mean, template_mean, dialogue_mean)):
        return None
    output_centered = [value - output_mean for value in output_values]
    template_centered = [value - template_mean for value in template_values]
    dialogue_centered = [value - dialogue_mean for value in dialogue_values]
    output_energy = sum(value * value for value in output_centered)
    template_energy = sum(value * value for value in template_centered)
    dialogue_energy = sum(value * value for value in dialogue_centered)
    if not all(
        math.isfinite(value)
        for value in (output_energy, template_energy, dialogue_energy)
    ):
        return None
    if output_energy <= 0.0 or template_energy <= 0.0:
        return None
    if dialogue_energy <= 0.0:
        covariance = sum(
            output * template
            for output, template in zip(output_centered, template_centered)
        )
        denominator = math.sqrt(output_energy * template_energy)
        correlation = covariance / denominator
        return correlation if math.isfinite(correlation) else None

    output_beta = sum(
        output * dialogue
        for output, dialogue in zip(output_centered, dialogue_centered)
    ) / dialogue_energy
    template_beta = sum(
        template * dialogue
        for template, dialogue in zip(template_centered, dialogue_centered)
    ) / dialogue_energy
    if not math.isfinite(output_beta) or not math.isfinite(template_beta):
        return None
    output_residual = [
        output - output_beta * dialogue
        for output, dialogue in zip(output_centered, dialogue_centered)
    ]
    template_residual = [
        template - template_beta * dialogue
        for template, dialogue in zip(template_centered, dialogue_centered)
    ]
    output_residual_energy = sum(value * value for value in output_residual)
    template_residual_energy = sum(value * value for value in template_residual)
    if not math.isfinite(output_residual_energy) or not math.isfinite(template_residual_energy):
        return None
    # An output explained entirely by dialogue contains no independently
    # observable cue.  Quantized private PCM can leave tiny numerical residue,
    # so treat less than 1e-8 of the original energy as zero evidence.
    if (
        output_residual_energy <= output_energy * 1e-8
        or template_residual_energy <= template_energy * 1e-8
    ):
        return 0.0
    covariance = sum(
        output * template
        for output, template in zip(output_residual, template_residual)
    )
    correlation = covariance / math.sqrt(
        output_residual_energy * template_residual_energy
    )
    if not math.isfinite(correlation):
        return None
    return max(-1.0, min(1.0, correlation))


def _estimate_dialogue_pipeline_lag(
    output_samples: list[tuple[float, float]],
    dialogue_samples: list[tuple[float, float]],
    events: list[dict[str, Any]],
    *,
    max_lag_samples: int = _CANDIDATE_MAX_PIPELINE_LAG_SAMPLES,
) -> tuple[float, int, float] | None:
    """Bind one candidate lag and gain outside every cue window."""
    if type(max_lag_samples) is not int or max_lag_samples < 0:
        return None
    common_length = min(len(output_samples), len(dialogue_samples))
    if common_length <= 2 * max_lag_samples:
        return None
    exclusions: list[tuple[int, int]] = []
    for event in events:
        if not isinstance(event, dict):
            return None
        start = event.get("event_start_sample")
        duration = event.get("duration_samples")
        if type(start) is not int or type(duration) is not int or start < 0 or duration <= 0:
            return None
        # A wrong trial lag can differ from the true bounded lag by twice the
        # search radius.  Exclude that full neighborhood so no cue sample can
        # influence selection of the global dialogue authority.
        exclusions.append((
            max(0, start - 2 * max_lag_samples),
            min(common_length, start + duration + 2 * max_lag_samples),
        ))
    dialogue_indices = [
        index
        for index in range(
            max_lag_samples,
            common_length - max_lag_samples,
            _PIPELINE_LAG_DIALOGUE_STRIDE,
        )
        if not any(lower <= index < upper for lower, upper in exclusions)
    ]
    if len(dialogue_indices) < _PIPELINE_LAG_MIN_DIALOGUE_SAMPLES:
        return None
    dialogue_values = [
        (dialogue_samples[index][0] + dialogue_samples[index][1]) / 2.0
        for index in dialogue_indices
    ]
    if not all(math.isfinite(value) for value in dialogue_values):
        return None

    best: tuple[float, int] | None = None
    zero_control = [0.0] * len(dialogue_values)
    for lag in range(-max_lag_samples, max_lag_samples + 1):
        output_values = [
            (
                output_samples[index + lag][0]
                + output_samples[index + lag][1]
            ) / 2.0
            for index in dialogue_indices
        ]
        correlation = _partial_correlation(
            output_values,
            dialogue_values,
            zero_control,
        )
        if correlation is None:
            continue
        candidate = (correlation, lag)
        if (
            best is None
            or correlation > best[0]
            or (correlation == best[0] and abs(lag) < abs(best[1]))
        ):
            best = candidate
    if best is None:
        return None
    output_values = [
        (
            output_samples[index + best[1]][0]
            + output_samples[index + best[1]][1]
        ) / 2.0
        for index in dialogue_indices
    ]
    dialogue_energy = sum(value * value for value in dialogue_values)
    dialogue_gain = sum(
        output * dialogue
        for output, dialogue in zip(output_values, dialogue_values)
    ) / dialogue_energy
    if not math.isfinite(dialogue_gain) or dialogue_gain <= 0.0:
        return None
    return best[0], best[1], dialogue_gain


def _signed_projection_gain(
    observed: list[float],
    expected: list[float],
    indices: list[int],
) -> float | None:
    """Return signed least-squares gain for one non-empty cue region."""
    if not indices:
        return None
    expected_energy = sum(expected[index] ** 2 for index in indices)
    if not math.isfinite(expected_energy) or expected_energy <= 0.0:
        return None
    gain = sum(
        observed[index] * expected[index]
        for index in indices
    ) / expected_energy
    return gain if math.isfinite(gain) else None


def _cue_template_partial_correlation(
    output_samples: list[tuple[float, float]],
    stem_samples: list[tuple[float, float]],
    dialogue_samples: list[tuple[float, float]],
    event_start_sample: int,
    duration_samples: int,
    *,
    pipeline_lag_samples: int,
    dialogue_gain: float,
    max_lag_samples: int = _CANDIDATE_MAX_PIPELINE_LAG_SAMPLES,
) -> tuple[float, int, bool] | None:
    """Densely check one cue at the independently bound pipeline lag."""
    if type(event_start_sample) is not int or type(duration_samples) is not int:
        return None
    if (
        type(pipeline_lag_samples) is not int
        or not isinstance(dialogue_gain, (int, float))
        or isinstance(dialogue_gain, bool)
        or not math.isfinite(dialogue_gain)
        or dialogue_gain <= 0.0
        or type(max_lag_samples) is not int
        or max_lag_samples < 0
        or abs(pipeline_lag_samples) > max_lag_samples
        or duration_samples <= 0
        or event_start_sample < 0
    ):
        return None
    template_end = min(len(stem_samples), event_start_sample + duration_samples)
    if template_end > len(dialogue_samples):
        return None
    template = [
        (stem_samples[index][0] + stem_samples[index][1]) / 2.0
        for index in range(event_start_sample, template_end)
    ]
    if not template:
        return None
    peak = max(abs(value) for value in template)
    if peak <= 0.0:
        return None
    active = [index for index, value in enumerate(template) if abs(value) >= peak * 0.01]
    if len(active) < 16:
        return None
    residual_values: list[float] = []
    expected_values: list[float] = []
    for template_index in active:
        output_index = event_start_sample + pipeline_lag_samples + template_index
        dialogue_index = event_start_sample + template_index
        if 0 <= output_index < len(output_samples):
            output_frame = output_samples[output_index]
            dialogue_frame = dialogue_samples[dialogue_index]
            output_value = (output_frame[0] + output_frame[1]) / 2.0
            dialogue_value = (dialogue_frame[0] + dialogue_frame[1]) / 2.0
            residual_values.append(output_value - dialogue_gain * dialogue_value)
            expected_values.append(dialogue_gain * template[template_index])
    if len(residual_values) < max(16, len(active) // 2):
        return None
    correlation = _partial_correlation(
        residual_values,
        expected_values,
        [0.0] * len(expected_values),
    )
    if correlation is None:
        return None
    expected_peak = max(abs(value) for value in expected_values)
    energy_regions = (
        [
            index for index, value in enumerate(expected_values)
            if abs(value) < CANDIDATE_CORRELATION_THRESHOLD * expected_peak
        ],
        [
            index for index, value in enumerate(expected_values)
            if abs(value) >= CANDIDATE_CORRELATION_THRESHOLD * expected_peak
        ],
    )
    region_gains = [
        _signed_projection_gain(residual_values, expected_values, region)
        for region in energy_regions
        if region
    ]
    complete = bool(region_gains) and all(
        gain is not None and gain >= CANDIDATE_CORRELATION_THRESHOLD
        for gain in region_gains
    )
    return correlation, pipeline_lag_samples, complete


def _candidate_window_peak_dbfs(
    samples: list[tuple[float, float]], center_sample: int, *, width_samples: int = SAMPLE_RATE // 4
) -> float:
    half = width_samples // 2
    peak = max(
        (
            abs(samples[index][channel])
            if 0 <= index < len(samples) else 0.0
        )
        for index in range(center_sample - half, center_sample + half)
        for channel in range(CHANNELS)
    )
    return round(dbfs(peak), 6)


def seconds_to_samples(value: str | int | float | Decimal) -> int:
    """Convert exactly once using Decimal round-half-up; reject bool/nonfinite."""
    if isinstance(value, bool):
        raise SfxDeliveryError("seconds must be a finite number, not bool")
    try:
        decimal = Decimal(str(value))
    except Exception as exc:
        raise SfxDeliveryError("seconds must be numeric") from exc
    if not decimal.is_finite() or decimal < 0:
        raise SfxDeliveryError("seconds must be finite and non-negative")
    return int((decimal * SAMPLE_RATE).to_integral_value(rounding=ROUND_HALF_UP))


def _pack_s24(sample: float) -> bytes:
    value = max(-1.0, min(1.0 - 1 / 8388608, sample))
    integer = int(round(value * 8388608))
    return int(integer).to_bytes(3, "little", signed=True)


def _unpack_s24(payload: bytes) -> float:
    return int.from_bytes(payload, "little", signed=True) / 8388608.0


def decode_s24le_wav(path: Path) -> DecodedWav:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise SfxDeliveryError(f"unreadable WAV: {path}") from exc
    return decode_s24le_wav_bytes(payload, source=str(path))


def decode_s24le_wav_bytes(payload: bytes, *, source: str = "<bytes>") -> DecodedWav:
    """Decode one already-read WAV payload without reading its source again."""
    try:
        with wave.open(io.BytesIO(payload), "rb") as wav:
            channels, width, rate = wav.getnchannels(), wav.getsampwidth(), wav.getframerate()
            compression = wav.getcomptype()
            pcm = wav.readframes(wav.getnframes())
    except (OSError, wave.Error) as exc:
        raise SfxDeliveryError(f"unreadable WAV: {source}") from exc
    if (rate, channels, width, compression) != (SAMPLE_RATE, CHANNELS, SAMPLE_WIDTH, "NONE"):
        raise SfxDeliveryError("WAV must be 48kHz stereo PCM s24le")
    if len(pcm) % (CHANNELS * SAMPLE_WIDTH):
        raise SfxDeliveryError("WAV PCM byte count is not frame aligned")
    samples = [
        (_unpack_s24(pcm[index:index + 3]), _unpack_s24(pcm[index + 3:index + 6]))
        for index in range(0, len(pcm), 6)
    ]
    return DecodedWav(rate, channels, width, pcm, samples)


def _decode_candidate_audio_path(path: Path) -> DecodedWav:
    """Decode one immutable candidate snapshot as stereo float PCM."""
    ffprobe = shutil.which("ffprobe")
    ffmpeg = shutil.which("ffmpeg")
    if not ffprobe or not ffmpeg:
        raise SfxDeliveryError("ffprobe and ffmpeg are required for candidate audio evidence")
    probe = subprocess.run(
        [
            ffprobe, "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=sample_rate,channels",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if probe.returncode != 0:
        raise SfxDeliveryError(f"candidate audio probe failed: {probe.stderr.strip()[-500:]}")
    try:
        streams = json.loads(probe.stdout).get("streams", [])
        metadata = streams[0]
        sample_rate = int(metadata.get("sample_rate"))
        channels = int(metadata.get("channels"))
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SfxDeliveryError("candidate audio stream metadata is invalid") from exc
    if sample_rate != SAMPLE_RATE or channels != CHANNELS:
        raise SfxDeliveryError("candidate final audio must be native 48kHz stereo")
    decoded = subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(path),
            "-map", "0:a:0", "-f", "f32le", "-ac", str(CHANNELS),
            "-ar", str(SAMPLE_RATE), "pipe:1",
        ],
        capture_output=True,
        timeout=120,
    )
    if decoded.returncode != 0:
        raise SfxDeliveryError(
            f"candidate audio decode failed: {decoded.stderr.decode(errors='replace')[-500:]}"
        )
    pcm = decoded.stdout
    frame_bytes = CHANNELS * 4
    if not pcm or len(pcm) % frame_bytes:
        raise SfxDeliveryError("candidate decoded PCM is empty or frame-misaligned")
    samples = [tuple(frame) for frame in struct.iter_unpack("<ff", pcm)]
    if not all(
        math.isfinite(sample)
        for frame in samples
        for sample in frame
    ):
        raise SfxDeliveryError("candidate decoded PCM contains non-finite samples")
    return DecodedWav(SAMPLE_RATE, CHANNELS, 4, pcm, samples)


def _decode_candidate_audio_bytes(payload: bytes, *, source: str) -> DecodedWav:
    """Decode the exact bytes already used for candidate hash evidence.

    ffprobe and ffmpeg both need a pathname, so give them a private snapshot
    rather than reopening a live candidate path after its hash was measured.
    This keeps the reported hash and decoded observation bound to one byte
    sequence across atomic candidate replacement or symlink races.
    """
    if not isinstance(payload, bytes) or not payload:
        raise SfxDeliveryError("candidate output bytes are empty")
    with tempfile.TemporaryDirectory(prefix="sfx-candidate-") as directory:
        snapshot = Path(directory) / "candidate.mp4"
        snapshot.write_bytes(payload)
        try:
            return _decode_candidate_audio_path(snapshot)
        except SfxDeliveryError as exc:
            raise SfxDeliveryError(f"{source}: {exc}") from exc


def decode_candidate_audio(path: Path) -> DecodedWav:
    """Decode a candidate's native 48 kHz final audio from one byte snapshot."""
    try:
        payload = Path(path).read_bytes()
    except OSError as exc:
        raise SfxDeliveryError(f"unreadable candidate: {path}") from exc
    return _decode_candidate_audio_bytes(payload, source=str(path))


def _window_rms(samples: list[tuple[float, float]], start: int) -> float:
    # Measurement windows deliberately zero-pad both output boundaries; Python
    # negative slices would instead wrap around to the tail of the stem.
    window = [
        samples[index] if 0 <= index < len(samples) else (0.0, 0.0)
        for index in range(start, start + WINDOW_SAMPLES)
    ]
    channel_rms = []
    for channel in range(CHANNELS):
        channel_rms.append(math.sqrt(sum(frame[channel] ** 2 for frame in window) / WINDOW_SAMPLES))
    return max(channel_rms)


def dbfs(amplitude: float) -> float:
    return -120.0 if amplitude <= 0 else 20.0 * math.log10(amplitude)


def _percentile_10(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[math.floor((len(ordered) - 1) * 0.10)]


def _window_rms_values(samples: list[tuple[float, float]]) -> list[float]:
    """Compute every zero-padded 5 ms RMS window in O(N) time."""
    count = len(samples)
    if not count:
        return []
    prefix = [[0.0] * (count + 1) for _ in range(CHANNELS)]
    for index, frame in enumerate(samples, start=1):
        for channel in range(CHANNELS):
            prefix[channel][index] = prefix[channel][index - 1] + frame[channel] ** 2
    values: list[float] = []
    for start in range(count):
        end = min(count, start + WINDOW_SAMPLES)
        values.append(max(
            math.sqrt(max(0.0, prefix[channel][end] - prefix[channel][start]) / WINDOW_SAMPLES)
            for channel in range(CHANNELS)
        ))
    return values


def transient_metrics(samples: list[tuple[float, float]]) -> dict[str, Any]:
    if not samples:
        return {"noise_floor_dbfs": -120.0, "transient_anchor_sample": None}
    # The catalog and delivered stem must use the identical sliding 5ms
    # detector.  Non-overlapping catalog windows versus sliding stem windows
    # create a 239-sample bias that can hide a +3841 mutation.
    starts = list(range(0, len(samples)))
    rms = _window_rms_values(samples)
    floor = _percentile_10(rms)
    threshold = max(10 ** (-45 / 20), floor * (10 ** (12 / 20)))
    for start, value in zip(starts, rms):
        if value >= threshold:
            # A 5ms RMS observation is timestamped at the last included
            # sample.  This preserves a sample-exact alignment boundary while
            # remaining translation-invariant between catalog and stem.
            return {
                "noise_floor_dbfs": round(dbfs(floor), 6),
                "transient_anchor_sample": start + WINDOW_SAMPLES - 1,
            }
    return {"noise_floor_dbfs": round(dbfs(floor), 6), "transient_anchor_sample": None}


def detect_transient(samples: list[tuple[float, float]], *, expected_sample: int) -> int | None:
    if type(expected_sample) is not int:
        raise SfxDeliveryError("expected sample must be an integer")
    threshold_floor = 10 ** (-45 / 20)
    # Use full stem noise floor, then search only the required alignment range.
    rms_values = _window_rms_values(samples)
    floor = _percentile_10(rms_values)
    threshold = max(threshold_floor, floor * (10 ** (12 / 20)))
    lower, upper = expected_sample - ALIGNMENT_TOLERANCE, expected_sample + ALIGNMENT_TOLERANCE
    # Candidate starts are sample-exact.  Advancing in 5ms hops makes a
    # +3841-sample mutation fall into the preceding +3840 window and falsely
    # pass the strict 80ms contract.
    # Candidate windows may start before sample zero; the helper below keeps
    # the same zero-padding semantics as _window_rms without an O(N*window)
    # slice for every candidate.
    count = len(samples)
    prefix = [[0.0] * (count + 1) for _ in range(CHANNELS)]
    for index, frame in enumerate(samples, start=1):
        for channel in range(CHANNELS):
            prefix[channel][index] = prefix[channel][index - 1] + frame[channel] ** 2
    for start in range(lower - WINDOW_SAMPLES + 1, upper - WINDOW_SAMPLES + 2):
        begin = max(0, start)
        end = min(count, start + WINDOW_SAMPLES)
        value = max(
            math.sqrt(max(0.0, prefix[channel][end] - prefix[channel][begin]) / WINDOW_SAMPLES)
            if begin < end else 0.0
            for channel in range(CHANNELS)
        )
        if value >= threshold:
            return start + WINDOW_SAMPLES - 1
    return None


def alignment_ok(expected_sample: int, observed_sample: int | None) -> bool:
    return observed_sample is not None and abs(observed_sample - expected_sample) <= ALIGNMENT_TOLERANCE


def _bake_s24_pcm(pcm: bytes, gain_db: float) -> bytes:
    """Apply deterministic integer s24 gain to interleaved PCM bytes."""
    if isinstance(gain_db, bool) or not isinstance(gain_db, (int, float)) or not math.isfinite(float(gain_db)):
        raise SfxDeliveryError("stem gain must be a finite number")
    gain = 10 ** (float(gain_db) / 20.0)
    if gain <= 0 or not math.isfinite(gain):
        raise SfxDeliveryError("stem gain is invalid")
    output = bytearray()
    for index in range(0, len(pcm), CHANNELS * SAMPLE_WIDTH):
        for channel in range(CHANNELS):
            offset = index + channel * SAMPLE_WIDTH
            integer = int.from_bytes(pcm[offset:offset + SAMPLE_WIDTH], "little", signed=True)
            scaled = max(-8388608, min(8388607, int(round(integer * gain))))
            output.extend(scaled.to_bytes(SAMPLE_WIDTH, "little", signed=True))
    return bytes(output)


def generate_soft_ui_tick(path: Path) -> dict[str, Any]:
    """Write the original local starter asset; no referenced sound is used."""
    leading_silence = WINDOW_SAMPLES
    audible_frames = int(SAMPLE_RATE * 0.12)
    # Keep enough true trailing silence that the normative 10th-percentile
    # floor is zero for both the short catalog asset and its long QA stem.
    trailing_silence = 1200
    frames = leading_silence + audible_frames + trailing_silence
    pcm = bytearray()
    for index in range(frames):
        t = max(0, index - leading_silence) / SAMPLE_RATE
        envelope = (
            0.0 if index < leading_silence or index >= leading_silence + audible_frames
            else math.exp(-32 * t)
        )
        # A short deterministic attack keeps the measured anchor invariant
        # after the final-domain -12 dB stem bake.
        if index == leading_silence:
            value = 0.50
        else:
            # Two sine partials make a restrained original UI tick.
            value = 0.47 * envelope * (math.sin(2 * math.pi * 1550 * t) + 0.35 * math.sin(2 * math.pi * 2325 * t))
        pcm.extend(_pack_s24(value))
        pcm.extend(_pack_s24(value))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(bytes(pcm))
    decoded = decode_s24le_wav(path)
    rms = max(math.sqrt(sum(frame[channel] ** 2 for frame in decoded.samples) / len(decoded.samples)) for channel in range(CHANNELS))
    peak = max(abs(value) for frame in decoded.samples for value in frame)
    metrics = transient_metrics(decoded.samples)
    return {
        "asset_id": "soft-ui-tick-v1", "pack": "phase0d-local-procedural", "role": "title_enter",
        "generator": {"name": "soft_ui_tick", "version": 1, "recipe": "two_sine_exponential_decay"},
        "duration_samples": len(decoded.samples), "transient_anchor_sample": metrics["transient_anchor_sample"],
        "noise_floor_dbfs": metrics["noise_floor_dbfs"], "rms_dbfs": round(dbfs(rms), 6), "peak_dbfs": round(dbfs(peak), 6),
        "wav_sha256": sha256_file(path), "decoded_pcm_sha256": sha256_bytes(decoded.pcm),
        "provenance": "original local procedural generation; no external/reference audio", "review_state": "approved_generated",
    }


def _generate_local_procedural_asset(path: Path, asset_id: str) -> dict[str, Any]:
    """Generate one deterministic Phase 1 starter asset from a local recipe."""
    spec = _STARTER_ASSET_SPECS[asset_id]
    leading_silence = WINDOW_SAMPLES
    audible_frames = int(SAMPLE_RATE * spec["audible_seconds"])
    trailing_silence = 1200
    frames = leading_silence + audible_frames + trailing_silence
    pcm = bytearray()
    kind = spec["kind"]
    for index in range(frames):
        relative = index - leading_silence
        if relative < 0 or relative >= audible_frames:
            value = 0.0
        elif relative == 0:
            # A deterministic attack sample makes the transient anchor stable
            # across all local procedural recipes.
            value = 0.50
        else:
            t = relative / SAMPLE_RATE
            progress = relative / max(1, audible_frames - 1)
            if kind == "pop":
                envelope = math.exp(-34 * t)
                value = 0.45 * envelope * (
                    math.sin(2 * math.pi * 980 * t)
                    + 0.22 * math.sin(2 * math.pi * 1960 * t)
                )
            elif kind == "whoosh":
                envelope = math.sin(math.pi * progress) ** 0.7
                frequency = 260 + 1800 * progress
                value = 0.48 * envelope * math.sin(2 * math.pi * frequency * t)
            elif kind == "impact":
                envelope = math.exp(-26 * t)
                value = 0.45 * envelope * (
                    math.sin(2 * math.pi * 125 * t)
                    + 0.20 * math.sin(2 * math.pi * 1500 * t)
                )
            elif kind == "riser":
                envelope = 0.18 + 0.82 * progress
                frequency = 220 + 2200 * progress
                value = 0.46 * envelope * math.sin(2 * math.pi * frequency * t)
            elif kind == "typing":
                envelope = math.exp(-62 * t)
                value = 0.48 * envelope * math.sin(2 * math.pi * 1880 * t)
            elif kind == "chime":
                first = 0.34 * math.exp(-14 * t) * math.sin(2 * math.pi * 880 * t)
                second_t = max(0.0, t - 0.075)
                second = 0.24 * math.exp(-18 * second_t) * math.sin(2 * math.pi * 1320 * second_t)
                value = first + second
            else:  # pragma: no cover - recipes are fixed by the local table
                raise SfxDeliveryError(f"unknown procedural starter recipe: {kind}")
        pcm.extend(_pack_s24(value))
        pcm.extend(_pack_s24(value))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(bytes(pcm))
    decoded = decode_s24le_wav(path)
    rms = max(
        math.sqrt(sum(frame[channel] ** 2 for frame in decoded.samples) / len(decoded.samples))
        for channel in range(CHANNELS)
    )
    peak = max(abs(value) for frame in decoded.samples for value in frame)
    metrics = transient_metrics(decoded.samples)
    return {
        "asset_id": asset_id,
        "pack": STARTER_PACK_ID,
        "role": STARTER_ASSET_ROLES[asset_id],
        "generator": {"name": spec["name"], "version": 1, "recipe": spec["recipe"]},
        "duration_samples": len(decoded.samples),
        "transient_anchor_sample": metrics["transient_anchor_sample"],
        "noise_floor_dbfs": metrics["noise_floor_dbfs"],
        "rms_dbfs": round(dbfs(rms), 6),
        "peak_dbfs": round(dbfs(peak), 6),
        "wav_sha256": sha256_file(path),
        "decoded_pcm_sha256": sha256_bytes(decoded.pcm),
        "provenance": "original local procedural generation; no external/reference audio",
        "review_state": "approved_generated",
    }


def _generate_starter_assets(directory: Path) -> tuple[dict[str, dict[str, Any]], dict[str, DecodedWav]]:
    """Build the complete starter pack and return metadata plus decoded proof."""
    assets: dict[str, dict[str, Any]] = {}
    decoded: dict[str, DecodedWav] = {}
    for asset_id in STARTER_ASSET_IDS:
        path = Path(directory) / STARTER_ASSET_FILENAMES[asset_id]
        if asset_id == "soft-ui-tick-v1":
            # Keep Phase 0d PCM bytes and its canonical filename unchanged;
            # only the catalog pack identity advances for the full starter pack.
            asset = generate_soft_ui_tick(path)
            asset = {**asset, "pack": STARTER_PACK_ID}
        else:
            asset = _generate_local_procedural_asset(path, asset_id)
        assets[asset_id] = asset
        decoded[asset_id] = decode_s24le_wav(path)
    return assets, decoded


def write_one_cue_stem(
    path: Path,
    *,
    total_samples: int,
    asset_path: Path,
    event_start_sample: int,
    gain_db: float = -12.0,
) -> DecodedWav:
    if type(total_samples) is not int or type(event_start_sample) is not int or total_samples <= 0 or event_start_sample < 0:
        raise SfxDeliveryError("stem timing must use positive integer samples")
    if isinstance(gain_db, bool) or not isinstance(gain_db, (int, float)) or not math.isfinite(float(gain_db)):
        raise SfxDeliveryError("stem gain must be a finite number")
    asset = decode_s24le_wav(asset_path)
    if event_start_sample + len(asset.samples) > total_samples:
        raise SfxDeliveryError("SFX payload extends beyond final output")
    baked_asset_pcm = _bake_s24_pcm(asset.pcm, float(gain_db))
    silence = b"\0" * (total_samples * CHANNELS * SAMPLE_WIDTH)
    payload = bytearray(silence)
    start = event_start_sample * CHANNELS * SAMPLE_WIDTH
    payload[start:start + len(baked_asset_pcm)] = baked_asset_pcm
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(bytes(payload))
    return decode_s24le_wav(path)


def _contract_errors(name: str, artifact: dict[str, Any]) -> list[str]:
    """Use the repository's strict dialect without importing it at module load."""
    try:
        import contract_registry

        return contract_registry.validate_artifact(name, artifact)
    except Exception as exc:  # pragma: no cover - defensive import boundary
        return [str(exc)]


def validate_catalog(catalog: dict[str, Any]) -> bool:
    return isinstance(catalog, dict) and not _contract_errors("sfx_catalog", catalog)


def validate_plan(plan: dict[str, Any]) -> bool:
    return isinstance(plan, dict) and not _contract_errors("audio_event_plan", plan)


def canonical_trigger(visual_evidence: dict[str, Any]) -> dict[str, Any]:
    """Pick exactly one delivered faithful title trigger from renderer evidence."""
    if not isinstance(visual_evidence, dict) or not isinstance(visual_evidence.get("items"), list):
        raise SfxDeliveryError("renderer visual evidence items are required")
    candidates = []
    for item in visual_evidence["items"]:
        if not isinstance(item, dict) or item.get("kind") != "title":
            continue
        motion = item.get("motion") if isinstance(item.get("motion"), dict) else {}
        requested = motion.get("requested")
        delivered = motion.get("delivered")
        if (
            requested == "pop"
            or (isinstance(requested, str) and requested.startswith("slide"))
        ) and (
            isinstance(delivered, str)
            and delivered.strip()
            and delivered.lower() not in {"none", "static"}
            and motion.get("status") != "fallback"
            and motion.get("faithful") is True
        ):
            candidates.append(item)
    if len(candidates) != 1:
        raise SfxDeliveryError("exactly one faithful title pop/slide trigger is required")
    item = candidates[0]
    onset = seconds_to_samples(item.get("start"))
    identifier = item.get("id")
    if not isinstance(identifier, str) or not identifier:
        raise SfxDeliveryError("eligible title trigger needs an id")
    motion = item["motion"]
    return {
        "id": identifier,
        "onset_sample": onset,
        "kind": "title",
        "motion": {
            "requested": motion.get("requested"),
            "delivered": motion.get("delivered"),
            "faithful": motion.get("faithful"),
            "status": motion.get("status"),
        },
    }


def plan_role_events(visual_evidence: dict[str, Any]) -> list[dict[str, Any]]:
    """Plan role proposals from faithful renderer evidence only.

    This seam deliberately stops at role/asset proposals.  It does not create
    an audio plan or claim stem timing, mixing, density, or completion cues.
    """
    if not isinstance(visual_evidence, dict) or not isinstance(visual_evidence.get("items"), list):
        raise SfxDeliveryError("renderer visual evidence items are required")

    proposals: list[dict[str, Any]] = []
    seen_trigger_ids: set[str] = set()
    for item in visual_evidence["items"]:
        if not isinstance(item, dict):
            continue
        motion = item.get("motion")
        if not isinstance(motion, dict):
            continue
        status = motion.get("status")
        if (
            motion.get("faithful") is not True
            or not isinstance(status, str)
            or status not in {"native", "rendered"}
        ):
            continue
        requested = motion.get("requested")
        delivered = motion.get("delivered")
        if (
            not isinstance(requested, str)
            or not isinstance(delivered, str)
            or not delivered.strip()
            or delivered.lower() in {"none", "static"}
            or delivered != requested
        ):
            continue
        kind = item.get("kind")
        component_id = item.get("component_id")
        if component_id is not None and not isinstance(component_id, str):
            continue
        role_asset: tuple[str, str] | None = None
        if kind == "title":
            title_kind = item.get("title_kind")
            if (
                title_kind == "section"
                and component_id in {None, "kinetic-title", "title-lockup"}
                and requested in {"pop", "pop-in", "slide-up", "slide-in"}
            ):
                role_asset = ("transition", "short-whoosh-v1")
            elif component_id == "prompt-card" and requested == "word-cascade":
                role_asset = ("typing", "typing-tick-v1")
            elif (
                component_id in {None, "kinetic-title", "title-lockup"}
                and requested in {"pop", "pop-in", "slide-up", "slide-in"}
            ):
                role_asset = ("title_enter", "soft-ui-tick-v1")
        elif kind == "dynamic_list" and component_id in {"dynamic-list", "warning-checklist"}:
            if requested in {"staggered-reveal", "check-pop"}:
                role_asset = ("row_reveal", "short-pop-v1")
        elif kind == "stat" and component_id == "progress":
            if (
                item.get("family") == "grid_progress"
                and item.get("trigger_role") == "grid_complete"
                and requested == "fill"
            ):
                role_asset = ("complete", "completion-chime-v1")
            elif requested in {"count-up", "fill"}:
                role_asset = ("count_tick", "short-riser-v1")
        elif kind == "stat" and component_id == "hero-stat":
            if requested == "count-up":
                role_asset = ("count_tick", "short-riser-v1")
        elif kind == "chart" and component_id == "dashboard":
            if requested in {"pan", "fill"}:
                role_asset = ("grid_fill", "soft-impact-v1")
        elif (
            kind == "mosaic"
            and component_id == "asset-mosaic"
            and item.get("family") == "asset_mosaic"
            and item.get("trigger_role") == "scene_transition"
            and requested == "pan"
        ):
            role_asset = ("transition", "short-whoosh-v1")
        if role_asset is None:
            continue
        trigger_id = item.get("id")
        if not isinstance(trigger_id, str) or not trigger_id.strip():
            raise SfxDeliveryError("eligible trigger needs a non-empty id")
        try:
            onset_sample = seconds_to_samples(item.get("start"))
        except SfxDeliveryError as exc:
            raise SfxDeliveryError(f"eligible trigger start is invalid: {exc}") from exc
        if trigger_id in seen_trigger_ids:
            raise SfxDeliveryError(f"duplicate eligible trigger id: {trigger_id}")
        seen_trigger_ids.add(trigger_id)
        role, asset_id = role_asset
        proposals.append({
            "trigger_id": trigger_id,
            "trigger_onset_sample": onset_sample,
            "role": role,
            "asset_id": asset_id,
            "evidence": {"trigger": copy.deepcopy(item)},
        })

    if not proposals:
        raise SfxDeliveryError("no eligible renderer role events")
    proposals.sort(key=lambda proposal: proposal["trigger_onset_sample"])
    return proposals


def _density_catalog_by_asset_id(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Validate and index the complete Phase 1 catalog without changing it."""
    if not isinstance(catalog, dict):
        raise SfxDeliveryError("density policy catalog must be an object")
    catalog_errors = _contract_errors("sfx_catalog", catalog)
    if catalog_errors:
        raise SfxDeliveryError(f"density policy catalog contract failed: {catalog_errors[0]}")
    assets = catalog.get("assets")
    if not isinstance(assets, list) or len(assets) != len(STARTER_ASSET_IDS):
        raise SfxDeliveryError("density policy requires the complete Phase 1 catalog")

    by_asset_id: dict[str, dict[str, Any]] = {}
    for asset in assets:
        if not isinstance(asset, dict):
            raise SfxDeliveryError("density policy catalog asset must be an object")
        asset_id = asset.get("asset_id")
        if not isinstance(asset_id, str) or asset_id in by_asset_id:
            raise SfxDeliveryError("density policy catalog has duplicate asset id")
        by_asset_id[asset_id] = asset
    if set(by_asset_id) != set(STARTER_ASSET_IDS):
        raise SfxDeliveryError("density policy catalog is not the complete Phase 1 starter pack")
    try:
        with tempfile.TemporaryDirectory(prefix="sfx-density-catalog-") as directory:
            expected_assets, _ = _generate_starter_assets(Path(directory))
    except Exception as exc:
        raise SfxDeliveryError(f"density policy starter catalog cannot be rebuilt: {exc}") from exc
    for asset_id in STARTER_ASSET_IDS:
        if by_asset_id[asset_id] != expected_assets[asset_id]:
            raise SfxDeliveryError(
                f"density policy catalog asset is not the deterministic starter asset: {asset_id}"
            )
    return by_asset_id


def _resolve_density_proposal(
    proposal: dict[str, Any],
    catalog_by_asset_id: dict[str, dict[str, Any]],
    total_samples: int,
    seen_trigger_ids: set[str],
) -> dict[str, Any]:
    """Validate one tracer-2 proposal and resolve its final-domain interval."""
    if not isinstance(proposal, dict):
        raise SfxDeliveryError("density policy proposal must be an object")
    required = (
        "trigger_id",
        "trigger_onset_sample",
        "role",
        "asset_id",
        "evidence",
    )
    missing = [field for field in required if field not in proposal]
    if missing:
        raise SfxDeliveryError(
            f"density policy proposal missing required field: {missing[0]}"
        )

    trigger_id = proposal["trigger_id"]
    if not isinstance(trigger_id, str) or not trigger_id.strip():
        raise SfxDeliveryError("density policy proposal trigger_id must be non-empty")
    if trigger_id in seen_trigger_ids:
        raise SfxDeliveryError(f"density policy duplicate trigger id: {trigger_id}")
    seen_trigger_ids.add(trigger_id)

    trigger_onset = proposal["trigger_onset_sample"]
    if type(trigger_onset) is not int or trigger_onset < 0:
        raise SfxDeliveryError(
            "density policy proposal trigger_onset_sample must be a non-negative integer"
        )
    role = proposal["role"]
    asset_id = proposal["asset_id"]
    if not isinstance(role, str) or not isinstance(asset_id, str):
        raise SfxDeliveryError("density policy proposal role and asset_id must be strings")
    expected_asset_id = _DENSITY_ROLE_ASSETS.get(role)
    if expected_asset_id is None or asset_id != expected_asset_id:
        raise SfxDeliveryError(
            f"density policy role/asset mapping is invalid: {role!r} -> {asset_id!r}"
        )
    evidence = proposal["evidence"]
    if not isinstance(evidence, dict):
        raise SfxDeliveryError("density policy proposal evidence must be an object")
    evidence_trigger = evidence.get("trigger")
    if not isinstance(evidence_trigger, dict):
        raise SfxDeliveryError("density policy evidence.trigger must be an object")
    try:
        authoritative_proposals = plan_role_events({
            "items": [copy.deepcopy(evidence_trigger)]
        })
    except SfxDeliveryError as exc:
        raise SfxDeliveryError(
            f"density policy renderer evidence is not eligible: {exc}"
        ) from exc
    except Exception as exc:  # pragma: no cover - defensive evidence boundary
        raise SfxDeliveryError(
            f"density policy renderer evidence cannot be validated: {exc}"
        ) from exc
    if len(authoritative_proposals) != 1:
        raise SfxDeliveryError(
            "density policy renderer evidence must produce exactly one proposal"
        )
    try:
        proposal_matches_evidence = proposal == authoritative_proposals[0]
    except Exception as exc:  # pragma: no cover - defensive comparison boundary
        raise SfxDeliveryError(
            f"density policy proposal cannot be compared with renderer evidence: {exc}"
        ) from exc
    if not proposal_matches_evidence:
        raise SfxDeliveryError(
            "density policy proposal does not exactly match renderer evidence"
        )
    if evidence_trigger.get("id") != trigger_id:
        raise SfxDeliveryError("density policy evidence trigger id does not match proposal")
    has_timing_authority = False
    if "start" in evidence_trigger:
        try:
            evidence_onset = seconds_to_samples(evidence_trigger["start"])
        except SfxDeliveryError as exc:
            raise SfxDeliveryError(f"density policy evidence trigger start is invalid: {exc}") from exc
        if evidence_onset != trigger_onset:
            raise SfxDeliveryError("density policy evidence trigger start does not match proposal")
        has_timing_authority = True
    if "onset_sample" in evidence_trigger:
        evidence_onset = evidence_trigger["onset_sample"]
        if type(evidence_onset) is not int or evidence_onset < 0:
            raise SfxDeliveryError(
                "density policy evidence trigger onset_sample must be a non-negative integer"
            )
        if evidence_onset != trigger_onset:
            raise SfxDeliveryError("density policy evidence trigger onset does not match proposal")
        has_timing_authority = True
    if not has_timing_authority:
        raise SfxDeliveryError(
            "density policy evidence trigger requires start or onset_sample timing"
        )

    asset = catalog_by_asset_id.get(asset_id)
    if asset is None:  # pragma: no cover - complete-catalog validation covers this
        raise SfxDeliveryError(f"density policy asset is unavailable: {asset_id}")
    duration = asset.get("duration_samples")
    anchor = asset.get("transient_anchor_sample")
    if type(duration) is not int or duration <= 0 or type(anchor) is not int or not 0 <= anchor < duration:
        raise SfxDeliveryError(f"density policy catalog timing is invalid for {asset_id}")

    event_start = max(0, trigger_onset - anchor)
    expected_transient = event_start + anchor
    if abs(expected_transient - trigger_onset) > ALIGNMENT_TOLERANCE:
        raise SfxDeliveryError(
            f"density policy expected transient is outside {ALIGNMENT_TOLERANCE}-sample trigger tolerance"
        )
    event_end = event_start + duration
    if event_end > total_samples:
        raise SfxDeliveryError("density policy SFX payload extends beyond final output")

    try:
        evidence_copy = copy.deepcopy(evidence)
    except Exception as exc:
        raise SfxDeliveryError("density policy proposal evidence cannot be copied") from exc
    return {
        "trigger_id": trigger_id,
        "trigger_onset_sample": trigger_onset,
        "event_start_sample": event_start,
        "expected_transient_sample": expected_transient,
        "duration_samples": duration,
        "event_end_sample": event_end,
        "asset_transient_anchor_sample": anchor,
        "role": role,
        "asset_id": asset_id,
        "evidence": evidence_copy,
    }


def _density_would_exceed_overlap(
    candidate: dict[str, Any], kept: list[dict[str, Any]]
) -> bool:
    """Check overlap with half-open intervals, ending before starting at ties."""
    points: list[tuple[int, int]] = []
    for event in (*kept, candidate):
        points.append((event["event_start_sample"], 1))
        points.append((event["event_end_sample"], -1))
    active = 0
    for _, delta in sorted(points, key=lambda point: (point[0], point[1])):
        active += delta
        if active > _DENSITY_MAX_OVERLAP:
            return True
    return False


def apply_density_policy(
    proposals: list[dict[str, Any]],
    catalog: dict[str, Any],
    total_samples: int,
) -> dict[str, Any]:
    """Resolve and deterministically filter Phase 1 role-event proposals.

    The returned ``kept`` entries are final-domain interval resolutions.  The
    ``dropped`` entries retain the same resolution plus one explicit policy
    reason, and ``policy_evidence`` records the limits and deterministic order
    used for the decision.  This seam does not write an audio plan or mix.
    """
    if type(total_samples) is not int or total_samples <= 0:
        raise SfxDeliveryError("density policy total_samples must be a positive integer")
    if not isinstance(proposals, list):
        raise SfxDeliveryError("density policy proposals must be an array")

    catalog_by_asset_id = _density_catalog_by_asset_id(catalog)
    seen_trigger_ids: set[str] = set()
    resolved = [
        _resolve_density_proposal(
            proposal, catalog_by_asset_id, total_samples, seen_trigger_ids
        )
        for proposal in proposals
    ]
    candidates = sorted(
        resolved,
        key=lambda event: (_DENSITY_PRIORITY[event["role"]], event["trigger_onset_sample"], event["trigger_id"]),
    )
    density_cap = (total_samples * _DENSITY_MAX_CUES_PER_MINUTE) // (60 * SAMPLE_RATE)
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []

    for candidate in candidates:
        reason: str | None = None
        if any(
            abs(
                candidate["expected_transient_sample"]
                - event["expected_transient_sample"]
            )
            < _DENSITY_MIN_ONSET_SAMPLES
            for event in kept
        ):
            reason = "adjacent_onset"
        elif _density_would_exceed_overlap(candidate, kept):
            reason = "overlap_limit"
        elif len(kept) >= density_cap:
            reason = "clip_density_limit"

        if reason is None:
            kept.append(candidate)
            continue
        dropped.append({
            **copy.deepcopy(candidate),
            "reason": reason,
            "resolved_event": copy.deepcopy(candidate),
        })

    kept.sort(key=lambda event: (event["trigger_onset_sample"], event["trigger_id"]))
    dropped_reasons: dict[str, int] = {}
    for item in dropped:
        dropped_reasons[item["reason"]] = dropped_reasons.get(item["reason"], 0) + 1
    policy_evidence = {
        "total_samples": total_samples,
        "sample_rate": SAMPLE_RATE,
        "adjacent_onset_samples": _DENSITY_MIN_ONSET_SAMPLES,
        "adjacent_onset_authority": "expected_transient_sample",
        "max_overlap": _DENSITY_MAX_OVERLAP,
        "max_cues_per_minute": _DENSITY_MAX_CUES_PER_MINUTE,
        "density_cap": density_cap,
        "priority_order": [
            "title_enter",
            "transition",
            "grid_fill",
            "complete",
            "count_tick",
            "row_reveal",
            "typing",
        ],
        "candidate_order": [event["trigger_id"] for event in candidates],
        "kept_trigger_ids": [event["trigger_id"] for event in kept],
        "dropped_trigger_ids": [event["trigger_id"] for event in dropped],
        "dropped_reasons": dropped_reasons,
    }
    return {
        "kept": copy.deepcopy(kept),
        "dropped": copy.deepcopy(dropped),
        "policy_evidence": policy_evidence,
    }


def _canonical_hash(value: Any) -> str:
    try:
        import contract_registry

        return contract_registry.canonical_hash(value)
    except Exception as exc:
        raise SfxDeliveryError(f"cannot canonicalize SFX payload: {exc}") from exc


def canonical_motion_plan_hash(visual_evidence: dict[str, Any]) -> str:
    """Hash ordered renderer evidence items, never a caller-supplied hash."""
    if not isinstance(visual_evidence, dict) or not isinstance(visual_evidence.get("items"), list):
        raise SfxDeliveryError("renderer visual evidence items are required")
    return _canonical_hash({"items": visual_evidence["items"]})


def effective_cut_map_sha256(project_dir: Path, state: dict[str, Any]) -> str:
    """Return an owned cut-map byte hash, or a non-null canonical segment hash."""
    root = Path(project_dir).expanduser().resolve()
    cut_map = root / "working" / "cut_map.json"
    if cut_map.is_symlink():
        raise SfxDeliveryError("working/cut_map.json must not be a symlink")
    if cut_map.is_file():
        return sha256_file(cut_map)
    segments = state.get("segments") if isinstance(state, dict) else []
    if not isinstance(segments, list):
        segments = []
    return _canonical_hash({"segments": segments})


def _duration_samples_from_evidence(visual_evidence: dict[str, Any]) -> int:
    raw_samples = visual_evidence.get("duration_samples")
    if type(raw_samples) is int and raw_samples > 0:
        return raw_samples
    if "duration_s" in visual_evidence:
        samples = seconds_to_samples(visual_evidence["duration_s"])
        if samples > 0:
            return samples
    ends = [seconds_to_samples(item.get("end")) for item in visual_evidence.get("items", [])
            if isinstance(item, dict) and item.get("end") is not None]
    if ends and max(ends) > 0:
        return max(ends)
    raise SfxDeliveryError("visual evidence must include a positive duration")


def _require_sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise SfxDeliveryError(f"{field} must be lowercase 64-hex")
    return value


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _normalized_renderer_event_evidence(resolved_event: dict[str, Any]) -> dict[str, Any]:
    """Reduce one density proposal to strict v2 evidence plus its source hash."""
    evidence = resolved_event.get("evidence")
    raw_trigger = evidence.get("trigger") if isinstance(evidence, dict) else None
    if not isinstance(raw_trigger, dict):
        raise SfxDeliveryError("multi-event renderer evidence trigger is required")
    motion = raw_trigger.get("motion")
    if not isinstance(motion, dict):
        raise SfxDeliveryError("multi-event renderer evidence motion is required")
    component_id = raw_trigger.get("component_id")
    if component_id is not None and not isinstance(component_id, str):
        raise SfxDeliveryError("multi-event renderer component_id must be a string or null")
    normalized_trigger = {
        "id": raw_trigger.get("id"),
        "onset_sample": resolved_event.get("trigger_onset_sample"),
        "kind": raw_trigger.get("kind"),
        "component_id": component_id,
        "motion": {
            "requested": motion.get("requested"),
            "delivered": motion.get("delivered"),
            "faithful": motion.get("faithful"),
            "status": motion.get("status"),
        },
    }
    if raw_trigger.get("title_kind") == "section":
        normalized_trigger["title_kind"] = "section"
    scene_pair = (raw_trigger.get("family"), raw_trigger.get("trigger_role"))
    if scene_pair in {
        ("grid_progress", "grid_complete"),
        ("asset_mosaic", "scene_transition"),
    }:
        normalized_trigger["family"] = scene_pair[0]
        normalized_trigger["trigger_role"] = scene_pair[1]
    if not isinstance(normalized_trigger["id"], str) or not normalized_trigger["id"].strip():
        raise SfxDeliveryError("multi-event renderer evidence trigger id is required")
    if type(normalized_trigger["onset_sample"]) is not int or normalized_trigger["onset_sample"] < 0:
        raise SfxDeliveryError("multi-event renderer evidence onset must be a non-negative integer")
    return {
        "trigger": normalized_trigger,
        "renderer_trigger_sha256": _canonical_hash(raw_trigger),
    }


def _normalized_density_drop(resolved_event: dict[str, Any]) -> dict[str, Any]:
    """Serialize only deterministic, hash-bound fields for one dropped event."""
    return {
        "trigger_id": resolved_event.get("trigger_id"),
        "trigger_onset_sample": resolved_event.get("trigger_onset_sample"),
        "event_start_sample": resolved_event.get("event_start_sample"),
        "duration_samples": resolved_event.get("duration_samples"),
        "asset_transient_anchor_sample": resolved_event.get("asset_transient_anchor_sample"),
        "expected_transient_sample": resolved_event.get("expected_transient_sample"),
        "role": resolved_event.get("role"),
        "asset_id": resolved_event.get("asset_id"),
        "evidence": _normalized_renderer_event_evidence(resolved_event),
        "reason": resolved_event.get("reason"),
    }


def _pack_s24_integer(value: int) -> bytes:
    integer = max(-8388608, min(8388607, int(value)))
    return integer.to_bytes(SAMPLE_WIDTH, "little", signed=True)


def _write_multi_event_stem(
    path: Path,
    *,
    total_samples: int,
    events: list[dict[str, Any]],
    asset_paths: dict[str, Path],
) -> DecodedWav:
    """Mix all event payloads samplewise into one deterministic s24 stem."""
    if type(total_samples) is not int or total_samples <= 0:
        raise SfxDeliveryError("multi-event stem timing must use positive integer samples")
    mixed = [0] * (total_samples * CHANNELS)
    for event in events:
        asset_id = event.get("asset_id")
        asset_path = asset_paths.get(asset_id)
        start = event.get("event_start_sample")
        duration = event.get("duration_samples")
        if asset_path is None or type(start) is not int or type(duration) is not int:
            raise SfxDeliveryError("multi-event stem event timing or asset is invalid")
        asset = decode_s24le_wav(asset_path)
        if len(asset.samples) != duration or start < 0 or start + duration > total_samples:
            raise SfxDeliveryError("multi-event stem payload extends beyond final output")
        gain_db = event.get("gain_db")
        if (
            isinstance(gain_db, bool)
            or not isinstance(gain_db, (int, float))
            or not math.isfinite(float(gain_db))
            or not -24.0 <= float(gain_db) <= -6.0
        ):
            raise SfxDeliveryError("multi-event stem event gain is invalid")
        baked_pcm = _bake_s24_pcm(asset.pcm, float(gain_db))
        for frame_index in range(duration):
            destination = (start + frame_index) * CHANNELS
            source = frame_index * CHANNELS * SAMPLE_WIDTH
            for channel in range(CHANNELS):
                offset = source + channel * SAMPLE_WIDTH
                sample = int.from_bytes(
                    baked_pcm[offset:offset + SAMPLE_WIDTH], "little", signed=True
                )
                mixed[destination + channel] = max(
                    -8388608, min(8388607, mixed[destination + channel] + sample)
                )
    pcm = bytearray()
    for sample in mixed:
        pcm.extend(_pack_s24_integer(sample))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(bytes(pcm))
    return decode_s24le_wav(path)


def stage_multi_event_delivery(
    stage_dir: Path,
    visual_evidence: dict[str, Any],
    timeline_revision: str,
    cut_map_sha256: str,
) -> tuple[Path, Path, Path]:
    """Stage a schema-v2 deterministic multi-event plan, catalog and stem."""
    stage = Path(stage_dir)
    stage.mkdir(parents=True, exist_ok=True)
    timeline_revision = _require_sha256(timeline_revision, "timeline_revision")
    cut_map_sha256 = _require_sha256(cut_map_sha256, "cut_map_sha256")
    try:
        evidence_snapshot = copy.deepcopy(visual_evidence)
    except Exception as exc:
        raise SfxDeliveryError("renderer visual evidence cannot be copied") from exc
    proposals = plan_role_events(evidence_snapshot)
    total_samples = _duration_samples_from_evidence(evidence_snapshot)
    motion_hash = canonical_motion_plan_hash(evidence_snapshot)

    generated_assets, _ = _generate_starter_assets(stage)
    catalog = {
        "schema_version": 1,
        "sample_rate": SAMPLE_RATE,
        "channels": CHANNELS,
        "sample_width_bytes": SAMPLE_WIDTH,
        "assets": [generated_assets[asset_id] for asset_id in STARTER_ASSET_IDS],
    }
    catalog_errors = _contract_errors("sfx_catalog", catalog)
    if catalog_errors:
        raise SfxDeliveryError(f"sfx_catalog contract failed: {catalog_errors[0]}")
    density = apply_density_policy(proposals, catalog, total_samples)
    kept = sorted(
        density["kept"],
        key=lambda event: (event["trigger_onset_sample"], event["trigger_id"]),
    )
    if not kept:
        raise SfxDeliveryError("multi-event density policy kept no events")
    events: list[dict[str, Any]] = []
    for index, resolved_event in enumerate(kept, start=1):
        evidence = _normalized_renderer_event_evidence(resolved_event)
        events.append({
            "id": f"sfx-event-{index:04d}",
            "trigger_id": resolved_event["trigger_id"],
            "trigger_onset_sample": resolved_event["trigger_onset_sample"],
            "event_start_sample": resolved_event["event_start_sample"],
            "duration_samples": resolved_event["duration_samples"],
            "asset_id": resolved_event["asset_id"],
            "asset_transient_anchor_sample": resolved_event["asset_transient_anchor_sample"],
            "expected_transient_sample": resolved_event["expected_transient_sample"],
            "role": resolved_event["role"],
            "gain_db": -12,
            "fades": {"in_samples": 0, "out_samples": 0},
            "duck_group": "dialogue_priority",
            "evidence": evidence,
            "reason": f"faithful {resolved_event['role']}",
            "review_state": "approved_generated",
        })

    density_evidence = {
        **density["policy_evidence"],
        "dropped": [_normalized_density_drop(item) for item in density["dropped"]],
    }
    asset_paths = {
        asset_id: stage / STARTER_ASSET_FILENAMES[asset_id]
        for asset_id in STARTER_ASSET_IDS
    }
    stem_path = stage / "sfx_stem.wav"
    decoded = _write_multi_event_stem(
        stem_path,
        total_samples=total_samples,
        events=events,
        asset_paths=asset_paths,
    )
    stem_bytes = stem_path.read_bytes()
    plan = {
        "schema_version": 2,
        "timeline_revision": timeline_revision,
        "cut_map_sha256": cut_map_sha256,
        "resolved_motion_plan_hash": motion_hash,
        "sample_rate": SAMPLE_RATE,
        "channels": CHANNELS,
        "sample_width_bytes": SAMPLE_WIDTH,
        "alignment_tolerance_samples": ALIGNMENT_TOLERANCE,
        "sfx_stem_sha256": sha256_bytes(stem_bytes),
        "sfx_stem_decoded_pcm_sha256": sha256_bytes(decoded.pcm),
        "sfx_stem_sample_count": len(decoded.samples),
        "mix_mode": "deterministic_s24_saturating_sum_v1",
        "density": density_evidence,
        "events": events,
    }
    plan_errors = _contract_errors("audio_event_plan", plan)
    if plan_errors:
        raise SfxDeliveryError(f"audio_event_plan contract failed: {plan_errors[0]}")
    catalog_path = stage / "audio_catalog.json"
    plan_path = stage / "audio_event_plan.json"
    _write_json(catalog_path, catalog)
    _write_json(plan_path, plan)
    return plan_path, catalog_path, stem_path


def stage_one_cue_delivery(
    stage_dir: Path,
    visual_evidence: dict[str, Any],
    timeline_revision: str,
    cut_map_sha256: str,
) -> tuple[Path, Path, Path]:
    """Stage one deterministic generated cue and its hash-bound artifacts."""
    stage = Path(stage_dir)
    stage.mkdir(parents=True, exist_ok=True)
    timeline_revision = _require_sha256(timeline_revision, "timeline_revision")
    cut_map_sha256 = _require_sha256(cut_map_sha256, "cut_map_sha256")
    trigger = canonical_trigger(visual_evidence)
    motion_hash = canonical_motion_plan_hash(visual_evidence)
    total_samples = _duration_samples_from_evidence(visual_evidence)

    generated_assets, _ = _generate_starter_assets(stage)
    asset = generated_assets["soft-ui-tick-v1"]
    asset_path = stage / STARTER_ASSET_FILENAMES["soft-ui-tick-v1"]
    catalog = {
        "schema_version": 1,
        "sample_rate": SAMPLE_RATE,
        "channels": CHANNELS,
        "sample_width_bytes": SAMPLE_WIDTH,
        "assets": [generated_assets[asset_id] for asset_id in STARTER_ASSET_IDS],
    }
    event_start = max(0, trigger["onset_sample"] - asset["transient_anchor_sample"])
    expected = event_start + asset["transient_anchor_sample"]
    if not alignment_ok(trigger["onset_sample"], expected):
        raise SfxDeliveryError("SFX trigger cannot be aligned inside final timeline")
    stem_path = stage / "sfx_stem.wav"
    decoded = write_one_cue_stem(
        stem_path,
        total_samples=total_samples,
        asset_path=asset_path,
        event_start_sample=event_start,
        gain_db=-12.0,
    )
    stem_bytes = stem_path.read_bytes()
    plan = {
        "schema_version": 1,
        "timeline_revision": timeline_revision,
        "cut_map_sha256": cut_map_sha256,
        "resolved_motion_plan_hash": motion_hash,
        "sample_rate": SAMPLE_RATE,
        "channels": CHANNELS,
        "sample_width_bytes": SAMPLE_WIDTH,
        "alignment_tolerance_samples": ALIGNMENT_TOLERANCE,
        "sfx_stem_sha256": sha256_bytes(stem_bytes),
        "sfx_stem_decoded_pcm_sha256": sha256_bytes(decoded.pcm),
        "sfx_stem_sample_count": len(decoded.samples),
        "events": [{
            "id": "sfx-title-enter-0001",
            "trigger_id": trigger["id"],
            "trigger_onset_sample": trigger["onset_sample"],
            "event_start_sample": event_start,
            "duration_samples": asset["duration_samples"],
            "asset_id": asset["asset_id"],
            "asset_transient_anchor_sample": asset["transient_anchor_sample"],
            "expected_transient_sample": expected,
            "role": "title_enter",
            "gain_db": -12,
            "fades": {"in_samples": 0, "out_samples": 0},
            "duck_group": "dialogue_priority",
            "evidence": {"trigger": trigger},
            "reason": "faithful title enter",
            "review_state": "approved_generated",
        }],
    }
    catalog_errors = _contract_errors("sfx_catalog", catalog)
    plan_errors = _contract_errors("audio_event_plan", plan)
    if catalog_errors:
        raise SfxDeliveryError(f"sfx_catalog contract failed: {catalog_errors[0]}")
    if plan_errors:
        raise SfxDeliveryError(f"audio_event_plan contract failed: {plan_errors[0]}")
    catalog_path = stage / "audio_catalog.json"
    plan_path = stage / "audio_event_plan.json"
    _write_json(catalog_path, catalog)
    _write_json(plan_path, plan)
    return plan_path, catalog_path, stem_path


def _read_json_once(path: Path) -> tuple[dict[str, Any] | None, bytes | None, str | None]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        return None, None, f"{path.name}: unreadable ({exc})"
    try:
        import contract_registry

        value = contract_registry.load_artifact_text(payload.decode("utf-8"))
    except Exception as exc:
        return None, payload, f"{path.name}: strict JSON parse failed ({exc})"
    if not isinstance(value, dict):
        return None, payload, f"{path.name}: root must be an object"
    return value, payload, None


def _snapshot_private_wav(path: Path) -> tuple[bytes, tuple[int, int]]:
    """Read one non-symlink regular file once and bind its filesystem identity."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SfxDeliveryError(f"{path.name}: private evidence is unreadable ({exc})") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SfxDeliveryError(f"{path.name}: private evidence must be a regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    return b"".join(chunks), (metadata.st_dev, metadata.st_ino)


def _window_rms_dbfs(
    samples: list[tuple[float, float]], expected_sample: int,
) -> float:
    half_window = DIALOGUE_PRIORITY_WINDOW_SAMPLES // 2
    sums = [0.0, 0.0]
    for position in range(expected_sample - half_window, expected_sample + half_window):
        frame = samples[position] if 0 <= position < len(samples) else (0.0, 0.0)
        sums[0] += frame[0] * frame[0]
        sums[1] += frame[1] * frame[1]
    rms = max(math.sqrt(value / DIALOGUE_PRIORITY_WINDOW_SAMPLES) for value in sums)
    value = DIALOGUE_PRIORITY_SILENCE_DBFS if rms <= 0.0 else 20.0 * math.log10(rms)
    if not math.isfinite(value):
        raise SfxDeliveryError("dialogue-priority RMS is non-finite")
    return value


def _dialogue_priority_evidence(
    plan_events: list[Any],
    expected_sample_count: int | None,
    dialogue_path: Path,
    ducked_sfx_path: Path,
) -> tuple[
    dict[str, Any] | None,
    list[str],
    DecodedWav | None,
    DecodedWav | None,
]:
    failures: list[str] = []
    if dialogue_path.name != "dialogue_priority_dialogue.wav":
        failures.append("dialogue-priority dialogue evidence has the wrong role path")
    if ducked_sfx_path.name != "dialogue_priority_sfx.wav":
        failures.append("dialogue-priority SFX evidence has the wrong role path")
    try:
        dialogue_bytes, dialogue_identity = _snapshot_private_wav(dialogue_path)
        sfx_bytes, sfx_identity = _snapshot_private_wav(ducked_sfx_path)
        if dialogue_identity == sfx_identity:
            raise SfxDeliveryError("dialogue-priority evidence stems alias the same file")
        dialogue = decode_s24le_wav_bytes(dialogue_bytes, source=str(dialogue_path))
        ducked_sfx = decode_s24le_wav_bytes(sfx_bytes, source=str(ducked_sfx_path))
    except SfxDeliveryError as exc:
        return None, failures + [str(exc)], None, None
    if expected_sample_count is None:
        failures.append("dialogue-priority expected sample count is unavailable")
    else:
        if len(dialogue.samples) != expected_sample_count:
            failures.append("dialogue-priority dialogue sample count does not match plan")
        if len(ducked_sfx.samples) != expected_sample_count:
            failures.append("dialogue-priority SFX sample count does not match plan")

    event_evidence: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for event in plan_events:
        event_id = event.get("id") if isinstance(event, dict) else None
        expected = event.get("expected_transient_sample") if isinstance(event, dict) else None
        if not isinstance(event_id, str) or not event_id or event_id in seen_ids:
            failures.append("dialogue-priority plan event IDs must be unique non-empty strings")
            continue
        seen_ids.add(event_id)
        if type(expected) is not int or expected < 0:
            failures.append(f"dialogue-priority event {event_id} has invalid expected sample")
            continue
        dialogue_db = _window_rms_dbfs(dialogue.samples, expected)
        sfx_db = _window_rms_dbfs(ducked_sfx.samples, expected)
        relative_db = sfx_db - dialogue_db
        if not all(math.isfinite(value) for value in (dialogue_db, sfx_db, relative_db)):
            failures.append(f"dialogue-priority event {event_id} has non-finite measurement")
            continue
        active = dialogue_db > DIALOGUE_PRIORITY_THRESHOLD_DBFS
        passed = not active or sfx_db <= dialogue_db - DIALOGUE_PRIORITY_REQUIRED_REDUCTION_DB
        status = "inactive" if not active else ("pass" if passed else "fail")
        if not passed:
            failures.append(
                f"dialogue-priority event {event_id}: SFX is only "
                f"{-relative_db:.6f} dB below active dialogue"
            )
        event_evidence.append({
            "event_id": event_id,
            "expected_transient_sample": expected,
            "dialogue_rms_dbfs": dialogue_db,
            "sfx_rms_dbfs": sfx_db,
            "sfx_relative_to_dialogue_db": relative_db,
            "active": active,
            "status": status,
        })

    evidence = {
        "authority": DIALOGUE_PRIORITY_AUTHORITY,
        "policy": {
            "sample_rate": SAMPLE_RATE,
            "channels": CHANNELS,
            "sample_width_bytes": SAMPLE_WIDTH,
            "window_samples": DIALOGUE_PRIORITY_WINDOW_SAMPLES,
            "window_alignment": "centered_on_expected_transient_zero_padded",
            "channel_aggregation": "maximum_per_channel_rms",
            "dialogue_active_strictly_above_dbfs": DIALOGUE_PRIORITY_THRESHOLD_DBFS,
            "required_sfx_reduction_db": DIALOGUE_PRIORITY_REQUIRED_REDUCTION_DB,
            "digital_silence_dbfs": DIALOGUE_PRIORITY_SILENCE_DBFS,
        },
        "dialogue_stem": {
            "role": "pre-final-loudnorm_dialogue",
            "file_sha256": sha256_bytes(dialogue_bytes),
            "decoded_pcm_sha256": sha256_bytes(dialogue.pcm),
            "sample_rate": dialogue.sample_rate,
            "channels": dialogue.channels,
            "sample_width_bytes": dialogue.sample_width,
            "sample_count": len(dialogue.samples),
        },
        "sfx_stem": {
            "role": "post-sidechain_pre-amix_sfx",
            "file_sha256": sha256_bytes(sfx_bytes),
            "decoded_pcm_sha256": sha256_bytes(ducked_sfx.pcm),
            "sample_rate": ducked_sfx.sample_rate,
            "channels": ducked_sfx.channels,
            "sample_width_bytes": ducked_sfx.sample_width,
            "sample_count": len(ducked_sfx.samples),
        },
        "event_count": len(event_evidence),
        "active_event_count": sum(item["active"] for item in event_evidence),
        "passed_event_count": sum(item["status"] in {"pass", "inactive"} for item in event_evidence),
        "events": event_evidence,
    }
    if len(event_evidence) != len(plan_events):
        failures.append("dialogue-priority evidence count does not match plan events")
    return evidence, failures, dialogue, ducked_sfx


def verify_delivery(
    plan_path: Path,
    catalog_path: Path,
    stem_path: Path,
    visual_evidence: dict[str, Any],
    expected_timeline_revision: str,
    expected_cut_map_sha256: str,
    candidate_path: Path | None = None,
    dialogue_priority_dialogue_path: Path | None = None,
    dialogue_priority_sfx_path: Path | None = None,
    expected_studio_edits_sha256: str | None = None,
) -> dict[str, Any]:
    """Independently verify staged bytes and return a stable QA report."""
    probe_plan, probe_plan_bytes, probe_plan_error = _read_json_once(Path(plan_path))
    probe_version = probe_plan.get("schema_version") if isinstance(probe_plan, dict) else None
    if type(probe_version) is int and probe_version == 2:
        return _verify_multi_event_delivery(
            Path(plan_path),
            Path(catalog_path),
            Path(stem_path),
            visual_evidence,
            expected_timeline_revision,
            expected_cut_map_sha256,
            candidate_path,
            dialogue_priority_dialogue_path,
            dialogue_priority_sfx_path,
            plan_snapshot=(probe_plan, probe_plan_bytes, probe_plan_error),
            expected_studio_edits_sha256=expected_studio_edits_sha256,
        )
    failures: list[str] = []
    warnings: list[str] = []
    if any(
        path is not None
        for path in (dialogue_priority_dialogue_path, dialogue_priority_sfx_path)
    ):
        failures.append("audio_event_plan v1 cannot use dialogue-priority v2 evidence")
    # Dispatch and verification use this one immutable plan snapshot.  A
    # replacement between a version probe and a second read must not select a
    # verifier for one schema and inspect bytes from another.
    plan, plan_bytes, plan_error = probe_plan, probe_plan_bytes, probe_plan_error
    catalog, catalog_bytes, catalog_error = _read_json_once(Path(catalog_path))
    try:
        stem_bytes = Path(stem_path).read_bytes()
    except OSError as exc:
        stem_bytes = None
        failures.append(f"{Path(stem_path).name}: unreadable ({exc})")
    for error in (plan_error, catalog_error):
        if error:
            failures.append(error)
    if plan is not None:
        failures.extend(f"audio_event_plan: {error}" for error in _contract_errors("audio_event_plan", plan))
    if catalog is not None:
        failures.extend(f"sfx_catalog: {error}" for error in _contract_errors("sfx_catalog", catalog))

    expected_count = len(plan.get("events", [])) if isinstance(plan, dict) and isinstance(plan.get("events"), list) else 0
    expected_sample_count = (
        plan.get("sfx_stem_sample_count")
        if isinstance(plan, dict) and type(plan.get("sfx_stem_sample_count")) is int
        else None
    )
    delivered_events: list[dict[str, Any]] = []
    observed_cues: list[dict[str, Any]] = []
    observed_hashes: dict[str, Any] = {
        "plan_file_sha256": sha256_bytes(plan_bytes) if plan_bytes is not None else None,
        "catalog_file_sha256": sha256_bytes(catalog_bytes) if catalog_bytes is not None else None,
        "stem_file_sha256": sha256_bytes(stem_bytes) if stem_bytes is not None else None,
        "stem_decoded_pcm_sha256": None,
        "catalog_asset_wav_sha256": None,
        "catalog_asset_decoded_pcm_sha256": None,
        "catalog_asset_evidence": {},
        "resolved_motion_plan_hash": None,
    }
    decoded_stem: DecodedWav | None = None
    if stem_bytes is not None:
        try:
            decoded_stem = decode_s24le_wav_bytes(stem_bytes, source=str(stem_path))
            observed_hashes["stem_decoded_pcm_sha256"] = sha256_bytes(decoded_stem.pcm)
        except SfxDeliveryError as exc:
            failures.append(f"sfx_stem: {exc}")

    candidate_output_sha256: str | None = None
    candidate_audio: DecodedWav | None = None
    candidate_sample_count_delta: int | None = None
    candidate_sample_count_ok: bool | None = None
    if candidate_path is not None:
        candidate_path = Path(candidate_path)
        try:
            candidate_bytes = candidate_path.read_bytes()
            candidate_output_sha256 = sha256_bytes(candidate_bytes)
            # Decode the same bytes that produced candidate_output_sha256;
            # never reopen a live pathname after hashing it.
            candidate_audio = _decode_candidate_audio_bytes(
                candidate_bytes, source=str(candidate_path)
            )
            if expected_sample_count is None:
                failures.append("candidate output: expected SFX sample count is unavailable")
                candidate_sample_count_ok = False
            else:
                candidate_sample_count_delta = len(candidate_audio.samples) - expected_sample_count
                candidate_sample_count_ok = (
                    candidate_sample_count_within_tolerance(
                        candidate_sample_count_delta
                    )
                )
                if not candidate_sample_count_ok:
                    failures.append(
                        "candidate output: decoded sample count differs from planned SFX stem "
                        f"by {candidate_sample_count_delta} samples"
                    )
        except (OSError, SfxDeliveryError, subprocess.TimeoutExpired) as exc:
            failures.append(f"candidate output audio evidence: {exc}")

    # Regenerate every approved local asset and compare every catalog field and
    # staged WAV byte hash.  The verifier never trusts catalog claims alone.
    generated_assets: dict[str, dict[str, Any]] = {}
    generated_decoded_assets: dict[str, DecodedWav] = {}
    try:
        with tempfile.TemporaryDirectory(prefix="sfx-verify-") as directory:
            generated_assets, generated_decoded_assets = _generate_starter_assets(Path(directory))
    except Exception as exc:
        failures.append(f"generated starter assets: {exc}")
    if generated_assets:
        assets = catalog.get("assets") if isinstance(catalog, dict) else None
        catalog_assets = assets if isinstance(assets, list) else []
        catalog_by_id = {
            item.get("asset_id"): item
            for item in catalog_assets
            if isinstance(item, dict) and isinstance(item.get("asset_id"), str)
        }
        if len(catalog_assets) != len(STARTER_ASSET_IDS):
            failures.append(
                "sfx_catalog: production delivery requires exactly seven starter assets"
            )
        if set(catalog_by_id) != set(STARTER_ASSET_IDS):
            failures.append("sfx_catalog: starter asset IDs do not match the complete local pack")
        for asset_id in STARTER_ASSET_IDS:
            expected_asset = generated_assets[asset_id]
            catalog_asset = catalog_by_id.get(asset_id)
            if (
                not isinstance(catalog_asset, dict)
                or _canonical_hash(catalog_asset) != _canonical_hash(expected_asset)
            ):
                failures.append(
                    f"sfx_catalog: {asset_id} does not match independently generated metadata"
                )
            asset_path = Path(catalog_path).parent / STARTER_ASSET_FILENAMES[asset_id]
            try:
                staged_bytes = asset_path.read_bytes()
                staged_decoded = decode_s24le_wav_bytes(staged_bytes, source=str(asset_path))
                staged_hash = sha256_bytes(staged_bytes)
                staged_pcm_hash = sha256_bytes(staged_decoded.pcm)
                if staged_hash != expected_asset["wav_sha256"]:
                    failures.append(f"sfx_catalog: {asset_id} staged WAV hash mismatch")
                if staged_pcm_hash != expected_asset["decoded_pcm_sha256"]:
                    failures.append(f"sfx_catalog: {asset_id} staged decoded PCM hash mismatch")
                if len(staged_decoded.samples) != expected_asset["duration_samples"]:
                    failures.append(f"sfx_catalog: {asset_id} staged duration mismatch")
                observed_hashes["catalog_asset_evidence"][asset_id] = {
                    "wav_sha256": staged_hash,
                    "decoded_pcm_sha256": staged_pcm_hash,
                    "duration_samples": len(staged_decoded.samples),
                }
            except (OSError, SfxDeliveryError) as exc:
                failures.append(f"sfx_catalog: {asset_id} staged WAV evidence unavailable ({exc})")
            if asset_id == "soft-ui-tick-v1" and isinstance(catalog_asset, dict):
                observed_hashes["catalog_asset_wav_sha256"] = catalog_asset.get("wav_sha256")
                observed_hashes["catalog_asset_decoded_pcm_sha256"] = catalog_asset.get("decoded_pcm_sha256")

    event: dict[str, Any] | None = None
    if plan is not None and isinstance(plan.get("events"), list) and len(plan["events"]) == 1:
        if isinstance(plan["events"][0], dict):
            event = plan["events"][0]
    if plan is not None:
        if plan.get("timeline_revision") != expected_timeline_revision:
            failures.append("audio_event_plan: stale timeline_revision")
        if plan.get("cut_map_sha256") != expected_cut_map_sha256:
            failures.append("audio_event_plan: stale cut_map_sha256")
        try:
            final_sample_count = _duration_samples_from_evidence(visual_evidence)
            if plan.get("sfx_stem_sample_count") != final_sample_count:
                failures.append("audio_event_plan: SFX stem sample count does not match visual final duration")
        except SfxDeliveryError as exc:
            failures.append(f"renderer evidence duration: {exc}")
        try:
            motion_hash = canonical_motion_plan_hash(visual_evidence)
            observed_hashes["resolved_motion_plan_hash"] = motion_hash
            if plan.get("resolved_motion_plan_hash") != motion_hash:
                failures.append("audio_event_plan: stale resolved_motion_plan_hash")
        except SfxDeliveryError as exc:
            failures.append(f"renderer evidence: {exc}")

    trigger: dict[str, Any] | None = None
    try:
        trigger = canonical_trigger(visual_evidence)
    except SfxDeliveryError as exc:
        failures.append(f"renderer evidence trigger: {exc}")

    if plan is not None and stem_bytes is not None:
        if plan.get("sfx_stem_sha256") != observed_hashes["stem_file_sha256"]:
            failures.append("audio_event_plan: exact SFX stem hash mismatch")
        if plan.get("sfx_stem_decoded_pcm_sha256") != observed_hashes["stem_decoded_pcm_sha256"]:
            failures.append("audio_event_plan: decoded SFX PCM hash mismatch")
        if decoded_stem is not None and plan.get("sfx_stem_sample_count") != len(decoded_stem.samples):
            failures.append("audio_event_plan: SFX stem sample count mismatch")

    if event is not None:
        catalog_asset = None
        if isinstance(catalog, dict) and isinstance(catalog.get("assets"), list):
            catalog_asset = next(
                (
                    item for item in catalog["assets"]
                    if isinstance(item, dict) and item.get("asset_id") == event.get("asset_id")
                ),
                None,
            )
        if isinstance(catalog_asset, dict):
            event_fields = {
                "asset_id": "asset_id",
                "duration_samples": "duration_samples",
                "transient_anchor_sample": "asset_transient_anchor_sample",
            }
            for catalog_field, event_field in event_fields.items():
                if event.get(event_field) != catalog_asset.get(catalog_field):
                    failures.append(f"audio_event_plan: event {event_field} does not match catalog asset")
        current_event_bound = False
        if trigger is not None:
            if event.get("trigger_id") != trigger.get("id") or event.get("trigger_onset_sample") != trigger.get("onset_sample"):
                failures.append("audio_event_plan: current trigger does not match event")
            evidence_trigger = ((event.get("evidence") or {}).get("trigger")
                                if isinstance(event.get("evidence"), dict) else None)
            if evidence_trigger != trigger:
                failures.append("audio_event_plan: evidence trigger does not match current renderer evidence")
            current_event_bound = (
                event.get("trigger_id") == trigger.get("id")
                and event.get("trigger_onset_sample") == trigger.get("onset_sample")
                and evidence_trigger == trigger
            )
        expected = event.get("expected_transient_sample")
        start = event.get("event_start_sample")
        anchor = event.get("asset_transient_anchor_sample")
        if all(type(value) is int for value in (expected, start, anchor)) and expected != start + anchor:
            failures.append("audio_event_plan: expected transient formula mismatch")
        if isinstance(expected, int) and isinstance(event.get("trigger_onset_sample"), int) and not alignment_ok(event["trigger_onset_sample"], expected):
            failures.append("audio_event_plan: expected transient exceeds alignment tolerance")
        duration = event.get("duration_samples")
        if decoded_stem is not None and all(type(value) is int for value in (start, duration)):
            if start < 0 or duration <= 0 or start + duration > len(decoded_stem.samples):
                failures.append("audio_event_plan: event is outside SFX stem bounds")
        generated_event_decoded = generated_decoded_assets.get(event.get("asset_id"))
        if generated_event_decoded is None:
            failures.append("audio_event_plan: event asset is not an approved starter asset")
        if generated_event_decoded is not None and decoded_stem is not None and isinstance(start, int):
            expected_pcm = bytearray(len(decoded_stem.pcm))
            baked_pcm = _bake_s24_pcm(generated_event_decoded.pcm, -12.0)
            byte_start = start * CHANNELS * SAMPLE_WIDTH
            if byte_start < 0 or byte_start + len(baked_pcm) > len(expected_pcm):
                failures.append("sfx_stem: deterministic cue payload is outside stem bounds")
            else:
                expected_pcm[byte_start:byte_start + len(baked_pcm)] = baked_pcm
                if decoded_stem.pcm != bytes(expected_pcm):
                    failures.append("sfx_stem: decoded PCM is not the deterministic -12 dB bake")
        window_peak_dbfs: float | None = None
        if decoded_stem is not None and isinstance(expected, int):
            half_window = SAMPLE_RATE // 8  # centered 250 ms zero-padded window
            peak = max(
                (abs(decoded_stem.samples[index][channel])
                 if 0 <= index < len(decoded_stem.samples) else 0.0)
                for index in range(expected - half_window, expected + half_window)
                for channel in range(CHANNELS)
            )
            window_peak_dbfs = round(dbfs(peak), 6)
            if window_peak_dbfs < -42.0:
                failures.append("sfx_stem: cue peak is below -42 dBFS")
        fades = event.get("fades")
        if isinstance(fades, dict) and all(type(fades.get(name)) is int for name in ("in_samples", "out_samples")) and isinstance(duration, int):
            if fades["in_samples"] + fades["out_samples"] > duration:
                failures.append("audio_event_plan: fades exceed event duration")
        if decoded_stem is not None and isinstance(expected, int):
            try:
                observed = detect_transient(decoded_stem.samples, expected_sample=expected)
            except SfxDeliveryError as exc:
                observed = None
                failures.append(f"sfx_stem cue detector: {exc}")
            trigger_onset = event.get("trigger_onset_sample")
            aligned_to_trigger = (
                current_event_bound
                and
                isinstance(trigger_onset, int)
                and alignment_ok(trigger_onset, observed)
            )
            cue = {
                "id": event.get("id"),
                "event_id": event.get("id"),
                "trigger_onset_sample": trigger_onset,
                "expected_transient_sample": expected,
                "observed_transient_sample": observed,
                "delta_samples": (observed - trigger_onset)
                if observed is not None and isinstance(trigger_onset, int) else None,
                "aligned": aligned_to_trigger,
                "window_peak_dbfs": window_peak_dbfs,
                "status": "pass" if aligned_to_trigger else "fail",
            }
            observed_cues.append(cue)
            if observed is None:
                failures.append("sfx_stem: silent or missing transient")
            elif not aligned_to_trigger:
                failures.append("sfx_stem: detected transient exceeds trigger alignment tolerance")
            else:
                delivered_events.append(cue)

        if candidate_audio is not None and generated_event_decoded is not None:
            output_cue: dict[str, Any] = {
                "event_id": event.get("id"),
                "trigger_onset_sample": event.get("trigger_onset_sample"),
                "expected_transient_sample": expected,
                "observed_transient_sample": None,
                "delta_samples": None,
                "aligned": False,
                "correlation": None,
                "window_peak_dbfs": None,
                "status": "fail",
            }
            start = event.get("event_start_sample")
            duration = event.get("duration_samples")
            if isinstance(start, int) and isinstance(duration, int) and isinstance(expected, int):
                match = _cue_template_correlation(
                    candidate_audio.samples,
                    decoded_stem.samples if decoded_stem is not None else [],
                    start,
                    duration,
                )
                if match is not None:
                    correlation, lag = match
                    observed_output = expected + lag
                    trigger_onset = event.get("trigger_onset_sample")
                    output_cue.update({
                        "observed_transient_sample": observed_output,
                        "delta_samples": (
                            observed_output - trigger_onset
                            if isinstance(trigger_onset, int) else None
                        ),
                        "aligned": (
                            isinstance(trigger_onset, int)
                            and alignment_ok(trigger_onset, observed_output)
                        ),
                        "correlation": round(correlation, 6),
                        "window_peak_dbfs": _candidate_window_peak_dbfs(candidate_audio.samples, expected),
                    })
                    if correlation >= CANDIDATE_CORRELATION_THRESHOLD and output_cue["aligned"]:
                        output_cue["status"] = "pass"
                    else:
                        failures.append("candidate output: deterministic SFX cue correlation/alignment failed")
                else:
                    failures.append("candidate output: deterministic SFX cue correlation unavailable")
            else:
                failures.append("candidate output: event timing is invalid for audio evidence")
            observed_cues.append({**output_cue, "evidence_source": "candidate_output_audio"})
            if (
                output_cue["status"] != "pass"
                or candidate_sample_count_ok is False
            ):
                # A sidecar cue cannot count as delivered when the candidate
                # output itself did not carry its final-domain audio.
                delivered_events = [
                    item for item in delivered_events
                    if item.get("id") != event.get("id")
                ]
        elif candidate_path is not None:
            delivered_events = []

    if failures:
        delivered_events = []
    report = {
        "schema_version": 1,
        "source": "independent_sfx_evidence",
        "status": "fail" if failures else "pass",
        "failures": failures,
        "warnings": warnings,
        "expected_event_count": expected_count,
        "delivered_event_count": len(delivered_events),
        "events": delivered_events,
        "observed_cue_evidence": observed_cues,
        "observed_hash_evidence": observed_hashes,
        "expected_timeline_revision": expected_timeline_revision,
        "expected_cut_map_sha256": expected_cut_map_sha256,
        "expected_studio_edits_sha256": expected_studio_edits_sha256,
        "candidate_output_sha256": candidate_output_sha256,
        "output_audio_evidence": (
            {
                "sample_rate": candidate_audio.sample_rate,
                "channels": candidate_audio.channels,
                "sample_width_bytes": candidate_audio.sample_width,
                "sample_count": len(candidate_audio.samples),
                "expected_sample_count": expected_sample_count,
                "sample_count_delta": candidate_sample_count_delta,
                "sample_count_tolerance_samples": CANDIDATE_SAMPLE_COUNT_TOLERANCE,
                "sample_count_tolerance_trailing_samples": (
                    CANDIDATE_SAMPLE_COUNT_TOLERANCE_TRAILING
                ),
                "decoded_pcm_sha256": sha256_bytes(candidate_audio.pcm),
            }
            if candidate_audio is not None else None
        ),
    }
    return report


def _verify_multi_event_delivery(
    plan_path: Path,
    catalog_path: Path,
    stem_path: Path,
    visual_evidence: dict[str, Any],
    expected_timeline_revision: str,
    expected_cut_map_sha256: str,
    candidate_path: Path | None = None,
    dialogue_priority_dialogue_path: Path | None = None,
    dialogue_priority_sfx_path: Path | None = None,
    plan_snapshot: tuple[dict[str, Any] | None, bytes | None, str | None] | None = None,
    expected_studio_edits_sha256: str | None = None,
) -> dict[str, Any]:
    """Independently verify every v2 event, density decision and mixed PCM."""
    failures: list[str] = []
    warnings: list[str] = []
    if plan_snapshot is None:
        plan, plan_bytes, plan_error = _read_json_once(plan_path)
    else:
        plan, plan_bytes, plan_error = plan_snapshot
    catalog, catalog_bytes, catalog_error = _read_json_once(catalog_path)
    try:
        stem_bytes = stem_path.read_bytes()
    except OSError as exc:
        stem_bytes = None
        failures.append(f"{stem_path.name}: unreadable ({exc})")
    for error in (plan_error, catalog_error):
        if error:
            failures.append(error)
    if plan is not None:
        failures.extend(
            f"audio_event_plan: {error}"
            for error in _contract_errors("audio_event_plan", plan)
        )
    if catalog is not None:
        failures.extend(
            f"sfx_catalog: {error}"
            for error in _contract_errors("sfx_catalog", catalog)
        )

    plan_events = (
        plan.get("events")
        if isinstance(plan, dict) and isinstance(plan.get("events"), list)
        else []
    )
    expected_count = len(plan_events)
    expected_sample_count = (
        plan.get("sfx_stem_sample_count")
        if isinstance(plan, dict) and type(plan.get("sfx_stem_sample_count")) is int
        else None
    )
    observed_hashes: dict[str, Any] = {
        "plan_file_sha256": sha256_bytes(plan_bytes) if plan_bytes is not None else None,
        "catalog_file_sha256": sha256_bytes(catalog_bytes) if catalog_bytes is not None else None,
        "stem_file_sha256": sha256_bytes(stem_bytes) if stem_bytes is not None else None,
        "stem_decoded_pcm_sha256": None,
        "catalog_asset_wav_sha256": None,
        "catalog_asset_decoded_pcm_sha256": None,
        "catalog_asset_evidence": {},
        "resolved_motion_plan_hash": None,
    }
    decoded_stem: DecodedWav | None = None
    if stem_bytes is not None:
        try:
            decoded_stem = decode_s24le_wav_bytes(stem_bytes, source=str(stem_path))
            observed_hashes["stem_decoded_pcm_sha256"] = sha256_bytes(decoded_stem.pcm)
        except SfxDeliveryError as exc:
            failures.append(f"sfx_stem: {exc}")

    candidate_output_sha256: str | None = None
    candidate_audio: DecodedWav | None = None
    candidate_sample_count_delta: int | None = None
    candidate_sample_count_ok: bool | None = None
    if candidate_path is not None:
        candidate_path = Path(candidate_path)
        try:
            candidate_bytes = candidate_path.read_bytes()
            candidate_output_sha256 = sha256_bytes(candidate_bytes)
            candidate_audio = _decode_candidate_audio_bytes(
                candidate_bytes, source=str(candidate_path)
            )
            if expected_sample_count is None:
                failures.append("candidate output: expected SFX sample count is unavailable")
                candidate_sample_count_ok = False
            else:
                candidate_sample_count_delta = len(candidate_audio.samples) - expected_sample_count
                candidate_sample_count_ok = (
                    candidate_sample_count_within_tolerance(
                        candidate_sample_count_delta
                    )
                )
                if not candidate_sample_count_ok:
                    failures.append(
                        "candidate output: decoded sample count differs from planned SFX stem "
                        f"by {candidate_sample_count_delta} samples"
                    )
        except (OSError, SfxDeliveryError, subprocess.TimeoutExpired) as exc:
            failures.append(f"candidate output audio evidence: {exc}")

    generated_assets: dict[str, dict[str, Any]] = {}
    generated_decoded_assets: dict[str, DecodedWav] = {}
    try:
        with tempfile.TemporaryDirectory(prefix="sfx-verify-v2-") as directory:
            generated_assets, generated_decoded_assets = _generate_starter_assets(Path(directory))
    except Exception as exc:
        failures.append(f"generated starter assets: {exc}")
    if generated_assets:
        catalog_assets = catalog.get("assets") if isinstance(catalog, dict) else None
        catalog_assets = catalog_assets if isinstance(catalog_assets, list) else []
        catalog_by_id = {
            item.get("asset_id"): item
            for item in catalog_assets
            if isinstance(item, dict) and isinstance(item.get("asset_id"), str)
        }
        if len(catalog_assets) != len(STARTER_ASSET_IDS):
            failures.append("sfx_catalog: production delivery requires exactly seven starter assets")
        if set(catalog_by_id) != set(STARTER_ASSET_IDS):
            failures.append("sfx_catalog: starter asset IDs do not match the complete local pack")
        for asset_id in STARTER_ASSET_IDS:
            expected_asset = generated_assets[asset_id]
            catalog_asset = catalog_by_id.get(asset_id)
            if (
                not isinstance(catalog_asset, dict)
                or _canonical_hash(catalog_asset) != _canonical_hash(expected_asset)
            ):
                failures.append(
                    f"sfx_catalog: {asset_id} does not match independently generated metadata"
                )
            asset_path = catalog_path.parent / STARTER_ASSET_FILENAMES[asset_id]
            try:
                staged_bytes = asset_path.read_bytes()
                staged_decoded = decode_s24le_wav_bytes(staged_bytes, source=str(asset_path))
                staged_hash = sha256_bytes(staged_bytes)
                staged_pcm_hash = sha256_bytes(staged_decoded.pcm)
                if staged_hash != expected_asset["wav_sha256"]:
                    failures.append(f"sfx_catalog: {asset_id} staged WAV hash mismatch")
                if staged_pcm_hash != expected_asset["decoded_pcm_sha256"]:
                    failures.append(f"sfx_catalog: {asset_id} staged decoded PCM hash mismatch")
                if len(staged_decoded.samples) != expected_asset["duration_samples"]:
                    failures.append(f"sfx_catalog: {asset_id} staged duration mismatch")
                observed_hashes["catalog_asset_evidence"][asset_id] = {
                    "wav_sha256": staged_hash,
                    "decoded_pcm_sha256": staged_pcm_hash,
                    "duration_samples": len(staged_decoded.samples),
                }
            except (OSError, SfxDeliveryError) as exc:
                failures.append(f"sfx_catalog: {asset_id} staged WAV evidence unavailable ({exc})")
            if asset_id == "soft-ui-tick-v1" and isinstance(catalog_asset, dict):
                observed_hashes["catalog_asset_wav_sha256"] = catalog_asset.get("wav_sha256")
                observed_hashes["catalog_asset_decoded_pcm_sha256"] = catalog_asset.get("decoded_pcm_sha256")

    expected_events: list[dict[str, Any]] = []
    expected_density: dict[str, Any] | None = None
    current_total_samples: int | None = None
    try:
        evidence_snapshot = copy.deepcopy(visual_evidence)
        current_total_samples = _duration_samples_from_evidence(evidence_snapshot)
        current_motion_hash = canonical_motion_plan_hash(evidence_snapshot)
        observed_hashes["resolved_motion_plan_hash"] = current_motion_hash
        proposals = plan_role_events(evidence_snapshot)
        current_catalog = {
            "schema_version": 1,
            "sample_rate": SAMPLE_RATE,
            "channels": CHANNELS,
            "sample_width_bytes": SAMPLE_WIDTH,
            "assets": [generated_assets[asset_id] for asset_id in STARTER_ASSET_IDS],
        }
        current_density = apply_density_policy(proposals, current_catalog, current_total_samples)
        current_kept = sorted(
            current_density["kept"],
            key=lambda event: (event["trigger_onset_sample"], event["trigger_id"]),
        )
        for index, resolved_event in enumerate(current_kept, start=1):
            expected_events.append({
                "id": f"sfx-event-{index:04d}",
                "trigger_id": resolved_event["trigger_id"],
                "trigger_onset_sample": resolved_event["trigger_onset_sample"],
                "event_start_sample": resolved_event["event_start_sample"],
                "duration_samples": resolved_event["duration_samples"],
                "asset_id": resolved_event["asset_id"],
                "asset_transient_anchor_sample": resolved_event["asset_transient_anchor_sample"],
                "expected_transient_sample": resolved_event["expected_transient_sample"],
                "role": resolved_event["role"],
                "gain_db": -12,
                "fades": {"in_samples": 0, "out_samples": 0},
                "duck_group": "dialogue_priority",
                "evidence": _normalized_renderer_event_evidence(resolved_event),
                "reason": f"faithful {resolved_event['role']}",
                "review_state": "approved_generated",
            })
        declared_studio = plan.get("studio_edits") if isinstance(plan, dict) else None
        declared_studio_hash = (
            plan.get("studio_edits_sha256") if isinstance(plan, dict) else None
        )
        if expected_studio_edits_sha256 is None:
            if declared_studio is not None or declared_studio_hash is not None:
                failures.append("audio_event_plan: studio edits were not authorized by current state")
        elif not re.fullmatch(r"[0-9a-f]{64}", expected_studio_edits_sha256):
            failures.append("audio_event_plan: expected studio edit hash is invalid")
        elif not isinstance(declared_studio, dict):
            failures.append("audio_event_plan: current state requires missing studio edits")
        elif (
            declared_studio_hash != expected_studio_edits_sha256
            or _canonical_hash(declared_studio)
            != expected_studio_edits_sha256
        ):
            failures.append("audio_event_plan: studio edit hash does not match current state")
        else:
            expected_by_id = {event["id"]: event for event in expected_events}
            studio_events = declared_studio.get("events")
            if not isinstance(studio_events, list):
                failures.append("audio_event_plan: studio edits events are invalid")
            else:
                for edit in studio_events:
                    if not isinstance(edit, dict):
                        failures.append("audio_event_plan: studio edit is invalid")
                        continue
                    base_event = expected_by_id.get(edit.get("id"))
                    if not isinstance(base_event, dict):
                        failures.append("audio_event_plan: studio edit event is not in current base plan")
                        continue
                    if _canonical_hash(base_event) != edit.get("source_event_sha256"):
                        failures.append("audio_event_plan: studio edit source event hash is stale")
                        continue
                    start = edit.get("event_start_sample")
                    gain = edit.get("gain_db")
                    if type(start) is not int or isinstance(gain, bool) or not isinstance(gain, (int, float)):
                        failures.append("audio_event_plan: studio edit values are invalid")
                        continue
                    if (
                        base_event.get("event_start_sample") == start
                        and base_event.get("gain_db") == gain
                    ):
                        failures.append("audio_event_plan: studio edit has no change")
                        continue
                    base_event["event_start_sample"] = start
                    base_event["gain_db"] = gain
                    base_event["expected_transient_sample"] = (
                        start + base_event["asset_transient_anchor_sample"]
                    )
        expected_density = {
            **current_density["policy_evidence"],
            "dropped": [
                _normalized_density_drop(item) for item in current_density["dropped"]
            ],
        }
        if plan is not None:
            if plan.get("timeline_revision") != expected_timeline_revision:
                failures.append("audio_event_plan: stale timeline_revision")
            if plan.get("cut_map_sha256") != expected_cut_map_sha256:
                failures.append("audio_event_plan: stale cut_map_sha256")
            if plan.get("sfx_stem_sample_count") != current_total_samples:
                failures.append("audio_event_plan: SFX stem sample count does not match visual final duration")
            if plan.get("resolved_motion_plan_hash") != current_motion_hash:
                failures.append("audio_event_plan: stale resolved_motion_plan_hash")
            if plan.get("events") != expected_events:
                failures.append("audio_event_plan: events do not match current renderer role/density evidence")
            if plan.get("density") != expected_density:
                failures.append("audio_event_plan: density evidence does not match current renderer role/density evidence")
    except (SfxDeliveryError, KeyError, TypeError, ValueError) as exc:
        failures.append(f"renderer evidence: cannot independently recompute v2 plan ({exc})")

    if plan is not None and stem_bytes is not None:
        if plan.get("sfx_stem_sha256") != observed_hashes["stem_file_sha256"]:
            failures.append("audio_event_plan: exact SFX stem hash mismatch")
        if plan.get("sfx_stem_decoded_pcm_sha256") != observed_hashes["stem_decoded_pcm_sha256"]:
            failures.append("audio_event_plan: decoded SFX PCM hash mismatch")
        if decoded_stem is not None and plan.get("sfx_stem_sample_count") != len(decoded_stem.samples):
            failures.append("audio_event_plan: SFX stem sample count mismatch")

    # Rebuild the mixed PCM from independent decoded assets.  This intentionally
    # duplicates the samplewise summing loop instead of calling the producer's
    # complete-mix helper, so a forged plan/stem cannot make verifier and
    # producer fail green together.
    if decoded_stem is not None and generated_decoded_assets and current_total_samples is not None:
        expected_mix = [0] * (current_total_samples * CHANNELS)
        for event in expected_events:
            decoded_asset = generated_decoded_assets.get(event.get("asset_id"))
            start = event.get("event_start_sample")
            duration = event.get("duration_samples")
            if decoded_asset is None or type(start) is not int or type(duration) is not int:
                failures.append("sfx_stem: independently reconstructed event is invalid")
                continue
            if len(decoded_asset.samples) != duration or start < 0 or start + duration > current_total_samples:
                failures.append("sfx_stem: independently reconstructed event is outside final output")
                continue
            baked_pcm = _bake_s24_pcm(decoded_asset.pcm, float(event["gain_db"]))
            for frame_index in range(duration):
                destination = (start + frame_index) * CHANNELS
                source = frame_index * CHANNELS * SAMPLE_WIDTH
                for channel in range(CHANNELS):
                    offset = source + channel * SAMPLE_WIDTH
                    sample = int.from_bytes(
                        baked_pcm[offset:offset + SAMPLE_WIDTH], "little", signed=True
                    )
                    expected_mix[destination + channel] = max(
                        -8388608,
                        min(8388607, expected_mix[destination + channel] + sample),
                    )
        expected_pcm = bytearray()
        for sample in expected_mix:
            expected_pcm.extend(_pack_s24_integer(sample))
        if len(decoded_stem.samples) != current_total_samples or decoded_stem.pcm != bytes(expected_pcm):
            failures.append("sfx_stem: decoded PCM is not the independent deterministic saturating sum")

    delivered_events: list[dict[str, Any]] = []
    observed_cues: list[dict[str, Any]] = []
    for index, event in enumerate(plan_events):
        if not isinstance(event, dict):
            continue
        expected = event.get("expected_transient_sample")
        trigger_onset = event.get("trigger_onset_sample")
        observed: int | None = None
        if decoded_stem is not None and type(expected) is int:
            try:
                observed = detect_transient(decoded_stem.samples, expected_sample=expected)
            except SfxDeliveryError as exc:
                failures.append(f"sfx_stem cue detector: {exc}")
        window_peak_dbfs: float | None = None
        if decoded_stem is not None and type(expected) is int:
            half_window = SAMPLE_RATE // 8
            peak = max(
                (
                    abs(decoded_stem.samples[position][channel])
                    if 0 <= position < len(decoded_stem.samples) else 0.0
                )
                for position in range(expected - half_window, expected + half_window)
                for channel in range(CHANNELS)
            )
            window_peak_dbfs = round(dbfs(peak), 6)
        current_bound = index < len(expected_events) and event == expected_events[index]
        aligned = (
            current_bound
            and type(trigger_onset) is int
            and alignment_ok(trigger_onset, observed)
        )
        cue = {
            "id": event.get("id"),
            "event_id": event.get("id"),
            "trigger_onset_sample": trigger_onset,
            "expected_transient_sample": expected,
            "observed_transient_sample": observed,
            "delta_samples": (
                observed - trigger_onset
                if observed is not None and type(trigger_onset) is int else None
            ),
            "aligned": aligned,
            "window_peak_dbfs": window_peak_dbfs,
            "status": "pass" if aligned else "fail",
        }
        observed_cues.append(cue)
        if observed is None:
            failures.append(f"sfx_stem: event {event.get('id')} has no observed transient")
        elif not aligned:
            failures.append(f"sfx_stem: event {event.get('id')} binding/alignment failed")
        else:
            delivered_events.append(cue)

    dialogue_priority: dict[str, Any] | None = None
    dialogue_control: DecodedWav | None = None
    post_sidechain_sfx: DecodedWav | None = None
    priority_paths = (dialogue_priority_dialogue_path, dialogue_priority_sfx_path)
    if not all(path is not None for path in priority_paths):
        failures.append("audio_event_plan v2 requires both dialogue-priority evidence stems")
    else:
        assert dialogue_priority_dialogue_path is not None
        assert dialogue_priority_sfx_path is not None
        (
            dialogue_priority,
            priority_failures,
            dialogue_control,
            post_sidechain_sfx,
        ) = _dialogue_priority_evidence(
            plan_events,
            expected_sample_count,
            Path(dialogue_priority_dialogue_path),
            Path(dialogue_priority_sfx_path),
        )
        failures.extend(priority_failures)

    # The final candidate contains the post-sidechain SFX, so correlate against
    # the exact private snapshot decoded above.  The canonical pre-duck stem
    # remains the independent rebuild/hash/transient authority earlier in this
    # verifier; using it here would reject genuine active-dialogue ducking.
    if candidate_audio is not None:
        candidate_cues: list[dict[str, Any]] = []
        pipeline_lag: int | None = None
        dialogue_gain: float | None = None
        if dialogue_control is not None:
            lag_match = _estimate_dialogue_pipeline_lag(
                candidate_audio.samples,
                dialogue_control.samples,
                plan_events,
            )
            if (
                lag_match is not None
                and lag_match[0] >= CANDIDATE_CORRELATION_THRESHOLD
            ):
                pipeline_lag = lag_match[1]
                dialogue_gain = lag_match[2]
            else:
                failures.append(
                    "candidate output: dialogue-controlled pipeline lag unavailable"
                )
        for event in plan_events:
            if not isinstance(event, dict):
                continue
            output_cue: dict[str, Any] = {
                "event_id": event.get("id"),
                "trigger_onset_sample": event.get("trigger_onset_sample"),
                "expected_transient_sample": event.get("expected_transient_sample"),
                "observed_transient_sample": None,
                "delta_samples": None,
                "aligned": False,
                "correlation": None,
                "window_peak_dbfs": None,
                "status": "fail",
                "evidence_source": "candidate_output_audio",
            }
            start = event.get("event_start_sample")
            duration = event.get("duration_samples")
            expected = event.get("expected_transient_sample")
            if (
                dialogue_control is not None
                and post_sidechain_sfx is not None
                and pipeline_lag is not None
                and dialogue_gain is not None
                and type(start) is int
                and type(duration) is int
                and type(expected) is int
            ):
                match = _cue_template_partial_correlation(
                    candidate_audio.samples,
                    post_sidechain_sfx.samples,
                    dialogue_control.samples,
                    start,
                    duration,
                    pipeline_lag_samples=pipeline_lag,
                    dialogue_gain=dialogue_gain,
                )
                if match is not None:
                    correlation, lag, complete = match
                    observed_output = expected + lag
                    trigger_onset = event.get("trigger_onset_sample")
                    output_cue.update({
                        "observed_transient_sample": observed_output,
                        "delta_samples": (
                            observed_output - trigger_onset
                            if type(trigger_onset) is int else None
                        ),
                        "aligned": (
                            type(trigger_onset) is int
                            and alignment_ok(trigger_onset, observed_output)
                        ),
                        "correlation": round(correlation, 6),
                        "window_peak_dbfs": _candidate_window_peak_dbfs(
                            candidate_audio.samples, expected
                        ),
                    })
                    if (
                        correlation >= CANDIDATE_CORRELATION_THRESHOLD
                        and complete
                        and output_cue["aligned"]
                    ):
                        output_cue["status"] = "pass"
                    else:
                        failures.append(
                            "candidate output: deterministic SFX cue correlation/alignment failed"
                        )
                else:
                    failures.append("candidate output: deterministic SFX cue correlation unavailable")
            else:
                failures.append("candidate output: event timing is invalid for audio evidence")
            candidate_cues.append(output_cue)
            observed_cues.append(output_cue)
        if any(item["status"] != "pass" for item in candidate_cues) or candidate_sample_count_ok is False:
            delivered_events = []
    if failures:
        delivered_events = []

    return {
        "schema_version": 2,
        "source": "independent_sfx_evidence",
        "status": "fail" if failures else "pass",
        "failures": failures,
        "warnings": warnings,
        "expected_event_count": expected_count,
        "delivered_event_count": len(delivered_events),
        "events": delivered_events,
        "observed_cue_evidence": observed_cues,
        "observed_hash_evidence": observed_hashes,
        "expected_timeline_revision": expected_timeline_revision,
        "expected_cut_map_sha256": expected_cut_map_sha256,
        "expected_studio_edits_sha256": expected_studio_edits_sha256,
        "candidate_output_sha256": candidate_output_sha256,
        "output_audio_evidence": (
            {
                "sample_rate": candidate_audio.sample_rate,
                "channels": candidate_audio.channels,
                "sample_width_bytes": candidate_audio.sample_width,
                "sample_count": len(candidate_audio.samples),
                "expected_sample_count": expected_sample_count,
                "sample_count_delta": candidate_sample_count_delta,
                "sample_count_tolerance_samples": CANDIDATE_SAMPLE_COUNT_TOLERANCE,
                "sample_count_tolerance_trailing_samples": (
                    CANDIDATE_SAMPLE_COUNT_TOLERANCE_TRAILING
                ),
                "decoded_pcm_sha256": sha256_bytes(candidate_audio.pcm),
            }
            if candidate_audio is not None else None
        ),
        "dialogue_priority_evidence": dialogue_priority,
    }
