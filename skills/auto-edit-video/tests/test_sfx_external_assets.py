"""Phase 3b: external-licensed SFX library load, staging, and verifier tests."""
from __future__ import annotations

import json
import math
import os
import struct
import sys
import tempfile
import unittest
import wave
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import sfx_delivery  # noqa: E402
import sfx_import  # noqa: E402

from test_sfx_delivery_v2 import _multi_event_visual_evidence  # noqa: E402


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


def _make_record(
    sfx_root: Path,
    asset_id: str,
    category: str,
    *,
    freq_hz: float = 1000.0,
    nframes: int = 4800,
) -> dict:
    wav_dir = sfx_root / category
    wav_dir.mkdir(parents=True, exist_ok=True)
    wav_path = wav_dir / f"{asset_id}.wav"
    _write_pcm24_wav(wav_path, _sine_frames(nframes, sfx_import.FULL_SCALE_24 // 2, freq_hz))

    licenses_dir = sfx_root / "licenses"
    licenses_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = licenses_dir / f"{asset_id}-license.md"
    evidence_path.write_text(f"test license evidence for {asset_id}\n", encoding="utf-8")

    raw, _ = sfx_import.read_wav_pcm24_stereo(wav_path)
    metrics = sfx_import.compute_metrics(sfx_import.frames_from_raw(raw, nframes))
    metrics.pop("_peak_linear", None)

    record = {
        "asset_id": asset_id,
        "category": category,
        "pack": "external-licensed",
        "source": {
            "page_url": f"https://pixabay.com/sound-effects/{asset_id}/",
            "file_url": f"https://cdn.pixabay.com/audio/{asset_id}.mp3",
            "author": "unit-test-author",
            "provider": "pixabay",
            "downloaded_at": "2026-09-03T00:00:00Z",
        },
        "source_sha256": "0" * 64,
        "delivery_sha256": sfx_import.sha256_of_file(wav_path),
        "decoded_pcm_sha256": sfx_import.sha256_of_bytes(raw),
        "normalizer": {"tool": "ffmpeg", "version": "ffmpeg version test", "params": ["dummy"]},
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
            "evidence_file": f"licenses/{asset_id}-license.md",
            "evidence_sha256": sfx_import.sha256_of_file(evidence_path),
        },
        "approval": {
            "approver": "claude-code-on-behalf-of-nat",
            "approved_at": "2026-09-03T00:00:00Z",
            "basis": "license snapshot + source page",
        },
        "review_state": "approved_licensed",
        "relative_path": f"{category}/{asset_id}.wav",
    }
    record["record_sha256"] = sfx_import.record_hash(record)
    return record


def _write_library(sfx_root: Path, records: list[dict]) -> Path:
    library_path = sfx_root / "LIBRARY.json"
    library_path.write_text(
        json.dumps(
            {"library_id": "unit-test-sfx-library", "schema_version": 1, "records": records},
            sort_keys=True,
            indent=2,
        ),
        encoding="utf-8",
    )
    return library_path


def _small_library(sfx_root: Path) -> tuple[Path, list[dict]]:
    records = [
        _make_record(sfx_root, "unit-cash-v1", "cash_coin", freq_hz=900.0),
        _make_record(sfx_root, "unit-click-v1", "click_pop", freq_hz=1200.0),
    ]
    return _write_library(sfx_root, records), records


@contextmanager
def _library_env(library_path: Path):
    previous = os.environ.get(sfx_delivery.SFX_LIBRARY_ENV)
    os.environ[sfx_delivery.SFX_LIBRARY_ENV] = str(library_path)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(sfx_delivery.SFX_LIBRARY_ENV, None)
        else:
            os.environ[sfx_delivery.SFX_LIBRARY_ENV] = previous


class LoadSfxLibraryTests(unittest.TestCase):
    def test_library_loads_and_indexes_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sfx_root = Path(directory)
            library_path, records = _small_library(sfx_root)
            library = sfx_delivery.load_sfx_library(library_path)
        self.assertEqual(len(library["records"]), 2)
        self.assertEqual(set(library["by_asset_id"]), {"unit-cash-v1", "unit-click-v1"})
        self.assertEqual(library["by_asset_id"]["unit-cash-v1"]["record_sha256"], records[0]["record_sha256"])

    def test_symlink_library_file_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sfx_root = Path(directory)
            library_path, _ = _small_library(sfx_root)
            symlink_path = sfx_root / "LIBRARY-link.json"
            symlink_path.symlink_to(library_path)
            with self.assertRaises(sfx_delivery.SfxDeliveryError):
                sfx_delivery.load_sfx_library(symlink_path)

    def test_symlink_wav_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sfx_root = Path(directory)
            library_path, records = _small_library(sfx_root)
            wav_path = sfx_root / records[0]["relative_path"]
            real_target = sfx_root / "elsewhere.wav"
            wav_path.rename(real_target)
            wav_path.symlink_to(real_target)
            with self.assertRaises(sfx_delivery.SfxDeliveryError):
                sfx_delivery.load_sfx_library(library_path)

    def test_symlink_evidence_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sfx_root = Path(directory)
            library_path, records = _small_library(sfx_root)
            evidence_path = sfx_root / records[0]["license"]["evidence_file"]
            real_target = sfx_root / "elsewhere-license.md"
            evidence_path.rename(real_target)
            evidence_path.symlink_to(real_target)
            with self.assertRaises(sfx_delivery.SfxDeliveryError):
                sfx_delivery.load_sfx_library(library_path)

    def test_path_escape_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sfx_root = Path(directory) / "sfx"
            sfx_root.mkdir()
            outside_dir = Path(directory) / "outside"
            outside_dir.mkdir()
            record = _make_record(sfx_root, "unit-escape-v1", "cash_coin")
            wav_path = sfx_root / record["relative_path"]
            outside_wav = outside_dir / "escape.wav"
            wav_path.rename(outside_wav)
            record["relative_path"] = "../outside/escape.wav"
            record["record_sha256"] = sfx_import.record_hash(record)
            library_path = _write_library(sfx_root, [record])
            with self.assertRaises(sfx_delivery.SfxDeliveryError):
                sfx_delivery.load_sfx_library(library_path)

    def test_record_tamper_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sfx_root = Path(directory)
            record = _make_record(sfx_root, "unit-tamper-v1", "cash_coin")
            record["category"] = "click_pop"
            library_path = _write_library(sfx_root, [record])
            with self.assertRaises(sfx_delivery.SfxDeliveryError):
                sfx_delivery.load_sfx_library(library_path)

    def test_evidence_tamper_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sfx_root = Path(directory)
            record = _make_record(sfx_root, "unit-evidence-tamper-v1", "cash_coin")
            library_path = _write_library(sfx_root, [record])
            evidence_path = sfx_root / record["license"]["evidence_file"]
            evidence_path.write_text("tampered evidence\n", encoding="utf-8")
            with self.assertRaises(sfx_delivery.SfxDeliveryError):
                sfx_delivery.load_sfx_library(library_path)

    def test_non_approved_review_state_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sfx_root = Path(directory)
            record = _make_record(sfx_root, "unit-pending-v1", "cash_coin")
            record["review_state"] = "pending_review"
            library_path = _write_library(sfx_root, [record])
            with self.assertRaises(sfx_delivery.SfxDeliveryError):
                sfx_delivery.load_sfx_library(library_path)


class StageAndVerifyExternalAssetsTests(unittest.TestCase):
    def test_v2_staging_with_external_assets_passes_verifier(self) -> None:
        evidence = _multi_event_visual_evidence()
        with tempfile.TemporaryDirectory() as sfx_dir, tempfile.TemporaryDirectory() as stage_dir:
            library_path, records = _small_library(Path(sfx_dir))
            with _library_env(library_path):
                plan_path, catalog_path, stem_path = sfx_delivery.stage_multi_event_delivery(
                    Path(stage_dir), evidence, "a" * 64, "b" * 64, catalog_profile="v2"
                )
                catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
                external = [a for a in catalog["assets"] if a.get("pack") == "external-licensed"]
                self.assertEqual(len(external), len(records))
                report = sfx_delivery.verify_delivery(
                    plan_path, catalog_path, stem_path, evidence, "a" * 64, "b" * 64
                )
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["failures"], [])

    def test_staged_wav_byte_tamper_fails_verifier(self) -> None:
        evidence = _multi_event_visual_evidence()
        with tempfile.TemporaryDirectory() as sfx_dir, tempfile.TemporaryDirectory() as stage_dir:
            library_path, records = _small_library(Path(sfx_dir))
            with _library_env(library_path):
                plan_path, catalog_path, stem_path = sfx_delivery.stage_multi_event_delivery(
                    Path(stage_dir), evidence, "a" * 64, "b" * 64, catalog_profile="v2"
                )
                staged_wav = Path(stage_dir) / f"{records[0]['asset_id']}.wav"
                data = bytearray(staged_wav.read_bytes())
                data[-1] ^= 0xFF
                staged_wav.write_bytes(bytes(data))
                report = sfx_delivery.verify_delivery(
                    plan_path, catalog_path, stem_path, evidence, "a" * 64, "b" * 64
                )
            self.assertEqual(report["status"], "fail")
            self.assertTrue(report["failures"])

    def test_catalog_wav_sha256_mismatch_vs_library_fails_verifier(self) -> None:
        evidence = _multi_event_visual_evidence()
        with tempfile.TemporaryDirectory() as sfx_dir, tempfile.TemporaryDirectory() as stage_dir:
            library_path, records = _small_library(Path(sfx_dir))
            with _library_env(library_path):
                plan_path, catalog_path, stem_path = sfx_delivery.stage_multi_event_delivery(
                    Path(stage_dir), evidence, "a" * 64, "b" * 64, catalog_profile="v2"
                )
                catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
                for asset in catalog["assets"]:
                    if asset.get("pack") == "external-licensed":
                        asset["wav_sha256"] = "f" * 64
                        break
                plan = json.loads(plan_path.read_text(encoding="utf-8"))
                plan["catalog_revision_sha256"] = sfx_delivery._canonical_hash(catalog)
                catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
                plan_path.write_text(json.dumps(plan), encoding="utf-8")
                report = sfx_delivery.verify_delivery(
                    plan_path, catalog_path, stem_path, evidence, "a" * 64, "b" * 64
                )
            self.assertEqual(report["status"], "fail")
            self.assertTrue(report["failures"])

    def test_stem_tamper_fails_verifier(self) -> None:
        evidence = _multi_event_visual_evidence()
        with tempfile.TemporaryDirectory() as sfx_dir, tempfile.TemporaryDirectory() as stage_dir:
            library_path, _ = _small_library(Path(sfx_dir))
            with _library_env(library_path):
                plan_path, catalog_path, stem_path = sfx_delivery.stage_multi_event_delivery(
                    Path(stage_dir), evidence, "a" * 64, "b" * 64, catalog_profile="v2"
                )
                data = bytearray(stem_path.read_bytes())
                data[-1] ^= 0xFF
                stem_path.write_bytes(bytes(data))
                report = sfx_delivery.verify_delivery(
                    plan_path, catalog_path, stem_path, evidence, "a" * 64, "b" * 64
                )
            self.assertEqual(report["status"], "fail")
            self.assertTrue(report["failures"])

    def test_include_external_false_still_passes(self) -> None:
        evidence = _multi_event_visual_evidence()
        with tempfile.TemporaryDirectory() as sfx_dir, tempfile.TemporaryDirectory() as stage_dir:
            library_path, _ = _small_library(Path(sfx_dir))
            with _library_env(library_path):
                plan_path, catalog_path, stem_path = sfx_delivery.stage_multi_event_delivery(
                    Path(stage_dir), evidence, "a" * 64, "b" * 64,
                    catalog_profile="v2", include_external=False,
                )
                catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
                self.assertEqual(len(catalog["assets"]), 9)
                report = sfx_delivery.verify_delivery(
                    plan_path, catalog_path, stem_path, evidence, "a" * 64, "b" * 64
                )
            self.assertEqual(report["status"], "pass")


class RealSfxLibraryIntegrationTests(unittest.TestCase):
    def test_real_library_loads_verifies_and_stages_twelve_assets(self) -> None:
        library = sfx_delivery.load_sfx_library()
        self.assertEqual(len(library["records"]), 12)
        categories = Counter(record["category"] for record in library["records"])
        self.assertEqual(dict(categories), {category: 2 for category in sfx_delivery.SFX_CATEGORIES})

        evidence = _multi_event_visual_evidence()
        with tempfile.TemporaryDirectory() as stage_dir:
            plan_path, catalog_path, stem_path = sfx_delivery.stage_multi_event_delivery(
                Path(stage_dir), evidence, "a" * 64, "b" * 64, catalog_profile="v2"
            )
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            external = [a for a in catalog["assets"] if a.get("pack") == "external-licensed"]
            self.assertEqual(len(catalog["assets"]), 21)
            self.assertEqual(len(external), 12)
            report = sfx_delivery.verify_delivery(
                plan_path, catalog_path, stem_path, evidence, "a" * 64, "b" * 64
            )
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["failures"], [])


if __name__ == "__main__":
    unittest.main()
