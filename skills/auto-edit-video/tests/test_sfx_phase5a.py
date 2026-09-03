"""Phase 5a contract-layer tests.

Covers the tightened ``audio_event_plan`` semantic rules introduced to close
three review findings:

- BLOCKER: ``validate_event``/``validate_hint_event`` must never silently
  accept an event whose evidence is missing the required trigger/hint
  branch.
- MAJOR: ``catalog_revision_sha256`` is required (and format-checked) for
  schema v3 and forbidden for v1/v2, and the v2 verifier always compares the
  plan's declared value against the canonically hashed staged catalog.
- MAJOR: dropped density candidates carrying hint evidence are dispatched
  through ``validate_hint_event`` and ``hint_out_of_range`` is only accepted
  when the event genuinely exceeds the sfx stem bounds.
"""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import sfx_delivery  # noqa: E402
from contract_registry import (  # noqa: E402
    load_artifact_text,
    semantic_audio_event_plan,
    validate_artifact,
)

AUDIO_EVENT_PLAN_FIXTURES = SKILL_DIR / "contracts" / "fixtures" / "audio_event_plan"


def _load(name: str):
    return load_artifact_text((AUDIO_EVENT_PLAN_FIXTURES / name).read_text("utf-8"))


def _single_title_visual_evidence() -> dict:
    return {
        "schema_version": 1,
        "duration_s": 6.0,
        "items": [
            {
                "id": "title-1",
                "start": "0.20",
                "end": "0.50",
                "kind": "title",
                "component_id": "title-lockup",
                "title_kind": "section",
                "motion": {
                    "requested": "slide-up",
                    "delivered": "slide-up",
                    "faithful": True,
                    "status": "native",
                },
            },
        ],
    }


def _v3_plan_with_dropped_hint(*, reason: str, out_of_range: bool) -> dict:
    """A schema-v3 plan with one kept trigger event and one dropped hint."""
    fixture = copy.deepcopy(_load("valid_v3_hint.json"))
    title_event = next(e for e in fixture["events"] if e["trigger_id"] == "title-1")
    total_samples = 480000
    fixture["sfx_stem_sample_count"] = total_samples
    fixture["events"] = [title_event]
    density = fixture["density"]
    density["total_samples"] = total_samples
    density["density_cap"] = (total_samples * 40) // (60 * 48000)
    density["candidate_order"] = ["title-1", "hint-1"]
    density["kept_trigger_ids"] = ["title-1"]
    density["dropped_trigger_ids"] = ["hint-1"]
    density["dropped_reasons"] = {reason: 1}
    if out_of_range:
        expected = total_samples - 240 + 4000
    else:
        expected = 200000
    start = expected - 240
    duration = 8000
    density["dropped"] = [
        {
            "trigger_id": "hint-1",
            "role": "content_hint",
            "category": "cash_coin",
            "asset_id": "coin-blip-v1",
            "trigger_onset_sample": expected,
            "event_start_sample": start,
            "asset_transient_anchor_sample": 240,
            "expected_transient_sample": expected,
            "duration_samples": duration,
            "evidence": {
                "hint": {
                    "onset_sample": expected,
                    "category": "cash_coin",
                    "reason": "detected a coin/cash-register cue in the raw footage audio",
                    "source_state_revision": "d16fdd85e4926ac50b435aaa8be41f8f28bc4cc274cb6ceee3b3d0c2dfda71c1",
                }
            },
            "reason": reason,
        }
    ]
    return fixture


class BlockerTriggerHintEvidenceTests(unittest.TestCase):
    def test_v1_empty_evidence_is_rejected(self) -> None:
        fixture = copy.deepcopy(_load("valid_1.json"))
        fixture["events"][0]["evidence"] = {}
        errors = validate_artifact("audio_event_plan", fixture)
        self.assertTrue(errors)
        self.assertIn(
            "$.events[0].evidence.trigger: schema v1 requires complete renderer trigger evidence",
            errors,
        )

    def test_v2_empty_evidence_is_rejected(self) -> None:
        fixture = copy.deepcopy(_load("valid_v2.json"))
        fixture["events"][0]["evidence"] = {}
        errors = validate_artifact("audio_event_plan", fixture)
        self.assertTrue(errors)
        self.assertIn(
            "$.events[0].evidence.trigger: schema v2 requires complete renderer trigger evidence",
            errors,
        )

    def test_v3_trigger_event_with_neither_evidence_branch_is_rejected(self) -> None:
        fixture = copy.deepcopy(_load("valid_v3_external_trigger.json"))
        fixture["events"][0]["evidence"] = {}
        errors = semantic_audio_event_plan(fixture)
        self.assertIn("$.events[0].evidence: must contain exactly one of trigger or hint", errors)
        self.assertIn(
            "$.events[0].evidence.trigger: schema v3 requires complete renderer trigger evidence",
            errors,
        )

    def test_v3_hint_event_with_neither_evidence_branch_is_rejected(self) -> None:
        fixture = copy.deepcopy(_load("valid_v3_hint.json"))
        hint_index = next(i for i, e in enumerate(fixture["events"]) if e["trigger_id"] == "coin-1")
        fixture["events"][hint_index]["evidence"] = {}
        errors = semantic_audio_event_plan(fixture)
        self.assertIn(
            f"$.events[{hint_index}].evidence: must contain exactly one of trigger or hint", errors
        )


