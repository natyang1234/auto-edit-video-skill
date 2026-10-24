"""github-fonts: allowlisted, commit-pinned author repositories as a caption font source."""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import asset_registry  # noqa: E402
import contract_registry  # noqa: E402
import github_fonts  # noqa: E402
import open_font_providers as ofp  # noqa: E402
from asset_provider_service import AssetProviderError, AssetProviderService  # noqa: E402
from font_test_fixture import build_ttf  # noqa: E402
from test_asset_provider_service import FakeClock, FakeDownloader, OFL_LICENSE  # noqa: E402
from test_font_security import build_otf  # noqa: E402

FONT = build_ttf()
OTF = build_otf()
REL_FONT = build_ttf(family_name="Release Test")  # distinct bytes: allowlist blobs must be unique
# a licence with an author-specific header that the generic whitelist rejects
LICENSE_WITH_LABELS = b"[Kosugi Maru]\nCopyright (c) 2010 MOTOYA CO.,LTD.\n\n[Test Font]\n" + OFL_LICENSE


def fake_allowlist() -> dict[str, dict]:
    def entry(font_id, raw, container, lic):
        name = f"Test-{font_id}.{container}"
        return {
            "id": font_id, "family": f"Fam {font_id}", "label": font_id, "owner": "author", "repo": "repo",
            "commit": "a" * 40, "font_path": f"fonts/{name}", "license_path": "OFL.txt",
            "font_blob_sha1": github_fonts.git_blob_sha1(raw), "license_blob_sha1": github_fonts.git_blob_sha1(lic),
            "license_spdx": "OFL-1.1", "container": container,
        }
    return {
        "ttf-font": entry("ttf-font", FONT, "ttf", LICENSE_WITH_LABELS),
        "otf-font": entry("otf-font", OTF, "otf", OFL_LICENSE),
    }


def contents_payload(item: dict, *, sha: str | None = None, path: str | None = None) -> dict:
    return {"type": "file", "path": path or item["font_path"], "sha": sha or item["font_blob_sha1"], "size": 1234}


