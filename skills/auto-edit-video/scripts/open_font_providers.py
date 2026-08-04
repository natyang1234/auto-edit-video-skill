#!/usr/bin/env python3
"""Deterministic, network-free Google Fonts and Fontsource adapters.

The adapters in this module deliberately do not perform provider I/O.  A
caller builds one of the discovery URLs, fetches it through its own consented
transport, and passes the JSON response here for validation.  URLs retained
on a candidate are rebuilt from validated identifiers and pinned versions;
provider supplied URLs are never trusted as-is.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterator, Mapping
import unicodedata
from urllib.parse import urlsplit

from open_asset_providers import ProviderDataError, normalize_query


GOOGLE_FONTS_REF = "2796410152d4f9524b68ed46e69c1b60f8e0f7c3"
GOOGLE_FONTS_REPOSITORY = "google/fonts"
GOOGLE_FONTS_CONTENTS_ENDPOINT = (
    "https://api.github.com/repos/google/fonts/contents"
)
GOOGLE_FONTS_RAW_ENDPOINT = "https://raw.githubusercontent.com/google/fonts"
GOOGLE_FONTS_LANDING_ENDPOINT = "https://github.com/google/fonts/blob"

FONTSOURCE_API_ENDPOINT = "https://api.fontsource.org/v1/fonts"
FONTSOURCE_CDN_ENDPOINT = "https://cdn.jsdelivr.net/fontsource/fonts"
FONTSOURCE_NPM_ENDPOINT = "https://cdn.jsdelivr.net/npm/@fontsource"
FONTSOURCE_LANDING_ENDPOINT = "https://fontsource.org/fonts"

FONT_MIME = "font/ttf"
FONT_TTF_MIME = FONT_MIME

_GOOGLE_ROOT_LICENSES = {
    "ofl": ("OFL-1.1", "OFL.txt"),
    "apache": ("Apache-2.0", "LICENSE.txt"),
    "ufl": ("Ubuntu-font-1.0", "UFL.txt"),
}
GOOGLE_FONT_LICENSES = {
    root: license_name for root, (license_name, _filename) in _GOOGLE_ROOT_LICENSES.items()
}

# Fixed evidence URLs are used for Fontsource candidates.  They are stable
# license documents, rather than a URL supplied by the provider payload.
FONT_LICENSE_URLS = {
    "OFL-1.1": "https://scripts.sil.org/OFL",
    "Apache-2.0": "https://www.apache.org/licenses/LICENSE-2.0",
    "Ubuntu-font-1.0": "https://ubuntu.com/legal/font-licence",
}
FONT_LICENSE_CANONICAL_URLS = FONT_LICENSE_URLS

_FONT_LICENSE_ALIASES = {
    "ofl-1.1": "OFL-1.1",
    "ofl 1.1": "OFL-1.1",
    "sil ofl 1.1": "OFL-1.1",
    "apache-2.0": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
    "ubuntu-font-1.0": "Ubuntu-font-1.0",
    "ubuntu font licence 1.0": "Ubuntu-font-1.0",
    "ubuntu font license 1.0": "Ubuntu-font-1.0",
}

_GOOGLE_FAMILY_RE = re.compile(r"^[a-z0-9]{1,80}$")
_FONT_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SEMVER_RE = re.compile(r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA_RE = _COMMIT_RE
_FILE_NAME_RE = re.compile(r"^[^/\\]+$")
_COMPONENT_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_STYLE_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_NESTED_STYLE_KEYS = frozenset({"normal", "italic", "oblique", "regular"})

GOOGLE_FONT_MAX_BYTES = 50 * 1024 * 1024
FONT_METADATA_MAX_BYTES = 2 * 1024 * 1024
FONT_MAX_TEXT = 1000
FONT_MAX_UNICODE_RANGE = 16 * 1024
MAX_GOOGLE_LISTING_ITEMS = 5000
MAX_FONTSOURCE_VARIANTS = 2000


def _has_control(value: str) -> bool:
    return any(unicodedata.category(char) in {"Cc", "Cf"} for char in value)


def _bounded_text(value: Any, maximum: int, *, required: bool = False) -> str | None:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or len(value) > maximum or _has_control(value):
        return None
    value = value.strip()
    if required and not value:
        return None
    return value


def _normalize_nfkc_ascii(value: Any, *, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ProviderDataError(f"{label} must be a string")
    normalized = unicodedata.normalize("NFKC", value)
    if (
        not normalized
        or len(normalized) > maximum
        or not normalized.isascii()
        or _has_control(normalized)
        or normalized != normalized.lower()
        or any(char.isspace() for char in normalized)
    ):
        raise ProviderDataError(f"{label} must be lower-case ASCII")
    return normalized


def normalize_google_family_id(family: str) -> str:
    """Validate one Google Fonts repository family directory id."""

    normalized = _normalize_nfkc_ascii(family, label="Google family", maximum=80)
    if not _GOOGLE_FAMILY_RE.fullmatch(normalized):
        raise ProviderDataError("Google family must match [a-z0-9]{1,80}")
    if any(char in normalized for char in "/\\%?#"):
        raise ProviderDataError("Google family contains a path or URL character")
    return normalized


def normalize_fontsource_id(font_id: str) -> str:
    """Validate a Fontsource package id in strict lower-case kebab case."""

    normalized = _normalize_nfkc_ascii(font_id, label="Fontsource id", maximum=80)
    if not _FONT_ID_RE.fullmatch(normalized):
        raise ProviderDataError("Fontsource id must be lower-case ASCII kebab-case")
    return normalized


def normalize_fontsource_version(version: str) -> str:
    """Validate a release version (no ranges, ``latest``, or pre-releases)."""

    if not isinstance(version, str) or len(version) > 32 or not _SEMVER_RE.fullmatch(version):
        raise ProviderDataError("Fontsource version must be strict semver")
    return version


def _validate_commit(ref: Any) -> str:
    if not isinstance(ref, str) or not _COMMIT_RE.fullmatch(ref):
        raise ProviderDataError("Google Fonts reference must be an immutable commit")
    return ref


def _validate_google_root(root: Any) -> str:
    if not isinstance(root, str) or root not in _GOOGLE_ROOT_LICENSES:
        raise ProviderDataError("Google Fonts license root is not allowed")
    return root


def google_fonts_contents_url(root: str, family: str) -> str:
    """Build the pinned GitHub contents API URL for one family directory."""

    root = _validate_google_root(root)
    family = normalize_google_family_id(family)
    return (
        f"{GOOGLE_FONTS_CONTENTS_ENDPOINT}/{root}/{family}"
        f"?ref={GOOGLE_FONTS_REF}"
    )


build_google_fonts_contents_url = google_fonts_contents_url
google_fonts_listing_url = google_fonts_contents_url


def fontsource_discovery_url(font_id: str) -> str:
    """Build a Fontsource metadata discovery URL; it performs no I/O."""

    font_id = normalize_fontsource_id(font_id)
    return f"{FONTSOURCE_API_ENDPOINT}/{font_id}"


build_fontsource_discovery_url = fontsource_discovery_url
fontsource_font_url = fontsource_discovery_url


def _validate_https_url(value: Any, *, hostname: str | None = None) -> str | None:
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
        or any(segment in {".", ".."} for segment in parsed.path.split("/"))
    ):
        return None
    if hostname is not None and parsed_hostname.casefold() != hostname:
        return None
    return value


def _valid_size(value: Any, maximum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value <= maximum


def _valid_sha(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA_RE.fullmatch(value))


def _valid_google_name(value: Any) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 255
        or not value.isascii()
        or _has_control(value)
        or value != value.strip()
        or not _FILE_NAME_RE.fullmatch(value)
        or value in {".", ".."}
        or any(char in value for char in "%?#")
    ):
        return False
    return value.endswith(".ttf") or value in {"OFL.txt", "LICENSE.txt", "UFL.txt"}


def _canonical_license(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 120 or _has_control(value):
        return None
    normalized = " ".join(value.strip().split()).casefold()
    return _FONT_LICENSE_ALIASES.get(normalized)


def _font_candidate_url(value: Any, *, expected: str | None = None) -> str | None:
    validated = _validate_https_url(value)
    if validated is None:
        return None
    if expected is not None and validated != expected:
        return None
    return validated


@dataclass(frozen=True)
class FontAssetCandidate:
    """Immutable, browser-safe metadata for one TTF candidate.

    ``download_url`` stays private to the import service.  ``public_dict``
    intentionally omits it, just like the image/SVG provider candidates.
    """

    provider_id: str
    candidate_id: str
    family: str
    style: str
    weight: int | None
    subset: str
    unicode_range: str
    version: str
    license_spdx: str
    download_url: str
    landing_url: str
    license_url: str
    license_download_url: str
    mime_type: str
    attribution_required: bool = True

    def __post_init__(self) -> None:
        for field in ("provider_id", "candidate_id", "family", "version", "license_spdx"):
            value = getattr(self, field)
            if (
                not isinstance(value, str)
                or not value
                or len(value) > FONT_MAX_TEXT
                or _has_control(value)
                or value != value.strip()
            ):
                raise ProviderDataError(f"font candidate {field} is invalid")
        for field in ("style", "subset"):
            value = getattr(self, field)
            if (
                not isinstance(value, str)
                or len(value) > 120
                or _has_control(value)
                or value != value.strip()
            ):
                raise ProviderDataError(f"font candidate {field} is invalid")
        if (
            not isinstance(self.unicode_range, str)
            or len(self.unicode_range) > FONT_MAX_UNICODE_RANGE
            or _has_control(self.unicode_range)
            or self.unicode_range != self.unicode_range.strip()
        ):
            raise ProviderDataError("font candidate unicode range is invalid")
        if self.weight is not None and (
            isinstance(self.weight, bool)
            or not isinstance(self.weight, int)
            or not 1 <= self.weight <= 1000
        ):
            raise ProviderDataError("font candidate weight is invalid")
        if self.license_spdx not in FONT_LICENSE_URLS:
            raise ProviderDataError("font candidate license is not allowed")
        if self.mime_type != FONT_MIME:
            raise ProviderDataError("font candidate MIME must be font/ttf")
        if not isinstance(self.attribution_required, bool):
            raise ProviderDataError("font candidate attribution flag is invalid")
        for field in ("download_url", "landing_url", "license_url", "license_download_url"):
            if _font_candidate_url(getattr(self, field)) is None:
                raise ProviderDataError(f"font candidate {field} URL is invalid")

    @property
    def license(self) -> str:
        """Compatibility alias for callers that call the SPDX field license."""

        return self.license_spdx

    def public_dict(self) -> dict[str, Any]:
        """Return metadata safe for browser exposure, excluding downloads."""

        return {
            "provider_id": self.provider_id,
            "candidate_id": self.candidate_id,
            "family": self.family,
            "style": self.style,
            "weight": self.weight,
            "subset": self.subset,
            "unicode_range": self.unicode_range,
            "version": self.version,
            "license_spdx": self.license_spdx,
            "landing_url": self.landing_url,
            "license_url": self.license_url,
            "mime_type": self.mime_type,
            "attribution_required": self.attribution_required,
        }


def _google_candidate(
    *,
    root: str,
    family: str,
    name: str,
    sha: str,
    license_spdx: str,
    license_name: str,
) -> FontAssetCandidate:
    relative_path = f"{root}/{family}/{name}"
    license_path = f"{root}/{family}/{license_name}"
    return FontAssetCandidate(
        provider_id="google-fonts",
        candidate_id=f"{sha}:{name}",
        family=family,
        style="",
        weight=None,
        subset="",
        unicode_range="",
        version=GOOGLE_FONTS_REF,
        license_spdx=license_spdx,
        download_url=f"{GOOGLE_FONTS_RAW_ENDPOINT}/{GOOGLE_FONTS_REF}/{relative_path}",
        landing_url=f"{GOOGLE_FONTS_LANDING_ENDPOINT}/{GOOGLE_FONTS_REF}/{relative_path}",
        license_url=f"{GOOGLE_FONTS_LANDING_ENDPOINT}/{GOOGLE_FONTS_REF}/{license_path}",
        license_download_url=(
            f"{GOOGLE_FONTS_RAW_ENDPOINT}/{GOOGLE_FONTS_REF}/{license_path}"
        ),
        mime_type=FONT_MIME,
        attribution_required=True,
    )


def parse_google_fonts_contents(
    payload: Any,
    root: str,
    family: str,
    *,
    ref: str = GOOGLE_FONTS_REF,
) -> list[FontAssetCandidate]:
    """Parse one caller-supplied GitHub contents listing.

    The listing's license file is validated as part of the same response.  A
    malformed font item is skipped, while malformed top-level data or an
    ambiguous/missing license fails closed with :class:`ProviderDataError`.
    """

    root = _validate_google_root(root)
    family = normalize_google_family_id(family)
    if _validate_commit(ref) != GOOGLE_FONTS_REF:
        raise ProviderDataError("Google Fonts parser only accepts the pinned ref")
    if not isinstance(payload, list) or len(payload) > MAX_GOOGLE_LISTING_ITEMS:
        raise ProviderDataError("Google Fonts contents payload must be a bounded list")

    expected_license, expected_license_name = _GOOGLE_ROOT_LICENSES[root]
    expected_prefix = f"{root}/{family}/"
    seen_paths: set[str] = set()
    license_item: dict[str, Any] | None = None
    candidates: list[FontAssetCandidate] = []

    for raw_item in payload:
        if not isinstance(raw_item, dict):
            continue
        name = raw_item.get("name")
        path = raw_item.get("path")
        if isinstance(path, str):
            if path in seen_paths:
                raise ProviderDataError("Google Fonts listing contains duplicate paths")
            seen_paths.add(path)
        # A named license is security-critical: do not silently skip a broken
        # license entry and accidentally accept a listing without evidence.
        if name in {"OFL.txt", "LICENSE.txt", "UFL.txt"}:
            if name != expected_license_name:
                raise ProviderDataError("Google Fonts listing has the wrong license file")
            if not isinstance(path, str) or path != f"{expected_prefix}{name}":
                raise ProviderDataError("Google Fonts license path is invalid")
            if license_item is not None:
                raise ProviderDataError("Google Fonts listing contains duplicate license files")
            if (
                raw_item.get("type") != "file"
                or not _valid_size(raw_item.get("size"), FONT_METADATA_MAX_BYTES)
                or not _valid_sha(raw_item.get("sha"))
                or not _valid_google_name(name)
            ):
                raise ProviderDataError("Google Fonts license item is malformed")
            license_item = raw_item
            continue

        if (
            not isinstance(name, str)
            or not _valid_google_name(name)
            or not name.endswith(".ttf")
            or not isinstance(path, str)
            or path != f"{expected_prefix}{name}"
            or raw_item.get("type") != "file"
            or not _valid_size(raw_item.get("size"), GOOGLE_FONT_MAX_BYTES)
            or not _valid_sha(raw_item.get("sha"))
        ):
            continue
        candidates.append(
            _google_candidate(
                root=root,
                family=family,
                name=name,
                sha=raw_item["sha"],
                license_spdx=expected_license,
                license_name=expected_license_name,
            )
        )

    if license_item is None:
        raise ProviderDataError("Google Fonts listing has no corresponding license")
    return candidates


# Names used by callers in earlier adapter drafts are retained as harmless
# aliases; all of them point at the same deterministic implementation.
parse_google_fonts_listing = parse_google_fonts_contents
parse_google_fonts_repo_listing = parse_google_fonts_contents
parse_google_fonts = parse_google_fonts_contents


def _canonical_fontsource_license(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return _FONT_LICENSE_ALIASES.get(" ".join(value.strip().split()).casefold())


def _license_url_matches(value: Any, *, font_id: str, canonical: str) -> bool:
    expected = FONT_LICENSE_URLS[canonical]
    if not isinstance(value, str):
        return False
    if value == expected or value == expected + "/":
        return True
    validated = _validate_https_url(value, hostname="fontsource.org")
    if validated is None:
        return False
    parsed = urlsplit(validated)
    if parsed.netloc != "fontsource.org":
        return False
    return parsed.path in {f"/fonts/{font_id}", f"/fonts/{font_id}/license"}


def _parse_fontsource_license(payload: Mapping[str, Any], font_id: str) -> tuple[str, str]:
    if "license" not in payload and not any(
        key in payload for key in ("licenseId", "license_id", "licenseType", "license_type")
    ):
        raise ProviderDataError("Fontsource payload has no license evidence")

    raw_license = payload.get("license")
    values: list[Any] = []
    urls: list[Any] = []
    if isinstance(raw_license, str):
        values.append(raw_license)
    elif isinstance(raw_license, Mapping):
        for key in ("id", "spdx", "type", "name", "licenseId", "license_id"):
            if key in raw_license:
                values.append(raw_license[key])
        for key in ("url", "licenseUrl", "license_url"):
            if key in raw_license:
                urls.append(raw_license[key])
    elif raw_license is not None:
        raise ProviderDataError("Fontsource license field is malformed")
    for key in ("licenseId", "license_id", "licenseType", "license_type"):
        if key in payload:
            values.append(payload[key])
    for key in ("licenseUrl", "license_url"):
        if key in payload:
            urls.append(payload[key])

    canonical_values = {_canonical_fontsource_license(value) for value in values}
    canonical_values.discard(None)
    if not canonical_values or len(canonical_values) != 1:
        raise ProviderDataError("Fontsource license id is unknown or mismatched")
    # Any supplied identifier that is not canonical is an error, rather than
    # being ignored in favour of another duplicate field.
    if any(_canonical_fontsource_license(value) is None for value in values):
        raise ProviderDataError("Fontsource license id is unknown")
    canonical = next(iter(canonical_values))
    if any(not _license_url_matches(value, font_id=font_id, canonical=canonical) for value in urls):
        raise ProviderDataError("Fontsource license evidence URL is invalid")
    return canonical, FONT_LICENSE_URLS[canonical]


def _parse_weight(value: Any, *, allow_numeric_text: bool = False) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 1 <= value <= 1000 else None
    if allow_numeric_text and isinstance(value, str) and value.isascii() and value.isdigit():
        number = int(value)
        return number if 1 <= number <= 1000 else None
    return None


def _normalize_component(value: Any, *, label: str, maximum: int = 80) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFKC", value)
    if (
        not normalized
        or len(normalized) > maximum
        or not normalized.isascii()
        or _has_control(normalized)
        or normalized != normalized.lower()
        or not _COMPONENT_RE.fullmatch(normalized)
    ):
        return None
    return normalized


def _normalize_style(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFKC", value)
    if (
        not normalized
        or len(normalized) > 32
        or not normalized.isascii()
        or _has_control(normalized)
        or normalized != normalized.lower()
        or not _STYLE_RE.fullmatch(normalized)
    ):
        return None
    return normalized


def _mapping_field(mapping: Mapping[str, Any], *names: str) -> tuple[Any, bool, bool]:
    """Return (value, present, conflict) for case-sensitive aliases."""

    present = [name for name in names if name in mapping]
    if not present:
        return None, False, False
    first = mapping[present[0]]
    return first, True, any(mapping[name] != first for name in present[1:])


def _iter_fontsource_variants(variants: Any) -> Iterator[dict[str, Any]]:
    """Yield explicit variant mappings from list or nested official shapes."""

    if isinstance(variants, list):
        if len(variants) > MAX_FONTSOURCE_VARIANTS:
            raise ProviderDataError("Fontsource variants list is too large")
        for item in variants[:MAX_FONTSOURCE_VARIANTS]:
            if isinstance(item, dict):
                yield item
        return
    if not isinstance(variants, Mapping):
        raise ProviderDataError("Fontsource variants must be a list or mapping")

    def walk(node: Any, context: dict[str, Any], depth: int) -> Iterator[dict[str, Any]]:
        if depth > 6:
            return
        if isinstance(node, list):
            if len(node) > MAX_FONTSOURCE_VARIANTS:
                raise ProviderDataError("Fontsource variants list is too large")
            for item in node[:MAX_FONTSOURCE_VARIANTS]:
                if isinstance(item, dict):
                    merged = dict(context)
                    merged.update(item)
                    yield merged
            return
        if not isinstance(node, Mapping):
            return
        explicit_keys = {
            "subset",
            "weight",
            "style",
            "unicodeRange",
            "unicode_range",
            "url",
            "download_url",
            "downloadUrl",
        }
        if any(key in node for key in explicit_keys):
            merged = dict(context)
            merged.update(node)
            yield merged
            return
        for key, child in node.items():
            if not isinstance(key, str) or len(key) > 120 or _has_control(key):
                continue
            next_context = dict(context)
            if key.isascii() and key.isdigit():
                next_context["weight"] = key
            elif key in _NESTED_STYLE_KEYS:
                next_context["style"] = key
            elif _normalize_component(key, label="subset") is not None:
                next_context["subset"] = key
            else:
                # Compound keys are accepted only in the explicit
                # subset-weight-style form used by generated metadata.
                match = re.fullmatch(r"([a-z0-9]+(?:-[a-z0-9]+)*)-(\d+)-([a-z][a-z0-9-]{0,31})", key)
                if match:
                    next_context.update(
                        {"subset": match.group(1), "weight": match.group(2), "style": match.group(3)}
                    )
                else:
                    continue
            yield from walk(child, next_context, depth + 1)

    yield from walk(variants, {}, 0)


def _fontsource_download_url(
    font_id: str,
    version: str,
    subset: str,
    weight: int,
    style: str,
    supplied: Any = None,
) -> str | None:
    canonical = f"{FONTSOURCE_CDN_ENDPOINT}/{font_id}@{version}/{subset}-{weight}-{style}.ttf"
    if supplied is None:
        return canonical
    # The official nested response stores a URL object containing woff2,
    # woff, and ttf links.  Only the ttf member is eligible; the other
    # formats are deliberately ignored rather than emitted as candidates.
    if isinstance(supplied, Mapping):
        supplied = supplied.get("ttf")
    if not isinstance(supplied, str):
        return None
    validated = _validate_https_url(supplied, hostname="cdn.jsdelivr.net")
    if validated is None:
        return None
    parsed = urlsplit(validated)
    if parsed.netloc != "cdn.jsdelivr.net":
        return None
    expected_path = f"/fontsource/fonts/{font_id}@{version}/{subset}-{weight}-{style}.ttf"
    if parsed.path != expected_path:
        return None
    return canonical


def parse_fontsource(
    payload: Any,
    font_id: str,
    expected_version: str,
) -> list[FontAssetCandidate]:
    """Parse one official Fontsource font-id response without network I/O."""

    font_id = normalize_fontsource_id(font_id)
    expected_version = normalize_fontsource_version(expected_version)
    if not isinstance(payload, dict):
        raise ProviderDataError("Fontsource payload must be an object")
    payload_id = payload.get("id")
    if payload_id != font_id:
        raise ProviderDataError("Fontsource payload id does not match request")
    payload_version = payload.get("version")
    if payload_version != expected_version or not isinstance(payload_version, str):
        raise ProviderDataError("Fontsource payload version does not match request")
    family = _bounded_text(payload.get("family"), FONT_MAX_TEXT, required=True)
    if family is None:
        raise ProviderDataError("Fontsource family is malformed")
    license_spdx, license_url = _parse_fontsource_license(payload, font_id)
    if "variants" not in payload:
        raise ProviderDataError("Fontsource payload has no variants")

    unicode_ranges: dict[str, str] = {}
    if "unicodeRange" in payload:
        raw_unicode_ranges = payload["unicodeRange"]
        if not isinstance(raw_unicode_ranges, Mapping):
            raise ProviderDataError("Fontsource unicodeRange must be a mapping")
        for raw_subset, raw_range in raw_unicode_ranges.items():
            subset = _normalize_component(raw_subset, label="subset")
            value = _bounded_text(raw_range, FONT_MAX_UNICODE_RANGE)
            if subset is None or value is None:
                raise ProviderDataError("Fontsource unicodeRange entry is malformed")
            unicode_ranges[subset] = value

    landing_url = f"{FONTSOURCE_LANDING_ENDPOINT}/{font_id}"
    candidates: list[FontAssetCandidate] = []
    seen: set[tuple[str, int, str]] = set()
    for raw_variant in _iter_fontsource_variants(payload["variants"]):
        subset_value, subset_present, subset_conflict = _mapping_field(raw_variant, "subset")
        weight_value, weight_present, weight_conflict = _mapping_field(raw_variant, "weight")
        style_value, style_present, style_conflict = _mapping_field(raw_variant, "style")
        unicode_value, unicode_present, unicode_conflict = _mapping_field(
            raw_variant, "unicodeRange", "unicode_range"
        )
        url_value, url_present, url_conflict = _mapping_field(
            raw_variant, "url", "download_url", "downloadUrl"
        )
        if (
            subset_conflict
            or weight_conflict
            or style_conflict
            or unicode_conflict
            or url_conflict
            or not subset_present
            or not weight_present
            or not style_present
        ):
            continue
        subset = _normalize_component(subset_value, label="subset")
        weight = _parse_weight(weight_value, allow_numeric_text=True)
        style = _normalize_style(style_value)
        if subset is None or weight is None or style is None:
            continue
        unicode_range = ""
        if unicode_present:
            if not isinstance(unicode_value, str):
                continue
            unicode_range = _bounded_text(unicode_value, FONT_MAX_UNICODE_RANGE)  # type: ignore[assignment]
            if unicode_range is None:
                continue
        elif subset in unicode_ranges:
            unicode_range = unicode_ranges[subset]
        key = (subset, weight, style)
        if key in seen:
            raise ProviderDataError("Fontsource payload contains duplicate candidates")
        seen.add(key)
        supplied_url = url_value if url_present else None
        if url_present and supplied_url is None:
            continue
        download_url = _fontsource_download_url(
            font_id, expected_version, subset, weight, style, supplied_url
        )
        if download_url is None:
            continue
        candidates.append(
            FontAssetCandidate(
                provider_id="fontsource",
                candidate_id=f"{font_id}:{expected_version}:{subset}:{weight}:{style}",
                family=family,
                style=style,
                weight=weight,
                subset=subset,
                unicode_range=unicode_range,
                version=expected_version,
                license_spdx=license_spdx,
                download_url=download_url,
                landing_url=landing_url,
                license_url=license_url,
                license_download_url=(
                    f"{FONTSOURCE_NPM_ENDPOINT}/{font_id}@{expected_version}/LICENSE"
                ),
                mime_type=FONT_MIME,
                attribution_required=True,
            )
        )
    return candidates


parse_fontsource_metadata = parse_fontsource
parse_fontsource_font = parse_fontsource
parse_fontsource_response = parse_fontsource


__all__ = [
    "FONT_LICENSE_CANONICAL_URLS",
    "FONT_LICENSE_URLS",
    "FONT_MIME",
    "FONT_TTF_MIME",
    "FONTSOURCE_API_ENDPOINT",
    "FONTSOURCE_CDN_ENDPOINT",
    "FONTSOURCE_LANDING_ENDPOINT",
    "FONTSOURCE_NPM_ENDPOINT",
    "GOOGLE_FONTS_CONTENTS_ENDPOINT",
    "GOOGLE_FONTS_LANDING_ENDPOINT",
    "GOOGLE_FONTS_RAW_ENDPOINT",
    "GOOGLE_FONTS_REF",
    "GOOGLE_FONTS_REPOSITORY",
    "GOOGLE_FONT_LICENSES",
    "FontAssetCandidate",
    "ProviderDataError",
    "build_fontsource_discovery_url",
    "build_google_fonts_contents_url",
    "fontsource_discovery_url",
    "fontsource_font_url",
    "google_fonts_contents_url",
    "google_fonts_listing_url",
    "normalize_fontsource_id",
    "normalize_fontsource_version",
    "normalize_google_family_id",
    "normalize_query",
    "parse_fontsource",
    "parse_fontsource_font",
    "parse_fontsource_metadata",
    "parse_fontsource_response",
    "parse_google_fonts",
    "parse_google_fonts_contents",
    "parse_google_fonts_listing",
    "parse_google_fonts_repo_listing",
]
