"""Phase 5c: v2 Studio-edit rejection guard and adaptive pipeline-lag stride.

Covers two Codex-review fixes layered on the v1/v2 SFX delivery pipeline:

* ``stage_phase0d_sfx`` now rejects, at the producer entry point, any
  project that mixes ``catalog_profile: "v2"`` with existing Studio
  ``audio_event_edits`` -- instead of silently building an incomplete
  starter-asset path map that can only remix v1's seven fixed assets.
  Studio edits remain fully supported under ``catalog_profile: "v1"``.
* ``_estimate_dialogue_pipeline_lag`` now samples its safe (non-SFX-cue)
  dialogue window with an adaptive stride, so a long real SFX asset that
  eats most of a short final clip's untouched span no longer starves the
  minimum-evidence floor of independent measurements it would have met at
  the default stride against a short procedural asset.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import contract_registry  # noqa: E402
import delivery_envelope  # noqa: E402
import editor_server  # noqa: E402
import sfx_delivery  # noqa: E402
from render_editor_timeline import stage_phase0d_sfx  # noqa: E402


def _phase0d_state() -> dict:
    return {
        "canvas": {"width": 1080, "height": 1920, "fps": 30},
        "segments": [{"source_start": 0.0, "source_end": 2.0}],
        "overlays": [{
            "id": "card", "type": "image", "source": "assets/card.png",
            "start": 0.0, "end": 1.0, "visible": True,
            "style": {"width": 50, "x": 50, "y": 50},
        }],
    }


def _phase0d_evidence() -> dict:
    return {
        "schema_version": 1,
        "duration_s": 6.0,
        "items": [{
            "id": "title-1",
            "start": "0.20",
            "end": "0.50",
            "kind": "title",
            "component_id": "title-lockup",
            "motion": {
                "requested": "slide-up",
                "delivered": "slide-up",
                "faithful": True,
                "status": "native",
            },
        }],
    }


class V2CatalogStudioEditRejectionTests(unittest.TestCase):
    """catalog_profile v2 must reject Studio audio-event edits up front."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="phase5c-v2-reject-")
        self.project = Path(self.temp.name)
        (self.project / "source").mkdir()
        (self.project / "assets").mkdir()
        (self.project / "working").mkdir()
        (self.project / "source/source.mp4").write_bytes(b"source")
        (self.project / "assets/card.png").write_bytes(b"image")
        self.state = _phase0d_state()
        self.evidence = _phase0d_evidence()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_v2_catalog_with_studio_edits_is_rejected_before_staging(self) -> None:
        self.state["director_style"] = "kinetic-explainer"
        self.state["audio_event_edits"] = {
            "schema_version": 1,
            "source_render_id": "source-render",
            "source_plan_sha256": "a" * 64,
            "source_timeline_revision": "b" * 64,
            "events": [],
        }
        with patch("sfx_delivery.stage_multi_event_delivery") as staged:
            with self.assertRaisesRegex(
                ValueError,
                r"Studio audio event edits are not supported under catalog_profile v2",
            ):
                stage_phase0d_sfx(
                    self.project, self.state, "render-id",
                    self.project / "render-stage", self.evidence,
                )
        staged.assert_not_called()

    def test_v2_catalog_without_studio_edits_still_stages(self) -> None:
        self.state["director_style"] = "kinetic-explainer"

        def fake_stage(root, visual_evidence, timeline_revision, cut_hash, **kwargs):
            root = Path(root)
            root.mkdir(parents=True, exist_ok=True)
            plan_path = root / delivery_envelope.STAGE_FILENAMES["audio_event_plan"]
            catalog_path = root / delivery_envelope.STAGE_FILENAMES["audio_catalog"]
            stem_path = root / delivery_envelope.STAGE_FILENAMES["sfx_stem"]
            plan_path.write_text("{}", encoding="utf-8")
            catalog_path.write_text("{}", encoding="utf-8")
            stem_path.write_bytes(b"stem")
            return plan_path, catalog_path, stem_path

        for label, edits in (("missing key", None), ("explicit none", None)):
            with self.subTest(label=label):
                if "audio_event_edits" in self.state:
                    del self.state["audio_event_edits"]
                if label == "explicit none":
                    self.state["audio_event_edits"] = edits
                stage = self.project / f"render-stage-{label.replace(' ', '-')}"
                with patch(
                    "sfx_delivery.stage_multi_event_delivery", side_effect=fake_stage
                ) as staged:
                    plan_path, catalog_path, stem_path = stage_phase0d_sfx(
                        self.project, self.state, "render-id", stage, self.evidence,
                    )
                staged.assert_called_once()
                self.assertEqual(staged.call_args.kwargs.get("catalog_profile"), "v2")
                self.assertTrue(plan_path.is_file())
                self.assertTrue(catalog_path.is_file())
                self.assertTrue(stem_path.is_file())

    def test_v1_catalog_with_studio_edits_still_resolves(self) -> None:
        evidence = self.evidence
        self.state.update({"schema_version": 2, "project_id": "phase5c-v1-regression"})
        base_revision = editor_server.editor_base_state_revision(self.state)
        cut_hash = sfx_delivery.effective_cut_map_sha256(self.project, self.state)
        source_dir = self.project / "source-plan"
        source_plan_path, _catalog, _stem = sfx_delivery.stage_multi_event_delivery(
            source_dir, evidence, base_revision, cut_hash
        )
        source_plan = json.loads(source_plan_path.read_text(encoding="utf-8"))
        source_event = source_plan["events"][0]
        edits = {
            "schema_version": 1,
            "source_render_id": "source-render",
            "source_plan_sha256": hashlib.sha256(source_plan_path.read_bytes()).hexdigest(),
            "source_timeline_revision": base_revision,
            "events": [{
                "id": source_event["id"],
                "source_event_sha256": contract_registry.canonical_hash(source_event),
                "event_start_sample": source_event["event_start_sample"],
                "gain_db": -18,
            }],
        }
        self.state["audio_event_edits"] = edits
        stage = self.project / "render-stage"
        with patch(
            "render_editor_timeline.resolve_audio_event_source",
            return_value=("source-render", source_plan, edits["source_plan_sha256"]),
        ):
            plan_path, _catalog_path, _stem_path = stage_phase0d_sfx(
                self.project, self.state, "render-id", stage, evidence
            )
        resolved = json.loads(plan_path.read_text(encoding="utf-8"))
        self.assertEqual(resolved["events"][0]["gain_db"], -18)
        self.assertEqual(
            resolved["studio_edits_sha256"], contract_registry.canonical_hash(edits)
        )


