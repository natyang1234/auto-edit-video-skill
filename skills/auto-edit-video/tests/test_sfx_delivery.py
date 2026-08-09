"""Phase 0d final-domain SFX primitives."""
from __future__ import annotations

import sys
import tempfile
import unittest
import wave
import json
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import sfx_delivery  # noqa: E402
import qa_video  # noqa: E402


class SfxDeliveryTests(unittest.TestCase):
    @staticmethod
    def visual_evidence(*, reordered: bool = False) -> dict:
        title = {
            "id": "title-1",
            "start": 0.2,
            "end": 1.0,
            "kind": "title",
            "motion": {
                "requested": "pop",
                "delivered": "pop",
                "faithful": True,
                "status": "native",
            },
        }
        stat = {
            "id": "stat-1",
            "start": 1.1,
            "end": 1.5,
            "kind": "stat",
            "motion": {
                "requested": "fade",
                "delivered": "fade",
                "faithful": True,
                "status": "native",
            },
        }
        return {
            "schema_version": 1,
            "duration_s": 2.0,
            "items": [stat, title] if reordered else [title, stat],
        }

    def test_seconds_to_samples_is_decimal_half_up(self) -> None:
        self.assertEqual(sfx_delivery.seconds_to_samples("0.00001041666666666666666666666667"), 1)
        self.assertEqual(sfx_delivery.seconds_to_samples("0.00003125"), 2)

    def test_generated_asset_is_valid_s24le_and_has_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            asset = Path(directory) / "tick.wav"
            catalog = sfx_delivery.generate_soft_ui_tick(asset)
            decoded = sfx_delivery.decode_s24le_wav(asset)
        self.assertEqual(decoded.sample_rate, 48000)
        self.assertEqual(decoded.channels, 2)
        self.assertEqual(decoded.sample_width, 3)
        self.assertEqual(catalog["asset_id"], "soft-ui-tick-v1")
        self.assertGreaterEqual(catalog["transient_anchor_sample"], 0)
        self.assertTrue(sfx_delivery.validate_catalog({
            "schema_version": 1,
            "sample_rate": 48000,
            "channels": 2,
            "sample_width_bytes": 3,
            "assets": [catalog],
        }))

    def test_transient_window_boundary_and_tolerance(self) -> None:
        samples = [(0.0, 0.0)] * 5000
        samples[3840] = (0.5, 0.5)
        self.assertEqual(sfx_delivery.detect_transient(samples, expected_sample=0), 3840)
        self.assertTrue(sfx_delivery.alignment_ok(0, 3840))
        self.assertFalse(sfx_delivery.alignment_ok(0, 3841))

    def test_actual_shifted_stem_3840_passes_and_3841_fails(self) -> None:
        # Detection itself, not only a metadata subtraction, must preserve the
        # single-sample boundary beyond the 80ms alignment allowance.
        for shift, observed_expected, expected in ((3840, 3840, True), (3841, None, False)):
            samples = [(0.0, 0.0)] * (shift + 1300)
            # The detector timestamps a 5ms window at its final sample.
            samples[shift] = (0.5, 0.5)
            observed = sfx_delivery.detect_transient(samples, expected_sample=0)
            self.assertEqual(observed, observed_expected)
            self.assertEqual(sfx_delivery.alignment_ok(0, observed), expected)

    def test_generated_asset_stem_shift_boundary_is_not_absorbed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            asset = root / "asset.wav"
            catalog = sfx_delivery.generate_soft_ui_tick(asset)
            anchor = catalog["transient_anchor_sample"]
            event_start = 1000
            expected = event_start + anchor
            for shift, allowed in ((0, True), (3840, True), (3841, False)):
                stem = root / f"stem-{shift}.wav"
                decoded = sfx_delivery.write_one_cue_stem(
                    stem, total_samples=20000, asset_path=asset,
                    event_start_sample=event_start + shift,
                )
                observed = sfx_delivery.detect_transient(decoded.samples, expected_sample=expected)
                self.assertEqual(sfx_delivery.alignment_ok(expected, observed), allowed)

    def test_full_verifier_keeps_the_3840_3841_trigger_boundary(self) -> None:
        evidence = self.visual_evidence()
        evidence["items"][0]["start"] = 0.5
        revision, cut_hash = "a" * 64, "b" * 64
        for shift, expected_status in ((3840, "pass"), (3841, "fail")):
            with self.subTest(shift=shift), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                plan_path, catalog_path, stem_path = sfx_delivery.stage_one_cue_delivery(
                    root, evidence, revision, cut_hash
                )
                plan = json.loads(plan_path.read_text(encoding="utf-8"))
                event = plan["events"][0]
                event["event_start_sample"] += shift
                event["expected_transient_sample"] += shift
                decoded = sfx_delivery.write_one_cue_stem(
                    stem_path,
                    total_samples=plan["sfx_stem_sample_count"],
                    asset_path=root / "generated-soft-ui-tick.wav",
                    event_start_sample=event["event_start_sample"],
                    gain_db=event["gain_db"],
                )
                plan["sfx_stem_sha256"] = sfx_delivery.sha256_file(stem_path)
                plan["sfx_stem_decoded_pcm_sha256"] = sfx_delivery.sha256_bytes(decoded.pcm)
                plan_path.write_text(
                    json.dumps(plan, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )
                report = sfx_delivery.verify_delivery(
                    plan_path, catalog_path, stem_path, evidence, revision, cut_hash
                )
                self.assertEqual(report["status"], expected_status)
                self.assertEqual(
                    report["observed_cue_evidence"][0]["delta_samples"], shift
                )

    def test_silence_has_no_transient(self) -> None:
        self.assertIsNone(sfx_delivery.detect_transient([(0.0, 0.0)] * 5000, expected_sample=0))

    def test_candidate_decode_uses_one_private_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "candidate.mp4"
            original = b"candidate bytes before replacement"
            candidate.write_bytes(original)
            observed_snapshot: list[bytes] = []

            def inspect_snapshot(snapshot: Path) -> sfx_delivery.DecodedWav:
                observed_snapshot.append(snapshot.read_bytes())
                # Simulate an atomic candidate replacement during decode.  The
                # decoder must still have received the bytes hashed/read first.
                candidate.write_bytes(b"replacement candidate bytes")
                return sfx_delivery.DecodedWav(48000, 2, 4, b"\0" * 8, [(0.0, 0.0)])

            with patch.object(sfx_delivery, "_decode_candidate_audio_path", side_effect=inspect_snapshot):
                decoded = sfx_delivery.decode_candidate_audio(candidate)
            self.assertEqual(observed_snapshot, [original])
            self.assertEqual(len(decoded.samples), 1)

    def test_stage_verify_report_matches_qa_contract_and_bakes_gain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = self.visual_evidence()
            revision, cut_hash = "a" * 64, "b" * 64
            plan_path, catalog_path, stem_path = sfx_delivery.stage_one_cue_delivery(
                root, evidence, revision, cut_hash
            )
            report = sfx_delivery.verify_delivery(
                plan_path, catalog_path, stem_path, evidence, revision, cut_hash
            )
            self.assertEqual(report["status"], "pass")
            self.assertEqual(
                set(("schema_version", "source", "status", "failures", "warnings",
                     "expected_event_count", "delivered_event_count", "events")),
                set(report) & {"schema_version", "source", "status", "failures", "warnings",
                               "expected_event_count", "delivered_event_count", "events"},
            )
            self.assertNotIn("expected_count", report)
            self.assertNotIn("delivered_count", report)
            self.assertEqual(report["expected_event_count"], 1)
            self.assertEqual(report["delivered_event_count"], len(report["events"]))
            # A sidecar-only verifier result is intentionally not a
            # publishable QA receipt; final-domain validation needs the
            # candidate output hash/audio evidence as well.
            with self.assertRaisesRegex(ValueError, "candidate_output_sha256"):
                qa_video.validate_sfx_report(report)
            decoded_asset = sfx_delivery.decode_s24le_wav(root / "generated-soft-ui-tick.wav")
            decoded_stem = sfx_delivery.decode_s24le_wav(stem_path)
            self.assertLess(
                max(abs(value) for frame in decoded_stem.samples for value in frame),
                max(abs(value) for frame in decoded_asset.samples for value in frame),
            )

    def test_verify_rejects_stale_timeline_cut_and_motion_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = self.visual_evidence()
            revision, cut_hash = "a" * 64, "b" * 64
            paths = sfx_delivery.stage_one_cue_delivery(root, evidence, revision, cut_hash)
            for expected_revision, expected_cut, mutated_evidence in (
                ("c" * 64, cut_hash, evidence),
                (revision, "d" * 64, evidence),
                (revision, cut_hash, self.visual_evidence(reordered=True)),
                (revision, cut_hash, {**evidence, "items": []}),
            ):
                with self.subTest(expected_revision=expected_revision, expected_cut=expected_cut):
                    report = sfx_delivery.verify_delivery(
                        *paths, mutated_evidence, expected_revision, expected_cut
                    )
                    self.assertEqual(report["status"], "fail")
                    self.assertTrue(report["failures"])
                    self.assertEqual(report["delivered_event_count"], len(report["events"]))

    def test_verify_rejects_silent_exact_and_pcm_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = self.visual_evidence()
            revision, cut_hash = "a" * 64, "b" * 64
            plan_path, catalog_path, stem_path = sfx_delivery.stage_one_cue_delivery(
                root, evidence, revision, cut_hash
            )
            original = stem_path.read_bytes()
            for mutation in ("silent", "exact", "pcm"):
                stem_path.write_bytes(original)
                if mutation == "silent":
                    with wave.open(str(stem_path), "wb") as wav:
                        wav.setnchannels(2)
                        wav.setsampwidth(3)
                        wav.setframerate(48000)
                        wav.writeframes(b"\0" * (2 * 3 * 48000 * 2))
                elif mutation == "exact":
                    payload = bytearray(original)
                    payload[0] ^= 1
                    stem_path.write_bytes(bytes(payload))
                else:
                    payload = bytearray(original)
                    payload[-100] ^= 1
                    stem_path.write_bytes(bytes(payload))
                report = sfx_delivery.verify_delivery(
                    plan_path, catalog_path, stem_path, evidence, revision, cut_hash
                )
                self.assertEqual(report["status"], "fail", mutation)
                self.assertEqual(report["delivered_event_count"], len(report["events"]), mutation)

    def test_verify_rejects_self_consistent_forged_gain_and_final_duration(self) -> None:
        """Live hashes cannot bless a stem that was not the planned -12 dB bake."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = self.visual_evidence()
            revision, cut_hash = "a" * 64, "b" * 64
            plan_path, catalog_path, stem_path = sfx_delivery.stage_one_cue_delivery(
                root, evidence, revision, cut_hash
            )
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            event = plan["events"][0]
            forged = sfx_delivery.write_one_cue_stem(
                stem_path,
                total_samples=plan["sfx_stem_sample_count"],
                asset_path=root / "generated-soft-ui-tick.wav",
                event_start_sample=event["event_start_sample"],
                gain_db=0.0,
            )
            # Re-sign both public hash claims. Independent QA must reconstruct
            # the deterministic planned PCM instead of trusting either claim.
            plan["sfx_stem_sha256"] = sfx_delivery.sha256_file(stem_path)
            plan["sfx_stem_decoded_pcm_sha256"] = sfx_delivery.sha256_bytes(forged.pcm)
            plan_path.write_text(
                json.dumps(plan, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            forged_report = sfx_delivery.verify_delivery(
                plan_path, catalog_path, stem_path, evidence, revision, cut_hash
            )
            self.assertEqual(forged_report["status"], "fail")
            self.assertTrue(
                any("deterministic -12 dB bake" in item for item in forged_report["failures"])
            )

            duration_report = sfx_delivery.verify_delivery(
                plan_path,
                catalog_path,
                stem_path,
                {**evidence, "duration_s": 2.1},
                revision,
                cut_hash,
            )
            self.assertEqual(duration_report["status"], "fail")
            self.assertTrue(
                any("visual final duration" in item for item in duration_report["failures"])
            )

    def test_strict_schema_rejects_nested_extras_and_semantic_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            asset_path = Path(directory) / "tick.wav"
            asset = sfx_delivery.generate_soft_ui_tick(asset_path)
        catalog = {
            "schema_version": 1,
            "sample_rate": 48000,
            "channels": 2,
            "sample_width_bytes": 3,
            "assets": [asset],
        }
        self.assertTrue(sfx_delivery.validate_catalog(catalog))
        extra = json.loads(json.dumps(catalog))
        extra["assets"][0]["generator"]["extra"] = True
        self.assertFalse(sfx_delivery.validate_catalog(extra))
        boundary = json.loads(json.dumps(catalog))
        boundary["assets"][0]["transient_anchor_sample"] = boundary["assets"][0]["duration_samples"]
        self.assertFalse(sfx_delivery.validate_catalog(boundary))
