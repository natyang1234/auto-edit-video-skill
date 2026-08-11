"""Phase 0 contract registry tests: dialect strictness, fixtures, hashing."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import contract_registry  # noqa: E402
import structured_card_compositor as scc  # noqa: E402
from contract_registry import (  # noqa: E402
    ContractError,
    canonical_hash,
    check_schema_dialect,
    load_artifact_text,
    load_manifest,
    load_schemas,
    run_bundle_suite,
    run_fixture_suite,
    run_instance_suite,
    semantic_asset_provenance,
    semantic_audio_event_plan,
    semantic_approval_receipt,
    semantic_evidence_references,
    semantic_master_timeline,
    semantic_provider_interface,
    semantic_structured_layer,
    semantic_video_analysis,
    semantic_visual_plan,
    validate,
    validate_artifact,
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

    def test_const_requires_exact_json_type_and_value_at_public_contract_seams(self) -> None:
        fixture = load_artifact_text(
            (
                SKILL_DIR
                / "contracts"
                / "fixtures"
                / "sfx_catalog"
                / "valid_1.json"
            ).read_text("utf-8")
        )
        self.assertEqual(validate_artifact("sfx_catalog", fixture), [])

        for impostor in (True, 1.0):
            with self.subTest(field="schema_version", impostor=impostor):
                malformed = json.loads(json.dumps(fixture))
                malformed["schema_version"] = impostor
                self.assertTrue(validate_artifact("sfx_catalog", malformed))
                self.assertIn(
                    "$.schema_version: must be exact integer 1",
                    contract_registry.semantic_sfx_catalog(malformed),
                )
            with self.subTest(field="generator.version", impostor=impostor):
                malformed = json.loads(json.dumps(fixture))
                malformed["assets"][0]["generator"]["version"] = impostor
                self.assertTrue(validate_artifact("sfx_catalog", malformed))
                self.assertIn(
                    "$.assets[0].generator.version: must be exact integer 1",
                    contract_registry.semantic_sfx_catalog(malformed),
                )
            with self.subTest(field="studio_edits.schema_version", impostor=impostor):
                self.assertTrue(
                    validate(
                        {"studio_edits": {"schema_version": impostor}},
                        {
                            "type": "object",
                            "properties": {
                                "studio_edits": {
                                    "type": "object",
                                    "properties": {"schema_version": {"const": 1}},
                                }
                            },
                        },
                    )
                )


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

    def test_audio_event_plan_version_is_an_exact_supported_integer(self) -> None:
        fixture_dir = SKILL_DIR / "contracts" / "fixtures" / "audio_event_plan"
        valid_v1 = load_artifact_text((fixture_dir / "valid_1.json").read_text("utf-8"))
        valid_v2 = load_artifact_text((fixture_dir / "valid_v2.json").read_text("utf-8"))
        self.assertEqual(validate_artifact("audio_event_plan", valid_v1), [])
        self.assertEqual(validate_artifact("audio_event_plan", valid_v2), [])

        for version in (True, 1.0, 2.0, "2", 3, None):
            with self.subTest(version=version):
                malformed = json.loads(json.dumps(valid_v1))
                malformed["schema_version"] = version
                self.assertTrue(validate_artifact("audio_event_plan", malformed))
                self.assertEqual(
                    semantic_audio_event_plan(malformed),
                    ["$.schema_version: must be exact integer 1 or 2"],
                )
                self.assertEqual(
                    semantic_audio_event_plan({"schema_version": version}),
                    ["$.schema_version: must be exact integer 1 or 2"],
                )

        missing = json.loads(json.dumps(valid_v1))
        missing.pop("schema_version")
        self.assertTrue(validate_artifact("audio_event_plan", missing))
        self.assertEqual(
            semantic_audio_event_plan(missing),
            ["$.schema_version: must be exact integer 1 or 2"],
        )

        float_fixture = load_artifact_text(
            (fixture_dir / "invalid_schema_version_float.json").read_text("utf-8")
        )
        self.assertIs(type(float_fixture["schema_version"]), float)
        self.assertTrue(validate_artifact("audio_event_plan", float_fixture))


class StylePackRegistryTests(unittest.TestCase):
    def test_registry_exposes_exactly_three_valid_packs(self) -> None:
        self.assertEqual(
            scc.style_pack_ids(),
            ("dark-data-presenter", "kinetic-social", "editorial-paper"),
        )
        for pack_id in scc.style_pack_ids():
            pack = scc.load_style_pack(pack_id)
            self.assertEqual(contract_registry.validate_artifact("style_pack", pack), [])

    def test_every_pack_keeps_all_component_ids_render_compatible(self) -> None:
        expected_ids = {
            item["id"] for item in scc.load_default_pack()["components"]
        }
        for pack_id in scc.style_pack_ids():
            components = scc.load_style_pack(pack_id)["components"]
            self.assertEqual({item["id"] for item in components}, expected_ids)
            for item in components:
                layer_types = [
                    layer_type
                    for layer_type, kinds in scc.COMPONENTS_BY_TYPE.items()
                    if item["kind"] in kinds
                ]
                self.assertEqual(len(layer_types), 1)
                resolved = scc.resolve_component(
                    {"components": components}, layer_types[0], item["id"]
                )
                self.assertEqual(resolved["id"], item["id"])

    def test_unknown_style_pack_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            scc.load_style_pack("vaporwave-9000")


class SemanticValidatorTests(unittest.TestCase):
    def test_director_mode_experience_contract_and_semantics(self) -> None:
        mode = {
            "id": "kinetic-explainer",
            "version": 1,
            "envelope": {
                "cut_density": "high",
                "motion_intensity": "high",
                "caption_style_scope": ["track", "bilingual"],
            },
            "constraints": {"visual_density": "dense"},
            "experience": {
                "caption_delivery": {
                    "mode": "bilingual",
                    "required": True,
                    "artifact_version": 2,
                },
                "translation": {
                    "required": True,
                    "target_language": "en",
                    "provider": "approved-provider-required",
                    "consent_policy": "provider-specific-prior-consent",
                },
                "scene_pack": "kinetic-explainer-v1",
                "style_pack": "kinetic-social",
                "stage_layout": "split-graphic-presenter",
                "visual_density": "dense",
                "motion_intensity": "high",
                "sfx": {
                    "mode": "paired",
                    "pack": "kinetic-local-starter",
                    "cue_coverage_policy": "required",
                },
            },
            "required_capabilities": {
                "ids": ["caption-delivery-v2", "audio-event-mixer-v1"],
                "translation_consent_policy": "provider-specific-prior-consent",
            },
            "auto_select_rules": ["explicit_profile", "reference_style_match"],
        }
        artifact = {"schema_version": 1, "modes": [mode]}
        self.assertEqual(contract_registry.validate_artifact("director_mode", artifact), [])

        blank_mode_id = json.loads(json.dumps(artifact))
        blank_mode_id["modes"][0]["id"] = "   "
        self.assertTrue(any(
            "id: must be a non-empty string" in error
            for error in contract_registry.validate_artifact("director_mode", blank_mode_id)
        ))

        strict = json.loads(json.dumps(artifact))
        strict["modes"][0]["experience"]["caption_delivery"]["unexpected"] = True
        strict["modes"][0]["required_capabilities"]["unexpected"] = True
        strict_errors = contract_registry.validate_artifact("director_mode", strict)
        self.assertTrue(any("caption_delivery: unexpected property" in error for error in strict_errors))
        self.assertTrue(any("required_capabilities: unexpected property" in error for error in strict_errors))

        unpaired = json.loads(json.dumps(artifact))
        del unpaired["modes"][0]["required_capabilities"]
        self.assertTrue(any(
            "must both be present or absent" in error
            for error in contract_registry.validate_artifact("director_mode", unpaired)
        ))

        empty_arrays = json.loads(json.dumps(artifact))
        empty_arrays["modes"][0]["required_capabilities"]["ids"] = []
        empty_arrays["modes"][0]["auto_select_rules"] = []
        empty_errors = contract_registry.validate_artifact("director_mode", empty_arrays)
        self.assertTrue(any("required_capabilities.ids: fewer than minItems" in error for error in empty_errors))
        self.assertTrue(any("auto_select_rules: fewer than minItems" in error for error in empty_errors))

        invalid = json.loads(json.dumps(artifact))
        invalid_mode = invalid["modes"][0]
        invalid_mode["id"] = "kinetic-explainer"
        invalid["modes"].append(json.loads(json.dumps(invalid_mode)))
        invalid_mode["experience"]["motion_intensity"] = "low"
        invalid_mode["experience"]["visual_density"] = "sparse"
        invalid_mode["required_capabilities"]["ids"] = ["caption-delivery-v2", " caption-delivery-v2 "]
        invalid_mode["auto_select_rules"] = ["explicit_profile", " explicit_profile "]
        errors = contract_registry.validate_artifact("director_mode", invalid)
        self.assertTrue(any("duplicate mode id" in error for error in errors))
        self.assertTrue(any("motion_intensity" in error for error in errors))
        self.assertTrue(any("visual_density" in error for error in errors))
        self.assertTrue(any("duplicate capability id" in error for error in errors))
        self.assertTrue(any("duplicate auto_select_rule" in error for error in errors))

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


class DiscriminatedPayloadTests(unittest.TestCase):
    def _layer(self, layer_type: str, payload: dict) -> dict:
        return {
            "items": [
                {
                    "id": "structured-layer-abcdef01",
                    "visual_plan_item_id": "visual-beat-abcdef01",
                    "type": layer_type,
                    "payload": payload,
                }
            ]
        }

    def test_stat_requires_per_datum_evidence(self) -> None:
        good = self._layer(
            "stat",
            {
                "value": "87%",
                "label": "留存",
                "evidence_id": "evidence-abcdef01",
                "source_literal": "留存是百分之八十七",
            },
        )
        self.assertEqual(semantic_structured_layer(good), [])
        bad = self._layer("stat", {"value": "87%", "label": "留存"})
        self.assertTrue(semantic_structured_layer(bad))

    def test_chart_datums_each_bind_evidence(self) -> None:
        bad = self._layer(
            "chart",
            {
                "chart_kind": "bar",
                "datums": [{"label": "Q1", "value": 1.0, "evidence_id": "evidence-x"}],
            },
        )
        self.assertTrue(
            any("source_literal" in e for e in semantic_structured_layer(bad))
        )

    def test_dynamic_list_item_needs_evidence_or_conceptual(self) -> None:
        bad = self._layer("dynamic_list", {"items": [{"text": "第一步"}]})
        self.assertTrue(semantic_structured_layer(bad))
        good = self._layer(
            "dynamic_list", {"items": [{"text": "第一步", "conceptual": True}]}
        )
        self.assertEqual(semantic_structured_layer(good), [])

    def test_ocr_spans_forbidden_without_engine(self) -> None:
        analysis = {
            "engines": {"ocr": {"status": "not_configured"}},
            "ocr_spans": [{"start": 0, "end": 1, "text": "x", "confidence": 1.0}],
        }
        self.assertTrue(semantic_video_analysis(analysis))
        analysis["ocr_spans"] = []
        self.assertEqual(semantic_video_analysis(analysis), [])

    def test_structured_beat_asset_exclusivity(self) -> None:
        item = {
            "id": "visual-beat-abcdef01",
            "start": 1.0,
            "end": 2.0,
            "beat": "stat",
            "structured_layer_id": "structured-layer-abcdef01",
            "selected_asset": "asset-1",
            "conceptual_only": True,
            "evidence_ids": [],
        }
        errors = semantic_visual_plan({"items": [item]})
        self.assertTrue(any("selected_asset" in e for e in errors))
        item["beat"] = "broll"
        errors = semantic_visual_plan({"items": [item]})
        self.assertTrue(any("structured layer" in e for e in errors))


class ProviderInterfaceSemanticTests(unittest.TestCase):
    def _provider(self, **overrides) -> dict:
        provider = {
            "id": "openverse",
            "kind": "image",
            "consent_required": True,
            "cost_class": "free",
            "idempotency_key_fields": ["query", "page"],
            "license_class": "open",
            "offline_fallback": "skip",
            "preflight": {
                "required": True,
                "checks": ["reachability", "license-metadata"],
            },
        }
        provider.update(overrides)
        return provider

    def test_provider_ids_and_preflight_requirements(self) -> None:
        duplicate = self._provider()
        duplicate["preflight"] = {"required": False, "checks": ["reachability"]}
        errors = semantic_provider_interface(
            {"providers": [self._provider(), duplicate]}
        )
        self.assertTrue(any("duplicate provider id" in error for error in errors))
        self.assertTrue(any("license-metadata" in error for error in errors))
        self.assertTrue(any("cannot disable preflight" in error for error in errors))

    def test_provider_lists_are_trimmed_nonempty_and_unique(self) -> None:
        provider = self._provider(
            id="generation",
            kind="generation",
            license_class="generated",
            idempotency_key_fields=[" query ", "query", "  "],
            preflight={
                "required": False,
                "checks": [" reachability ", "reachability", ""],
            },
        )
        errors = semantic_provider_interface({"providers": [provider]})
        self.assertTrue(any("idempotency_key_fields" in error for error in errors))
        self.assertTrue(any("preflight.checks" in error for error in errors))

    def test_open_provider_accepts_trimmed_license_check(self) -> None:
        provider = self._provider()
        provider["preflight"]["checks"] = [" reachability ", " license-metadata "]
        self.assertEqual(semantic_provider_interface({"providers": [provider]}), [])


class AssetProvenanceSemanticTests(unittest.TestCase):
    def _asset(self, **overrides) -> dict:
        asset = {
            "asset_id": "asset-1",
            "path": "assets/a.png",
            "sha256": "a" * 64,
            "origin": "folder-import",
            "provider_id": None,
            "source_url": None,
            "license": {
                "spdx": "CC0-1.0",
                "attribution_required": False,
                "attribution_text": "",
                "verified_at": "2026-08-04T03:00:00Z",
            },
            "review_status": "approved",
        }
        asset.update(overrides)
        return asset

    def test_asset_ids_paths_and_normalized_path_safety(self) -> None:
        first = self._asset()
        second = self._asset()
        unsafe = self._asset(
            asset_id="asset-3", path="assets/../outside.png"
        )
        errors = semantic_asset_provenance({"items": [first, second, unsafe]})
        self.assertTrue(any("duplicate asset_id" in error for error in errors))
        self.assertTrue(any("duplicate path" in error for error in errors))
        self.assertTrue(any("normalized POSIX" in error for error in errors))

    def test_asset_origin_relationships_and_secure_provider_url(self) -> None:
        provider = self._asset(
            asset_id="asset-provider",
            path="assets/provider.png",
            origin="provider",
            provider_id=" ",
            source_url="https://user:secret@example.com/provider.png",
        )
        upload = self._asset(
            asset_id="asset-upload",
            path="assets/upload.png",
            origin="user-upload",
            provider_id="provider",
            source_url="https://example.com/upload.png",
        )
        generated = self._asset(
            asset_id="asset-generated",
            path="assets/generated.png",
            origin="generated",
            provider_id="",
        )
        errors = semantic_asset_provenance({"items": [provider, upload, generated]})
        self.assertTrue(any("non-empty provider_id" in error for error in errors))
        self.assertTrue(any("without credentials" in error for error in errors))
        self.assertTrue(any("requires null provider_id" in error for error in errors))
        self.assertTrue(any("requires null source_url" in error for error in errors))

    def test_license_review_and_attribution_rules(self) -> None:
        naive = self._asset(
            asset_id="asset-naive",
            path="assets/naive.png",
            license={
                "spdx": "UNKNOWN",
                "attribution_required": False,
                "attribution_text": "",
                "verified_at": "2026-08-04T03:00:00",
            },
        )
        cc_by = self._asset(
            asset_id="asset-cc-by",
            path="assets/cc-by.png",
            license={
                "spdx": "CC-BY-4.0",
                "attribution_required": True,
                "attribution_text": "",
                "verified_at": "2026-08-04T03:00:00Z",
            },
        )
        errors = semantic_asset_provenance({"items": [naive, cc_by]})
        self.assertTrue(any("timezone-aware ISO-8601" in error for error in errors))
        self.assertTrue(any("attribution_text" in error for error in errors))
        self.assertTrue(any("final-allowlist" in error for error in errors))
        self.assertTrue(any("UNKNOWN cannot be approved" in error for error in errors))

    def test_provider_license_evidence_requires_exact_canonical_path(self) -> None:
        provider = self._asset(
            origin="provider",
            provider_id="openverse",
            source_url="https://openverse.org/image",
            license={
                "spdx": "CC-BY-4.0",
                "evidence_url": "https://creativecommons.org/licenses/by/4.0////",
                "attribution_required": True,
                "attribution_text": "Jane Example",
                "verified_at": "2026-08-04T03:00:00Z",
            },
        )

        errors = semantic_asset_provenance({"items": [provider]})

        self.assertTrue(any("canonical SPDX license URL" in error for error in errors))

    def test_svg_repo_mit_and_isc_require_exact_provider_pinned_license(self) -> None:
        cases = (
            (
                "heroicons",
                "MIT",
                "https://github.com/tailwindlabs/heroicons/blob/0435d4ca364a608cc75e2f8683d374e55abbae26/LICENSE",
            ),
            (
                "lucide",
                "ISC",
                "https://github.com/lucide-icons/lucide/blob/f12b0de177fbc2a6795e99be065887e72b237123/LICENSE",
            ),
        )
        for provider_id, spdx, evidence_url in cases:
            with self.subTest(provider_id=provider_id):
                provider = self._asset(
                    origin="provider",
                    provider_id=provider_id,
                    source_url="https://github.com/example/repo/blob/pin/icon.svg",
                    license={
                        "spdx": spdx,
                        "evidence_url": evidence_url,
                        "attribution_required": True,
                        "attribution_text": "Upstream contributors",
                        "verified_at": "2026-08-04T03:00:00Z",
                    },
                )
                self.assertEqual(semantic_asset_provenance({"items": [provider]}), [])
                hostile = json.loads(json.dumps(provider))
                hostile["license"]["evidence_url"] = "https://attacker.invalid/LICENSE"
                self.assertTrue(
                    any(
                        "canonical SPDX license URL" in error
                        for error in semantic_asset_provenance({"items": [hostile]})
                    )
                )

    def test_font_license_evidence_requires_pinned_google_or_versioned_fontsource_url(self) -> None:
        cases = (
            (
                "google-fonts",
                "OFL-1.1",
                "https://raw.githubusercontent.com/google/fonts/"
                "2796410152d4f9524b68ed46e69c1b60f8e0f7c3/ofl/roboto/OFL.txt",
            ),
            (
                "fontsource",
                "Ubuntu-font-1.0",
                "https://cdn.jsdelivr.net/npm/@fontsource/ubuntu@5.1.0/LICENSE",
            ),
        )
        for provider_id, spdx, evidence_url in cases:
            with self.subTest(provider_id=provider_id):
                provider = self._asset(
                    origin="provider",
                    provider_id=provider_id,
                    source_url="https://fontsource.org/fonts/ubuntu",
                    path="assets/fonts/" + "a" * 64 + ".ttf",
                    license={
                        "spdx": spdx,
                        "evidence_url": evidence_url,
                        "attribution_required": True,
                        "attribution_text": "Font authors",
                        "verified_at": "2026-08-04T03:00:00Z",
                    },
                )
                self.assertEqual(semantic_asset_provenance({"items": [provider]}), [])
                for hostile_url in (
                    evidence_url.replace("2796410152d4f9524b68ed46e69c1b60f8e0f7c3", "main"),
                    evidence_url.replace("@5.1.0", "@latest"),
                    evidence_url.replace(".com/", ".com:443/"),
                ):
                    hostile = json.loads(json.dumps(provider))
                    hostile["license"]["evidence_url"] = hostile_url
                    if hostile_url == evidence_url:
                        continue
                    self.assertTrue(semantic_asset_provenance({"items": [hostile]}))


class BundleSuiteTests(unittest.TestCase):
    def test_bundle_suite_is_green(self) -> None:
        valid_count, invalid_count, failures = run_bundle_suite()
        self.assertEqual(failures, [])
        self.assertGreaterEqual(valid_count, 1)
        self.assertGreaterEqual(invalid_count, 2)

    def test_unknown_evidence_is_rejected_at_bundle_level(self) -> None:
        evidence_map = {
            "schema_version": 1,
            "source_sha256": "a" * 64,
            "transcript_revision": "a" * 64,
            "items": [],
            "revision": "a" * 64,
        }
        artifact = {"evidence_ids": ["evidence-nope"]}
        errors = semantic_evidence_references(artifact, evidence_map)
        self.assertTrue(any("unknown evidence id" in e for e in errors))


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
