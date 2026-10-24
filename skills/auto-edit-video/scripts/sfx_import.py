#!/usr/bin/env python3
"""Import external, licensed SFX source files into the local evidence-backed library.

Normalizes each source (mp3/wav) into a deterministic 48kHz/24-bit/stereo WAV
via ffmpeg, computes provenance hashes and audio metrics with the stdlib
``wave`` module only, and appends a fully-verifiable record to
``assets/sfx/LIBRARY.json``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from sfx_mapping import SFX_CATEGORIES  # noqa: E402

SAMPLE_RATE = 48000
CHANNELS = 2
SAMPLE_WIDTH_BYTES = 3
FULL_SCALE_24 = 2 ** 23

SILENCE_THRESHOLD_DBFS = -60.0
HEAD_PAD_SAMPLES = 240  # 5ms @ 48kHz
TAIL_PAD_SAMPLES = 960  # 20ms @ 48kHz
NOISE_FLOOR_WINDOW_SAMPLES = 2400  # 50ms @ 48kHz
TRANSIENT_RATIO = 0.30

MIN_DURATION_SAMPLES = 1920  # 40ms @ 48kHz
MAX_DURATION_SAMPLES = 96000  # 2000ms @ 48kHz

MIN_RMS_DBFS = -45.0
PEAK_RANGE_DBFS = (-12.0, -1.0)
TARGET_PEAK_DBFS = -3.0

ASSET_ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*-v[0-9]+$")

SCHEMA_VERSION = 1
LIBRARY_ID = "auto-edit-sfx-library"

FORBIDDEN_PATH_FRAGMENTS = ("/private/", "/tmp/", "/Users" "/")
APPROVAL_REQUIRED_FIELDS = ("approver", "authority", "approved_at", "basis", "evidence_file", "evidence_sha256")

KNOWN_LICENSES = {
    "pixabay-content-license": {
        "name": "Pixabay Content License",
        "source_url": "https://pixabay.com/service/license-summary/",
        "commercial_ok": True,
        "attribution_required": False,
    },
}

BITEXACT_FLAGS = ["-fflags", "+bitexact", "-flags:a", "+bitexact", "-map_metadata", "-1"]


class SfxImportError(Exception):
    pass


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def record_hash(record: dict[str, Any]) -> str:
    stripped = {k: v for k, v in record.items() if k != "record_sha256"}
    return sha256_of_bytes(canonical_json(stripped))


_FFMPEG_VERSION_LINE: str | None = None


def ffmpeg_version_line() -> str:
    global _FFMPEG_VERSION_LINE
    if _FFMPEG_VERSION_LINE is None:
        out = subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, check=True, text=True
        ).stdout
        _FFMPEG_VERSION_LINE = out.splitlines()[0].strip()
    return _FFMPEG_VERSION_LINE


def run_ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(
        ["ffmpeg", "-y", "-nostdin", "-loglevel", "error", *args],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise SfxImportError(f"ffmpeg failed: {' '.join(args)}\n{proc.stderr}")


def read_wav_pcm24_stereo(path: Path) -> tuple[bytes, int]:
    import wave

    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != SAMPLE_WIDTH_BYTES:
            raise SfxImportError(f"expected 24-bit PCM, got sampwidth={w.getsampwidth()}")
        if w.getnchannels() != CHANNELS:
            raise SfxImportError(f"expected stereo, got nchannels={w.getnchannels()}")
        if w.getframerate() != SAMPLE_RATE:
            raise SfxImportError(f"expected {SAMPLE_RATE}Hz, got {w.getframerate()}")
        nframes = w.getnframes()
        raw = w.readframes(nframes)
    return raw, nframes


def unpack24_signed(b: bytes) -> int:
    v = b[0] | (b[1] << 8) | (b[2] << 16)
    if v & 0x800000:
        v -= 0x1000000
    return v


def frames_from_raw(raw: bytes, nframes: int) -> list[tuple[int, int]]:
    frame_bytes = SAMPLE_WIDTH_BYTES * CHANNELS
    frames = []
    for i in range(nframes):
        off = i * frame_bytes
        left = unpack24_signed(raw[off : off + 3])
        right = unpack24_signed(raw[off + 3 : off + 6])
        frames.append((left, right))
    return frames


def linear_to_dbfs(amplitude: float) -> float:
    if amplitude <= 0:
        return float("-inf")
    return 20.0 * math.log10(amplitude / FULL_SCALE_24)


def find_active_bounds(
    frames: list[tuple[int, int]], threshold_dbfs: float
) -> tuple[int, int] | None:
    threshold_linear = FULL_SCALE_24 * (10 ** (threshold_dbfs / 20.0))
    first_active = None
    last_active = None
    for idx, (left, right) in enumerate(frames):
        amp = max(abs(left), abs(right))
        if amp > threshold_linear:
            if first_active is None:
                first_active = idx
            last_active = idx
    if first_active is None:
        return None
    return first_active, last_active


def compute_trim_bounds(frames: list[tuple[int, int]]) -> tuple[int, int]:
    bounds = find_active_bounds(frames, SILENCE_THRESHOLD_DBFS)
    if bounds is None:
        raise SfxImportError("no signal above silence threshold; entire clip is silence")
    first_active, last_active = bounds
    total = len(frames)
    start = max(0, first_active - HEAD_PAD_SAMPLES)
    end = min(total, last_active + 1 + TAIL_PAD_SAMPLES)
    return start, end


def compute_metrics(frames: list[tuple[int, int]]) -> dict[str, Any]:
    if not frames:
        raise SfxImportError("cannot compute metrics on empty audio")
    duration_samples = len(frames)

    peak_linear = 0
    sum_sq = 0.0
    count = 0
    for left, right in frames:
        peak_linear = max(peak_linear, abs(left), abs(right))
        sum_sq += float(left) * float(left) + float(right) * float(right)
        count += 2
    rms_linear = math.sqrt(sum_sq / count) if count else 0.0

    peak_dbfs = linear_to_dbfs(peak_linear)
    rms_dbfs = linear_to_dbfs(rms_linear)

    transient_threshold = peak_linear * TRANSIENT_RATIO
    transient_anchor_sample = 0
    for idx, (left, right) in enumerate(frames):
        if max(abs(left), abs(right)) > transient_threshold:
            transient_anchor_sample = idx
            break

    frame_sq = [float(left) * float(left) + float(right) * float(right) for left, right in frames]
    window = min(NOISE_FLOOR_WINDOW_SAMPLES, duration_samples)
    window_sum = sum(frame_sq[:window])
    quietest_sum = window_sum
    for i in range(1, duration_samples - window + 1):
        window_sum += frame_sq[i + window - 1] - frame_sq[i - 1]
        if window_sum < quietest_sum:
            quietest_sum = window_sum
    quietest_rms = math.sqrt(quietest_sum / (window * 2)) if window else 0.0
    noise_floor_dbfs = linear_to_dbfs(quietest_rms if quietest_rms else 0.0)

    return {
        "duration_samples": duration_samples,
        "transient_anchor_sample": transient_anchor_sample,
        "noise_floor_dbfs": round(noise_floor_dbfs, 3),
        "rms_dbfs": round(rms_dbfs, 3),
        "peak_dbfs": round(peak_dbfs, 3),
        "_peak_linear": peak_linear,
    }


def check_duration_range(nframes: int) -> None:
    if not (MIN_DURATION_SAMPLES <= nframes <= MAX_DURATION_SAMPLES):
        raise SfxImportError(
            f"trimmed duration {nframes} samples "
            f"({nframes / SAMPLE_RATE * 1000:.1f}ms) outside "
            f"[{MIN_DURATION_SAMPLES}, {MAX_DURATION_SAMPLES}] samples "
            f"([40, 2000]ms)"
        )


def check_peak_rms_range(metrics: dict[str, Any]) -> None:
    if metrics["rms_dbfs"] <= MIN_RMS_DBFS:
        raise SfxImportError(
            f"rms {metrics['rms_dbfs']}dBFS <= {MIN_RMS_DBFS}dBFS floor"
        )
    lo, hi = PEAK_RANGE_DBFS
    if not (lo <= metrics["peak_dbfs"] <= hi):
        raise SfxImportError(
            f"peak {metrics['peak_dbfs']}dBFS outside [{lo}, {hi}]dBFS"
        )


def normalize_source(source: Path, out_wav: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run the deterministic ffmpeg normalization chain, return (params, metrics)."""
    with tempfile.TemporaryDirectory(prefix="sfx-import-") as tmpdir:
        tmp = Path(tmpdir)
        stage1 = tmp / "stage1_resample.wav"
        stage2 = tmp / "stage2_trimmed.wav"

        stage1_args = [
            "-i", str(source),
            *BITEXACT_FLAGS,
            "-ar", str(SAMPLE_RATE),
            "-ac", str(CHANNELS),
            "-c:a", "pcm_s24le",
            "-f", "wav",
            str(stage1),
        ]
        run_ffmpeg(stage1_args)

        raw, nframes = read_wav_pcm24_stereo(stage1)
        frames = frames_from_raw(raw, nframes)
        trim_start, trim_end = compute_trim_bounds(frames)

        stage2_args = [
            "-i", str(stage1),
            *BITEXACT_FLAGS,
            "-af", f"atrim=start_sample={trim_start}:end_sample={trim_end},asetpts=PTS-STARTPTS",
            "-c:a", "pcm_s24le",
            "-f", "wav",
            str(stage2),
        ]
        run_ffmpeg(stage2_args)

        raw2, nframes2 = read_wav_pcm24_stereo(stage2)
        trimmed_frames = frames_from_raw(raw2, nframes2)

        check_duration_range(nframes2)

        trimmed_metrics = compute_metrics(trimmed_frames)
        measured_peak_dbfs = linear_to_dbfs(trimmed_metrics["_peak_linear"])
        if measured_peak_dbfs == float("-inf"):
            raise SfxImportError("trimmed audio is silent; cannot peak-normalize")
        gain_db = TARGET_PEAK_DBFS - measured_peak_dbfs

        stage3_args = [
            "-i", str(stage2),
            *BITEXACT_FLAGS,
            "-af", f"volume={gain_db:.6f}dB",
            "-c:a", "pcm_s24le",
            "-f", "wav",
            str(out_wav),
        ]
        run_ffmpeg(stage3_args)

    raw_final, nframes_final = read_wav_pcm24_stereo(out_wav)
    final_frames = frames_from_raw(raw_final, nframes_final)
    metrics = compute_metrics(final_frames)
    metrics.pop("_peak_linear", None)
    check_peak_rms_range(metrics)

    params = {
        "sample_rate": SAMPLE_RATE,
        "channels": CHANNELS,
        "bit_depth": SAMPLE_WIDTH_BYTES * 8,
        "silence_threshold_dbfs": SILENCE_THRESHOLD_DBFS,
        "head_pad_samples": HEAD_PAD_SAMPLES,
        "tail_pad_samples": TAIL_PAD_SAMPLES,
        "target_peak_dbfs": TARGET_PEAK_DBFS,
        "ffmpeg_version": ffmpeg_version_line(),
    }
    decoded_pcm_sha256 = sha256_of_bytes(raw_final)
    return params, {**metrics, "decoded_pcm_sha256": decoded_pcm_sha256}


