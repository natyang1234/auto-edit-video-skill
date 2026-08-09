#!/usr/bin/env python3
"""Deterministic, local-only Phase 0d SFX delivery primitives.

All final-timeline timing is stored as integer 48kHz samples.  This module
intentionally has no renderer or provider dependency so validation can be
repeated by the delivery and QA paths.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import shutil
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
# A maximum over 1,025 codec-offset trials needs a margin against accidental
# broadband-noise matches.  Real AAC mixes remain near 0.99; deterministic
# white-noise probes observed below 0.15 in this bounded search.
CANDIDATE_CORRELATION_THRESHOLD = 0.30
# AAC framing commonly contributes 512 decoded samples of end padding for a
# 48 kHz stream.  Bind that bounded codec tolerance explicitly to the staged
# final-domain stem duration; all larger count drift fails closed.
CANDIDATE_SAMPLE_COUNT_TOLERANCE = 1024


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

    asset_path = stage / "generated-soft-ui-tick.wav"
    asset = generate_soft_ui_tick(asset_path)
    catalog = {
        "schema_version": 1,
        "sample_rate": SAMPLE_RATE,
        "channels": CHANNELS,
        "sample_width_bytes": SAMPLE_WIDTH,
        "assets": [asset],
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


def verify_delivery(
    plan_path: Path,
    catalog_path: Path,
    stem_path: Path,
    visual_evidence: dict[str, Any],
    expected_timeline_revision: str,
    expected_cut_map_sha256: str,
    candidate_path: Path | None = None,
) -> dict[str, Any]:
    """Independently verify staged bytes and return a stable QA report."""
    failures: list[str] = []
    warnings: list[str] = []
    plan, plan_bytes, plan_error = _read_json_once(Path(plan_path))
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
    candidate_sample_count_within_tolerance: bool | None = None
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
                candidate_sample_count_within_tolerance = False
            else:
                candidate_sample_count_delta = len(candidate_audio.samples) - expected_sample_count
                candidate_sample_count_within_tolerance = (
                    abs(candidate_sample_count_delta) <= CANDIDATE_SAMPLE_COUNT_TOLERANCE
                )
                if not candidate_sample_count_within_tolerance:
                    failures.append(
                        "candidate output: decoded sample count differs from planned SFX stem "
                        f"by {candidate_sample_count_delta} samples"
                    )
        except (OSError, SfxDeliveryError, subprocess.TimeoutExpired) as exc:
            failures.append(f"candidate output audio evidence: {exc}")

    # Regenerate the approved local asset and compare every catalog field.
    generated_asset: dict[str, Any] | None = None
    generated_decoded: DecodedWav | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="sfx-verify-") as directory:
            generated_path = Path(directory) / "soft-ui-tick.wav"
            generated_asset = generate_soft_ui_tick(generated_path)
            generated_decoded = decode_s24le_wav(generated_path)
    except Exception as exc:
        failures.append(f"generated asset: {exc}")
    if catalog is not None and generated_asset is not None:
        assets = catalog.get("assets")
        if not isinstance(assets, list) or len(assets) != 1 or assets[0] != generated_asset:
            failures.append("sfx_catalog: catalog asset does not match deterministic generated asset")
        elif isinstance(assets[0], dict):
            observed_hashes["catalog_asset_wav_sha256"] = assets[0].get("wav_sha256")
            observed_hashes["catalog_asset_decoded_pcm_sha256"] = assets[0].get("decoded_pcm_sha256")

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
        if isinstance(catalog, dict) and isinstance(catalog.get("assets"), list) and len(catalog["assets"]) == 1:
            catalog_asset = catalog["assets"][0]
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
        if generated_decoded is not None and decoded_stem is not None and isinstance(start, int):
            expected_pcm = bytearray(len(decoded_stem.pcm))
            baked_pcm = _bake_s24_pcm(generated_decoded.pcm, -12.0)
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

        if candidate_audio is not None and generated_decoded is not None:
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
                or candidate_sample_count_within_tolerance is False
            ):
                # A sidecar cue cannot count as delivered when the candidate
                # output itself did not carry its final-domain audio.
                delivered_events = [
                    item for item in delivered_events
                    if item.get("id") != event.get("id")
                ]
        elif candidate_path is not None:
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
                "decoded_pcm_sha256": sha256_bytes(candidate_audio.pcm),
            }
            if candidate_audio is not None else None
        ),
    }
    return report
