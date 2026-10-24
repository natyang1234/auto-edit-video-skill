"""Allowlist for the github-fonts caption font provider (2026-10-22).

Only fonts listed in contracts/github_fonts.json can be searched or imported: each entry
pins an immutable commit and the Git blob SHA-1 of both the font file and its licence file, so
the provider is a generalisation of google-fonts with the same evidence strength. The JSON's
SHA-256 is pinned here; a manifest that does not hash to it makes the provider unavailable
(fail closed) rather than trusting edited data.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

MANIFEST_PATH = Path(__file__).resolve().parent.parent / "contracts" / "github_fonts.json"
MANIFEST_SHA256 = "7ce60b9a4a2984071316a77560519b246937d3b1aa0f338aa039a8648254dc65"
GITHUB_RAW_ENDPOINT = "https://raw.githubusercontent.com"
GITHUB_LANDING_ENDPOINT = "https://github.com"
GITHUB_API_ENDPOINT = "https://api.github.com"
FONT_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,40}\Z")
_REPO_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
_PATH_RE = re.compile(r"(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:ttf|otf|txt|md)\Z")
_LICENSE_PATH_RE = re.compile(r"(?:[A-Za-z0-9_.-]+/)*(?:OFL\.txt|LICENSE(?:\.txt|\.md)?|license\.txt|COPYING)\Z")
_RELEASE_URL_RE = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]{1,100})/([A-Za-z0-9_.-]{1,100})/releases/download/([A-Za-z0-9_.-]{1,100})/([A-Za-z0-9_.-]{1,120}\.zip)\Z"
)
_MEMBER_RE = re.compile(r"[A-Za-z0-9_.-]{1,120}\.(?:ttf|otf)\Z")
MAX_RELEASE_ZIP_BYTES = 32 * 1024 * 1024
RELEASE_DOWNLOAD_HOSTS = frozenset({"github.com", "release-assets.githubusercontent.com"})

_CACHE: dict[str, Any] | None = None


def _load() -> dict[str, Any]:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    try:
        raw = MANIFEST_PATH.read_bytes()
    except OSError:
        _CACHE = {}
        return _CACHE
    if hashlib.sha256(raw).hexdigest() != MANIFEST_SHA256:
        _CACHE = {}  # edited manifest → provider unavailable
        return _CACHE
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        _CACHE = {}
        return _CACHE
    fonts = data.get("fonts") if isinstance(data, dict) else None
    out: dict[str, Any] = {}
    for font_id, entry in (fonts or {}).items():
        if not isinstance(font_id, str) or not FONT_ID_RE.match(font_id) or not isinstance(entry, dict):
            continue
        try:
            source = entry.get("source")
            if source is not None:
                # release-zip source (2026-10-22): the font only ships inside a GitHub Release asset.
                # Release assets are mutable, so the zip and the single member are pinned by exact
                # size and SHA-256; the licence still comes from the commit-pinned repository tree.
                m = _RELEASE_URL_RE.match(str(source.get("url", "")))
                if not (
                    isinstance(source, dict) and source.get("kind") == "release-zip" and m
                    and m.group(1) == entry["owner"] and m.group(2) == entry["repo"]
                    and isinstance(source.get("zip_size"), int) and 0 < source["zip_size"] <= MAX_RELEASE_ZIP_BYTES
                    and re.fullmatch(r"[0-9a-f]{64}", str(source.get("zip_sha256", "")))
                    and _MEMBER_RE.match(str(source.get("member", ""))) and "/" not in source["member"]
                    and isinstance(source.get("member_size"), int) and 0 < source["member_size"] <= MAX_RELEASE_ZIP_BYTES
                    and re.fullmatch(r"[0-9a-f]{64}", str(source.get("member_sha256", "")))
                    and entry["font_path"] == source["member"]
                ):
                    continue
            if not (
                _REPO_RE.match(entry["owner"]) and _REPO_RE.match(entry["repo"])
                and re.fullmatch(r"[0-9a-f]{40}", entry["commit"])
                and _PATH_RE.match(entry["font_path"]) and entry["font_path"].rsplit(".", 1)[1] in {"ttf", "otf"}
                and _LICENSE_PATH_RE.match(entry["license_path"])
                and re.fullmatch(r"[0-9a-f]{40}", entry["font_blob_sha1"])
                and re.fullmatch(r"[0-9a-f]{40}", entry["license_blob_sha1"])
                and entry["license_spdx"] in {"OFL-1.1", "Apache-2.0", "Ubuntu-font-1.0"}
                and entry["container"] == entry["font_path"].rsplit(".", 1)[1]
                and isinstance(entry["family"], str) and 0 < len(entry["family"]) <= 80
            ):
                continue
        except (KeyError, TypeError, AttributeError):
            continue
        out[font_id] = {"id": font_id, **entry}
    # Identity is keyed by the font blob SHA (candidate_id → entry), so it must be unique; a
    # manifest with two ids sharing a blob is ambiguous and makes the provider unavailable.
    blobs = [e["font_blob_sha1"] for e in out.values()]
    if len(blobs) != len(set(blobs)):
        out = {}
    _CACHE = out
    return _CACHE


def available() -> bool:
    return bool(_load())


def ids() -> list[str]:
    return sorted(_load())


def entry(font_id: Any) -> dict[str, Any] | None:
    if not isinstance(font_id, str):
        return None
    return _load().get(font_id)


def entry_by_font_blob(blob_sha1: Any) -> dict[str, Any] | None:
    for item in _load().values():
        if item["font_blob_sha1"] == blob_sha1:
            return item
    return None


def raw_url(item: dict[str, Any], path: str) -> str:
    return f"{GITHUB_RAW_ENDPOINT}/{item['owner']}/{item['repo']}/{item['commit']}/{path}"


def landing_url(item: dict[str, Any], path: str) -> str:
    return f"{GITHUB_LANDING_ENDPOINT}/{item['owner']}/{item['repo']}/blob/{item['commit']}/{path}"


def is_release(item: dict[str, Any]) -> bool:
    return isinstance(item.get("source"), dict) and item["source"].get("kind") == "release-zip"


def release_tag(item: dict[str, Any]) -> str:
    return _RELEASE_URL_RE.match(item["source"]["url"]).group(3)


def release_api_url(item: dict[str, Any]) -> str:
    """GitHub release object for the pinned tag (asset name/size/url are re-checked against pins)."""
    return f"{GITHUB_API_ENDPOINT}/repos/{item['owner']}/{item['repo']}/releases/tags/{release_tag(item)}"


def font_download_url(item: dict[str, Any]) -> str:
    return item["source"]["url"] if is_release(item) else raw_url(item, item["font_path"])


def font_landing_url(item: dict[str, Any]) -> str:
    if is_release(item):
        return f"{GITHUB_LANDING_ENDPOINT}/{item['owner']}/{item['repo']}/releases/tag/{release_tag(item)}"
    return landing_url(item, item["font_path"])


def discovery_url(item: dict[str, Any]) -> str:
    return release_api_url(item) if is_release(item) else contents_url(item)


def contents_url(item: dict[str, Any]) -> str:
    """GitHub contents object of the pinned font file at the pinned commit (small payload; the
    recursive git tree of some repos exceeds the search byte limit)."""
    return f"{GITHUB_API_ENDPOINT}/repos/{item['owner']}/{item['repo']}/contents/{item['font_path']}?ref={item['commit']}"


def tree_url(item: dict[str, Any]) -> str:  # kept for callers that want the whole tree
    return f"{GITHUB_API_ENDPOINT}/repos/{item['owner']}/{item['repo']}/git/trees/{item['commit']}?recursive=1"


def git_blob_sha1(raw: bytes) -> str:
    return hashlib.sha1(f"blob {len(raw)}\0".encode("ascii") + raw, usedforsecurity=False).hexdigest()


__all__ = [
    "GITHUB_API_ENDPOINT", "GITHUB_LANDING_ENDPOINT", "GITHUB_RAW_ENDPOINT", "MANIFEST_PATH", "MANIFEST_SHA256",
    "MAX_RELEASE_ZIP_BYTES", "RELEASE_DOWNLOAD_HOSTS", "available", "contents_url", "discovery_url", "entry", "font_download_url", "font_landing_url", "is_release", "release_api_url", "release_tag", "entry_by_font_blob", "git_blob_sha1", "ids", "landing_url", "raw_url", "tree_url",
]
