"""Phase 1 sfx_catalog v2 / audio_event_plan v3 contract tests.

Covers the additive schema-v2/v3 semantic dispatch introduced alongside
``scripts/sfx_mapping.py``. Existing v1/v2 behavior is covered by
``tests/test_contract_registry.py`` and is intentionally not re-tested here.
"""
from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import contract_registry  # noqa: E402
from contract_registry import (  # noqa: E402
    load_artifact_text,
    semantic_audio_event_plan,
    semantic_sfx_catalog,
    validate_artifact,
)
from sfx_mapping import (  # noqa: E402
    ROLE_TO_CATEGORY,
    SFX_CATEGORIES,
    STARTER_ASSET_IDS,
    V2_EXTRA_PROCEDURAL_ASSET_IDS,
    V2_PROCEDURAL_ASSET_IDS,
)

SFX_CATALOG_FIXTURES = SKILL_DIR / "contracts" / "fixtures" / "sfx_catalog"
AUDIO_EVENT_PLAN_FIXTURES = SKILL_DIR / "contracts" / "fixtures" / "audio_event_plan"


def _load(fixture_dir: Path, name: str):
    return load_artifact_text((fixture_dir / name).read_text("utf-8"))


class SfxMappingTests(unittest.TestCase):
    def test_v2_extras_are_not_part_of_the_frozen_v1_starter_set(self) -> None:
        self.assertNotIn("coin-blip-v1", STARTER_ASSET_IDS)
        self.assertNotIn("glitch-buzz-v1", STARTER_ASSET_IDS)
        self.assertEqual(len(STARTER_ASSET_IDS), 7)
        self.assertEqual(
            V2_PROCEDURAL_ASSET_IDS, STARTER_ASSET_IDS + V2_EXTRA_PROCEDURAL_ASSET_IDS
        )

    def test_role_to_category_only_covers_renderer_motion_roles(self) -> None:
        for role, category in ROLE_TO_CATEGORY.items():
            self.assertIn(category, SFX_CATEGORIES)
        self.assertNotIn("content_hint", ROLE_TO_CATEGORY)


class SfxCatalogV2Tests(unittest.TestCase):
    def test_v1_legacy_catalog_is_unaffected_by_v2_support(self) -> None:
        fixture = _load(SFX_CATALOG_FIXTURES, "valid_1.json")
        self.assertEqual(validate_artifact("sfx_catalog", fixture), [])
        self.assertEqual(semantic_sfx_catalog(fixture), [])

    def test_v2_procedural_catalog_with_new_extras_is_valid(self) -> None:
        fixture = _load(SFX_CATALOG_FIXTURES, "valid_v2_procedural.json")
        self.assertEqual(fixture["schema_version"], 2)
        asset_ids = {asset["asset_id"] for asset in fixture["assets"]}
        self.assertIn("coin-blip-v1", asset_ids)
        self.assertIn("glitch-buzz-v1", asset_ids)
        self.assertEqual(validate_artifact("sfx_catalog", fixture), [])

    def test_v2_external_licensed_catalog_is_valid(self) -> None:
        fixture = _load(SFX_CATALOG_FIXTURES, "valid_v2_external.json")
        self.assertEqual(fixture["assets"][0]["pack"], "external-licensed")
        self.assertEqual(validate_artifact("sfx_catalog", fixture), [])

    def test_v2_external_licensed_missing_license_is_rejected(self) -> None:
        fixture = _load(SFX_CATALOG_FIXTURES, "invalid_v2_external_missing_license.json")
        errors = validate_artifact("sfx_catalog", fixture)
        self.assertTrue(errors)
        self.assertTrue(any("license" in error for error in errors))

    def test_v2_external_licensed_must_not_declare_generator(self) -> None:
        fixture = _load(SFX_CATALOG_FIXTURES, "invalid_v2_v1_fields_mix.json")
        errors = validate_artifact("sfx_catalog", fixture)
        self.assertTrue(errors)
        self.assertTrue(any("generator" in error for error in errors))

    def test_schema_version_two_is_no_longer_an_impostor(self) -> None:
        fixture = _load(SFX_CATALOG_FIXTURES, "valid_v2_procedural.json")
        self.assertEqual(semantic_sfx_catalog(fixture), [])
        malformed = copy.deepcopy(fixture)
        malformed["schema_version"] = 3
        self.assertIn(
            "$.schema_version: must be exact integer 1",
            semantic_sfx_catalog(malformed),
        )

    def test_v2_procedural_asset_generator_mismatch_is_rejected(self) -> None:
        fixture = copy.deepcopy(_load(SFX_CATALOG_FIXTURES, "valid_v2_procedural.json"))
        fixture["assets"][1]["generator"]["name"] = "wrong_generator_name"
        errors = semantic_sfx_catalog(fixture)
        self.assertIn(
            "$.assets[1].generator.name: does not match asset id",
            errors,
        )


