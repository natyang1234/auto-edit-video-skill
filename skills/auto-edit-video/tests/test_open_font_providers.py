from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest


SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from open_font_providers import (  # noqa: E402
    FONT_MIME,
    FONTSOURCE_CDN_ENDPOINT,
    GOOGLE_FONTS_REF,
    FontAssetCandidate,
    ProviderDataError,
    fontsource_discovery_url,
    google_fonts_contents_url,
    normalize_fontsource_id,
    normalize_fontsource_version,
    normalize_google_family_id,
    parse_fontsource,
    parse_google_fonts_contents,
)


SHA_A = "a" * 40
SHA_B = "b" * 40


def google_file(root: str = "ofl", family: str = "roboto", **overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "name": "Roboto-Regular.ttf",
        "path": f"{root}/{family}/Roboto-Regular.ttf",
        "sha": SHA_A,
        "size": 42_000,
        "type": "file",
        "url": "https://api.github.com/untrusted",
        "html_url": "https://evil.example/untrusted",
        "download_url": "https://evil.example/untrusted.ttf",
    }
    item.update(overrides)
    return item


def google_license(root: str = "ofl", family: str = "roboto", **overrides: object) -> dict[str, object]:
    name = {"ofl": "OFL.txt", "apache": "LICENSE.txt", "ufl": "UFL.txt"}[root]
    item: dict[str, object] = {
        "name": name,
        "path": f"{root}/{family}/{name}",
        "sha": SHA_B,
        "size": 2_000,
        "type": "file",
        "url": "https://api.github.com/untrusted",
    }
    item.update(overrides)
    return item


def fontsource_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "roboto",
        "family": "Roboto",
        "version": "5.1.0",
        "license": {
            "id": "Apache-2.0",
            "url": "https://www.apache.org/licenses/LICENSE-2.0",
        },
        "variants": [
            {
                "subset": "latin",
                "weight": 400,
                "style": "normal",
                "unicodeRange": "U+0000-00FF",
                "url": f"{FONTSOURCE_CDN_ENDPOINT}/roboto@5.1.0/latin-400-normal.ttf",
            },
            {
                "subset": "latin-ext",
                "weight": 700,
                "style": "italic",
                "unicodeRange": "U+0100-024F",
            },
        ],
    }
    payload.update(overrides)
    return payload


