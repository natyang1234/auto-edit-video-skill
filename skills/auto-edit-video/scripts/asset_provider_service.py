#!/usr/bin/env python3
"""Project-private orchestration for consented open image providers.

This module owns consent persistence, hardened provider JSON downloads, opaque
single-use import capabilities, image validation, and provenance publication.
It performs no filesystem or network I/O until a public method is called.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import struct
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asset_registry
from hardened_downloader import (
    DownloadError,
    ValidationError as DownloadValidationError,
    download_https,
    system_resolver,
)
from open_asset_providers import (
    OpenAssetCandidate,
    ProviderDataError,
    normalize_query,
    openverse_search_url,
    parse_openverse,
    parse_wikimedia,
    wikimedia_search_url,
)
from open_svg_providers import (
    SVG_MIME,
    SvgAssetCandidate,
    heroicons_candidate,
    lucide_candidate,
    parse_wikimedia_svg,
    tabler_candidate,
    wikimedia_svg_search_url,
)


CONSENT_REL = Path("working/provider_consents.json")
SEARCH_CACHE_REL = Path("working/provider-cache")
SEARCH_MAX_BYTES = 1024 * 1024
ASSET_MAX_BYTES = 25 * 1024 * 1024
SVG_MAX_BYTES = 2 * 1024 * 1024
MAX_DIMENSION = 8192
MAX_PIXELS = 32_000_000
MAX_IMPORT_TOKENS = 200
MAX_SEARCH_METADATA_FILES = 200
_SVG_PREFLIGHT_FIELDS = frozenset({"available", "checks_ok", "code", "identity"})
_SVG_RASTERIZER_IDENTITY_FIELDS = frozenset(
    {
        "version",
        "executable_sha256",
        "sandbox_executable_sha256",
        "sandbox_profile_sha256",
    }
)
_SVG_RASTER_RESULT_FIELDS = frozenset(
    {"png_bytes", "png_sha256", "width", "height", "metadata"}
)
_LOWER_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")

_PROVIDERS: tuple[dict[str, Any], ...] = (
    {
        "id": "openverse",
        "label": "Openverse",
        "kind": "image",
        "consent_required": True,
        "cost_class": "free",
        "network_disclosure": "會將最多 6 個搜尋詞傳送至 Openverse。",
    },
    {
        "id": "wikimedia",
        "label": "Wikimedia Commons",
        "kind": "image",
        "consent_required": True,
        "cost_class": "free",
        "network_disclosure": "會將最多 6 個搜尋詞傳送至 Wikimedia Commons。",
    },
    {
        "id": "heroicons",
        "label": "Heroicons",
        "kind": "svg",
        "consent_required": True,
        "cost_class": "free",
        "network_disclosure": "會從固定的 Heroicons GitHub pinned revision 下載指定圖示。",
    },
    {
        "id": "lucide",
        "label": "Lucide",
        "kind": "svg",
        "consent_required": True,
        "cost_class": "free",
        "network_disclosure": "會從固定的 Lucide GitHub pinned revision 下載指定圖示。",
    },
    {
        "id": "tabler",
        "label": "Tabler Icons",
        "kind": "svg",
        "consent_required": True,
        "cost_class": "free",
        "network_disclosure": "會從固定的 Tabler Icons GitHub pinned revision 下載指定圖示。",
    },
    {
        "id": "wikimedia-svg",
        "label": "Wikimedia Commons SVG",
        "kind": "svg",
        "consent_required": True,
        "cost_class": "free",
        "network_disclosure": "會將最多 6 個搜尋詞傳送至 Wikimedia Commons，僅搜尋 SVG 檔案。",
    },
)
_PROVIDER_BY_ID = {item["id"]: item for item in _PROVIDERS}
_SEARCH_HOSTS = {
    "openverse": frozenset({"api.openverse.org"}),
    "wikimedia": frozenset({"commons.wikimedia.org"}),
    "wikimedia-svg": frozenset({"commons.wikimedia.org"}),
}
_IMPORT_HOSTS = {
    "openverse": frozenset({"api.openverse.org"}),
    "wikimedia": frozenset({"upload.wikimedia.org"}),
    "heroicons": frozenset({"raw.githubusercontent.com"}),
    "lucide": frozenset({"raw.githubusercontent.com"}),
    "tabler": frozenset({"raw.githubusercontent.com"}),
    "wikimedia-svg": frozenset({"upload.wikimedia.org"}),
}
_MIME_EXTENSION = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
_SAFE_ID = re.compile(r"[^A-Za-z0-9_-]+")


class AssetProviderError(RuntimeError):
    """A browser-safe provider failure with an explicit HTTP status mapping."""

    def __init__(self, message: str, *, status_code: int, code: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True)
class _ImportGrant:
    candidate: OpenAssetCandidate | SvgAssetCandidate
    provider_id: str
    query_hash: str
    expires_at: float


@dataclass(frozen=True)
class _ValidatedSvgRasterResult:
    png_bytes: bytes
    png_sha256: str
    width: int
    height: int
    identity: dict[str, str]


Downloader = Callable[..., Any]
Resolver = Callable[[str, int], Sequence[str]]
VisualValidator = Callable[[Path], bool]
SvgPipeline = Any
Clock = Callable[[], float]


def _error(message: str, status: int, code: str) -> AssetProviderError:
    return AssetProviderError(message, status_code=status, code=code)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _valid_utc_z(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return True


def _valid_confirmer(value: Any) -> bool:
    return bool(
        isinstance(value, str)
        and value
        and value == value.strip()
        and len(value) <= 120
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def _reject_constant(value: str) -> None:
    del value
    raise ValueError("non-finite JSON number")


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_nonfinite_numbers(value: Any) -> None:
    """Reject exponent overflows that ``parse_constant`` does not observe."""
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite JSON number")
    if isinstance(value, dict):
        for nested in value.values():
            _reject_nonfinite_numbers(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_nonfinite_numbers(nested)


def _strict_json_bytes(payload: bytes) -> Any:
    try:
        text = payload.decode("utf-8", errors="strict")
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
        _reject_nonfinite_numbers(parsed)
        return parsed
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("invalid strict UTF-8 JSON") from exc


def _json_bytes(payload: Any) -> bytes:
    try:
        return (
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise _error("provider metadata could not be serialized", 409, "storage_conflict") from exc


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    if not data.startswith(b"\xff\xd8"):
        return None
    index = 2
    sof_markers = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    while index < len(data):
        while index < len(data) and data[index] == 0xFF:
            index += 1
        if index >= len(data):
            return None
        marker = data[index]
        index += 1
        if marker in {0xD8, 0xD9}:
            continue
        if marker == 0xDA:
            return None
        if index + 2 > len(data):
            return None
        length = int.from_bytes(data[index : index + 2], "big")
        if length < 2 or index + length > len(data):
            return None
        if marker in sof_markers:
            if length < 7:
                return None
            height = int.from_bytes(data[index + 3 : index + 5], "big")
            width = int.from_bytes(data[index + 5 : index + 7], "big")
            return (width, height) if width > 0 and height > 0 else None
        index += length
    return None


def _image_dimensions(path: Path, expected_mime: str) -> tuple[int, int]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ValueError("image could not be read") from exc
    dimensions: tuple[int, int] | None = None
    if expected_mime == "image/png":
        if (
            len(data) >= 24
            and data.startswith(b"\x89PNG\r\n\x1a\n")
            and data[12:16] == b"IHDR"
        ):
            dimensions = struct.unpack(">II", data[16:24])
    elif expected_mime == "image/jpeg":
        dimensions = _jpeg_dimensions(data)
    elif expected_mime == "image/webp":
        if len(data) >= 30 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            fourcc = data[12:16]
            if fourcc == b"VP8X":
                dimensions = (
                    1 + int.from_bytes(data[24:27], "little"),
                    1 + int.from_bytes(data[27:30], "little"),
                )
            elif fourcc == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
                dimensions = (
                    int.from_bytes(data[26:28], "little") & 0x3FFF,
                    int.from_bytes(data[28:30], "little") & 0x3FFF,
                )
            elif fourcc == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
                packed = int.from_bytes(data[21:25], "little")
                dimensions = (1 + (packed & 0x3FFF), 1 + ((packed >> 14) & 0x3FFF))
    if dimensions is None or dimensions[0] <= 0 or dimensions[1] <= 0:
        raise ValueError("image MIME magic or dimensions are invalid")
    return dimensions


def _safe_candidate_id(candidate_id: str) -> str:
    safe = _SAFE_ID.sub("-", candidate_id).strip("-")[:100]
    if not safe:
        raise _error("provider candidate identity is invalid", 422, "provider_data_invalid")
    return safe


class AssetProviderService:
    """Thread-safe provider service scoped to exactly one project directory."""

    def __init__(
        self,
        project_dir: str | os.PathLike[str],
        *,
        downloader: Downloader = download_https,
        resolver: Resolver = system_resolver,
        transport: Any = None,
        svg_pipeline: SvgPipeline | None = None,
        clock: Clock = time.monotonic,
        token_ttl_s: float = 1800,
    ) -> None:
        if not callable(downloader) or not callable(resolver) or not callable(clock):
            raise TypeError("downloader, resolver, and clock must be callable")
        if (
            isinstance(token_ttl_s, bool)
            or not isinstance(token_ttl_s, (int, float))
            or not math.isfinite(token_ttl_s)
            or token_ttl_s <= 0
        ):
            raise ValueError("token_ttl_s must be a finite positive number")
        self._project_dir = Path(project_dir)
        self._downloader = downloader
        self._resolver = resolver
        self._transport = transport
        # The production pipeline is deliberately unavailable unless the
        # hardened rasterizer can prove its pinned executable/sandbox setup.
        # Tests must inject an explicit pipeline; an absent dependency never
        # silently turns SVG network access on.
        if svg_pipeline is None:
            try:
                from svg_security import ResvgRasterizer  # type: ignore[import-not-found]

                svg_pipeline = ResvgRasterizer(None)
            except ImportError:
                svg_pipeline = None
        self._svg_pipeline = svg_pipeline
        self._svg_preflight_identity = self._validate_svg_preflight(
            self._probe_svg_pipeline(svg_pipeline)
        )
        self._clock = clock
        self._token_ttl_s = float(token_ttl_s)
        self._tokens: OrderedDict[str, _ImportGrant] = OrderedDict()
        self._lock = threading.RLock()

    def _root(self) -> Path:
        try:
            root = self._project_dir.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise _error("project directory is unavailable", 409, "storage_conflict") from exc
        if not root.is_dir():
            raise _error("project directory is unavailable", 409, "storage_conflict")
        return root

    def _safe_directory(self, root: Path, relative: Path) -> Path:
        current = root
        for part in relative.parts:
            current = current / part
            try:
                if current.is_symlink():
                    raise _error("project storage path is unsafe", 409, "storage_conflict")
                current.mkdir(exist_ok=True)
            except AssetProviderError:
                raise
            except OSError as exc:
                raise _error("project storage is unavailable", 409, "storage_conflict") from exc
            if not current.is_dir() or current.is_symlink():
                raise _error("project storage path is unsafe", 409, "storage_conflict")
        return current

    def _atomic_write(self, root: Path, relative: Path, payload: bytes) -> Path:
        parent = self._safe_directory(root, relative.parent)
        target = parent / relative.name
        if target.is_symlink():
            raise _error("project storage target is unsafe", 409, "storage_conflict")
        temporary = parent / f".{target.name}.{uuid.uuid4().hex}.part"
        try:
            with temporary.open("xb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if target.is_symlink():
                raise _error("project storage target is unsafe", 409, "storage_conflict")
            os.replace(temporary, target)
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except AssetProviderError:
            raise
        except OSError as exc:
            raise _error("project storage write failed", 409, "storage_conflict") from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        return target

    def _load_consents(self, root: Path) -> dict[str, dict[str, Any]]:
        current = root
        for part in CONSENT_REL.parent.parts:
            current = current / part
            if current.is_symlink():
                raise _error("provider consent storage is unsafe", 409, "storage_conflict")
            if not current.exists():
                break
        path = root / CONSENT_REL
        if path.is_symlink():
            raise _error("provider consent storage is unsafe", 409, "storage_conflict")
        if not path.exists():
            return {}
        if not path.is_file():
            raise _error("provider consent storage is invalid", 409, "storage_conflict")
        try:
            raw = path.read_bytes()
            artifact = _strict_json_bytes(raw)
        except (OSError, ValueError) as exc:
            raise _error("provider consent storage is invalid", 409, "storage_conflict") from exc
        if not isinstance(artifact, dict) or set(artifact) != {"schema_version", "items"}:
            raise _error("provider consent storage is invalid", 409, "storage_conflict")
        if artifact.get("schema_version") != 1 or not isinstance(artifact.get("items"), list):
            raise _error("provider consent storage is invalid", 409, "storage_conflict")
        consents: dict[str, dict[str, Any]] = {}
        for item in artifact["items"]:
            if not isinstance(item, dict) or set(item) != {
                "provider_id",
                "kind",
                "consented",
                "consented_at",
                "confirmed_by",
            }:
                raise _error("provider consent storage is invalid", 409, "storage_conflict")
            provider_id = item.get("provider_id")
            if (
                not isinstance(provider_id, str)
                or provider_id not in _PROVIDER_BY_ID
                or item.get("kind") != _PROVIDER_BY_ID[provider_id]["kind"]
                or not isinstance(item.get("consented"), bool)
                or not _valid_utc_z(item.get("consented_at"))
                or not _valid_confirmer(item.get("confirmed_by"))
                or provider_id in consents
            ):
                raise _error("provider consent storage is invalid", 409, "storage_conflict")
            consents[provider_id] = dict(item)
        return consents

    def _write_consents(self, root: Path, consents: Mapping[str, dict[str, Any]]) -> None:
        artifact = {
            "schema_version": 1,
            "items": [consents[key] for key in sorted(consents)],
        }
        self._atomic_write(root, CONSENT_REL, _json_bytes(artifact))

    def _prune_search_metadata(self, cache_dir: Path) -> None:
        try:
            entries = sorted(
                cache_dir.glob("*.meta.json"),
                key=lambda path: (path.lstat().st_mtime_ns, path.name),
            )
            for stale in entries[:-MAX_SEARCH_METADATA_FILES]:
                stale.unlink()
        except OSError as exc:
            raise _error(
                "provider metadata retention failed", 409, "storage_conflict"
            ) from exc

    def _provider(self, provider_id: str) -> dict[str, Any]:
        if not isinstance(provider_id, str) or provider_id not in _PROVIDER_BY_ID:
            raise _error("provider was not found", 404, "provider_not_found")
        return _PROVIDER_BY_ID[provider_id]

    def _clean_expired_tokens(self) -> None:
        now = self._clock()
        expired = [token for token, grant in self._tokens.items() if grant.expires_at <= now]
        for token in expired:
            self._tokens.pop(token, None)

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._clean_expired_tokens()
            root = self._root()
            consents = self._load_consents(root)
            providers = []
            for catalog_item in _PROVIDERS:
                consent = consents.get(catalog_item["id"])
                available = True
                availability_code = "available"
                if catalog_item["kind"] == "svg":
                    available, availability_code = self._svg_available()
                providers.append(
                    {
                        **catalog_item,
                        "available": available,
                        "availability_code": availability_code,
                        "consented": bool(consent and consent["consented"]),
                        "consented_at": consent.get("consented_at") if consent else None,
                        "confirmed_by": consent.get("confirmed_by") if consent else None,
                    }
                )
            return {"providers": providers}

    @staticmethod
    def _probe_svg_pipeline(pipeline: SvgPipeline | None) -> Any:
        """Probe once per service instance; status/GET paths only read this cache."""
        if pipeline is None:
            return None
        try:
            return pipeline.preflight()
        except Exception:
            return None

    @staticmethod
    def _validate_svg_preflight(preflight: Any) -> dict[str, str] | None:
        """Accept only the exact successful result emitted by the hardened rasterizer."""
        if preflight is None or isinstance(preflight, Mapping):
            return None
        try:
            fields = vars(preflight)
        except TypeError:
            return None
        if type(fields) is not dict or set(fields) != _SVG_PREFLIGHT_FIELDS:
            return None
        if fields["available"] is not True or fields["checks_ok"] is not True:
            return None
        if type(fields["code"]) is not str or fields["code"] != "OK":
            return None
        return AssetProviderService._validate_svg_rasterizer_identity(fields["identity"])

    @staticmethod
    def _validate_svg_rasterizer_identity(identity: Any) -> dict[str, str] | None:
        """Return a defensive copy of an exact, well-formed rasterizer identity."""
        if type(identity) is not dict or set(identity) != _SVG_RASTERIZER_IDENTITY_FIELDS:
            return None
        version = identity["version"]
        if (
            type(version) is not str
            or not 1 <= len(version) <= 200
            or _CONTROL_RE.search(version) is not None
        ):
            return None
        for field in _SVG_RASTERIZER_IDENTITY_FIELDS - {"version"}:
            value = identity[field]
            if type(value) is not str or _LOWER_SHA256_RE.fullmatch(value) is None:
                return None
        return dict(identity)

    @staticmethod
    def _validate_svg_raster_result(result: Any) -> _ValidatedSvgRasterResult | None:
        """Snapshot an exact raster result without invoking result properties."""
        if result is None or isinstance(result, Mapping):
            return None
        try:
            fields = vars(result)
        except Exception:
            return None
        if type(fields) is not dict or set(fields) != _SVG_RASTER_RESULT_FIELDS:
            return None
        png_bytes = fields["png_bytes"]
        png_sha256 = fields["png_sha256"]
        width = fields["width"]
        height = fields["height"]
        if type(png_bytes) is not bytes:
            return None
        if type(png_sha256) is not str or _LOWER_SHA256_RE.fullmatch(png_sha256) is None:
            return None
        if (
            type(width) is not int
            or type(height) is not int
            or width <= 0
            or height <= 0
            or width > MAX_DIMENSION
            or height > MAX_DIMENSION
            or width * height > MAX_PIXELS
        ):
            return None
        identity = AssetProviderService._validate_svg_rasterizer_identity(fields["metadata"])
        if identity is None:
            return None
        return _ValidatedSvgRasterResult(
            png_bytes=png_bytes,
            png_sha256=png_sha256,
            width=width,
            height=height,
            identity=identity,
        )

    def _svg_available(self) -> tuple[bool, str]:
        if self._svg_preflight_identity is not None:
            return True, "available"
        return False, "svg_rasterizer_unavailable"

    def _require_svg_available(self) -> None:
        available, _code = self._svg_available()
        if not available:
            raise _error("SVG rasterizer is unavailable", 503, "svg_rasterizer_unavailable")

    def set_consent(
        self, provider_id: str, consented: bool, confirmed_by: str
    ) -> dict[str, Any]:
        with self._lock:
            self._clean_expired_tokens()
            provider = self._provider(provider_id)
            if not isinstance(consented, bool):
                raise _error("consented must be a boolean", 400, "malformed_request")
            if not _valid_confirmer(confirmed_by):
                raise _error("confirmed_by is invalid", 400, "malformed_request")
            root = self._root()
            consents = self._load_consents(root)
            item = {
                "provider_id": provider["id"],
                "kind": provider["kind"],
                "consented": consented,
                "consented_at": _utc_now(),
                "confirmed_by": confirmed_by,
            }
            consents[provider_id] = item
            self._write_consents(root, consents)
            return dict(item)

    def grant_consent(self, provider_id: str, confirmed_by: str) -> dict[str, Any]:
        return self.set_consent(provider_id, True, confirmed_by)

    def revoke_consent(self, provider_id: str, confirmed_by: str) -> dict[str, Any]:
        return self.set_consent(provider_id, False, confirmed_by)

    def _require_consent(self, root: Path, provider_id: str) -> None:
        self._provider(provider_id)
        consent = self._load_consents(root).get(provider_id)
        if not consent or consent.get("consented") is not True:
            raise _error("provider consent is required", 403, "consent_required")

    def _parse_provider_payload(
        self, provider_id: str, path: Path
    ) -> list[OpenAssetCandidate]:
        try:
            payload = path.read_bytes()
            if len(payload) > SEARCH_MAX_BYTES:
                raise ValueError("provider JSON exceeds limit")
            parsed = _strict_json_bytes(payload)
            candidates = (
                parse_openverse(parsed)
                if provider_id == "openverse"
                else parse_wikimedia(parsed)
            )
        except (OSError, ValueError, ProviderDataError) as exc:
            raise _error("provider returned invalid data", 422, "provider_data_invalid") from exc
        return [
            candidate
            for candidate in candidates[:20]
            if candidate.width <= MAX_DIMENSION
            and candidate.height <= MAX_DIMENSION
            and candidate.width * candidate.height <= MAX_PIXELS
        ]

    def _parse_wikimedia_svg_payload(self, path: Path) -> list[SvgAssetCandidate]:
        try:
            payload = path.read_bytes()
            if len(payload) > SEARCH_MAX_BYTES:
                raise ValueError("provider JSON exceeds limit")
            parsed = _strict_json_bytes(payload)
            return parse_wikimedia_svg(parsed)
        except (OSError, ValueError, ProviderDataError) as exc:
            raise _error("provider returned invalid data", 422, "provider_data_invalid") from exc

    def _repo_svg_candidate(self, provider_id: str, query: str) -> SvgAssetCandidate:
        builders = {
            "heroicons": heroicons_candidate,
            "lucide": lucide_candidate,
            "tabler": tabler_candidate,
        }
        try:
            return builders[provider_id](query)
        except (KeyError, ProviderDataError) as exc:
            raise _error("search query violates provider policy", 422, "policy_rejected") from exc

    def _download(
        self,
        url: str,
        destination: Path,
        root: Path,
        *,
        allowed_hosts: frozenset[str],
        max_bytes: int,
        validator: Callable[[Path], None],
    ) -> Any:
        kwargs: dict[str, Any] = {
            "project_dir": root,
            "allowed_hosts": allowed_hosts,
            "max_bytes": max_bytes,
            "validator": validator,
            "resolver": self._resolver,
            "clock": self._clock,
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return self._downloader(url, destination, **kwargs)

    def search(self, provider_id: str, query: str, page: int = 1) -> dict[str, Any]:
        with self._lock:
            self._clean_expired_tokens()
            provider = self._provider(provider_id)
            root = self._root()
            self._require_consent(root, provider_id)
            if provider["kind"] == "svg":
                # This must precede query normalization, cache creation, and
                # downloader construction.  SVG ingestion has no degraded
                # raw-SVG mode.
                self._require_svg_available()
            if isinstance(page, bool) or not isinstance(page, int):
                raise _error("page must be an integer", 400, "malformed_request")
            if provider_id in {"heroicons", "lucide", "tabler"}:
                candidate = self._repo_svg_candidate(provider_id, query)
                query_hash = hashlib.sha256(candidate.candidate_id.encode("utf-8")).hexdigest()
                now = self._clock()
                token = secrets.token_urlsafe(32)
                self._tokens[token] = _ImportGrant(
                    candidate=candidate,
                    provider_id=provider_id,
                    query_hash=query_hash,
                    expires_at=now + self._token_ttl_s,
                )
                while len(self._tokens) > MAX_IMPORT_TOKENS:
                    self._tokens.popitem(last=False)
                # Exact-slug repository adapters never make a search request
                # and retain no plaintext query metadata on disk.
                return {
                    "provider_id": provider_id,
                    "page": page,
                    "items": [{**candidate.public_dict(), "import_token": token}],
                }
            try:
                normalized_query = normalize_query(query)
                search_url = (
                    openverse_search_url(normalized_query, page=page, page_size=20)
                    if provider_id == "openverse"
                    else (
                        wikimedia_svg_search_url(normalized_query, page=page, page_size=20)
                        if provider_id == "wikimedia-svg"
                        else wikimedia_search_url(normalized_query, page=page, page_size=20)
                    )
                )
            except ProviderDataError as exc:
                raise _error("search query violates provider policy", 422, "policy_rejected") from exc

            cache_dir = self._safe_directory(root, SEARCH_CACHE_REL)
            cache_id = secrets.token_hex(16)
            raw_path = cache_dir / f"search-{provider_id}-{cache_id}.json"

            def validate_json(path: Path) -> None:
                if provider_id == "wikimedia-svg":
                    self._parse_wikimedia_svg_payload(path)
                else:
                    self._parse_provider_payload(provider_id, path)

            try:
                self._download(
                    search_url,
                    raw_path,
                    root,
                    allowed_hosts=_SEARCH_HOSTS[provider_id],
                    max_bytes=SEARCH_MAX_BYTES,
                    validator=validate_json,
                )
                if not raw_path.is_file() or raw_path.is_symlink():
                    raise _error("provider download did not produce data", 502, "provider_failure")
                candidates = (
                    self._parse_wikimedia_svg_payload(raw_path)
                    if provider_id == "wikimedia-svg"
                    else self._parse_provider_payload(provider_id, raw_path)
                )
            except AssetProviderError:
                raw_path.unlink(missing_ok=True)
                raise
            except DownloadValidationError as exc:
                raw_path.unlink(missing_ok=True)
                raise _error("provider returned invalid data", 422, "provider_data_invalid") from exc
            except DownloadError as exc:
                raw_path.unlink(missing_ok=True)
                raise _error("provider request failed", 502, "provider_failure") from exc
            except Exception as exc:
                raw_path.unlink(missing_ok=True)
                raise _error("provider request failed", 502, "provider_failure") from exc

            # Provider bytes are hostile transport material, not an audit
            # artifact.  Persist only the canonical metadata below so provider
            # echoes cannot retain plaintext queries or grow an unbounded cache.
            try:
                raw_path.unlink()
            except OSError as exc:
                raise _error(
                    "provider cache cleanup failed", 409, "storage_conflict"
                ) from exc

            query_hash = hashlib.sha256(normalized_query.encode("utf-8")).hexdigest()
            metadata = {
                "schema_version": 1,
                "provider_id": provider_id,
                "kind": provider["kind"],
                "query_hash": query_hash,
                "page": page,
                "raw_file": raw_path.name,
                "candidate_count": len(candidates),
                "created_at": _utc_now(),
            }
            self._atomic_write(
                root,
                SEARCH_CACHE_REL / f"search-{provider_id}-{cache_id}.meta.json",
                _json_bytes(metadata),
            )
            self._prune_search_metadata(cache_dir)
            public_items = []
            now = self._clock()
            for candidate in candidates:
                token = secrets.token_urlsafe(32)
                self._tokens[token] = _ImportGrant(
                    candidate=candidate,
                    provider_id=provider_id,
                    query_hash=query_hash,
                    expires_at=now + self._token_ttl_s,
                )
                public_items.append({**candidate.public_dict(), "import_token": token})
            while len(self._tokens) > MAX_IMPORT_TOKENS:
                self._tokens.popitem(last=False)
            return {"provider_id": provider_id, "page": page, "items": public_items}

    def import_candidate(
        self, import_token: str, visual_validator: VisualValidator
    ) -> dict[str, Any]:
        with self._lock:
            self._clean_expired_tokens()
            if not isinstance(import_token, str) or not import_token:
                raise _error("import token was not found", 404, "import_token_not_found")
            grant = self._tokens.pop(import_token, None)
            if grant is None:
                raise _error("import token was not found", 404, "import_token_not_found")
            candidate = grant.candidate
            provider_id = grant.provider_id
            if (
                candidate.provider_id != provider_id
                or provider_id not in _PROVIDER_BY_ID
                or (
                    candidate.mime_type not in _MIME_EXTENSION
                    and candidate.mime_type != SVG_MIME
                )
            ):
                raise _error("import token was not found", 404, "import_token_not_found")
            if not callable(visual_validator):
                raise _error("visual validator is required", 400, "malformed_request")
            if (
                candidate.width > MAX_DIMENSION
                or candidate.height > MAX_DIMENSION
                or candidate.width * candidate.height > MAX_PIXELS
            ):
                raise _error("provider image dimensions exceed policy", 422, "policy_rejected")

            root = self._root()
            self._require_consent(root, provider_id)
            if isinstance(candidate, SvgAssetCandidate):
                self._require_svg_available()
                return self._import_svg_candidate(root, provider_id, candidate, grant.query_hash)
            safe_id = _safe_candidate_id(candidate.candidate_id)
            extension = _MIME_EXTENSION[candidate.mime_type]
            relative = Path("assets/providers") / provider_id / f"{safe_id}{extension}"
            parent = self._safe_directory(root, relative.parent)
            destination = parent / relative.name
            if destination.is_symlink():
                raise _error("provider asset path is unsafe", 409, "registry_conflict")
            asset_id = f"provider-{provider_id}-{safe_id}"

            try:
                registry_before = asset_registry.load_registry(root)
            except asset_registry.AssetRegistryError as exc:
                raise _error("asset registry is invalid", 409, "registry_conflict") from exc
            matching_item = next(
                (
                    item
                    for item in registry_before["items"]
                    if item.get("asset_id") == asset_id and item.get("path") == relative.as_posix()
                ),
                None,
            )
            if destination.exists():
                if not destination.is_file() or destination.is_symlink():
                    raise _error("provider asset path is unsafe", 409, "registry_conflict")
                current_hash = _hash_file(destination)
                if (
                    matching_item is not None
                    and matching_item.get("sha256") == current_hash
                    and not asset_registry.provider_consistency_errors(root, matching_item)
                ):
                    return {
                        "item": matching_item,
                        "source": relative.as_posix(),
                        "url": f"/{relative.as_posix()}",
                        "idempotent": True,
                    }
                raise _error("provider asset conflicts with existing state", 409, "registry_conflict")

            def validate_image(path: Path) -> None:
                width, height = _image_dimensions(path, candidate.mime_type)
                if width > MAX_DIMENSION or height > MAX_DIMENSION or width * height > MAX_PIXELS:
                    raise ValueError("decoded dimensions exceed policy")
                if visual_validator(path) is not True:
                    raise ValueError("visual decoder rejected image")

            try:
                self._download(
                    candidate.download_url,
                    destination,
                    root,
                    allowed_hosts=_IMPORT_HOSTS[provider_id],
                    max_bytes=ASSET_MAX_BYTES,
                    validator=validate_image,
                )
                if not destination.is_file() or destination.is_symlink():
                    raise _error("provider download did not produce an asset", 502, "provider_failure")
                digest = _hash_file(destination)
            except AssetProviderError:
                destination.unlink(missing_ok=True)
                raise
            except DownloadValidationError as exc:
                destination.unlink(missing_ok=True)
                raise _error("provider image failed validation", 422, "policy_rejected") from exc
            except DownloadError as exc:
                destination.unlink(missing_ok=True)
                raise _error("provider asset download failed", 502, "provider_failure") from exc
            except Exception as exc:
                destination.unlink(missing_ok=True)
                raise _error("provider asset download failed", 502, "provider_failure") from exc

            item = {
                "asset_id": asset_id,
                "path": relative.as_posix(),
                "sha256": digest,
                "origin": "provider",
                "provider_id": provider_id,
                "source_url": candidate.landing_url,
                "license": {
                    "spdx": candidate.license_spdx,
                    "evidence_url": candidate.license_url,
                    "attribution_required": candidate.attribution_required,
                    "attribution_text": candidate.attribution_text,
                    "verified_at": _utc_now(),
                },
                "review_status": "approved",
            }
            registry_path = root / asset_registry.PROVENANCE_REL
            attribution_path = root / asset_registry.ATTRIBUTION_REL
            registry_existed = registry_path.is_file() and not registry_path.is_symlink()
            attribution_before: bytes | None = None
            if attribution_path.is_file() and not attribution_path.is_symlink():
                try:
                    attribution_before = attribution_path.read_bytes()
                except OSError:
                    attribution_before = None
            receipt_created = False
            try:
                asset_registry.save_provider_receipt(
                    root,
                    item,
                    candidate_id=candidate.candidate_id,
                    download_url=candidate.download_url,
                )
                receipt_created = True
                asset_registry.upsert_item(root, item)
            except asset_registry.AssetRegistryError as exc:
                destination.unlink(missing_ok=True)
                receipt_path = (
                    root
                    / asset_registry.PROVIDER_RECEIPTS_REL
                    / (hashlib.sha256(asset_id.encode("utf-8")).hexdigest() + ".json")
                )
                if receipt_created and not receipt_path.is_symlink():
                    receipt_path.unlink(missing_ok=True)
                try:
                    try:
                        registry_after = asset_registry.load_registry(root)
                    except asset_registry.AssetRegistryError:
                        registry_after = None
                    if registry_after != registry_before:
                        if registry_existed:
                            asset_registry.save_registry(root, registry_before)
                        elif not registry_path.is_symlink():
                            registry_path.unlink(missing_ok=True)
                        if attribution_before is not None:
                            self._atomic_write(
                                root,
                                asset_registry.ATTRIBUTION_REL,
                                attribution_before,
                            )
                        elif not attribution_path.is_symlink():
                            attribution_path.unlink(missing_ok=True)
                except (AssetProviderError, asset_registry.AssetRegistryError, OSError):
                    pass
                if "rollback failed" in str(exc):
                    raise _error(
                        "asset registry rollback failed",
                        409,
                        "registry_rollback_failed",
                    ) from exc
                raise _error("asset registry update failed", 409, "registry_conflict") from exc
            return {
                "item": item,
                "source": relative.as_posix(),
                "url": f"/{relative.as_posix()}",
                "idempotent": False,
            }

    def _import_svg_candidate(
        self, root: Path, provider_id: str, candidate: SvgAssetCandidate, query_hash: str
    ) -> dict[str, Any]:
        """Download hostile SVG privately, sanitize/rasterize, then publish PNG only."""
        safe_id = _safe_candidate_id(candidate.candidate_id)
        try:
            current = asset_registry.current_svg_provider_item(
                root,
                provider_id=provider_id,
                candidate_id=candidate.candidate_id,
                query_hash=query_hash,
                download_url=candidate.download_url,
            )
        except asset_registry.AssetRegistryError as exc:
            raise _error("asset registry is invalid", 409, "registry_conflict") from exc
        if current is not None:
            return {
                "item": current,
                "source": current["path"],
                "url": f"/{current['path']}",
                "idempotent": True,
            }

        staging_relative = Path("working/source_artifacts/svg") / f".{uuid.uuid4().hex}.svg.part"
        staging = self._safe_directory(root, staging_relative.parent) / staging_relative.name
        created_paths: list[Path] = []
        publication_snapshot: dict[str, bytes | None] | None = None

        def rollback() -> None:
            errors: list[str] = []
            for path in reversed(created_paths):
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    errors.append(str(exc))
            if publication_snapshot is not None:
                try:
                    asset_registry.restore_publication(root, publication_snapshot)
                except asset_registry.AssetRegistryError as exc:
                    errors.append(str(exc))
            if errors:
                raise _error(
                    "SVG import rollback failed", 409, "registry_rollback_failed"
                )

        try:
            self._download(
                candidate.download_url,
                staging,
                root,
                allowed_hosts=_IMPORT_HOSTS[provider_id],
                max_bytes=SVG_MAX_BYTES,
                validator=lambda path: None,
            )
            if not staging.is_file() or staging.is_symlink():
                raise _error("provider download did not produce an SVG", 502, "provider_failure")
            raw = staging.read_bytes()
            raw_sha = hashlib.sha256(raw).hexdigest()
            from svg_security import (
                SvgSecurityError,
                sanitize_and_rasterize,
                validate_png_bytes,
            )

            try:
                sanitized, png = sanitize_and_rasterize(
                    raw,
                    requested_width=candidate.width,
                    requested_height=candidate.height,
                    rasterizer=self._svg_pipeline,
                )
            except SvgSecurityError as exc:
                if exc.code.startswith(("SVG_", "PNG_")):
                    raise _error("SVG failed security policy", 422, "policy_rejected") from exc
                raise _error(
                    "SVG rasterizer is unavailable", 503, "svg_rasterizer_unavailable"
                ) from exc
            except Exception as exc:
                raise _error(
                    "SVG rasterizer returned an invalid result",
                    503,
                    "svg_rasterizer_unavailable",
                ) from exc
            # Receipt identities are security evidence, not optional labels.
            identity = self._svg_preflight_identity
            if identity is None:
                raise _error("SVG rasterizer identity is incomplete", 503, "svg_rasterizer_unavailable")
            raster_result = self._validate_svg_raster_result(png)
            if not isinstance(sanitized.metadata, dict) or raster_result is None:
                raise _error("SVG security evidence is incomplete", 503, "svg_rasterizer_unavailable")
            if raster_result.identity != identity:
                raise _error(
                    "SVG rasterizer identity does not match preflight",
                    503,
                    "svg_rasterizer_unavailable",
                )
            try:
                validated_png = validate_png_bytes(
                    raster_result.png_bytes,
                    expected_width=candidate.width,
                    expected_height=candidate.height,
                )
            except SvgSecurityError as exc:
                raise _error("SVG PNG failed validation", 422, "policy_rejected") from exc
            if (
                raster_result.png_sha256 != validated_png.png_sha256
                or raster_result.width != candidate.width
                or raster_result.height != candidate.height
            ):
                raise _error("SVG security evidence is incomplete", 503, "svg_rasterizer_unavailable")
            raw_relative = Path("working/source_artifacts/svg") / f"{raw_sha}.svg.untrusted"
            sanitized_relative = Path("working/sanitized_svg") / f"{sanitized.sanitized_sha256}.svg"
            png_relative = Path("assets/generated/svg") / f"{raster_result.png_sha256}.png"
            asset_id = f"provider-{provider_id}-{safe_id}-{raster_result.png_sha256[:16]}"
            review_status = "pending" if provider_id == "wikimedia-svg" else "approved"
            verified_at = _utc_now()
            item = {
                "asset_id": asset_id,
                "path": png_relative.as_posix(),
                "sha256": raster_result.png_sha256,
                "origin": "provider",
                "provider_id": provider_id,
                "source_url": candidate.landing_url,
                "license": {"spdx": candidate.license_spdx, "evidence_url": candidate.license_url,
                            "attribution_required": candidate.attribution_required,
                            "attribution_text": candidate.attribution_text, "verified_at": verified_at},
                "review_status": review_status,
            }

            try:
                publication_snapshot = asset_registry.snapshot_publication(root)
                # Content-addressed targets must be absent or byte-identical.
                # Track only files this transaction created; rollback must not
                # remove a pre-existing identical cache entry.
                for relative, payload in (
                    (raw_relative, raw),
                    (sanitized_relative, sanitized.canonical_svg),
                    (png_relative, raster_result.png_bytes),
                ):
                    target = root / relative
                    if target.exists() or target.is_symlink():
                        if (
                            target.is_symlink()
                            or not target.is_file()
                            or target.read_bytes() != payload
                        ):
                            raise _error(
                                "SVG content-addressed path conflicts",
                                409,
                                "registry_conflict",
                            )
                    else:
                        try:
                            self._atomic_write(root, relative, payload)
                        except Exception:
                            # Atomic publication may have replaced the final
                            # name before a later fsync failure. Track that
                            # exact payload so rollback still owns it.
                            if (
                                target.is_file()
                                and not target.is_symlink()
                                and target.read_bytes() == payload
                            ):
                                created_paths.append(target)
                            raise
                        else:
                            created_paths.append(target)

                receipt = asset_registry.save_svg_provider_receipt(
                    root,
                    item,
                    candidate_id=candidate.candidate_id,
                    query_hash=query_hash,
                    download_url=candidate.download_url,
                    raw_sha256=raw_sha,
                    raw_path=raw_relative.as_posix(),
                    raw_size=len(raw),
                    sanitized_sha256=sanitized.sanitized_sha256,
                    sanitized_path=sanitized_relative.as_posix(),
                    sanitized_size=len(sanitized.canonical_svg),
                    png_size=len(raster_result.png_bytes),
                    png_width=raster_result.width,
                    png_height=raster_result.height,
                    sanitizer_identity=sanitized.metadata,
                    rasterizer_identity=dict(identity),
                )
                receipt_path = (
                    root
                    / asset_registry.PROVIDER_RECEIPTS_REL
                    / (hashlib.sha256(asset_id.encode("utf-8")).hexdigest() + ".json")
                )
                created_paths.append(receipt_path)
                if not isinstance(receipt, dict) or not receipt_path.is_file() or receipt_path.is_symlink():
                    raise asset_registry.AssetRegistryError(
                        "SVG provider receipt publication failed"
                    )
                asset_registry.upsert_item(root, item)
            except (AssetProviderError, asset_registry.AssetRegistryError, OSError) as exc:
                try:
                    rollback()
                except AssetProviderError:
                    raise
                if isinstance(exc, AssetProviderError):
                    raise
                raise _error(
                    "SVG registry transaction failed", 409, "registry_conflict"
                ) from exc
            return {
                "item": item,
                "source": png_relative.as_posix(),
                "url": f"/{png_relative.as_posix()}",
                "idempotent": False,
            }
        except AssetProviderError:
            raise
        except DownloadValidationError as exc:
            raise _error("provider SVG failed validation", 422, "policy_rejected") from exc
        except DownloadError as exc:
            raise _error("provider SVG import failed", 502, "provider_failure") from exc
        except asset_registry.AssetRegistryError as exc:
            raise _error("asset registry is invalid", 409, "registry_conflict") from exc
        except OSError as exc:
            if publication_snapshot is not None:
                try:
                    rollback()
                except AssetProviderError:
                    raise
            raise _error("SVG project storage failed", 409, "registry_conflict") from exc
        finally:
            try:
                staging.unlink(missing_ok=True)
            except OSError:
                pass


__all__ = ["AssetProviderError", "AssetProviderService"]