class AudioEventPlanV3Tests(unittest.TestCase):
    def test_v1_and_v2_are_unaffected_by_v3_support(self) -> None:
        v1 = _load(AUDIO_EVENT_PLAN_FIXTURES, "valid_1.json")
        v2 = _load(AUDIO_EVENT_PLAN_FIXTURES, "valid_v2.json")
        self.assertEqual(validate_artifact("audio_event_plan", v1), [])
        self.assertEqual(validate_artifact("audio_event_plan", v2), [])
        self.assertEqual(semantic_audio_event_plan(v1), [])
        self.assertEqual(semantic_audio_event_plan(v2), [])

    def test_v3_hint_evidence_plan_is_valid(self) -> None:
        fixture = _load(AUDIO_EVENT_PLAN_FIXTURES, "valid_v3_hint.json")
        self.assertEqual(fixture["schema_version"], 3)
        hint_events = [event for event in fixture["events"] if event["role"] == "content_hint"]
        self.assertEqual(len(hint_events), 1)
        self.assertEqual(validate_artifact("audio_event_plan", fixture), [])
        self.assertEqual(semantic_audio_event_plan(fixture), [])

    def test_v3_hint_role_must_match_hint_evidence(self) -> None:
        fixture = _load(AUDIO_EVENT_PLAN_FIXTURES, "invalid_v3_hint_role_mismatch.json")
        errors = semantic_audio_event_plan(fixture)
        self.assertEqual(
            errors,
            ["$.events[2].role: hint evidence requires role content_hint"],
        )

    def test_v3_event_must_not_declare_both_trigger_and_hint_evidence(self) -> None:
        fixture = _load(AUDIO_EVENT_PLAN_FIXTURES, "invalid_v3_both_evidence.json")
        errors = semantic_audio_event_plan(fixture)
        self.assertIn(
            "$.events[2].evidence: must contain exactly one of trigger or hint",
            errors,
        )

    def test_v3_hint_category_must_match_evidence_hint_category(self) -> None:
        fixture = copy.deepcopy(_load(AUDIO_EVENT_PLAN_FIXTURES, "valid_v3_hint.json"))
        fixture["events"][2]["category"] = "error_glitch"
        errors = semantic_audio_event_plan(fixture)
        self.assertIn(
            "$.events[2].category: must match evidence.hint.category",
            errors,
        )

    def test_v3_hint_event_still_enforces_transient_arithmetic(self) -> None:
        fixture = copy.deepcopy(_load(AUDIO_EVENT_PLAN_FIXTURES, "valid_v3_hint.json"))
        fixture["events"][2]["expected_transient_sample"] += 1
        errors = semantic_audio_event_plan(fixture)
        self.assertIn(
            "$.events[2].expected_transient_sample: must equal event_start_sample + asset_transient_anchor_sample",
            errors,
        )


if __name__ == "__main__":
    unittest.main()
