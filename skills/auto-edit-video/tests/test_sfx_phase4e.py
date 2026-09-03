"""Phase 4e: port v1 candidate/dialogue-priority/studio-edit verification to v2.

Covers ``_verify_multi_event_delivery_v2`` binding by ``asset_id`` against
the frozen v2 catalog: candidate output correlation, dialogue-priority
ducking evidence, and fail-closed studio-edit rejection, plus a plan mixing
``content_hint`` and externally-licensed-asset events, and a byte-identical
check that v1's golden staging hashes are unchanged.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import wave
from pathlib import Path
from unittest.mock import patch
import unittest

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import sfx_delivery  # noqa: E402
from test_sfx_delivery_v2 import _multi_event_visual_evidence  # noqa: E402

REVISION = "a" * 64
CUT_HASH = "b" * 64
SOURCE_STATE_REVISION = "e" * 64


def _write_priority_wav(path: Path, total_samples: int, level_db: float) -> None:
    amplitude = 10 ** (level_db / 20.0)
    sample = sfx_delivery._pack_s24(amplitude)
    frame = sample + sample
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(3)
        wav.setframerate(48000)
        wav.writeframes(frame * total_samples)


def _stage_v2(root: Path, evidence: dict, **kwargs) -> tuple[Path, Path, Path]:
    return sfx_delivery.stage_multi_event_delivery(
        root, evidence, REVISION, CUT_HASH, catalog_profile="v2", **kwargs
    )


class V2CandidateAndDialoguePriorityBindingTests(unittest.TestCase):
    def test_v2_candidate_and_dialogue_priority_binding_passes(self) -> None:
        evidence = _multi_event_visual_evidence()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path, catalog_path, stem_path = _stage_v2(
                root, evidence, include_external=False
            )
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            total_samples = plan["sfx_stem_sample_count"]
            dialogue_path = root / "dialogue_priority_dialogue.wav"
            sfx_path = root / "dialogue_priority_sfx.wav"
            _write_priority_wav(dialogue_path, total_samples, -38.0)
            _write_priority_wav(sfx_path, total_samples, -52.0)
            candidate_samples = [(0.0, 0.0)] * total_samples
            candidate_audio = sfx_delivery.DecodedWav(
                48000, 2, 4, b"\0" * (total_samples * 8), candidate_samples
            )
            candidate_path = root / "candidate.mp4"
            candidate_bytes = b"v2-candidate-binding-pass"
            candidate_path.write_bytes(candidate_bytes)

            with (
                patch.object(
                    sfx_delivery,
                    "_decode_candidate_audio_bytes",
                    return_value=candidate_audio,
                ),
                patch.object(
                    sfx_delivery,
                    "_estimate_dialogue_pipeline_lag",
                    return_value=(1.0, 0, 1.0),
                ),
                patch.object(
                    sfx_delivery,
                    "_cue_template_partial_correlation",
                    return_value=(0.9, 0, True),
                ),
            ):
                report = sfx_delivery.verify_delivery(
                    plan_path, catalog_path, stem_path,
                    evidence, REVISION, CUT_HASH,
                    candidate_path=candidate_path,
                    dialogue_priority_dialogue_path=dialogue_path,
                    dialogue_priority_sfx_path=sfx_path,
                )

            self.assertEqual(report["status"], "pass", report["failures"])
            self.assertEqual(report["schema_version"], 2)
            self.assertEqual(
                report["candidate_output_sha256"], hashlib.sha256(candidate_bytes).hexdigest()
            )
            self.assertIsNotNone(report["dialogue_priority_evidence"])
            self.assertEqual(len(report["observed_cue_evidence"]), len(plan["events"]))
            self.assertTrue(all(cue["status"] == "pass" for cue in report["observed_cue_evidence"]))
            self.assertEqual(report["delivered_event_count"], len(plan["events"]))

    def test_v2_candidate_tampering_fails(self) -> None:
        evidence = _multi_event_visual_evidence()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path, catalog_path, stem_path = _stage_v2(
                root, evidence, include_external=False
            )
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            total_samples = plan["sfx_stem_sample_count"]
            dialogue_path = root / "dialogue_priority_dialogue.wav"
            sfx_path = root / "dialogue_priority_sfx.wav"
            _write_priority_wav(dialogue_path, total_samples, -38.0)
            _write_priority_wav(sfx_path, total_samples, -52.0)
            candidate_samples = [(0.0, 0.0)] * total_samples
            candidate_audio = sfx_delivery.DecodedWav(
                48000, 2, 4, b"\0" * (total_samples * 8), candidate_samples
            )
            candidate_path = root / "candidate.mp4"
            candidate_path.write_bytes(b"v2-candidate-tampered-output")

            with (
                patch.object(
                    sfx_delivery,
                    "_decode_candidate_audio_bytes",
                    return_value=candidate_audio,
                ),
                patch.object(
                    sfx_delivery,
                    "_estimate_dialogue_pipeline_lag",
                    return_value=(1.0, 0, 1.0),
                ),
                patch.object(
                    sfx_delivery,
                    "_cue_template_partial_correlation",
                    return_value=(0.05, 0, True),
                ),
            ):
                report = sfx_delivery.verify_delivery(
                    plan_path, catalog_path, stem_path,
                    evidence, REVISION, CUT_HASH,
                    candidate_path=candidate_path,
                    dialogue_priority_dialogue_path=dialogue_path,
                    dialogue_priority_sfx_path=sfx_path,
                )

            self.assertEqual(report["status"], "fail")
            self.assertEqual(report["delivered_event_count"], 0)
            self.assertTrue(
                any(cue["status"] == "fail" for cue in report["observed_cue_evidence"])
            )
            self.assertTrue(any(
                "candidate output" in failure for failure in report["failures"]
            ))

    def test_v2_dialogue_priority_sample_count_inconsistency_fails(self) -> None:
        evidence = _multi_event_visual_evidence()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path, catalog_path, stem_path = _stage_v2(
                root, evidence, include_external=False
            )
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            total_samples = plan["sfx_stem_sample_count"]
            dialogue_path = root / "dialogue_priority_dialogue.wav"
            sfx_path = root / "dialogue_priority_sfx.wav"
            _write_priority_wav(dialogue_path, total_samples - 4800, -38.0)
            _write_priority_wav(sfx_path, total_samples, -52.0)

            report = sfx_delivery.verify_delivery(
                plan_path, catalog_path, stem_path,
                evidence, REVISION, CUT_HASH,
                dialogue_priority_dialogue_path=dialogue_path,
                dialogue_priority_sfx_path=sfx_path,
            )

            self.assertEqual(report["status"], "fail")
            self.assertEqual(report["delivered_event_count"], 0)
            self.assertTrue(any(
                "dialogue-priority dialogue sample count does not match plan" in failure
                for failure in report["failures"]
            ))

    def test_v2_studio_edits_declared_without_authorization_fails(self) -> None:
        evidence = _multi_event_visual_evidence()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path, catalog_path, stem_path = _stage_v2(
                root, evidence, include_external=False
            )
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["studio_edits"] = {"edits": []}
            plan["studio_edits_sha256"] = "c" * 64
            plan_path.write_text(json.dumps(plan), encoding="utf-8")

            report = sfx_delivery.verify_delivery(
                plan_path, catalog_path, stem_path,
                evidence, REVISION, CUT_HASH,
                expected_studio_edits_sha256=None,
            )

            self.assertEqual(report["status"], "fail")
            self.assertTrue(any(
                "studio edits were not authorized" in failure
                for failure in report["failures"]
            ))

    def test_v2_studio_edits_never_resolvable_even_when_expected(self) -> None:
        evidence = _multi_event_visual_evidence()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path, catalog_path, stem_path = _stage_v2(
                root, evidence, include_external=False
            )

            report = sfx_delivery.verify_delivery(
                plan_path, catalog_path, stem_path,
                evidence, REVISION, CUT_HASH,
                expected_studio_edits_sha256="d" * 64,
            )

            self.assertEqual(report["status"], "fail")
            self.assertEqual(report["expected_studio_edits_sha256"], "d" * 64)
            self.assertTrue(any(
                "does not support studio-edit resolution" in failure
                for failure in report["failures"]
            ))

    def test_v2_plan_mixing_content_hint_and_external_asset_events_passes_under_binding(
        self,
    ) -> None:
        evidence = _multi_event_visual_evidence()
        hints = {
            "schema_version": 1,
            "hints": [
                {"onset_sample": 150000, "category": "cash_coin", "reason": "coin cue"}
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path, catalog_path, stem_path = _stage_v2(
                root, evidence,
                include_external=True,
                sfx_hints=hints,
                source_state_revision=SOURCE_STATE_REVISION,
            )
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            packs = {asset["asset_id"]: asset["pack"] for asset in catalog["assets"]}
            content_hint_events = [e for e in plan["events"] if e["role"] == "content_hint"]
            external_events = [
                e for e in plan["events"]
                if e["role"] != "content_hint" and packs[e["asset_id"]] == "external-licensed"
            ]
            self.assertTrue(content_hint_events)
            self.assertTrue(external_events)

            total_samples = plan["sfx_stem_sample_count"]
            dialogue_path = root / "dialogue_priority_dialogue.wav"
            sfx_path = root / "dialogue_priority_sfx.wav"
            _write_priority_wav(dialogue_path, total_samples, -38.0)
            _write_priority_wav(sfx_path, total_samples, -52.0)
            candidate_samples = [(0.0, 0.0)] * total_samples
            candidate_audio = sfx_delivery.DecodedWav(
                48000, 2, 4, b"\0" * (total_samples * 8), candidate_samples
            )
            candidate_path = root / "candidate.mp4"
            candidate_path.write_bytes(b"v2-mixed-content-hint-external-asset")

            with (
                patch.object(
                    sfx_delivery,
                    "_decode_candidate_audio_bytes",
                    return_value=candidate_audio,
                ),
                patch.object(
                    sfx_delivery,
                    "_estimate_dialogue_pipeline_lag",
                    return_value=(1.0, 0, 1.0),
                ),
                patch.object(
                    sfx_delivery,
                    "_cue_template_partial_correlation",
                    return_value=(0.9, 0, True),
                ),
            ):
                report = sfx_delivery.verify_delivery(
                    plan_path, catalog_path, stem_path,
                    evidence, REVISION, CUT_HASH,
                    candidate_path=candidate_path,
                    dialogue_priority_dialogue_path=dialogue_path,
                    dialogue_priority_sfx_path=sfx_path,
                )

            self.assertEqual(report["status"], "pass", report["failures"])
            self.assertEqual(report["delivered_event_count"], len(plan["events"]))


class V1GoldenHashesUnchangedTests(unittest.TestCase):
    GOLDEN = {
        "audio_event_plan.json": "c7433118669455b9ada0bebc83d63950d6915a26be5f776c0833a330b3607e17",
        "audio_catalog.json": "08b961ba76e5d15f7eb37c7ea28adca090380f402ca6d5848c25d3682eeca50b",
        "sfx_stem.wav": "b88fc7efea475860450f2735a26ac87bb81b7d12b7d159bfc2f6606511ec5031",
    }

    def test_v1_staging_golden_bytes_unchanged_by_v2_verifier_changes(self) -> None:
        evidence = _multi_event_visual_evidence()
        with tempfile.TemporaryDirectory() as directory:
            plan_path, catalog_path, stem_path = sfx_delivery.stage_multi_event_delivery(
                Path(directory), evidence, REVISION, CUT_HASH, catalog_profile="v1"
            )
            hashes = {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (plan_path, catalog_path, stem_path)
            }
        self.assertEqual(hashes, self.GOLDEN)


if __name__ == "__main__":
    unittest.main()
