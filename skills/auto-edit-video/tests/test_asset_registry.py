"""Tests for the project-scoped provenance registry and attribution projection."""

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

import asset_registry  # noqa: E402


class AssetRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def item(
        self,
        asset_id: str = "asset-a",
        path: str = "assets/a.png",
        *,
        origin: str = "folder-import",
        spdx: str = "CC0-1.0",
        attribution_required: bool = False,
        attribution_text: str = "",
        review_status: str = "approved",
    ) -> dict:
        provider_id = "openverse" if origin == "provider" else None
        source_url = "https://example.test/assets/a" if origin == "provider" else None
        if origin == "generated":
            provider_id = "local-generator"
        return {
            "asset_id": asset_id,
            "path": path,
            "sha256": (asset_id.encode().hex() * 64)[:64],
            "origin": origin,
            "provider_id": provider_id,
            "source_url": source_url,
            "license": {
                "spdx": spdx,
                "evidence_url": (
                    "https://creativecommons.org/licenses/by/4.0/"
                    if origin == "provider" and spdx == "CC-BY-4.0"
                    else (
                        "https://creativecommons.org/licenses/by-sa/4.0/"
                        if origin == "provider" and spdx == "CC-BY-SA-4.0"
                        else (
                        "https://creativecommons.org/publicdomain/zero/1.0/"
                        if origin == "provider" and spdx == "CC0-1.0"
                        else None
                        )
                    )
                ),
                "attribution_required": attribution_required,
                "attribution_text": attribution_text,
                "verified_at": "2026-08-04T03:00:00+00:00",
            },
            "review_status": review_status,
        }

    def save_items(self, *items: dict) -> dict:
        artifact = {"schema_version": 1, "items": list(items)}
        asset_registry.save_registry(self.project, artifact)
        return artifact

    def test_missing_registry_returns_empty_v1_artifact(self) -> None:
        self.assertEqual(
            asset_registry.load_registry(self.project),
            {"schema_version": 1, "items": []},
        )

    def test_invalid_legacy_registry_is_rejected(self) -> None:
        target = self.project / asset_registry.PROVENANCE_REL
        target.parent.mkdir(parents=True)
        target.write_text('{"items": []}', encoding="utf-8")
        before = target.read_bytes()
        with self.assertRaises(asset_registry.AssetRegistryError):
            asset_registry.load_registry(self.project)
        with self.assertRaises(asset_registry.AssetRegistryError):
            asset_registry.migrate_legacy_registry(self.project)
        self.assertEqual(target.read_bytes(), before)
        self.assertFalse((self.project / asset_registry.ATTRIBUTION_REL).exists())

    def write_exact_legacy_registry(self) -> tuple[Path, bytes]:
        asset = self.project / "assets/old.png"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(b"legacy-upload")
        target = self.project / asset_registry.PROVENANCE_REL
        payload = json.dumps(
            {
                "items": [
                    {
                        "file": "assets/old.png",
                        "original_name": "old.png",
                        "source": "user-uploaded-through-local-editor",
                        "bytes": len(asset.read_bytes()),
                        "sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
                        "uploaded_at": "2026-08-04T03:00:00+00:00",
                    }
                ]
            }
        ).encode("utf-8")
        target.write_bytes(payload)
        return target, payload

    def test_load_exact_legacy_registry_is_pure_and_fails_closed(self) -> None:
        target, before = self.write_exact_legacy_registry()
        mtime_before = target.stat().st_mtime_ns

        with self.assertRaises(asset_registry.AssetRegistryError):
            asset_registry.load_registry(self.project)

        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(target.stat().st_mtime_ns, mtime_before)
        self.assertFalse((self.project / asset_registry.ATTRIBUTION_REL).exists())

    def test_explicit_exact_legacy_manual_upload_migration_is_atomic(self) -> None:
        self.write_exact_legacy_registry()

        migrated = asset_registry.migrate_legacy_registry(self.project)

        self.assertEqual(migrated["schema_version"], 1)
        self.assertEqual(migrated["items"][0]["origin"], "user-upload")
        self.assertEqual(migrated["items"][0]["review_status"], "pending")
        self.assertEqual(migrated["items"][0]["license"]["spdx"], "UNKNOWN")
        self.assertEqual(asset_registry.load_registry(self.project), migrated)
        self.assertTrue((self.project / asset_registry.ATTRIBUTION_REL).is_file())

    def test_upsert_migrates_exact_legacy_before_mutation(self) -> None:
        self.write_exact_legacy_registry()
        added = self.item(
            asset_id="asset-new",
            path="assets/new.png",
            origin="user-upload",
            spdx="UNKNOWN",
            review_status="pending",
        )

        result = asset_registry.upsert_item(self.project, added)

        self.assertEqual(
            {item["asset_id"] for item in result["items"]},
            {"asset-legacy-" + hashlib.sha256(b"assets/old.png").hexdigest()[:20], "asset-new"},
        )

    def test_failed_validation_preserves_previous_file_and_partial_cleanup(self) -> None:
        original = self.save_items(self.item())
        target = self.project / asset_registry.PROVENANCE_REL
        before = target.read_bytes()
        invalid = {"schema_version": 1, "items": [{"asset_id": "missing"}]}
        with self.assertRaises(asset_registry.AssetRegistryError):
            asset_registry.save_registry(self.project, invalid)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(list(target.parent.glob("*.part")), [])
        self.assertEqual(asset_registry.load_registry(self.project), original)

    def test_save_refuses_provenance_symlink(self) -> None:
        target = self.project / asset_registry.PROVENANCE_REL
        target.parent.mkdir(parents=True)
        outside = self.project / "outside.json"
        outside.write_text("sentinel", encoding="utf-8")
        target.symlink_to(outside)
        with self.assertRaises(asset_registry.AssetRegistryError):
            self.save_items(self.item())
        self.assertEqual(outside.read_text(encoding="utf-8"), "sentinel")

    def test_upsert_rejects_path_and_asset_id_conflicts(self) -> None:
        self.save_items(self.item())
        with self.assertRaises(asset_registry.AssetRegistryError):
            asset_registry.upsert_item(
                self.project,
                self.item(asset_id="asset-b", path="assets/a.png"),
            )
        with self.assertRaises(asset_registry.AssetRegistryError):
            asset_registry.upsert_item(
                self.project,
                self.item(asset_id="asset-a", path="assets/other.png"),
            )

    def test_upsert_rolls_back_registry_when_attribution_publish_fails(self) -> None:
        original = self.save_items(self.item())
        asset_registry.refresh_attribution(self.project, original)
        registry_path = self.project / asset_registry.PROVENANCE_REL
        attribution_path = self.project / asset_registry.ATTRIBUTION_REL
        registry_before = registry_path.read_bytes()
        attribution_before = attribution_path.read_bytes()
        real_write = asset_registry._atomic_write_bytes
        failed = False

        def fail_attribution_once(path: Path, payload: bytes, label: str) -> None:
            nonlocal failed
            if label == "ATTRIBUTION.md" and not failed:
                failed = True
                raise asset_registry.AssetRegistryError("simulated attribution failure")
            real_write(path, payload, label)

        with patch.object(asset_registry, "_atomic_write_bytes", fail_attribution_once):
            with self.assertRaises(asset_registry.AssetRegistryError):
                asset_registry.upsert_item(
                    self.project, self.item(asset_id="asset-b", path="assets/b.png")
                )

        self.assertEqual(registry_path.read_bytes(), registry_before)
        self.assertEqual(attribution_path.read_bytes(), attribution_before)

    def test_upsert_reports_rollback_failure_explicitly(self) -> None:
        original = self.save_items(self.item())
        asset_registry.refresh_attribution(self.project, original)
        real_write = asset_registry._atomic_write_bytes
        attribution_failed = False

        def fail_publish_and_registry_rollback(
            path: Path, payload: bytes, label: str
        ) -> None:
            nonlocal attribution_failed
            if label == "ATTRIBUTION.md" and not attribution_failed:
                attribution_failed = True
                raise asset_registry.AssetRegistryError("simulated attribution failure")
            if label == "provenance registry" and attribution_failed:
                raise asset_registry.AssetRegistryError("simulated rollback failure")
            real_write(path, payload, label)

        with patch.object(
            asset_registry, "_atomic_write_bytes", fail_publish_and_registry_rollback
        ):
            with self.assertRaises(asset_registry.AssetRegistryError) as raised:
                asset_registry.upsert_item(
                    self.project, self.item(asset_id="asset-b", path="assets/b.png")
                )

        self.assertIn("rollback failed", str(raised.exception))

    def test_current_item_requires_matching_hash(self) -> None:
        item = self.item()
        self.save_items(item)
        self.assertEqual(
            asset_registry.current_item(self.project, item["path"], item["sha256"]),
            item,
        )
        self.assertIsNone(
            asset_registry.current_item(self.project, item["path"], "0" * 64)
        )
        self.assertIsNone(
            asset_registry.current_item(self.project, "assets/other.png", item["sha256"])
        )

    def test_provider_license_allowlist_and_attribution_requirements(self) -> None:
        allowed = self.item(
            origin="provider",
            spdx="CC-BY-4.0",
            attribution_required=True,
            attribution_text="Author",
        )
        self.assertEqual(asset_registry.auto_license_errors(allowed), [])

        rejected = dict(allowed)
        rejected["license"] = dict(allowed["license"], spdx="GPL-3.0")
        self.assertTrue(asset_registry.auto_license_errors(rejected))

        missing_attr = dict(allowed)
        missing_attr["license"] = dict(
            allowed["license"], attribution_required=False, attribution_text=""
        )
        self.assertTrue(asset_registry.auto_license_errors(missing_attr))

        pending = dict(allowed, review_status="pending")
        self.assertTrue(asset_registry.auto_license_errors(pending))

    def test_provider_auto_license_requires_hash_bound_consistency_evidence(self) -> None:
        item = self.item(
            asset_id="provider-openverse-a",
            path="assets/providers/openverse/a.png",
            origin="provider",
            spdx="CC-BY-4.0",
            attribution_required=True,
            attribution_text="Author",
        )
        item["license"]["evidence_url"] = (
            "https://creativecommons.org/licenses/by/4.0/"
        )
        self.assertTrue(asset_registry.provider_consistency_errors(self.project, item))
        unknown_provider = dict(item, provider_id="custom-provider")
        self.assertTrue(
            any(
                "built-in provider" in error
                for error in asset_registry.provider_consistency_errors(
                    self.project, unknown_provider
                )
            )
        )
        wrong_path = dict(item, path="assets/providers/wikimedia/a.png")
        self.assertTrue(
            any(
                "path does not match" in error
                for error in asset_registry.provider_consistency_errors(
                    self.project, wrong_path
                )
            )
        )

        receipt = asset_registry.save_provider_receipt(
            self.project,
            item,
            candidate_id="candidate-a",
            download_url="https://api.openverse.org/v1/images/candidate-a/thumb/",
        )

        self.assertEqual(asset_registry.provider_consistency_errors(self.project, item), [])
        self.assertEqual(receipt["asset_sha256"], item["sha256"])
        self.assertNotIn("download_url", receipt)
        tampered = dict(item, sha256="0" * 64)
        self.assertTrue(asset_registry.provider_consistency_errors(self.project, tampered))

    def test_attribution_is_sorted_and_control_text_is_one_line(self) -> None:
        first = self.item(
            asset_id="asset-z",
            path="assets/z.png",
            origin="provider",
            spdx="CC-BY-4.0",
            attribution_required=True,
            attribution_text="Zed\n- forged bullet\r# forged heading",
        )
        second = self.item(
            asset_id="asset-a",
            path="assets/a2.png",
            origin="provider",
            spdx="CC-BY-SA-4.0",
            attribution_required=True,
            attribution_text="Ada\tCreator",
        )
        artifact = {"schema_version": 1, "items": [first, second]}
        markdown = asset_registry.attribution_markdown(artifact)
        self.assertLess(markdown.index("asset-a"), markdown.index("asset-z"))
        self.assertIn("Source URL: https&#x3A;&#x2F;&#x2F;example", markdown)
        self.assertIn("SPDX: CC-BY-SA-4.0", markdown)
        self.assertNotIn("\n- forged bullet", markdown)
        self.assertNotIn("\n# forged heading", markdown)
        self.assertEqual(markdown, asset_registry.attribution_markdown({**artifact, "items": [second, first]}))

    def test_attribution_escapes_hostile_html_markdown_and_remote_images(self) -> None:
        hostile = self.item(
            origin="provider",
            spdx="CC-BY-4.0",
            attribution_required=True,
            attribution_text=(
                '<script>alert(1)</script> ![x](https://tracker.invalid/pixel.png)'
            ),
        )
        hostile["source_url"] = "https://tracker.invalid/source.png)"

        markdown = asset_registry.attribution_markdown(
            {"schema_version": 1, "items": [hostile]}
        )

        self.assertNotIn("<script", markdown)
        self.assertNotIn("![", markdown)
        self.assertNotIn("https://tracker.invalid", markdown)
        self.assertIn("alert", markdown)

    def test_attribution_missing_and_tampered_are_detected(self) -> None:
        item = self.item(
            origin="provider",
            spdx="CC-BY-4.0",
            attribution_required=True,
            attribution_text="Author",
        )
        artifact = self.save_items(item)
        self.assertTrue(asset_registry.attribution_errors(self.project))
        asset_registry.refresh_attribution(self.project, artifact)
        self.assertEqual(asset_registry.attribution_errors(self.project, artifact), [])
        attribution = self.project / asset_registry.ATTRIBUTION_REL
        attribution.write_text("tampered\n", encoding="utf-8")
        self.assertTrue(asset_registry.attribution_errors(self.project, artifact))

    def test_attribution_symlink_is_rejected_and_reported(self) -> None:
        outside = self.project / "outside.md"
        outside.write_text("outside", encoding="utf-8")
        target = self.project / asset_registry.ATTRIBUTION_REL
        target.symlink_to(outside)
        with self.assertRaises(asset_registry.AssetRegistryError):
            asset_registry.refresh_attribution(self.project)
        self.assertTrue(asset_registry.attribution_errors(self.project))

    def test_empty_attribution_still_requires_deterministic_file(self) -> None:
        self.assertIn("無須列名", asset_registry.attribution_markdown({"schema_version": 1, "items": []}))
        # No projection exists yet; attribution_errors is the non-writing gate.
        self.assertTrue(asset_registry.attribution_errors(self.project))

    def test_atomic_replace_failure_cleans_partial(self) -> None:
        self.save_items(self.item())
        target = self.project / asset_registry.PROVENANCE_REL
        before = target.read_bytes()
        with patch.object(asset_registry.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(asset_registry.AssetRegistryError):
                asset_registry.save_registry(self.project, {"schema_version": 1, "items": []})
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(list(target.parent.glob("*.part")), [])


if __name__ == "__main__":
    unittest.main()