def load_library(library_path: Path) -> dict[str, Any]:
    if not library_path.exists():
        return {"schema_version": SCHEMA_VERSION, "library_id": LIBRARY_ID, "records": []}
    with open(library_path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_library_atomic(library_path: Path, library: dict[str, Any]) -> None:
    library_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(library_path.parent), prefix=".library-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(library, f, indent=2, sort_keys=True, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_name, library_path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def iso8601_from_mtime(path: Path) -> str:
    import datetime

    ts = path.stat().st_mtime
    return (
        datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def iso8601_now() -> str:
    import datetime

    return (
        datetime.datetime.now(tz=datetime.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def is_iso8601(value: Any) -> bool:
    import datetime

    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def check_no_forbidden_path_fragments(record: dict[str, Any]) -> None:
    text = json.dumps(record)
    for fragment in FORBIDDEN_PATH_FRAGMENTS:
        if fragment in text:
            raise SfxImportError(f"record must not contain absolute path fragment {fragment!r}")


def cmd_import(args: argparse.Namespace) -> int:
    if args.category not in SFX_CATEGORIES:
        print(f"error: unknown category {args.category!r}; must be one of {SFX_CATEGORIES}", file=sys.stderr)
        return 1
    if not ASSET_ID_RE.match(args.asset_id):
        print(f"error: asset-id {args.asset_id!r} does not match ^[a-z0-9]+(-[a-z0-9]+)*-v[0-9]+$", file=sys.stderr)
        return 1
    if args.license not in KNOWN_LICENSES:
        print(f"error: unknown license {args.license!r}; must be one of {list(KNOWN_LICENSES)}", file=sys.stderr)
        return 1
    if not args.approver.strip():
        print("error: --approver must not be blank", file=sys.stderr)
        return 1
    if not args.approved_by_authority.strip():
        print("error: --approved-by-authority must not be blank", file=sys.stderr)
        return 1

    source = Path(args.source).resolve()
    if not source.is_file():
        print(f"error: source not found: {source}", file=sys.stderr)
        return 1

    license_evidence = Path(args.license_evidence).resolve()
    if not license_evidence.is_file():
        print(f"error: license evidence not found: {license_evidence}", file=sys.stderr)
        return 1

    source_evidence = Path(args.source_evidence).resolve()
    if not source_evidence.is_file():
        print(f"error: source evidence not found: {source_evidence}", file=sys.stderr)
        return 1

    library_path = Path(args.library).resolve()
    library = load_library(library_path)
    existing_ids = {r["asset_id"] for r in library["records"]}
    if args.asset_id in existing_ids:
        print(f"error: asset_id {args.asset_id!r} already exists in {library_path}", file=sys.stderr)
        return 1

    sfx_root = library_path.parent

    try:
        evidence_file = str(license_evidence.relative_to(sfx_root))
    except ValueError:
        print(f"error: --license-evidence must live inside {sfx_root}", file=sys.stderr)
        return 1

    canonical_source_evidence = sfx_root / "evidence" / f"{args.asset_id}.md"
    canonical_source_evidence.parent.mkdir(parents=True, exist_ok=True)
    if source_evidence != canonical_source_evidence:
        canonical_source_evidence.write_bytes(source_evidence.read_bytes())
    source_evidence_file = str(canonical_source_evidence.relative_to(sfx_root))
    source_evidence_sha256 = sha256_of_file(canonical_source_evidence)

    out_wav = sfx_root / args.category / f"{args.asset_id}.wav"
    out_wav.parent.mkdir(parents=True, exist_ok=True)

    try:
        params, norm = normalize_source(source, out_wav)
    except SfxImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if out_wav.exists():
            out_wav.unlink()
        return 1

    source_sha256 = sha256_of_file(source)
    delivery_sha256 = sha256_of_file(out_wav)
    decoded_pcm_sha256 = norm.pop("decoded_pcm_sha256")
    evidence_sha256 = sha256_of_file(license_evidence)
    license_meta = KNOWN_LICENSES[args.license]

    record: dict[str, Any] = {
        "asset_id": args.asset_id,
        "category": args.category,
        "pack": "external-licensed",
        "source": {
            "page_url": args.source_page,
            "file_url": args.file_url,
            "author": args.author,
            "provider": "pixabay",
            "downloaded_at": iso8601_from_mtime(source),
        },
        "source_sha256": source_sha256,
        "delivery_sha256": delivery_sha256,
        "decoded_pcm_sha256": decoded_pcm_sha256,
        "normalizer": {
            "tool": "ffmpeg",
            "version": ffmpeg_version_line(),
            "params": params,
        },
        "wav": {
            "sample_rate": SAMPLE_RATE,
            "channels": CHANNELS,
            "sample_width_bytes": SAMPLE_WIDTH_BYTES,
            "duration_samples": norm["duration_samples"],
            "transient_anchor_sample": norm["transient_anchor_sample"],
            "noise_floor_dbfs": norm["noise_floor_dbfs"],
            "rms_dbfs": norm["rms_dbfs"],
            "peak_dbfs": norm["peak_dbfs"],
        },
        "license": {
            "name": license_meta["name"],
            "source_url": license_meta["source_url"],
            "commercial_ok": license_meta["commercial_ok"],
            "attribution_required": license_meta["attribution_required"],
            "evidence_file": evidence_file,
            "evidence_sha256": evidence_sha256,
        },
        "approval": {
            "approver": args.approver,
            "authority": args.approved_by_authority,
            "approved_at": iso8601_now(),
            "basis": "license snapshot + source page",
            "evidence_file": evidence_file,
            "evidence_sha256": evidence_sha256,
            "source_evidence_file": source_evidence_file,
            "source_evidence_sha256": source_evidence_sha256,
        },
        "review_state": "approved_licensed",
        "relative_path": f"{args.category}/{args.asset_id}.wav",
    }
    try:
        check_no_forbidden_path_fragments(record)
    except SfxImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if out_wav.exists():
            out_wav.unlink()
        return 1
    record["record_sha256"] = record_hash(record)

    library["records"].append(record)
    library["records"].sort(key=lambda r: r["asset_id"])
    write_library_atomic(library_path, library)

    print(f"imported {args.asset_id} -> {out_wav.relative_to(sfx_root.parent)}")
    print(f"  duration_samples={record['wav']['duration_samples']} peak_dbfs={record['wav']['peak_dbfs']} rms_dbfs={record['wav']['rms_dbfs']}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    library_path = Path(args.library).resolve()
    if not library_path.exists():
        print(f"error: library not found: {library_path}", file=sys.stderr)
        return 1
    library = load_library(library_path)
    sfx_root = library_path.parent

    failures: list[str] = []
    for record in library["records"]:
        asset_id = record.get("asset_id", "<unknown>")

        expected_hash = record.get("record_sha256")
        actual_hash = record_hash(record)
        if actual_hash != expected_hash:
            failures.append(f"{asset_id}: record_sha256 mismatch (expected {expected_hash}, got {actual_hash})")

        wav_path = sfx_root / record["relative_path"]
        if not wav_path.is_file():
            failures.append(f"{asset_id}: delivery file missing: {wav_path}")
            continue

        actual_delivery = sha256_of_file(wav_path)
        if actual_delivery != record["delivery_sha256"]:
            failures.append(
                f"{asset_id}: delivery_sha256 mismatch (expected {record['delivery_sha256']}, got {actual_delivery})"
            )

        raw, _ = read_wav_pcm24_stereo(wav_path)
        actual_pcm = sha256_of_bytes(raw)
        if actual_pcm != record["decoded_pcm_sha256"]:
            failures.append(
                f"{asset_id}: decoded_pcm_sha256 mismatch (expected {record['decoded_pcm_sha256']}, got {actual_pcm})"
            )

        evidence_path = sfx_root / record["license"]["evidence_file"]
        if not evidence_path.is_file():
            failures.append(f"{asset_id}: license evidence missing: {evidence_path}")
        else:
            actual_evidence = sha256_of_file(evidence_path)
            if actual_evidence != record["license"]["evidence_sha256"]:
                failures.append(
                    f"{asset_id}: license evidence_sha256 mismatch "
                    f"(expected {record['license']['evidence_sha256']}, got {actual_evidence})"
                )

        approval = record.get("approval")
        if not isinstance(approval, dict):
            failures.append(f"{asset_id}: approval block missing")
            continue
        for field in APPROVAL_REQUIRED_FIELDS:
            if not isinstance(approval.get(field), str) or not approval.get(field):
                failures.append(f"{asset_id}: approval.{field} must be a non-empty string")
        if isinstance(approval.get("approved_at"), str) and not is_iso8601(approval["approved_at"]):
            failures.append(f"{asset_id}: approval.approved_at is not ISO 8601: {approval['approved_at']!r}")

        approval_evidence_file = approval.get("evidence_file")
        if isinstance(approval_evidence_file, str) and approval_evidence_file:
            approval_evidence_path = sfx_root / approval_evidence_file
            if approval_evidence_path.is_symlink():
                failures.append(f"{asset_id}: approval evidence path must not be a symlink: {approval_evidence_path}")
            elif not approval_evidence_path.is_file():
                failures.append(f"{asset_id}: approval evidence missing: {approval_evidence_path}")
            elif not approval_evidence_path.resolve().is_relative_to(sfx_root.resolve()):
                failures.append(f"{asset_id}: approval evidence path escapes library root: {approval_evidence_path}")
            else:
                actual_approval_evidence = sha256_of_file(approval_evidence_path)
                if actual_approval_evidence != approval.get("evidence_sha256"):
                    failures.append(f"{asset_id}: approval.evidence_sha256 mismatch")

        source_evidence_file = approval.get("source_evidence_file")
        if not isinstance(source_evidence_file, str) or not source_evidence_file:
            failures.append(f"{asset_id}: approval.source_evidence_file must be a non-empty string")
        else:
            source_evidence_path = sfx_root / source_evidence_file
            if source_evidence_path.is_symlink():
                failures.append(f"{asset_id}: source evidence path must not be a symlink: {source_evidence_path}")
            elif not source_evidence_path.is_file():
                failures.append(f"{asset_id}: source evidence missing: {source_evidence_path}")
            elif not source_evidence_path.resolve().is_relative_to(sfx_root.resolve()):
                failures.append(f"{asset_id}: source evidence path escapes library root: {source_evidence_path}")
            else:
                actual_source_evidence = sha256_of_file(source_evidence_path)
                if actual_source_evidence != approval.get("source_evidence_sha256"):
                    failures.append(f"{asset_id}: approval.source_evidence_sha256 mismatch")

    if failures:
        print(f"verify FAILED: {len(failures)} problem(s)")
        for f in failures:
            print(f"  - {f}")
        return 1

    print(f"verify OK: {len(library['records'])} record(s) in {library_path}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    library_path = Path(args.library).resolve()
    library = load_library(library_path)
    records = sorted(library["records"], key=lambda r: (r["category"], r["asset_id"]))
    by_category: dict[str, int] = {}
    for r in records:
        by_category[r["category"]] = by_category.get(r["category"], 0) + 1
        print(
            f"{r['category']:14s} {r['asset_id']:32s} "
            f"dur={r['wav']['duration_samples']:6d}smp "
            f"peak={r['wav']['peak_dbfs']:7.3f}dBFS "
            f"rms={r['wav']['rms_dbfs']:7.3f}dBFS"
        )
    print(f"total: {len(records)} record(s) across {len(by_category)} categor{'y' if len(by_category) == 1 else 'ies'}")
    for cat in sorted(by_category):
        print(f"  {cat}: {by_category[cat]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sfx_import.py")
    sub = parser.add_subparsers(dest="command", required=True)

    p_import = sub.add_parser("import", help="normalize and add an external SFX source to the library")
    p_import.add_argument("--source", required=True)
    p_import.add_argument("--asset-id", required=True, dest="asset_id")
    p_import.add_argument("--category", required=True)
    p_import.add_argument("--source-page", required=True, dest="source_page")
    p_import.add_argument("--file-url", required=True, dest="file_url")
    p_import.add_argument("--author", required=True)
    p_import.add_argument("--license", required=True)
    p_import.add_argument("--license-evidence", required=True, dest="license_evidence")
    p_import.add_argument("--source-evidence", required=True, dest="source_evidence")
    p_import.add_argument("--library", default="assets/sfx/LIBRARY.json")
    p_import.add_argument("--approver", required=True)
    p_import.add_argument("--approved-by-authority", required=True, dest="approved_by_authority")
    p_import.set_defaults(func=cmd_import)

    p_verify = sub.add_parser("verify", help="recompute all hashes and check for tampering")
    p_verify.add_argument("--library", default="assets/sfx/LIBRARY.json")
    p_verify.set_defaults(func=cmd_verify)

    p_list = sub.add_parser("list", help="list all records in the library")
    p_list.add_argument("--library", default="assets/sfx/LIBRARY.json")
    p_list.set_defaults(func=cmd_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
