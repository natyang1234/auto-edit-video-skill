#!/usr/bin/env python3
"""Deterministic, network-free parsers for public image providers.

The functions in this module only build URLs and parse caller-supplied JSON.
They never fetch a URL and never trust a provider-supplied download URL.  A
malformed item is skipped without exposing its payload; malformed top-level
responses raise :class:`ProviderDataError` so callers can fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
import re
from typing import Any
import unicodedata
from urllib.parse import urlencode, urlsplit


OPENVERSE_ENDPOINT = "https://api.openverse.org/v1/images/"
WIKIMEDIA_ENDPOINT = "https://commons.wikimedia.org/w/api.php"
OPENVERSE_DOWNLOAD_TEMPLATE = (
    "https://api.openverse.org/v1/images/{candidate_id}/thumb/"
    "?full_size=true&compressed=false"
)
WIKIMEDIA_METADATA_FIELDS = (
    "LicenseShortName",
    "LicenseUrl",
    "Attribution",
    "AttributionRequired",
    "Artist",
    "Credit",
    "NonFree",
)
WIKIMEDIA_METADATA_FILTER = "|".join(WIKIMEDIA_METADATA_FIELDS)

_MIME_BY_FILETYPE = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}
_MIME_ALLOWED = frozenset(_MIME_BY_FILETYPE.values())
_UUID_LIKE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_LICENSE_CC0 = re.compile(r"^cc0(?:[- ]1\.0)(?: universal)?$")
_LICENSE_BY = re.compile(r"^cc[- ]by(?:[- ]4\.0)(?: international)?$")
_CANONICAL_LICENSE_URLS = {
    "CC0-1.0": "https://creativecommons.org/publicdomain/zero/1.0/",
    "CC-BY-4.0": "https://creativecommons.org/licenses/by/4.0/",
    "CC-BY-SA-4.0": "https://creativecommons.org/licenses/by-sa/4.0/",
}


class ProviderDataError(ValueError):
    """Raised when a provider response or URL-builder argument is malformed."""


@dataclass(frozen=True)
class OpenAssetCandidate:
    """A safe, immutable asset candidate shared by both providers."""

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

    def public_dict(self) -> dict[str, Any]:
        """Return fields safe to send to a browser; never expose download_url."""
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


def _has_control(value: str) -> bool:
    """Return whether a string contains C0/control or format characters."""
    return any(unicodedata.category(char) in {"Cc", "Cf"} for char in value)


def normalize_query(query: str) -> str:
    """Normalize a human-entered search query under privacy-safe bounds."""
    if not isinstance(query, str):
        raise ProviderDataError("query must be a string")
    normalized = unicodedata.normalize("NFKC", query)
    if _has_control(normalized):
        raise ProviderDataError("query contains a control character")
    normalized = " ".join(normalized.split())
    terms = normalized.split(" ") if normalized else []
    if not terms or len(terms) > 6:
        raise ProviderDataError("query must contain 1-6 non-space terms")
    if len(normalized) > 120:
        raise ProviderDataError("query exceeds 120 characters")
    return normalized


def _validate_page(value: int, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ProviderDataError(f"{name} must be an integer in 1..{maximum}")
    return value


def _validate_paging(page: int, page_size: int) -> tuple[int, int]:
    return _validate_page(page, "page", 500), _validate_page(page_size, "page_size", 20)


def openverse_search_url(query: str, page: int = 1, page_size: int = 12) -> str:
    """Build the deterministic, token-free Openverse search URL."""
    normalized_query = normalize_query(query)
    page, page_size = _validate_paging(page, page_size)
    params = [
        ("q", normalized_query),
        ("page", page),
        ("page_size", page_size),
        ("license", "cc0,by"),
    ]
    return f"{OPENVERSE_ENDPOINT}?{urlencode(params)}"


def wikimedia_search_url(query: str, page: int = 1, page_size: int = 12) -> str:
    """Build the deterministic Wikimedia Commons Action API URL."""
    normalized_query = normalize_query(query)
    page, page_size = _validate_paging(page, page_size)
    params = [
        ("action", "query"),
        ("format", "json"),
        ("formatversion", 2),
        ("generator", "search"),
        ("gsrnamespace", 6),
        ("gsrsearch", normalized_query),
        ("gsrlimit", page_size),
        ("gsroffset", (page - 1) * page_size),
        ("prop", "imageinfo"),
        ("iiprop", "url|extmetadata|mime|size"),
        ("iiurlwidth", 1920),
        ("iiextmetadatafilter", WIKIMEDIA_METADATA_FILTER),
    ]
    return f"{WIKIMEDIA_ENDPOINT}?{urlencode(params)}"


def _bounded_text(value: Any, maximum: int, *, required: bool = False) -> str | None:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or len(value) > maximum or _has_control(value):
        return None
    value = value.strip()
    if required and not value:
        return None
    return value


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _https_url(
    value: Any,
    *,
    hostname: str | None = None,
    path_prefix: str | None = None,
) -> str | None:
    if not isinstance(value, str) or len(value) > 2048 or _has_control(value):
        return None
    if any(char.isspace() for char in value):
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
        parsed_hostname = parsed.hostname
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or parsed_hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        return None
    if hostname is not None and parsed_hostname.casefold() != hostname.casefold():
        return None
    if path_prefix is not None and not parsed.path.startswith(path_prefix):
        return None
    return value


def _fixed_openverse_download(candidate_id: str) -> str:
    return OPENVERSE_DOWNLOAD_TEMPLATE.format(candidate_id=candidate_id)


def canonical_license_url(value: Any, spdx: str) -> str | None:
    """Return the canonical CC URL only when host and path match *spdx*."""
    expected = _CANONICAL_LICENSE_URLS.get(spdx)
    candidate = _https_url(value, hostname="creativecommons.org")
    if candidate is None or expected is None:
        return None
    parsed = urlsplit(candidate)
    if parsed.query or parsed.fragment:
        return None
    expected_path = urlsplit(expected).path
    if parsed.path not in {expected_path, expected_path.rstrip("/")}:
        return None
    return expected


def _openverse_license(item: dict[str, Any]) -> tuple[str, bool] | None:
    license_name = item.get("license")
    version = item.get("license_version")
    if not isinstance(license_name, str) or not isinstance(version, str):
        return None
    pair = (license_name.strip().casefold(), version.strip().casefold())
    if pair == ("cc0", "1.0"):
        return "CC0-1.0", False
    if pair == ("by", "4.0"):
        return "CC-BY-4.0", True
    return None


def parse_openverse(payload: Any) -> list[OpenAssetCandidate]:
    """Parse up to twenty safe Openverse candidates, skipping bad items."""
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ProviderDataError("Openverse payload must contain a results list")

    candidates: list[OpenAssetCandidate] = []
    for item in payload["results"][:20]:
        if not isinstance(item, dict):
            continue
        candidate_id = item.get("id")
        if not isinstance(candidate_id, str) or not _UUID_LIKE.fullmatch(candidate_id):
            continue
        if item.get("mature") is not False:
            continue
        title = _bounded_text(item.get("title"), 300, required=True)
        creator = _bounded_text(item.get("creator"), 300)
        attribution = _bounded_text(item.get("attribution"), 1000)
        if title is None or creator is None or attribution is None:
            continue
        landing_url = _https_url(item.get("foreign_landing_url"))
        license_url = _https_url(item.get("license_url"))
        source_url = item.get("url")
        if (
            landing_url is None
            or license_url is None
            or not isinstance(source_url, str)
            or len(source_url) > 2048
            or _has_control(source_url)
        ):
            continue
        filetype = item.get("filetype")
        if not isinstance(filetype, str):
            continue
        mime_type = _MIME_BY_FILETYPE.get(filetype.strip().casefold())
        if mime_type is None:
            continue
        dimensions = (_positive_int(item.get("width")), _positive_int(item.get("height")))
        if dimensions[0] is None or dimensions[1] is None:
            continue
        license_data = _openverse_license(item)
        if license_data is None:
            continue
        license_spdx, attribution_required = license_data
        license_url = canonical_license_url(license_url, license_spdx)
        if license_url is None:
            continue
        if attribution_required and (not creator or not attribution):
            continue
        candidates.append(
            OpenAssetCandidate(
                provider_id="openverse",
                candidate_id=candidate_id,
                title=title,
                download_url=_fixed_openverse_download(candidate_id),
                landing_url=landing_url,
                creator=creator,
                license_spdx=license_spdx,
                license_url=license_url,
                attribution_text=attribution,
                mime_type=mime_type,
                width=dimensions[0],
                height=dimensions[1],
                attribution_required=attribution_required,
            )
        )
    return candidates


class _PlainTextHTMLParser(HTMLParser):
    """Small HTML-to-text parser with script/style content suppressed."""

    _BLOCK_TAGS = frozenset({"br", "div", "li", "p", "pre", "section", "tr"})
    _HIDDEN_TAGS = frozenset({"script", "style", "template"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in self._HIDDEN_TAGS:
            self._hidden_depth += 1
        elif self._hidden_depth == 0 and tag in self._BLOCK_TAGS:
            self.parts.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
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
    if not isinstance(value, str):
        return None
    try:
        parser = _PlainTextHTMLParser()
        parser.feed(value)
        parser.close()
        text = unescape("".join(parser.parts))
    except (TypeError, ValueError):
        return None
    # Remove control characters while preserving a separator for whitespace
    # controls so adjacent words cannot be accidentally concatenated.
    cleaned: list[str] = []
    for char in text:
        category = unicodedata.category(char)
        if category == "Cc":
            if char.isspace():
                cleaned.append(" ")
            continue
        if category == "Cf":
            continue
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
    return _html_plain_text(raw["value"], maximum)


def _parse_required_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    normalized = value.casefold()
    if normalized in {"true", "yes", "1"}:
        return True
    if normalized in {"false", "no", "0"}:
        return False
    return None


def _wikimedia_license(name: str | None) -> tuple[str, bool] | None:
    if not name:
        return None
    normalized = " ".join(name.casefold().split())
    if _LICENSE_CC0.fullmatch(normalized):
        return "CC0-1.0", False
    if _LICENSE_BY.fullmatch(normalized):
        return "CC-BY-4.0", True
    return None


def _wikimedia_metadata_is_well_shaped(metadata: dict[str, Any]) -> bool:
    for key in WIKIMEDIA_METADATA_FIELDS:
        if key in metadata and (
            not isinstance(metadata[key], dict)
            or not isinstance(metadata[key].get("value"), str)
        ):
            return False
    return True


def parse_wikimedia(payload: Any) -> list[OpenAssetCandidate]:
    """Parse up to twenty safe Wikimedia Commons candidates."""
    if not isinstance(payload, dict):
        raise ProviderDataError("Wikimedia payload must be an object")
    query = payload.get("query")
    if not isinstance(query, dict) or not isinstance(query.get("pages"), list):
        raise ProviderDataError("Wikimedia payload must contain query.pages")

    candidates: list[OpenAssetCandidate] = []
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
        if not isinstance(mime, str) or mime.strip().casefold() not in _MIME_ALLOWED:
            continue
        mime_type = mime.strip().casefold()
        width = _positive_int(info.get("width"))
        height = _positive_int(info.get("height"))
        if width is None or height is None:
            continue
        download_url = _https_url(
            info.get("thumburl"),
            hostname="upload.wikimedia.org",
            path_prefix="/wikipedia/commons/thumb/",
        )
        landing_url = _https_url(
            info.get("descriptionurl"), hostname="commons.wikimedia.org"
        )
        metadata = info.get("extmetadata")
        if (
            download_url is None
            or landing_url is None
            or not isinstance(metadata, dict)
            or not _wikimedia_metadata_is_well_shaped(metadata)
        ):
            continue

        nonfree = _metadata_text(metadata, "NonFree", 32)
        if nonfree is None:
            continue
        nonfree_flag = _parse_required_bool(nonfree)
        if nonfree_flag is None and nonfree:
            continue
        if nonfree_flag is True:
            continue

        license_name = _metadata_text(metadata, "LicenseShortName", 300, required=True)
        license_url = _metadata_text(metadata, "LicenseUrl", 2048, required=True)
        if license_name is None or license_url is None or not license_name or not license_url:
            continue
        license_data = _wikimedia_license(license_name)
        license_url = canonical_license_url(license_url, license_data[0]) if license_data else None
        if license_data is None or license_url is None:
            continue
        license_spdx, attribution_required = license_data

        required_text = _metadata_text(metadata, "AttributionRequired", 32, required=True)
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
            fallback_parts = [part for part in (artist, credit) if part]
            attribution = " — ".join(fallback_parts)
            if len(attribution) > 1000:
                continue
        if attribution_required and not attribution:
            continue
        candidates.append(
            OpenAssetCandidate(
                provider_id="wikimedia",
                candidate_id=str(pageid),
                title=title,
                download_url=download_url,
                landing_url=landing_url,
                creator=creator,
                license_spdx=license_spdx,
                license_url=license_url,
                attribution_text=attribution,
                mime_type=mime_type,
                width=width,
                height=height,
                attribution_required=attribution_required,
            )
        )
    return candidates


__all__ = [
    "OpenAssetCandidate",
    "ProviderDataError",
    "canonical_license_url",
    "normalize_query",
    "openverse_search_url",
    "parse_openverse",
    "parse_wikimedia",
    "wikimedia_search_url",
]
