"""Phase 2 SFX v2 catalog delivery, verification and density policy."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import sfx_delivery  # noqa: E402
import sfx_mapping  # noqa: E402


def _multi_event_visual_evidence() -> dict:
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
            {
                "id": "typing-dropped",
                "start": "0.25",
                "end": "0.45",
                "kind": "title",
                "component_id": "prompt-card",
                "title_kind": "section",
                "motion": {
                    "requested": "word-cascade",
                    "delivered": "word-cascade",
                    "faithful": True,
                    "status": "rendered",
                },
            },
            {
                "id": "row-1",
                "start": "0.70",
                "end": "1.10",
                "kind": "dynamic_list",
                "component_id": "dynamic-list",
                "motion": {
                    "requested": "staggered-reveal",
                    "delivered": "staggered-reveal",
                    "faithful": True,
                    "status": "rendered",
                },
            },
            {
                "id": "count-1",
                "start": "1.20",
                "end": "1.60",
                "kind": "stat",
                "component_id": "hero-stat",
                "motion": {
                    "requested": "count-up",
                    "delivered": "count-up",
                    "faithful": True,
                    "status": "rendered",
                },
            },
            {
                "id": "grid-1",
                "start": "1.70",
                "end": "2.10",
                "kind": "chart",
                "component_id": "dashboard",
                "family": "grid_progress",
                "trigger_role": "grid_complete",
                "motion": {
                    "requested": "pan",
                    "delivered": "pan",
                    "faithful": True,
                    "status": "native",
                },
            },
        ],
    }


class StageMultiEventDeliveryV1UnchangedTests(unittest.TestCase):
    def test_default_profile_matches_explicit_v1_profile_and_private_v1_function(self) -> None:
        evidence = _multi_event_visual_evidence()
        revision, cut_hash = "a" * 64, "b" * 64
        with tempfile.TemporaryDirectory() as default_dir, \
                tempfile.TemporaryDirectory() as explicit_dir, \
                tempfile.TemporaryDirectory() as private_dir:
            default_paths = sfx_delivery.stage_multi_event_delivery(
                Path(default_dir), evidence, revision, cut_hash
            )
            explicit_paths = sfx_delivery.stage_multi_event_delivery(
                Path(explicit_dir), evidence, revision, cut_hash, catalog_profile="v1"
            )
            private_paths = sfx_delivery._stage_multi_event_delivery_v1(
                Path(private_dir), evidence, revision, cut_hash
            )
            for default_path, explicit_path, private_path in zip(
                default_paths, explicit_paths, private_paths
            ):
                default_bytes = default_path.read_bytes()
                self.assertEqual(default_bytes, explicit_path.read_bytes())
                self.assertEqual(default_bytes, private_path.read_bytes())

    def test_unknown_catalog_profile_is_rejected(self) -> None:
        evidence = _multi_event_visual_evidence()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(sfx_delivery.SfxDeliveryError):
                sfx_delivery.stage_multi_event_delivery(
                    Path(directory), evidence, "a" * 64, "b" * 64, catalog_profile="v3"
                )


class StageMultiEventDeliveryV2Tests(unittest.TestCase):
    def test_v2_catalog_has_nine_procedural_assets_and_verifier_passes(self) -> None:
        evidence = _multi_event_visual_evidence()
        revision, cut_hash = "a" * 64, "b" * 64
        with tempfile.TemporaryDirectory() as directory:
            plan_path, catalog_path, stem_path = sfx_delivery.stage_multi_event_delivery(
                Path(directory), evidence, revision, cut_hash, catalog_profile="v2", include_external=False
            )
            import json

            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            self.assertEqual(plan["schema_version"], 3)
            self.assertEqual(len(catalog["assets"]), 9)
            self.assertEqual(
                {asset["asset_id"] for asset in catalog["assets"]},
                set(sfx_mapping.V2_PROCEDURAL_ASSET_IDS),
            )
            report = sfx_delivery.verify_delivery(
                plan_path, catalog_path, stem_path, evidence, revision, cut_hash
            )
            self.assertEqual(report["status"], "pass", report["failures"])
            self.assertEqual(report["delivered_event_count"], len(plan["events"]))

    def test_category_rate_limit_drops_second_click_pop_event_in_window(self) -> None:
        evidence = _multi_event_visual_evidence()
        revision, cut_hash = "a" * 64, "b" * 64
        with tempfile.TemporaryDirectory() as directory:
            plan_path, _, _ = sfx_delivery.stage_multi_event_delivery(
                Path(directory), evidence, revision, cut_hash, catalog_profile="v2"
            )
            import json

            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            dropped_reasons = {
                item["trigger_id"]: item["reason"] for item in plan["density"]["dropped"]
            }
            self.assertEqual(dropped_reasons.get("row-1"), "category_rate_limit")
            self.assertEqual(
                plan["density"]["dropped_reasons"].get("category_rate_limit"), 1
            )

    def test_catalog_revision_sha256_present_and_matches_staged_catalog(self) -> None:
        evidence = _multi_event_visual_evidence()
        revision, cut_hash = "a" * 64, "b" * 64
        with tempfile.TemporaryDirectory() as directory:
            plan_path, catalog_path, _ = sfx_delivery.stage_multi_event_delivery(
                Path(directory), evidence, revision, cut_hash, catalog_profile="v2"
            )
            import json

            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            catalog_revision_sha256 = plan["catalog_revision_sha256"]
            self.assertEqual(len(catalog_revision_sha256), 64)
            self.assertTrue(all(char in "0123456789abcdef" for char in catalog_revision_sha256))
            self.assertEqual(catalog_revision_sha256, sfx_delivery._canonical_hash(catalog))


class DensityCatalogExternalAssetFailClosedTests(unittest.TestCase):
    def test_external_asset_not_in_sfx_library_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, catalog_path, _ = sfx_delivery.stage_multi_event_delivery(
                Path(directory), _multi_event_visual_evidence(), "a" * 64, "b" * 64,
                catalog_profile="v2", include_external=False,
            )
            import json

            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        catalog["assets"].append({
            "asset_id": "unknown-external-v1",
            "pack": "external-licensed",
            "role": "content_hint",
            "category": "cash_coin",
            "duration_samples": 4800,
            "transient_anchor_sample": 0,
            "noise_floor_dbfs": -90.0,
            "rms_dbfs": -18.0,
            "peak_dbfs": -3.0,
            "wav_sha256": "a" * 64,
            "decoded_pcm_sha256": "b" * 64,
            "provenance": "external test fixture",
            "review_state": "approved_licensed",
            "license": {
                "name": "test-license",
                "source_url": "https://example.invalid/license",
                "commercial_ok": True,
                "attribution_required": False,
            },
            "library_record_sha256": "c" * 64,
            "source_sha256": "d" * 64,
        })
        with self.assertRaises(sfx_delivery.SfxDeliveryError) as ctx:
            sfx_delivery._density_catalog_by_asset_id_v2(catalog)
        self.assertIn("unknown-external-v1", str(ctx.exception))


class SelectAssetTests(unittest.TestCase):
    @staticmethod
    def _catalog(*assets: dict) -> dict:
        return {"assets": list(assets)}

    def test_single_candidate_is_returned_regardless_of_index(self) -> None:
        catalog = self._catalog({"asset_id": "soft-ui-tick-v1", "category": "click_pop"})
        for index in range(3):
            self.assertEqual(
                sfx_delivery.select_asset("click_pop", catalog, event_index=index),
                "soft-ui-tick-v1",
            )

    def test_candidates_rotate_deterministically_by_event_index(self) -> None:
        catalog = self._catalog(
            {"asset_id": "b-asset-v1", "category": "click_pop", "pack": "phase1-local-procedural"},
            {"asset_id": "a-asset-v1", "category": "click_pop", "pack": "phase1-local-procedural"},
        )
        chosen = [
            sfx_delivery.select_asset("click_pop", catalog, event_index=index)
            for index in range(4)
        ]
        self.assertEqual(chosen, ["a-asset-v1", "b-asset-v1", "a-asset-v1", "b-asset-v1"])

    def test_prefer_external_sorts_external_candidates_first(self) -> None:
        catalog = self._catalog(
            {"asset_id": "a-local-v1", "category": "click_pop", "pack": "phase1-local-procedural"},
            {"asset_id": "z-external-v1", "category": "click_pop", "pack": "external-licensed"},
        )
        self.assertEqual(
            sfx_delivery.select_asset("click_pop", catalog, event_index=0, prefer="external"),
            "z-external-v1",
        )

    def test_missing_category_raises_without_silent_fallback(self) -> None:
        catalog = self._catalog({"asset_id": "a-asset-v1", "category": "click_pop"})
        with self.assertRaises(sfx_delivery.SfxDeliveryError):
            sfx_delivery.select_asset("error_glitch", catalog, event_index=0)


class FillInSoundTests(unittest.TestCase):
    def _generate(self, asset_id: str) -> tuple[dict, dict]:
        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            first_path = Path(first_dir) / sfx_mapping.V2_PROCEDURAL_ASSET_FILENAMES[asset_id]
            second_path = Path(second_dir) / sfx_mapping.V2_PROCEDURAL_ASSET_FILENAMES[asset_id]
            first = sfx_delivery._generate_local_procedural_asset(first_path, asset_id)
            second = sfx_delivery._generate_local_procedural_asset(second_path, asset_id)
            self.assertEqual(first_path.read_bytes(), second_path.read_bytes())
            return first, second

    def test_coin_fill_in_is_deterministic_and_within_dbfs_range(self) -> None:
        first, second = self._generate("coin-blip-v1")
        self.assertEqual(first, second)
        self.assertEqual(first["wav_sha256"], second["wav_sha256"])
        self.assertTrue(-4.0 <= first["peak_dbfs"] <= -2.0)
        self.assertGreater(first["rms_dbfs"], -45.0)
        self.assertEqual(first["role"], "content_hint")

    def test_glitch_fill_in_is_deterministic_and_within_dbfs_range(self) -> None:
        first, second = self._generate("glitch-buzz-v1")
        self.assertEqual(first, second)
        self.assertEqual(first["wav_sha256"], second["wav_sha256"])
        self.assertTrue(-4.0 <= first["peak_dbfs"] <= -2.0)
        self.assertGreater(first["rms_dbfs"], -45.0)
        self.assertEqual(first["role"], "content_hint")


if __name__ == "__main__":
    unittest.main()