def _seeded_noise(total: int, seed: int, amplitude: float) -> list[tuple[float, float]]:
    samples: list[tuple[float, float]] = []
    for _ in range(total):
        seed = (1664525 * seed + 1013904223) & 0xFFFFFFFF
        value = ((((seed >> 8) / 16777215.0) * 2.0) - 1.0) * amplitude
        samples.append((value, value))
    return samples


class PipelineLagAdaptiveStrideTests(unittest.TestCase):
    """Long real cues must not starve pipeline-lag evidence sampling."""

    def test_subtract_intervals_merges_and_clips(self) -> None:
        self.assertEqual(
            sfx_delivery._subtract_intervals(0, 100, [(20, 40), (35, 50), (90, 200)]),
            [(0, 20), (50, 90)],
        )
        self.assertEqual(sfx_delivery._subtract_intervals(0, 100, []), [(0, 100)])
        self.assertEqual(sfx_delivery._subtract_intervals(0, 100, [(-10, 200)]), [])
        self.assertEqual(sfx_delivery._subtract_intervals(50, 50, [(0, 100)]), [])

    def test_pipeline_lag_adaptive_stride_recovers_short_safe_window(self) -> None:
        max_lag_samples = 512
        event_start_sample = 1024
        duration_samples = 2000
        total = 5160  # leaves exactly 600 safe samples after the 2x-lag exclusion

        dialogue_samples = _seeded_noise(total, 0xC0FFEE, 10 ** (-20.0 / 20.0))
        stem_samples = [(0.0, 0.0)] * total
        cue_seed = 0xBADC0FFE
        cue_amplitude = 10 ** (-26.0 / 20.0)
        for offset in range(duration_samples):
            cue_seed = (1664525 * cue_seed + 1013904223) & 0xFFFFFFFF
            noise = (((cue_seed >> 8) / 16777215.0) * 2.0) - 1.0
            value = cue_amplitude * noise
            stem_samples[event_start_sample + offset] = (value, value)
        output_samples = [
            (d[0] + s[0], d[1] + s[1]) for d, s in zip(dialogue_samples, stem_samples)
        ]
        event = {
            "event_start_sample": event_start_sample,
            "duration_samples": duration_samples,
        }

        lag_match = sfx_delivery._estimate_dialogue_pipeline_lag(
            output_samples, dialogue_samples, [event], max_lag_samples=max_lag_samples,
        )
        self.assertIsNotNone(lag_match)
        correlation, found_lag, _dialogue_gain = lag_match
        self.assertEqual(found_lag, 0)
        self.assertGreaterEqual(correlation, sfx_delivery.CANDIDATE_CORRELATION_THRESHOLD)

    def test_pipeline_lag_still_requires_minimum_evidence_floor(self) -> None:
        max_lag_samples = 512
        event_start_sample = 1024
        duration_samples = 2000
        total = 4960  # leaves only 400 safe samples: below the floor even at stride 1

        dialogue_samples = [(0.0, 0.0)] * total
        output_samples = [(0.0, 0.0)] * total
        event = {
            "event_start_sample": event_start_sample,
            "duration_samples": duration_samples,
        }

        lag_match = sfx_delivery._estimate_dialogue_pipeline_lag(
            output_samples, dialogue_samples, [event], max_lag_samples=max_lag_samples,
        )
        self.assertIsNone(lag_match)


if __name__ == "__main__":
    unittest.main()
