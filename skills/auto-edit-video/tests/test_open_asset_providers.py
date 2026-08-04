from __future__ import annotations

from copy import deepcopy
import sys
import unittest
from urllib.parse import parse_qs, urlsplit

SKILL_DIR = __import__("pathlib").Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from open_asset_providers import (  # noqa: E402
    OpenAssetCandidate,
    ProviderDataError,
    normalize_query,
    openverse_search_url,
    parse_openverse,
    parse_wikimedia,
    wikimedia_search_url,
)


OPENVERSE_ID = "123e4567-e89b-12d3-a456-426614174000"


def openverse_item(**overrides) -> dict:
    item = {
        "id": OPENVERSE_ID,
        "title": "A cat",
        "foreign_landing_url": "https://example.org/image",
        "url": "https://provider.example/arbitrary-original.jpg",
        "creator": "Jane Example",
        "license": "by",
        "license_version": "4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "attribution": "Jane Example",
        "mature": False,
        "width": 640,
        "height": 480,
        "filetype": "jpg",
    }
    item.update(overrides)
    return item


def wikimedia_page(**metadata_overrides) -> dict:
    metadata = {
        "LicenseShortName": {"value": "CC BY 4.0"},
        "LicenseUrl": {"value": "https://creativecommons.org/licenses/by/4.0/"},
        "Attribution": {"value": "<b>Jane Example</b>"},
        "AttributionRequired": {"value": "true"},
        "Artist": {"value": "Jane Example"},
        "Credit": {"value": "Example Archive"},
        "NonFree": {"value": "false"},
    }
    metadata.update(metadata_overrides)
    return {
        "pageid": 123,
        "title": "File:Cat.jpg",
        "imageinfo": [
            {
                "mime": "image/jpeg",
                "width": 640,
                "height": 480,
                "thumburl": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/a.jpg",
                "descriptionurl": "https://commons.wikimedia.org/wiki/File:Cat.jpg",
                "extmetadata": metadata,
            }
        ],
    }


class QueryAndUrlTests(unittest.TestCase):
    def test_normalize_query_nfkc_trim_collapse_and_term_bound(self) -> None:
        self.assertEqual(normalize_query("  Ｃａｔ   video  "), "Cat video")
        self.assertEqual(normalize_query("a b c d e f"), "a b c d e f")
        for query in ("", "a b c d e f g", "a\nb", "a\x00b"):
            with self.assertRaises(ProviderDataError):
                normalize_query(query)
        with self.assertRaises(ProviderDataError):
            normalize_query("x " * 121)

    def test_url_builders_are_deterministic_and_scoped(self) -> None:
        open_url = openverse_search_url("  Ｃａｔ   video ", page=2, page_size=5)
        self.assertEqual(open_url, openverse_search_url("Cat video", page=2, page_size=5))
        open_query = parse_qs(urlsplit(open_url).query)
        self.assertEqual(urlsplit(open_url).scheme, "https")
        self.assertEqual(urlsplit(open_url).netloc, "api.openverse.org")
        self.assertEqual(open_query["q"], ["Cat video"])
        self.assertEqual(open_query["license"], ["cc0,by"])
        self.assertNotIn("token", open_query)

        wiki_url = wikimedia_search_url("cat", page=3, page_size=4)
        wiki_query = parse_qs(urlsplit(wiki_url).query)
        self.assertEqual(urlsplit(wiki_url).netloc, "commons.wikimedia.org")
        self.assertEqual(wiki_query["gsroffset"], ["8"])
        self.assertEqual(wiki_query["gsrnamespace"], ["6"])
        self.assertEqual(wiki_query["iiurlwidth"], ["1920"])
        self.assertEqual(
            wiki_query["iiextmetadatafilter"],
            ["LicenseShortName|LicenseUrl|Attribution|AttributionRequired|Artist|Credit|NonFree"],
        )

    def test_url_builder_bounds(self) -> None:
        for builder in (openverse_search_url, wikimedia_search_url):
            for kwargs in (
                {"page": 0},
                {"page": 501},
                {"page_size": 0},
                {"page_size": 21},
                {"page": True},
            ):
                with self.assertRaises(ProviderDataError):
                    builder("cat", **kwargs)


