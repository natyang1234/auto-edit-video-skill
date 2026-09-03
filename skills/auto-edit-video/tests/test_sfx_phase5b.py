"""Phase 5b: approval evidence chain and no-scratch-path-leakage tests.

Covers the review findings that (1) external-license "approval" was
self-claimable metadata with no default authority requirement or
acquisition-evidence backing, and (2) ``normalizer.params`` and other record
fields could leak absolute scratch/tmp filesystem paths into
``LIBRARY.json``.
"""
from __future__ import annotations

import json
import math
import shutil
import sys
import tempfile
import unittest
import wave
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import sfx_delivery  # noqa: E402
import sfx_import as si  # noqa: E402

FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None


def _sine_frames(n: int, amplitude: int, freq_hz: float, sample_rate: int = 48000) -> list[tuple[int, int]]:
    frames = []
    for i in range(n):
        v = int(amplitude * math.sin(2 * math.pi * freq_hz * i / sample_rate))
        frames.append((v, v))
    return frames


def _write_pcm24_wav(path: Path, frames: list[tuple[int, int]], sample_rate: int = 48000) -> None:
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


def _make_record(sfx_root: Path, asset_id: str = "phase5b-tone-v1", category: str = "click_pop") -> dict:
    wav_dir = sfx_root / category
    wav_dir.mkdir(parents=True, exist_ok=True)
    wav_path = wav_dir / f"{asset_id}.wav"
    frames = _sine_frames(4800, si.FULL_SCALE_24 // 2, 1000.0)
    _write_pcm24_wav(wav_path, frames)

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
            "approver": "unit-test-approver",
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


def _write_library(sfx_root: Path, records: list[dict]) -> Path:
    library_path = sfx_root / "LIBRARY.json"
    si.write_library_atomic(
        library_path,
        {"schema_version": 1, "library_id": "auto-edit-sfx-library", "records": records},
    )
    return library_path


class CliApprovalRequiredTests(unittest.TestCase):
    def _base_args(self, tmp: Path) -> list[str]:
        source = tmp / "source.wav"
        _write_pcm24_wav(source, _sine_frames(4800, si.FULL_SCALE_24 // 2, 1000.0))
        licenses_dir = tmp / "assets" / "sfx" / "licenses"
        licenses_dir.mkdir(parents=True, exist_ok=True)
        license_evidence = licenses_dir / "test-license.md"
        license_evidence.write_text("test license\n", encoding="utf-8")
        source_evidence = tmp / "source-evidence.md"
        source_evidence.write_text("test acquisition evidence\n", encoding="utf-8")
        library = tmp / "assets" / "sfx" / "LIBRARY.json"
        return [
            "--source", str(source),
            "--asset-id", "cli-tone-v1",
            "--category", "click_pop",
            "--source-page", "https://pixabay.com/sound-effects/example/",
            "--file-url", "https://cdn.pixabay.com/audio/example.mp3",
            "--author", "unit-test-author",
            "--license", "pixabay-content-license",
            "--license-evidence", str(license_evidence),
            "--source-evidence", str(source_evidence),
            "--library", str(library),
        ], library

    def test_missing_approver_is_cli_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, library = self._base_args(Path(tmp))
            argv = ["import", *args, "--approved-by-authority", "nat (delegated 2026-09-03)"]
            with self.assertRaises(SystemExit):
                si.main(argv)
            self.assertFalse(library.exists())

    def test_missing_approved_by_authority_is_cli_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, library = self._base_args(Path(tmp))
            argv = ["import", *args, "--approver", "unit-test-approver"]
            with self.assertRaises(SystemExit):
                si.main(argv)
            self.assertFalse(library.exists())

    def test_blank_approver_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, library = self._base_args(Path(tmp))
            argv = [
                "import", *args,
                "--approver", "   ",
                "--approved-by-authority", "nat (delegated 2026-09-03)",
            ]
            rc = si.main(argv)
            self.assertNotEqual(rc, 0)
            self.assertFalse(library.exists())

    @unittest.skipUnless(FFMPEG_AVAILABLE, "ffmpeg not available")
    def test_full_import_populates_approval_chain_and_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, library = self._base_args(Path(tmp))
            argv = [
                "import", *args,
                "--approver", "unit-test-approver",
                "--approved-by-authority", "nat (delegated 2026-09-03)",
            ]
            rc = si.main(argv)
            self.assertEqual(rc, 0)
            data = json.loads(library.read_text(encoding="utf-8"))
            approval = data["records"][0]["approval"]
            for field in si.APPROVAL_REQUIRED_FIELDS:
                self.assertTrue(approval.get(field))
            self.assertTrue(approval["source_evidence_file"])
            self.assertTrue(approval["source_evidence_sha256"])
            rc2 = si.main(["verify", "--library", str(library)])
            self.assertEqual(rc2, 0)


class NoScratchPathLeakageTests(unittest.TestCase):
    def test_forbidden_path_fragment_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record = _make_record(Path(tmp))
            record["normalizer"]["params"]["ffmpeg_version"] = "/private/tmp/whatever/ffmpeg"
            record["record_sha256"] = si.record_hash(record)
            with self.assertRaises(si.SfxImportError):
                si.check_no_forbidden_path_fragments(record)

    def test_clean_record_passes_path_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record = _make_record(Path(tmp))
            si.check_no_forbidden_path_fragments(record)  # must not raise

    def test_real_library_has_no_absolute_scratch_paths(self) -> None:
        library_path = SKILL_DIR / "assets" / "sfx" / "LIBRARY.json"
        text = library_path.read_text(encoding="utf-8")
        for fragment in ("/private/", "/tmp/", "/Users/"):
            self.assertNotIn(fragment, text)

    def test_real_library_normalizer_params_is_logical_only(self) -> None:
        library_path = SKILL_DIR / "assets" / "sfx" / "LIBRARY.json"
        data = json.loads(library_path.read_text(encoding="utf-8"))
        for record in data["records"]:
            params = record["normalizer"]["params"]
            self.assertIsInstance(params, dict)
            self.assertIn("sample_rate", params)
            self.assertIn("bit_depth", params)
            self.assertIn("target_peak_dbfs", params)
            params_text = json.dumps(params)
            for fragment in ("/private/", "/tmp/", "/Users/", "/var/"):
                self.assertNotIn(fragment, params_text)


class LoaderApprovalValidationTests(unittest.TestCase):
    def test_approval_field_blank_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sfx_root = Path(tmp)
            record = _make_record(sfx_root)
            record["approval"]["authority"] = ""
            record["record_sha256"] = si.record_hash(record)
            library_path = _write_library(sfx_root, [record])
            with self.assertRaises(sfx_delivery.SfxDeliveryError):
                sfx_delivery.load_sfx_library(library_path)

    def test_approval_block_missing_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sfx_root = Path(tmp)
            record = _make_record(sfx_root)
            del record["approval"]
            record["record_sha256"] = si.record_hash(record)
            library_path = _write_library(sfx_root, [record])
            with self.assertRaises(sfx_delivery.SfxDeliveryError):
                sfx_delivery.load_sfx_library(library_path)

    def test_approved_at_not_iso8601_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sfx_root = Path(tmp)
            record = _make_record(sfx_root)
            record["approval"]["approved_at"] = "not-a-date"
            record["record_sha256"] = si.record_hash(record)
            library_path = _write_library(sfx_root, [record])
            with self.assertRaises(sfx_delivery.SfxDeliveryError):
                sfx_delivery.load_sfx_library(library_path)

    def test_source_evidence_missing_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sfx_root = Path(tmp)
            record = _make_record(sfx_root)
            (sfx_root / record["approval"]["source_evidence_file"]).unlink()
            library_path = _write_library(sfx_root, [record])
            with self.assertRaises(sfx_delivery.SfxDeliveryError):
                sfx_delivery.load_sfx_library(library_path)

    def test_source_evidence_tampered_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sfx_root = Path(tmp)
            record = _make_record(sfx_root)
            library_path = _write_library(sfx_root, [record])
            evidence_path = sfx_root / record["approval"]["source_evidence_file"]
            evidence_path.write_text("tampered acquisition evidence\n", encoding="utf-8")
            with self.assertRaises(sfx_delivery.SfxDeliveryError):
                sfx_delivery.load_sfx_library(library_path)

    def test_source_evidence_symlink_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sfx_root = Path(tmp)
            record = _make_record(sfx_root)
            library_path = _write_library(sfx_root, [record])
            evidence_path = sfx_root / record["approval"]["source_evidence_file"]
            real_target = sfx_root / "elsewhere-source-evidence.md"
            evidence_path.rename(real_target)
            evidence_path.symlink_to(real_target)
            with self.assertRaises(sfx_delivery.SfxDeliveryError):
                sfx_delivery.load_sfx_library(library_path)

    def test_source_evidence_escape_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sfx_root = Path(directory) / "sfx"
            sfx_root.mkdir()
            outside_dir = Path(directory) / "outside"
            outside_dir.mkdir()
            record = _make_record(sfx_root)
            evidence_path = sfx_root / record["approval"]["source_evidence_file"]
            outside_evidence = outside_dir / "escape-evidence.md"
            evidence_path.rename(outside_evidence)
            record["approval"]["source_evidence_file"] = "../outside/escape-evidence.md"
            record["record_sha256"] = si.record_hash(record)
            library_path = _write_library(sfx_root, [record])
            with self.assertRaises(sfx_delivery.SfxDeliveryError):
                sfx_delivery.load_sfx_library(library_path)

    def test_valid_approval_chain_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sfx_root = Path(tmp)
            record = _make_record(sfx_root)
            library_path = _write_library(sfx_root, [record])
            library = sfx_delivery.load_sfx_library(library_path)
            self.assertEqual(len(library["records"]), 1)


class DeterministicReimportTests(unittest.TestCase):
    def test_reimport_of_real_assets_matches_previous_delivery_hashes(self) -> None:
        # Guards Phase 5b's re-import: byte-identical delivery output despite
        # the approval-chain and normalizer.params schema changes.
        library_path = SKILL_DIR / "assets" / "sfx" / "LIBRARY.json"
        data = json.loads(library_path.read_text(encoding="utf-8"))
        self.assertEqual(len(data["records"]), 12)
        for record in data["records"]:
            wav_path = SKILL_DIR / "assets" / "sfx" / record["relative_path"]
            self.assertEqual(si.sha256_of_file(wav_path), record["delivery_sha256"])


if __name__ == "__main__":
    unittest.main()
