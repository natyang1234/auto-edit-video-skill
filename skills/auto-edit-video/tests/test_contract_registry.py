"""Phase 0 contract registry tests: dialect strictness, fixtures, hashing."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import contract_registry  # noqa: E402
from contract_registry import (  # noqa: E402
    ContractError,
    canonical_hash,
    check_schema_dialect,
    load_artifact_text,
    load_manifest,
    load_schemas,
    run_fixture_suite,
    run_instance_suite,
    semantic_approval_receipt,
    semantic_master_timeline,
    semantic_visual_plan,
    validate,
)


class DialectTests(unittest.TestCase):
    def test_unknown_keyword_fails_loudly(self) -> None:
        with self.assertRaises(ContractError):
            check_schema_dialect({"type": "object", "oneOf": []})

    def test_unknown_nested_keyword_fails(self) -> None:
        schema = {"type": "object", "properties": {"a": {"type": "string", "format": "uri"}}}
        with self.assertRaises(ContractError):
            check_schema_dialect(schema)

    def test_metadata_keywords_are_allowed(self) -> None:
        check_schema_dialect({"$id": "x", "title": "t", "x_schema_version": 1, "type": "object"})

    def test_every_shipped_schema_passes_dialect_check(self) -> None:
        schemas = load_schemas()
        self.assertEqual(sorted(schemas), sorted(load_manifest()))

    def test_validate_rejects_extra_properties_and_bad_enum(self) -> None:
        schema = {
            "type": "object",
            "properties": {"kind": {"enum": ["a", "b"]}},
            "required": ["kind"],
            "additionalProperties": False,
        }
        self.assertEqual(validate({"kind": "a"}, schema), [])
        self.assertTrue(validate({"kind": "z"}, schema))
        self.assertTrue(validate({"kind": "a", "extra": 1}, schema))
        self.assertTrue(validate({}, schema))


class ArtifactParsingTests(unittest.TestCase):
    def test_duplicate_keys_rejected(self) -> None:
        with self.assertRaises(ContractError):
            load_artifact_text('{"a": 1, "a": 2}')

    def test_nan_and_infinity_rejected(self) -> None:
        for text in ('{"a": NaN}', '{"a": Infinity}', '{"a": -Infinity}'):
            with self.assertRaises(ContractError):
                load_artifact_text(text)

    def test_canonical_hash_is_key_order_independent_and_stable(self) -> None:
        first = canonical_hash({"b": 1, "a": [1, 2, {"z": True}]})
        second = canonical_hash({"a": [1, 2, {"z": True}], "b": 1})
        self.assertEqual(first, second)
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertNotEqual(first, canonical_hash({"a": [1, 2, {"z": False}], "b": 1}))

    def test_canonical_hash_rejects_nonfinite(self) -> None:
        with self.assertRaises(ContractError):
            canonical_hash({"a": float("inf")})


class SemanticValidatorTests(unittest.TestCase):
    def test_master_timeline_duration_must_match_segment_sum(self) -> None:
        timeline = {
            "segments": [{"source_start": 0.0, "source_end": 2.0}],
            "post_cut_duration_s": 9.0,
        }
        self.assertTrue(semantic_master_timeline(timeline))
        timeline["post_cut_duration_s"] = 2.0
        self.assertEqual(semantic_master_timeline(timeline), [])

    def test_final_approval_must_bind_preview_hash(self) -> None:
        receipt = {"gate": "final", "approved": True, "approved_preview_artifact_hash": None}
        self.assertTrue(semantic_approval_receipt(receipt))
        receipt["approved_preview_artifact_hash"] = "a" * 64
        self.assertEqual(semantic_approval_receipt(receipt), [])

    def test_visual_plan_timing_ssot_backreference(self) -> None:
        plan = {
            "items": [
                {
                    "id": "visual-beat-abcdef01",
                    "start": 1.0,
                    "end": 2.0,
                    "beat": "title",
                    "structured_layer_id": "structured-layer-abcdef01",
                    "conceptual_only": True,
                    "evidence_ids": [],
                }
            ]
        }
        broken_layers = {
            "items": [
                {"id": "structured-layer-abcdef01", "visual_plan_item_id": "visual-beat-other"}
            ]
        }
        good_layers = {
            "items": [
                {"id": "structured-layer-abcdef01", "visual_plan_item_id": "visual-beat-abcdef01"}
            ]
        }
        self.assertTrue(semantic_visual_plan(plan, broken_layers))
        self.assertEqual(semantic_visual_plan(plan, good_layers), [])
        self.assertEqual(semantic_visual_plan(plan, None), [])


class FixtureSuiteTests(unittest.TestCase):
    def test_full_fixture_suite_is_green(self) -> None:
        valid_count, invalid_count, failures = run_fixture_suite()
        self.assertEqual(failures, [])
        self.assertGreaterEqual(valid_count, len(load_manifest()))
        self.assertGreaterEqual(invalid_count, len(load_manifest()))

    def test_style_pack_instance_passes_its_schema(self) -> None:
        self.assertEqual(run_instance_suite(), [])

    def test_cli_validate_exits_zero(self) -> None:
        self.assertEqual(contract_registry.main(["contract_registry.py", "validate"]), 0)


if __name__ == "__main__":
    unittest.main()
