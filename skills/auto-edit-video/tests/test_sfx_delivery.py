"""Phase 0d final-domain SFX primitives."""
from __future__ import annotations

import sys
import shutil
import subprocess
import tempfile
import unittest
import wave
import json
import math
import struct
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import sfx_delivery  # noqa: E402
import qa_video  # noqa: E402
import contract_registry  # noqa: E402
import editor_server  # noqa: E402


class SfxDeliveryTests(unittest.TestCase):
    @staticmethod
    def write_priority_stems(
        root: Path, total_samples: int, dialogue_db: float, sfx_db: float,
    ) -> tuple[Path, Path]:
        def write(path: Path, level_db: float) -> None:
            amplitude = 10 ** (level_db / 20.0)
            sample = sfx_delivery._pack_s24(amplitude)
            frame = sample + sample
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(2)
                wav.setsampwidth(3)
                wav.setframerate(48000)
                wav.writeframes(frame * total_samples)

        dialogue = root / "dialogue_priority_dialogue.wav"
        sfx = root / "dialogue_priority_sfx.wav"
        write(dialogue, dialogue_db)
        write(sfx, sfx_db)
        return dialogue, sfx

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

    def test_dialogue_priority_window_zero_pads_both_boundaries(self) -> None:
        samples = [(1.0, 0.25)] * 12000
        self.assertAlmostEqual(sfx_delivery._window_rms_dbfs(samples, 6000), 0.0, places=9)
        expected_half_window_db = 20.0 * math.log10(2 ** -0.5)
        self.assertAlmostEqual(
            sfx_delivery._window_rms_dbfs(samples, 0), expected_half_window_db, places=9
        )
        self.assertAlmostEqual(
            sfx_delivery._window_rms_dbfs(samples, len(samples)),
            expected_half_window_db,
            places=9,
        )

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

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"),
        "ffmpeg and ffprobe are required",
    )
    def test_candidate_decode_rejects_nonfinite_float_wav_samples(self) -> None:
        for label, nonfinite in (
            ("nan", float("nan")),
            ("positive-infinity", float("inf")),
            ("negative-infinity", float("-inf")),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                raw_path = root / "candidate.f32le"
                wav_path = root / "candidate.wav"
                frames = [(0.0, 0.0)] * 2048
                frames[1024] = (nonfinite, nonfinite)
                raw_path.write_bytes(b"".join(struct.pack("<ff", *frame) for frame in frames))
                subprocess.run(
                    [
                        "ffmpeg", "-v", "error", "-y",
                        "-f", "f32le", "-ar", "48000", "-ac", "2",
                        "-i", str(raw_path),
                        "-c:a", "pcm_f32le", str(wav_path),
                    ],
                    check=True,
                    capture_output=True,
                )

                with self.assertRaisesRegex(sfx_delivery.SfxDeliveryError, "non-finite"):
                    sfx_delivery.decode_candidate_audio(wav_path)

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"),
        "ffmpeg and ffprobe are required",
    )
    def test_candidate_decode_rejects_one_channel_audio(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "mono.wav"
            with wave.open(str(candidate), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(48000)
                wav.writeframes(struct.pack("<h", 1024) * 2048)
            with self.assertRaisesRegex(
                sfx_delivery.SfxDeliveryError, "native 48kHz stereo"
            ):
                sfx_delivery.decode_candidate_audio(candidate)

    def test_partial_correlation_rejects_nonfinite_inputs(self) -> None:
        output = [
            -0.7312715117751976,
            0.6948674738744653,
            0.0,
            -0.4898619485211566,
            -0.009129825816118098,
            -0.10101787042252375,
        ]
        template = [
            0.3031859454455259,
            0.5774467022710263,
            -0.8122808264515302,
            -0.9433050469559874,
            0.6715302078397394,
            -0.13446586418989326,
        ]
        dialogue = [
            0.524560164915884,
            -0.9957878932977786,
            -0.10922561189039715,
            0.44308006468156513,
            -0.5424755574590947,
            0.8905413911078446,
        ]
        for label, nonfinite in (
            ("nan", float("nan")),
            ("positive-infinity", float("inf")),
            ("negative-infinity", float("-inf")),
        ):
            with self.subTest(label=label):
                poisoned = list(output)
                poisoned[2] = nonfinite
                self.assertIsNone(
                    sfx_delivery._partial_correlation(poisoned, template, dialogue)
                )
        self.assertIsNone(
            sfx_delivery._partial_correlation(
                [1e308, -1e308, 1e308, -1e308],
                [0.25, -0.5, 0.75, -1.0],
                [-0.1, 0.2, -0.3, 0.4],
            )
        )

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

    def test_v2_verify_rejects_noninteger_catalog_const_impostors(self) -> None:
        evidence = self.multi_event_visual_evidence()
        revision, cut_hash = "a" * 64, "b" * 64
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = sfx_delivery.stage_multi_event_delivery(
                root, evidence, revision, cut_hash
            )
            plan = json.loads(paths[0].read_text(encoding="utf-8"))
            dialogue, ducked_sfx = self.write_priority_stems(
                root, plan["sfx_stem_sample_count"], -38.0, -52.0
            )
            original_catalog = paths[1].read_text(encoding="utf-8")

            exact = sfx_delivery.verify_delivery(
                *paths,
                evidence,
                revision,
                cut_hash,
                dialogue_priority_dialogue_path=dialogue,
                dialogue_priority_sfx_path=ducked_sfx,
            )
            self.assertEqual(exact["status"], "pass", exact["failures"])

            for field, impostor in (
                ("schema_version", True),
                ("schema_version", 1.0),
                ("generator.version", True),
                ("generator.version", 1.0),
            ):
                with self.subTest(field=field, impostor=impostor):
                    catalog = json.loads(original_catalog)
                    if field == "schema_version":
                        catalog["schema_version"] = impostor
                    else:
                        catalog["assets"][0]["generator"]["version"] = impostor
                    paths[1].write_text(
                        json.dumps(catalog, sort_keys=True, separators=(",", ":")) + "\n",
                        encoding="utf-8",
                    )
                    report = sfx_delivery.verify_delivery(
                        *paths,
                        evidence,
                        revision,
                        cut_hash,
                        dialogue_priority_dialogue_path=dialogue,
                        dialogue_priority_sfx_path=ducked_sfx,
                    )
                    self.assertEqual(report["status"], "fail", report["failures"])
                    self.assertEqual(report["delivered_event_count"], 0)
                    self.assertTrue(
                        any("expected const 1" in failure for failure in report["failures"]),
                        report["failures"],
                    )
                    if field == "generator.version":
                        self.assertTrue(
                            any(
                                "does not match independently generated metadata" in failure
                                for failure in report["failures"]
                            ),
                            report["failures"],
                        )

    def test_stage_verify_delivers_deterministic_seven_asset_starter_catalog(self) -> None:
        expected_roles = {
            "soft-ui-tick-v1": "title_enter",
            "short-pop-v1": "row_reveal",
            "short-whoosh-v1": "transition",
            "soft-impact-v1": "grid_fill",
            "short-riser-v1": "count_tick",
            "typing-tick-v1": "typing",
            "completion-chime-v1": "complete",
        }
        expected_filenames = {
            "soft-ui-tick-v1": "generated-soft-ui-tick.wav",
            **{
                asset_id: f"generated-{asset_id}.wav"
                for asset_id in expected_roles
                if asset_id != "soft-ui-tick-v1"
            },
        }
        evidence = self.visual_evidence()
        revision, cut_hash = "a" * 64, "b" * 64

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path, catalog_path, stem_path = sfx_delivery.stage_one_cue_delivery(
                root, evidence, revision, cut_hash
            )
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            assets = catalog["assets"]
            self.assertEqual(
                {asset["asset_id"]: asset["role"] for asset in assets},
                expected_roles,
            )
            self.assertEqual(len(assets), len(expected_roles))
            self.assertEqual(len({asset["asset_id"] for asset in assets}), 7)
            for asset in assets:
                asset_path = root / expected_filenames[asset["asset_id"]]
                self.assertTrue(asset_path.is_file(), asset["asset_id"])
                decoded = sfx_delivery.decode_s24le_wav(asset_path)
                self.assertEqual(
                    (decoded.sample_rate, decoded.channels, decoded.sample_width),
                    (48000, 2, 3),
                )
                self.assertEqual(asset["duration_samples"], len(decoded.samples))
                self.assertEqual(asset["wav_sha256"], sfx_delivery.sha256_file(asset_path))
                self.assertEqual(
                    asset["decoded_pcm_sha256"],
                    sfx_delivery.sha256_bytes(decoded.pcm),
                )
                self.assertGreater(asset["rms_dbfs"], -45)
                self.assertGreaterEqual(asset["peak_dbfs"], -12)
                self.assertLessEqual(asset["peak_dbfs"], -1)
                self.assertEqual(
                    asset["provenance"],
                    "original local procedural generation; no external/reference audio",
                )
                self.assertEqual(asset["review_state"], "approved_generated")

            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(len(plan["events"]), 1)
            self.assertEqual(plan["events"][0]["asset_id"], "soft-ui-tick-v1")
            self.assertEqual(plan["events"][0]["role"], "title_enter")
            report = sfx_delivery.verify_delivery(
                plan_path, catalog_path, stem_path, evidence, revision, cut_hash
            )
            self.assertEqual(report["status"], "pass")

            non_cued_path = root / expected_filenames["completion-chime-v1"]
            original_non_cued = non_cued_path.read_bytes()
            mutated_non_cued = bytearray(original_non_cued)
            mutated_non_cued[-1] ^= 1
            non_cued_path.write_bytes(bytes(mutated_non_cued))
            tampered_file_report = sfx_delivery.verify_delivery(
                plan_path, catalog_path, stem_path, evidence, revision, cut_hash
            )
            self.assertEqual(tampered_file_report["status"], "fail")
            self.assertTrue(
                any("completion-chime-v1" in failure for failure in tampered_file_report["failures"])
            )
            non_cued_path.write_bytes(original_non_cued)

            tampered_catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            tampered_catalog["assets"][-1]["decoded_pcm_sha256"] = "0" * 64
            catalog_path.write_text(
                json.dumps(tampered_catalog, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            tampered_catalog_report = sfx_delivery.verify_delivery(
                plan_path, catalog_path, stem_path, evidence, revision, cut_hash
            )
            self.assertEqual(tampered_catalog_report["status"], "fail")
            self.assertTrue(
                any("completion-chime-v1" in failure for failure in tampered_catalog_report["failures"])
            )

        with tempfile.TemporaryDirectory() as first_directory, tempfile.TemporaryDirectory() as second_directory:
            first = sfx_delivery.stage_one_cue_delivery(
                Path(first_directory), evidence, revision, cut_hash
            )
            second = sfx_delivery.stage_one_cue_delivery(
                Path(second_directory), evidence, revision, cut_hash
            )
            for first_path, second_path in zip(first, second):
                self.assertEqual(first_path.read_bytes(), second_path.read_bytes())
            for asset_id in expected_roles:
                self.assertEqual(
                    (Path(first_directory) / expected_filenames[asset_id]).read_bytes(),
                    (Path(second_directory) / expected_filenames[asset_id]).read_bytes(),
                )
            self.assertEqual(
                sfx_delivery.verify_delivery(*first, evidence, revision, cut_hash),
                sfx_delivery.verify_delivery(*second, evidence, revision, cut_hash),
            )

    def test_plan_role_events_uses_only_faithful_renderer_evidence(self) -> None:
        title = {
            "id": "title-enter",
            "start": "0.5",
            "end": "0.9",
            "kind": "title",
            "component_id": "title-lockup",
            "motion": {
                "requested": "slide-up",
                "delivered": "slide-up",
                "faithful": True,
                "status": "native",
            },
        }
        typing = {
            "id": "prompt-typing",
            "start": "0.75",
            "end": "1.1",
            "kind": "title",
            "component_id": "prompt-card",
            "motion": {
                "requested": "word-cascade",
                "delivered": "word-cascade",
                "faithful": True,
                "status": "rendered",
            },
        }
        dynamic_list = {
            "id": "list-reveal",
            "start": "0.00001041666666666666666666666667",
            "end": "0.4",
            "kind": "dynamic_list",
            "component_id": "dynamic-list",
            "motion": {
                "requested": "staggered-reveal",
                "delivered": "staggered-reveal",
                "faithful": True,
                "status": "rendered",
            },
        }
        stat = {
            "id": "stat-count",
            "start": "1.25",
            "end": "1.6",
            "kind": "stat",
            "component_id": "hero-stat",
            "motion": {
                "requested": "count-up",
                "delivered": "count-up",
                "faithful": True,
                "status": "rendered",
            },
        }
        fallback = {
            "id": "fallback-list",
            "start": "0.25",
            "end": "0.6",
            "kind": "dynamic_list",
            "component_id": "dynamic-list",
            "motion": {
                "requested": "staggered-reveal",
                "delivered": "fade",
                "faithful": False,
                "status": "fallback",
            },
        }
        unknown = {
            "id": "unknown-motion",
            "start": "0.1",
            "end": "0.2",
            "kind": "unknown",
            "component_id": "mystery",
            "motion": {
                "requested": "zoom",
                "delivered": "zoom",
                "faithful": True,
                "status": "rendered",
            },
        }
        unknown_title_component = {
            "id": "unknown-title-component",
            "start": "1.5",
            "end": "1.8",
            "kind": "title",
            "component_id": "mystery",
            "motion": {
                "requested": "slide-up",
                "delivered": "slide-up",
                "faithful": True,
                "status": "rendered",
            },
        }
        unsupported_title_motion = {
            "id": "unsupported-title-motion",
            "start": "1.75",
            "end": "2.0",
            "kind": "title",
            "component_id": "title-lockup",
            "motion": {
                "requested": "slideshow-hostile",
                "delivered": "slideshow-hostile",
                "faithful": True,
                "status": "rendered",
            },
        }
        unhashable_component = {
            "id": "unhashable-title-component",
            "start": "1.9",
            "end": "2.0",
            "kind": "title",
            "component_id": [],
            "motion": {
                "requested": "slide-up",
                "delivered": "slide-up",
                "faithful": True,
                "status": "rendered",
            },
        }
        malformed_status = {
            "id": "malformed-motion-status",
            "start": "2.1",
            "end": "2.2",
            "kind": "title",
            "component_id": None,
            "motion": {
                "requested": "slide-up",
                "delivered": "slide-up",
                "faithful": True,
                "status": [],
            },
        }
        evidence = {
            "schema_version": 1,
            "duration_s": 2.0,
            "items": [
                stat,
                unknown,
                typing,
                fallback,
                title,
                dynamic_list,
                unknown_title_component,
                unsupported_title_motion,
                unhashable_component,
                malformed_status,
            ],
        }

        proposals = sfx_delivery.plan_role_events(evidence)

        self.assertEqual(
            [proposal["trigger_id"] for proposal in proposals],
            ["list-reveal", "title-enter", "prompt-typing", "stat-count"],
        )
        self.assertEqual(
            [proposal["trigger_onset_sample"] for proposal in proposals],
            [1, 24000, 36000, 60000],
        )
        self.assertEqual(
            [(proposal["role"], proposal["asset_id"]) for proposal in proposals],
            [
                ("row_reveal", "short-pop-v1"),
                ("title_enter", "soft-ui-tick-v1"),
                ("typing", "typing-tick-v1"),
                ("count_tick", "short-riser-v1"),
            ],
        )
        snapshots = {item["id"]: item for item in evidence["items"]}
        for proposal in proposals:
            self.assertEqual(proposal["evidence"]["trigger"], snapshots[proposal["trigger_id"]])
            self.assertIsNot(proposal["evidence"]["trigger"], snapshots[proposal["trigger_id"]])
        self.assertNotIn("fallback-list", [proposal["trigger_id"] for proposal in proposals])
        self.assertNotIn("unknown-motion", [proposal["trigger_id"] for proposal in proposals])
        self.assertNotIn(
            "unknown-title-component", [proposal["trigger_id"] for proposal in proposals]
        )
        self.assertNotIn(
            "unsupported-title-motion", [proposal["trigger_id"] for proposal in proposals]
        )

        duplicate = json.loads(json.dumps(evidence))
        duplicate["items"].append(json.loads(json.dumps(typing)))
        with self.assertRaisesRegex(sfx_delivery.SfxDeliveryError, "duplicate"):
            sfx_delivery.plan_role_events(duplicate)
        with self.assertRaisesRegex(sfx_delivery.SfxDeliveryError, "eligible"):
            sfx_delivery.plan_role_events({"items": [fallback, unknown]})

        missing_id = json.loads(json.dumps(evidence))
        del missing_id["items"][2]["id"]
        with self.assertRaisesRegex(sfx_delivery.SfxDeliveryError, "id"):
            sfx_delivery.plan_role_events(missing_id)
        negative_start = json.loads(json.dumps(evidence))
        negative_start["items"][2]["start"] = "-0.1"
        with self.assertRaisesRegex(sfx_delivery.SfxDeliveryError, "start"):
            sfx_delivery.plan_role_events(negative_start)

    def test_section_title_uses_the_transition_role_only(self) -> None:
        section = {
            "id": "section-delivery",
            "start": "1.5",
            "end": "4.5",
            "kind": "title",
            "component_id": "kinetic-title",
            "title_kind": "section",
            "evidence_id": "evidence-5ec75ec7",
            "source_literal": "接下來談交付流程",
            "motion": {
                "requested": "slide-up",
                "delivered": "slide-up",
                "faithful": True,
                "status": "native",
            },
        }

        proposals = sfx_delivery.plan_role_events({"items": [section]})
        self.assertEqual(
            [(item["role"], item["asset_id"]) for item in proposals],
            [("transition", "short-whoosh-v1")],
        )

        opening = json.loads(json.dumps(section))
        opening["title_kind"] = "full-screen-hook"
        self.assertEqual(
            sfx_delivery.plan_role_events({"items": [opening]})[0]["role"],
            "title_enter",
        )
        wrong_component = json.loads(json.dumps(section))
        wrong_component["component_id"] = "prompt-card"
        with self.assertRaisesRegex(sfx_delivery.SfxDeliveryError, "eligible"):
            sfx_delivery.plan_role_events({"items": [wrong_component]})

    def test_grid_progress_completion_uses_the_completion_chime_only(self) -> None:
        progress = {
            "id": "grid-progress-delivery",
            "start": "2.5",
            "end": "7.5",
            "kind": "stat",
            "component_id": "progress",
            "family": "grid_progress",
            "trigger_role": "grid_complete",
            "motion": {
                "requested": "fill",
                "delivered": "fill",
                "faithful": True,
                "status": "rendered",
            },
        }

        proposals = sfx_delivery.plan_role_events({"items": [progress]})
        self.assertEqual(
            [(item["role"], item["asset_id"]) for item in proposals],
            [("complete", "completion-chime-v1")],
        )

        generic = json.loads(json.dumps(progress))
        generic.pop("family")
        generic.pop("trigger_role")
        self.assertEqual(
            [(item["role"], item["asset_id"]) for item in sfx_delivery.plan_role_events({"items": [generic]})],
            [("count_tick", "short-riser-v1")],
        )

    def test_asset_mosaic_pan_uses_the_transition_whoosh(self) -> None:
        mosaic = {
            "id": "asset-mosaic-delivery",
            "start": "3.0",
            "end": "8.0",
            "kind": "mosaic",
            "component_id": "asset-mosaic",
            "family": "asset_mosaic",
            "trigger_role": "scene_transition",
            "motion": {
                "requested": "pan",
                "delivered": "pan",
                "faithful": True,
                "status": "native",
            },
        }
        self.assertEqual(
            [
                (item["role"], item["asset_id"])
                for item in sfx_delivery.plan_role_events({"items": [mosaic]})
            ],
            [("transition", "short-whoosh-v1")],
        )

        unbound = json.loads(json.dumps(mosaic))
        unbound.pop("family")
        unbound.pop("trigger_role")
        with self.assertRaisesRegex(sfx_delivery.SfxDeliveryError, "eligible"):
            sfx_delivery.plan_role_events({"items": [unbound]})

    def test_apply_density_policy_is_deterministic_and_fails_closed(self) -> None:
        evidence = self.visual_evidence()
        evidence["duration_s"] = 15.0
        revision, cut_hash = "a" * 64, "b" * 64

        def proposal(trigger_id: str, onset_seconds: str, role: str, asset_id: str) -> dict:
            renderer_shape = {
                "title_enter": ("title", "title-lockup", "slide-up", "native"),
                "typing": ("title", "prompt-card", "word-cascade", "rendered"),
                "row_reveal": (
                    "dynamic_list",
                    "dynamic-list",
                    "staggered-reveal",
                    "rendered",
                ),
                "count_tick": ("stat", "hero-stat", "count-up", "rendered"),
                "grid_fill": ("chart", "dashboard", "pan", "native"),
            }
            kind, component_id, motion_name, status = renderer_shape[role]
            item = {
                "id": trigger_id,
                "start": onset_seconds,
                "kind": kind,
                "component_id": component_id,
                "motion": {
                    "requested": motion_name,
                    "delivered": motion_name,
                    "faithful": True,
                    "status": status,
                },
            }
            planned = sfx_delivery.plan_role_events({"items": [item]})
            self.assertEqual(len(planned), 1)
            self.assertEqual(planned[0]["role"], role)
            self.assertEqual(planned[0]["asset_id"], asset_id)
            return planned[0]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, catalog_path, _ = sfx_delivery.stage_one_cue_delivery(
                root, evidence, revision, cut_hash
            )
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            total_samples = sfx_delivery.seconds_to_samples("15")

            forged_proposal = {
                "trigger_id": "forged-renderer-trigger",
                "trigger_onset_sample": sfx_delivery.seconds_to_samples("1.0"),
                "role": "typing",
                "asset_id": "typing-tick-v1",
                "evidence": {
                    "trigger": {
                        "id": "forged-renderer-trigger",
                        "onset_sample": sfx_delivery.seconds_to_samples("1.0"),
                    }
                },
            }
            with self.subTest(boundary="forged_renderer_evidence"), self.assertRaises(
                sfx_delivery.SfxDeliveryError
            ):
                sfx_delivery.apply_density_policy(
                    [forged_proposal], catalog, total_samples
                )

            with self.subTest(boundary="priority_replaces_nearby_typing"):
                priority_proposals = [
                    proposal("typing-100", "1.00", "typing", "typing-tick-v1"),
                    proposal("title-105", "1.05", "title_enter", "soft-ui-tick-v1"),
                ]
                result = sfx_delivery.apply_density_policy(
                    priority_proposals,
                    catalog,
                    total_samples,
                )
                self.assertEqual(
                    [event["trigger_id"] for event in result["kept"]], ["title-105"]
                )
                self.assertEqual(result["dropped"][0]["reason"], "adjacent_onset")
                self.assertEqual(result["dropped"][0]["trigger_id"], "typing-100")
                priority_proposals[1]["evidence"]["trigger"]["id"] = "caller-mutated"
                self.assertEqual(
                    result["kept"][0]["evidence"]["trigger"]["id"], "title-105"
                )

            with self.subTest(boundary="adjacency_uses_expected_transient"):
                boundary_result = sfx_delivery.apply_density_policy(
                    [
                        proposal("title-zero", "0", "title_enter", "soft-ui-tick-v1"),
                        proposal("typing-5760", "0.12", "typing", "typing-tick-v1"),
                    ],
                    catalog,
                    total_samples,
                )
                self.assertEqual(
                    [event["trigger_id"] for event in boundary_result["kept"]],
                    ["title-zero"],
                )
                self.assertEqual(
                    boundary_result["dropped"][0]["trigger_id"], "typing-5760"
                )
                self.assertEqual(
                    boundary_result["dropped"][0]["reason"], "adjacent_onset"
                )
                self.assertEqual(
                    boundary_result["kept"][0]["expected_transient_sample"], 240
                )
                self.assertEqual(
                    boundary_result["dropped"][0]["expected_transient_sample"], 5760
                )
                self.assertEqual(
                    boundary_result["policy_evidence"]["adjacent_onset_authority"],
                    "expected_transient_sample",
                )

                control_result = sfx_delivery.apply_density_policy(
                    [
                        proposal(
                            "title-zero-control", "0", "title_enter", "soft-ui-tick-v1"
                        ),
                        proposal(
                            "typing-6000", "0.125", "typing", "typing-tick-v1"
                        ),
                    ],
                    catalog,
                    total_samples,
                )
                self.assertEqual(
                    [event["trigger_id"] for event in control_result["kept"]],
                    ["title-zero-control", "typing-6000"],
                )
                self.assertEqual(control_result["dropped"], [])

            with self.subTest(boundary="half_open_overlap_limit"):
                result = sfx_delivery.apply_density_policy(
                    [
                        proposal("count-200", "2.00", "count_tick", "short-riser-v1"),
                        proposal("count-212", "2.12", "count_tick", "short-riser-v1"),
                        proposal("count-224", "2.24", "count_tick", "short-riser-v1"),
                    ],
                    catalog,
                    total_samples,
                )
                self.assertEqual(
                    [event["trigger_id"] for event in result["kept"]],
                    ["count-200", "count-212"],
                )
                self.assertEqual(result["dropped"][0]["reason"], "overlap_limit")
                self.assertEqual(result["policy_evidence"]["adjacent_onset_samples"], 5760)
                self.assertEqual(result["policy_evidence"]["max_overlap"], 2)
                forged_catalog = json.loads(json.dumps(catalog))
                forged_riser = next(
                    asset for asset in forged_catalog["assets"]
                    if asset["asset_id"] == "short-riser-v1"
                )
                forged_riser["duration_samples"] = 6000
                forged_riser["transient_anchor_sample"] = 240
                self.assertTrue(sfx_delivery.validate_catalog(forged_catalog))
                with self.assertRaisesRegex(sfx_delivery.SfxDeliveryError, "catalog|asset"):
                    sfx_delivery.apply_density_policy(
                        [
                            proposal("count-200", "2.00", "count_tick", "short-riser-v1"),
                            proposal("count-212", "2.12", "count_tick", "short-riser-v1"),
                            proposal("count-224", "2.24", "count_tick", "short-riser-v1"),
                        ],
                        forged_catalog,
                        total_samples,
                    )

            with self.subTest(boundary="clip_density_limit"):
                row_proposals = [
                    proposal(f"row-{index:02d}", f"{index}.00", "row_reveal", "short-pop-v1")
                    for index in range(11)
                ]
                result = sfx_delivery.apply_density_policy(
                    row_proposals, catalog, total_samples
                )
                self.assertEqual(len(result["kept"]), 10)
                self.assertEqual(result["kept"][-1]["trigger_id"], "row-09")
                self.assertEqual(result["dropped"][-1]["trigger_id"], "row-10")
                self.assertEqual(result["dropped"][-1]["reason"], "clip_density_limit")
                self.assertEqual(result["policy_evidence"]["density_cap"], 10)

            malformed = proposal("bad", "0.0", "typing", "typing-tick-v1")
            for missing in ("trigger_id", "trigger_onset_sample", "role", "asset_id", "evidence"):
                invalid = json.loads(json.dumps(malformed))
                del invalid[missing]
                with self.subTest(malformed=missing), self.assertRaises(sfx_delivery.SfxDeliveryError):
                    sfx_delivery.apply_density_policy([invalid], catalog, total_samples)

            wrong_mapping = proposal("wrong-role", "0.0", "typing", "typing-tick-v1")
            wrong_mapping = json.loads(json.dumps(wrong_mapping))
            wrong_mapping["asset_id"] = "soft-ui-tick-v1"
            with self.assertRaisesRegex(sfx_delivery.SfxDeliveryError, "role.*asset|asset.*role"):
                sfx_delivery.apply_density_policy([wrong_mapping], catalog, total_samples)

            exact_proposal = proposal("exact-proposal", "1.0", "typing", "typing-tick-v1")
            exact_mutations = {
                "caller_id": lambda item: item.__setitem__("trigger_id", "changed-id"),
                "caller_onset": lambda item: item.__setitem__(
                    "trigger_onset_sample", item["trigger_onset_sample"] + 1
                ),
                "caller_valid_role_asset": lambda item: item.update({
                    "role": "title_enter",
                    "asset_id": "soft-ui-tick-v1",
                }),
                "extra_proposal_field": lambda item: item.__setitem__("extra", True),
                "extra_evidence_field": lambda item: item["evidence"].__setitem__(
                    "extra", True
                ),
            }
            for name, mutate in exact_mutations.items():
                mutated = json.loads(json.dumps(exact_proposal))
                mutate(mutated)
                with self.subTest(exact_mismatch=name), self.assertRaisesRegex(
                    sfx_delivery.SfxDeliveryError, "renderer evidence"
                ):
                    sfx_delivery.apply_density_policy([mutated], catalog, total_samples)

            mismatched_evidence = proposal("claimed", "1.0", "typing", "typing-tick-v1")
            mismatched_evidence["evidence"]["trigger"]["id"] = "different"
            with self.assertRaisesRegex(sfx_delivery.SfxDeliveryError, "evidence"):
                sfx_delivery.apply_density_policy(
                    [mismatched_evidence], catalog, total_samples
                )
            mismatched_start = proposal("claimed-start", "1.0", "typing", "typing-tick-v1")
            mismatched_start["evidence"]["trigger"]["start"] = "1.25"
            with self.assertRaisesRegex(sfx_delivery.SfxDeliveryError, "evidence"):
                sfx_delivery.apply_density_policy(
                    [mismatched_start], catalog, total_samples
                )

            duplicate = proposal("duplicate", "0.0", "typing", "typing-tick-v1")
            with self.assertRaisesRegex(sfx_delivery.SfxDeliveryError, "duplicate"):
                sfx_delivery.apply_density_policy([duplicate, json.loads(json.dumps(duplicate))], catalog, total_samples)

            out_of_bounds = proposal("too-late", "15.0", "row_reveal", "short-pop-v1")
            with self.assertRaisesRegex(sfx_delivery.SfxDeliveryError, "outside|payload"):
                sfx_delivery.apply_density_policy([out_of_bounds], catalog, total_samples)

    @staticmethod
    def multi_event_visual_evidence() -> dict:
        title, count = SfxDeliveryTests.visual_evidence()["items"]
        title.update({
            "id": "title-1",
            "start": "0.20",
            "end": "0.50",
            "component_id": "title-lockup",
            "motion": {
                "requested": "slide-up",
                "delivered": "slide-up",
                "faithful": True,
                "status": "native",
            },
        })
        count.update({
            "id": "count-1",
            "start": "1.20",
            "end": "1.60",
            "kind": "stat",
            "component_id": "hero-stat",
            "motion": {
                "requested": "count-up",
                "delivered": "count-up",
                "faithful": True,
                "status": "rendered",
            },
        })
        return {
            "schema_version": 1,
            "duration_s": 6.0,
            "items": [
                title,
                {
                    "id": "typing-dropped",
                    "start": "0.25",
                    "end": "0.45",
                    "kind": "title",
                    "component_id": "prompt-card",
                    "motion": {
                        "requested": "word-cascade",
                        "delivered": "word-cascade",
                        "faithful": True,
                        "status": "rendered",
                    },
                },
                {
                    "id": "row-1",
                    "start": "0.70",
                    "end": "1.10",
                    "kind": "dynamic_list",
                    "component_id": "dynamic-list",
                    "motion": {
                        "requested": "staggered-reveal",
                        "delivered": "staggered-reveal",
                        "faithful": True,
                        "status": "rendered",
                    },
                },
                count,
                {
                    "id": "grid-1",
                    "start": "1.70",
                    "end": "2.10",
                    "kind": "chart",
                    "component_id": "dashboard",
                    "motion": {
                        "requested": "pan",
                        "delivered": "pan",
                        "faithful": True,
                        "status": "native",
                    },
                },
            ],
        }

    def test_stage_multi_event_delivery_is_deterministic_and_verifies_every_kept_event(self) -> None:
        evidence = self.multi_event_visual_evidence()
        original_evidence = json.loads(json.dumps(evidence))
        revision, cut_hash = "a" * 64, "b" * 64

        with tempfile.TemporaryDirectory() as first_directory, tempfile.TemporaryDirectory() as second_directory:
            first = sfx_delivery.stage_multi_event_delivery(
                Path(first_directory), evidence, revision, cut_hash
            )
            self.assertEqual(evidence, original_evidence)
            evidence["items"][0]["id"] = "caller-mutated"
            evidence["items"][0]["motion"]["requested"] = "caller-mutated"
            second = sfx_delivery.stage_multi_event_delivery(
                Path(second_directory), original_evidence, revision, cut_hash
            )

            for first_path, second_path in zip(first, second):
                self.assertEqual(first_path.read_bytes(), second_path.read_bytes())
            first_plan = json.loads(first[0].read_text(encoding="utf-8"))
            self.assertEqual(
                [event["trigger_id"] for event in first_plan["events"]],
                ["title-1", "row-1", "count-1", "grid-1"],
            )
            self.assertEqual(
                [item["trigger_id"] for item in first_plan["density"]["dropped"]],
                ["typing-dropped"],
            )
            self.assertEqual(
                first_plan["density"]["dropped"][0]["reason"], "adjacent_onset"
            )
            self.assertEqual(
                first_plan["density"]["dropped"][0]["evidence"]["trigger"]["id"],
                "typing-dropped",
            )

            priority_paths = self.write_priority_stems(
                Path(first_directory), first_plan["sfx_stem_sample_count"], -38.0, -52.0
            )
            report = sfx_delivery.verify_delivery(
                *first, original_evidence, revision, cut_hash,
                dialogue_priority_dialogue_path=priority_paths[0],
                dialogue_priority_sfx_path=priority_paths[1],
            )
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["schema_version"], 2)
            self.assertEqual(report["expected_event_count"], 4)
            self.assertEqual(report["delivered_event_count"], 4)
            self.assertEqual(len(report["observed_cue_evidence"]), 4)
            self.assertTrue(all(cue["status"] == "pass" for cue in report["observed_cue_evidence"]))
            priority = report["dialogue_priority_evidence"]
            self.assertEqual(priority["event_count"], 4)
            self.assertEqual(priority["active_event_count"], 4)
            self.assertEqual(priority["passed_event_count"], 4)

    def test_studio_gain_is_baked_and_verifier_requires_current_expected_hash(self) -> None:
        evidence = self.multi_event_visual_evidence()
        revision, cut_hash = "a" * 64, "b" * 64
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path, catalog_path, stem_path = sfx_delivery.stage_multi_event_delivery(
                root, evidence, revision, cut_hash
            )
            base_plan = json.loads(plan_path.read_text(encoding="utf-8"))
            source_event = base_plan["events"][0]
            edits = {
                "schema_version": 1,
                "source_render_id": "studio-source",
                "source_plan_sha256": "c" * 64,
                "source_timeline_revision": revision,
                "events": [
                    {
                        "id": source_event["id"],
                        "source_event_sha256": contract_registry.canonical_hash(source_event),
                        "event_start_sample": source_event["event_start_sample"],
                        "gain_db": -24,
                    }
                ],
            }
            resolved = editor_server.resolve_studio_audio_plan(base_plan, edits)
            asset_paths = {
                asset_id: root / sfx_delivery.STARTER_ASSET_FILENAMES[asset_id]
                for asset_id in sfx_delivery.STARTER_ASSET_IDS
            }
            decoded = sfx_delivery._write_multi_event_stem(
                stem_path,
                total_samples=resolved["sfx_stem_sample_count"],
                events=resolved["events"],
                asset_paths=asset_paths,
            )
            resolved["sfx_stem_sha256"] = sfx_delivery.sha256_bytes(stem_path.read_bytes())
            resolved["sfx_stem_decoded_pcm_sha256"] = sfx_delivery.sha256_bytes(decoded.pcm)
            plan_path.write_text(json.dumps(resolved, indent=2) + "\n", encoding="utf-8")
            expected_hash = contract_registry.canonical_hash(edits)
            dialogue, priority_sfx = self.write_priority_stems(
                root, resolved["sfx_stem_sample_count"], -38.0, -52.0
            )
            passed = sfx_delivery.verify_delivery(
                plan_path,
                catalog_path,
                stem_path,
                evidence,
                revision,
                cut_hash,
                dialogue_priority_dialogue_path=dialogue,
                dialogue_priority_sfx_path=priority_sfx,
                expected_studio_edits_sha256=expected_hash,
            )
            self.assertEqual(passed["status"], "pass", passed["failures"])

            missing = sfx_delivery.verify_delivery(
                plan_path,
                catalog_path,
                stem_path,
                evidence,
                revision,
                cut_hash,
                dialogue_priority_dialogue_path=dialogue,
                dialogue_priority_sfx_path=priority_sfx,
            )
            self.assertEqual(missing["status"], "fail")
            self.assertTrue(any("not authorized" in item for item in missing["failures"]))
            tampered = sfx_delivery.verify_delivery(
                plan_path,
                catalog_path,
                stem_path,
                evidence,
                revision,
                cut_hash,
                dialogue_priority_dialogue_path=dialogue,
                dialogue_priority_sfx_path=priority_sfx,
                expected_studio_edits_sha256="d" * 64,
            )
            self.assertEqual(tampered["status"], "fail")
            self.assertTrue(any("does not match current state" in item for item in tampered["failures"]))

    def test_v2_dialogue_priority_is_required_and_enforces_measured_boundaries(self) -> None:
        evidence = self.multi_event_visual_evidence()
        revision, cut_hash = "a" * 64, "b" * 64
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = sfx_delivery.stage_multi_event_delivery(root, evidence, revision, cut_hash)
            plan = json.loads(paths[0].read_text(encoding="utf-8"))
            missing = sfx_delivery.verify_delivery(*paths, evidence, revision, cut_hash)
            self.assertEqual(missing["status"], "fail")
            self.assertEqual(missing["delivered_event_count"], 0)
            self.assertTrue(any("requires both" in item for item in missing["failures"]))

            for dialogue_db, sfx_db, expected_status, expected_active in (
                (-44.084, -47.041, "fail", True),
                (-38.064, -51.556, "pass", True),
                (-56.126, -40.0, "pass", False),
            ):
                with self.subTest(dialogue_db=dialogue_db, sfx_db=sfx_db):
                    dialogue_path, sfx_path = self.write_priority_stems(
                        root, plan["sfx_stem_sample_count"], dialogue_db, sfx_db
                    )
                    report = sfx_delivery.verify_delivery(
                        *paths, evidence, revision, cut_hash,
                        dialogue_priority_dialogue_path=dialogue_path,
                        dialogue_priority_sfx_path=sfx_path,
                    )
                    self.assertEqual(report["status"], expected_status, report["failures"])
                    self.assertTrue(all(
                        item["active"] is expected_active
                        for item in report["dialogue_priority_evidence"]["events"]
                    ))
                    if expected_status == "fail":
                        self.assertEqual(report["delivered_event_count"], 0)

    def test_v2_dialogue_priority_rejects_alias_symlink_format_and_length(self) -> None:
        evidence = self.multi_event_visual_evidence()
        revision, cut_hash = "a" * 64, "b" * 64
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = sfx_delivery.stage_multi_event_delivery(root, evidence, revision, cut_hash)
            plan = json.loads(paths[0].read_text(encoding="utf-8"))
            dialogue, sfx = self.write_priority_stems(
                root, plan["sfx_stem_sample_count"], -38.0, -52.0
            )

            sfx.unlink()
            sfx.hardlink_to(dialogue)
            alias = sfx_delivery.verify_delivery(
                *paths, evidence, revision, cut_hash,
                dialogue_priority_dialogue_path=dialogue,
                dialogue_priority_sfx_path=sfx,
            )
            self.assertEqual(alias["status"], "fail")
            self.assertTrue(any("alias" in item for item in alias["failures"]))

            sfx.unlink()
            sfx.symlink_to(dialogue)
            symlink = sfx_delivery.verify_delivery(
                *paths, evidence, revision, cut_hash,
                dialogue_priority_dialogue_path=dialogue,
                dialogue_priority_sfx_path=sfx,
            )
            self.assertEqual(symlink["status"], "fail")

            sfx.unlink()
            with wave.open(str(sfx), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(44100)
                wav.writeframes(b"\0\0" * plan["sfx_stem_sample_count"])
            wrong_format = sfx_delivery.verify_delivery(
                *paths, evidence, revision, cut_hash,
                dialogue_priority_dialogue_path=dialogue,
                dialogue_priority_sfx_path=sfx,
            )
            self.assertEqual(wrong_format["status"], "fail")
            self.assertTrue(any("48kHz stereo PCM s24le" in item for item in wrong_format["failures"]))

            dialogue, sfx = self.write_priority_stems(
                root, plan["sfx_stem_sample_count"] - 1, -38.0, -52.0
            )
            truncated = sfx_delivery.verify_delivery(
                *paths, evidence, revision, cut_hash,
                dialogue_priority_dialogue_path=dialogue,
                dialogue_priority_sfx_path=sfx,
            )
            self.assertEqual(truncated["status"], "fail")
            self.assertTrue(any("sample count" in item for item in truncated["failures"]))

    def test_v2_dialogue_priority_hash_and_decode_share_one_file_snapshot(self) -> None:
        evidence = self.multi_event_visual_evidence()
        revision, cut_hash = "a" * 64, "b" * 64
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = sfx_delivery.stage_multi_event_delivery(root, evidence, revision, cut_hash)
            plan = json.loads(paths[0].read_text(encoding="utf-8"))
            dialogue, sfx = self.write_priority_stems(
                root, plan["sfx_stem_sample_count"], -38.0, -52.0
            )
            original = dialogue.read_bytes()
            real_decode = sfx_delivery.decode_s24le_wav_bytes
            calls = 0

            def mutate_after_snapshot(payload: bytes, *, source: str = "<bytes>"):
                nonlocal calls
                if source == str(dialogue):
                    calls += 1
                if source == str(dialogue) and calls == 1:
                    dialogue.write_bytes(b"replacement")
                return real_decode(payload, source=source)

            with patch.object(
                sfx_delivery, "decode_s24le_wav_bytes", side_effect=mutate_after_snapshot
            ):
                report = sfx_delivery.verify_delivery(
                    *paths, evidence, revision, cut_hash,
                    dialogue_priority_dialogue_path=dialogue,
                    dialogue_priority_sfx_path=sfx,
                )
            self.assertEqual(report["status"], "pass", report["failures"])
            self.assertEqual(
                report["dialogue_priority_evidence"]["dialogue_stem"]["file_sha256"],
                sfx_delivery.sha256_bytes(original),
            )

    def test_v2_candidate_correlation_uses_post_sidechain_sfx_during_active_dialogue(
        self,
    ) -> None:
        evidence = self.multi_event_visual_evidence()
        revision, cut_hash = "a" * 64, "b" * 64
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = sfx_delivery.stage_multi_event_delivery(
                root, evidence, revision, cut_hash
            )
            plan = json.loads(paths[0].read_text(encoding="utf-8"))
            pre_duck = sfx_delivery.decode_s24le_wav(paths[2])

            seed = 0xC0FFEE
            dialogue_amplitude = (10 ** (-44.0 / 20.0)) * math.sqrt(3.0)
            dialogue_samples = []
            for _ in pre_duck.samples:
                seed = (1664525 * seed + 1013904223) & 0xFFFFFFFF
                sample = ((((seed >> 8) / 16777215.0) * 2.0) - 1.0) * dialogue_amplitude
                dialogue_samples.append((sample, sample))
            post_duck_samples = [
                (left * 0.3, right * 0.3) if index % 31 == 0 else (0.0, 0.0)
                for index, (left, right) in enumerate(pre_duck.samples)
            ]
            candidate_samples = [
                (dialogue[0] + sfx[0], dialogue[1] + sfx[1])
                for dialogue, sfx in zip(dialogue_samples, post_duck_samples)
            ]

            def write_private_wav(path: Path, samples: list[tuple[float, float]]) -> None:
                with wave.open(str(path), "wb") as wav:
                    wav.setnchannels(2)
                    wav.setsampwidth(3)
                    wav.setframerate(48000)
                    wav.writeframes(b"".join(
                        sfx_delivery._pack_s24(left) + sfx_delivery._pack_s24(right)
                        for left, right in samples
                    ))

            dialogue_path = root / "dialogue_priority_dialogue.wav"
            post_duck_path = root / "dialogue_priority_sfx.wav"
            write_private_wav(dialogue_path, dialogue_samples)
            write_private_wav(post_duck_path, post_duck_samples)
            decoded_post_duck = sfx_delivery.decode_s24le_wav(post_duck_path)
            post_duck_snapshot = post_duck_path.read_bytes()
            candidate_pcm = b"".join(
                struct.pack("<ff", left, right) for left, right in candidate_samples
            )
            candidate_audio = sfx_delivery.DecodedWav(
                48000, 2, 4, candidate_pcm, candidate_samples
            )
            candidate_path = root / "candidate.mp4"
            candidate_path.write_bytes(b"deterministic active-dialogue candidate snapshot")

            for event in plan["events"]:
                pre_match = sfx_delivery._cue_template_correlation(
                    candidate_samples,
                    pre_duck.samples,
                    event["event_start_sample"],
                    event["duration_samples"],
                )
                post_match = sfx_delivery._cue_template_correlation(
                    candidate_samples,
                    decoded_post_duck.samples,
                    event["event_start_sample"],
                    event["duration_samples"],
                )
                self.assertIsNotNone(pre_match)
                self.assertIsNotNone(post_match)
                self.assertLess(pre_match[0], sfx_delivery.CANDIDATE_CORRELATION_THRESHOLD)
                self.assertGreaterEqual(
                    post_match[0], sfx_delivery.CANDIDATE_CORRELATION_THRESHOLD
                )

            real_decode = sfx_delivery.decode_s24le_wav_bytes

            def replace_post_duck_after_snapshot(
                payload: bytes, *, source: str = "<bytes>"
            ) -> sfx_delivery.DecodedWav:
                if source == str(post_duck_path):
                    post_duck_path.write_bytes(b"replacement after private snapshot")
                return real_decode(payload, source=source)

            with (
                patch.object(
                    sfx_delivery,
                    "_decode_candidate_audio_bytes",
                    return_value=candidate_audio,
                ),
                patch.object(
                    sfx_delivery,
                    "decode_s24le_wav_bytes",
                    side_effect=replace_post_duck_after_snapshot,
                ),
            ):
                report = sfx_delivery.verify_delivery(
                    *paths,
                    evidence,
                    revision,
                    cut_hash,
                    candidate_path=candidate_path,
                    dialogue_priority_dialogue_path=dialogue_path,
                    dialogue_priority_sfx_path=post_duck_path,
                )
            self.assertEqual(report["status"], "pass", report["failures"])
            self.assertEqual(
                report["dialogue_priority_evidence"]["active_event_count"],
                len(plan["events"]),
            )
            self.assertEqual(
                report["dialogue_priority_evidence"]["sfx_stem"]["file_sha256"],
                sfx_delivery.sha256_bytes(post_duck_snapshot),
            )
            candidate_cues = [
                cue
                for cue in report["observed_cue_evidence"]
                if cue.get("evidence_source") == "candidate_output_audio"
            ]
            self.assertEqual(len(candidate_cues), len(plan["events"]))
            self.assertTrue(all(cue["status"] == "pass" for cue in candidate_cues))

    def test_v2_candidate_threshold_uses_unrounded_correlation(self) -> None:
        evidence = self.multi_event_visual_evidence()
        revision, cut_hash = "a" * 64, "b" * 64
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = sfx_delivery.stage_multi_event_delivery(
                root, evidence, revision, cut_hash
            )
            plan = json.loads(paths[0].read_text(encoding="utf-8"))
            dialogue, ducked_sfx = self.write_priority_stems(
                root, plan["sfx_stem_sample_count"], -38.0, -52.0
            )
            samples = [(0.0, 0.0)] * plan["sfx_stem_sample_count"]
            candidate_audio = sfx_delivery.DecodedWav(
                48000, 2, 4, b"\0" * (len(samples) * 8), samples
            )
            candidate_path = root / "candidate.mp4"
            candidate_path.write_bytes(b"raw-correlation-rounding-boundary")

            with (
                patch.object(
                    sfx_delivery,
                    "_decode_candidate_audio_bytes",
                    return_value=candidate_audio,
                ),
                patch.object(
                    sfx_delivery,
                    "_cue_template_partial_correlation",
                    return_value=(0.2999996, 0, True),
                ),
                patch.object(
                    sfx_delivery,
                    "_estimate_dialogue_pipeline_lag",
                    return_value=(1.0, 0, 1.0),
                ),
            ):
                report = sfx_delivery.verify_delivery(
                    *paths,
                    evidence,
                    revision,
                    cut_hash,
                    candidate_path=candidate_path,
                    dialogue_priority_dialogue_path=dialogue,
                    dialogue_priority_sfx_path=ducked_sfx,
                )

            candidate_cues = [
                cue
                for cue in report["observed_cue_evidence"]
                if cue.get("evidence_source") == "candidate_output_audio"
            ]
            self.assertEqual(report["status"], "fail")
            self.assertEqual(report["delivered_event_count"], 0)
            self.assertTrue(candidate_cues)
            self.assertTrue(all(cue["correlation"] == 0.3 for cue in candidate_cues))
            self.assertTrue(all(cue["status"] == "fail" for cue in candidate_cues))

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"),
        "ffmpeg and ffprobe are required",
    )
    def test_v2_verifier_fails_closed_on_true_float_wav_nonfinite_samples(self) -> None:
        evidence = self.multi_event_visual_evidence()
        revision, cut_hash = "a" * 64, "b" * 64
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = sfx_delivery.stage_multi_event_delivery(
                root, evidence, revision, cut_hash
            )
            plan = json.loads(paths[0].read_text(encoding="utf-8"))
            total_samples = plan["sfx_stem_sample_count"]
            dialogue, ducked_sfx = self.write_priority_stems(
                root, total_samples, -38.0, -52.0
            )
            for label, nonfinite in (
                ("nan", float("nan")),
                ("positive-infinity", float("inf")),
                ("negative-infinity", float("-inf")),
            ):
                with self.subTest(label=label):
                    raw_path = root / f"{label}.f32le"
                    wav_path = root / f"{label}.wav"
                    poison_frame = 1024
                    raw_path.write_bytes(
                        b"\0" * (poison_frame * 8)
                        + struct.pack("<ff", nonfinite, nonfinite)
                        + b"\0" * ((total_samples - poison_frame - 1) * 8)
                    )
                    subprocess.run(
                        [
                            "ffmpeg", "-v", "error", "-y",
                            "-f", "f32le", "-ar", "48000", "-ac", "2",
                            "-i", str(raw_path),
                            "-c:a", "pcm_f32le", str(wav_path),
                        ],
                        check=True,
                        capture_output=True,
                    )
                    report = sfx_delivery.verify_delivery(
                        *paths,
                        evidence,
                        revision,
                        cut_hash,
                        candidate_path=wav_path,
                        dialogue_priority_dialogue_path=dialogue,
                        dialogue_priority_sfx_path=ducked_sfx,
                    )
                    self.assertEqual(report["status"], "fail")
                    self.assertTrue(any(
                        "non-finite" in failure for failure in report["failures"]
                    ))

    def test_v2_partial_correlation_controls_dialogue_gain_and_rejects_dialogue_only(
        self,
    ) -> None:
        evidence = self.multi_event_visual_evidence()
        revision, cut_hash = "a" * 64, "b" * 64
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = sfx_delivery.stage_multi_event_delivery(
                root, evidence, revision, cut_hash
            )
            pre_duck = sfx_delivery.decode_s24le_wav(paths[2])

            seed = 0xC0FFEE
            dialogue_amplitude = (10 ** (-44.0 / 20.0)) * math.sqrt(3.0)
            dialogue_samples = []
            for _ in pre_duck.samples:
                seed = (1664525 * seed + 1013904223) & 0xFFFFFFFF
                sample = ((((seed >> 8) / 16777215.0) * 2.0) - 1.0) * dialogue_amplitude
                dialogue_samples.append((sample, sample))
            post_duck_samples = [
                (left * 0.08, right * 0.08) if index % 31 == 0 else (0.0, 0.0)
                for index, (left, right) in enumerate(pre_duck.samples)
            ]

            def write_private_wav(path: Path, samples: list[tuple[float, float]]) -> None:
                with wave.open(str(path), "wb") as wav:
                    wav.setnchannels(2)
                    wav.setsampwidth(3)
                    wav.setframerate(48000)
                    wav.writeframes(b"".join(
                        sfx_delivery._pack_s24(left) + sfx_delivery._pack_s24(right)
                        for left, right in samples
                    ))

            dialogue_path = root / "dialogue_priority_dialogue.wav"
            post_duck_path = root / "dialogue_priority_sfx.wav"
            write_private_wav(dialogue_path, dialogue_samples)
            write_private_wav(post_duck_path, post_duck_samples)
            candidate_path = root / "candidate.mp4"
            candidate_path.write_bytes(b"dialogue-gain-variation-candidate")

            for gain_db in (0.0, 0.985, 3.0, 6.0):
                with self.subTest(gain_db=gain_db):
                    gain = 10 ** (gain_db / 20.0)
                    samples = [
                        (
                            dialogue[0] * gain + sfx[0],
                            dialogue[1] * gain + sfx[1],
                        )
                        for dialogue, sfx in zip(dialogue_samples, post_duck_samples)
                    ]
                    candidate_audio = sfx_delivery.DecodedWav(
                        48000,
                        2,
                        4,
                        b"".join(struct.pack("<ff", *frame) for frame in samples),
                        samples,
                    )
                    with patch.object(
                        sfx_delivery,
                        "_decode_candidate_audio_bytes",
                        return_value=candidate_audio,
                    ):
                        report = sfx_delivery.verify_delivery(
                            *paths,
                            evidence,
                            revision,
                            cut_hash,
                            candidate_path=candidate_path,
                            dialogue_priority_dialogue_path=dialogue_path,
                            dialogue_priority_sfx_path=post_duck_path,
                        )
                    candidate_cues = [
                        cue
                        for cue in report["observed_cue_evidence"]
                        if cue.get("evidence_source") == "candidate_output_audio"
                    ]
                    self.assertEqual(report["status"], "pass", report["failures"])
                    self.assertTrue(all(
                        cue["correlation"] >= sfx_delivery.CANDIDATE_CORRELATION_THRESHOLD
                        for cue in candidate_cues
                    ))

            dialogue_only = sfx_delivery.DecodedWav(
                48000,
                2,
                4,
                b"".join(struct.pack("<ff", *frame) for frame in dialogue_samples),
                dialogue_samples,
            )
            with patch.object(
                sfx_delivery,
                "_decode_candidate_audio_bytes",
                return_value=dialogue_only,
            ):
                negative = sfx_delivery.verify_delivery(
                    *paths,
                    evidence,
                    revision,
                    cut_hash,
                    candidate_path=candidate_path,
                    dialogue_priority_dialogue_path=dialogue_path,
                    dialogue_priority_sfx_path=post_duck_path,
                )
            negative_cues = [
                cue
                for cue in negative["observed_cue_evidence"]
                if cue.get("evidence_source") == "candidate_output_audio"
            ]
            self.assertEqual(negative["status"], "fail")
            self.assertEqual(negative["delivered_event_count"], 0)
            self.assertTrue(all(
                cue["correlation"] is not None
                and cue["correlation"] < sfx_delivery.CANDIDATE_CORRELATION_THRESHOLD
                for cue in negative_cues
            ))

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"),
        "ffmpeg and ffprobe are required",
    )
    def test_v2_true_aac_sparse_sample_candidate_fails_dense_cue_verification(
        self,
    ) -> None:
        evidence = self.multi_event_visual_evidence()
        revision, cut_hash = "a" * 64, "b" * 64
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = sfx_delivery.stage_multi_event_delivery(
                root, evidence, revision, cut_hash
            )
            plan = json.loads(paths[0].read_text(encoding="utf-8"))
            pre_duck = sfx_delivery.decode_s24le_wav(paths[2])
            total_samples = len(pre_duck.samples)

            seed = 0x12345678
            dialogue_amplitude = 10 ** (-24.0 / 20.0)
            dialogue_samples: list[tuple[float, float]] = []
            for _ in range(total_samples):
                seed = (1664525 * seed + 1013904223) & 0xFFFFFFFF
                sample = (
                    ((((seed >> 8) / 16777215.0) * 2.0) - 1.0)
                    * dialogue_amplitude
                )
                dialogue_samples.append((sample, sample))
            post_duck_samples = [
                (left * 0.3, right * 0.3)
                for left, right in pre_duck.samples
            ]

            preserved: set[int] = set()
            event_active: list[tuple[int, list[int], list[float]]] = []
            active_count = 0
            for event in plan["events"]:
                start = event["event_start_sample"]
                end = start + event["duration_samples"]
                template = [
                    (left + right) / 2.0
                    for left, right in post_duck_samples[start:end]
                ]
                peak = max(abs(value) for value in template)
                active = [
                    index
                    for index, value in enumerate(template)
                    if abs(value) >= peak * 0.01
                ]
                active_count += len(active)
                event_active.append((start, active, template))
                sampled = active[::8]
                if sampled[-1] != active[-1]:
                    sampled.append(active[-1])
                preserved.update(start + index for index in sampled)
            sparse_post_duck = [
                frame if index in preserved else (0.0, 0.0)
                for index, frame in enumerate(post_duck_samples)
            ]
            self.assertAlmostEqual(len(preserved) / active_count, 0.1251370447, places=9)

            # Keep approximately one eighth of each cue, but choose its
            # highest-energy active samples.  Unlike the every-eighth attack,
            # this retains enough waveform energy for dense Pearson shape to
            # remain above 0.30 despite deleting almost seven eighths of the
            # audible cue.  A second variant amplifies those same points 8x,
            # preventing an average relative-gain check from being sufficient.
            target_top_count = round(active_count * 0.125076)
            allocations = [
                int(len(active) * 0.125076)
                for _, active, _ in event_active
            ]
            remaining = target_top_count - sum(allocations)
            allocation_order = sorted(
                range(len(event_active)),
                key=lambda index: (
                    len(event_active[index][1]) * 0.125076 - allocations[index],
                    -index,
                ),
                reverse=True,
            )
            for index in allocation_order[:remaining]:
                allocations[index] += 1
            top_energy: set[int] = set()
            for allocation, (start, active, template) in zip(
                allocations, event_active
            ):
                ranked = sorted(
                    active,
                    key=lambda index: abs(template[index]),
                    reverse=True,
                )
                top_energy.update(start + index for index in ranked[:allocation])
            self.assertEqual(len(top_energy), target_top_count)
            self.assertAlmostEqual(len(top_energy) / active_count, 0.125076, places=4)
            top_energy_post_duck = [
                frame if index in top_energy else (0.0, 0.0)
                for index, frame in enumerate(post_duck_samples)
            ]
            amplified_top_energy_post_duck = [
                (frame[0] * 8.0, frame[1] * 8.0)
                if index in top_energy else (0.0, 0.0)
                for index, frame in enumerate(post_duck_samples)
            ]

            def write_wav(path: Path, samples: list[tuple[float, float]]) -> None:
                with wave.open(str(path), "wb") as wav:
                    wav.setnchannels(2)
                    wav.setsampwidth(3)
                    wav.setframerate(48000)
                    wav.writeframes(b"".join(
                        sfx_delivery._pack_s24(left) + sfx_delivery._pack_s24(right)
                        for left, right in samples
                    ))

            dialogue_path = root / "dialogue_priority_dialogue.wav"
            post_duck_path = root / "dialogue_priority_sfx.wav"
            write_wav(dialogue_path, dialogue_samples)
            write_wav(post_duck_path, post_duck_samples)

            def encode_candidate(
                label: str,
                cue_samples: list[tuple[float, float]],
                *,
                codec: str = "aac",
                bitrate: str | None = "192k",
            ) -> Path:
                source = root / f"{label}.wav"
                candidate = root / f"{label}.m4a"
                write_wav(source, [
                    (dialogue[0] + cue[0], dialogue[1] + cue[1])
                    for dialogue, cue in zip(dialogue_samples, cue_samples)
                ])
                command = [
                    "ffmpeg", "-v", "error", "-y", "-i", str(source),
                    "-c:a", codec,
                ]
                if bitrate is not None:
                    command.extend(("-b:a", bitrate))
                command.append(str(candidate))
                subprocess.run(
                    command,
                    check=True,
                    capture_output=True,
                )
                return candidate

            full_reports = {}
            for label, codec, bitrate in (
                ("aac-32k", "aac", "32k"),
                ("aac-64k", "aac", "64k"),
                ("aac-128k", "aac", "128k"),
                ("aac-192k", "aac", "192k"),
                ("alac", "alac", None),
            ):
                full_reports[label] = sfx_delivery.verify_delivery(
                    *paths,
                    evidence,
                    revision,
                    cut_hash,
                    candidate_path=encode_candidate(
                        f"full-{label}",
                        post_duck_samples,
                        codec=codec,
                        bitrate=bitrate,
                    ),
                    dialogue_priority_dialogue_path=dialogue_path,
                    dialogue_priority_sfx_path=post_duck_path,
                )
            sparse_report = sfx_delivery.verify_delivery(
                *paths,
                evidence,
                revision,
                cut_hash,
                candidate_path=encode_candidate("sparse", sparse_post_duck),
                dialogue_priority_dialogue_path=dialogue_path,
                dialogue_priority_sfx_path=post_duck_path,
            )
            top_energy_report = sfx_delivery.verify_delivery(
                *paths,
                evidence,
                revision,
                cut_hash,
                candidate_path=encode_candidate(
                    "top-energy", top_energy_post_duck
                ),
                dialogue_priority_dialogue_path=dialogue_path,
                dialogue_priority_sfx_path=post_duck_path,
            )
            amplified_top_energy_report = sfx_delivery.verify_delivery(
                *paths,
                evidence,
                revision,
                cut_hash,
                candidate_path=encode_candidate(
                    "amplified-top-energy", amplified_top_energy_post_duck
                ),
                dialogue_priority_dialogue_path=dialogue_path,
                dialogue_priority_sfx_path=post_duck_path,
            )

            for label, full_report in full_reports.items():
                with self.subTest(label=label):
                    self.assertEqual(
                        full_report["status"], "pass", full_report["failures"]
                    )
            self.assertEqual(sparse_report["status"], "fail")
            self.assertEqual(sparse_report["delivered_event_count"], 0)
            for label, report in (
                ("top-energy", top_energy_report),
                ("amplified-top-energy", amplified_top_energy_report),
            ):
                with self.subTest(label=label):
                    self.assertEqual(
                        report["status"], "fail", report["observed_cue_evidence"]
                    )
                    self.assertEqual(report["delivered_event_count"], 0)

    def test_cue_template_partial_correlation_tolerates_pipeline_lag(self) -> None:
        # A real render's published candidate can be delayed by a small,
        # uniform sample offset relative to the private pre-encode dialogue
        # and post-sidechain SFX evidence stems (e.g. loudnorm/AAC encode
        # pipeline delay); those two private stems share one lag-free
        # timeline with each other. The dialogue sample used to regress out
        # private dialogue at a trial lag must stay at the *unshifted*
        # position, not the candidate-shifted position, or genuine active
        # dialogue desynchronizes the partial regression and collapses the
        # correlation below threshold even though the cue is genuinely
        # present.
        duration_samples = 2400
        event_start_sample = 20000
        max_lag_samples = 512
        total = event_start_sample + duration_samples + 50000

        seed = 0xC0FFEE

        def rand() -> float:
            nonlocal seed
            seed = (1664525 * seed + 1013904223) & 0xFFFFFFFF
            return (((seed >> 8) / 16777215.0) * 2.0) - 1.0

        # Dialogue must dominate over the ducked cue (dialogue-priority
        # ducking requires the SFX to sit at least 6dB below active
        # dialogue), so keep dialogue louder than the cue here too.
        dialogue_amplitude = 10 ** (-20.0 / 20.0)
        dialogue_samples = []
        for _ in range(total):
            value = rand() * dialogue_amplitude
            dialogue_samples.append((value, value))

        # A broadband decaying noise burst (not a periodic tone) avoids a
        # periodic waveform's own autocorrelation side-lobes masking the
        # alignment bug across the +-max_lag_samples search.
        stem_seed = 0xBADC0FFE
        stem_samples = [(0.0, 0.0)] * total
        cue_amplitude = 10 ** (-26.0 / 20.0)
        for offset in range(duration_samples):
            stem_seed = (1664525 * stem_seed + 1013904223) & 0xFFFFFFFF
            noise = (((stem_seed >> 8) / 16777215.0) * 2.0) - 1.0
            decay = math.exp(-offset / 400.0)
            value = cue_amplitude * decay * noise
            stem_samples[event_start_sample + offset] = (value, value)

        event = {
            "event_start_sample": event_start_sample,
            "duration_samples": duration_samples,
        }
        for lag_samples in (0, 80, 200, 250, 512):
            with self.subTest(lag_samples=lag_samples):
                output_samples = [(0.0, 0.0)] * total
                for index in range(lag_samples, total):
                    source = index - lag_samples
                    dialogue = dialogue_samples[source]
                    stem = stem_samples[source]
                    output_samples[index] = (
                        dialogue[0] + stem[0],
                        dialogue[1] + stem[1],
                    )

                lag_match = sfx_delivery._estimate_dialogue_pipeline_lag(
                    output_samples,
                    dialogue_samples,
                    [event],
                    max_lag_samples=max_lag_samples,
                )
                self.assertIsNotNone(lag_match)
                dialogue_correlation, found_lag, dialogue_gain = lag_match
                self.assertGreaterEqual(
                    dialogue_correlation,
                    sfx_delivery.CANDIDATE_CORRELATION_THRESHOLD,
                )
                self.assertEqual(found_lag, lag_samples)
                result = sfx_delivery._cue_template_partial_correlation(
                    output_samples,
                    stem_samples,
                    dialogue_samples,
                    event_start_sample,
                    duration_samples,
                    pipeline_lag_samples=found_lag,
                    dialogue_gain=dialogue_gain,
                    max_lag_samples=max_lag_samples,
                )
                self.assertIsNotNone(result)
                correlation, cue_lag, complete = result
                self.assertGreaterEqual(
                    correlation,
                    sfx_delivery.CANDIDATE_CORRELATION_THRESHOLD,
                    f"lag={cue_lag} correlation={correlation}",
                )
                self.assertEqual(cue_lag, lag_samples)
                self.assertTrue(complete)

        outside_lag = max_lag_samples + 1
        outside_output = [(0.0, 0.0)] * total
        for index in range(outside_lag, total):
            source = index - outside_lag
            dialogue = dialogue_samples[source]
            stem = stem_samples[source]
            outside_output[index] = (
                dialogue[0] + stem[0],
                dialogue[1] + stem[1],
            )
        outside_match = sfx_delivery._estimate_dialogue_pipeline_lag(
            outside_output,
            dialogue_samples,
            [event],
            max_lag_samples=max_lag_samples,
        )
        self.assertTrue(
            outside_match is None
            or outside_match[0] < sfx_delivery.CANDIDATE_CORRELATION_THRESHOLD
        )

        verified_lag = 200

        def shifted_mix(cue: list[tuple[float, float]]) -> list[tuple[float, float]]:
            output = [(0.0, 0.0)] * total
            for index in range(verified_lag, total):
                source = index - verified_lag
                output[index] = (
                    dialogue_samples[source][0] + cue[source][0],
                    dialogue_samples[source][1] + cue[source][1],
                )
            return output

        wrong_seed = 0x51A7E
        wrong_cue = [(0.0, 0.0)] * total
        for offset in range(duration_samples):
            wrong_seed = (1664525 * wrong_seed + 1013904223) & 0xFFFFFFFF
            noise = (((wrong_seed >> 8) / 16777215.0) * 2.0) - 1.0
            value = cue_amplitude * math.exp(-offset / 400.0) * noise
            wrong_cue[event_start_sample + offset] = (value, value)
        inverted_cue = [(-left, -right) for left, right in stem_samples]
        silence = [(0.0, 0.0)] * total
        for label, cue in (
            ("dialogue-only", silence),
            ("wrong-cue", wrong_cue),
            ("phase-inversion", inverted_cue),
        ):
            with self.subTest(negative=label):
                output = shifted_mix(cue)
                lag_match = sfx_delivery._estimate_dialogue_pipeline_lag(
                    output,
                    dialogue_samples,
                    [event],
                    max_lag_samples=max_lag_samples,
                )
                self.assertIsNotNone(lag_match)
                self.assertGreaterEqual(
                    lag_match[0], sfx_delivery.CANDIDATE_CORRELATION_THRESHOLD
                )
                self.assertEqual(lag_match[1], verified_lag)
                cue_match = sfx_delivery._cue_template_partial_correlation(
                    output,
                    stem_samples,
                    dialogue_samples,
                    event_start_sample,
                    duration_samples,
                    pipeline_lag_samples=lag_match[1],
                    dialogue_gain=lag_match[2],
                )
                if label == "dialogue-only":
                    self.assertTrue(
                        cue_match is None
                        or cue_match[0]
                        < sfx_delivery.CANDIDATE_CORRELATION_THRESHOLD
                    )
                else:
                    self.assertIsNotNone(cue_match)
                    self.assertLess(
                        cue_match[0], sfx_delivery.CANDIDATE_CORRELATION_THRESHOLD
                    )

        noise_seed = 0xBAD5EED
        noise_output: list[tuple[float, float]] = []
        for _ in range(total):
            noise_seed = (1664525 * noise_seed + 1013904223) & 0xFFFFFFFF
            value = ((((noise_seed >> 8) / 16777215.0) * 2.0) - 1.0) * 0.1
            noise_output.append((value, value))
        noise_lag = sfx_delivery._estimate_dialogue_pipeline_lag(
            noise_output,
            dialogue_samples,
            [event],
            max_lag_samples=max_lag_samples,
        )
        self.assertTrue(
            noise_lag is None
            or noise_lag[0] < sfx_delivery.CANDIDATE_CORRELATION_THRESHOLD
        )

        cue_only_lag = sfx_delivery._estimate_dialogue_pipeline_lag(
            stem_samples,
            dialogue_samples,
            [event],
            max_lag_samples=max_lag_samples,
        )
        self.assertTrue(
            cue_only_lag is None
            or cue_only_lag[0] < sfx_delivery.CANDIDATE_CORRELATION_THRESHOLD
        )

        short_duration = 48
        short_start = total - short_duration
        short_stem = [(0.0, 0.0)] * total
        for offset in range(short_duration):
            polarity = -1.0 if offset % 2 == 0 else 1.0
            value = 0.02 * (1.0 - offset / 64.0) * polarity
            short_stem[short_start + offset] = (value, value)
        short_output = [
            (dialogue[0] + cue[0], dialogue[1] + cue[1])
            for dialogue, cue in zip(dialogue_samples, short_stem)
        ]
        short_event = {
            "event_start_sample": short_start,
            "duration_samples": short_duration,
        }
        short_lag = sfx_delivery._estimate_dialogue_pipeline_lag(
            short_output,
            dialogue_samples,
            [short_event],
            max_lag_samples=max_lag_samples,
        )
        self.assertIsNotNone(short_lag)
        self.assertEqual(short_lag[1], 0)
        short_match = sfx_delivery._cue_template_partial_correlation(
            short_output,
            short_stem,
            dialogue_samples,
            short_start,
            short_duration,
            pipeline_lag_samples=short_lag[1],
            dialogue_gain=short_lag[2],
        )
        self.assertIsNotNone(short_match)
        self.assertGreaterEqual(
            short_match[0], sfx_delivery.CANDIDATE_CORRELATION_THRESHOLD
        )
        self.assertTrue(short_match[2])

    def test_verify_multi_event_fails_closed_for_stale_forged_and_mutated_artifacts(self) -> None:
        evidence = self.multi_event_visual_evidence()
        revision, cut_hash = "a" * 64, "b" * 64
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path, catalog_path, stem_path = sfx_delivery.stage_multi_event_delivery(
                root, evidence, revision, cut_hash
            )
            original_plan = json.loads(plan_path.read_text(encoding="utf-8"))
            original_stem = stem_path.read_bytes()

            self.assertEqual(
                json.loads(plan_path.read_text(encoding="utf-8"))["events"][0]["evidence"]["trigger"]["id"],
                "title-1",
            )
            stale_evidence = json.loads(json.dumps(evidence))
            stale_evidence["items"][0]["end"] = "0.55"
            stale_report = sfx_delivery.verify_delivery(
                plan_path, catalog_path, stem_path, stale_evidence, revision, cut_hash
            )
            self.assertEqual(stale_report["status"], "fail")

            mutations = {
                "role_asset_pair": lambda plan: plan["events"][0].update({
                    "role": "row_reveal",
                    "asset_id": "short-pop-v1",
                }),
                "renderer_hash": lambda plan: plan["events"][0]["evidence"].update({
                    "renderer_trigger_sha256": "0" * 64,
                }),
                "event_order": lambda plan: plan.update({
                    "events": list(reversed(plan["events"])),
                }),
                "density_reason": lambda plan: (
                    plan["density"]["dropped"].__setitem__(0, {
                        **plan["density"]["dropped"][0],
                        "reason": "overlap_limit",
                    }),
                    plan["density"]["dropped_reasons"].update({"adjacent_onset": 0, "overlap_limit": 1}),
                ),
                "duplicate_event": lambda plan: plan["events"].__setitem__(
                    3, json.loads(json.dumps(plan["events"][0]))
                ),
                "missing_event": lambda plan: plan["events"].pop(),
            }
            for name, mutate in mutations.items():
                with self.subTest(mutation=name):
                    forged = json.loads(json.dumps(original_plan))
                    mutate(forged)
                    plan_path.write_text(
                        json.dumps(forged, sort_keys=True, separators=(",", ":")) + "\n",
                        encoding="utf-8",
                    )
                    report = sfx_delivery.verify_delivery(
                        plan_path, catalog_path, stem_path, evidence, revision, cut_hash
                    )
                    self.assertEqual(report["status"], "fail")
                plan_path.write_text(
                    json.dumps(original_plan, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )

            mutated_pcm = bytearray(original_stem)
            event = original_plan["events"][1]
            frame_start = 44 + event["event_start_sample"] * 6
            frame_end = frame_start + event["duration_samples"] * 6
            mutated_pcm[frame_start:frame_end] = b"\0" * (frame_end - frame_start)
            stem_path.write_bytes(bytes(mutated_pcm))
            forged_hashes = json.loads(json.dumps(original_plan))
            decoded_mutated = sfx_delivery.decode_s24le_wav(stem_path)
            forged_hashes["sfx_stem_sha256"] = sfx_delivery.sha256_file(stem_path)
            forged_hashes["sfx_stem_decoded_pcm_sha256"] = sfx_delivery.sha256_bytes(decoded_mutated.pcm)
            plan_path.write_text(
                json.dumps(forged_hashes, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            pcm_report = sfx_delivery.verify_delivery(
                plan_path, catalog_path, stem_path, evidence, revision, cut_hash
            )
            self.assertEqual(pcm_report["status"], "fail")
            self.assertTrue(any("saturating sum" in failure for failure in pcm_report["failures"]))

    def test_verify_multi_event_dispatch_uses_one_plan_snapshot(self) -> None:
        evidence = self.multi_event_visual_evidence()
        revision, cut_hash = "a" * 64, "b" * 64
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path, catalog_path, stem_path = sfx_delivery.stage_multi_event_delivery(
                root, evidence, revision, cut_hash
            )
            actual_plan = json.loads(plan_path.read_text(encoding="utf-8"))
            probe_v1 = json.loads(json.dumps(actual_plan))
            probe_v1["schema_version"] = 1
            real_read_json_once = sfx_delivery._read_json_once
            with patch.object(
                sfx_delivery,
                "_read_json_once",
                side_effect=[
                    (probe_v1, plan_path.read_bytes(), None),
                    (actual_plan, plan_path.read_bytes(), None),
                    real_read_json_once(catalog_path),
                ],
            ):
                report = sfx_delivery.verify_delivery(
                    plan_path, catalog_path, stem_path, evidence, revision, cut_hash
                )
            self.assertEqual(report["status"], "fail")

    def test_verify_delivery_rejects_malformed_plan_versions(self) -> None:
        evidence = self.multi_event_visual_evidence()
        revision, cut_hash = "a" * 64, "b" * 64
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = sfx_delivery.stage_multi_event_delivery(
                root, evidence, revision, cut_hash
            )
            plan = json.loads(paths[0].read_text(encoding="utf-8"))
            dialogue, sfx = self.write_priority_stems(
                root, plan["sfx_stem_sample_count"], -38.0, -52.0
            )
            for version in (True, 1.0, 2.0, "2", 3, None):
                with self.subTest(version=version):
                    malformed = json.loads(json.dumps(plan))
                    malformed["schema_version"] = version
                    paths[0].write_text(
                        json.dumps(malformed, sort_keys=True, separators=(",", ":")) + "\n",
                        encoding="utf-8",
                    )
                    report = sfx_delivery.verify_delivery(
                        *paths,
                        evidence,
                        revision,
                        cut_hash,
                        dialogue_priority_dialogue_path=dialogue,
                        dialogue_priority_sfx_path=sfx,
                    )
                    self.assertEqual(report["status"], "fail", report["failures"])
                    self.assertEqual(report["delivered_event_count"], 0)

            missing = json.loads(json.dumps(plan))
            missing.pop("schema_version")
            paths[0].write_text(
                json.dumps(missing, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            report = sfx_delivery.verify_delivery(
                *paths,
                evidence,
                revision,
                cut_hash,
                dialogue_priority_dialogue_path=dialogue,
                dialogue_priority_sfx_path=sfx,
            )
            self.assertEqual(report["status"], "fail", report["failures"])
            self.assertEqual(report["delivered_event_count"], 0)


class DialoguePriorityDuckTests(unittest.TestCase):
    """The mixer, not the QA threshold, is what makes room for the dialogue.

    SFX is delivered under speech, and "under" is a measured relation: the
    stem has to sit `DIALOGUE_PRIORITY_REQUIRED_REDUCTION_DB` below the
    dialogue in the window around every transient, or the render fails
    closed with `SFX is only 2.180225 dB below active dialogue` — which is
    what a real 90-second cut did twice tonight, on two of four events. The
    sidechain compressor in the graph is a shape, not a guarantee; the
    guarantee is a per-event attenuation measured against the dialogue this
    cut actually carries, before a single frame is rendered.
    """

    LEVELS = (-28.0, -40.0)

    def _dialogue(self, root: Path, total_samples: int, level_db: float) -> Path:
        amplitude = 10 ** (level_db / 20.0)
        frame = sfx_delivery._pack_s24(amplitude) * 2
        path = root / "dialogue_probe.wav"
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(2)
            wav.setsampwidth(3)
            wav.setframerate(48000)
            wav.writeframes(frame * total_samples)
        return path

    def _stage(self, root: Path, **kwargs):
        evidence = SfxDeliveryTests.multi_event_visual_evidence()
        plan_path, _catalog, stem_path = sfx_delivery.stage_multi_event_delivery(
            root, evidence, "a" * 64, "b" * 64, **kwargs
        )
        return json.loads(plan_path.read_text(encoding="utf-8")), stem_path

    def _relative_db(self, plan: dict, stem_path: Path, dialogue_path: Path):
        stem = sfx_delivery.decode_s24le_wav(stem_path)
        dialogue = sfx_delivery.decode_s24le_wav(dialogue_path)
        return {
            event["id"]: (
                sfx_delivery._window_rms_dbfs(
                    stem.samples, event["expected_transient_sample"]
                )
                - sfx_delivery._window_rms_dbfs(
                    dialogue.samples, event["expected_transient_sample"]
                )
            )
            for event in plan["events"]
        }

    def test_without_a_duck_the_stem_talks_over_the_dialogue(self) -> None:
        # The starting position, pinned so the fix below is not measuring
        # its own success: at the authored -12 dB these events are louder
        # than a -28 dBFS dialogue allows.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, stem_path = self._stage(root)
            dialogue = self._dialogue(root, plan["sfx_stem_sample_count"], -28.0)
            relative = self._relative_db(plan, stem_path, dialogue)
            self.assertTrue(
                any(
                    value > -sfx_delivery.DIALOGUE_PRIORITY_REQUIRED_REDUCTION_DB
                    for value in relative.values()
                ),
                relative,
            )

    def test_every_event_is_ducked_under_the_dialogue_it_will_be_mixed_with(
        self,
    ) -> None:
        for level in self.LEVELS:
            with self.subTest(dialogue_dbfs=level):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    reference, _stem = self._stage(root / "reference")
                    dialogue = self._dialogue(
                        root, reference["sfx_stem_sample_count"], level
                    )
                    plan, stem_path = self._stage(
                        root / "ducked", dialogue_probe=dialogue
                    )
                    relative = self._relative_db(plan, stem_path, dialogue)
                    for event_id, value in relative.items():
                        self.assertLessEqual(
                            value,
                            -sfx_delivery.DIALOGUE_PRIORITY_REQUIRED_REDUCTION_DB,
                            f"{event_id}: {value:.3f} dB relative to dialogue",
                        )

    def test_the_duck_is_a_separate_field_from_the_gain_the_studio_owns(self) -> None:
        # The authored gain stays in the range the Studio offers a human, so
        # a ducked plan is still a plan the editor can open and edit. The
        # attenuation the mixer decided lives beside it, and only there.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference, _stem = self._stage(root / "reference")
            dialogue = self._dialogue(root, reference["sfx_stem_sample_count"], -28.0)
            plan, _stem_path = self._stage(root / "ducked", dialogue_probe=dialogue)
            self.assertFalse(contract_registry.validate_artifact("audio_event_plan", plan))
            self.assertTrue(any(event["dialogue_duck_db"] < 0 for event in plan["events"]))
            for event in plan["events"]:
                self.assertEqual(event["gain_db"], -12)
                self.assertGreaterEqual(
                    event["dialogue_duck_db"], sfx_delivery.DIALOGUE_DUCK_FLOOR_DB
                )
                self.assertLessEqual(event["dialogue_duck_db"], 0)

    def test_a_dialogue_too_quiet_to_be_active_ducks_nothing(self) -> None:
        # Below the active threshold there is no dialogue to protect, and
        # attenuating anyway would quietly throw away the sound design the
        # cut asked for.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference, reference_stem = self._stage(root / "reference")
            silence = self._dialogue(
                root,
                reference["sfx_stem_sample_count"],
                sfx_delivery.DIALOGUE_PRIORITY_THRESHOLD_DBFS - 6.0,
            )
            plan, stem_path = self._stage(root / "quiet", dialogue_probe=silence)
            self.assertTrue(
                all(event["dialogue_duck_db"] == 0 for event in plan["events"])
            )
            self.assertEqual(stem_path.read_bytes(), reference_stem.read_bytes())

    def test_a_ducked_delivery_still_verifies_against_an_independent_rebuild(
        self,
    ) -> None:
        # The verifier rebuilds the stem from the plan alone, on purpose,
        # without borrowing the producer's arithmetic. A duck the rebuild
        # does not know about reads as a forged stem — which is exactly how
        # a real cut failed with "decoded PCM is not the independent
        # deterministic saturating sum" once the ducking went in.
        evidence = SfxDeliveryTests.multi_event_visual_evidence()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference, _stem = self._stage(root / "reference")
            dialogue = self._dialogue(root, reference["sfx_stem_sample_count"], -28.0)
            staged = sfx_delivery.stage_multi_event_delivery(
                root / "ducked", evidence, "a" * 64, "b" * 64, dialogue_probe=dialogue
            )
            plan = json.loads(staged[0].read_text(encoding="utf-8"))
            self.assertTrue(any(event["dialogue_duck_db"] < 0 for event in plan["events"]))
            priority_paths = SfxDeliveryTests.write_priority_stems(
                root, plan["sfx_stem_sample_count"], -38.0, -52.0
            )
            report = sfx_delivery.verify_delivery(
                *staged, evidence, "a" * 64, "b" * 64,
                dialogue_priority_dialogue_path=priority_paths[0],
                dialogue_priority_sfx_path=priority_paths[1],
            )
            self.assertEqual(report["status"], "pass", report["failures"])

    def test_a_forged_duck_outside_the_mixers_range_is_refused(self) -> None:
        evidence = SfxDeliveryTests.multi_event_visual_evidence()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staged = sfx_delivery.stage_multi_event_delivery(
                root / "plain", evidence, "a" * 64, "b" * 64
            )
            plan = json.loads(staged[0].read_text(encoding="utf-8"))
            plan["events"][0]["dialogue_duck_db"] = (
                sfx_delivery.DIALOGUE_DUCK_FLOOR_DB - 1.0
            )
            staged[0].write_text(json.dumps(plan), encoding="utf-8")
            priority_paths = SfxDeliveryTests.write_priority_stems(
                root, plan["sfx_stem_sample_count"], -38.0, -52.0
            )
            report = sfx_delivery.verify_delivery(
                *staged, evidence, "a" * 64, "b" * 64,
                dialogue_priority_dialogue_path=priority_paths[0],
                dialogue_priority_sfx_path=priority_paths[1],
            )
            self.assertEqual(report["status"], "fail")
            self.assertTrue(
                any("duck" in failure for failure in report["failures"]),
                report["failures"],
            )

    def test_the_ducked_plan_is_reproducible_from_the_same_dialogue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference, _stem = self._stage(root / "reference")
            dialogue = self._dialogue(root, reference["sfx_stem_sample_count"], -28.0)
            first_plan, first_stem = self._stage(root / "first", dialogue_probe=dialogue)
            second_plan, second_stem = self._stage(
                root / "second", dialogue_probe=dialogue
            )
            self.assertEqual(first_plan["events"], second_plan["events"])
            self.assertEqual(first_stem.read_bytes(), second_stem.read_bytes())


if __name__ == "__main__":
    unittest.main()
