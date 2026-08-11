"""Real FFmpeg/public-CLI tracer for the Phase 0d kinetic SFX route."""
from __future__ import annotations

import hashlib
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS))

import delivery_envelope  # noqa: E402
import render_editor_timeline as renderer  # noqa: E402
import sfx_delivery  # noqa: E402
import visual_quality  # noqa: E402
import qa_video  # noqa: E402
from editor_server import gate_revision  # noqa: E402


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs local ffmpeg")
class Phase0dSfxIntegrationTests(unittest.TestCase):
    """Exercise the public final route against actual rendered media bytes."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="phase0d-sfx-integration-")
        self.project = Path(self.tmp.name) / "project"
        for name in ("source", "working", "renders"):
            (self.project / name).mkdir(parents=True, exist_ok=True)
        self.source = self.project / "source" / "talking-head.mp4"
        self._make_source()
        self.source_sha = self.sha256(self.source)
        self.state = {
            "schema_version": 2,
            "project_id": "phase0d-real",
            "source_sha256": self.source_sha,
            "segments": [{
                "id": "segment-phase0d-0001",
                "source_start": 0.0,
                "source_end": 2.4,
                "origin": "default_full_source",
            }],
            "variants": [],
            "rights": {"asserted": False, "assertion_revision": None},
            "canvas": {
                "platform_id": "instagram-reels", "width": 360, "height": 640,
                "fps": 30, "fit": "cover",
            },
            "director_style": "kinetic-explainer",
            "qa_policy": {"profile": "strict"},
            "caption_defaults": {},
            "highlights": [],
            "asset_digests": {},
            "overlays": [{
                "id": "phase0d-title", "type": "title", "text": "REAL MIX",
                "start": 0.5, "end": 1.3, "visible": True,
                "style": {
                    "font_size": 52, "color": "#FFFFFF", "stroke_width": 2,
                    "stroke_color": "#111111", "x": 50, "y": 45,
                    "animation": "pop",
                },
            }],
        }
        self.write_json("working/editor_state.json", self.state)
        manifest = {
            "schema_version": 1,
            "project_id": "phase0d-real",
            "source": {
                "staged_path": "source/talking-head.mp4", "duration_s": 2.4,
                "sha256": self.source_sha, "has_audio": True,
            },
            "approvals": {"timeline": {
                "approved": True,
                "state_revision": gate_revision(self.project, "timeline", self.state),
            }},
        }
        self.write_json("project.json", manifest)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _make_source(self) -> None:
        # A quiet low-frequency dialogue stand-in makes a high-frequency tick
        # observable without relying on any external media or provider.
        result = subprocess.run([
            renderer.ffmpeg_path(), "-y",
            "-f", "lavfi", "-i", "testsrc2=s=360x640:r=30:d=2.4",
            "-f", "lavfi", "-i", "sine=f=160:r=48000:d=2.4",
            "-filter:a", "volume=0.025",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
            str(self.source),
        ], text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def write_json(self, relative: str, payload: dict) -> None:
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def public_render(self, output: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run([
            sys.executable, str(SCRIPTS / "render_editor_timeline.py"),
            "--project-dir", str(self.project), "--output", str(output), "--quality", "final",
        ], text=True, capture_output=True, timeout=55)

    def decode_f32le(self, video: Path, start: float, duration: float) -> list[float]:
        result = subprocess.run([
            renderer.ffmpeg_path(), "-v", "error", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
            "-i", str(video), "-vn", "-ac", "1", "-ar", "48000", "-f", "f32le", "-",
        ], capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        return list(struct.unpack("<%df" % (len(result.stdout) // 4), result.stdout))

    @staticmethod
    def rms(values: list[float]) -> float:
        return (sum(value * value for value in values) / max(len(values), 1)) ** 0.5

    def assert_no_staging_residue(self) -> None:
        staging = self.project / "working/delivery_envelopes/.staging"
        if not staging.exists():
            return
        self.assertEqual(
            [item for item in staging.iterdir() if item.name != ".locks"], [],
            "failed/stale direct-render staging residue",
        )

    def test_public_cli_final_mixes_and_publishes_canonical_sfx_evidence(self) -> None:
        output = self.project / "renders" / "phase0d-final.mp4"
        result = self.public_render(output)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(output.is_file())

        render_id = renderer.direct_final_render_id(self.state, output)
        envelope_path = delivery_envelope.finalized_path(self.project, render_id)
        envelope = json.loads(envelope_path.read_text("utf-8"))
        self.assertEqual(envelope["state"], "finalized")
        self.assertIsNotNone(envelope["timeline"]["cut_map_sha256"])
        artifacts = envelope["artifacts"]
        for name in ("audio_event_plan", "audio_catalog", "sfx_stem"):
            self.assertIsNotNone(artifacts[name])
            canonical = self.project / artifacts[name]["path"]
            self.assertTrue(canonical.is_file(), name)
            self.assertEqual(artifacts[name]["sha256"], self.sha256(canonical), name)

        report = json.loads((self.project / artifacts["qa_report"]["path"]).read_text("utf-8"))
        self.assertEqual(report["schema_version"], 3)
        sfx_report = report["sfx_delivery"]
        self.assertEqual(sfx_report["source"], "independent_sfx_evidence")
        self.assertEqual(sfx_report["status"], "pass")
        self.assertEqual(sfx_report["candidate_output_sha256"], self.sha256(output))
        self.assertEqual(
            {
                sfx_report["output_audio_evidence"]["sample_rate"],
                sfx_report["output_audio_evidence"]["channels"],
                sfx_report["output_audio_evidence"]["sample_width_bytes"],
            },
            {48000, 2, 4},
        )
        candidate_cues = [
            cue for cue in sfx_report["observed_cue_evidence"]
            if cue.get("evidence_source") == "candidate_output_audio"
        ]
        self.assertEqual(len(candidate_cues), 1)
        self.assertGreaterEqual(
            candidate_cues[0]["correlation"],
            sfx_delivery.CANDIDATE_CORRELATION_THRESHOLD,
        )
        self.assertTrue(any(
            cue.get("evidence_source") == "candidate_output_audio"
            and cue.get("status") == "pass"
            for cue in sfx_report["observed_cue_evidence"]
        ))
        qa_video.validate_sfx_report(sfx_report)
        stem = sfx_delivery.decode_s24le_wav(self.project / artifacts["sfx_stem"]["path"])
        self.assertEqual((stem.sample_rate, stem.channels, stem.sample_width), (48000, 2, 3))

        probe = subprocess.run([
            "ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
            "stream=sample_rate", "-of", "default=nokey=1:noprint_wrappers=1", str(output),
        ], text=True, capture_output=True, timeout=15)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(probe.stdout.strip(), "48000")

        # The cue is mixed in the public MP4, not merely passed in its private
        # stem.  Compare an onset window with equally-long low-frequency-only
        # dialogue away from the cue; the procedural tick creates a clear,
        # but intentionally threshold-light, energy increase.
        plan = json.loads((self.project / artifacts["audio_event_plan"]["path"]).read_text("utf-8"))
        onset = plan["events"][0]["trigger_onset_sample"] / 48000
        cue_rms = self.rms(self.decode_f32le(output, onset, 0.25))
        remote_rms = self.rms(self.decode_f32le(output, 1.75, 0.25))
        self.assertGreater(cue_rms, remote_rms * 1.08)
        self.assert_no_staging_residue()

    def test_truncated_or_extended_candidate_cannot_reuse_full_duration_sidecars(self) -> None:
        """Final-domain SFX proof must bind candidate duration, not only its cue."""
        output = self.project / "renders" / "phase0d-duration-source.mp4"
        result = self.public_render(output)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        render_id = renderer.direct_final_render_id(self.state, output)
        envelope = json.loads(
            delivery_envelope.finalized_path(self.project, render_id).read_text("utf-8")
        )
        artifacts = envelope["artifacts"]
        visual_path = self.project / artifacts["visual_evidence"]["path"]
        plan_path = self.project / artifacts["audio_event_plan"]["path"]
        catalog_path = self.project / artifacts["audio_catalog"]["path"]
        stem_path = self.project / artifacts["sfx_stem"]["path"]
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        # Recreate well-formed private-role fixtures solely to keep this test
        # focused on candidate duration binding after public delivery has
        # correctly cleaned the genuine same-render private WAVs.
        dialogue_priority = self.project / "working/dialogue_priority_dialogue.wav"
        sfx_priority = self.project / "working/dialogue_priority_sfx.wav"
        for path, source_filter in (
            (dialogue_priority, "sine=f=160:r=48000:d=2.4,volume=0.0125"),
            (sfx_priority, "anullsrc=r=48000:cl=stereo:d=2.4"),
        ):
            fixture = subprocess.run([
                renderer.ffmpeg_path(), "-y", "-f", "lavfi", "-i", source_filter,
                "-ac", "2", "-ar", "48000", "-c:a", "pcm_s24le", str(path),
            ], text=True, capture_output=True, timeout=30)
            self.assertEqual(fixture.returncode, 0, fixture.stderr)
        for duration in (0.9, 1.0, 1.5):
            with self.subTest(duration=duration):
                candidate = self.project / "renders" / f"candidate-{duration:.1f}.mp4"
                truncated = subprocess.run([
                    renderer.ffmpeg_path(), "-y", "-i", str(output),
                    "-t", f"{duration:.1f}", "-c", "copy", str(candidate),
                ], text=True, capture_output=True, timeout=30)
                self.assertEqual(truncated.returncode, 0, truncated.stderr)
                qa = subprocess.run([
                    sys.executable, str(SCRIPTS / "qa_video.py"),
                    "--video", str(candidate),
                    "--report", str(self.project / "working" / f"duration-{duration:.1f}.json"),
                    "--contact", str(self.project / "working" / f"duration-{duration:.1f}.png"),
                    "--visual-evidence", str(visual_path),
                    "--audio-event-plan", str(plan_path),
                    "--audio-catalog", str(catalog_path),
                    "--sfx-stem", str(stem_path),
                    "--dialogue-priority-dialogue", str(dialogue_priority),
                    "--dialogue-priority-sfx", str(sfx_priority),
                    "--expected-timeline-revision", plan["timeline_revision"],
                    "--expected-cut-map-sha256", plan["cut_map_sha256"],
                ], text=True, capture_output=True, timeout=55)
                self.assertEqual(qa.returncode, 2, qa.stdout + qa.stderr)
                report = json.loads(
                    (self.project / "working" / f"duration-{duration:.1f}.json").read_text(
                        encoding="utf-8"
                    )
                )
                audio_evidence = report["sfx_delivery"]["output_audio_evidence"]
                self.assertEqual(
                    audio_evidence["expected_sample_count"],
                    plan["sfx_stem_sample_count"],
                )
                self.assertEqual(
                    audio_evidence["sample_count_delta"],
                    audio_evidence["sample_count"] - audio_evidence["expected_sample_count"],
                )
                self.assertEqual(
                    audio_evidence["sample_count_tolerance_samples"],
                    sfx_delivery.CANDIDATE_SAMPLE_COUNT_TOLERANCE,
                )
                self.assertGreater(
                    abs(audio_evidence["sample_count_delta"]),
                    audio_evidence["sample_count_tolerance_samples"],
                )

    def test_alignment_failure_after_real_stage_retracts_everything(self) -> None:
        output = self.project / "renders" / "prior-output.mp4"
        prior = b"prior bytes must survive failed atomic publication"
        output.write_bytes(prior)
        render_id = renderer.direct_final_render_id(self.state, output)
        original_stage = renderer.stage_phase0d_sfx

        def shifted_stage(*args, **kwargs):
            plan_path, catalog_path, stem_path = original_stage(*args, **kwargs)
            plan = json.loads(plan_path.read_text("utf-8"))
            event = plan["events"][0]
            asset = Path(stem_path).with_name("generated-soft-ui-tick.wav")
            shifted_start = event["event_start_sample"] + 3841
            decoded = sfx_delivery.write_one_cue_stem(
                stem_path, total_samples=plan["sfx_stem_sample_count"], asset_path=asset,
                event_start_sample=shifted_start, gain_db=event["gain_db"],
            )
            event["event_start_sample"] = shifted_start
            event["expected_transient_sample"] = shifted_start + event["asset_transient_anchor_sample"]
            plan["sfx_stem_sha256"] = self.sha256(stem_path)
            plan["sfx_stem_decoded_pcm_sha256"] = hashlib.sha256(decoded.pcm).hexdigest()
            plan_path.write_text(json.dumps(plan, sort_keys=True, separators=(",", ":")) + "\n", "utf-8")
            return plan_path, catalog_path, stem_path

        with patch.object(renderer, "stage_phase0d_sfx", side_effect=shifted_stage):
            with self.assertRaises(RuntimeError) as raised:
                renderer.render_project(self.project, output, "final")
        self.assertIn("sfx delivery", str(raised.exception).lower())
        self.assertEqual(output.read_bytes(), prior)
        self.assertFalse(delivery_envelope.finalized_path(self.project, render_id).exists())
        self.assertFalse((self.project / "working/audio_event_plans" / f"{render_id}.json").exists())
        self.assertFalse((self.project / "working/audio_catalogs" / f"{render_id}.json").exists())
        self.assertFalse((self.project / "working/sfx_stems" / f"{render_id}.wav").exists())
        self.assert_no_staging_residue()

    def test_dialogue_only_candidate_cannot_reuse_passing_sfx_sidecars(self) -> None:
        """RED: sidecar-only SFX proof must not pass an output with no cue."""
        candidate = self.project / "renders" / "candidate-no-sfx.mp4"
        # Keep the candidate comfortably inside ordinary dialogue QA levels;
        # only the missing SFX cue should make this case fail.
        boosted = subprocess.run([
            renderer.ffmpeg_path(), "-y", "-i", str(self.source),
            "-filter:a", "volume=20", "-ac", "2", "-ar", "48000",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
            str(candidate),
        ], text=True, capture_output=True, timeout=30)
        self.assertEqual(boosted.returncode, 0, boosted.stderr)
        raw_evidence = {
            "schema_version": 1,
            "source": "renderer_evidence_raw",
            "duration_s": 2.4,
            "motion_intensity": "high",
            "expected_visual_beat_count": 1,
            "items": [{
                "id": "phase0d-title",
                "start": 0.5,
                "end": 1.3,
                "kind": "title",
                "component_id": None,
                "style_pack_id": None,
                "font_evidence_required": True,
                "minimum_primary_font_px": 52.0,
                "motion": {
                    "requested": "pop",
                    "delivered": "pop",
                    "faithful": True,
                    "status": "native",
                },
            }],
        }
        visual_report = visual_quality.rendered_visual_quality_report(raw_evidence)
        visual_path = self.project / "working" / "visual-evidence.json"
        visual_path.write_text(json.dumps(visual_report), encoding="utf-8")
        stage = self.project / "working" / "sfx-stage"
        plan_path, catalog_path, stem_path = sfx_delivery.stage_one_cue_delivery(
            stage,
            raw_evidence,
            renderer.editor_state_revision(self.state),
            sfx_delivery.effective_cut_map_sha256(self.project, self.state),
        )
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        report_path = self.project / "working" / "no-sfx-qa.json"
        contact_path = self.project / "working" / "no-sfx-contact.png"
        result = subprocess.run([
            sys.executable,
            str(SCRIPTS / "qa_video.py"),
            "--video", str(candidate),
            "--report", str(report_path),
            "--contact", str(contact_path),
            "--visual-evidence", str(visual_path),
            "--audio-event-plan", str(plan_path),
            "--audio-catalog", str(catalog_path),
            "--sfx-stem", str(stem_path),
            "--expected-timeline-revision", plan["timeline_revision"],
            "--expected-cut-map-sha256", plan["cut_map_sha256"],
        ], text=True, capture_output=True, timeout=55)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        sfx_report = report["sfx_delivery"]
        self.assertEqual(sfx_report["status"], "fail")
        self.assertEqual(sfx_report["candidate_output_sha256"], self.sha256(candidate))
        self.assertEqual(sfx_report["output_audio_evidence"]["sample_rate"], 48000)
        self.assertEqual(sfx_report["output_audio_evidence"]["channels"], 2)
        self.assertFalse(any(
            cue.get("evidence_source") == "candidate_output_audio"
            and cue.get("status") == "pass"
            for cue in sfx_report.get("observed_cue_evidence", [])
        ))

    def test_candidate_replacement_cannot_reuse_old_sfx_report(self) -> None:
        """An old receipt must not bind a replacement candidate byte stream."""
        candidate = self.project / "renders" / "candidate-replacement.mp4"
        old_bytes = b"candidate-a"
        candidate.write_bytes(old_bytes)
        old_hash = hashlib.sha256(old_bytes).hexdigest()
        verification = {"candidate_output_sha256": old_hash}
        report_payload = {"sfx_delivery": {"candidate_output_sha256": old_hash}}
        candidate.write_bytes(b"candidate-b")
        with self.assertRaisesRegex(RuntimeError, "candidate output hash"):
            renderer.assert_sfx_candidate_binding(verification, report_payload, candidate)

    def test_white_noise_candidate_cannot_pass_by_lagged_correlation(self) -> None:
        """Broadband noise must not win the maximum-over-lags cue search."""
        candidate = self.project / "renders" / "candidate-white-noise.mp4"
        generated = subprocess.run([
            renderer.ffmpeg_path(), "-y", "-i", str(self.source),
            "-f", "lavfi", "-i",
            "anoisesrc=color=white:amplitude=0.1:duration=2.4:sample_rate=48000:seed=424242",
            "-map", "0:v:0", "-map", "1:a:0", "-ac", "2", "-ar", "48000",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", str(candidate),
        ], text=True, capture_output=True, timeout=30)
        self.assertEqual(generated.returncode, 0, generated.stderr)
        raw_evidence = {
            "schema_version": 1,
            "source": "renderer_evidence_raw",
            "duration_s": 2.4,
            "motion_intensity": "high",
            "expected_visual_beat_count": 1,
            "items": [{
                "id": "phase0d-title",
                "start": 0.5,
                "end": 1.3,
                "kind": "title",
                "component_id": None,
                "style_pack_id": None,
                "font_evidence_required": True,
                "minimum_primary_font_px": 52.0,
                "motion": {
                    "requested": "pop",
                    "delivered": "pop",
                    "faithful": True,
                    "status": "native",
                },
            }],
        }
        visual_report = visual_quality.rendered_visual_quality_report(raw_evidence)
        visual_path = self.project / "working" / "white-noise-visual-evidence.json"
        visual_path.write_text(json.dumps(visual_report), encoding="utf-8")
        stage = self.project / "working" / "white-noise-sfx-stage"
        plan_path, catalog_path, stem_path = sfx_delivery.stage_one_cue_delivery(
            stage,
            raw_evidence,
            renderer.editor_state_revision(self.state),
            sfx_delivery.effective_cut_map_sha256(self.project, self.state),
        )
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        result = subprocess.run([
            sys.executable, str(SCRIPTS / "qa_video.py"),
            "--video", str(candidate),
            "--report", str(self.project / "working" / "white-noise-qa.json"),
            "--contact", str(self.project / "working" / "white-noise-contact.png"),
            "--visual-evidence", str(visual_path),
            "--audio-event-plan", str(plan_path),
            "--audio-catalog", str(catalog_path),
            "--sfx-stem", str(stem_path),
            "--expected-timeline-revision", plan["timeline_revision"],
            "--expected-cut-map-sha256", plan["cut_map_sha256"],
        ], text=True, capture_output=True, timeout=55)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        report = json.loads(
            (self.project / "working" / "white-noise-qa.json").read_text(encoding="utf-8")
        )
        candidate_cue = next(
            cue for cue in report["sfx_delivery"]["observed_cue_evidence"]
            if cue.get("evidence_source") == "candidate_output_audio"
        )
        self.assertGreater(candidate_cue["correlation"], 0.08)
        self.assertLess(
            candidate_cue["correlation"],
            sfx_delivery.CANDIDATE_CORRELATION_THRESHOLD,
        )
        self.assertEqual(report["sfx_delivery"]["status"], "fail")

    def test_pink_and_brown_noise_candidates_fail_output_binding(self) -> None:
        raw_evidence = {
            "schema_version": 1,
            "source": "renderer_evidence_raw",
            "duration_s": 2.4,
            "motion_intensity": "high",
            "expected_visual_beat_count": 1,
            "items": [{
                "id": "phase0d-title",
                "start": 0.5,
                "end": 1.3,
                "kind": "title",
                "component_id": None,
                "style_pack_id": None,
                "font_evidence_required": True,
                "minimum_primary_font_px": 52.0,
                "motion": {
                    "requested": "pop",
                    "delivered": "pop",
                    "faithful": True,
                    "status": "native",
                },
            }],
        }
        visual_report = visual_quality.rendered_visual_quality_report(raw_evidence)
        revision = renderer.editor_state_revision(self.state)
        cut_hash = sfx_delivery.effective_cut_map_sha256(self.project, self.state)
        paths = sfx_delivery.stage_one_cue_delivery(
            self.project / "working" / "colored-noise-sfx-stage",
            raw_evidence,
            revision,
            cut_hash,
        )
        for color in ("pink", "brown"):
            with self.subTest(color=color):
                candidate = self.project / "renders" / f"candidate-{color}-noise.mp4"
                generated = subprocess.run([
                    renderer.ffmpeg_path(), "-y", "-i", str(self.source),
                    "-f", "lavfi", "-i",
                    f"anoisesrc=color={color}:amplitude=0.1:duration=2.4:sample_rate=48000:seed=424242",
                    "-map", "0:v:0", "-map", "1:a:0", "-ac", "2", "-ar", "48000",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", str(candidate),
                ], text=True, capture_output=True, timeout=30)
                self.assertEqual(generated.returncode, 0, generated.stderr)
                report = sfx_delivery.verify_delivery(
                    *paths,
                    visual_report,
                    revision,
                    cut_hash,
                    candidate_path=candidate,
                )
                candidate_cue = next(
                    cue for cue in report["observed_cue_evidence"]
                    if cue.get("evidence_source") == "candidate_output_audio"
                )
                self.assertEqual(report["status"], "fail")
                self.assertLess(
                    candidate_cue["correlation"],
                    sfx_delivery.CANDIDATE_CORRELATION_THRESHOLD,
                )
