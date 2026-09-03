"""sfx_hints contract and director_mode catalog_profile=v2 gate.

Covers the additive ``sfx_hints`` schema and the ``experience.sfx.catalog_profile``
optional field on ``director_mode``, introduced alongside ``scripts/sfx_mapping.py``
category constants. Existing director_mode behavior covered by
``tests/test_contract_registry.py`` intentionally not re-tested here.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import contract_registry  # noqa: E402
from contract_registry import (  # noqa: E402
    load_artifact_text,
    semantic_director_mode,
    semantic_sfx_hints,
    validate_artifact,
)
import director_resolver  # noqa: E402

SFX_HINTS_FIXTURES = SKILL_DIR / "contracts" / "fixtures" / "sfx_hints"
DIRECTOR_MODE_FIXTURES = SKILL_DIR / "contracts" / "fixtures" / "director_mode"


def _load(fixture_dir: Path, name: str):
    return load_artifact_text((fixture_dir / name).read_text("utf-8"))


class SfxHintsContractTests(unittest.TestCase):
    def test_valid_hints_pass_schema_and_semantic(self) -> None:
        fixture = _load(SFX_HINTS_FIXTURES, "valid_1.json")
        self.assertEqual(validate_artifact("sfx_hints", fixture), [])
        self.assertEqual(semantic_sfx_hints(fixture), [])

    def test_empty_hints_pass(self) -> None:
        fixture = _load(SFX_HINTS_FIXTURES, "valid_empty.json")
        self.assertEqual(validate_artifact("sfx_hints", fixture), [])

    def test_extra_property_is_rejected(self) -> None:
        fixture = _load(SFX_HINTS_FIXTURES, "invalid_extra.json")
        errors = validate_artifact("sfx_hints", fixture)
        self.assertTrue(errors)
        self.assertTrue(any("confidence" in error for error in errors))

    def test_non_increasing_onset_is_rejected(self) -> None:
        fixture = _load(SFX_HINTS_FIXTURES, "invalid_onset_order.json")
        errors = validate_artifact("sfx_hints", fixture)
        self.assertIn(
            "$.hints[1].onset_sample: must be strictly increasing", errors
        )

    def test_unknown_category_is_rejected(self) -> None:
        fixture = _load(SFX_HINTS_FIXTURES, "invalid_category.json")
        errors = validate_artifact("sfx_hints", fixture)
        self.assertTrue(errors)
        self.assertTrue(any("category" in error for error in errors))


class DirectorModeSfxCatalogProfileTests(unittest.TestCase):
    def test_disabled_sfx_with_catalog_profile_v2_is_rejected(self) -> None:
        fixture = _load(DIRECTOR_MODE_FIXTURES, "invalid_sfx_catalog_profile.json")
        errors = semantic_director_mode(fixture)
        self.assertIn(
            "$.modes[0].experience.sfx.catalog_profile: v2 requires sfx.mode != disabled",
            errors,
        )

    def test_paired_sfx_with_catalog_profile_v2_is_accepted(self) -> None:
        fixture = _load(DIRECTOR_MODE_FIXTURES, "valid_2.json")
        self.assertEqual(validate_artifact("director_mode", fixture), [])
        self.assertEqual(semantic_director_mode(fixture), [])

    def test_registry_kinetic_explainer_uses_catalog_profile_v2(self) -> None:
        registry = director_resolver.load_director_registry()
        mode = next(
            item for item in registry["modes"] if item["id"] == "kinetic-explainer"
        )
        self.assertEqual(mode["experience"]["sfx"]["catalog_profile"], "v2")

    def test_legacy_director_has_no_catalog_profile(self) -> None:
        profile = director_resolver.resolve_director_profile("teacher-punch")
        self.assertNotIn("catalog_profile", profile["experience"]["sfx"])

    def test_legacy_sfx_experience_validates_against_sfx_subschema(self) -> None:
        profile = director_resolver.resolve_director_profile("teacher-punch")
        schemas = contract_registry.load_schemas()
        sfx_schema = (
            schemas["director_mode"]["properties"]["modes"]["items"]["properties"]
            ["experience"]["properties"]["sfx"]
        )
        errors = contract_registry.validate(profile["experience"]["sfx"], sfx_schema)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
