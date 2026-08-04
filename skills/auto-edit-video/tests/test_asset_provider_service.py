from __future__ import annotations

import binascii
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
import struct
from typing import Any
from unittest.mock import patch
import zlib


SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))
sys.path.insert(0, str(SKILL_DIR / "tests"))

from asset_provider_service import AssetProviderError, AssetProviderService  # noqa: E402
from hardened_downloader import (  # noqa: E402
    TransportError as DownloadTransportError,
    ValidationError as DownloadValidationError,
)
import asset_registry  # noqa: E402
from font_test_fixture import build_ttf  # noqa: E402


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


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    checksum = binascii.crc32(kind)
    checksum = binascii.crc32(payload, checksum) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)


def strict_png(width: int, height: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    rows = b"".join(b"\0" + b"\x22" * (width * 4) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(rows))
        + _png_chunk(b"IEND", b"")
    )


BENIGN_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    b'<path fill="currentColor" d="M2 2h20v20H2z"/></svg>'
)

OFL_LICENSE = (SKILL_DIR / "contracts/licenses/OFL-1.1.txt").read_bytes()
APACHE_LICENSE = (SKILL_DIR / "contracts/licenses/Apache-2.0.txt").read_bytes()


def google_font_payload(
    *,
    name: str = "PhaseTwo.ttf",
    sha: str | None = None,
    root: str = "ofl",
    license_raw: bytes = OFL_LICENSE,
) -> bytes:
    license_name = {"ofl": "OFL.txt", "apache": "LICENSE.txt", "ufl": "UFL.txt"}[root]
    if sha is None:
        font = build_ttf()
        sha = hashlib.sha1(
            f"blob {len(font)}\0".encode("ascii") + font,
            usedforsecurity=False,
        ).hexdigest()
    return json.dumps(
        [
            {
                "name": name,
                "path": f"{root}/phasetwotest/{name}",
                "type": "file",
                "size": 2048,
                "sha": sha,
            },
            {
                "name": license_name,
                "path": f"{root}/phasetwotest/{license_name}",
                "type": "file",
                "size": len(license_raw),
                "sha": "b" * 40,
            },
        ]
    ).encode("utf-8")


def fontsource_font_payload(*, license_spdx: str = "OFL-1.1") -> bytes:
    return json.dumps(
        {
            "id": "phase-two-test",
            "family": "Phase Two Test",
            "version": "5.1.0",
            "license": {
                "id": license_spdx,
                "url": {
                    "OFL-1.1": "https://scripts.sil.org/OFL",
                    "Apache-2.0": "https://www.apache.org/licenses/LICENSE-2.0",
                }[license_spdx],
            },
            "variants": {
                "400": {
                    "normal": {
                        "latin": {
                            "url": "https://cdn.jsdelivr.net/fontsource/fonts/phase-two-test@5.1.0/latin-400-normal.ttf",
                            "unicodeRange": "U+0000-00FF",
                        }
                    }
                }
            },
        }
    ).encode("utf-8")
_DEFAULT_RASTER_METADATA = object()