class CatalogRevisionShaTests(unittest.TestCase):
    def test_v3_missing_catalog_revision_is_rejected(self) -> None:
        fixture = _load("invalid_v3_missing_catalog_revision.json")
        errors = validate_artifact("audio_event_plan", fixture)
        self.assertIn(
            "$.catalog_revision_sha256: schema v3 requires 64 lowercase hex characters", errors
        )

    def test_v3_malformed_catalog_revision_is_rejected(self) -> None:
        fixture = copy.deepcopy(_load("valid_v3_external_trigger.json"))
        fixture["catalog_revision_sha256"] = "not-hex"
        errors = semantic_audio_event_plan(fixture)
        self.assertIn(
            "$.catalog_revision_sha256: schema v3 requires 64 lowercase hex characters", errors
        )

    def test_v1_catalog_revision_present_is_rejected(self) -> None:
        fixture = copy.deepcopy(_load("valid_1.json"))
        fixture["catalog_revision_sha256"] = "a" * 64
        errors = semantic_audio_event_plan(fixture)
        self.assertIn("$.catalog_revision_sha256: schema v1 must not contain this field", errors)

    def test_v2_catalog_revision_present_is_rejected(self) -> None:
        fixture = _load("invalid_v2_has_catalog_revision.json")
        errors = validate_artifact("audio_event_plan", fixture)
        self.assertIn("$.catalog_revision_sha256: schema v2 must not contain this field", errors)

    def test_verifier_rejects_catalog_revision_mismatch(self) -> None:
        evidence = _single_title_visual_evidence()
        revision, cut_hash = "a" * 64, "b" * 64
        with tempfile.TemporaryDirectory() as directory:
            plan_path, catalog_path, stem_path = sfx_delivery.stage_multi_event_delivery(
                Path(directory), evidence, revision, cut_hash, catalog_profile="v2"
            )
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(plan["schema_version"], 3)
            good_report = sfx_delivery.verify_delivery(
                plan_path, catalog_path, stem_path, evidence, revision, cut_hash
            )
            self.assertEqual(good_report["status"], "pass", good_report["failures"])

            plan["catalog_revision_sha256"] = "0" * 64
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            bad_report = sfx_delivery.verify_delivery(
                plan_path, catalog_path, stem_path, evidence, revision, cut_hash
            )
            self.assertEqual(bad_report["status"], "fail")
            self.assertIn(
                "audio_event_plan.catalog_revision_sha256 does not match the staged catalog",
                bad_report["failures"],
            )


class HintOutOfRangeTests(unittest.TestCase):
    def test_genuine_hint_out_of_range_drop_is_accepted(self) -> None:
        fixture = _v3_plan_with_dropped_hint(reason="hint_out_of_range", out_of_range=True)
        self.assertEqual(validate_artifact("audio_event_plan", fixture), [])

    def test_mislabeled_hint_out_of_range_drop_is_rejected(self) -> None:
        fixture = _v3_plan_with_dropped_hint(reason="hint_out_of_range", out_of_range=False)
        errors = semantic_audio_event_plan(fixture)
        self.assertIn(
            "$.density.dropped[0].reason: hint_out_of_range requires the event to exceed sfx stem bounds",
            errors,
        )

    def test_dropped_hint_with_empty_evidence_is_rejected(self) -> None:
        fixture = _v3_plan_with_dropped_hint(reason="hint_out_of_range", out_of_range=True)
        fixture["density"]["dropped"][0]["evidence"] = {"hint": {}}
        errors = semantic_audio_event_plan(fixture)
        self.assertTrue(
            any(e.startswith("$.density.dropped[0]") for e in errors),
            errors,
        )

    def test_dropped_hint_illegal_category_is_rejected(self) -> None:
        fixture = _v3_plan_with_dropped_hint(reason="hint_out_of_range", out_of_range=True)
        fixture["density"]["dropped"][0]["category"] = "not_a_category"
        fixture["density"]["dropped"][0]["evidence"]["hint"]["category"] = "not_a_category"
        errors = semantic_audio_event_plan(fixture)
        self.assertIn(
            "$.density.dropped[0].category: must be a recognized SFX category", errors
        )


if __name__ == "__main__":
    unittest.main()
