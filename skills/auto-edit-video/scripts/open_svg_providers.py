#!/usr/bin/env python3
"""Deterministic, network-free SVG metadata adapters.

This module only validates caller supplied metadata and constructs URLs from
fixed, pinned repository references.  It never fetches or parses SVG bytes.
The three icon repositories are exact-slug adapters (there is deliberately no
free-form repository search), while the Wikimedia adapter parses a caller
supplied Action API response.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
import re
from typing import Any
import unicodedata
from urllib.parse import urlencode, urlsplit

from open_asset_providers import ProviderDataError, canonical_license_url, normalize_query


SVG_MIME = "image/svg+xml"
WIKIMEDIA_ENDPOINT = "https://commons.wikimedia.org/w/api.php"

# These are the peeled commit objects for immutable release tags.  Keeping a
# commit rather than a mutable branch/tag makes the URL builder deterministic
# even if a repository later moves a release tag.
HEROICONS_REF = "0435d4ca364a608cc75e2f8683d374e55abbae26"  # heroicons v2.2.0
LUCIDE_REF = "f12b0de177fbc2a6795e99be065887e72b237123"  # lucide 0.468.0
TABLER_REF = "8ac7d81b72ece11072ef25ea9fd92e80c6f3c9fc"  # tabler-icons v3.46.0

# Descriptive aliases make the pin explicit to callers without duplicating
# mutable configuration in a future provider service.
HEROICONS_PINNED_REF = HEROICONS_REF
LUCIDE_PINNED_REF = LUCIDE_REF
TABLER_PINNED_REF = TABLER_REF

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_HEX_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_REJECTED_ICON_PREFIXES = ("brand-", "logo-", "trademark-")

_WIKIMEDIA_METADATA_FIELDS = (
    "LicenseShortName",
    "LicenseUrl",
    "Attribution",
    "AttributionRequired",
    "Artist",
    "Credit",
    "NonFree",
)
_WIKIMEDIA_METADATA_FILTER = "|".join(_WIKIMEDIA_METADATA_FIELDS)

_CANONICAL_SVG_LICENSES = {
    "CC0-1.0": "https://creativecommons.org/publicdomain/zero/1.0/",
    "CC-BY-4.0": "https://creativecommons.org/licenses/by/4.0/",
    "CC-BY-SA-4.0": "https://creativecommons.org/licenses/by-sa/4.0/",
}
_LICENSE_CC0 = re.compile(r"^cc0(?:[- ]1\.0)(?: universal)?$")
_LICENSE_BY = re.compile(r"^cc[- ]by(?:[- ]4\.0)(?: international)?$")
_LICENSE_BY_SA = re.compile(
    r"^cc[- ]by[- ]sa(?:[- ]4\.0)(?: international)?$"
)


def _has_control(value: str) -> bool:
    """Return whether *value* has C0/control or Unicode format characters."""

    return any(unicodedata.category(char) in {"Cc", "Cf"} for char in value)


def _bounded_text(value: Any, maximum: int, *, required: bool = False) -> str | None:
    """Validate a bounded provider text value without coercing arbitrary data."""

    if value is None and not required:
        return ""
    if not isinstance(value, str) or len(value) > maximum or _has_control(value):
        return None
    value = value.strip()
    if required and not value:
        return None
    return value


def _positive_int(value: Any, *, maximum: int = 100_000) -> int | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > maximum
    ):
        return None
    return value


def _https_url(
    value: Any,
    *,
    hostname: str | None = None,
    path_prefix: str | None = None,
    path_required: bool = False,
) -> str | None:
    """Validate an HTTPS URL used as provider metadata.

    The SVG adapters never accept a URL supplied by icon callers.  This helper
    is for Wikimedia's response only and therefore rejects credentials,
    non-standard ports, controls, whitespace, encoded path characters, and
    query/fragment components that could hide a different resource.
    """

    if not isinstance(value, str) or len(value) > 2048 or _has_control(value):
        return None
    if not value or any(char.isspace() for char in value):
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
        parsed_hostname = parsed.hostname
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.netloc
        or parsed_hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.query
        or parsed.fragment
        or "%" in parsed.path
        or "//" in parsed.path
    ):
        return None
    if hostname is not None and parsed_hostname.casefold() != hostname.casefold():
        return None
    if path_required and not parsed.path:
        return None
    if path_prefix is not None and not parsed.path.startswith(path_prefix):
        return None
    # Prevent dot-segment aliases even when the path happens to start with an
    # allowlisted prefix.  Wikimedia file paths normally contain no such
    # segments, so rejecting them is strictly safer than normalizing them.
    if any(segment in {".", ".."} for segment in parsed.path.split("/")):
        return None
    return value


def _validate_commit(ref: str) -> str:
    if not _HEX_COMMIT_RE.fullmatch(ref):
        raise ProviderDataError("provider reference is not an immutable commit")
    return ref


def normalize_svg_slug(slug: str) -> str:
    """Normalize and validate one exact icon slug.

    Full-width ASCII is normalized with NFKC, then only lower-case ASCII
    kebab-case is accepted.  This intentionally rejects whitespace, path
    traversal, percent encoding, URL/meta characters, controls, and all
    non-ASCII slugs.  The caller cannot provide a repository URL or ref.
    """

    if not isinstance(slug, str):
        raise ProviderDataError("SVG slug must be a string")
    normalized = unicodedata.normalize("NFKC", slug)
    if _has_control(normalized):
        raise ProviderDataError("SVG slug contains a control character")
    if len(normalized) > 100 or not normalized.isascii() or not _SLUG_RE.fullmatch(normalized):
        raise ProviderDataError("SVG slug must be lower-case ASCII kebab-case")
    if normalized.startswith(_REJECTED_ICON_PREFIXES):
        raise ProviderDataError("brand and trademark SVG icons are not allowed")
    return normalized


def _validate_candidate_url(value: Any, field: str) -> str:
    validated = _https_url(value, path_required=True)
    if validated is None:
        raise ProviderDataError(f"SVG candidate {field} URL is invalid")
    return validated


@dataclass(frozen=True)
class SvgAssetCandidate:
    """Immutable, browser-safe SVG metadata.

    ``download_url`` is intentionally retained only in the private candidate
    object; :meth:`public_dict` omits it so a browser cannot turn metadata
    search results into an arbitrary download primitive.
    """

    provider_id: str
    candidate_id: str
    title: str
    download_url: str
    landing_url: str
    creator: str
    license_spdx: str
    license_url: str
    attribution_text: str
    mime_type: str
    width: int
    height: int
    attribution_required: bool

    def __post_init__(self) -> None:
        for field in ("provider_id", "candidate_id", "title", "license_spdx"):
            value = getattr(self, field)
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 1000
                or _has_control(value)
                or value != value.strip()
            ):
                raise ProviderDataError(f"SVG candidate {field} is invalid")
        for field in ("creator", "attribution_text"):
            value = getattr(self, field)
            if (
                not isinstance(value, str)
                or len(value) > 1000
                or _has_control(value)
                or value != value.strip()
            ):
                raise ProviderDataError(f"SVG candidate {field} is invalid")
        if self.mime_type != SVG_MIME:
            raise ProviderDataError("SVG candidate MIME must be image/svg+xml")
        if not isinstance(self.attribution_required, bool):
            raise ProviderDataError("SVG candidate attribution flag is invalid")
        if _positive_int(self.width) is None or _positive_int(self.height) is None:
            raise ProviderDataError("SVG candidate dimensions are invalid")
        _validate_candidate_url(self.download_url, "download")
        _validate_candidate_url(self.landing_url, "landing")
        _validate_candidate_url(self.license_url, "license")

    def public_dict(self) -> dict[str, Any]:
        """Return metadata safe to expose to a browser, excluding download URL."""

        return {
            "provider_id": self.provider_id,
            "candidate_id": self.candidate_id,
            "title": self.title,
            "landing_url": self.landing_url,
            "creator": self.creator,
            "license_spdx": self.license_spdx,
            "license_url": self.license_url,
            "attribution_text": self.attribution_text,
            "mime_type": self.mime_type,
            "width": self.width,
            "height": self.height,
            "attribution_required": self.attribution_required,
        }


def _repo_candidate(
    *,
    provider_id: str,
    slug: str,
    repo: str,
    ref: str,
    path: str,
    license_spdx: str,
    creator: str,
) -> SvgAssetCandidate:
    slug = normalize_svg_slug(slug)
    ref = _validate_commit(ref)
    # ``path`` is a local constant selected by this module, never caller data.
    relative_path = f"{path}{slug}.svg"
    download_url = f"https://raw.githubusercontent.com/{repo}/{ref}/{relative_path}"
    landing_url = f"https://github.com/{repo}/blob/{ref}/{relative_path}"
    license_url = f"https://github.com/{repo}/blob/{ref}/LICENSE"
    return SvgAssetCandidate(
        provider_id=provider_id,
        candidate_id=slug,
        title=slug,
        download_url=download_url,
        landing_url=landing_url,
        creator=creator,
        license_spdx=license_spdx,
        license_url=license_url,
        attribution_text=creator,
        mime_type=SVG_MIME,
        width=24,
        height=24,
        attribution_required=True,
    )


def heroicons_candidate(slug: str) -> SvgAssetCandidate:
    """Build one exact-slug Heroicons outline 24px candidate."""

    return _repo_candidate(
        provider_id="heroicons",
        slug=slug,
        repo="tailwindlabs/heroicons",
        ref=HEROICONS_REF,
        path="optimized/24/outline/",
        license_spdx="MIT",
        creator="Tailwind Labs",
    )


def lucide_candidate(slug: str) -> SvgAssetCandidate:
    """Build one exact-slug Lucide candidate."""

    return _repo_candidate(
        provider_id="lucide",
        slug=slug,
        repo="lucide-icons/lucide",
        ref=LUCIDE_REF,
        path="icons/",
        license_spdx="ISC",
        creator="Lucide Contributors",
    )


def tabler_candidate(slug: str) -> SvgAssetCandidate:
    """Build one exact-slug Tabler outline candidate (brand icons excluded)."""

    return _repo_candidate(
        provider_id="tabler",
        slug=slug,
        repo="tabler/tabler-icons",
        ref=TABLER_REF,
        path="icons/outline/",
        license_spdx="MIT",
        creator="Tabler Icons",
    )


# Explicit aliases keep the adapter naming unsurprising for callers that use a
# ``build_*`` convention; all aliases retain the same exact-slug semantics.
build_heroicons_candidate = heroicons_candidate
build_lucide_candidate = lucide_candidate
build_tabler_candidate = tabler_candidate


def heroicons_query(slug: str) -> SvgAssetCandidate:
    """Resolve an exact Heroicons slug without performing network I/O."""

    return heroicons_candidate(slug)


def lucide_query(slug: str) -> SvgAssetCandidate:
    """Resolve an exact Lucide slug without performing network I/O."""

    return lucide_candidate(slug)


def tabler_query(slug: str) -> SvgAssetCandidate:
    """Resolve an exact Tabler slug without performing network I/O."""

    return tabler_candidate(slug)


class _PlainTextHTMLParser(HTMLParser):
    """Minimal HTML-to-text parser copied locally from image provider policy.

    The helper is intentionally local because the source module's parser is
    private; SVG metadata never gets inserted into a DOM or interpreted as
    markup after this conversion.
    """

    _BLOCK_TAGS = frozenset({"br", "div", "li", "p", "pre", "section", "tr"})
    _HIDDEN_TAGS = frozenset({"script", "style", "template"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        tag = tag.casefold()
        if tag in self._HIDDEN_TAGS:
            self._hidden_depth += 1
        elif self._hidden_depth == 0 and tag in self._BLOCK_TAGS:
            self.parts.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if self._hidden_depth == 0 and tag.casefold() in self._BLOCK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in self._HIDDEN_TAGS and self._hidden_depth:
            self._hidden_depth -= 1
        elif self._hidden_depth == 0 and tag in self._BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._hidden_depth == 0:
            self.parts.append(data)


def _html_plain_text(value: Any, maximum: int) -> str | None:
    if not isinstance(value, str) or len(value) > maximum:
        return None
    try:
        parser = _PlainTextHTMLParser()
        parser.feed(value)
        parser.close()
        text = unescape("".join(parser.parts))
    except (TypeError, ValueError):
        return None
    cleaned: list[str] = []
    for char in text:
        category = unicodedata.category(char)
        if category == "Cc":
            if char.isspace():
                cleaned.append(" ")
            else:
                return None
        elif category == "Cf":
            return None
        else:
            cleaned.append(char)
    text = " ".join("".join(cleaned).split())
    if len(text) > maximum:
        return None
    return text


def _metadata_text(
    metadata: dict[str, Any], key: str, maximum: int, *, required: bool = False
) -> str | None:
    if key not in metadata:
        return None if required else ""
    raw = metadata[key]
    if not isinstance(raw, dict) or not isinstance(raw.get("value"), str):
        return None
    value = _html_plain_text(raw["value"], maximum)
    if value is None:
        return None
    if required and not value:
        return None
    return value


def _parse_required_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    normalized = value.casefold()
    if normalized in {"true", "yes", "1"}:
        return True
    if normalized in {"false", "no", "0"}:
        return False
    return None


def _wikimedia_svg_license(name: str | None) -> tuple[str, bool] | None:
    if not name:
        return None
    normalized = " ".join(unicodedata.normalize("NFKC", name).casefold().split())
    if _LICENSE_CC0.fullmatch(normalized):
        return "CC0-1.0", False
    if _LICENSE_BY.fullmatch(normalized):
        return "CC-BY-4.0", True
    if _LICENSE_BY_SA.fullmatch(normalized):
        return "CC-BY-SA-4.0", True
    return None


def _metadata_is_well_shaped(metadata: dict[str, Any]) -> bool:
    for key in _WIKIMEDIA_METADATA_FIELDS:
        if key in metadata:
            value = metadata[key]
            if not isinstance(value, dict) or not isinstance(value.get("value"), str):
                return False
    return True


def wikimedia_svg_search_url(query: str, page: int = 1, page_size: int = 12) -> str:
    """Build a deterministic Commons API search URL restricted to SVG files."""

    normalized_query = normalize_query(query)
    if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 500:
        raise ProviderDataError("page must be an integer in 1..500")
    if (
        isinstance(page_size, bool)
        or not isinstance(page_size, int)
        or not 1 <= page_size <= 20
    ):
        raise ProviderDataError("page_size must be an integer in 1..20")
    params = [
        ("action", "query"),
        ("format", "json"),
        ("formatversion", 2),
        ("generator", "search"),
        ("gsrnamespace", 6),
        ("gsrsearch", f"{normalized_query} filetype:svg"),
        ("gsrlimit", page_size),
        ("gsroffset", (page - 1) * page_size),
        ("prop", "imageinfo"),
        ("iiprop", "url|extmetadata|mime|size"),
        ("iiextmetadatafilter", _WIKIMEDIA_METADATA_FILTER),
    ]
    return f"{WIKIMEDIA_ENDPOINT}?{urlencode(params)}"


# A short alias mirrors the image provider's ``wikimedia_search_url`` name.
wikimedia_svg_search = wikimedia_svg_search_url


def parse_wikimedia_svg(payload: Any) -> list[SvgAssetCandidate]:
    """Parse up to twenty safe Wikimedia Commons SVG candidates.

    Malformed top-level shape raises :class:`ProviderDataError`; malformed
    individual pages are skipped.  This function inspects metadata only and
    never reads, parses, or executes the SVG document itself.
    """

    if not isinstance(payload, dict):
        raise ProviderDataError("Wikimedia SVG payload must be an object")
    query = payload.get("query")
    if not isinstance(query, dict) or not isinstance(query.get("pages"), list):
        raise ProviderDataError("Wikimedia SVG payload must contain query.pages")

    candidates: list[SvgAssetCandidate] = []
    for page in query["pages"][:20]:
        if not isinstance(page, dict):
            continue
        pageid = _positive_int(page.get("pageid"))
        title = _bounded_text(page.get("title"), 300, required=True)
        imageinfo = page.get("imageinfo")
        if pageid is None or title is None or not isinstance(imageinfo, list) or not imageinfo:
            continue
        info = imageinfo[0]
        if not isinstance(info, dict):
            continue
        mime = info.get("mime")
        if not isinstance(mime, str) or mime.strip().casefold() != SVG_MIME:
            continue
        width = _positive_int(info.get("width"))
        height = _positive_int(info.get("height"))
        if width is None or height is None:
            continue

        # Prefer Wikimedia's original URL.  A few API fixtures only expose a
        # thumbnail URL, so that field is accepted as a bounded fallback; both
        # forms are constrained to Commons' upload path.
        if "url" in info:
            download_url = _https_url(
                info.get("url"),
                hostname="upload.wikimedia.org",
                path_prefix="/wikipedia/commons/",
                path_required=True,
            )
            if download_url is None:
                continue
        else:
            download_url = _https_url(
                info.get("thumburl"),
                hostname="upload.wikimedia.org",
                path_prefix="/wikipedia/commons/",
                path_required=True,
            )
            if download_url is None:
                continue
        if "thumburl" in info:
            thumburl = _https_url(
                info.get("thumburl"),
                hostname="upload.wikimedia.org",
                path_prefix="/wikipedia/commons/",
                path_required=True,
            )
            if thumburl is None:
                continue

        landing_url = _https_url(
            info.get("descriptionurl"),
            hostname="commons.wikimedia.org",
            path_required=True,
        )
        metadata = info.get("extmetadata")
        if (
            landing_url is None
            or not isinstance(metadata, dict)
            or not _metadata_is_well_shaped(metadata)
        ):
            continue

        nonfree = _metadata_text(metadata, "NonFree", 32, required=True)
        if _parse_required_bool(nonfree) is not False:
            continue

        license_name = _metadata_text(
            metadata, "LicenseShortName", 300, required=True
        )
        license_evidence = _metadata_text(metadata, "LicenseUrl", 2048, required=True)
        license_data = _wikimedia_svg_license(license_name)
        if license_data is None or license_evidence is None:
            continue
        license_spdx, attribution_required = license_data
        license_url = canonical_license_url(license_evidence, license_spdx)
        if license_url is None:
            continue

        required_text = _metadata_text(
            metadata, "AttributionRequired", 32, required=True
        )
        required_flag = _parse_required_bool(required_text)
        if required_flag is None or required_flag is not attribution_required:
            continue

        attribution = _metadata_text(metadata, "Attribution", 1000)
        artist = _metadata_text(metadata, "Artist", 300)
        credit = _metadata_text(metadata, "Credit", 300)
        if attribution is None or artist is None or credit is None:
            continue
        creator = artist or credit
        if attribution_required and not attribution:
            fallback = [part for part in (artist, credit) if part]
            attribution = " — ".join(fallback)
            if len(attribution) > 1000:
                continue
        if attribution_required and not attribution:
            continue

        candidates.append(
            SvgAssetCandidate(
                provider_id="wikimedia",
                candidate_id=str(pageid),
                title=title,
                download_url=download_url,
                landing_url=landing_url,
                creator=creator,
                license_spdx=license_spdx,
                license_url=license_url,
                attribution_text=attribution,
                mime_type=SVG_MIME,
                width=width,
                height=height,
                attribution_required=attribution_required,
            )
        )
    return candidates


__all__ = [
    "HEROICONS_PINNED_REF",
    "HEROICONS_REF",
    "LUCIDE_PINNED_REF",
    "LUCIDE_REF",
    "SVG_MIME",
    "TABLER_PINNED_REF",
    "TABLER_REF",
    "SvgAssetCandidate",
    "ProviderDataError",
    "build_heroicons_candidate",
    "build_lucide_candidate",
    "build_tabler_candidate",
    "heroicons_candidate",
    "heroicons_query",
    "lucide_candidate",
    "lucide_query",
    "normalize_svg_slug",
    "parse_wikimedia_svg",
    "tabler_candidate",
    "tabler_query",
    "wikimedia_svg_search",
    "wikimedia_svg_search_url",
]