class GoogleFontProviderTests(unittest.TestCase):
    def test_family_and_contents_url_are_pinned(self) -> None:
        self.assertEqual(normalize_google_family_id("ｒｏｂｏｔｏ"), "roboto")
        self.assertEqual(
            google_fonts_contents_url("ofl", "roboto"),
            "https://api.github.com/repos/google/fonts/contents/ofl/roboto"
            f"?ref={GOOGLE_FONTS_REF}",
        )
        for value in ("", "Roboto", "open-sans", "../roboto", "roboto/x", "ro%bot", "a\x00b", "é"):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ProviderDataError):
                    normalize_google_family_id(value)
        with self.assertRaises(ProviderDataError):
            google_fonts_contents_url("main", "roboto")

    def test_listing_rebuilds_urls_and_requires_the_matching_license(self) -> None:
        candidates = parse_google_fonts_contents(
            [google_file(), google_license()], "ofl", "roboto"
        )
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertIsInstance(candidate, FontAssetCandidate)
        self.assertEqual(candidate.family, "roboto")
        self.assertEqual(candidate.candidate_id, f"{SHA_A}:Roboto-Regular.ttf")
        self.assertEqual(candidate.version, GOOGLE_FONTS_REF)
        self.assertEqual(candidate.mime_type, FONT_MIME)
        self.assertTrue(candidate.attribution_required)
        self.assertIn(f"/{GOOGLE_FONTS_REF}/ofl/roboto/Roboto-Regular.ttf", candidate.download_url)
        self.assertIn(f"/{GOOGLE_FONTS_REF}/ofl/roboto/OFL.txt", candidate.license_url)
        self.assertNotIn("evil.example", candidate.download_url)
        self.assertNotIn("download_url", candidate.public_dict())
        self.assertNotIn("license_download_url", candidate.public_dict())
        self.assertEqual(
            candidate.license_download_url,
            "https://raw.githubusercontent.com/google/fonts/"
            f"{GOOGLE_FONTS_REF}/ofl/roboto/OFL.txt",
        )
        with self.assertRaises(ProviderDataError):
            parse_google_fonts_contents([google_file()], "ofl", "roboto")

    def test_listing_skips_bad_files_but_fails_license_ambiguity(self) -> None:
        bad = [
            google_file(type="dir", name="dir.ttf", path="ofl/roboto/dir.ttf"),
            google_file(name="font.woff2", path="ofl/roboto/font.woff2"),
            google_file(path="apache/roboto/Roboto-Regular.ttf"),
            google_file(sha="not-a-sha", name="bad-sha.ttf", path="ofl/roboto/bad-sha.ttf"),
            google_file(size=50 * 1024 * 1024 + 1, name="big.ttf", path="ofl/roboto/big.ttf"),
            google_file(name="Roboto-Regular.ttf", path="ofl/roboto/../Roboto-Regular.ttf"),
        ]
        self.assertEqual(
            len(parse_google_fonts_contents(bad + [google_file(), google_license()], "ofl", "roboto")),
            1,
        )
        for payload in (
            [google_file(), google_license(), google_license()],
            [google_file(), google_license(type="dir")],
            [google_file(), google_license(path="apache/roboto/OFL.txt")],
        ):
            with self.assertRaises(ProviderDataError):
                parse_google_fonts_contents(payload, "ofl", "roboto")
        with self.assertRaises(ProviderDataError):
            parse_google_fonts_contents([google_file(), google_license()], "ofl", "roboto", ref="main")
        for payload in (None, {}, {"items": []}, "not-list"):
            with self.assertRaises(ProviderDataError):
                parse_google_fonts_contents(payload, "ofl", "roboto")

    def test_all_google_license_roots_are_canonical(self) -> None:
        for root, license_name in (("ofl", "OFL-1.1"), ("apache", "Apache-2.0"), ("ufl", "Ubuntu-font-1.0")):
            candidate = parse_google_fonts_contents(
                [google_file(root=root), google_license(root=root)], root, "roboto"
            )[0]
            self.assertEqual(candidate.license_spdx, license_name)


