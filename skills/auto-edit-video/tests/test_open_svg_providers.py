from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest
from urllib.parse import parse_qs, urlsplit


SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from open_svg_providers import (  # noqa: E402
    HEROICONS_REF,
    LUCIDE_REF,
    SVG_MIME,
    TABLER_REF,
    ProviderDataError,
    SvgAssetCandidate,
    heroicons_candidate,
    lucide_candidate,
    normalize_svg_slug,
    parse_wikimedia_svg,
    tabler_candidate,
    wikimedia_svg_search_url,
)


def metadata(**overrides: object) -> dict[str, dict[str, str]]:
    value = {
        "LicenseShortName": {"value": "CC BY 4.0"},
        "LicenseUrl": {"value": "https://creativecommons.org/licenses/by/4.0/"},
        "Attribution": {"value": "<b>Jane Example</b>"},
        "AttributionRequired": {"value": "true"},
        "Artist": {"value": "Jane Example"},
        "Credit": {"value": "Example Archive"},
        "NonFree": {"value": "false"},
    }
    value.update(overrides)  # type: ignore[arg-type]
    return value


def wikimedia_page(**info_overrides: object) -> dict[str, object]:
    info: dict[str, object] = {
        "mime": "image/svg+xml",
        "width": 24,
        "height": 24,
        "url": "https://upload.wikimedia.org/wikipedia/commons/a/ab/Cat.svg",
        "thumburl": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Cat.svg/128px-Cat.svg.png",
        "descriptionurl": "https://commons.wikimedia.org/wiki/File:Cat.svg",
        "extmetadata": metadata(),
    }
    info.update(info_overrides)
    return {
        "pageid": 123,
        "title": "File:Cat.svg",
        "imageinfo": [info],
    }


class IconAdapterTests(unittest.TestCase):
    def test_each_repo_builder_is_fixed_and_svg_only(self) -> None:
        cases = (
            (
                heroicons_candidate("arrow-right"),
                "tailwindlabs/heroicons",
                HEROICONS_REF,
                "optimized/24/outline/arrow-right.svg",
                "MIT",
            ),
            (
                lucide_candidate("arrow-right"),
                "lucide-icons/lucide",
                LUCIDE_REF,
                "icons/arrow-right.svg",
                "ISC",
            ),
            (
                tabler_candidate("arrow-right"),
                "tabler/tabler-icons",
                TABLER_REF,
                "icons/outline/arrow-right.svg",
                "MIT",
            ),
        )
        for candidate, repo, ref, path, license_spdx in cases:
            with self.subTest(provider=candidate.provider_id):
                self.assertIsInstance(candidate, SvgAssetCandidate)
                self.assertEqual(candidate.mime_type, SVG_MIME)
                self.assertEqual(candidate.width, 24)
                self.assertEqual(candidate.height, 24)
                self.assertEqual(candidate.license_spdx, license_spdx)
                self.assertIn(f"raw.githubusercontent.com/{repo}/{ref}/{path}", candidate.download_url)
                self.assertIn(f"github.com/{repo}/blob/{ref}/{path}", candidate.landing_url)
                self.assertNotIn("download_url", candidate.public_dict())

    def test_slug_validation_normalizes_nfkc_and_rejects_injection(self) -> None:
        self.assertEqual(normalize_svg_slug("ａｒｒｏｗ-ｒｉｇｈｔ"), "arrow-right")
        bad = (
            "",
            " ",
            "arrow right",
            "../arrow",
            "arrow/../x",
            "arrow%2fright",
            "arrow?x=1",
            "arrow#x",
            "arrow&x",
            "arrow\x00right",
            "brand-github",
            "logo-github",
            "trademark-mark",
            "étoile",
            "Arrow-right",
            "a" * 101,
        )
        for slug in bad:
            with self.subTest(slug=repr(slug)):
                with self.assertRaises(ProviderDataError):
                    heroicons_candidate(slug)
                with self.assertRaises(ProviderDataError):
                    lucide_candidate(slug)
                with self.assertRaises(ProviderDataError):
                    tabler_candidate(slug)

    def test_immutable_candidate_rejects_wrong_mime_and_mutation(self) -> None:
        candidate = heroicons_candidate("activity")
        with self.assertRaises((AttributeError, TypeError)):
            candidate.title = "changed"  # type: ignore[misc]
        with self.assertRaises(ProviderDataError):
            SvgAssetCandidate(
                provider_id="test",
                candidate_id="one",
                title="One",
                download_url="https://example.org/one.svg",
                landing_url="https://example.org/one",
                creator="Example",
                license_spdx="MIT",
                license_url="https://example.org/LICENSE",
                attribution_text="Example",
                mime_type="image/png",
                width=24,
                height=24,
                attribution_required=False,
            )