class OpenverseParserTests(unittest.TestCase):
    def test_license_name_and_url_must_identify_the_same_canonical_license(self) -> None:
        hostile_urls = (
            "https://attacker.invalid/not-cc-by",
            "https://creativecommons.org/licenses/by/4.0////",
        )
        for license_url in hostile_urls:
            with self.subTest(license_url=license_url):
                hostile = openverse_item(license_url=license_url)
                self.assertEqual(parse_openverse({"results": [hostile]}), [])

    def test_licences_that_forbid_commercial_use_or_derivatives_are_dropped(self) -> None:
        # The delivery is a derivative work and may be used commercially, so
        # only CC0 and CC BY qualify. Everything else is dropped by not being
        # on the allowlist rather than by being named, which is why this is
        # asserted directly.
        for name, version in (
            ("by-nc", "4.0"),
            ("by-nd", "4.0"),
            ("by-nc-nd", "4.0"),
            ("by-nc-sa", "4.0"),
            ("by-sa", "4.0"),
            ("by", "3.0"),
        ):
            with self.subTest(licence=f"{name} {version}"):
                item = openverse_item(
                    license=name,
                    license_version=version,
                    license_url=f"https://creativecommons.org/licenses/{name}/{version}/",
                )
                self.assertEqual(parse_openverse({"results": [item]}), [])

    def test_cc0_and_by_candidates_use_fixed_download_url(self) -> None:
        cc0 = openverse_item(
            id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            title="Public cat",
            creator=None,
            attribution=None,
            license="cc0",
            license_version="1.0",
            license_url="https://creativecommons.org/publicdomain/zero/1.0/",
            filetype="webp",
        )
        result = parse_openverse({"results": [cc0, openverse_item()]})
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].license_spdx, "CC0-1.0")
        self.assertFalse(result[0].attribution_required)
        self.assertEqual(result[0].mime_type, "image/webp")
        self.assertEqual(
            result[1].download_url,
            "https://api.openverse.org/v1/images/123e4567-e89b-12d3-a456-426614174000/thumb/?full_size=true&compressed=false",
        )
        self.assertNotIn("arbitrary-original", result[1].download_url)

    def test_openverse_bad_items_skip_and_top_level_shape_raises(self) -> None:
        bad = [
            openverse_item(mature=True),
            openverse_item(id="not-a-uuid"),
            openverse_item(license="by", license_version="3.0"),
            openverse_item(filetype="gif"),
            openverse_item(foreign_landing_url="http://example.org/image"),
            openverse_item(license_url="https://user:secret@example.org/license"),
            openverse_item(creator="", attribution=""),
        ]
        self.assertEqual(parse_openverse({"results": bad + [openverse_item()]}), [
            parse_openverse({"results": [openverse_item()]})[0]
        ])
        self.assertEqual(len(parse_openverse({"results": [openverse_item()] * 21})), 20)
        for payload in (None, {}, {"results": {}}, {"results": ["bad"]}):
            if payload == {"results": ["bad"]}:
                self.assertEqual(parse_openverse(payload), [])
            else:
                with self.assertRaises(ProviderDataError):
                    parse_openverse(payload)

    def test_public_dict_is_safe_and_dataclass_is_immutable(self) -> None:
        candidate = parse_openverse({"results": [openverse_item()]})[0]
        self.assertIsInstance(candidate, OpenAssetCandidate)
        public = candidate.public_dict()
        self.assertNotIn("download_url", public)
        self.assertEqual(public["provider_id"], "openverse")
        with self.assertRaises((AttributeError, TypeError)):
            candidate.title = "changed"  # type: ignore[misc]


class WikimediaParserTests(unittest.TestCase):
    def test_license_name_and_url_must_identify_the_same_canonical_license(self) -> None:
        hostile = wikimedia_page(
            LicenseUrl={"value": "https://attacker.invalid/not-cc-by"}
        )
        self.assertEqual(parse_wikimedia({"query": {"pages": [hostile]}}), [])

    def test_cc_by_html_plain_text_and_artist_credit_fallback(self) -> None:
        page = wikimedia_page(
            Attribution={"value": "<b>Jane</b> &amp; <script>alert(1)</script>"}
        )
        result = parse_wikimedia({"query": {"pages": [page]}})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].license_spdx, "CC-BY-4.0")
        self.assertEqual(result[0].attribution_text, "Jane &")

        fallback = wikimedia_page(Attribution={"value": ""})
        fallback_result = parse_wikimedia({"query": {"pages": [fallback]}})
        self.assertEqual(fallback_result[0].attribution_text, "Jane Example — Example Archive")

    def test_wikimedia_cc0_and_shape_limits(self) -> None:
        cc0 = wikimedia_page(
            LicenseShortName={"value": "CC0 1.0"},
            LicenseUrl={"value": "https://creativecommons.org/publicdomain/zero/1.0/"},
            AttributionRequired={"value": "false"},
            Attribution={"value": ""},
            Artist={"value": ""},
            Credit={"value": ""},
        )
        result = parse_wikimedia({"query": {"pages": [cc0]}})
        self.assertEqual(result[0].license_spdx, "CC0-1.0")
        self.assertFalse(result[0].attribution_required)
        self.assertEqual(result[0].provider_id, "wikimedia")

        too_long = wikimedia_page()
        too_long["title"] = "x" * 301
        self.assertEqual(parse_wikimedia({"query": {"pages": [too_long]}}), [])

    def test_hostile_wikimedia_items_fail_closed(self) -> None:
        base = wikimedia_page()
        cases = [
            {"imageinfo": [{**base["imageinfo"][0], "thumburl": "javascript:alert(1)"}]},
            {"imageinfo": [{**base["imageinfo"][0], "thumburl": "https://evil.example/thumb/a.jpg"}]},
            {"imageinfo": [{**base["imageinfo"][0], "descriptionurl": "https://evil.example/wiki/File:x"}]},
            {"NonFree": {"value": "true"}},
            {"LicenseShortName": {"value": "CC BY-SA 4.0"}},
            {"Attribution": {"value": ""}, "Artist": {"value": ""}, "Credit": {"value": ""}},
        ]
        for change in cases:
            page = deepcopy(base)
            if "imageinfo" in change:
                page.update(change)
            else:
                page["imageinfo"][0]["extmetadata"].update(change)
            self.assertEqual(parse_wikimedia({"query": {"pages": [page]}}), [])

        for payload in (None, {}, {"query": {}}, {"query": {"pages": {}}}):
            with self.assertRaises(ProviderDataError):
                parse_wikimedia(payload)
        self.assertEqual(parse_wikimedia({"query": {"pages": ["bad"]}}), [])
        self.assertEqual(
            len(parse_wikimedia({"query": {"pages": [wikimedia_page()] * 21}})), 20
        )


if __name__ == "__main__":
    unittest.main()
