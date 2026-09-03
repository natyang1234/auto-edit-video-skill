"""Phase 4b SFX v2 director-mode hints and semantic trigger-asset selection.

Covers: sfx_hints -> content_hint plan events, v2/v3 trigger events selecting
externally-preferred assets by category, v1 hint-ignore + golden bytes, and
the relaxed v3 role->category semantic invariant. Existing v1/v2 behavior is
covered by ``tests/test_sfx_delivery_v2.py`` / ``tests/test_contract_registry.py``
and intentionally not re-tested here.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import contract_registry  # noqa: E402
import sfx_delivery  # noqa: E402
import sfx_mapping  # noqa: E402

from test_sfx_delivery_v2 import _multi_event_visual_evidence  # noqa: E402

AUDIO_EVENT_PLAN_FIXTURES = SKILL_DIR / "contracts" / "fixtures" / "audio_event_plan"

REVISION = "a" * 64
CUT_HASH = "b" * 64
SOURCE_STATE_REVISION = "e" * 64


def _load_fixture(name: str) -> dict:
    return contract_registry.load_artifact_text((AUDIO_EVENT_PLAN_FIXTURES / name).read_text("utf-8"))


def _hints(*entries: tuple[int, str, str]) -> dict:
    return {
        "schema_version": 1,
        "hints": [
            {"onset_sample": onset, "category": category, "reason": reason}
            for onset, category, reason in entries
        ],
    }


class V2TriggerExternalSelectionTests(unittest.TestCase):
    def test_v2_staging_trigger_events_select_external_assets_with_category(self) -> None:
        evidence = _multi_event_visual_evidence()
        with tempfile.TemporaryDirectory() as directory:
            plan_path, catalog_path, _ = sfx_delivery.stage_multi_event_delivery(
                Path(directory), evidence, REVISION, CUT_HASH, catalog_profile="v2"
            )
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        external_ids = {
            asset["asset_id"] for asset in catalog["assets"] if asset.get("pack") == "external-licensed"
        }
        catalog_by_id = {asset["asset_id"]: asset for asset in catalog["assets"]}
        trigger_events = [event for event in plan["events"] if event["role"] != "content_hint"]
        self.assertTrue(trigger_events)
        self.assertTrue(
            any(event["asset_id"] in external_ids for event in trigger_events),
            "expected at least one v2 trigger event to resolve to an external-licensed asset",
        )
        for event in trigger_events:
            self.assertEqual(event["category"], sfx_mapping.ROLE_TO_CATEGORY[event["role"]])
            self.assertEqual(catalog_by_id[event["asset_id"]]["category"], event["category"])

    def test_include_external_false_still_lets_trigger_events_pass(self) -> None:
        evidence = _multi_event_visual_evidence()
        with tempfile.TemporaryDirectory() as directory:
            plan_path, catalog_path, stem_path = sfx_delivery.stage_multi_event_delivery(
                Path(directory),
                evidence,
                REVISION,
                CUT_HASH,
                catalog_profile="v2",
                include_external=False,
            )
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            self.assertEqual(len(catalog["assets"]), 9)
            self.assertTrue(all(asset.get("pack") != "external-licensed" for asset in catalog["assets"]))
            report = sfx_delivery.verify_delivery(
                plan_path, catalog_path, stem_path, evidence, REVISION, CUT_HASH
            )
            self.assertEqual(report["status"], "pass", report["failures"])


class SfxHintsToContentHintEventsTests(unittest.TestCase):
    def test_hints_become_content_hint_events_that_pass_the_verifier(self) -> None:
        # A longer clip than the shared 6s fixture so the clip-density cap
        # (cues/minute) has room for the 3 trigger events plus both hints.
        evidence = copy.deepcopy(_multi_event_visual_evidence())
        evidence["duration_s"] = 10.0
        hints = _hints(
            (90000, "error_glitch", "glitch cue detected"),
            (150000, "cash_coin", "coin cue detected"),
        )
        with tempfile.TemporaryDirectory() as directory:
            plan_path, catalog_path, stem_path = sfx_delivery.stage_multi_event_delivery(
                Path(directory),
                evidence,
                REVISION,
                CUT_HASH,
                catalog_profile="v2",
                sfx_hints=hints,
                source_state_revision=SOURCE_STATE_REVISION,
            )
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            hint_events = [event for event in plan["events"] if event["role"] == "content_hint"]
            self.assertEqual(len(hint_events), 2)
            categories = {event["category"] for event in hint_events}
            self.assertEqual(categories, {"cash_coin", "error_glitch"})
            for event in hint_events:
                self.assertEqual(event["evidence"]["hint"]["category"], event["category"])
                self.assertEqual(event["evidence"]["hint"]["source_state_revision"], SOURCE_STATE_REVISION)
            report = sfx_delivery.verify_delivery(
                plan_path, catalog_path, stem_path, evidence, REVISION, CUT_HASH
            )
            self.assertEqual(report["status"], "pass", report["failures"])

    def test_out_of_range_hint_is_soft_dropped_with_hint_out_of_range_reason(self) -> None:
        evidence = _multi_event_visual_evidence()
        total_samples = sfx_delivery._duration_samples_from_evidence(evidence)
        hints = _hints((total_samples, "cash_coin", "coin cue right at the end"))
        with tempfile.TemporaryDirectory() as directory:
            plan_path, _, _ = sfx_delivery.stage_multi_event_delivery(
                Path(directory),
                evidence,
                REVISION,
                CUT_HASH,
                catalog_profile="v2",
                sfx_hints=hints,
                source_state_revision=SOURCE_STATE_REVISION,
            )
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            dropped = plan["density"]["dropped"]
            self.assertTrue(any(item["reason"] == "hint_out_of_range" for item in dropped))
            self.assertFalse(any(event["role"] == "content_hint" for event in plan["events"]))

    def test_hints_participate_in_category_rate_limit_caps(self) -> None:
        evidence = _multi_event_visual_evidence()
        hints = _hints(
            (20000, "error_glitch", "glitch cue one"),
            (100000, "error_glitch", "glitch cue two, same window"),
        )
        with tempfile.TemporaryDirectory() as directory:
            plan_path, _, _ = sfx_delivery.stage_multi_event_delivery(
                Path(directory),
                evidence,
                REVISION,
                CUT_HASH,
                catalog_profile="v2",
                sfx_hints=hints,
                source_state_revision=SOURCE_STATE_REVISION,
            )
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            hint_events = [event for event in plan["events"] if event["role"] == "content_hint"]
            self.assertEqual(len(hint_events), 1)
            dropped = plan["density"]["dropped"]
            self.assertTrue(any(item["reason"] == "category_rate_limit" for item in dropped))


class V1IgnoresHintsAndStaysGoldenTests(unittest.TestCase):
    GOLDEN = {
        "audio_event_plan.json": "c7433118669455b9ada0bebc83d63950d6915a26be5f776c0833a330b3607e17",
        "audio_catalog.json": "08b961ba76e5d15f7eb37c7ea28adca090380f402ca6d5848c25d3682eeca50b",
        "sfx_stem.wav": "b88fc7efea475860450f2735a26ac87bb81b7d12b7d159bfc2f6606511ec5031",
    }

    def test_v1_staging_ignores_hints_and_reports_them_via_result_meta(self) -> None:
        evidence = _multi_event_visual_evidence()
        hints = _hints(
            (20000, "cash_coin", "should be ignored"),
            (200000, "error_glitch", "should also be ignored"),
        )
        result_meta: dict = {}
        with tempfile.TemporaryDirectory() as directory:
            plan_path, catalog_path, stem_path = sfx_delivery.stage_multi_event_delivery(
                Path(directory),
                evidence,
                REVISION,
                CUT_HASH,
                catalog_profile="v1",
                sfx_hints=hints,
                source_state_revision=SOURCE_STATE_REVISION,
                result_meta=result_meta,
            )
            self.assertEqual(result_meta["ignored_hints"], 2)
            hashes = {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (plan_path, catalog_path, stem_path)
            }
            self.assertEqual(hashes, self.GOLDEN)

    def test_v1_staging_without_hints_matches_golden_bytes_too(self) -> None:
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


class CatalogRevisionSensitivityTests(unittest.TestCase):
    def test_catalog_revision_sha256_changes_when_external_assets_change(self) -> None:
        evidence = _multi_event_visual_evidence()
        with tempfile.TemporaryDirectory() as with_external, tempfile.TemporaryDirectory() as without_external:
            plan_with_path, _, _ = sfx_delivery.stage_multi_event_delivery(
                Path(with_external), evidence, REVISION, CUT_HASH, catalog_profile="v2", include_external=True
            )
            plan_without_path, _, _ = sfx_delivery.stage_multi_event_delivery(
                Path(without_external), evidence, REVISION, CUT_HASH, catalog_profile="v2", include_external=False
            )
            plan_with = json.loads(plan_with_path.read_text(encoding="utf-8"))
            plan_without = json.loads(plan_without_path.read_text(encoding="utf-8"))
        self.assertNotEqual(plan_with["catalog_revision_sha256"], plan_without["catalog_revision_sha256"])


class V3FixtureTests(unittest.TestCase):
    def test_valid_v3_external_trigger_fixture_passes(self) -> None:
        fixture = _load_fixture("valid_v3_external_trigger.json")
        self.assertEqual(contract_registry.validate_artifact("audio_event_plan", fixture), [])

    def test_invalid_v3_category_role_mismatch_fixture_is_rejected(self) -> None:
        fixture = _load_fixture("invalid_v3_category_role_mismatch.json")
        errors = contract_registry.semantic_audio_event_plan(fixture)
        self.assertIn("$.events[1].category: must match ROLE_TO_CATEGORY[role]", errors)


if __name__ == "__main__":
    unittest.main()
