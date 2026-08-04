from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch


SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from asset_provider_service import AssetProviderError, AssetProviderService  # noqa: E402
from hardened_downloader import (  # noqa: E402
    TransportError as DownloadTransportError,
    ValidationError as DownloadValidationError,
)
import asset_registry  # noqa: E402


class FakeClock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


OPENVERSE_ID = "123e4567-e89b-12d3-a456-426614174000"


def openverse_payload(*, width: int = 640, height: int = 480) -> bytes:
    return json.dumps(
        {
            "results": [
                {
                    "id": OPENVERSE_ID,
                    "title": "A cat",
                    "foreign_landing_url": "https://example.org/image",
                    "url": "https://provider.example/untrusted-original.jpg",
                    "creator": "Jane Example",
                    "license": "by",
                    "license_version": "4.0",
                    "license_url": "https://creativecommons.org/licenses/by/4.0/",
                    "attribution": "Jane Example",
                    "mature": False,
                    "width": width,
                    "height": height,
                    "filetype": "jpg",
                }
            ]
        }
    ).encode("utf-8")


def openverse_many(count: int) -> bytes:
    base = json.loads(openverse_payload().decode("utf-8"))["results"][0]
    return json.dumps(
        {
            "results": [
                {
                    **base,
                    "id": f"00000000-0000-4000-8000-{index:012x}",
                    "title": f"Image {index}",
                }
                for index in range(count)
            ]
        }
    ).encode("utf-8")


def wikimedia_payload() -> bytes:
    return json.dumps(
        {
            "query": {
                "pages": [
                    {
                        "pageid": 123,
                        "title": "File:Cat.jpg",
                        "imageinfo": [
                            {
                                "mime": "image/jpeg",
                                "width": 640,
                                "height": 480,
                                "thumburl": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/a.jpg",
                                "descriptionurl": "https://commons.wikimedia.org/wiki/File:Cat.jpg",
                                "extmetadata": {
                                    "LicenseShortName": {"value": "CC BY 4.0"},
                                    "LicenseUrl": {"value": "https://creativecommons.org/licenses/by/4.0/"},
                                    "Attribution": {"value": "Jane Example"},
                                    "AttributionRequired": {"value": "true"},
                                    "Artist": {"value": "Jane Example"},
                                    "Credit": {"value": "Example Archive"},
                                    "NonFree": {"value": "false"},
                                },
                            }
                        ],
                    }
                ]
            }
        }
    ).encode("utf-8")


class FakeDownloader:
    def __init__(self, payloads: list[bytes]) -> None:
        self.payloads = list(payloads)
        self.calls: list[tuple[str, Path, dict[str, Any]]] = []

    def __call__(self, url: str, destination: Path, **kwargs: Any) -> None:
        self.calls.append((url, Path(destination), kwargs))
        if not self.payloads:
            raise AssertionError("unexpected download")
        payload = self.payloads.pop(0)
        part = Path(destination).with_name(f".{Path(destination).name}.fake.part")
        part.write_bytes(payload)
        try:
            try:
                kwargs["validator"](part)
            except Exception as exc:
                raise DownloadValidationError("fake validator rejection") from exc
            os.replace(part, destination)
        finally:
            part.unlink(missing_ok=True)


class FailingImportDownloader(FakeDownloader):
    def __call__(self, url: str, destination: Path, **kwargs: Any) -> None:
        if self.calls:
            self.calls.append((url, Path(destination), kwargs))
            raise DownloadTransportError("simulated network failure")
        super().__call__(url, destination, **kwargs)