class FakeSvgPipeline:
    """Available test pipeline that never impersonates ResvgRasterizer."""

    def __init__(
        self,
        *,
        png_payload: bytes | None = None,
        raster_metadata: Any = _DEFAULT_RASTER_METADATA,
    ) -> None:
        self.preflight_calls = 0
        self.rasterize_calls = 0
        self.png_payload = png_payload
        self.raster_metadata = raster_metadata
        self.identity = {
            "version": "resvg-test-1",
            "executable_sha256": "a" * 64,
            "sandbox_executable_sha256": "b" * 64,
            "sandbox_profile_sha256": "c" * 64,
        }

    def preflight(self) -> SimpleNamespace:
        self.preflight_calls += 1
        return SimpleNamespace(available=True, checks_ok=True, code="OK", identity=dict(self.identity))

    def rasterize(self, sanitized: Any) -> SimpleNamespace:
        self.rasterize_calls += 1
        width = sanitized.metadata["requested_width"]
        height = sanitized.metadata["requested_height"]
        payload = self.png_payload or strict_png(width, height)
        return SimpleNamespace(
            png_bytes=payload,
            png_sha256=hashlib.sha256(payload).hexdigest(),
            width=width,
            height=height,
            metadata=(
                dict(self.identity)
                if self.raster_metadata is _DEFAULT_RASTER_METADATA
                else self.raster_metadata
            ),
        )


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
        self.assertEqual(
            [item["id"] for item in status["providers"]],
            [
                "openverse", "wikimedia", "heroicons", "lucide", "tabler",
                "wikimedia-svg", "google-fonts", "fontsource",
            ],
        )
        self.assertEqual(
            [item["kind"] for item in status["providers"]],
            ["image", "image", "svg", "svg", "svg", "svg", "font", "font"],
        )
        self.assertTrue(all(item["available"] for item in status["providers"][:2]))
        self.assertTrue(all(item["availability_code"] == "available" for item in status["providers"][:2]))
        self.assertTrue(all(not item["available"] for item in status["providers"][2:6]))
        self.assertTrue(all(item["availability_code"] == "svg_rasterizer_unavailable" for item in status["providers"][2:6]))
        self.assertTrue(all(item["available"] for item in status["providers"][6:]))
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

    def test_unavailable_svg_rejects_before_query_or_download(self) -> None:
        downloader = FakeDownloader([])
        service = AssetProviderService(
            self.project, downloader=downloader,
            resolver=lambda host, port: ["93.184.216.34"], clock=self.clock,
        )
        service.grant_consent("heroicons", "nat")
        with self.assertRaises(AssetProviderError) as rejected:
            service.search("heroicons", "arrow-right")
        self.assertEqual(rejected.exception.status_code, 503)
        self.assertEqual(rejected.exception.code, "svg_rasterizer_unavailable")
        self.assertEqual(downloader.calls, [])

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

    def _svg_service(
        self, payloads: list[bytes], *, pipeline: FakeSvgPipeline | None = None
    ) -> tuple[AssetProviderService, FakeDownloader, FakeSvgPipeline]:
        downloader = FakeDownloader(payloads)
        selected = pipeline or FakeSvgPipeline()
        service = AssetProviderService(
            self.project,
            downloader=downloader,
            resolver=lambda host, port: ["93.184.216.34"],
            clock=self.clock,
            svg_pipeline=selected,
        )
        return service, downloader, selected

    def _import_repo_svg(
        self, service: AssetProviderService, provider_id: str, slug: str
    ) -> dict[str, Any]:
        service.grant_consent(provider_id, "nat")
        token = service.search(provider_id, slug)["items"][0]["import_token"]
        return service.import_candidate(token, lambda _path: True)

    def test_svg_preflight_is_cached_once_and_status_is_pure_read(self) -> None:
        pipeline = FakeSvgPipeline()
        service, downloader, _pipeline = self._svg_service([], pipeline=pipeline)
        self.assertEqual(pipeline.preflight_calls, 1)
        before = sorted(path.relative_to(self.project) for path in self.project.rglob("*"))
        for _ in range(3):
            status = service.status()
            self.assertTrue(
                all(item["available"] for item in status["providers"] if item["kind"] == "svg")
            )
        self.assertEqual(pipeline.preflight_calls, 1)
        self.assertEqual(downloader.calls, [])
        self.assertEqual(
            sorted(path.relative_to(self.project) for path in self.project.rglob("*")),
            before,
        )

    def test_svg_preflight_fails_closed_on_untrusted_success_shapes(self) -> None:
        identity = {
            "version": "resvg-test-1",
            "executable_sha256": "a" * 64,
            "sandbox_executable_sha256": "b" * 64,
            "sandbox_profile_sha256": "c" * 64,
        }
        malformed = {
            "checks_false": SimpleNamespace(
                available=True, checks_ok=False, code="OK", identity=dict(identity)
            ),
            "arbitrary_code": SimpleNamespace(
                available=True, checks_ok=True, code="arbitrary", identity=dict(identity)
            ),
            "empty_identity": SimpleNamespace(
                available=True, checks_ok=True, code="OK", identity={}
            ),
            "extra_identity": SimpleNamespace(
                available=True,
                checks_ok=True,
                code="OK",
                identity={**identity, "unexpected": "value"},
            ),
            "uppercase_hash": SimpleNamespace(
                available=True,
                checks_ok=True,
                code="OK",
                identity={**identity, "executable_sha256": "A" * 64},
            ),
            "control_version": SimpleNamespace(
                available=True,
                checks_ok=True,
                code="OK",
                identity={**identity, "version": "resvg\nforged"},
            ),
            "boolish_available": SimpleNamespace(
                available=1, checks_ok=True, code="OK", identity=dict(identity)
            ),
            "missing_field": SimpleNamespace(
                available=True, checks_ok=True, identity=dict(identity)
            ),
            "extra_field": SimpleNamespace(
                available=True,
                checks_ok=True,
                code="OK",
                identity=dict(identity),
                unexpected=True,
            ),
            "mapping": {
                "available": True,
                "checks_ok": True,
                "code": "OK",
                "identity": dict(identity),
            },
        }

        for label, preflight in malformed.items():
            with self.subTest(label=label):
                pipeline = FakeSvgPipeline()
                pipeline.preflight = lambda value=preflight: value  # type: ignore[method-assign]
                service, downloader, _pipeline = self._svg_service([], pipeline=pipeline)
                for _ in range(2):
                    svg_status = [
                        item for item in service.status()["providers"] if item["kind"] == "svg"
                    ]
                    self.assertTrue(svg_status)
                    self.assertTrue(all(item["available"] is False for item in svg_status))
                    self.assertTrue(
                        all(
                            item["availability_code"] == "svg_rasterizer_unavailable"
                            for item in svg_status
                        )
                    )
                service.grant_consent("heroicons", "nat")
                with self.assertRaises(AssetProviderError) as rejected:
                    service.search("heroicons", "arrow-right")
                self.assertEqual(rejected.exception.status_code, 503)
                self.assertEqual(rejected.exception.code, "svg_rasterizer_unavailable")
                self.assertEqual(downloader.calls, [])

    def test_unpinned_available_preflight_cannot_reach_svg_import(self) -> None:
        unpinned = FakeSvgPipeline()

        def unpinned_preflight() -> SimpleNamespace:
            unpinned.preflight_calls += 1
            return SimpleNamespace(
                available=True,
                checks_ok=False,
                code="arbitrary",
                identity={},
            )

        unpinned.preflight = unpinned_preflight  # type: ignore[method-assign]
        blocked, downloader, _pipeline = self._svg_service([], pipeline=unpinned)
        self.assertEqual(unpinned.preflight_calls, 1)
        for _ in range(2):
            svg_status = [
                item for item in blocked.status()["providers"] if item["kind"] == "svg"
            ]
            self.assertTrue(all(item["available"] is False for item in svg_status))
        self.assertEqual(unpinned.preflight_calls, 1)

        issuer, _issuer_downloader, _issuer_pipeline = self._svg_service([])
        issuer.grant_consent("heroicons", "nat")
        token = issuer.search("heroicons", "arrow-right")["items"][0]["import_token"]
        blocked._tokens[token] = issuer._tokens.pop(token)
        with self.assertRaises(AssetProviderError) as rejected:
            blocked.import_candidate(token, lambda _path: True)
        self.assertEqual(rejected.exception.status_code, 503)
        self.assertEqual(rejected.exception.code, "svg_rasterizer_unavailable")
        self.assertEqual(downloader.calls, [])
        self.assertEqual(unpinned.rasterize_calls, 0)
        self.assertEqual(unpinned.preflight_calls, 1)

    def _assert_no_svg_publication(self, project: Path | None = None) -> None:
        root = project or self.project
        for relative in (
            "working/source_artifacts/svg",
            "working/sanitized_svg",
            "assets/generated/svg",
            asset_registry.PROVIDER_RECEIPTS_REL,
        ):
            directory = root / relative
            if directory.exists():
                self.assertEqual(
                    [path for path in directory.rglob("*") if path.is_file() or path.is_symlink()],
                    [],
                )
        self.assertFalse((root / asset_registry.PROVENANCE_REL).exists())
        self.assertFalse((root / asset_registry.ATTRIBUTION_REL).exists())
        self.assertEqual(list(root.rglob("*.part")), [])

    def test_raster_metadata_cannot_override_cached_preflight_identity(self) -> None:
        pipeline = FakeSvgPipeline()
        service, downloader, _pipeline = self._svg_service([BENIGN_SVG], pipeline=pipeline)
        pipeline.identity["executable_sha256"] = "d" * 64
        service.grant_consent("heroicons", "nat")
        token = service.search("heroicons", "arrow-right")["items"][0]["import_token"]

        with self.assertRaises(AssetProviderError) as rejected:
            service.import_candidate(token, lambda _path: True)
        self.assertEqual(rejected.exception.status_code, 503)
        self.assertEqual(rejected.exception.code, "svg_rasterizer_unavailable")
        self.assertEqual(len(downloader.calls), 1)
        self.assertEqual(pipeline.rasterize_calls, 1)
        self._assert_no_svg_publication()
        with self.assertRaises(AssetProviderError) as replayed:
            service.import_candidate(token, lambda _path: True)
        self.assertEqual(replayed.exception.status_code, 404)
        self.assertEqual(replayed.exception.code, "import_token_not_found")

    def test_raster_metadata_requires_exact_plain_cached_identity(self) -> None:
        identity = {
            "version": "resvg-test-1",
            "executable_sha256": "a" * 64,
            "sandbox_executable_sha256": "b" * 64,
            "sandbox_profile_sha256": "c" * 64,
        }

        class IdentityMapping(dict[str, str]):
            pass

        malformed = {
            "missing": {key: value for key, value in identity.items() if key != "version"},
            "extra": {**identity, "unexpected": "value"},
            "mapping_subclass": IdentityMapping(identity),
            "changed_version": {**identity, "version": "resvg-test-2"},
            "changed_hash": {**identity, "executable_sha256": "d" * 64},
            "uppercase_hash": {**identity, "executable_sha256": "A" * 64},
        }
        for label, metadata in malformed.items():
            with self.subTest(label=label):
                pipeline = FakeSvgPipeline(raster_metadata=metadata)
                service, downloader, _pipeline = self._svg_service(
                    [BENIGN_SVG], pipeline=pipeline
                )
                service.grant_consent("heroicons", "nat")
                token = service.search("heroicons", "arrow-right")["items"][0][
                    "import_token"
                ]
                with self.assertRaises(AssetProviderError) as rejected:
                    service.import_candidate(token, lambda _path: True)
                self.assertEqual(rejected.exception.status_code, 503)
                self.assertEqual(rejected.exception.code, "svg_rasterizer_unavailable")
                self.assertEqual(len(downloader.calls), 1)
                self._assert_no_svg_publication()

    def test_malformed_raster_result_shapes_fail_closed_without_publication(self) -> None:
        class ThrowingProperties:
            @property
            def png_bytes(self) -> bytes:
                raise RuntimeError("hostile getter")

        factories = {
            "none": lambda _valid: None,
            "mapping": lambda valid: dict(valid),
            "missing_metadata": lambda valid: SimpleNamespace(
                **{key: value for key, value in valid.items() if key != "metadata"}
            ),
            "extra_field": lambda valid: SimpleNamespace(**valid, unexpected=True),
            "throwing_property": lambda _valid: ThrowingProperties(),
            "png_bytes_type": lambda valid: SimpleNamespace(**{**valid, "png_bytes": "PNG"}),
            "hash_type": lambda valid: SimpleNamespace(**{**valid, "png_sha256": 7}),
            "width_type": lambda valid: SimpleNamespace(**{**valid, "width": True}),
            "height_type": lambda valid: SimpleNamespace(**{**valid, "height": "24"}),
            "metadata_type": lambda valid: SimpleNamespace(**{**valid, "metadata": []}),
        }
        for label, factory in factories.items():
            with self.subTest(label=label):
                project = self.project / label
                project.mkdir()
                pipeline = FakeSvgPipeline()

                def malformed_rasterize(
                    sanitized: Any,
                    *,
                    selected: Any = factory,
                    selected_pipeline: FakeSvgPipeline = pipeline,
                ) -> Any:
                    selected_pipeline.rasterize_calls += 1
                    width = sanitized.metadata["requested_width"]
                    height = sanitized.metadata["requested_height"]
                    payload = strict_png(width, height)
                    valid = {
                        "png_bytes": payload,
                        "png_sha256": hashlib.sha256(payload).hexdigest(),
                        "width": width,
                        "height": height,
                        "metadata": dict(selected_pipeline.identity),
                    }
                    return selected(valid)

                pipeline.rasterize = malformed_rasterize  # type: ignore[method-assign]
                downloader = FakeDownloader([BENIGN_SVG])
                service = AssetProviderService(
                    project,
                    downloader=downloader,
                    resolver=lambda host, port: ["93.184.216.34"],
                    clock=self.clock,
                    svg_pipeline=pipeline,
                )
                service.grant_consent("heroicons", "nat")
                token = service.search("heroicons", "arrow-right")["items"][0][
                    "import_token"
                ]
                with self.assertRaises(AssetProviderError) as rejected:
                    service.import_candidate(token, lambda _path: True)
                self.assertEqual(rejected.exception.status_code, 503)
                self.assertEqual(rejected.exception.code, "svg_rasterizer_unavailable")
                self.assertEqual(len(downloader.calls), 1)
                self._assert_no_svg_publication(project)
                with self.assertRaises(AssetProviderError) as replayed:
                    service.import_candidate(token, lambda _path: True)
                self.assertEqual(replayed.exception.status_code, 404)

    def test_repo_svg_import_publishes_png_receipt_provenance_and_is_idempotent(self) -> None:
        service, downloader, pipeline = self._svg_service([BENIGN_SVG])
        imported = self._import_repo_svg(service, "heroicons", "arrow-right")
        self.assertFalse(imported["idempotent"])
        self.assertEqual(len(downloader.calls), 1, "exact repo search must not download")
        self.assertEqual(pipeline.rasterize_calls, 1)
        self.assertTrue(imported["source"].startswith("assets/generated/svg/"))
        self.assertTrue(imported["source"].endswith(".png"))
        self.assertNotIn(".svg", imported["source"])
        item = imported["item"]
        self.assertEqual(asset_registry.provider_consistency_errors(self.project, item), [])
        receipt_path = (
            self.project
            / asset_registry.PROVIDER_RECEIPTS_REL
            / (hashlib.sha256(item["asset_id"].encode()).hexdigest() + ".json")
        )
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["schema_version"], 2)
        self.assertEqual(receipt["decision"], "approved")
        self.assertRegex(receipt["evidence_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(receipt["issued_at"], item["license"]["verified_at"])
        self.assertEqual(receipt["png"]["mime"], "image/png")
        self.assertTrue((self.project / receipt["raw"]["path"]).is_file())
        self.assertTrue((self.project / receipt["sanitized"]["path"]).is_file())
        self.assertTrue((self.project / receipt["png"]["path"]).is_file())
        self.assertIn("Tailwind Labs", (self.project / "ATTRIBUTION.md").read_text("utf-8"))
        self.assertEqual(list(self.project.rglob("*.part")), [])

        again = self._import_repo_svg(service, "heroicons", "arrow-right")
        self.assertTrue(again["idempotent"])
        self.assertEqual(len(downloader.calls), 1, "current v2 receipt must avoid re-download")
        self.assertEqual(pipeline.rasterize_calls, 1)

    def test_svg_registry_failure_rolls_back_exact_bytes_and_only_new_files(self) -> None:
        baseline = {
            "asset_id": "baseline",
            "path": "assets/baseline.png",
            "sha256": "d" * 64,
            "origin": "folder-import",
            "provider_id": None,
            "source_url": None,
            "license": {
                "spdx": "CC0-1.0",
                "evidence_url": None,
                "attribution_required": False,
                "attribution_text": "",
                "verified_at": "2026-08-04T03:00:00Z",
            },
            "review_status": "approved",
        }
        asset_registry.upsert_item(self.project, baseline)
        registry_path = self.project / asset_registry.PROVENANCE_REL
        attribution_path = self.project / asset_registry.ATTRIBUTION_REL
        registry_before = registry_path.read_bytes()
        attribution_before = attribution_path.read_bytes()
        service, _downloader, _pipeline = self._svg_service([BENIGN_SVG])
        service.grant_consent("heroicons", "nat")
        token = service.search("heroicons", "arrow-right")["items"][0]["import_token"]

        def corrupt_then_fail(_root: Path, _item: dict[str, Any]) -> dict[str, Any]:
            registry_path.write_bytes(b"corrupt-registry")
            attribution_path.write_bytes(b"corrupt-attribution")
            raise asset_registry.AssetRegistryError("simulated registry failure")

        with patch.object(asset_registry, "upsert_item", side_effect=corrupt_then_fail):
            with self.assertRaises(AssetProviderError) as rejected:
                service.import_candidate(token, lambda _path: True)
        self.assertEqual(rejected.exception.status_code, 409)
        self.assertEqual(rejected.exception.code, "registry_conflict")
        self.assertEqual(registry_path.read_bytes(), registry_before)
        self.assertEqual(attribution_path.read_bytes(), attribution_before)
        self.assertEqual(list((self.project / "assets/generated/svg").glob("*.png")), [])
        self.assertEqual(list((self.project / "working/provider-receipts").glob("*.json")), [])
        self.assertEqual(list((self.project / "working/source_artifacts/svg").glob("*.untrusted")), [])
        self.assertEqual(list((self.project / "working/sanitized_svg").glob("*.svg")), [])
        self.assertEqual(list(self.project.rglob("*.part")), [])

    def test_different_candidate_same_png_is_conflict_without_merging_attribution(self) -> None:
        service, downloader, _pipeline = self._svg_service([BENIGN_SVG, BENIGN_SVG])
        first = self._import_repo_svg(service, "heroicons", "arrow-right")
        before_registry = (self.project / asset_registry.PROVENANCE_REL).read_bytes()
        before_attribution = (self.project / asset_registry.ATTRIBUTION_REL).read_bytes()
        with self.assertRaises(AssetProviderError) as rejected:
            self._import_repo_svg(service, "tabler", "arrow-left")
        self.assertEqual(rejected.exception.status_code, 409)
        self.assertEqual(rejected.exception.code, "registry_conflict")
        self.assertEqual((self.project / asset_registry.PROVENANCE_REL).read_bytes(), before_registry)
        self.assertEqual((self.project / asset_registry.ATTRIBUTION_REL).read_bytes(), before_attribution)
        self.assertEqual(len(list((self.project / "working/provider-receipts").glob("*.json"))), 1)
        self.assertEqual(len(json.loads(before_registry)["items"]), 1)
        self.assertEqual(len(downloader.calls), 2)
        self.assertEqual((self.project / first["source"]).read_bytes(), strict_png(24, 24))

    def test_svg_content_collision_preserves_preexisting_and_cleans_new_files(self) -> None:
        pipeline = FakeSvgPipeline()
        payload = strict_png(24, 24)
        digest = hashlib.sha256(payload).hexdigest()
        collision = self.project / f"assets/generated/svg/{digest}.png"
        collision.parent.mkdir(parents=True)
        collision.write_bytes(b"preexisting-conflict")
        service, _downloader, _pipeline = self._svg_service([BENIGN_SVG], pipeline=pipeline)
        with self.assertRaises(AssetProviderError) as rejected:
            self._import_repo_svg(service, "heroicons", "arrow-right")
        self.assertEqual(rejected.exception.status_code, 409)
        self.assertEqual(collision.read_bytes(), b"preexisting-conflict")
        self.assertEqual(list((self.project / "working/source_artifacts/svg").glob("*.untrusted")), [])
        self.assertEqual(list((self.project / "working/sanitized_svg").glob("*.svg")), [])
        self.assertFalse((self.project / asset_registry.PROVENANCE_REL).exists())
        self.assertEqual(list(self.project.rglob("*.part")), [])

    def _write_font_state(self, text: str, *, metadata_title: str = "") -> None:
        path = self.project / "working/editor_state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "title": metadata_title,
                    "overlays": [
                        {
                            "id": "caption-1",
                            "type": "caption",
                            "text": text,
                            "start": 0.0,
                            "end": 1.0,
                            "style": {},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_font_catalog_search_and_google_import_are_private_bound_and_idempotent(self) -> None:
        self._write_font_state("A中", metadata_title="龍 is UI metadata only")
        font = build_ttf()
        listing = google_font_payload(root="apache", license_raw=APACHE_LICENSE)
        downloader = FakeDownloader([listing, APACHE_LICENSE, font, listing])
        service = AssetProviderService(
            self.project,
            downloader=downloader,
            resolver=lambda host, port: ["93.184.216.34"],
            clock=self.clock,
        )
        service.grant_consent("google-fonts", "nat")
        result = service.search("google-fonts", "apache/phasetwotest")
        public = result["items"][0]
        self.assertNotIn("download_url", public)
        self.assertNotIn("license_download_url", public)
        self.assertNotIn("raw.githubusercontent.com", json.dumps(public))
        search_url, _path, search_policy = downloader.calls[0]
        self.assertEqual(
            search_url,
            "https://api.github.com/repos/google/fonts/contents/apache/phasetwotest"
            "?ref=2796410152d4f9524b68ed46e69c1b60f8e0f7c3",
        )
        self.assertEqual(search_policy["allowed_hosts"], frozenset({"api.github.com"}))

        imported = service.import_candidate(public["import_token"])
        self.assertFalse(imported["idempotent"])
        item = imported["item"]
        self.assertRegex(
            item["asset_id"],
            r"^font-google-fonts-[0-9a-f]{16}-[0-9a-f]{16}$",
        )
        self.assertTrue(item["path"].startswith("assets/fonts/"))
        receipt_path = (
            self.project
            / asset_registry.PROVIDER_RECEIPTS_REL
            / f"{hashlib.sha256(item['asset_id'].encode()).hexdigest()}.json"
        )
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertNotIn("query", receipt)
        self.assertEqual(
            receipt["query_hash"],
            hashlib.sha256(b"apache/phasetwotest").hexdigest(),
        )
        self.assertEqual(asset_registry.provider_consistency_errors(self.project, item), [])
        resolved = asset_registry.resolve_project_font(self.project, item["asset_id"])
        self.assertEqual(resolved["family"], "Phase Two Test")
        self.assertEqual(resolved["license_spdx"], "Apache-2.0")
        self.assertIn("required_glyphs", resolved)
        self.assertIn("validation_receipt", resolved)
        self.assertNotIn("validation", resolved)
        self.assertEqual(len(asset_registry.list_project_fonts(self.project)), 1)
        self.assertEqual(downloader.calls[1][2]["max_bytes"], 512 * 1024)
        self.assertEqual(downloader.calls[2][2]["max_bytes"], 32 * 1024 * 1024)
        self.assertEqual(downloader.calls[1][2]["allowed_hosts"], frozenset({"raw.githubusercontent.com"}))

        second = service.search("google-fonts", "apache/phasetwotest")["items"][0]
        again = service.import_candidate(second["import_token"])
        self.assertTrue(again["idempotent"])
        self.assertEqual(len(downloader.calls), 4, "idempotent import only repeats metadata search")

    def test_fontsource_exact_semver_import_uses_versioned_private_license(self) -> None:
        self._write_font_state("A")
        font = build_ttf()
        downloader = FakeDownloader(
            [fontsource_font_payload(license_spdx="Apache-2.0"), APACHE_LICENSE, font]
        )
        service = AssetProviderService(
            self.project,
            downloader=downloader,
            resolver=lambda host, port: ["93.184.216.34"],
            clock=self.clock,
        )
        service.grant_consent("fontsource", "nat")
        result = service.search("fontsource", "phase-two-test@5.1.0")
        public = result["items"][0]
        self.assertNotIn("license_download_url", public)
        imported = service.import_candidate(public["import_token"])
        self.assertEqual(imported["item"]["provider_id"], "fontsource")
        self.assertEqual(
            downloader.calls[1][0],
            "https://cdn.jsdelivr.net/npm/@fontsource/phase-two-test@5.1.0/LICENSE",
        )
        self.assertEqual(asset_registry.provider_consistency_errors(self.project, imported["item"]), [])
        resolved = asset_registry.resolve_project_font(
            self.project, imported["item"]["asset_id"]
        )
        self.assertEqual(resolved["license_spdx"], "Apache-2.0")

    def test_font_search_query_grammars_are_exact_and_reject_before_network(self) -> None:
        downloader = FakeDownloader([])
        service = AssetProviderService(
            self.project,
            downloader=downloader,
            resolver=lambda host, port: ["93.184.216.34"],
            clock=self.clock,
        )
        for provider_id in ("google-fonts", "fontsource"):
            service.grant_consent(provider_id, "nat")
        cases = (
            ("google-fonts", "OFL/roboto"),
            ("google-fonts", "ofl/roboto/evil"),
            ("google-fonts", "main/roboto"),
            ("fontsource", "roboto@latest"),
            ("fontsource", "roboto@5.1"),
            ("fontsource", "Roboto@5.1.0"),
        )
        for provider_id, query in cases:
            with self.subTest(provider_id=provider_id, query=query):
                with self.assertRaises(AssetProviderError) as rejected:
                    service.search(provider_id, query)
                self.assertEqual(rejected.exception.status_code, 422)
        self.assertEqual(downloader.calls, [])

    def test_font_import_rejects_license_html_spdx_mismatch_bad_magic_and_missing_glyph(self) -> None:
        apache = b"Apache License\nVersion 2.0, January 2004\nhttp://www.apache.org/licenses/\n"
        cases = {
            "html_license": (b"<!doctype html><html>login</html>", build_ttf(), "A"),
            "wrong_license": (apache, build_ttf(), "A"),
            "bad_magic": (OFL_LICENSE, b"not-a-font", "A"),
            "missing_glyph": (OFL_LICENSE, build_ttf(), "龍"),
        }
        for label, (license_raw, font_raw, text) in cases.items():
            with self.subTest(label=label):
                project = self.project / label
                project.mkdir()
                state = project / "working/editor_state.json"
                state.parent.mkdir()
                state.write_text(
                    json.dumps(
                        {
                            "schema_version": 2,
                            "overlays": [
                                {
                                    "type": "caption",
                                    "text": text,
                                    "start": 0.0,
                                    "end": 1.0,
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                downloader = FakeDownloader([google_font_payload(), license_raw, font_raw])
                service = AssetProviderService(
                    project,
                    downloader=downloader,
                    resolver=lambda host, port: ["93.184.216.34"],
                    clock=self.clock,
                )
                service.grant_consent("google-fonts", "nat")
                token = service.search("google-fonts", "ofl/phasetwotest")["items"][0]["import_token"]
                with self.assertRaises(AssetProviderError) as rejected:
                    service.import_candidate(token)
                self.assertEqual(rejected.exception.status_code, 422)
                self.assertFalse((project / asset_registry.PROVENANCE_REL).exists())
                self.assertEqual(list(project.rglob("*.part")), [])

    def test_custom_font_validator_without_exact_capability_cannot_enable_provider(self) -> None:
        from dataclasses import replace

        from font_security import (
            FONT_VALIDATOR_VERSION,
            LIMITS_SHA256,
            POLICY_VERSION,
            validate_font_bytes,
        )

        valid = validate_font_bytes(
            build_ttf(),
            "A",
            license_spdx="OFL-1.1",
            declared_mime="font/ttf",
        )

        def forged_validator(raw: bytes, *_args: Any, **_kwargs: Any) -> Any:
            digest = hashlib.sha256(raw).hexdigest()
            return replace(
                valid,
                byte_length=len(raw),
                sha256=digest,
                receipt={
                    **valid.receipt,
                    "byte_length": len(raw),
                    "font_sha256": digest,
                },
            )

        service = AssetProviderService(
            self.project,
            downloader=FakeDownloader([]),
            resolver=lambda host, port: ["93.184.216.34"],
            font_validator=forged_validator,
            font_capability_probe=lambda: SimpleNamespace(
                available=True,
                checks_ok=True,
                code="OK",
                identity={
                    "fonttools_version": "4.62.1",
                    "validator_version": FONT_VALIDATOR_VERSION,
                    "policy_version": POLICY_VERSION,
                    "limits_sha256": LIMITS_SHA256,
                },
            ),
            clock=self.clock,
        )
        status = [item for item in service.status()["providers"] if item["kind"] == "font"]
        self.assertTrue(all(item["available"] is False for item in status))
        service.grant_consent("google-fonts", "nat")
        with self.assertRaises(AssetProviderError) as rejected:
            service.search("google-fonts", "ofl/phasetwotest")
        self.assertEqual(rejected.exception.code, "font_validator_unavailable")
        self.assertFalse((self.project / asset_registry.PROVENANCE_REL).exists())
        self.assertEqual(list(self.project.rglob("*.part")), [])

    def test_malformed_font_capability_and_result_shapes_fail_closed(self) -> None:
        from font_security import FONT_VALIDATOR_VERSION, LIMITS_SHA256, POLICY_VERSION

        identity = {
            "fonttools_version": "4.62.1",
            "validator_version": FONT_VALIDATOR_VERSION,
            "policy_version": POLICY_VERSION,
            "limits_sha256": LIMITS_SHA256,
        }
        malformed = (
            {"available": True, "checks_ok": True, "code": "OK", "identity": identity},
            SimpleNamespace(available=True, checks_ok=False, code="OK", identity=identity),
            SimpleNamespace(available=True, checks_ok=True, code="OK", identity={}),
            SimpleNamespace(
                available=True,
                checks_ok=True,
                code="OK",
                identity={**identity, "fonttools_version": "latest"},
            ),
        )
        for index, capability in enumerate(malformed):
            with self.subTest(index=index):
                service = AssetProviderService(
                    self.project,
                    downloader=FakeDownloader([]),
                    resolver=lambda host, port: ["93.184.216.34"],
                    font_validator=lambda *_args, **_kwargs: True,
                    font_capability_probe=lambda value=capability: value,
                    clock=self.clock,
                )
                font_status = [
                    item for item in service.status()["providers"] if item["kind"] == "font"
                ]
                self.assertTrue(all(item["available"] is False for item in font_status))

        self._write_font_state("A")
        service = AssetProviderService(
            self.project,
            downloader=FakeDownloader([google_font_payload(), OFL_LICENSE, build_ttf()]),
            resolver=lambda host, port: ["93.184.216.34"],
            font_validator=lambda *_args, **_kwargs: True,
            font_capability_probe=lambda: SimpleNamespace(
                available=True, checks_ok=True, code="OK", identity=dict(identity)
            ),
            clock=self.clock,
        )
        service.grant_consent("google-fonts", "nat")
        with self.assertRaises(AssetProviderError) as rejected:
            service.search("google-fonts", "ofl/phasetwotest")
        self.assertEqual(rejected.exception.status_code, 503)
        self.assertEqual(rejected.exception.code, "font_validator_unavailable")
        self.assertFalse((self.project / asset_registry.PROVENANCE_REL).exists())

    def test_font_transaction_rollback_restores_publication_and_preserves_existing_hash(self) -> None:
        self._write_font_state("A")
        baseline = {
            "asset_id": "baseline",
            "path": "assets/baseline.png",
            "sha256": "d" * 64,
            "origin": "folder-import",
            "provider_id": None,
            "source_url": None,
            "license": {
                "spdx": "CC0-1.0",
                "evidence_url": None,
                "attribution_required": False,
                "attribution_text": "",
                "verified_at": "2026-08-04T03:00:00Z",
            },
            "review_status": "approved",
        }
        asset_registry.upsert_item(self.project, baseline)
        registry_path = self.project / asset_registry.PROVENANCE_REL
        attribution_path = self.project / asset_registry.ATTRIBUTION_REL
        registry_before = registry_path.read_bytes()
        attribution_before = attribution_path.read_bytes()
        license_hash = hashlib.sha256(OFL_LICENSE).hexdigest()
        existing_license = self.project / f"licenses/{license_hash}.txt"
        existing_license.parent.mkdir()
        existing_license.write_bytes(OFL_LICENSE)
        service = AssetProviderService(
            self.project,
            downloader=FakeDownloader([google_font_payload(), OFL_LICENSE, build_ttf()]),
            resolver=lambda host, port: ["93.184.216.34"],
            clock=self.clock,
        )
        service.grant_consent("google-fonts", "nat")
        token = service.search("google-fonts", "ofl/phasetwotest")["items"][0]["import_token"]

        def corrupt_then_fail(_root: Path, _item: dict[str, Any]) -> dict[str, Any]:
            registry_path.write_bytes(b"corrupt")
            attribution_path.write_bytes(b"corrupt")
            raise asset_registry.AssetRegistryError("simulated publication failure")

        with patch.object(asset_registry, "upsert_item", side_effect=corrupt_then_fail):
            with self.assertRaises(AssetProviderError) as rejected:
                service.import_candidate(token)
        self.assertEqual(rejected.exception.status_code, 409)
        self.assertEqual(registry_path.read_bytes(), registry_before)
        self.assertEqual(attribution_path.read_bytes(), attribution_before)
        self.assertEqual(existing_license.read_bytes(), OFL_LICENSE)
        self.assertEqual(list((self.project / "assets/fonts").glob("*")), [])
        self.assertEqual(list((self.project / asset_registry.PROVIDER_RECEIPTS_REL).glob("*.json")), [])
        self.assertEqual(list(self.project.rglob("*.part")), [])

    def test_font_receipt_collision_preserves_preexisting_evidence(self) -> None:
        self._write_font_state("A")
        font = build_ttf()
        blob_sha = hashlib.sha1(
            f"blob {len(font)}\0".encode("ascii") + font,
            usedforsecurity=False,
        ).hexdigest()
        candidate_id = f"{blob_sha}:PhaseTwo.ttf"
        asset_id = (
            "font-google-fonts-"
            f"{hashlib.sha256(candidate_id.encode()).hexdigest()[:16]}-"
            f"{hashlib.sha256(font).hexdigest()[:16]}"
        )
        receipt_path = (
            self.project
            / asset_registry.PROVIDER_RECEIPTS_REL
            / f"{hashlib.sha256(asset_id.encode()).hexdigest()}.json"
        )
        receipt_path.parent.mkdir(parents=True)
        sentinel = b'{"sentinel":"preexisting"}\n'
        receipt_path.write_bytes(sentinel)
        service = AssetProviderService(
            self.project,
            downloader=FakeDownloader([google_font_payload(), OFL_LICENSE, font]),
            resolver=lambda host, port: ["93.184.216.34"],
            clock=self.clock,
        )
        service.grant_consent("google-fonts", "nat")
        token = service.search("google-fonts", "ofl/phasetwotest")["items"][0]["import_token"]

        with self.assertRaises(AssetProviderError) as rejected:
            service.import_candidate(token)

        self.assertEqual(rejected.exception.status_code, 409)
        self.assertEqual(receipt_path.read_bytes(), sentinel)
        self.assertFalse((self.project / asset_registry.PROVENANCE_REL).exists())

    def test_different_font_candidate_same_binary_is_409_without_provenance_merge(self) -> None:
        self._write_font_state("A")
        font = build_ttf()
        downloader = FakeDownloader(
            [
                google_font_payload(), OFL_LICENSE, font,
                google_font_payload(name="PhaseTwoAlt.ttf"), OFL_LICENSE, font,
            ]
        )
        service = AssetProviderService(
            self.project,
            downloader=downloader,
            resolver=lambda host, port: ["93.184.216.34"],
            clock=self.clock,
        )
        service.grant_consent("google-fonts", "nat")
        first = service.search("google-fonts", "ofl/phasetwotest")["items"][0]
        imported = service.import_candidate(first["import_token"])
        before_registry = (self.project / asset_registry.PROVENANCE_REL).read_bytes()
        before_attribution = (self.project / asset_registry.ATTRIBUTION_REL).read_bytes()
        second = service.search("google-fonts", "ofl/phasetwotest")["items"][0]
        with self.assertRaises(AssetProviderError) as rejected:
            service.import_candidate(second["import_token"])
        self.assertEqual(rejected.exception.status_code, 409)
        self.assertEqual((self.project / asset_registry.PROVENANCE_REL).read_bytes(), before_registry)
        self.assertEqual((self.project / asset_registry.ATTRIBUTION_REL).read_bytes(), before_attribution)
        self.assertEqual(len(list((self.project / asset_registry.PROVIDER_RECEIPTS_REL).glob("*.json"))), 1)
        self.assertTrue((self.project / imported["source"]).is_file())


if __name__ == "__main__":
    unittest.main()