def wire(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


class GithubFontsProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.allow = fake_allowlist()
        self._patch = mock.patch.object(github_fonts, "_load", return_value=self.allow)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "project"
        self.project.mkdir()
        (self.project / "working").mkdir()
        (self.project / "working" / "editor_state.json").write_text(json.dumps({
            "schema_version": 1, "overlays": [{"id": "c1", "type": "caption", "text": "A", "start": 0.0, "end": 1.0, "style": {}}],
        }), encoding="utf-8")
        self.clock = FakeClock()

    def service(self, payloads):
        return AssetProviderService(
            self.project,
            downloader=FakeDownloader(payloads),
            resolver=lambda host, port: ["93.184.216.34"],
            clock=self.clock,
        )

    # --- parser -------------------------------------------------------------------------------
    def test_parser_accepts_only_the_pinned_file(self) -> None:
        item = self.allow["ttf-font"]
        [cand] = ofp.parse_github_fonts_tree(contents_payload(item), "ttf-font")
        self.assertEqual(cand.provider_id, "github-fonts")
        self.assertEqual(cand.candidate_id, f"{item['font_blob_sha1']}:Test-ttf-font.ttf")
        self.assertEqual(cand.mime_type, "font/ttf")
        self.assertEqual(cand.download_url, "https://raw.githubusercontent.com/author/repo/" + "a" * 40 + "/fonts/Test-ttf-font.ttf")
        [otf] = ofp.parse_github_fonts_tree(contents_payload(self.allow["otf-font"]), "otf-font")
        self.assertEqual(otf.mime_type, "font/otf")
        for bad in (
            contents_payload(item, sha="b" * 40),
            contents_payload(item, path="fonts/Other.ttf"),
            {"type": "dir", "path": item["font_path"], "sha": item["font_blob_sha1"], "size": 1},
            [],
        ):
            with self.assertRaises(ofp.ProviderDataError):
                ofp.parse_github_fonts_tree(bad, "ttf-font")
        with self.assertRaises(ofp.ProviderDataError):
            ofp.parse_github_fonts_tree(contents_payload(item), "not-listed")

    # --- service ------------------------------------------------------------------------------
    def test_query_must_be_an_allowlisted_id_never_a_repo_or_path(self) -> None:
        service = self.service([])
        service.grant_consent("github-fonts", "nat")
        for query in ("author/repo", "author/repo@" + "a" * 40, "fonts/Test-ttf-font.ttf", "../etc", "nope"):
            with self.assertRaises(AssetProviderError) as rejected:
                service.search("github-fonts", query)
            self.assertEqual(rejected.exception.status_code, 422, query)

    def test_search_import_verifies_both_blobs_and_trusts_the_pinned_licence_header(self) -> None:
        item = self.allow["ttf-font"]
        service = self.service([wire(contents_payload(item)), LICENSE_WITH_LABELS, FONT])
        service.grant_consent("github-fonts", "nat")
        token = service.search("github-fonts", "ttf-font")["items"][0]["import_token"]
        result = service.import_candidate(token)
        asset_id = result["item"]["asset_id"]
        self.assertRegex(asset_id, r"font-github-fonts-[0-9a-f]{16}-[0-9a-f]{16}")
        resolved = asset_registry.resolve_project_font(self.project, asset_id)
        self.assertEqual(resolved["sha256"], hashlib.sha256(FONT).hexdigest())
        self.assertEqual(asset_registry.provider_consistency_errors(self.project, result["item"]), [])

    def test_otf_candidate_imports_with_otf_container(self) -> None:
        item = self.allow["otf-font"]
        service = self.service([wire(contents_payload(item)), OFL_LICENSE, OTF])
        service.grant_consent("github-fonts", "nat")
        token = service.search("github-fonts", "otf-font")["items"][0]["import_token"]
        result = service.import_candidate(token)
        self.assertTrue(result["item"]["path"].endswith(".otf"))
        asset_registry.resolve_project_font(self.project, result["item"]["asset_id"])

    def test_font_bytes_not_matching_pinned_blob_are_rejected(self) -> None:
        item = self.allow["ttf-font"]
        service = self.service([wire(contents_payload(item)), LICENSE_WITH_LABELS, OTF])  # wrong bytes
        service.grant_consent("github-fonts", "nat")
        token = service.search("github-fonts", "ttf-font")["items"][0]["import_token"]
        with self.assertRaises(AssetProviderError) as rejected:
            service.import_candidate(token)
        self.assertEqual(rejected.exception.status_code, 422)
        self.assertFalse((self.project / asset_registry.PROVENANCE_REL).exists())

    def test_licence_bytes_not_matching_pinned_blob_are_rejected_even_if_valid_ofl(self) -> None:
        item = self.allow["ttf-font"]
        service = self.service([wire(contents_payload(item)), OFL_LICENSE, FONT])  # plain OFL, not the pinned file
        service.grant_consent("github-fonts", "nat")
        token = service.search("github-fonts", "ttf-font")["items"][0]["import_token"]
        with self.assertRaises(AssetProviderError) as rejected:
            service.import_candidate(token)
        self.assertEqual(rejected.exception.status_code, 422)

    def test_author_header_is_not_trusted_outside_the_allowlist(self) -> None:
        with self.assertRaises(asset_registry.AssetRegistryError):
            asset_registry.validate_font_license_text(LICENSE_WITH_LABELS, "OFL-1.1")
        asset_registry.validate_font_license_text(LICENSE_WITH_LABELS, "OFL-1.1", trusted_header=True)
        with self.assertRaises(asset_registry.AssetRegistryError):  # body must still be the full template
            asset_registry.validate_font_license_text(b"[x]\nnot a licence\n", "OFL-1.1", trusted_header=True)

    def test_licence_evidence_url_must_be_an_allowlisted_raw_url(self) -> None:
        item = self.allow["ttf-font"]
        good = github_fonts.raw_url(item, item["license_path"])
        self.assertTrue(contract_registry._canonical_font_license_evidence("github-fonts", good, "OFL-1.1"))
        self.assertTrue(asset_registry._font_license_evidence_matches("github-fonts", good, "OFL-1.1"))
        for bad in (good.replace("a" * 40, "b" * 40), good.replace("OFL.txt", "LICENSE"), "https://raw.githubusercontent.com/evil/repo/" + "a" * 40 + "/OFL.txt"):
            self.assertFalse(contract_registry._canonical_font_license_evidence("github-fonts", bad, "OFL-1.1"))
            self.assertFalse(asset_registry._font_license_evidence_matches("github-fonts", bad, "OFL-1.1"))

    def test_duplicate_font_blob_in_manifest_disables_provider(self) -> None:
        self._patch.stop()
        try:
            github_fonts._CACHE = None
            dup = {"fonts": {"a-font": dict(fake_allowlist()["ttf-font"]), "b-font": dict(fake_allowlist()["ttf-font"])}}
            for e in dup["fonts"].values():
                e.pop("id", None)
            raw = json.dumps(dup).encode("utf-8")
            with (
                mock.patch.object(github_fonts.MANIFEST_PATH.__class__, "read_bytes", return_value=raw),
                mock.patch.object(github_fonts, "MANIFEST_SHA256", hashlib.sha256(raw).hexdigest()),
            ):
                self.assertFalse(github_fonts.available())
        finally:
            github_fonts._CACHE = None
            self._patch.start()

    # --- release-zip source ---------------------------------------------------------------------
    @staticmethod
    def _zip(members: dict[str, bytes], *, compress=None, encrypted_flag: bool = False) -> bytes:
        import io as _io
        import zipfile

        buf = _io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=compress if compress is not None else zipfile.ZIP_DEFLATED) as z:
            for name, data in members.items():
                info = zipfile.ZipInfo(name)
                info.compress_type = compress if compress is not None else zipfile.ZIP_DEFLATED
                if encrypted_flag:
                    info.flag_bits |= 0x1
                z.writestr(info, data)
        return buf.getvalue()

    def _release_entry(self, zip_bytes: bytes, member: str = "Test-rel.ttf", member_bytes: bytes = REL_FONT) -> dict:
        return {
            "id": "rel-font", "family": "Fam rel", "label": "rel", "owner": "author", "repo": "repo",
            "commit": "a" * 40, "font_path": member, "license_path": "OFL.txt",
            "font_blob_sha1": github_fonts.git_blob_sha1(member_bytes), "license_blob_sha1": github_fonts.git_blob_sha1(OFL_LICENSE),
            "license_spdx": "OFL-1.1", "container": "ttf",
            "source": {"kind": "release-zip", "url": "https://github.com/author/repo/releases/download/v1.0/Test-rel.zip",
                       "zip_size": len(zip_bytes), "zip_sha256": hashlib.sha256(zip_bytes).hexdigest(),
                       "member": member, "member_size": len(member_bytes), "member_sha256": hashlib.sha256(member_bytes).hexdigest()},
        }

    @staticmethod
    def _release_payload(entry: dict, *, size=None, name=None, url=None, tag=None) -> bytes:
        src = entry["source"]
        return json.dumps({"tag_name": tag or "v1.0", "assets": [{
            "name": name or src["url"].rsplit("/", 1)[-1], "size": src["zip_size"] if size is None else size,
            "browser_download_url": url or src["url"]}]}).encode("utf-8")

    def test_release_zip_font_imports_and_resolves_against_member_sha(self) -> None:
        zip_bytes = self._zip({"Test-rel.ttf": REL_FONT})
        entry = self._release_entry(zip_bytes)
        self.allow["rel-font"] = entry
        service = self.service([self._release_payload(entry), OFL_LICENSE, zip_bytes])
        service.grant_consent("github-fonts", "nat")
        [cand] = service.search("github-fonts", "rel-font")["items"]
        self.assertEqual(cand["landing_url"], "https://github.com/author/repo/releases/tag/v1.0")
        result = service.import_candidate(cand["import_token"])
        self.assertEqual(result["item"]["sha256"], hashlib.sha256(REL_FONT).hexdigest())  # member, not zip
        asset_registry.resolve_project_font(self.project, result["item"]["asset_id"])
        # tamper the stored member → resolve fails
        (self.project / result["item"]["path"]).write_bytes(REL_FONT + b"\0")
        with self.assertRaises(asset_registry.AssetRegistryError):
            asset_registry.resolve_project_font(self.project, result["item"]["asset_id"])

    def test_release_payload_must_match_pinned_asset(self) -> None:
        zip_bytes = self._zip({"Test-rel.ttf": REL_FONT})
        entry = self._release_entry(zip_bytes)
        self.allow["rel-font"] = entry
        for bad in (
            self._release_payload(entry, size=entry["source"]["zip_size"] + 1),
            self._release_payload(entry, name="Other.zip"),
            self._release_payload(entry, url="https://github.com/author/repo/releases/download/v1.0/Other.zip"),
            self._release_payload(entry, tag="v1.1"),
        ):
            service = self.service([bad])
            service.grant_consent("github-fonts", "nat")
            with self.assertRaises(AssetProviderError) as rejected:
                service.search("github-fonts", "rel-font")
            self.assertEqual(rejected.exception.status_code, 422)

    def test_release_zip_rejections(self) -> None:
        """Each rule fires on its own: the entry pins the delivered zip's size/SHA, so the failure
        comes from the member rule under test, not from the zip hash."""
        import shutil
        import subprocess
        import tempfile
        import zipfile

        good = self._zip({"Test-rel.ttf": REL_FONT})
        cases = {
            "two members": self._zip({"Test-rel.ttf": REL_FONT, "extra.txt": b"x"}),
            "renamed member": self._zip({"Other.ttf": REL_FONT}),
            "wrong member bytes": self._zip({"Test-rel.ttf": OTF}),
            "bzip2": self._zip({"Test-rel.ttf": REL_FONT}, compress=zipfile.ZIP_BZIP2),
            "not a zip": b"PK\x03\x04garbage" * 10,
        }
        if shutil.which("zip"):  # a genuinely encrypted archive (zipfile cannot write one)
            with tempfile.TemporaryDirectory() as tmp:
                src = Path(tmp) / "Test-rel.ttf"
                src.write_bytes(REL_FONT)
                out = Path(tmp) / "enc.zip"
                subprocess.run(["zip", "-q", "-j", "-P", "secret", str(out), str(src)], check=True)
                cases["encrypted"] = out.read_bytes()
        for label, zip_bytes in cases.items():
            self.allow["rel-font"] = self._release_entry(zip_bytes)  # pins THIS zip's size/sha
            service = self.service([self._release_payload(self.allow["rel-font"]), OFL_LICENSE, zip_bytes])
            service.grant_consent("github-fonts", "nat")
            token = service.search("github-fonts", "rel-font")["items"][0]["import_token"]
            with self.assertRaises(AssetProviderError, msg=label) as rejected:
                service.import_candidate(token)
            self.assertIn(rejected.exception.status_code, (422, 502), label)
            self.assertFalse((self.project / asset_registry.PROVENANCE_REL).exists(), label)
        # asset replaced upstream: entry pins the good zip, a different zip arrives
        self.allow["rel-font"] = self._release_entry(good)
        service = self.service([self._release_payload(self.allow["rel-font"]), OFL_LICENSE, good + b"\0"])
        service.grant_consent("github-fonts", "nat")
        token = service.search("github-fonts", "rel-font")["items"][0]["import_token"]
        with self.assertRaises(AssetProviderError):
            service.import_candidate(token)

    def test_release_entry_schema_is_validated_at_load(self) -> None:
        good = self._zip({"Test-rel.ttf": REL_FONT})
        entry = self._release_entry(good)
        for mutate in (
            lambda e: e["source"].update(url="https://evil.example/x.zip"),
            lambda e: e["source"].update(url="https://github.com/other/repo/releases/download/v1/x.zip"),
            lambda e: e["source"].update(member="../x.ttf"),
            lambda e: e["source"].update(member_sha256="zz"),
            lambda e: e["source"].update(zip_size=0),
            lambda e: e.update(font_path="different.ttf"),
        ):
            import copy
            bad = copy.deepcopy(entry); mutate(bad)
            raw = json.dumps({"fonts": {"rel-font": {k: v for k, v in bad.items() if k != "id"}}}).encode("utf-8")
            self._patch.stop()
            try:
                github_fonts._CACHE = None
                with (
                    mock.patch.object(github_fonts.MANIFEST_PATH.__class__, "read_bytes", return_value=raw),
                    mock.patch.object(github_fonts, "MANIFEST_SHA256", hashlib.sha256(raw).hexdigest()),
                ):
                    self.assertIsNone(github_fonts.entry("rel-font"))
            finally:
                github_fonts._CACHE = None
                self._patch.start()

    def test_edited_manifest_makes_provider_unavailable(self) -> None:
        self._patch.stop()
        try:
            github_fonts._CACHE = None
            with mock.patch.object(github_fonts, "MANIFEST_SHA256", "0" * 64):
                self.assertFalse(github_fonts.available())
                self.assertIsNone(github_fonts.entry("mantou-sans"))
        finally:
            github_fonts._CACHE = None
            self._patch.start()


if __name__ == "__main__":
    unittest.main()