def jpeg_bytes(width: int = 2, height: int = 2) -> bytes:
    sof_payload = (
        b"\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03"
        + b"\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    )
    return b"\xff\xd8\xff\xc0" + (17).to_bytes(2, "big") + sof_payload + b"\xff\xd9"


class AssetProviderServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name) / "project"
        self.project.mkdir()
        self.clock = FakeClock()
        self.service = AssetProviderService(
            self.project,
            downloader=lambda *args, **kwargs: None,
            resolver=lambda host, port: ["93.184.216.34"],
            clock=self.clock,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_catalog_and_consent_are_project_scoped_persisted_and_secret_free(self) -> None:
        status = self.service.status()
        self.assertEqual([item["id"] for item in status["providers"]], ["openverse", "wikimedia"])
        self.assertTrue(all(item["kind"] == "image" for item in status["providers"]))
        self.assertTrue(all(item["consent_required"] for item in status["providers"]))
        self.assertTrue(all(item["cost_class"] == "free" for item in status["providers"]))
        self.assertTrue(all(item["network_disclosure"] for item in status["providers"]))

        with self.assertRaises(AssetProviderError) as denied:
            self.service.search("openverse", "private phrase", 1)
        self.assertEqual(denied.exception.status_code, 403)

        granted = self.service.grant_consent("openverse", "nat")
        self.assertTrue(granted["consented"])
        artifact_path = self.project / "working/provider_consents.json"
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        self.assertEqual(artifact["schema_version"], 1)
        self.assertEqual(artifact["items"][0]["provider_id"], "openverse")
        self.assertEqual(artifact["items"][0]["kind"], "image")
        serialized = artifact_path.read_text(encoding="utf-8")
        self.assertNotIn("private phrase", serialized)
        self.assertNotIn("token", serialized.casefold())
        self.assertNotIn("credential", serialized.casefold())

        restarted = AssetProviderService(
            self.project,
            downloader=lambda *args, **kwargs: None,
            resolver=lambda host, port: ["93.184.216.34"],
            clock=self.clock,
        )
        current = next(item for item in restarted.status()["providers"] if item["id"] == "openverse")
        self.assertTrue(current["consented"])
        self.assertEqual(current["confirmed_by"], "nat")

        revoked = restarted.revoke_consent("openverse", "nat")
        self.assertFalse(revoked["consented"])

    def test_search_uses_hardened_policy_and_returns_only_opaque_import_capability(self) -> None:
        downloader = FakeDownloader([openverse_payload()])
        service = AssetProviderService(
            self.project,
            downloader=downloader,
            resolver=lambda host, port: ["93.184.216.34"],
            clock=self.clock,
        )
        service.grant_consent("openverse", "nat")

        result = service.search("openverse", "private phrase", 2)

        self.assertEqual(len(result["items"]), 1)
        public = result["items"][0]
        self.assertNotIn("download_url", public)
        token = public["import_token"]
        self.assertNotIn(OPENVERSE_ID, token)
        self.assertNotIn("api.openverse.org", token)
        url, destination, kwargs = downloader.calls[0]
        self.assertTrue(url.startswith("https://api.openverse.org/v1/images/?"))
        self.assertIn("q=private+phrase", url)
        self.assertEqual(kwargs["allowed_hosts"], frozenset({"api.openverse.org"}))
        self.assertEqual(kwargs["max_bytes"], 1024 * 1024)
        self.assertEqual(
            destination.parent,
            (self.project / "working/provider-cache").resolve(),
        )
        self.assertNotIn("private", destination.name)

        metadata_files = list(destination.parent.glob("*.meta.json"))
        self.assertEqual(len(metadata_files), 1)
        metadata_text = metadata_files[0].read_text(encoding="utf-8")
        self.assertNotIn("private phrase", metadata_text)
        self.assertNotIn(token, metadata_text)

    def test_search_does_not_persist_raw_provider_echo_or_plaintext_query(self) -> None:
        payload = json.loads(openverse_payload().decode("utf-8"))
        payload["provider_echo"] = "private client name"
        downloader = FakeDownloader([json.dumps(payload).encode("utf-8")])
        service = AssetProviderService(
            self.project,
            downloader=downloader,
            resolver=lambda host, port: ["93.184.216.34"],
            clock=self.clock,
        )
        service.grant_consent("openverse", "nat")

        service.search("openverse", "private client name", 1)

        cache = self.project / "working/provider-cache"
        files = list(cache.iterdir())
        self.assertEqual([path.suffixes for path in files], [[".meta", ".json"]])
        persisted = files[0].read_text(encoding="utf-8")
        self.assertNotIn("private client name", persisted)
        self.assertNotIn("provider_echo", persisted)

    def test_sanitized_search_metadata_retention_is_bounded(self) -> None:
        downloader = FakeDownloader([openverse_payload() for _ in range(205)])
        service = AssetProviderService(
            self.project,
            downloader=downloader,
            resolver=lambda host, port: ["93.184.216.34"],
            clock=self.clock,
        )
        service.grant_consent("openverse", "nat")

        for page in range(1, 206):
            service.search("openverse", "cat", page)

        metadata = list((self.project / "working/provider-cache").glob("*.meta.json"))
        self.assertLessEqual(len(metadata), 200)

    def test_search_rejects_strict_json_failures_and_oversized_claims(self) -> None:
        bad_payloads = [
            b'{"results":[],"results":[]}',
            b'{"results":[],"score":NaN}',
            b'{"results":[],"score":1e999}',
            b'{"unexpected":[]}',
            b"\xff",
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                downloader = FakeDownloader([payload])
                service = AssetProviderService(
                    self.project,
                    downloader=downloader,
                    resolver=lambda host, port: ["93.184.216.34"],
                    clock=self.clock,
                )
                service.grant_consent("openverse", "nat")
                with self.assertRaises(AssetProviderError) as raised:
                    service.search("openverse", "cat", 1)
                self.assertEqual(raised.exception.status_code, 422)
                self.assertNotIn("cat", str(raised.exception))

        downloader = FakeDownloader([openverse_payload(width=9000, height=100)])
        service = AssetProviderService(
            self.project,
            downloader=downloader,
            resolver=lambda host, port: ["93.184.216.34"],
            clock=self.clock,
        )
        service.grant_consent("openverse", "nat")
        self.assertEqual(service.search("openverse", "cat", 1)["items"], [])

    def test_wikimedia_search_is_pinned_to_commons_api(self) -> None:
        downloader = FakeDownloader([wikimedia_payload()])
        service = AssetProviderService(
            self.project,
            downloader=downloader,
            resolver=lambda host, port: ["93.184.216.34"],
            clock=self.clock,
        )
        service.grant_consent("wikimedia", "nat")
        result = service.search("wikimedia", "cat", 3)
        self.assertEqual(result["items"][0]["provider_id"], "wikimedia")
        url, _destination, kwargs = downloader.calls[0]
        self.assertTrue(url.startswith("https://commons.wikimedia.org/w/api.php?"))
        self.assertIn("gsroffset=40", url)
        self.assertEqual(
            kwargs["allowed_hosts"],
            frozenset({"commons.wikimedia.org"}),
        )

    def test_import_is_single_use_validated_registered_attributed_and_idempotent(self) -> None:
        downloader = FakeDownloader(
            [openverse_payload(), jpeg_bytes(), openverse_payload()]
        )
        service = AssetProviderService(
            self.project,
            downloader=downloader,
            resolver=lambda host, port: ["93.184.216.34"],
            clock=self.clock,
        )
        service.grant_consent("openverse", "nat")
        token = service.search("openverse", "cat", 1)["items"][0]["import_token"]
        validated: list[Path] = []

        imported = service.import_candidate(
            token,
            lambda path: validated.append(path) is None,
        )

        self.assertFalse(imported["idempotent"])
        destination = self.project / imported["source"]
        self.assertEqual(destination.read_bytes(), jpeg_bytes())
        self.assertEqual(len(validated), 1)
        _url, _path, import_policy = downloader.calls[1]
        self.assertEqual(
            import_policy["allowed_hosts"],
            frozenset({"api.openverse.org"}),
        )
        self.assertEqual(import_policy["max_bytes"], 25 * 1024 * 1024)
        provenance = json.loads(
            (self.project / "assets/provenance.json").read_text(encoding="utf-8")
        )
        item = provenance["items"][0]
        self.assertEqual(item["origin"], "provider")
        self.assertEqual(item["provider_id"], "openverse")
        self.assertEqual(item["source_url"], "https://example.org/image")
        self.assertEqual(item["license"]["spdx"], "CC-BY-4.0")
        self.assertEqual(
            item["license"]["evidence_url"],
            "https://creativecommons.org/licenses/by/4.0/",
        )
        self.assertEqual(item["review_status"], "approved")
        attribution = (self.project / "ATTRIBUTION.md").read_text(encoding="utf-8")
        self.assertIn("Jane Example", attribution)
        self.assertNotIn(token, (self.project / "assets/provenance.json").read_text("utf-8"))

        with self.assertRaises(AssetProviderError) as reused:
            service.import_candidate(token, lambda path: True)
        self.assertEqual(reused.exception.status_code, 404)

        second_token = service.search("openverse", "cat", 1)["items"][0]["import_token"]
        again = service.import_candidate(second_token, lambda path: True)
        self.assertTrue(again["idempotent"])
        self.assertEqual(len(downloader.calls), 3, "idempotent import must not download again")

    def test_import_tokens_expire_and_cache_evicts_oldest_at_two_hundred(self) -> None:
        expiring_downloader = FakeDownloader([openverse_payload()])
        expiring = AssetProviderService(
            self.project,
            downloader=expiring_downloader,
            resolver=lambda host, port: ["93.184.216.34"],
            clock=self.clock,
            token_ttl_s=10,
        )
        expiring.grant_consent("openverse", "nat")
        expired_token = expiring.search("openverse", "cat", 1)["items"][0]["import_token"]
        self.clock.advance(11)
        with self.assertRaises(AssetProviderError) as expired:
            expiring.import_candidate(expired_token, lambda path: True)
        self.assertEqual(expired.exception.status_code, 404)

        payloads = [openverse_many(20) for _ in range(11)] + [jpeg_bytes()]
        downloader = FakeDownloader(payloads)
        bounded = AssetProviderService(
            self.project,
            downloader=downloader,
            resolver=lambda host, port: ["93.184.216.34"],
            clock=self.clock,
        )
        first_token = last_token = ""
        for index in range(11):
            result = bounded.search("openverse", f"cat {index}", 1)
            if index == 0:
                first_token = result["items"][0]["import_token"]
            last_token = result["items"][-1]["import_token"]
        with self.assertRaises(AssetProviderError) as evicted:
            bounded.import_candidate(first_token, lambda path: True)
        self.assertEqual(evicted.exception.status_code, 404)
        imported = bounded.import_candidate(last_token, lambda path: True)
        self.assertFalse(imported["idempotent"])

    def test_import_rejects_mime_decode_and_actual_dimension_failures_without_artifacts(self) -> None:
        downloader = FakeDownloader(
            [
                openverse_payload(),
                b"\x89PNG\r\n\x1a\n" + b"bad-mime",
                openverse_payload(),
                jpeg_bytes(width=9000, height=2),
                openverse_payload(),
                jpeg_bytes(),
            ]
        )
        service = AssetProviderService(
            self.project,
            downloader=downloader,
            resolver=lambda host, port: ["93.184.216.34"],
            clock=self.clock,
        )
        service.grant_consent("openverse", "nat")
        validators = [lambda path: True, lambda path: True, lambda path: False]
        for validator in validators:
            token = service.search("openverse", "cat", 1)["items"][0]["import_token"]
            with self.assertRaises(AssetProviderError) as rejected:
                service.import_candidate(token, validator)
            self.assertEqual(rejected.exception.status_code, 422)
            destination = (
                self.project
                / "assets/providers/openverse"
                / f"{OPENVERSE_ID}.jpg"
            )
            self.assertFalse(destination.exists())
            self.assertFalse((self.project / "assets/provenance.json").exists())

    def test_download_and_registry_failures_cleanup_new_file_but_preserve_preexisting(self) -> None:
        failing_download = FailingImportDownloader([openverse_payload()])
        service = AssetProviderService(
            self.project,
            downloader=failing_download,
            resolver=lambda host, port: ["93.184.216.34"],
            clock=self.clock,
        )
        service.grant_consent("openverse", "nat")
        token = service.search("openverse", "cat", 1)["items"][0]["import_token"]
        with self.assertRaises(AssetProviderError) as network:
            service.import_candidate(token, lambda path: True)
        self.assertEqual(network.exception.status_code, 502)

        registry_downloader = FakeDownloader([openverse_payload(), jpeg_bytes()])
        service = AssetProviderService(
            self.project,
            downloader=registry_downloader,
            resolver=lambda host, port: ["93.184.216.34"],
            clock=self.clock,
        )
        token = service.search("openverse", "cat", 1)["items"][0]["import_token"]
        destination = self.project / "assets/providers/openverse" / f"{OPENVERSE_ID}.jpg"
        with patch.object(
            asset_registry,
            "upsert_item",
            side_effect=asset_registry.AssetRegistryError("simulated conflict"),
        ):
            with self.assertRaises(AssetProviderError) as registry:
                service.import_candidate(token, lambda path: True)
        self.assertEqual(registry.exception.status_code, 409)
        self.assertFalse(destination.exists())
        self.assertFalse(
            (self.project / "assets/provenance.json").exists(),
            "failed registry publish must not create a new registry artifact",
        )
        receipts = self.project / asset_registry.PROVIDER_RECEIPTS_REL
        self.assertEqual(list(receipts.glob("*.json")) if receipts.exists() else [], [])

        conflict_downloader = FakeDownloader([openverse_payload()])
        service = AssetProviderService(
            self.project,
            downloader=conflict_downloader,
            resolver=lambda host, port: ["93.184.216.34"],
            clock=self.clock,
        )
        token = service.search("openverse", "cat", 1)["items"][0]["import_token"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"preexisting-unregistered")
        with self.assertRaises(AssetProviderError) as conflict:
            service.import_candidate(token, lambda path: True)
        self.assertEqual(conflict.exception.status_code, 409)
        self.assertEqual(destination.read_bytes(), b"preexisting-unregistered")

    def test_consent_storage_rejects_symlinked_working_directory(self) -> None:
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        working = self.project / "working"
        working.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(AssetProviderError) as rejected:
            self.service.status()
        self.assertEqual(rejected.exception.status_code, 409)
        self.assertEqual(list(outside.iterdir()), [])

    def test_malformed_consent_artifact_fails_closed_without_echoing_data(self) -> None:
        path = self.project / "working/provider_consents.json"
        path.parent.mkdir()
        malformed_items = [
            {
                "provider_id": "openverse",
                "kind": "image",
                "consented": True,
                "consented_at": "not-a-timeZ",
                "confirmed_by": "nat",
            },
            {
                "provider_id": [],
                "kind": "image",
                "consented": True,
                "consented_at": "2026-08-04T00:00:00Z",
                "confirmed_by": "nat",
            },
            {
                "provider_id": "openverse",
                "kind": "image",
                "consented": True,
                "consented_at": "2026-08-04T00:00:00Z",
                "confirmed_by": "secret\nquery",
            },
        ]
        for item in malformed_items:
            with self.subTest(item=item):
                path.write_text(
                    json.dumps({"schema_version": 1, "items": [item]}),
                    encoding="utf-8",
                )
                with self.assertRaises(AssetProviderError) as rejected:
                    self.service.status()
                self.assertEqual(rejected.exception.status_code, 409)
                self.assertNotIn("secret", str(rejected.exception))


if __name__ == "__main__":
    unittest.main()
