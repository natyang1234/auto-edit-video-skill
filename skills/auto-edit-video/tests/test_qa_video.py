"""Delivery QA gate regression: black, silent, and audio-less finals must fail closed."""

from __future__ import annotations

import json
import copy
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import qa_video  # noqa: E402
import contract_registry  # noqa: E402
from visual_quality import rendered_visual_quality_report  # noqa: E402

FFMPEG = shutil.which("ffmpeg")


def make_video(
    output: Path,
    *,
    video_source: str,
    audio_source: str | None,
    duration: float = 2.0,
) -> None:
    command = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y"]
    separator = ":" if "=" in video_source else "="
    command += [
        "-f",
        "lavfi",
        "-i",
        f"{video_source}{separator}size=320x240:rate=30:duration={duration}",
    ]
    if audio_source is not None:
        command += ["-f", "lavfi", "-i", f"{audio_source}:duration={duration}"]
    command += ["-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast"]
    if audio_source is not None:
        command += ["-c:a", "aac", "-shortest"]
    command += [str(output)]
    subprocess.run(command, check=True, text=True, capture_output=True)


class QaVideoGateTest(unittest.TestCase):
    def setUp(self) -> None:
        if not FFMPEG or not shutil.which("ffprobe"):
            self.skipTest("ffmpeg/ffprobe are required for QA gate tests")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def inspect(self, video: Path, policy: "qa_video.QaPolicy | None" = None):
        report_path = self.dir / f"{video.stem}-report.json"
        contact_path = self.dir / f"{video.stem}-contact.png"
        if policy is None:
            return qa_video.inspect(video, report_path, contact_path)
        return qa_video.inspect(video, report_path, contact_path, policy=policy)

    @staticmethod
    def sfx_report(
        *, status: str = "pass", failures: list[str] | None = None
    ) -> dict[str, object]:
        events = [{
            "id": "sfx-title-enter-0001",
            "expected_transient_sample": 12000,
            "status": "pass",
        }]
        return {
            "schema_version": 1,
            "source": "independent_sfx_evidence",
            "status": status,
            "expected_event_count": 1,
            "delivered_event_count": len(events),
            "events": events,
            "failures": [] if failures is None else failures,
            "warnings": [],
            "candidate_output_sha256": "a" * 64,
            "output_audio_evidence": {
                "sample_rate": 48000,
                "channels": 2,
                "sample_width_bytes": 4,
                "sample_count": 96000,
                "expected_sample_count": 96000,
                "sample_count_delta": 0,
                "sample_count_tolerance_samples": 1024,
                "sample_count_tolerance_trailing_samples": 4096,
                "decoded_pcm_sha256": "b" * 64,
            },
            "observed_cue_evidence": [{
                "event_id": "sfx-title-enter-0001",
                "evidence_source": "candidate_output_audio",
                "status": "pass",
                "aligned": True,
                "correlation": 0.99,
            }],
        }

    @classmethod
    def sfx_v2_report(
        cls, *, dialogue_db: float = -38.0, sfx_db: float = -44.0,
    ) -> dict[str, object]:
        report = cls.sfx_report()
        active = dialogue_db > -45.0
        passed = not active or sfx_db <= dialogue_db - 6.0
        event_status = "inactive" if not active else ("pass" if passed else "fail")
        report["schema_version"] = 2
        report["dialogue_priority_evidence"] = {
            "authority": qa_video.sfx_delivery.DIALOGUE_PRIORITY_AUTHORITY,
            "policy": {
                "sample_rate": 48000,
                "channels": 2,
                "sample_width_bytes": 3,
                "window_samples": 12000,
                "window_alignment": "centered_on_expected_transient_zero_padded",
                "channel_aggregation": "maximum_per_channel_rms",
                "dialogue_active_strictly_above_dbfs": -45.0,
                "required_sfx_reduction_db": 6.0,
                "digital_silence_dbfs": -120.0,
            },
            "dialogue_stem": {
                "role": "pre-final-loudnorm_dialogue",
                "file_sha256": "c" * 64,
                "decoded_pcm_sha256": "d" * 64,
                "sample_rate": 48000,
                "channels": 2,
                "sample_width_bytes": 3,
                "sample_count": 96000,
            },
            "sfx_stem": {
                "role": "post-sidechain_pre-amix_sfx",
                "file_sha256": "e" * 64,
                "decoded_pcm_sha256": "f" * 64,
                "sample_rate": 48000,
                "channels": 2,
                "sample_width_bytes": 3,
                "sample_count": 96000,
            },
            "event_count": 1,
            "active_event_count": int(active),
            "passed_event_count": int(passed),
            "events": [{
                "event_id": "sfx-title-enter-0001",
                "expected_transient_sample": 12000,
                "dialogue_rms_dbfs": dialogue_db,
                "sfx_rms_dbfs": sfx_db,
                "sfx_relative_to_dialogue_db": sfx_db - dialogue_db,
                "active": active,
                "status": event_status,
            }],
        }
        return report

    def test_reference_video_with_audio_passes(self) -> None:
        video = self.dir / "reference.mp4"
        make_video(video, video_source="testsrc", audio_source="sine=frequency=440")
        report, ok = self.inspect(video)
        self.assertTrue(ok, report["failures"])
        self.assertEqual(report["status"], "pass")
        self.assertIn("policy", report, "report must echo the enforced QA policy")
        self.assertEqual(report["sfx_delivery"]["source"], "not_provided")
        self.assertEqual(report["sfx_delivery"]["status"], "not_evaluated")

    def test_visual_authority_is_freshly_recomputed_not_aggregate_trusted(self) -> None:
        video = self.dir / "forged-visual-pass.mp4"
        make_video(video, video_source="testsrc", audio_source="sine=frequency=440")
        raw = {
            "schema_version": 1,
            "duration_s": 2.0,
            "motion_intensity": "low",
            "expected_visual_beat_count": 1,
            "items": [
                {
                    "id": "unexpected-scene",
                    "start": 0.0,
                    "end": 1.0,
                    "kind": "title",
                    "font_evidence_required": True,
                    "minimum_primary_font_px": 48.0,
                }
            ],
        }
        authority = {
            "schema_version": 1,
            "source": "frozen_visual_authority",
            "visual_plan_revision": "a" * 64,
            "visual_plan_sha256": "b" * 64,
            "structured_layers_sha256": "c" * 64,
            "artifact_index_sha256": "d" * 64,
            "a_roll_breathing_intervals": [],
            "items": [],
        }
        raw["a_roll_breathing_intervals"] = []
        raw["motion_probes"] = {}
        authority["authority_hash"] = contract_registry.canonical_hash(authority)
        forged = {
            "schema_version": 1,
            "source": "renderer_evidence",
            "status": "pass",
            "expected_visual_beat_count": 1,
            "visual_beat_count": 1,
            "failures": [],
            "warnings": [],
            "raw_evidence": raw,
            "authority": authority,
            "authority_hash": authority["authority_hash"],
        }

        with self.assertRaisesRegex(ValueError, "fresh visual authority recomputation"):
            qa_video.inspect(
                video,
                self.dir / "forged-visual-report.json",
                self.dir / "forged-visual-contact.png",
                visual_report=forged,
            )

    def test_static_pixels_cannot_reuse_a_forged_passing_motion_probe(self) -> None:
        video = self.dir / "static-forged-motion.mp4"
        make_video(
            video,
            video_source="color=c=gray",
            audio_source="sine=frequency=440",
        )
        base = self.dir / "motion-base.mp4"
        make_video(base, video_source="color=c=gray", audio_source=None)
        source = self.dir / "frozen-card.png"
        subprocess.run(
            [
                FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "color=c=white:s=60x60:r=1",
                "-frames:v", "1", "-update", "1", str(source),
            ],
            check=True,
            capture_output=True,
        )
        source_sha256 = qa_video.sfx_delivery.sha256_file(source)
        binding = {
            "artifact_sha256": source_sha256,
            "source_path": str(source),
            "source_sha256": source_sha256,
            "source_kind": "image",
            "canvas_width": 320,
            "canvas_height": 240,
            "source_start_sample": 0,
            "source_end_sample": 96_000,
            "placement": {
                "width_percent": 18.75,
                "x_percent": 25.0,
                "y_percent": 20.833333,
                "animation": "pan",
            },
        }
        public_binding = {
            key: value for key, value in binding.items() if key != "source_path"
        }
        scene = {
            "id": "opening-title",
            "start": 0.0,
            "end": 2.0,
            "kind": "title",
            "component_id": "opening-title",
            "style_pack_id": "kinetic-social",
            "font_evidence_required": True,
            "minimum_primary_font_px": 48.0,
            "structured_layer_id": "layer-opening-title",
            "structured_layer_hash": "a" * 64,
            "artifact_hash": source_sha256,
            "motion": {
                "requested": "slide-up",
                "delivered": "slide-up",
                "faithful": True,
                "status": "native",
            },
            "eligibility": "eligible",
            "eligibility_reason": None,
            "family": "title_reveal",
            "role": "opening_title",
            "importance": "high",
            "major_graphic": True,
            "micro_silent": False,
            "stage": "full_screen_graphic",
            "trigger_role": "title_enter",
            "motion_window_start_sample": 0,
            "motion_window_end_sample": 96000,
            "graphic_roi": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
            "presenter_roi": None,
            "static_fallback": False,
        }
        forged_probe = {
            "sample_positions": [9600, 48000, 86400],
            "graphic_roi": copy.deepcopy(scene["graphic_roi"]),
            "candidate_matches": [
                {"sample": sample, "ssim": 0.99, "matched": True}
                for sample in (9600, 48000, 86400)
            ],
            "pairs": [
                {
                    "left_sample": left,
                    "right_sample": right,
                    "ssim": 0.8,
                    "changed_pixel_fraction": 0.2,
                    "detected": True,
                }
                for left, right in (
                    (9600, 48000),
                    (48000, 86400),
                    (9600, 86400),
                )
            ],
            "detected": True,
        }
        raw = {
            "schema_version": 1,
            "duration_s": 2.0,
            "motion_intensity": "high",
            "expected_visual_beat_count": 1,
            "a_roll_breathing_intervals": [],
            "motion_probes": {scene["id"]: copy.deepcopy(forged_probe)},
            "motion_input": {
                "base_path": str(base),
                "base_sha256": qa_video.sfx_delivery.sha256_file(base),
                "canvas_width": 320,
                "canvas_height": 240,
                "fps": 30,
            },
            "frozen_graphics": {scene["id"]: binding},
            "motion_attribution": {scene["id"]: public_binding},
            "items": [scene],
        }
        authority_scene_fields = (
            "id", "start", "end", "kind", "family", "role",
            "structured_layer_id", "structured_layer_hash", "artifact_hash",
            "evidence_id", "source_literal", "assets", "graphic_roi",
            "presenter_roi", "motion_window_start_sample",
            "motion_window_end_sample",
        )
        authority = {
            "schema_version": 1,
            "source": "frozen_visual_authority",
            "visual_plan_revision": "c" * 64,
            "visual_plan_sha256": "d" * 64,
            "structured_layers_sha256": "e" * 64,
            "artifact_index_sha256": "f" * 64,
            "a_roll_breathing_intervals": [],
            "items": [
                {field: copy.deepcopy(scene.get(field)) for field in authority_scene_fields}
            ],
            "motion_input": {
                "base_sha256": raw["motion_input"]["base_sha256"],
                "canvas_width": 320,
                "canvas_height": 240,
                "fps": 30,
            },
        }
        authority["items"][0]["motion_attribution"] = copy.deepcopy(public_binding)
        authority["authority_hash"] = contract_registry.canonical_hash(authority)
        forged_report = rendered_visual_quality_report(raw, authority)
        self.assertEqual(forged_report["status"], "pass", forged_report["failures"])

        fresh_probe = copy.deepcopy(forged_probe)
        fresh_probe["pairs"][0]["ssim"] = 0.80003
        with patch(
            "visual_motion_probe.measure_declared_motion",
            return_value={scene["id"]: fresh_probe},
        ):
            fresh_report, ok = qa_video.inspect(
                video,
                self.dir / "fresh-motion-report.json",
                self.dir / "fresh-motion-contact.png",
                visual_report=forged_report,
            )
        self.assertTrue(ok, fresh_report["failures"])
        self.assertEqual(
            fresh_report["visual_delivery"]["motion_probes"][scene["id"]],
            fresh_probe,
        )
        self.assertNotEqual(
            fresh_report["visual_delivery"]["motion_probes"][scene["id"]],
            forged_probe,
        )

        with self.assertRaisesRegex(ValueError, "fresh visual authority recomputation"):
            qa_video.inspect(
                video,
                self.dir / "static-forged-report.json",
                self.dir / "static-forged-contact.png",
                visual_report=forged_report,
            )

        fps_mutation = copy.deepcopy(forged_report)
        fps_mutation["raw_evidence"]["motion_input"]["fps"] = 15
        with self.assertRaises(ValueError):
            qa_video.inspect(
                video,
                self.dir / "fps-mutated-report.json",
                self.dir / "fps-mutated-contact.png",
                visual_report=fps_mutation,
            )

        source_payload = source.read_bytes()
        source_replacement = self.dir / ".source-replacement.png"
        source_replacement.write_bytes(b"atomically replaced source")
        os.replace(source_replacement, source)
        with self.assertRaisesRegex(ValueError, "frozen graphic.*SHA-256"):
            qa_video.inspect(
                video,
                self.dir / "source-mutated-report.json",
                self.dir / "source-mutated-contact.png",
                visual_report=forged_report,
            )
        source.write_bytes(source_payload)

        base_payload = base.read_bytes()
        base_replacement = self.dir / ".base-replacement.mp4"
        base_replacement.write_bytes(b"atomically replaced base")
        os.replace(base_replacement, base)
        with self.assertRaisesRegex(ValueError, "motion base visual.*SHA-256"):
            qa_video.inspect(
                video,
                self.dir / "base-mutated-report.json",
                self.dir / "base-mutated-contact.png",
                visual_report=forged_report,
            )
        base.write_bytes(base_payload)

    def test_valid_independent_sfx_evidence_is_embedded(self) -> None:
        video = self.dir / "sfx-pass.mp4"
        make_video(video, video_source="testsrc", audio_source="sine=frequency=440")
        sfx = self.sfx_report()
        sfx["candidate_output_sha256"] = qa_video.sfx_delivery.sha256_file(video)
        report_path = self.dir / "sfx-pass-report.json"
        contact_path = self.dir / "sfx-pass-contact.png"
        report, ok = qa_video.inspect(
            video,
            report_path,
            contact_path,
            sfx_report=sfx,
        )
        self.assertTrue(ok, report["failures"])
        self.assertEqual(report["schema_version"], 3)
        self.assertEqual(report["sfx_delivery"], sfx)

    def test_stale_sfx_pass_report_cannot_bind_a_different_video(self) -> None:
        first = self.dir / "sfx-first.mp4"
        second = self.dir / "sfx-second.mp4"
        make_video(first, video_source="testsrc", audio_source="sine=frequency=440")
        make_video(second, video_source="testsrc", audio_source="sine=frequency=880")
        sfx = self.sfx_report()
        sfx["candidate_output_sha256"] = qa_video.sfx_delivery.sha256_file(first)
        with self.assertRaisesRegex(ValueError, "does not match live video"):
            qa_video.inspect(
                second,
                self.dir / "stale-report.json",
                self.dir / "stale-contact.png",
                sfx_report=sfx,
            )

    def test_candidate_correlation_threshold_is_explicit_and_inclusive(self) -> None:
        below = self.sfx_report()
        below["observed_cue_evidence"][0]["correlation"] = (
            qa_video.sfx_delivery.CANDIDATE_CORRELATION_THRESHOLD - 0.001
        )
        with self.assertRaisesRegex(ValueError, "candidate output cue pass"):
            qa_video.validate_sfx_report(below)
        at_boundary = self.sfx_report()
        at_boundary["observed_cue_evidence"][0]["correlation"] = (
            qa_video.sfx_delivery.CANDIDATE_CORRELATION_THRESHOLD
        )
        qa_video.validate_sfx_report(at_boundary)

    def test_candidate_sample_count_tolerance_is_explicit_and_consistent(self) -> None:
        at_boundary = self.sfx_report()
        evidence = at_boundary["output_audio_evidence"]
        evidence["sample_count"] = evidence["expected_sample_count"] + 1024
        evidence["sample_count_delta"] = 1024
        qa_video.validate_sfx_report(at_boundary)

        over_boundary = self.sfx_report()
        evidence = over_boundary["output_audio_evidence"]
        evidence["sample_count"] = evidence["expected_sample_count"] + 1025
        evidence["sample_count_delta"] = 1025
        with self.assertRaisesRegex(ValueError, "within codec tolerance"):
            qa_video.validate_sfx_report(over_boundary)

        forged_delta = self.sfx_report()
        forged_delta["output_audio_evidence"]["sample_count_delta"] = 1
        with self.assertRaisesRegex(ValueError, "delta is inconsistent"):
            qa_video.validate_sfx_report(forged_delta)

    def test_trailing_codec_truncation_has_its_own_directional_tolerance(self) -> None:
        # 2026-08-13 ruling: a real AAC final is consistently *shorter* than
        # the planned stem by roughly 3.1k-3.3k samples, because the encoder
        # drops the trailing partial frame rather than padding it.  That is a
        # one-directional codec artefact, so the deficit side is bounded at
        # four AAC frames (4 x 1024) while the surplus side stays at 1024:
        # audio that appears from nowhere is still not a codec artefact.
        def report_with(delta: int) -> dict:
            report = self.sfx_report()
            evidence = report["output_audio_evidence"]
            evidence["sample_count"] = evidence["expected_sample_count"] + delta
            evidence["sample_count_delta"] = delta
            return report

        qa_video.validate_sfx_report(report_with(-4000))
        qa_video.validate_sfx_report(report_with(-4096))
        qa_video.validate_sfx_report(report_with(900))
        for rejected in (-4200, -4097, 1100, 1025):
            with self.subTest(delta=rejected):
                with self.assertRaisesRegex(ValueError, "within codec tolerance"):
                    qa_video.validate_sfx_report(report_with(rejected))

        forged = report_with(-4000)
        forged["output_audio_evidence"]["sample_count_tolerance_trailing_samples"] = 8192
        with self.assertRaisesRegex(ValueError, "independent policy"):
            qa_video.validate_sfx_report(forged)

    def test_v2_dialogue_priority_thresholds_are_strict_and_inclusive(self) -> None:
        exact_threshold = self.sfx_v2_report(dialogue_db=-45.0, sfx_db=-20.0)
        qa_video.validate_sfx_report(exact_threshold)

        exact_reduction = self.sfx_v2_report(dialogue_db=-38.0, sfx_db=-44.0)
        qa_video.validate_sfx_report(exact_reduction)

        insufficient = self.sfx_v2_report(dialogue_db=-38.0, sfx_db=-43.999999)
        with self.assertRaisesRegex(ValueError, "all dialogue-priority events"):
            qa_video.validate_sfx_report(insufficient)

        just_active = self.sfx_v2_report(dialogue_db=-44.999999, sfx_db=-44.999999)
        with self.assertRaisesRegex(ValueError, "all dialogue-priority events"):
            qa_video.validate_sfx_report(just_active)

        forged = self.sfx_v2_report()
        forged["dialogue_priority_evidence"]["events"][0]["dialogue_rms_dbfs"] = float("nan")
        with self.assertRaisesRegex(ValueError, "finite"):
            qa_video.validate_sfx_report(forged)

        wrong_window = self.sfx_v2_report()
        wrong_window["dialogue_priority_evidence"]["events"][0][
            "expected_transient_sample"
        ] += 1
        with self.assertRaisesRegex(ValueError, "expected sample"):
            qa_video.validate_sfx_report(wrong_window)

        wrong_length = self.sfx_v2_report()
        wrong_length["dialogue_priority_evidence"]["sfx_stem"]["sample_count"] += 1
        with self.assertRaisesRegex(ValueError, "sample counts differ"):
            qa_video.validate_sfx_report(wrong_length)

        alias = self.sfx_v2_report()
        alias["dialogue_priority_evidence"]["sfx_stem"]["file_sha256"] = (
            alias["dialogue_priority_evidence"]["dialogue_stem"]["file_sha256"]
        )
        with self.assertRaisesRegex(ValueError, "must not alias"):
            qa_video.validate_sfx_report(alias)

        wrong_types = self.sfx_v2_report()
        wrong_types["schema_version"] = 2.0
        with self.assertRaisesRegex(ValueError, "schema_version"):
            qa_video.validate_sfx_report(wrong_types)
        wrong_types = self.sfx_v2_report()
        wrong_types["dialogue_priority_evidence"]["policy"]["channels"] = 2.0
        with self.assertRaisesRegex(ValueError, "policy"):
            qa_video.validate_sfx_report(wrong_types)
        wrong_types = self.sfx_v2_report()
        wrong_types["dialogue_priority_evidence"]["dialogue_stem"]["channels"] = 2.0
        with self.assertRaisesRegex(ValueError, "format"):
            qa_video.validate_sfx_report(wrong_types)

        legacy_forged = self.sfx_report()
        legacy_forged["dialogue_priority_evidence"] = {}
        with self.assertRaisesRegex(ValueError, "v1 cannot"):
            qa_video.validate_sfx_report(legacy_forged)

    def test_failed_independent_sfx_evidence_fails_the_delivery(self) -> None:
        video = self.dir / "sfx-fail.mp4"
        make_video(video, video_source="testsrc", audio_source="sine=frequency=440")
        sfx = self.sfx_report(status="fail", failures=["cue transient missing"])
        report, ok = qa_video.inspect(
            video,
            self.dir / "sfx-fail-report.json",
            self.dir / "sfx-fail-contact.png",
            sfx_report=sfx,
        )
        self.assertFalse(ok)
        self.assertIn("sfx delivery: cue transient missing", report["failures"])
        self.assertEqual(report["sfx_delivery"], sfx)

    def test_invalid_independent_sfx_evidence_is_rejected(self) -> None:
        video = self.dir / "sfx-invalid.mp4"
        make_video(video, video_source="testsrc", audio_source="sine=frequency=440")
        invalid_reports = (
            {**self.sfx_report(), "schema_version": 2},
            {**self.sfx_report(), "source": "renderer_evidence"},
            {**self.sfx_report(), "events": [], "delivered_event_count": 1},
            {**self.sfx_report(), "expected_event_count": 2},
            {**self.sfx_report(), "failures": "not-a-list"},
            {**self.sfx_report(), "expected_count": 1},
            {**self.sfx_report(), "event_count": 1},
            {key: value for key, value in self.sfx_report().items()
             if key != "candidate_output_sha256"},
            {key: value for key, value in self.sfx_report().items()
             if key != "output_audio_evidence"},
            {key: value for key, value in self.sfx_report().items()
             if key != "observed_cue_evidence"},
            {**self.sfx_report(), "candidate_output_sha256": "A" * 64},
        )
        for invalid in invalid_reports:
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "SFX report"):
                    qa_video.inspect(
                        video,
                        self.dir / "sfx-invalid-report.json",
                        self.dir / "sfx-invalid-contact.png",
                        sfx_report=invalid,
                    )

    def test_cli_failed_verifier_is_not_allowed_to_pass(self) -> None:
        video = self.dir / "sfx-cli-fail.mp4"
        make_video(video, video_source="testsrc", audio_source="sine=frequency=440")
        visual_path = self.dir / "visual.json"
        visual_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source": "renderer_evidence",
                    "status": "pass",
                    "expected_visual_beat_count": 1,
                    "visual_beat_count": 1,
                    "failures": [],
                    "warnings": [],
                }
            ),
            encoding="utf-8",
        )
        plan = self.dir / "audio-event-plan.json"
        catalog = self.dir / "audio-catalog.json"
        stem = self.dir / "sfx-stem.wav"
        for artifact in (plan, catalog, stem):
            artifact.write_bytes(b"placeholder")
        sfx = self.sfx_report(status="fail", failures=["independent verifier failure"])
        report_path = self.dir / "sfx-cli-fail-report.json"
        contact_path = self.dir / "sfx-cli-fail-contact.png"
        with patch.object(
            qa_video.sfx_delivery, "verify_delivery", return_value=sfx, create=True
        ) as verify:
            with patch("builtins.print"):
                with patch.object(
                    sys,
                    "argv",
                    [
                        str(SCRIPTS_DIR / "qa_video.py"),
                        "--video",
                        str(video),
                        "--report",
                        str(report_path),
                        "--contact",
                        str(contact_path),
                        "--visual-evidence",
                        str(visual_path),
                        "--audio-event-plan",
                        str(plan),
                        "--audio-catalog",
                        str(catalog),
                        "--sfx-stem",
                        str(stem),
                        "--expected-timeline-revision",
                        "a" * 64,
                        "--expected-cut-map-sha256",
                        "b" * 64,
                    ],
                ):
                    exit_code = qa_video.main()
        self.assertEqual(exit_code, 2)
        verify.assert_called_once_with(
            plan.resolve(),
            catalog.resolve(),
            stem.resolve(),
            {
                "schema_version": 1,
                "source": "renderer_evidence",
                "status": "pass",
                "expected_visual_beat_count": 1,
                "visual_beat_count": 1,
                "failures": [],
                "warnings": [],
            },
            expected_timeline_revision="a" * 64,
            expected_cut_map_sha256="b" * 64,
            candidate_path=video.resolve(),
        )

    def test_cli_rejects_partial_sfx_artifacts_or_bindings(self) -> None:
        partial_sets = (
            ("--audio-event-plan", "plan.json"),
            ("--audio-catalog", "catalog.json"),
            ("--sfx-stem", "stem.wav"),
            ("--expected-timeline-revision", "a" * 64),
            ("--expected-cut-map-sha256", "b" * 64),
        )
        for flag, value in partial_sets:
            with self.subTest(flag=flag):
                with patch.object(
                    sys,
                    "argv",
                    [
                        str(SCRIPTS_DIR / "qa_video.py"),
                        "--video",
                        str(self.dir / "does-not-exist.mp4"),
                        flag,
                        value,
                    ],
                ):
                    with patch("builtins.print") as printed:
                        exit_code = qa_video.main()
                self.assertEqual(exit_code, 2)
                self.assertTrue(
                    any("all-or-none" in str(call) for call in printed.call_args_list),
                    f"partial {flag} must report all-or-none failure",
                )

    def test_renderer_visual_evidence_is_embedded_in_the_qa_report(self) -> None:
        video = self.dir / "visual-pass.mp4"
        make_video(video, video_source="testsrc", audio_source="sine=frequency=440")
        visual = {
            "schema_version": 1,
            "source": "renderer_evidence",
            "status": "pass",
            "expected_visual_beat_count": 3,
            "visual_beat_count": 3,
            "failures": [],
            "warnings": [],
        }
        report_path = self.dir / "visual-pass-report.json"
        contact_path = self.dir / "visual-pass-contact.png"
        report, ok = qa_video.inspect(
            video,
            report_path,
            contact_path,
            visual_report=visual,
        )
        self.assertTrue(ok, report["failures"])
        self.assertEqual(report["schema_version"], 3)
        self.assertEqual(report["visual_delivery"], visual)

    def test_failed_renderer_visual_evidence_fails_the_delivery(self) -> None:
        video = self.dir / "visual-fail.mp4"
        make_video(video, video_source="testsrc", audio_source="sine=frequency=440")
        visual = {
            "schema_version": 1,
            "source": "renderer_evidence",
            "status": "fail",
            "expected_visual_beat_count": 1,
            "visual_beat_count": 1,
            "failures": ["motion fallback"],
            "warnings": [],
        }
        report_path = self.dir / "visual-fail-report.json"
        contact_path = self.dir / "visual-fail-contact.png"
        report, ok = qa_video.inspect(
            video,
            report_path,
            contact_path,
            visual_report=visual,
        )
        self.assertFalse(ok)
        self.assertIn("visual delivery: motion fallback", report["failures"])
        self.assertEqual(report["visual_delivery"], visual)

    def test_invalid_renderer_visual_evidence_is_rejected(self) -> None:
        video = self.dir / "visual-invalid.mp4"
        make_video(video, video_source="testsrc", audio_source="sine=frequency=440")
        with self.assertRaisesRegex(ValueError, "visual report"):
            qa_video.inspect(
                video,
                self.dir / "invalid-report.json",
                self.dir / "invalid-contact.png",
                visual_report={"schema_version": 1, "status": "pass"},
            )

    def test_black_silent_video_fails_closed(self) -> None:
        video = self.dir / "black-silent.mp4"
        make_video(video, video_source="color=c=black", audio_source="anullsrc=r=48000:cl=stereo")
        report, ok = self.inspect(video)
        self.assertFalse(ok, "a fully black, silent final must not pass QA")
        self.assertEqual(report["status"], "fail")
        joined = " ".join(report["failures"]).lower()
        self.assertIn("black", joined)
        self.assertTrue(
            "loudness" in joined or "silent" in joined,
            f"silence must be a failure, got: {report['failures']}",
        )

    def test_missing_audio_fails_by_default(self) -> None:
        video = self.dir / "no-audio.mp4"
        make_video(video, video_source="testsrc", audio_source=None)
        report, ok = self.inspect(video)
        self.assertFalse(ok, "missing audio must fail closed by default")
        self.assertIn("audio stream is missing", report["failures"])

    def test_missing_audio_can_be_allowed_by_policy(self) -> None:
        video = self.dir / "no-audio-allowed.mp4"
        make_video(video, video_source="testsrc", audio_source=None)
        policy = qa_video.QaPolicy(allow_missing_audio=True)
        report, ok = self.inspect(video, policy=policy)
        self.assertTrue(ok, report["failures"])
        self.assertIn("audio stream is missing", report["warnings"])

    def test_relaxed_black_policy_is_configurable_but_silence_still_fails(self) -> None:
        video = self.dir / "black-with-tone.mp4"
        make_video(video, video_source="color=c=black", audio_source="sine=frequency=440")
        relaxed = qa_video.QaPolicy(max_black_segment_seconds=60.0, max_black_ratio=1.1)
        report, ok = self.inspect(video, policy=relaxed)
        self.assertTrue(ok, report["failures"])
        self.assertTrue(
            any("black" in item for item in report["warnings"]),
            "relaxed black policy must still surface a warning",
        )
        # Same video under the default policy must fail on black coverage.
        report, ok = self.inspect(video)
        self.assertFalse(ok, "default policy must fail a fully black final")

    def test_fragmented_black_frames_cannot_evade_the_ratio_gate(self) -> None:
        # 0.45s black pulses every 0.5s: ~90% black overall, yet every segment
        # stays under the 0.5s blackdetect floor the gate previously relied on.
        video = self.dir / "strobe-black.mp4"
        command = [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "nullsrc=s=320x240:r=30:d=4,"
            "geq=lum='if(lt(mod(T,0.5),0.45),16,235)':cb=128:cr=128",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=4",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-shortest",
            str(video),
        ]
        subprocess.run(command, check=True, text=True, capture_output=True)
        report, ok = self.inspect(video)
        self.assertFalse(ok, "fragmented black frames must not evade the coverage gate")
        self.assertTrue(
            any("black frames cover" in item for item in report["failures"]),
            report["failures"],
        )

    def test_single_frame_black_flicker_at_60fps_fails(self) -> None:
        # Alternating black/white single frames at 60fps: 50% black overall,
        # each black run lasting one frame (~16.7ms) — below any detection
        # floor that sits above the frame duration.
        video = self.dir / "flicker-60fps.mp4"
        command = [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "nullsrc=s=320x240:r=60:d=4,"
            "geq=lum='if(mod(N,2),16,235)':cb=128:cr=128",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=4",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-shortest",
            str(video),
        ]
        subprocess.run(command, check=True, text=True, capture_output=True)
        report, ok = self.inspect(video)
        self.assertFalse(ok, "50% single-frame black flicker must fail the coverage gate")
        self.assertTrue(
            any("black frames cover" in item for item in report["failures"]),
            report["failures"],
        )

    def test_black_frame_with_caption_box_still_counts_as_black(self) -> None:
        # A failed background render that still draws the caption box leaves a
        # frame that is ~94% black. Frame-level black detection must not need
        # a near-perfectly black frame to notice.
        video = self.dir / "black-with-caption-box.mp4"
        command = [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=640x480:r=30:d=3,"
            "drawbox=x=20:y=380:w=300:h=60:color=white:t=fill",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-shortest",
            str(video),
        ]
        subprocess.run(command, check=True, text=True, capture_output=True)
        report, ok = self.inspect(video)
        self.assertFalse(ok, "a black frame carrying a caption box must still fail")
        self.assertTrue(
            any("black" in item for item in report["failures"]), report["failures"]
        )

    def test_letterboxed_content_is_not_treated_as_black(self) -> None:
        # Pillarboxed delivery (portrait content on a landscape canvas) has
        # large black margins but real content; it must keep passing.
        video = self.dir / "pillarboxed.mp4"
        command = [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=180x480:rate=30:duration=3,"
            "pad=width=640:height=480:x=230:y=0:color=black",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-shortest",
            str(video),
        ]
        subprocess.run(command, check=True, text=True, capture_output=True)
        report, ok = self.inspect(video)
        self.assertTrue(ok, f"pillarboxed content must not be flagged: {report['failures']}")

    def test_black_frame_with_side_bars_still_fails(self) -> None:
        # Decorative side bars cover only a small share of the frame but span
        # most of its height. Measuring only the area they bound would hide
        # the black between them.
        video = self.dir / "black-side-bars.mp4"
        command = [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=540x960:r=30:d=3,"
            "drawbox=x=70:y=305:w=60:h=350:color=white:t=fill,"
            "drawbox=x=410:y=305:w=60:h=350:color=white:t=fill",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-shortest",
            str(video),
        ]
        subprocess.run(command, check=True, text=True, capture_output=True)
        report, ok = self.inspect(video)
        self.assertFalse(ok, "a black frame framed by side bars must fail")
        self.assertTrue(
            any("black" in item for item in report["failures"]), report["failures"]
        )

    def test_audio_that_stops_partway_fails(self) -> None:
        # Total silent coverage alone lets this through: the audio dies 30%
        # in, leaving 70% silence, which is under the coverage threshold.
        video = self.dir / "audio-stops-partway.mp4"
        command = [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=30:duration=6",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=6",
            "-af",
            "volume=enable='gte(t,1.8)':volume=0",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-shortest",
            str(video),
        ]
        subprocess.run(command, check=True, text=True, capture_output=True)
        report, ok = self.inspect(video)
        self.assertFalse(ok, "audio that stops partway must fail")
        self.assertTrue(
            any("silent" in item for item in report["failures"]), report["failures"]
        )

    def make_audio_shaped_video(self, name: str, volume_expr: str, duration: float) -> Path:
        video = self.dir / name
        subprocess.run(
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"testsrc=size=320x240:rate=30:duration={duration}",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:sample_rate=48000:duration={duration}",
                "-af",
                f"volume='{volume_expr}':eval=frame",
                "-pix_fmt",
                "yuv420p",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-c:a",
                "aac",
                "-shortest",
                str(video),
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        return video

    def test_long_dead_air_fails_even_when_proportionally_small(self) -> None:
        # 20s delivery losing audio at 13s: 35% of the timeline, under any
        # proportional limit, but seven unbroken seconds of dead air.
        video = self.make_audio_shaped_video(
            "long-dead-air.mp4", "if(lt(t,13),0.3,0)", 20
        )
        report, ok = self.inspect(video)
        self.assertFalse(ok, "seven seconds of unbroken dead air must fail")
        self.assertTrue(
            any("unbroken" in item for item in report["failures"]), report["failures"]
        )

    def test_sparse_audio_fails_even_when_each_gap_is_short(self) -> None:
        # Sound for 1.5s out of every 8s: no single gap is large and total
        # coverage stays under the blanket limit, yet the delivery is silent
        # for four fifths of its length.
        video = self.make_audio_shaped_video(
            "sparse-audio.mp4", "if(lt(mod(t,8),1.5),0.3,0)", 24
        )
        report, ok = self.inspect(video)
        self.assertFalse(ok, "a mostly silent delivery must fail")
        self.assertTrue(
            any("carries sound" in item or "silent" in item for item in report["failures"]),
            report["failures"],
        )

    def test_short_clip_clipping_still_fails(self) -> None:
        video = self.dir / "short-clipping.mp4"
        subprocess.run(
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=320x240:rate=30:duration=0.3",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=997:sample_rate=48000:duration=0.3,volume=10",
                "-pix_fmt",
                "yuv420p",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-shortest",
                str(video),
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        report, ok = self.inspect(video)
        self.assertFalse(ok, "a clipping short clip must fail on peak level")
        self.assertTrue(
            any("clipping" in item for item in report["failures"]), report["failures"]
        )

    def test_normalised_dead_air_with_room_tone_fails(self) -> None:
        # The renderer normalises every final, which lifts the noise floor of
        # a dead passage far above any absolute silence threshold. Real
        # material always carries room tone, so this is the shape a truncated
        # soundtrack actually takes by the time QA sees it.
        raw = self.dir / "room-tone-raw.mp4"
        subprocess.run(
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=320x240:rate=25:duration=20",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=300:duration=4",
                "-f",
                "lavfi",
                "-i",
                "anoisesrc=color=white:amplitude=0.001:duration=16",
                "-filter_complex",
                "[1:a][2:a]concat=n=2:v=0:a=1[a]",
                "-map",
                "0:v",
                "-map",
                "[a]",
                "-pix_fmt",
                "yuv420p",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-c:a",
                "aac",
                "-t",
                "20",
                str(raw),
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        video = self.dir / "room-tone-normalised.mp4"
        subprocess.run(
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(raw),
                "-map",
                "0:v",
                "-map",
                "0:a",
                "-c:v",
                "copy",
                "-af",
                "loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000",
                "-c:a",
                "aac",
                str(video),
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        report, ok = self.inspect(video)
        self.assertFalse(
            ok, "normalisation must not disguise dead air as sound"
        )
        self.assertTrue(
            any("silent" in item for item in report["failures"]), report["failures"]
        )

    def test_digital_silence_tail_after_normalisation_fails(self) -> None:
        # Synthesised narration leaves exact digital zero between and after
        # phrases, which the loudness meter reports as "nan" rather than a
        # level. Those windows must count as dead air, not vanish from the
        # measurement.
        video = self.dir / "digital-silence-tail.mp4"
        subprocess.run(
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=320x180:r=30:d=20",
                "-f",
                "lavfi",
                "-i",
                "aevalsrc='0.2*sin(2*PI*220*t)*lt(t,6)':d=20:s=48000",
                "-shortest",
                "-af",
                "loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000",
                "-pix_fmt",
                "yuv420p",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-c:a",
                "aac",
                "-t",
                "20",
                str(video),
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        report, ok = self.inspect(video)
        self.assertFalse(ok, "a digital-silence tail must not vanish from the measurement")
        self.assertTrue(
            any("silent" in item for item in report["failures"]), report["failures"]
        )

    def test_audio_shorter_than_video_fails(self) -> None:
        # The meter stops when the audio stream ends, so the remaining time
        # is never measured. It is dead air, not absent time.
        video = self.dir / "short-audio-track.mp4"
        subprocess.run(
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=320x240:rate=30:duration=20",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=4",
                "-map",
                "0:v",
                "-map",
                "1:a",
                "-pix_fmt",
                "yuv420p",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-c:a",
                "aac",
                "-t",
                "20",
                str(video),
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        report, ok = self.inspect(video)
        self.assertFalse(ok, "an audio track shorter than the video must fail")
        self.assertGreater(report["silence"]["unmeasured_seconds"], 10)

    def test_barely_any_audio_track_fails(self) -> None:
        # An audio track of a few hundred milliseconds against a long video
        # yields too few windows to measure. Whether silence can be judged
        # must follow the delivery's length, not how much the meter read.
        video = self.dir / "tiny-audio-track.mp4"
        subprocess.run(
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=320x180:r=30:d=20",
                "-f",
                "lavfi",
                "-i",
                "aevalsrc=exprs=0.3*sin(2*PI*220*t):s=48000:d=0.5",
                "-af",
                "loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000",
                "-pix_fmt",
                "yuv420p",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-c:a",
                "aac",
                "-t",
                "20",
                str(video),
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        report, ok = self.inspect(video)
        self.assertFalse(ok, "half a second of audio in a 20s delivery must fail")
        self.assertIsNotNone(report["silence"], "the silence gate must not be skipped")

    def test_repeated_long_dropouts_fail(self) -> None:
        # Each silence stays under the per-run limits and total coverage stays
        # under the blanket limit, but the soundtrack keeps cutting out.
        video = self.make_audio_shaped_video(
            "repeated-dropouts.mp4", "if(lt(mod(t,10),4),0.3,0)", 30
        )
        report, ok = self.inspect(video)
        self.assertFalse(ok, "a soundtrack that keeps cutting out must fail")
        self.assertTrue(
            any("carries sound" in item or "silent" in item for item in report["failures"]),
            report["failures"],
        )

    def test_audio_running_past_the_picture_does_not_dilute_dead_air(self) -> None:
        # The container reports the longest stream, so an audio track that
        # outlives the picture stretches the timeline and thins out the
        # silence inside the delivery itself.
        video = self.dir / "audio-overhang.mp4"
        subprocess.run(
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=320x240:rate=30:duration=5",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=30",
                "-af",
                "volume=enable='lt(t,5)':volume=0",
                "-map",
                "0:v",
                "-map",
                "1:a",
                "-pix_fmt",
                "yuv420p",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-c:a",
                "aac",
                str(video),
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        report, ok = self.inspect(video)
        self.assertFalse(ok, "silence under the picture must not be diluted by an overhang")
        self.assertLess(
            report["media"]["duration_s"],
            10,
            "the delivery is as long as its picture, not its audio",
        )

    def test_understated_container_duration_cannot_silence_the_gates(self) -> None:
        # A container header claiming well under a second would put the
        # delivery below the length at which silence is judged, skipping the
        # gate entirely while the file still plays in full.
        source = self.dir / "honest.mp4"
        make_video(
            source,
            video_source="testsrc",
            audio_source="anullsrc=r=48000:cl=stereo",
            duration=3,
        )
        video = self.dir / "understated.mp4"
        video.write_bytes(source.read_bytes())
        data = bytearray(video.read_bytes())
        # Rewrite every 32-bit duration field in the movie headers to 0.5s.
        for atom, offset in (("mvhd", 16), ("mdhd", 16), ("tkhd", 20)):
            index = 0
            while True:
                index = data.find(atom.encode(), index + 1)
                if index < 0:
                    break
                scale_at = index + offset
                timescale = int.from_bytes(data[scale_at : scale_at + 4], "big")
                if 0 < timescale <= 1_000_000:
                    data[scale_at + 4 : scale_at + 8] = int(timescale // 2).to_bytes(4, "big")
        video.write_bytes(bytes(data))
        report, ok = self.inspect(video)
        self.assertFalse(ok, "a silent delivery must fail whatever its header claims")

    def test_a_second_video_stream_cannot_shorten_the_timeline(self) -> None:
        # Reading the first video stream as "the picture" lets a brief
        # decorative track stand in for a long delivery, shrinking every
        # ratio to whatever that track covers.
        source = self.dir / "dead-audio.mp4"
        subprocess.run(
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=320x240:rate=30:duration=20",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=20",
                "-af",
                "volume=enable='gte(t,2)':volume=0",
                "-pix_fmt",
                "yuv420p",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-c:a",
                "aac",
                str(source),
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        video = self.dir / "two-video-streams.mp4"
        subprocess.run(
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=red:s=320x240:r=1:d=1",
                "-i",
                str(source),
                "-map",
                "0:v",
                "-map",
                "1:v",
                "-map",
                "1:a",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "copy",
                str(video),
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        report, ok = self.inspect(video)
        self.assertFalse(ok, "a second picture makes the delivery ambiguous")
        self.assertTrue(
            any("ambiguous" in item for item in report["failures"]), report["failures"]
        )

    def make_half_black_video(self, name: str, extra: list[str] | None = None) -> Path:
        video = self.dir / name
        subprocess.run(
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "nullsrc=s=320x240:r=30:d=10,"
                "geq=lum='if(lt(mod(T,1),0.5),16,235)':cb=128:cr=128",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=10",
                *(extra or []),
                "-pix_fmt",
                "yuv420p",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-c:a",
                "aac",
                "-shortest",
                str(video),
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        return video

    def test_metadata_cannot_stretch_the_measured_timeline(self) -> None:
        # The decoded length is read from ffmpeg's progress output, and the
        # same stream also echoes the input's filename and metadata tags.
        # The renderer copies source metadata into deliveries, so a tag
        # shaped like a progress reading is attacker supplied.
        video = self.make_half_black_video(
            "tagged.mp4", ["-metadata", "comment=time=00:00:25"]
        )
        report, ok = self.inspect(video)
        self.assertFalse(ok, "a metadata tag must not stretch the timeline")
        self.assertLess(report["media"]["duration_s"], 15)

    def test_filename_cannot_stretch_the_measured_timeline(self) -> None:
        video = self.make_half_black_video("final time=00:00:25.mp4")
        report, ok = self.inspect(video)
        self.assertFalse(ok, "a filename must not stretch the timeline")
        self.assertLess(report["media"]["duration_s"], 15)

    def test_a_longer_second_stream_cannot_dilute_black_coverage(self) -> None:
        # Coverage is measured against the picture that decodes; a longer
        # decorative stream must not become the denominator.
        picture = self.make_half_black_video("mostly-black.mp4")
        video = self.dir / "diluted.mp4"
        subprocess.run(
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(picture),
                "-f",
                "lavfi",
                "-i",
                "color=c=red:s=320x240:r=30:d=40",
                "-map",
                "0:v",
                "-map",
                "1:v",
                "-map",
                "0:a",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "copy",
                str(video),
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        report, ok = self.inspect(video)
        self.assertFalse(ok, "a longer second stream must not dilute coverage")
        self.assertTrue(
            any("black" in item or "ambiguous" in item for item in report["failures"]),
            report["failures"],
        )

    def test_truncated_delivery_fails(self) -> None:
        # ffmpeg reports success on a truncated file, so a delivery that lost
        # half its length would otherwise be judged as a clean shorter one.
        source = self.dir / "full-length.mp4"
        subprocess.run(
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=320x240:rate=30:duration=10",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=10",
                "-pix_fmt",
                "yuv420p",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-c:a",
                "aac",
                # Header first, so a truncated copy still parses — which is
                # exactly the case that must not read as a shorter delivery.
                "-movflags",
                "+faststart",
                "-shortest",
                str(source),
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        video = self.dir / "truncated.mp4"
        data = source.read_bytes()
        video.write_bytes(data[: len(data) // 2])
        report, ok = self.inspect(video)
        self.assertFalse(ok, "a delivery that stops early must fail")
        self.assertTrue(
            any("truncated" in item for item in report["failures"]), report["failures"]
        )

    def test_ordinary_delivery_is_not_reported_as_truncated(self) -> None:
        # The truncation check compares two independent readings of length;
        # ordinary encodings must not drift far enough apart to trip it.
        for name, rate in (("cfr-30.mp4", "30"), ("cfr-2997.mp4", "30000/1001")):
            with self.subTest(name):
                video = self.dir / name
                subprocess.run(
                    [
                        FFMPEG,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-f",
                        "lavfi",
                        "-i",
                        f"testsrc=size=320x240:rate={rate}:duration=6",
                        "-f",
                        "lavfi",
                        "-i",
                        "sine=frequency=440:duration=6",
                        "-pix_fmt",
                        "yuv420p",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "ultrafast",
                        "-g",
                        "120",
                        "-c:a",
                        "aac",
                        "-movflags",
                        "+faststart",
                        "-shortest",
                        str(video),
                    ],
                    check=True,
                    text=True,
                    capture_output=True,
                )
                report, ok = self.inspect(video)
                self.assertTrue(ok, f"{name} must pass: {report['failures']}")

    def test_a_second_audio_track_is_ambiguous(self) -> None:
        video = self.dir / "two-audio-streams.mp4"
        subprocess.run(
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=320x240:rate=30:duration=3",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=stereo:d=3",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=3",
                "-map",
                "0:v",
                "-map",
                "1:a",
                "-map",
                "2:a",
                "-pix_fmt",
                "yuv420p",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-c:a",
                "aac",
                "-shortest",
                str(video),
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        report, ok = self.inspect(video)
        self.assertFalse(ok, "a second soundtrack makes the delivery ambiguous")
        self.assertTrue(
            any("ambiguous" in item for item in report["failures"]), report["failures"]
        )

    def test_clean_delivery_reports_no_dead_air(self) -> None:
        # Window counting must not invent a trailing gap on a delivery that
        # carries sound throughout.
        video = self.dir / "clean-audio.mp4"
        make_video(
            video, video_source="testsrc", audio_source="sine=frequency=440", duration=5
        )
        report, ok = self.inspect(video)
        self.assertTrue(ok, report["failures"])
        self.assertEqual(report["silence"]["silent_seconds"], 0.0)
        self.assertEqual(report["silence"]["unmeasured_seconds"], 0.0)

    def make_silent_delivery(self, name: str = "silent-delivery.mp4") -> Path:
        # What the renderer actually emits when the source has no audio: a
        # digital-silence bed, not an absent track.
        video = self.dir / name
        subprocess.run(
            [
                FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=8",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=8",
                "-af", "loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000",
                "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
                "-c:a", "aac", "-shortest", str(video),
            ],
            check=True, text=True, capture_output=True,
        )
        return video

    def test_silent_delivery_profile_passes_a_deliberately_silent_final(self) -> None:
        video = self.make_silent_delivery()
        report, ok = self.inspect(video)
        self.assertFalse(ok, "a silent final must fail without a declaration")

        policy = qa_video.QaPolicy.for_profile("silent_delivery", "b-roll loop, no score")
        report, ok = self.inspect(video, policy=policy)
        self.assertTrue(ok, report["failures"])
        self.assertEqual(report["profile"], "silent_delivery")
        self.assertEqual(report["intent"], "b-roll loop, no score")
        self.assertIn("allow_silent_delivery", report["relaxed_fields"])

    def test_long_pause_profile_passes_a_long_deliberate_pause(self) -> None:
        video = self.make_audio_shaped_video(
            "long-pause.mp4", "if(lt(t,6)+gt(t,20),0.3,0)", 30
        )
        report, ok = self.inspect(video)
        self.assertFalse(ok, "a fourteen second gap must fail without a declaration")
        policy = qa_video.QaPolicy.for_profile("long_pause_delivery", "documentary pacing")
        report, ok = self.inspect(video, policy=policy)
        self.assertTrue(ok, report["failures"])

    def test_no_profile_relaxes_a_damaged_delivery(self) -> None:
        # Whatever the delivery declares itself to be, these say the file is
        # broken rather than deliberate.
        black = self.dir / "declared-black.mp4"
        make_video(black, video_source="color=c=black", audio_source="sine=frequency=440")
        multi = self.dir / "declared-two-audio.mp4"
        subprocess.run(
            [
                FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=3",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                "-f", "lavfi", "-i", "sine=frequency=880:duration=3",
                "-map", "0:v", "-map", "1:a", "-map", "2:a",
                "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
                "-c:a", "aac", "-shortest", str(multi),
            ],
            check=True, text=True, capture_output=True,
        )
        for profile in ("silent_delivery", "long_pause_delivery"):
            policy = qa_video.QaPolicy.for_profile(profile, "declared")
            for label, video in (("black", black), ("two soundtracks", multi)):
                with self.subTest(f"{profile}/{label}"):
                    _report, ok = self.inspect(video, policy=policy)
                    self.assertFalse(ok, f"{label} must fail under {profile}")

    def test_silent_delivery_does_not_excuse_a_soundtrack_that_stops(self) -> None:
        # Declaring a delivery silent must not turn a truncated soundtrack
        # into an acceptable one: it carried sound, then lost it.
        video = self.make_audio_shaped_video(
            "sound-then-gone.mp4", "if(lt(t,4),0.3,0)", 20
        )
        policy = qa_video.QaPolicy.for_profile("silent_delivery", "declared silent")
        report, ok = self.inspect(video, policy=policy)
        self.assertFalse(ok, "a soundtrack that stops is damage, not intent")
        self.assertTrue(
            any("silent" in item for item in report["failures"]), report["failures"]
        )

    def test_profile_requires_a_stated_intent(self) -> None:
        for profile in ("silent_delivery", "long_pause_delivery"):
            with self.assertRaises(ValueError):
                qa_video.QaPolicy.for_profile(profile, "   ")
        with self.assertRaises(ValueError):
            qa_video.QaPolicy.for_profile("anything_goes", "x")
        self.assertEqual(qa_video.QaPolicy.for_profile("strict").profile, "strict")

    def test_short_call_to_action_tail_is_not_flagged(self) -> None:
        # A ten second clip closing on a four second silent card is a normal
        # delivery, not a dropout.
        video = self.make_audio_shaped_video(
            "cta-tail.mp4", "if(lt(t,6),0.3,0)", 10
        )
        report, ok = self.inspect(video)
        self.assertTrue(ok, f"a short silent outro must pass: {report['failures']}")

    def test_narration_with_pauses_is_not_flagged(self) -> None:
        # Repeated short gaps are normal pacing, not a dropout.
        video = self.dir / "narration-pauses.mp4"
        command = [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=30:duration=6",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=6",
            "-af",
            "volume=enable='between(mod(t,2),1,2)':volume=0",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-shortest",
            str(video),
        ]
        subprocess.run(command, check=True, text=True, capture_output=True)
        report, ok = self.inspect(video)
        self.assertTrue(ok, f"paced narration must not be flagged: {report['failures']}")

    def test_moderately_dark_contain_delivery_passes(self) -> None:
        # Contain fit pads to canvas in a near-black tone; a dark but real
        # picture inside those bars must still pass.
        video = self.dir / "contain-dark.mp4"
        command = [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=30:duration=3,"
            "eq=brightness=-0.32:contrast=0.6,"
            "scale=360:640:force_original_aspect_ratio=decrease,"
            "pad=360:640:(ow-iw)/2:(oh-ih)/2:color=0x171512",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-shortest",
            str(video),
        ]
        subprocess.run(command, check=True, text=True, capture_output=True)
        report, ok = self.inspect(video)
        self.assertTrue(
            ok, f"dark content inside contain padding must pass: {report['failures']}"
        )

    def test_audio_that_stops_after_the_opening_fails(self) -> None:
        # Integrated loudness is gated and ignores silence, so a final whose
        # narration was truncated still measures a healthy level.
        video = self.dir / "audio-drops-out.mp4"
        command = [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=30:duration=6",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=6",
            "-af",
            "volume=enable='gte(t,0.3)':volume=0",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-shortest",
            str(video),
        ]
        subprocess.run(command, check=True, text=True, capture_output=True)
        report, ok = self.inspect(video)
        self.assertFalse(ok, "audio that stops after the opening must fail")
        self.assertTrue(
            any("silent for" in item for item in report["failures"]), report["failures"]
        )

    def test_very_short_audible_clip_is_not_failed_for_loudness(self) -> None:
        # EBU R128 integrates over 400ms and reports -70 LUFS below that.
        video = self.dir / "very-short.mp4"
        make_video(
            video,
            video_source="testsrc",
            audio_source="sine=frequency=440",
            duration=0.3,
        )
        report, ok = self.inspect(video)
        self.assertTrue(ok, f"a 0.3s audible clip must not fail: {report['failures']}")

    def test_short_fully_black_video_fails(self) -> None:
        video = self.dir / "short-black.mp4"
        make_video(
            video,
            video_source="color=c=black",
            audio_source="sine=frequency=440",
            duration=0.4,
        )
        report, ok = self.inspect(video)
        self.assertFalse(ok, "a fully black clip under 0.5s must still fail")

    def test_non_finite_policy_values_are_rejected(self) -> None:
        for field in (
            "max_black_segment_seconds",
            "max_black_ratio",
            "min_integrated_lufs",
            "max_true_peak_dbfs",
        ):
            for bad in (float("nan"), float("inf"), float("-inf")):
                with self.assertRaises(ValueError, msg=f"{field}={bad}"):
                    qa_video.QaPolicy(**{field: bad})

        video = self.dir / "nan-flags.mp4"
        make_video(video, video_source="color=c=black", audio_source=None)
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS_DIR / "qa_video.py"),
                "--video",
                str(video),
                "--report",
                str(self.dir / "nan-report.json"),
                "--contact",
                str(self.dir / "nan-contact.png"),
                "--max-black-segment-seconds",
                "nan",
                "--max-black-ratio",
                "nan",
                "--allow-missing-audio",
            ],
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0, "NaN thresholds must not produce a pass")
        self.assertNotIn('"status": "pass"', result.stdout)

    def test_true_peak_clipping_and_unmeasured_peak_fail(self) -> None:
        video = self.dir / "peak-check.mp4"
        make_video(video, video_source="testsrc", audio_source="sine=frequency=440")
        from unittest.mock import patch

        with patch.object(
            qa_video, "loudness", return_value={"integrated_lufs": -14.0, "true_peak_dbfs": 0.5}
        ):
            report, ok = self.inspect(video)
        self.assertFalse(ok)
        self.assertTrue(any("clipping" in item for item in report["failures"]), report["failures"])

        with patch.object(qa_video, "loudness", return_value={"integrated_lufs": -14.0}):
            report, ok = self.inspect(video)
        self.assertFalse(ok)
        self.assertTrue(
            any("true peak could not be measured" in item for item in report["failures"]),
            report["failures"],
        )

    def test_cli_exit_codes_and_flags(self) -> None:
        bad = self.dir / "cli-black-silent.mp4"
        make_video(bad, video_source="color=c=black", audio_source="anullsrc=r=48000:cl=stereo")
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS_DIR / "qa_video.py"),
                "--video",
                str(bad),
                "--report",
                str(self.dir / "cli-report.json"),
                "--contact",
                str(self.dir / "cli-contact.png"),
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "fail")

        good = self.dir / "cli-black-tone.mp4"
        make_video(good, video_source="color=c=black", audio_source="sine=frequency=440")
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS_DIR / "qa_video.py"),
                "--video",
                str(good),
                "--report",
                str(self.dir / "cli-relaxed-report.json"),
                "--contact",
                str(self.dir / "cli-relaxed-contact.png"),
                "--max-black-segment-seconds",
                "60",
                "--max-black-ratio",
                "1.1",
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