class WikimediaSvgTests(unittest.TestCase):
    def test_search_url_is_commons_svg_scoped_and_bounded(self) -> None:
        url = wikimedia_svg_search_url("  Ｃａｔ   icon ", page=3, page_size=4)
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "commons.wikimedia.org")
        self.assertEqual(query["gsrnamespace"], ["6"])
        self.assertEqual(query["gsrsearch"], ["Cat icon filetype:svg"])
        self.assertEqual(query["gsroffset"], ["8"])
        self.assertEqual(query["gsrlimit"], ["4"])
        self.assertIn("LicenseShortName", query["iiextmetadatafilter"][0])
        for kwargs in ({"page": 0}, {"page_size": 21}, {"page": True}):
            with self.assertRaises(ProviderDataError):
                wikimedia_svg_search_url("cat", **kwargs)

    def test_parse_accepts_canonical_cc0_by_and_by_sa(self) -> None:
        for name, url, required, spdx in (
            (
                "CC0 1.0",
                "https://creativecommons.org/publicdomain/zero/1.0/",
                "false",
                "CC0-1.0",
            ),
            (
                "CC BY 4.0",
                "https://creativecommons.org/licenses/by/4.0/",
                "true",
                "CC-BY-4.0",
            ),
            (
                "CC BY-SA 4.0",
                "https://creativecommons.org/licenses/by-sa/4.0/",
                "true",
                "CC-BY-SA-4.0",
            ),
        ):
            page = wikimedia_page(
                extmetadata=metadata(
                    LicenseShortName={"value": name},
                    LicenseUrl={"value": url},
                    AttributionRequired={"value": required},
                    Attribution={"value": "Jane Example" if required == "true" else ""},
                )
            )
            result = parse_wikimedia_svg({"query": {"pages": [page]}})
            with self.subTest(license=spdx):
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0].license_spdx, spdx)
                self.assertEqual(result[0].mime_type, SVG_MIME)
                self.assertEqual(result[0].candidate_id, "123")

    def test_bad_items_skip_and_top_level_shape_raises(self) -> None:
        base = wikimedia_page()
        bad_pages = [
            deepcopy(base),
            {**deepcopy(base), "pageid": 0},
            {**deepcopy(base), "pageid": "123"},
            {**deepcopy(base), "title": ""},
            {**deepcopy(base), "imageinfo": [{**base["imageinfo"][0], "mime": "image/png"}]},  # type: ignore[index]
            {**deepcopy(base), "imageinfo": [{**base["imageinfo"][0], "url": "https://evil.example/x.svg"}]},  # type: ignore[index]
            {**deepcopy(base), "imageinfo": [{**base["imageinfo"][0], "descriptionurl": "https://evil.example/wiki/x"}]},  # type: ignore[index]
            {**deepcopy(base), "imageinfo": [{**base["imageinfo"][0], "extmetadata": metadata(LicenseShortName={"value": "CC BY-NC 4.0"})}]},  # type: ignore[index]
            {**deepcopy(base), "imageinfo": [{**base["imageinfo"][0], "extmetadata": metadata(LicenseShortName={"value": "CC BY-ND 4.0"})}]},  # type: ignore[index]
            {**deepcopy(base), "imageinfo": [{**base["imageinfo"][0], "extmetadata": metadata(NonFree={"value": "true"})}]},  # type: ignore[index]
            {**deepcopy(base), "imageinfo": [{**base["imageinfo"][0], "extmetadata": metadata(LicenseUrl={"value": "https://evil.example/license"})}]},  # type: ignore[index]
        ]
        self.assertEqual(len(parse_wikimedia_svg({"query": {"pages": bad_pages}})), 1)
        for payload in (None, {}, {"query": {}}, {"query": {"pages": {}}}):
            with self.assertRaises(ProviderDataError):
                parse_wikimedia_svg(payload)
        self.assertEqual(parse_wikimedia_svg({"query": {"pages": ["bad"]}}), [])
        self.assertEqual(
            len(parse_wikimedia_svg({"query": {"pages": [base] * 21}})), 20
        )


if __name__ == "__main__":
    unittest.main()