class FontsourceProviderTests(unittest.TestCase):
    def test_id_version_and_discovery_url_are_strict(self) -> None:
        self.assertEqual(normalize_fontsource_id("ｒｏｂｏｔｏ"), "roboto")
        self.assertEqual(fontsource_discovery_url("roboto"), "https://api.fontsource.org/v1/fonts/roboto")
        for value in ("", "Roboto", "../roboto", "roboto/evil", "roboto%2f", "roboto\x00"):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ProviderDataError):
                    normalize_fontsource_id(value)
        for value in ("latest", "^5.1.0", ">=5.1.0", "5.1", "5.1.0-beta.1", "05.1.0"):
            with self.subTest(value=value):
                with self.assertRaises(ProviderDataError):
                    normalize_fontsource_version(value)
        self.assertEqual(normalize_fontsource_version("5.1.0"), "5.1.0")

    def test_list_variants_rebuilds_ttf_urls_and_hides_download(self) -> None:
        candidates = parse_fontsource(fontsource_payload(), "roboto", "5.1.0")
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0].download_url, f"{FONTSOURCE_CDN_ENDPOINT}/roboto@5.1.0/latin-400-normal.ttf")
        self.assertTrue(candidates[0].download_url.endswith(".ttf"))
        self.assertEqual(candidates[0].license_spdx, "Apache-2.0")
        self.assertEqual(candidates[0].landing_url, "https://fontsource.org/fonts/roboto")
        self.assertNotIn("download_url", candidates[0].public_dict())
        self.assertNotIn("license_download_url", candidates[0].public_dict())
        self.assertEqual(
            candidates[0].license_download_url,
            "https://cdn.jsdelivr.net/npm/@fontsource/roboto@5.1.0/LICENSE",
        )
        with self.assertRaises((AttributeError, TypeError)):
            candidates[0].family = "changed"  # type: ignore[misc]

    def test_nested_variants_and_license_evidence(self) -> None:
        payload = fontsource_payload(
            license={"id": "OFL-1.1", "url": "https://fontsource.org/fonts/roboto/license"},
            variants={
                "400": {"normal": {"latin": {"url": "https://cdn.jsdelivr.net/fontsource/fonts/roboto@5.1.0/latin-400-normal.ttf", "unicodeRange": "U+0000-00FF"}}},
            },
        )
        candidates = parse_fontsource(payload, "roboto", "5.1.0")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].license_spdx, "OFL-1.1")
        self.assertEqual(candidates[0].subset, "latin")

    def test_official_nested_url_object_and_unicode_range_map(self) -> None:
        payload = fontsource_payload(
            unicodeRange={"latin": "U+0000-00FF"},
            variants={
                "400": {
                    "normal": {
                        "latin": {
                            "url": {
                                "woff2": "https://evil.example/font.woff2",
                                "woff": "https://evil.example/font.woff",
                                "ttf": "https://cdn.jsdelivr.net/fontsource/fonts/roboto@5.1.0/latin-400-normal.ttf",
                            }
                        }
                    }
                }
            },
        )
        candidates = parse_fontsource(payload, "roboto", "5.1.0")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].unicode_range, "U+0000-00FF")

    def test_mismatch_external_woff_and_duplicate_fail_closed(self) -> None:
        for overrides in (
            {"id": "other-font"},
            {"version": "5.1.1"},
            {"license": {"id": "MIT", "url": "https://opensource.org/licenses/MIT"}},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(ProviderDataError):
                    parse_fontsource(fontsource_payload(**overrides), "roboto", "5.1.0")

        external = deepcopy(fontsource_payload())
        external["variants"] = [
            {
                "subset": "latin",
                "weight": 400,
                "style": "normal",
                "url": "https://evil.example/font.ttf",
            }
        ]
        self.assertEqual(parse_fontsource(external, "roboto", "5.1.0"), [])

        port = deepcopy(fontsource_payload())
        port["variants"] = [
            {
                "subset": "latin",
                "weight": 400,
                "style": "normal",
                "url": "https://cdn.jsdelivr.net:443/fontsource/fonts/roboto@5.1.0/latin-400-normal.ttf",
            }
        ]
        self.assertEqual(parse_fontsource(port, "roboto", "5.1.0"), [])

        woff = deepcopy(fontsource_payload())
        woff["variants"] = [
            {
                "subset": "latin",
                "weight": 400,
                "style": "normal",
                "url": "https://cdn.jsdelivr.net/fontsource/fonts/roboto@5.1.0/latin-400-normal.woff2",
            }
        ]
        self.assertEqual(parse_fontsource(woff, "roboto", "5.1.0"), [])

        duplicate = fontsource_payload()
        duplicate["variants"] = [
            {"subset": "latin", "weight": 400, "style": "normal"},
            {"subset": "latin", "weight": 400, "style": "normal"},
        ]
        with self.assertRaises(ProviderDataError):
            parse_fontsource(duplicate, "roboto", "5.1.0")

    def test_variant_shape_and_top_level_errors(self) -> None:
        bad = fontsource_payload()
        bad["variants"] = [{"subset": "../latin", "weight": 400, "style": "normal"}, "bad"]
        self.assertEqual(parse_fontsource(bad, "roboto", "5.1.0"), [])
        for payload in (None, [], {}, {"id": "roboto"}):
            with self.assertRaises(ProviderDataError):
                parse_fontsource(payload, "roboto", "5.1.0")
        with self.assertRaises(ProviderDataError):
            parse_fontsource(fontsource_payload(), "roboto", "latest")


if __name__ == "__main__":
    unittest.main()
