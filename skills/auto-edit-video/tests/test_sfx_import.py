"""Tests for the external SFX import CLI: normalization, metrics, provenance hashes."""
from __future__ import annotations

import json
import math
import shutil
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import sfx_import as si  # noqa: E402

FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None


def write_pcm16_wav(path: Path, samples: list[tuple[int, int]], sample_rate: int, channels: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        payload = b"".join(struct.pack("<" + "h" * channels, *frame) for frame in samples)
        w.writeframes(payload)


def sine_tone_frames(n: int, amplitude: int, freq_hz: float, sample_rate: int, channels: int = 2) -> list[tuple[int, int]]:
    frames = []
    for i in range(n):
        v = int(amplitude * math.sin(2 * math.pi * freq_hz * i / sample_rate))
        frames.append((v, v) if channels == 2 else (v,))
    return frames


def write_pcm24_wav(path: Path, frames: list[tuple[int, int]], sample_rate: int = 48000) -> None:
    def pack24(v: int) -> bytes:
        if v < 0:
            v += 0x1000000
        return bytes([v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF])

    raw = b"".join(pack24(left) + pack24(right) for left, right in frames)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(3)
        w.setframerate(sample_rate)
        w.writeframes(raw)


def make_full_record(sfx_root: Path, asset_id: str = "unit-test-tone-v1") -> dict:
    category = "click_pop"
    wav_dir = sfx_root / category
    wav_dir.mkdir(parents=True, exist_ok=True)
    wav_path = wav_dir / f"{asset_id}.wav"
    frames = sine_tone_frames(4800, si.FULL_SCALE_24 // 2, 1000.0, 48000)
    write_pcm24_wav(wav_path, frames)

    licenses_dir = sfx_root / "licenses"
    licenses_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = licenses_dir / "test-license.md"
    evidence_path.write_text("test license evidence\n", encoding="utf-8")

    evidence_dir = sfx_root / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    source_evidence_path = evidence_dir / f"{asset_id}.md"
    source_evidence_path.write_text("test source acquisition evidence\n", encoding="utf-8")

    raw, _ = si.read_wav_pcm24_stereo(wav_path)
    metrics = si.compute_metrics(si.frames_from_raw(raw, len(frames)))
    metrics.pop("_peak_linear", None)

    record = {
        "asset_id": asset_id,
        "category": category,
        "pack": "external-licensed",
        "source": {
            "page_url": "https://pixabay.com/sound-effects/example/",
            "file_url": "https://cdn.pixabay.com/audio/example.mp3",
            "author": "unit-test-author",
            "provider": "pixabay",
            "downloaded_at": "2026-09-03T00:00:00Z",
        },
        "source_sha256": "0" * 64,
        "delivery_sha256": si.sha256_of_file(wav_path),
        "decoded_pcm_sha256": si.sha256_of_bytes(raw),
        "normalizer": {
            "tool": "ffmpeg",
            "version": "ffmpeg version test",
            "params": {
                "sample_rate": 48000,
                "channels": 2,
                "bit_depth": 24,
                "silence_threshold_dbfs": si.SILENCE_THRESHOLD_DBFS,
                "head_pad_samples": si.HEAD_PAD_SAMPLES,
                "tail_pad_samples": si.TAIL_PAD_SAMPLES,
                "target_peak_dbfs": si.TARGET_PEAK_DBFS,
                "ffmpeg_version": "ffmpeg version test",
            },
        },
        "wav": {
            "sample_rate": 48000,
            "channels": 2,
            "sample_width_bytes": 3,
            "duration_samples": metrics["duration_samples"],
            "transient_anchor_sample": metrics["transient_anchor_sample"],
            "noise_floor_dbfs": metrics["noise_floor_dbfs"],
            "rms_dbfs": metrics["rms_dbfs"],
            "peak_dbfs": metrics["peak_dbfs"],
        },
        "license": {
            "name": "Pixabay Content License",
            "source_url": "https://pixabay.com/service/license-summary/",
            "commercial_ok": True,
            "attribution_required": False,
            "evidence_file": "licenses/test-license.md",
            "evidence_sha256": si.sha256_of_file(evidence_path),
        },
        "approval": {
            "approver": "claude-code-on-behalf-of-nat",
            "authority": "nat (delegated 2026-09-03)",
            "approved_at": "2026-09-03T00:00:00Z",
            "basis": "license snapshot + source page",
            "evidence_file": "licenses/test-license.md",
            "evidence_sha256": si.sha256_of_file(evidence_path),
            "source_evidence_file": f"evidence/{asset_id}.md",
            "source_evidence_sha256": si.sha256_of_file(source_evidence_path),
        },
        "review_state": "approved_licensed",
        "relative_path": f"{category}/{asset_id}.wav",
    }
    record["record_sha256"] = si.record_hash(record)
    return record


class AssetIdPatternTests(unittest.TestCase):
    def test_valid_ids(self) -> None:
        for candidate in ["pop-v1", "cash-register-kaching-v1", "a-b-c-v12"]:
            self.assertRegex(candidate, si.ASSET_ID_RE)

    def test_invalid_ids(self) -> None:
        for candidate in ["Pop-v1", "pop", "pop-v", "pop_v1", "-pop-v1", "pop-v1-"]:
            self.assertNotRegex(candidate, si.ASSET_ID_RE)


class RecordHashTests(unittest.TestCase):
    def test_recomputable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record = make_full_record(Path(tmp))
            expected = record.pop("record_sha256")
            self.assertEqual(si.record_hash(record), expected)

    def test_changes_with_any_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record = make_full_record(Path(tmp))
            original_hash = record["record_sha256"]
            mutated = dict(record)
            mutated["category"] = "whoosh"
            self.assertNotEqual(si.record_hash(mutated), original_hash)


class SilenceBoundsTests(unittest.TestCase):
    def test_find_active_bounds_basic(self) -> None:
        loud = si.FULL_SCALE_24 // 2
        frames = [(0, 0)] * 100 + [(loud, loud)] * 50 + [(0, 0)] * 100
        bounds = si.find_active_bounds(frames, si.SILENCE_THRESHOLD_DBFS)
        self.assertEqual(bounds, (100, 149))

    def test_find_active_bounds_all_silent(self) -> None:
        frames = [(0, 0)] * 500
        self.assertIsNone(si.find_active_bounds(frames, si.SILENCE_THRESHOLD_DBFS))

    def test_compute_trim_bounds_preserves_pads(self) -> None:
        loud = si.FULL_SCALE_24 // 2
        frames = [(0, 0)] * 1000 + [(loud, loud)] * 200 + [(0, 0)] * 2000
        start, end = si.compute_trim_bounds(frames)
        self.assertEqual(start, 1000 - si.HEAD_PAD_SAMPLES)
        self.assertEqual(end, 1000 + 200 + si.TAIL_PAD_SAMPLES)

    def test_compute_trim_bounds_clamped_to_edges(self) -> None:
        loud = si.FULL_SCALE_24 // 2
        frames = [(loud, loud)] * 10
        start, end = si.compute_trim_bounds(frames)
        self.assertEqual((start, end), (0, 10))

    def test_compute_trim_bounds_rejects_all_silence(self) -> None:
        frames = [(0, 0)] * 500
        with self.assertRaises(si.SfxImportError):
            si.compute_trim_bounds(frames)


class MetricsTests(unittest.TestCase):
    def test_constant_signal_peak_and_rms(self) -> None:
        amp = si.FULL_SCALE_24 // 2
        frames = [(amp, amp)] * 1000
        metrics = si.compute_metrics(frames)
        expected_dbfs = si.linear_to_dbfs(amp)
        self.assertAlmostEqual(metrics["peak_dbfs"], expected_dbfs, places=3)
        self.assertAlmostEqual(metrics["rms_dbfs"], expected_dbfs, places=3)
        self.assertEqual(metrics["transient_anchor_sample"], 0)
        self.assertEqual(metrics["duration_samples"], 1000)

    def test_transient_anchor_detects_first_loud_frame(self) -> None:
        quiet = 10
        loud = si.FULL_SCALE_24 // 2
        frames = [(quiet, quiet)] * 300 + [(loud, loud)] * 10
        metrics = si.compute_metrics(frames)
        self.assertEqual(metrics["transient_anchor_sample"], 300)

    def test_noise_floor_finds_quietest_window(self) -> None:
        loud = si.FULL_SCALE_24 // 2
        quiet = 100
        frames = [(loud, loud)] * 3000 + [(quiet, quiet)] * 3000
        metrics = si.compute_metrics(frames)
        self.assertAlmostEqual(metrics["noise_floor_dbfs"], si.linear_to_dbfs(quiet), places=3)


class RangeGuardTests(unittest.TestCase):
    def test_duration_too_short_rejected(self) -> None:
        with self.assertRaises(si.SfxImportError):
            si.check_duration_range(si.MIN_DURATION_SAMPLES - 1)

    def test_duration_too_long_rejected(self) -> None:
        with self.assertRaises(si.SfxImportError):
            si.check_duration_range(si.MAX_DURATION_SAMPLES + 1)

    def test_duration_in_range_ok(self) -> None:
        si.check_duration_range(si.MIN_DURATION_SAMPLES)
        si.check_duration_range(si.MAX_DURATION_SAMPLES)

    def test_peak_out_of_range_rejected(self) -> None:
        with self.assertRaises(si.SfxImportError):
            si.check_peak_rms_range({"peak_dbfs": -0.2, "rms_dbfs": -10.0})

    def test_rms_too_low_rejected(self) -> None:
        with self.assertRaises(si.SfxImportError):
            si.check_peak_rms_range({"peak_dbfs": -3.0, "rms_dbfs": -50.0})

    def test_within_range_ok(self) -> None:
        si.check_peak_rms_range({"peak_dbfs": -3.0, "rms_dbfs": -20.0})


class VerifyTests(unittest.TestCase):
    def test_verify_ok_on_valid_library(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sfx_root = Path(tmp)
            record = make_full_record(sfx_root)
            library_path = sfx_root / "LIBRARY.json"
            si.write_library_atomic(
                library_path,
                {"schema_version": 1, "library_id": "auto-edit-sfx-library", "records": [record]},
            )
            rc = si.main(["verify", "--library", str(library_path)])
            self.assertEqual(rc, 0)

    def test_verify_detects_tampered_wav_byte(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sfx_root = Path(tmp)
            record = make_full_record(sfx_root)
            library_path = sfx_root / "LIBRARY.json"
            si.write_library_atomic(
                library_path,
                {"schema_version": 1, "library_id": "auto-edit-sfx-library", "records": [record]},
            )
            wav_path = sfx_root / record["relative_path"]
            data = bytearray(wav_path.read_bytes())
            data[-1] ^= 0xFF
            wav_path.write_bytes(bytes(data))
            rc = si.main(["verify", "--library", str(library_path)])
            self.assertNotEqual(rc, 0)

    def test_verify_detects_hand_edited_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sfx_root = Path(tmp)
            record = make_full_record(sfx_root)
            library_path = sfx_root / "LIBRARY.json"
            si.write_library_atomic(
                library_path,
                {"schema_version": 1, "library_id": "auto-edit-sfx-library", "records": [record]},
            )
            library = json.loads(library_path.read_text(encoding="utf-8"))
            library["records"][0]["category"] = "whoosh"
            library_path.write_text(json.dumps(library), encoding="utf-8")
            rc = si.main(["verify", "--library", str(library_path)])
            self.assertNotEqual(rc, 0)


@unittest.skipUnless(FFMPEG_AVAILABLE, "ffmpeg not available")
class EndToEndImportTests(unittest.TestCase):
    def _make_source(self, tmp: Path) -> Path:
        source = tmp / "source.wav"
        silence_head = [(0, 0)] * 2000
        tone = sine_tone_frames(20000, 20000, 1000.0, 44100)
        silence_tail = [(0, 0)] * 3000
        write_pcm16_wav(source, silence_head + tone + silence_tail, 44100, 2)
        return source

    def _run_import(self, tmp: Path, asset_id: str = "e2e-tone-v1") -> tuple[int, Path]:
        source = self._make_source(tmp)
        library = tmp / "assets" / "sfx" / "LIBRARY.json"
        rc = si.main([
            "import",
            "--source", str(source),
            "--asset-id", asset_id,
            "--category", "click_pop",
            "--source-page", "https://pixabay.com/sound-effects/example/",
            "--file-url", "https://cdn.pixabay.com/audio/example.mp3",
            "--author", "unit-test-author",
            "--license", "pixabay-content-license",
            "--license-evidence", str(self._license_evidence(tmp)),
            "--source-evidence", str(self._source_evidence(tmp, asset_id)),
            "--library", str(library),
            "--approver", "unit-test-approver",
            "--approved-by-authority", "unit-test-authority (delegated 2026-09-03)",
        ])
        return rc, library

    def _license_evidence(self, tmp: Path) -> Path:
        evidence = tmp / "assets" / "sfx" / "licenses" / "test-license.md"
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text("test license\n", encoding="utf-8")
        return evidence

    def _source_evidence(self, tmp: Path, asset_id: str) -> Path:
        evidence = tmp / f"{asset_id}-source-evidence.md"
        evidence.write_text("test source acquisition evidence\n", encoding="utf-8")
        return evidence

    def test_import_succeeds_and_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, library = self._run_import(Path(tmp))
            self.assertEqual(rc, 0)
            rc2 = si.main(["verify", "--library", str(library)])
            self.assertEqual(rc2, 0)

    def test_normalization_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
            rc1, library1 = self._run_import(Path(tmp1))
            rc2, library2 = self._run_import(Path(tmp2))
            self.assertEqual(rc1, 0)
            self.assertEqual(rc2, 0)
            wav1 = Path(tmp1) / "assets" / "sfx" / "click_pop" / "e2e-tone-v1.wav"
            wav2 = Path(tmp2) / "assets" / "sfx" / "click_pop" / "e2e-tone-v1.wav"
            self.assertEqual(wav1.read_bytes(), wav2.read_bytes())

    def test_silence_trim_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, library = self._run_import(Path(tmp))
            self.assertEqual(rc, 0)
            data = json.loads(library.read_text(encoding="utf-8"))
            record = data["records"][0]
            self.assertLess(record["wav"]["duration_samples"], 25000)
            self.assertGreater(record["wav"]["duration_samples"], 18000)

    def test_duplicate_asset_id_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            rc1, library = self._run_import(tmp_path)
            self.assertEqual(rc1, 0)
            rc2, _ = self._run_import(tmp_path)
            self.assertNotEqual(rc2, 0)
            data = json.loads(library.read_text(encoding="utf-8"))
            self.assertEqual(len(data["records"]), 1)

    def test_too_short_source_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / "short.wav"
            tone = sine_tone_frames(200, 20000, 1000.0, 44100)
            write_pcm16_wav(source, tone, 44100, 2)
            library = tmp_path / "assets" / "sfx" / "LIBRARY.json"
            rc = si.main([
                "import",
                "--source", str(source),
                "--asset-id", "too-short-v1",
                "--category", "click_pop",
                "--source-page", "https://pixabay.com/sound-effects/example/",
                "--file-url", "https://cdn.pixabay.com/audio/example.mp3",
                "--author", "unit-test-author",
                "--license", "pixabay-content-license",
                "--license-evidence", str(self._license_evidence(tmp_path)),
                "--source-evidence", str(self._source_evidence(tmp_path, "too-short-v1")),
                "--library", str(library),
                "--approver", "unit-test-approver",
                "--approved-by-authority", "unit-test-authority (delegated 2026-09-03)",
            ])
            self.assertNotEqual(rc, 0)
            self.assertFalse(library.exists())


if __name__ == "__main__":
    unittest.main()
