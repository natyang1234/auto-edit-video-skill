#!/usr/bin/env python3
"""Project-scoped asset provenance registry.

The registry deliberately keeps the on-disk format small: the contract lives in
``contracts/schemas/asset_provenance.schema.json`` and this module owns the
atomic persistence and attribution projection around it.  Every read and write
goes through :mod:`contract_registry`; a malformed registry is never treated as
an empty one.
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import re
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import contract_registry
try:
    from font_security import (
        FONT_VALIDATOR_VERSION,
        LIMITS_SHA256 as FONT_LIMITS_SHA256,
        PINNED_FONTTOOLS_VERSION,
        POLICY_VERSION as FONT_POLICY_VERSION,
        RECEIPT_VERSION as FONT_VALIDATION_RECEIPT_VERSION,
        FontSecurityError,
        FontValidationResult,
        validate_font_bytes,
    )
except ImportError:  # Pure-read callers must fail closed, not fail module import.
    FONT_VALIDATOR_VERSION = "font-security/1"
    FONT_POLICY_VERSION = "font-security-policy/1"
    FONT_VALIDATION_RECEIPT_VERSION = "font-validation-receipt/1"
    PINNED_FONTTOOLS_VERSION = "4.62.1"
    FONT_LIMITS_SHA256 = ""
    FontSecurityError = ValueError  # type: ignore[misc,assignment]
    FontValidationResult = None  # type: ignore[assignment]
    validate_font_bytes = None  # type: ignore[assignment]
from open_asset_providers import canonical_license_url
from open_font_providers import (
    FONT_LICENSE_URLS,
    FONTSOURCE_CDN_ENDPOINT,
    FONTSOURCE_LANDING_ENDPOINT,
    FONTSOURCE_NPM_ENDPOINT,
    GOOGLE_FONTS_LANDING_ENDPOINT,
    GOOGLE_FONTS_RAW_ENDPOINT,
    GOOGLE_FONTS_REF,
    normalize_fontsource_id,
    normalize_fontsource_version,
    normalize_google_family_id,
)
from svg_security import (
    LIMITS_SHA256 as SVG_LIMITS_SHA256,
    POLICY_VERSION as SVG_POLICY_VERSION,
    SANITIZER_VERSION as SVG_SANITIZER_VERSION,
    SvgSecurityError,
    validate_png_bytes,
)


PROVENANCE_REL = Path("assets/provenance.json")
ATTRIBUTION_REL = Path("ATTRIBUTION.md")
PROVIDER_RECEIPTS_REL = Path("working/provider-receipts")

_DEFAULT_ARTIFACT = {"schema_version": 1, "items": []}
_AUTO_LICENSE_ALLOWLIST = frozenset(
    {
        "CC0-1.0",
        "CC-BY-4.0",
        "CC-BY-SA-4.0",
        "Apache-2.0",
        "MIT",
        "ISC",
        "Unlicense",
        "OFL-1.1",
        "Ubuntu-font-1.0",
        "internal-original",
    }
)
_BUILTIN_PROVIDER_PREFIXES = {
    "openverse": "assets/providers/openverse/",
    "wikimedia": "assets/providers/wikimedia/",
    "heroicons": "assets/generated/svg/",
    "lucide": "assets/generated/svg/",
    "tabler": "assets/generated/svg/",
    "wikimedia-svg": "assets/generated/svg/",
    "google-fonts": "assets/fonts/",
    "fontsource": "assets/fonts/",
}
_SVG_PROVIDER_IDS = frozenset({"heroicons", "lucide", "tabler", "wikimedia-svg"})
_FONT_PROVIDER_IDS = frozenset({"google-fonts", "fontsource"})
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_EVIDENCE_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_SVG_RECEIPT_KEYS = frozenset(
    {
        "schema_version", "evidence_id", "asset_id", "provider_id",
        "candidate_id", "query_hash", "download_url_sha256",
        "registry_item_sha256", "license_spdx", "license_url", "decision",
        "issued_at", "raw", "sanitized", "png", "sanitizer", "rasterizer",
    }
)
_SVG_FILE_KEYS = frozenset({"path", "sha256", "size"})
_SVG_PNG_KEYS = frozenset({"path", "sha256", "size", "width", "height", "mime"})
_SVG_SANITIZER_KEYS = frozenset(
    {"policy_version", "sanitizer_version", "limits_sha256", "sanitize_cache_key_sha256"}
)
_SVG_RASTERIZER_KEYS = frozenset(
    {"version", "executable_sha256", "sandbox_executable_sha256", "sandbox_profile_sha256"}
)
_FONT_RECEIPT_KEYS = frozenset(
    {
        "schema_version", "evidence_id", "asset_id", "provider_id",
        "candidate_id", "query_hash", "download_url_sha256",
        "registry_item_sha256", "decision", "issued_at", "candidate",
        "font", "license", "validator", "capability",
    }
)
_FONT_CANDIDATE_KEYS = frozenset(
    {"family", "style", "weight", "subset", "unicode_range", "version", "source_url"}
)
_FONT_FILE_KEYS = frozenset({"path", "sha256", "size", "container", "mime"})
_FONT_LICENSE_KEYS = frozenset(
    {
        "path", "sha256", "normalized_sha256", "size", "spdx",
        "evidence_url", "download_url_sha256",
    }
)
_FONT_VALIDATOR_KEYS = frozenset({"receipt", "metadata"})
_FONT_VALIDATOR_METADATA_KEYS = frozenset(
    {
        "family", "subfamily", "style", "weight", "embedding_fs_type",
        "glyph_count", "scripts", "unicode_ranges", "required_text_nfc",
        "required_glyphs", "ignored_characters",
    }
)
_FONT_CAPABILITY_KEYS = frozenset(
    {"fonttools_version", "validator_version", "policy_version", "limits_sha256"}
)
_MISSING = object()

# The first two lines are intentionally fixed.  Keep this string private so
# callers cannot accidentally alter the format and make attribution drift.
_ATTRIBUTION_HEADER = "# ATTRIBUTION.md\n\nGenerated from `assets/provenance.json`.\n\n"
_ATTRIBUTION_EMPTY = "無須列名。\n"
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_SPACE_RE = re.compile(r"\s+")


class AssetRegistryError(ValueError):
    """Raised when a provenance registry or attribution projection is unsafe."""


def _registry_path(project_dir: Path) -> Path:
    return Path(project_dir) / PROVENANCE_REL


def _attribution_path(project_dir: Path) -> Path:
    return Path(project_dir) / ATTRIBUTION_REL


def _reject_symlink(path: Path, label: str) -> None:
    """Reject a target symlink, including a dangling one, before I/O."""
    try:
        is_link = path.is_symlink()
    except OSError as exc:
        raise AssetRegistryError(f"cannot inspect {label}: {path}") from exc
    if is_link:
        raise AssetRegistryError(f"{label} must not be a symlink: {path}")


def _reject_parent_symlink(path: Path, label: str) -> None:
    """Reject an existing target parent symlink before reads or writes."""
    parent = path.parent
    try:
        if parent.is_symlink():
            raise AssetRegistryError(f"{label} parent must not be a symlink: {parent}")
    except OSError as exc:
        raise AssetRegistryError(f"cannot inspect {label} parent: {parent}") from exc


def _prepare_parent(path: Path, label: str) -> None:
    """Create a target parent without following a symlinked directory."""
    parent = path.parent
    # For our project-relative targets, an existing parent symlink would make
    # an apparently local write escape the project.  Refuse it explicitly.
    if parent.exists() and parent.is_symlink():
        raise AssetRegistryError(f"{label} parent must not be a symlink: {parent}")
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AssetRegistryError(f"cannot create {label} parent: {parent}") from exc
    if parent.is_symlink():
        raise AssetRegistryError(f"{label} parent must not be a symlink: {parent}")


def _validation_errors(artifact: Any) -> list[str]:
    """Run strict schema, semantic, finite-number, and uniqueness checks."""
    errors: list[str] = []
    try:
        # ``validate_artifact`` supplies the schema and the semantic validator
        # (including validators added after this module).  Its schema checker
        # does not walk otherwise-unconstrained values for NaN, so canonical
        # hashing is also used as the contract's finite-number guard.
        errors.extend(contract_registry.validate_artifact("asset_provenance", artifact))
        contract_registry.canonical_hash(artifact)
    except Exception as exc:  # ContractError and malformed caller objects
        errors.append(str(exc) or exc.__class__.__name__)

    if isinstance(artifact, dict) and isinstance(artifact.get("items"), list):
        seen_ids: set[Any] = set()
        seen_paths: set[Any] = set()
        for index, item in enumerate(artifact["items"]):
            if not isinstance(item, dict):
                continue
            asset_id = item.get("asset_id")
            asset_path = item.get("path")
            try:
                duplicate_id = asset_id in seen_ids
            except TypeError:
                duplicate_id = False
            if duplicate_id:
                errors.append(f"$.items[{index}].asset_id: duplicate asset_id {asset_id!r}")
            else:
                try:
                    seen_ids.add(asset_id)
                except TypeError:
                    pass
            try:
                duplicate_path = asset_path in seen_paths
            except TypeError:
                duplicate_path = False
            if duplicate_path:
                errors.append(f"$.items[{index}].path: duplicate path {asset_path!r}")
            else:
                try:
                    seen_paths.add(asset_path)
                except TypeError:
                    pass
    return errors


def _validate_artifact(artifact: Any) -> dict:
    errors = _validation_errors(artifact)
    if errors:
        raise AssetRegistryError("asset provenance failed validation: " + "; ".join(errors))
    # The contract guarantees this shape after successful validation.  Keep a
    # defensive check here so a future validator cannot accidentally make this
    # API return a non-dict object.
    if not isinstance(artifact, dict):
        raise AssetRegistryError("asset provenance must be an object")
    return artifact


def _json_bytes(artifact: dict) -> bytes:
    try:
        return (
            json.dumps(
                artifact,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise AssetRegistryError("asset provenance cannot be serialized") from exc


def _atomic_write_bytes(path: Path, payload: bytes, label: str) -> None:
    """Write *payload* to a sibling .part and atomically replace *path*.

    The final path is checked both before and after staging.  ``os.replace``
    itself does not follow the final symlink, but refusing one makes accidental
    writes to an attacker-controlled target observable and fail closed.
    """
    _reject_parent_symlink(path, label)
    _reject_symlink(path, label)
    _prepare_parent(path, label)
    _reject_symlink(path, label)
    temporary = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.part"
    try:
        # Exclusive creation avoids clobbering a pre-existing partial file.
        with temporary.open("xb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _reject_symlink(path, label)
        os.replace(temporary, path)
    except AssetRegistryError:
        raise
    except OSError as exc:
        raise AssetRegistryError(f"cannot atomically write {label}: {path}") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            # Cleanup is best effort; never hide the original write error.
            pass


def _single_line(value: Any) -> str:
    """Make external text safe for a single deterministic Markdown line."""
    if value is None:
        return ""
    text = str(value)
    text = _CONTROL_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip()


def _markdown_plain_text(value: Any) -> str:
    """Encode untrusted text so Markdown can only render inert plain text.

    Numeric character references preserve the visible text while ensuring raw
    HTML, Markdown links/images, and URL autolink syntax never reach the parser.
    Letters, numbers, and spaces are the only characters emitted verbatim.
    """
    text = _single_line(value)
    return "".join(
        char if char in {" ", "-", "."} or char.isalnum() else f"&#x{ord(char):X};"
        for char in text
    )


def _file_snapshot(path: Path, label: str) -> bytes | None:
    _reject_parent_symlink(path, label)
    _reject_symlink(path, label)
    if not path.exists():
        return None
    if not path.is_file():
        raise AssetRegistryError(f"{label} is not a regular file: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise AssetRegistryError(f"cannot read {label}: {path}") from exc


def _restore_snapshot(path: Path, payload: bytes | None, label: str) -> None:
    _reject_symlink(path, label)
    if payload is None:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise AssetRegistryError(f"cannot remove rolled-back {label}: {path}") from exc
        return
    _atomic_write_bytes(path, payload, label)


def snapshot_publication(project_dir: Path) -> dict[str, bytes | None]:
    """Capture exact registry/attribution bytes for a larger asset transaction."""
    project = Path(project_dir)
    return {
        "registry": _file_snapshot(_registry_path(project), "provenance registry"),
        "attribution": _file_snapshot(_attribution_path(project), "ATTRIBUTION.md"),
    }


def restore_publication(
    project_dir: Path, snapshot: dict[str, bytes | None]
) -> None:
    """Restore a snapshot exactly, including original file absence."""
    if not isinstance(snapshot, dict) or set(snapshot) != {"registry", "attribution"}:
        raise AssetRegistryError("registry publication snapshot is invalid")
    project = Path(project_dir)
    errors: list[str] = []
    for path, key, label in (
        (_registry_path(project), "registry", "provenance registry"),
        (_attribution_path(project), "attribution", "ATTRIBUTION.md"),
    ):
        try:
            _restore_snapshot(path, snapshot[key], label)
        except AssetRegistryError as exc:
            errors.append(str(exc))
    if errors:
        raise AssetRegistryError(
            "registry transaction rollback failed: " + "; ".join(errors)
        )


def _publish_registry_and_attribution(project_dir: Path, artifact: dict) -> None:
    """Publish registry and attribution as one rollback-capable transaction."""
    project = Path(project_dir)
    validated = _validate_artifact(artifact)
    registry_path = _registry_path(project)
    attribution_path = _attribution_path(project)
    registry_before = _file_snapshot(registry_path, "provenance registry")
    attribution_before = _file_snapshot(attribution_path, "ATTRIBUTION.md")
    registry_payload = _json_bytes(validated)
    attribution_payload = attribution_markdown(validated).encode("utf-8")
    try:
        _atomic_write_bytes(registry_path, registry_payload, "provenance registry")
        _atomic_write_bytes(attribution_path, attribution_payload, "ATTRIBUTION.md")
    except AssetRegistryError as exc:
        rollback_errors: list[str] = []
        for path, payload, label in (
            (registry_path, registry_before, "provenance registry"),
            (attribution_path, attribution_before, "ATTRIBUTION.md"),
        ):
            try:
                _restore_snapshot(path, payload, label)
            except AssetRegistryError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        if rollback_errors:
            raise AssetRegistryError(
                "registry transaction failed and rollback failed: "
                + "; ".join(rollback_errors)
            ) from exc
        raise


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _legacy_manual_upload_candidate(project_dir: Path, artifact: Any) -> dict | None:
    """Convert only the exact historical local-editor upload shape."""
    if (
        not isinstance(artifact, dict)
        or set(artifact) != {"items"}
        or not isinstance(artifact.get("items"), list)
        or not artifact["items"]
    ):
        return None
    expected = {"file", "original_name", "source", "bytes", "sha256", "uploaded_at"}
    converted: list[dict[str, Any]] = []
    root = Path(project_dir).resolve()
    for legacy in artifact["items"]:
        if not isinstance(legacy, dict) or set(legacy) != expected:
            return None
        relative = legacy.get("file")
        original_name = legacy.get("original_name")
        byte_count = legacy.get("bytes")
        digest = legacy.get("sha256")
        uploaded_at = legacy.get("uploaded_at")
        if (
            not isinstance(relative, str)
            or not contract_registry._safe_asset_path(relative)
            or not isinstance(original_name, str)
            or not original_name
            or Path(original_name).name != original_name
            or legacy.get("source") != "user-uploaded-through-local-editor"
            or isinstance(byte_count, bool)
            or not isinstance(byte_count, int)
            or byte_count < 0
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or not contract_registry._timezone_aware_iso8601(uploaded_at)
        ):
            return None
        lexical = Path(project_dir) / relative
        if lexical.is_symlink():
            return None
        try:
            asset = lexical.resolve(strict=True)
            asset.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            return None
        if not asset.is_file() or asset.stat().st_size != byte_count or _hash_file(asset) != digest:
            return None
        converted.append(
            {
                "asset_id": "asset-legacy-"
                + hashlib.sha256(relative.encode("utf-8")).hexdigest()[:20],
                "path": relative,
                "sha256": digest,
                "origin": "user-upload",
                "provider_id": None,
                "source_url": None,
                "license": {
                    "spdx": "UNKNOWN",
                    "attribution_required": False,
                    "attribution_text": "",
                    "verified_at": uploaded_at,
                },
                "review_status": "pending",
            }
        )
    candidate = {"schema_version": 1, "items": converted}
    return _validate_artifact(candidate)


def _read_registry_artifact(project_dir: Path) -> Any:
    """Read and parse the registry without validating or mutating storage."""
    path = _registry_path(Path(project_dir))
    _reject_parent_symlink(path, "provenance registry")
    _reject_symlink(path, "provenance registry")
    if not path.exists():
        return _MISSING
    if not path.is_file():
        raise AssetRegistryError(f"provenance registry is not a regular file: {path}")
    try:
        text = path.read_text(encoding="utf-8")
        return contract_registry.load_artifact_text(text)
    except Exception as exc:
        raise AssetRegistryError(f"invalid provenance registry: {path}") from exc


def load_registry(project_dir: Path) -> dict:
    """Purely read and strictly validate ``assets/provenance.json``.

    A missing current registry is the only case that returns the empty v1
    artifact. Legacy formats, malformed JSON, duplicate keys, non-finite
    numbers, schema failures, and semantic failures all raise without writing
    any project file. Call :func:`migrate_legacy_registry` only from an
    explicitly mutating or controlled initialization path.
    """
    artifact = _read_registry_artifact(Path(project_dir))
    if artifact is _MISSING:
        return {"schema_version": 1, "items": []}
    return _validate_artifact(artifact)


def migrate_legacy_registry(project_dir: Path) -> dict:
    """Migrate only the exact historical local-editor upload artifact.

    Missing and already-current registries are no-ops. Unknown, malformed, or
    merely similar legacy shapes fail closed. This function may write and must
    therefore never be called from a request read path.
    """
    project = Path(project_dir)
    artifact = _read_registry_artifact(project)
    if artifact is _MISSING:
        return {"schema_version": 1, "items": []}
    try:
        return _validate_artifact(artifact)
    except AssetRegistryError:
        migrated = _legacy_manual_upload_candidate(project, artifact)
        if migrated is None:
            raise
        _publish_registry_and_attribution(project, migrated)
        return migrated


def save_registry(project_dir: Path, artifact: dict) -> None:
    """Validate and atomically save a provenance artifact."""
    validated = _validate_artifact(artifact)
    path = _registry_path(Path(project_dir))
    _atomic_write_bytes(path, _json_bytes(validated), "provenance registry")


def upsert_item(project_dir: Path, item: dict) -> dict:
    """Insert or replace an item keyed by ``asset_id`` without path rebinding."""
    if not isinstance(item, dict):
        raise AssetRegistryError("asset item must be an object")
    project = Path(project_dir)
    migrate_legacy_registry(project)
    artifact = load_registry(project)
    asset_id = item.get("asset_id")
    asset_path = item.get("path")
    if not isinstance(asset_id, str) or not asset_id:
        raise AssetRegistryError("asset item requires a non-empty asset_id")
    if not isinstance(asset_path, str) or not asset_path:
        raise AssetRegistryError("asset item requires a non-empty path")

    replacement_index: int | None = None
    for index, existing in enumerate(artifact["items"]):
        if existing["asset_id"] == asset_id:
            replacement_index = index
            if existing["path"] != asset_path:
                raise AssetRegistryError(
                    f"asset_id {asset_id!r} cannot change path "
                    f"from {existing['path']!r} to {asset_path!r}"
                )
        elif existing["path"] == asset_path:
            raise AssetRegistryError(
                f"path {asset_path!r} is already owned by asset_id "
                f"{existing['asset_id']!r}"
            )

    items = list(artifact["items"])
    if replacement_index is None:
        items.append(item)
    else:
        items[replacement_index] = item
    candidate = {"schema_version": 1, "items": items}
    _publish_registry_and_attribution(project, candidate)
    return candidate


def current_item(project_dir: Path, path: str, sha256: str) -> dict | None:
    """Return an item only when both its path and content hash match."""
    artifact = load_registry(Path(project_dir))
    for item in artifact["items"]:
        if item.get("path") == path and item.get("sha256") == sha256:
            return item
    return None


def auto_license_errors(item: dict) -> list[str]:
    """Return final-gate license errors for a provider-origin item.

    Registry schema/semantic validation owns provider identity, source URL,
    timestamp, and hash shape.  This function focuses on the final allowlist,
    review state, and attribution obligations. The caller-provided item is
    expected to have already passed registry semantic validation.
    """
    errors: list[str] = []
    if not isinstance(item, dict):
        return ["asset item must be an object"]
    if item.get("review_status") != "approved":
        errors.append("review_status must be approved")
    if item.get("origin") != "provider":
        return errors

    license_info = item.get("license")
    if not isinstance(license_info, dict):
        return errors + ["license metadata is required"]
    spdx = license_info.get("spdx")
    if spdx not in _AUTO_LICENSE_ALLOWLIST:
        errors.append(f"provider license is not allowed for auto final: {spdx!r}")
    if spdx in {"CC-BY-4.0", "CC-BY-SA-4.0"}:
        if license_info.get("attribution_required") is not True:
            errors.append(f"{spdx} requires attribution_required=true")
        if not isinstance(license_info.get("attribution_text"), str) or not license_info[
            "attribution_text"
        ].strip():
            errors.append(f"{spdx} requires non-empty attribution_text")
    return errors


def _provider_receipt_path(project_dir: Path, asset_id: str) -> Path:
    name = hashlib.sha256(asset_id.encode("utf-8")).hexdigest() + ".json"
    return Path(project_dir) / PROVIDER_RECEIPTS_REL / name


def _provider_identity_errors(item: dict) -> list[str]:
    errors: list[str] = []
    provider_id = item.get("provider_id")
    prefix = _BUILTIN_PROVIDER_PREFIXES.get(provider_id)
    if prefix is None:
        errors.append("provider_id is not a built-in provider")
    elif not isinstance(item.get("path"), str) or not item["path"].startswith(prefix):
        errors.append("provider asset path does not match provider_id")
    license_info = item.get("license") or {}
    canonical = canonical_license_url(
        license_info.get("evidence_url"), str(license_info.get("spdx") or "")
    )
    repo_license = {
        ("heroicons", "MIT"): "https://github.com/tailwindlabs/heroicons/blob/0435d4ca364a608cc75e2f8683d374e55abbae26/LICENSE",
        ("lucide", "ISC"): "https://github.com/lucide-icons/lucide/blob/f12b0de177fbc2a6795e99be065887e72b237123/LICENSE",
        ("tabler", "MIT"): "https://github.com/tabler/tabler-icons/blob/8ac7d81b72ece11072ef25ea9fd92e80c6f3c9fc/LICENSE",
    }.get((provider_id, license_info.get("spdx")))
    font_license_ok = _font_license_evidence_matches(
        provider_id, license_info.get("evidence_url"), license_info.get("spdx")
    )
    if canonical is None and license_info.get("evidence_url") != repo_license and not font_license_ok:
        errors.append("provider license evidence is not canonical")
    return errors


def _font_license_evidence_matches(provider_id: Any, value: Any, spdx: Any) -> bool:
    """Validate the exact immutable license text URL used by font imports."""
    if provider_id not in _FONT_PROVIDER_IDS or spdx not in FONT_LICENSE_URLS:
        return False
    if not isinstance(value, str) or len(value) > 2048 or _CONTROL_RE.search(value):
        return False
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.query
            or parsed.fragment
        ):
            return False
    except ValueError:
        return False
    if provider_id == "google-fonts":
        match = re.fullmatch(
            rf"/google/fonts/{GOOGLE_FONTS_REF}/(ofl|apache|ufl)/([a-z0-9]{{1,80}})/(OFL\.txt|LICENSE\.txt|UFL\.txt)",
            parsed.path,
        )
        expected = {
            "ofl": ("OFL-1.1", "OFL.txt"),
            "apache": ("Apache-2.0", "LICENSE.txt"),
            "ufl": ("Ubuntu-font-1.0", "UFL.txt"),
        }
        return bool(
            parsed.hostname == "raw.githubusercontent.com"
            and match
            and expected[match.group(1)] == (spdx, match.group(3))
        )
    match = re.fullmatch(
        r"/npm/@fontsource/([a-z0-9]+(?:-[a-z0-9]+)*)@((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))/LICENSE",
        parsed.path,
    )
    return bool(parsed.hostname == "cdn.jsdelivr.net" and match)


def _provider_static_errors(item: dict) -> list[str]:
    return auto_license_errors(item) + _provider_identity_errors(item)


def save_provider_receipt(
    project_dir: Path,
    item: dict,
    *,
    candidate_id: str,
    download_url: str,
) -> dict:
    """Publish unsigned, hash-bound consistency evidence issued by the server."""
    if _provider_static_errors(item):
        raise AssetRegistryError("provider item is not eligible for consistency evidence")
    if (
        not isinstance(candidate_id, str)
        or not candidate_id
        or len(candidate_id) > 200
        or _CONTROL_RE.search(candidate_id)
        or not isinstance(download_url, str)
        or not download_url.startswith("https://")
    ):
        raise AssetRegistryError("provider receipt inputs are invalid")
    receipt = {
        "schema_version": 1,
        "evidence_id": uuid.uuid4().hex,
        "asset_id": item["asset_id"],
        "provider_id": item["provider_id"],
        "candidate_id": candidate_id,
        "path": item["path"],
        "asset_sha256": item["sha256"],
        "registry_item_sha256": contract_registry.canonical_hash(item),
        "download_url_sha256": hashlib.sha256(download_url.encode("utf-8")).hexdigest(),
        "license_spdx": item["license"]["spdx"],
        "license_url": item["license"]["evidence_url"],
        "issued_at": item["license"]["verified_at"],
    }
    path = _provider_receipt_path(Path(project_dir), item["asset_id"])
    if path.exists() or path.is_symlink():
        raise AssetRegistryError("provider consistency evidence already exists")
    _atomic_write_bytes(path, _json_bytes(receipt), "provider consistency evidence")
    return receipt


def save_svg_provider_receipt(
    project_dir: Path, item: dict, *, candidate_id: str, query_hash: str, download_url: str,
    raw_sha256: str, raw_path: str, raw_size: int, sanitized_sha256: str,
    sanitized_path: str, sanitized_size: int, png_size: int, png_width: int, png_height: int,
    sanitizer_identity: dict, rasterizer_identity: dict,
) -> dict:
    """Write SVG v2 consistency evidence with every transformed artifact bound."""
    if item.get("provider_id") not in _SVG_PROVIDER_IDS:
        raise AssetRegistryError("SVG provider item is not eligible for consistency evidence")
    if (
        item.get("origin") != "provider"
        or not isinstance(item.get("license"), dict)
        or _validation_errors({"schema_version": 1, "items": [item]})
        or _provider_identity_errors(item)
    ):
        raise AssetRegistryError("SVG provider item is not eligible for consistency evidence")
    if (
        not isinstance(candidate_id, str)
        or not candidate_id
        or len(candidate_id) > 200
        or _CONTROL_RE.search(candidate_id)
        or not isinstance(download_url, str)
        or not download_url.startswith("https://")
        or len(download_url) > 2048
        or _CONTROL_RE.search(download_url)
    ):
        raise AssetRegistryError("SVG receipt inputs are invalid")
    if not isinstance(sanitizer_identity, dict) or not isinstance(rasterizer_identity, dict):
        raise AssetRegistryError("SVG security identities are invalid")
    try:
        sanitizer = {key: sanitizer_identity[key] for key in _SVG_SANITIZER_KEYS}
        rasterizer = {key: rasterizer_identity[key] for key in _SVG_RASTERIZER_KEYS}
    except KeyError as exc:
        raise AssetRegistryError("SVG security identities are incomplete") from exc
    if not all(
        isinstance(value, int) and not isinstance(value, bool) and value > 0
        for value in (raw_size, sanitized_size, png_size, png_width, png_height)
    ):
        raise AssetRegistryError("SVG security identities are incomplete")
    receipt = {
        "schema_version": 2,
        "evidence_id": uuid.uuid4().hex,
        "asset_id": item["asset_id"],
        "provider_id": item["provider_id"],
        "candidate_id": candidate_id,
        "query_hash": query_hash,
        "download_url_sha256": hashlib.sha256(download_url.encode("utf-8")).hexdigest(),
        "registry_item_sha256": contract_registry.canonical_hash(item),
        "license_spdx": item["license"]["spdx"],
        "license_url": item["license"]["evidence_url"],
        "decision": item["review_status"],
        "issued_at": item["license"]["verified_at"],
        "raw": {"path": raw_path, "sha256": raw_sha256, "size": raw_size},
        "sanitized": {"path": sanitized_path, "sha256": sanitized_sha256, "size": sanitized_size},
        "png": {
            "path": item["path"], "sha256": item["sha256"], "size": png_size,
            "width": png_width, "height": png_height, "mime": "image/png",
        },
        "sanitizer": sanitizer,
        "rasterizer": rasterizer,
    }
    if _svg_receipt_shape_errors(receipt):
        raise AssetRegistryError("SVG provider consistency evidence is invalid")
    path = _provider_receipt_path(Path(project_dir), item["asset_id"])
    if path.exists() or path.is_symlink():
        raise AssetRegistryError("provider consistency evidence already exists")
    _secure_prepare_relative_parent(
        Path(project_dir), path.relative_to(Path(project_dir)).as_posix(),
        "provider consistency evidence",
    )
    _atomic_write_bytes(path, _json_bytes(receipt), "provider consistency evidence")
    return receipt


def save_font_provider_receipt(
    project_dir: Path,
    item: dict,
    *,
    candidate_id: str,
    query: str,
    download_url: str,
    license_download_url: str,
    candidate_metadata: dict[str, Any],
    font_result: Any,
    license_path: str,
    license_sha256: str,
    license_normalized_sha256: str,
    license_size: int,
    capability_identity: dict[str, str],
) -> dict:
    """Publish strict font v3 provenance bound to binary and license bytes."""
    if (
        item.get("provider_id") not in _FONT_PROVIDER_IDS
        or item.get("origin") != "provider"
        or _validation_errors({"schema_version": 1, "items": [item]})
        or _provider_static_errors(item)
    ):
        raise AssetRegistryError("font provider item is not eligible for consistency evidence")
    if (
        not _bounded_identity(candidate_id)
        or not _bounded_identity(query)
        or len(query) > 200
        or not isinstance(download_url, str)
        or not isinstance(license_download_url, str)
        or len(download_url) > 2048
        or len(license_download_url) > 2048
        or item["license"].get("evidence_url") != license_download_url
    ):
        raise AssetRegistryError("font receipt inputs are invalid")
    if type(candidate_metadata) is not dict or set(candidate_metadata) != _FONT_CANDIDATE_KEYS:
        raise AssetRegistryError("font candidate metadata is invalid")
    if capability_identity != _font_capability_identity() or type(capability_identity) is not dict:
        raise AssetRegistryError("font capability identity is invalid")
    if (
        not isinstance(license_size, int)
        or isinstance(license_size, bool)
        or not 0 < license_size <= 512 * 1024
        or not isinstance(license_sha256, str)
        or _HASH_RE.fullmatch(license_sha256) is None
        or not isinstance(license_normalized_sha256, str)
        or _HASH_RE.fullmatch(license_normalized_sha256) is None
        or license_path != f"licenses/{license_sha256}.txt"
    ):
        raise AssetRegistryError("font license evidence is invalid")
    validator = _font_validation_snapshot(font_result)
    if (
        font_result.sha256 != item.get("sha256")
        or font_result.byte_length <= 0
        or item.get("path") != f"assets/fonts/{font_result.sha256}.{font_result.container}"
        or validator["receipt"].get("license_spdx") != item["license"].get("spdx")
        or validator["receipt"].get("declared_mime_verified") is not True
    ):
        raise AssetRegistryError("font validation result does not match registry item")
    receipt = {
        "schema_version": 3,
        "evidence_id": uuid.uuid4().hex,
        "asset_id": item["asset_id"],
        "provider_id": item["provider_id"],
        "candidate_id": candidate_id,
        "query_hash": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "download_url_sha256": hashlib.sha256(download_url.encode("utf-8")).hexdigest(),
        "registry_item_sha256": contract_registry.canonical_hash(item),
        "decision": item["review_status"],
        "issued_at": item["license"]["verified_at"],
        "candidate": dict(candidate_metadata),
        "font": {
            "path": item["path"],
            "sha256": item["sha256"],
            "size": font_result.byte_length,
            "container": font_result.container,
            "mime": font_result.mime,
        },
        "license": {
            "path": license_path,
            "sha256": license_sha256,
            "normalized_sha256": license_normalized_sha256,
            "size": license_size,
            "spdx": item["license"]["spdx"],
            "evidence_url": item["license"]["evidence_url"],
            "download_url_sha256": hashlib.sha256(
                license_download_url.encode("utf-8")
            ).hexdigest(),
        },
        "validator": validator,
        "capability": dict(capability_identity),
    }
    if _font_receipt_shape_errors(receipt):
        raise AssetRegistryError("font provider consistency evidence is invalid")
    path = _provider_receipt_path(Path(project_dir), item["asset_id"])
    if path.exists() or path.is_symlink():
        raise AssetRegistryError("provider consistency evidence already exists")
    _secure_prepare_relative_parent(
        Path(project_dir), path.relative_to(Path(project_dir)).as_posix(),
        "font provider consistency evidence",
    )
    _atomic_write_bytes(path, _json_bytes(receipt), "font provider consistency evidence")
    return receipt


def _secure_prepare_relative_parent(
    project_dir: Path, relative: str, label: str
) -> Path:
    """Create/check every parent segment without traversing symlinks."""
    if (
        not isinstance(relative, str)
        or not relative
        or relative.startswith("/")
        or "\\" in relative
        or any(part in {"", ".", ".."} for part in relative.split("/"))
    ):
        raise AssetRegistryError(f"{label} path is invalid")
    current = Path(project_dir)
    for part in relative.split("/")[:-1]:
        current = current / part
        try:
            if current.is_symlink():
                raise AssetRegistryError(f"{label} parent must not be a symlink")
            current.mkdir(exist_ok=True)
        except AssetRegistryError:
            raise
        except OSError as exc:
            raise AssetRegistryError(f"cannot create {label} parent") from exc
        if not current.is_dir() or current.is_symlink():
            raise AssetRegistryError(f"{label} parent is unsafe")
    return Path(project_dir) / relative


def _safe_existing_project_file(
    project_dir: Path, relative: str, label: str
) -> Path:
    if (
        not isinstance(relative, str)
        or not relative
        or relative.startswith("/")
        or "\\" in relative
        or any(part in {"", ".", ".."} for part in relative.split("/"))
    ):
        raise AssetRegistryError(f"{label} path is invalid")
    current = Path(project_dir)
    for part in relative.split("/")[:-1]:
        current = current / part
        try:
            if current.is_symlink() or not current.is_dir():
                raise AssetRegistryError(f"{label} parent is missing or unsafe")
        except OSError as exc:
            raise AssetRegistryError(f"cannot inspect {label} parent") from exc
    target = Path(project_dir) / relative
    try:
        if target.is_symlink() or not target.is_file():
            raise AssetRegistryError(f"{label} is missing or unsafe")
    except OSError as exc:
        raise AssetRegistryError(f"cannot inspect {label}") from exc
    return target


def project_required_font_text(project_dir: Path, asset_id: str | None = None) -> str:
    """Return bounded NFC text from the current project-owned editor state.

    The browser never supplies this value to the import gate. Missing state is
    a valid empty project; malformed or symlinked state fails closed.
    """
    import unicodedata

    project = Path(project_dir)
    relative = "working/editor_state.json"
    path = project / relative
    if not path.exists() and not path.is_symlink():
        return ""
    try:
        path = _safe_existing_project_file(project, relative, "editor font text state")
        raw = path.read_bytes()
        if len(raw) > 4 * 1024 * 1024:
            raise AssetRegistryError("editor font text state exceeds size policy")
        state = contract_registry.load_artifact_text(raw.decode("utf-8", errors="strict"))
    except AssetRegistryError:
        raise
    except Exception as exc:
        raise AssetRegistryError("editor font text state is invalid") from exc
    if not isinstance(state, dict):
        raise AssetRegistryError("editor font text state is invalid")

    render_windows: list[tuple[float, float]] | None = None
    source_duration: float | None = None
    manifest_relative = "project.json"
    manifest_path = project / manifest_relative
    if manifest_path.exists() or manifest_path.is_symlink():
        try:
            manifest_path = _safe_existing_project_file(
                project, manifest_relative, "project font render manifest"
            )
            manifest_raw = manifest_path.read_bytes()
            if len(manifest_raw) > 4 * 1024 * 1024:
                raise AssetRegistryError("project font render manifest exceeds size policy")
            manifest = contract_registry.load_artifact_text(
                manifest_raw.decode("utf-8", errors="strict")
            )
            source_duration = float(manifest.get("source", {}).get("duration_s", 0.0))
            if not math.isfinite(source_duration) or source_duration < 0:
                raise ValueError("source duration is invalid")
        except AssetRegistryError:
            raise
        except Exception as exc:
            raise AssetRegistryError("project font render manifest is invalid") from exc
        render_windows = [(0.0, source_duration)] if source_duration > 0 else []

    segments = state.get("segments")
    if isinstance(segments, list) and segments:
        if len(segments) > 20_000:
            raise AssetRegistryError("editor font text segments are invalid")
        parsed_segments: list[tuple[float, float]] = []
        try:
            for segment in segments:
                if not isinstance(segment, dict):
                    raise ValueError("segment is invalid")
                start = float(segment.get("source_start"))
                end = float(segment.get("source_end"))
                if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
                    raise ValueError("segment timing is invalid")
                if source_duration is not None and end > source_duration + 0.05:
                    raise ValueError("segment extends past source duration")
                if end > start:
                    parsed_segments.append((start, end))
        except (TypeError, ValueError) as exc:
            raise AssetRegistryError("editor font text segments are invalid") from exc
        render_windows = parsed_segments
    elif segments is not None and not isinstance(segments, list):
        raise AssetRegistryError("editor font text segments are invalid")

    pieces: list[str] = []
    overlays = state.get("overlays", [])
    if not isinstance(overlays, list) or len(overlays) > 20_000:
        raise AssetRegistryError("editor font text state overlays are invalid")
    defaults = state.get("caption_defaults")
    default_asset_id = defaults.get("font_asset_id") if isinstance(defaults, dict) else None
    for overlay in overlays:
        if not isinstance(overlay, dict):
            raise AssetRegistryError("editor font text overlay is invalid")
        kind = overlay.get("type")
        if kind not in {"caption", "emphasis", "title", "card", "animation"}:
            continue
        if not overlay.get("visible", True):
            continue
        try:
            start = float(overlay.get("start", 0.0))
            end = float(overlay.get("end", 0.0))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            continue
        if render_windows is not None and not any(
            min(end, window_end) > max(start, window_start)
            for window_start, window_end in render_windows
        ):
            continue
        style = overlay.get("style") if isinstance(overlay.get("style"), dict) else {}
        effective_asset_id = style.get("font_asset_id") or default_asset_id
        # During import, validate only legacy/unassigned render text.  Text
        # already bound explicitly or through defaults belongs to that exact
        # project font and must not expand a different candidate's coverage.
        if asset_id is None:
            if effective_asset_id:
                continue
        elif effective_asset_id != asset_id:
            continue
        text = overlay.get("text")
        if isinstance(text, str):
            pieces.append(text)
    normalized = unicodedata.normalize("NFC", "\n".join(pieces))
    if len(normalized) > 100_000:
        raise AssetRegistryError("project font text exceeds validation limit")
    return normalized


def _font_validation_snapshot(result: Any) -> dict[str, Any]:
    """Copy the exact security result into receipt-safe primitive values."""
    if FontValidationResult is None or type(result) is not FontValidationResult:
        raise AssetRegistryError("font validator returned an invalid result")
    receipt = result.receipt
    expected_receipt_keys = {
        "receipt_version", "validator_version", "policy_version", "fonttools_version",
        "limits_sha256", "font_sha256", "container", "mime",
        "declared_mime_verified", "byte_length", "license_spdx", "axis_count",
        "instance_count", "embedding_fs_type", "required_text_nfc_sha256",
        "unicode_version", "unicode_coverage_count", "unicode_coverage_sha256",
        "required_coverage_sha256", "system_fallback_allowed", "hinting_executed",
    }
    if type(receipt) is not dict or set(receipt) != expected_receipt_keys:
        raise AssetRegistryError("font validator receipt shape is invalid")
    metadata = {
        "family": result.family,
        "subfamily": result.subfamily,
        "style": result.style,
        "weight": result.weight,
        "embedding_fs_type": result.embedding_fs_type,
        "glyph_count": result.glyph_count,
        "scripts": list(result.scripts),
        "unicode_ranges": [list(value) for value in result.unicode_ranges],
        "required_text_nfc": result.required_text_nfc,
        "required_glyphs": [dict(value) for value in result.required_glyphs],
        "ignored_characters": [dict(value) for value in result.ignored_characters],
    }
    try:
        # Round-trip canonical JSON to reject non-primitives and mutable aliases.
        snapshot = contract_registry.load_artifact_text(
            json.dumps(
                {"receipt": receipt, "metadata": metadata},
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            )
        )
    except Exception as exc:
        raise AssetRegistryError("font validator result cannot be serialized") from exc
    return snapshot


def _font_capability_identity() -> dict[str, str]:
    if not FONT_LIMITS_SHA256:
        raise AssetRegistryError("font validator capability is unavailable")
    return {
        "fonttools_version": PINNED_FONTTOOLS_VERSION,
        "validator_version": FONT_VALIDATOR_VERSION,
        "policy_version": FONT_POLICY_VERSION,
        "limits_sha256": FONT_LIMITS_SHA256,
    }


def _canonical_license_lines(text: str) -> tuple[str, tuple[str, ...]]:
    """Normalize one license body without erasing meaningful line indentation."""
    import unicodedata

    normalized = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    lines = [line.rstrip(" \t") for line in normalized.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    canonical_lines = tuple(lines)
    canonical = "\n".join(canonical_lines) + ("\n" if canonical_lines else "")
    return canonical, canonical_lines


def validate_font_license_text(raw: bytes, spdx: str) -> tuple[str, str]:
    """Validate hostile license bytes and return canonical text/hash."""
    import unicodedata

    if not isinstance(raw, bytes) or not 0 < len(raw) <= 512 * 1024:
        raise AssetRegistryError("font license text exceeds size policy")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise AssetRegistryError("font license text must be UTF-8") from exc
    if "\x00" in text or any(
        unicodedata.category(char) in {"Cf", "Cs"}
        or (unicodedata.category(char) == "Cc" and char not in {"\n", "\r", "\t"})
        for char in text
    ):
        raise AssetRegistryError("font license text contains forbidden controls")
    if re.search(r"<\s*(?:!doctype|html|head|body|script|form|iframe)\b", text, re.I):
        raise AssetRegistryError("font license download returned HTML")
    normalized, normalized_lines = _canonical_license_lines(text)
    if not normalized_lines:
        raise AssetRegistryError("font license text is empty")
    template_name = {
        "OFL-1.1": "OFL-1.1.txt",
        "Apache-2.0": "Apache-2.0.txt",
        "Ubuntu-font-1.0": "Ubuntu-font-1.0.txt",
    }.get(spdx)
    if template_name is None:
        raise AssetRegistryError("font license text does not match declared SPDX")
    template_path = Path(__file__).resolve().parent.parent / "contracts/licenses" / template_name
    try:
        template_raw = template_path.read_bytes()
        _template, template_lines = _canonical_license_lines(
            template_raw.decode("utf-8", errors="strict")
        )
    except (OSError, UnicodeError) as exc:
        raise AssetRegistryError("font license template is unavailable") from exc
    if normalized_lines != template_lines:
        if (
            len(normalized_lines) <= len(template_lines)
            or normalized_lines[-len(template_lines) :] != template_lines
        ):
            raise AssetRegistryError("font license text does not match declared SPDX")
        prefix_lines = normalized_lines[: -len(template_lines)]
        prefix = "\n".join(prefix_lines)
        prefix_bytes = prefix.encode("utf-8")
        if not prefix_lines or len(prefix_bytes) > 8192 or len(prefix_lines) > 32:
            raise AssetRegistryError("font license copyright header is invalid")
        exact_boilerplate = {
            "This Font Software is licensed under the SIL Open Font License, Version 1.1.",
            "This license is copied below, and is also available with a FAQ at:",
            "https://openfontlicense.org",
            "https://scripts.sil.org/OFL",
        }
        for line in prefix_lines:
            if not line:
                continue
            if line in exact_boilerplate:
                continue
            if re.fullmatch(r"Copyright(?: \(c\))? [^\x00-\x1f\x7f]{1,500}", line):
                continue
            if re.fullmatch(r"with Reserved Font Name(?:s)? [^\x00-\x1f\x7f]{1,500}", line):
                continue
            raise AssetRegistryError("font license copyright header is invalid")
    return normalized, hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _positive_int(value: Any, *, maximum: int | None = None) -> bool:
    return bool(
        isinstance(value, int)
        and not isinstance(value, bool)
        and value > 0
        and (maximum is None or value <= maximum)
    )


def _bounded_identity(value: Any) -> bool:
    return bool(
        isinstance(value, str)
        and value
        and len(value) <= 200
        and not _CONTROL_RE.search(value)
    )


def _svg_receipt_shape_errors(receipt: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(receipt, dict) or set(receipt) != _SVG_RECEIPT_KEYS:
        return ["receipt keys are invalid"]
    if receipt.get("schema_version") != 2:
        errors.append("schema_version is invalid")
    for key in (
        "asset_id", "provider_id", "candidate_id", "license_spdx", "license_url",
        "decision", "issued_at",
    ):
        if not _bounded_identity(receipt.get(key)):
            errors.append(f"{key} is invalid")
    for key in (
        "query_hash", "download_url_sha256", "registry_item_sha256",
    ):
        if not isinstance(receipt.get(key), str) or _HASH_RE.fullmatch(receipt[key]) is None:
            errors.append(f"{key} is invalid")
    if not isinstance(receipt.get("evidence_id"), str) or _EVIDENCE_ID_RE.fullmatch(receipt["evidence_id"]) is None:
        errors.append("evidence_id is invalid")
    if receipt.get("provider_id") not in _SVG_PROVIDER_IDS:
        errors.append("provider_id is invalid")
    if receipt.get("decision") not in {"approved", "pending"}:
        errors.append("decision is invalid")
    if not contract_registry._timezone_aware_iso8601(receipt.get("issued_at")):
        errors.append("issued_at is invalid")

    for section, expected_keys, maximum in (
        ("raw", _SVG_FILE_KEYS, 2 * 1024 * 1024),
        ("sanitized", _SVG_FILE_KEYS, 2 * 1024 * 1024),
        ("png", _SVG_PNG_KEYS, 64 * 1024 * 1024),
    ):
        value = receipt.get(section)
        if not isinstance(value, dict) or set(value) != expected_keys:
            errors.append(f"{section} is invalid")
            continue
        if not isinstance(value.get("sha256"), str) or _HASH_RE.fullmatch(value["sha256"]) is None:
            errors.append(f"{section}.sha256 is invalid")
        if not _positive_int(value.get("size"), maximum=maximum):
            errors.append(f"{section}.size is invalid")
        digest = value.get("sha256")
        expected_path = None
        if isinstance(digest, str) and _HASH_RE.fullmatch(digest):
            if section == "raw":
                expected_path = f"working/source_artifacts/svg/{digest}.svg.untrusted"
            elif section == "sanitized":
                expected_path = f"working/sanitized_svg/{digest}.svg"
            else:
                expected_path = f"assets/generated/svg/{digest}.png"
        if value.get("path") != expected_path or "\\" in str(value.get("path")):
            errors.append(f"{section}.path is invalid")
    png = receipt.get("png")
    if isinstance(png, dict):
        if png.get("mime") != "image/png":
            errors.append("png.mime is invalid")
        if not _positive_int(png.get("width"), maximum=4096) or not _positive_int(png.get("height"), maximum=4096):
            errors.append("png dimensions are invalid")
        elif png["width"] * png["height"] > 16 * 1024 * 1024:
            errors.append("png pixel count is invalid")

    sanitizer = receipt.get("sanitizer")
    if not isinstance(sanitizer, dict) or set(sanitizer) != _SVG_SANITIZER_KEYS:
        errors.append("sanitizer identity is invalid")
    else:
        if sanitizer.get("policy_version") != SVG_POLICY_VERSION:
            errors.append("policy_version is stale")
        if sanitizer.get("sanitizer_version") != SVG_SANITIZER_VERSION:
            errors.append("sanitizer_version is stale")
        if sanitizer.get("limits_sha256") != SVG_LIMITS_SHA256:
            errors.append("limits_sha256 is stale")
        if not isinstance(sanitizer.get("sanitize_cache_key_sha256"), str) or _HASH_RE.fullmatch(sanitizer["sanitize_cache_key_sha256"]) is None:
            errors.append("sanitize cache identity is invalid")
    rasterizer = receipt.get("rasterizer")
    if not isinstance(rasterizer, dict) or set(rasterizer) != _SVG_RASTERIZER_KEYS:
        errors.append("rasterizer identity is invalid")
    else:
        if not _bounded_identity(rasterizer.get("version")):
            errors.append("rasterizer version is invalid")
        for key in _SVG_RASTERIZER_KEYS - {"version"}:
            if not isinstance(rasterizer.get(key), str) or _HASH_RE.fullmatch(rasterizer[key]) is None:
                errors.append(f"rasterizer {key} is invalid")
    if not errors and isinstance(sanitizer, dict):
        raw = receipt["raw"]
        expected_cache = contract_registry.canonical_hash(
            {
                "raw_sha256": raw["sha256"],
                "policy_version": sanitizer["policy_version"],
                "sanitizer_version": sanitizer["sanitizer_version"],
                "limits_sha256": sanitizer["limits_sha256"],
            }
        )
        if sanitizer["sanitize_cache_key_sha256"] != expected_cache:
            errors.append("sanitize cache identity does not match raw input")
    return errors


def _font_identity_urls(receipt: dict[str, Any]) -> tuple[str, str, str] | None:
    """Recompute immutable binary/license URLs from the v3 identity only."""
    provider_id = receipt.get("provider_id")
    candidate_id = receipt.get("candidate_id")
    candidate = receipt.get("candidate")
    if not isinstance(candidate_id, str) or not isinstance(candidate, dict):
        return None
    try:
        if provider_id == "google-fonts":
            source_url = candidate.get("source_url")
            match = re.fullmatch(
                re.escape(GOOGLE_FONTS_LANDING_ENDPOINT)
                + r"/([0-9a-f]{40})/(ofl|apache|ufl)/([a-z0-9]{1,80})/([^/\\%?#\x00-\x1f\x7f]+\.ttf)",
                source_url if isinstance(source_url, str) else "",
            )
            if match is None:
                return None
            version, root, family, source_name = match.groups()
            if version != GOOGLE_FONTS_REF or normalize_google_family_id(family) != family:
                return None
            sha, separator, name = candidate_id.partition(":")
            if (
                separator != ":"
                or re.fullmatch(r"[0-9a-f]{40}", sha) is None
                or name != source_name
                or candidate != {
                    "family": family,
                    "style": "",
                    "weight": None,
                    "subset": "",
                    "unicode_range": "",
                    "version": GOOGLE_FONTS_REF,
                    "source_url": (
                        f"{GOOGLE_FONTS_LANDING_ENDPOINT}/{GOOGLE_FONTS_REF}/"
                        f"{root}/{family}/{name}"
                    ),
                }
            ):
                return None
            license_name = {"ofl": "OFL.txt", "apache": "LICENSE.txt", "ufl": "UFL.txt"}[root]
            query_hash = hashlib.sha256(f"{root}/{family}".encode()).hexdigest()
            return (
                f"{GOOGLE_FONTS_RAW_ENDPOINT}/{GOOGLE_FONTS_REF}/{root}/{family}/{name}",
                f"{GOOGLE_FONTS_RAW_ENDPOINT}/{GOOGLE_FONTS_REF}/{root}/{family}/{license_name}",
                query_hash,
            )
        if provider_id == "fontsource":
            parts = candidate_id.split(":")
            if len(parts) != 5:
                return None
            cid, cversion, subset, weight_text, style = parts
            font_id, version = cid, cversion
            if (
                normalize_fontsource_id(font_id) != font_id
                or normalize_fontsource_version(version) != version
                or (cid, cversion) != (font_id, version)
                or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", subset)
                or not weight_text.isascii()
                or not weight_text.isdigit()
                or not 1 <= int(weight_text) <= 1000
                or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", style)
                or candidate.get("style") != style
                or candidate.get("weight") != int(weight_text)
                or candidate.get("subset") != subset
                or candidate.get("version") != version
                or candidate.get("source_url") != f"{FONTSOURCE_LANDING_ENDPOINT}/{font_id}"
            ):
                return None
            return (
                f"{FONTSOURCE_CDN_ENDPOINT}/{font_id}@{version}/{subset}-{weight_text}-{style}.ttf",
                f"{FONTSOURCE_NPM_ENDPOINT}/{font_id}@{version}/LICENSE",
                hashlib.sha256(f"{font_id}@{version}".encode()).hexdigest(),
            )
    except (TypeError, ValueError):
        return None
    return None


def _font_receipt_shape_errors(receipt: Any) -> list[str]:
    errors: list[str] = []
    if type(receipt) is not dict or set(receipt) != _FONT_RECEIPT_KEYS:
        return ["receipt keys are invalid"]
    if receipt.get("schema_version") != 3:
        errors.append("schema_version is invalid")
    if receipt.get("provider_id") not in _FONT_PROVIDER_IDS:
        errors.append("provider_id is invalid")
    for key in ("asset_id", "provider_id", "candidate_id", "decision", "issued_at"):
        if not _bounded_identity(receipt.get(key)):
            errors.append(f"{key} is invalid")
    if receipt.get("decision") != "approved":
        errors.append("decision is invalid")
    if not contract_registry._timezone_aware_iso8601(receipt.get("issued_at")):
        errors.append("issued_at is invalid")
    if not isinstance(receipt.get("evidence_id"), str) or _EVIDENCE_ID_RE.fullmatch(receipt["evidence_id"]) is None:
        errors.append("evidence_id is invalid")
    for key in ("query_hash", "download_url_sha256", "registry_item_sha256"):
        if not isinstance(receipt.get(key), str) or _HASH_RE.fullmatch(receipt[key]) is None:
            errors.append(f"{key} is invalid")
    candidate = receipt.get("candidate")
    if type(candidate) is not dict or set(candidate) != _FONT_CANDIDATE_KEYS:
        errors.append("candidate metadata is invalid")
    else:
        for key in ("family", "style", "subset", "unicode_range", "version", "source_url"):
            value = candidate.get(key)
            if not isinstance(value, str) or len(value) > 16 * 1024 or _CONTROL_RE.search(value):
                errors.append(f"candidate.{key} is invalid")
        weight = candidate.get("weight")
        if weight is not None and not _positive_int(weight, maximum=1000):
            errors.append("candidate.weight is invalid")

    font = receipt.get("font")
    if type(font) is not dict or set(font) != _FONT_FILE_KEYS:
        errors.append("font artifact is invalid")
    else:
        digest = font.get("sha256")
        container = font.get("container")
        mime = font.get("mime")
        if not isinstance(digest, str) or _HASH_RE.fullmatch(digest) is None:
            errors.append("font.sha256 is invalid")
        if not _positive_int(font.get("size"), maximum=32 * 1024 * 1024):
            errors.append("font.size is invalid")
        if container not in {"ttf", "otf"} or mime != {"ttf": "font/ttf", "otf": "font/otf"}.get(container):
            errors.append("font container or MIME is invalid")
        if font.get("path") != f"assets/fonts/{digest}.{container}":
            errors.append("font.path is invalid")

    license_info = receipt.get("license")
    if type(license_info) is not dict or set(license_info) != _FONT_LICENSE_KEYS:
        errors.append("license artifact is invalid")
    else:
        digest = license_info.get("sha256")
        if not isinstance(digest, str) or _HASH_RE.fullmatch(digest) is None:
            errors.append("license.sha256 is invalid")
        if not isinstance(license_info.get("normalized_sha256"), str) or _HASH_RE.fullmatch(
            license_info.get("normalized_sha256", "")
        ) is None:
            errors.append("license.normalized_sha256 is invalid")
        if not _positive_int(license_info.get("size"), maximum=512 * 1024):
            errors.append("license.size is invalid")
        if license_info.get("path") != f"licenses/{digest}.txt":
            errors.append("license.path is invalid")
        if license_info.get("spdx") not in FONT_LICENSE_URLS:
            errors.append("license.spdx is invalid")
        if not _font_license_evidence_matches(
            receipt.get("provider_id"), license_info.get("evidence_url"), license_info.get("spdx")
        ):
            errors.append("license evidence URL is invalid")
        if not isinstance(license_info.get("download_url_sha256"), str) or _HASH_RE.fullmatch(
            license_info.get("download_url_sha256", "")
        ) is None:
            errors.append("license download hash is invalid")

    capability = receipt.get("capability")
    if type(capability) is not dict or set(capability) != _FONT_CAPABILITY_KEYS:
        errors.append("capability identity is invalid")
    else:
        try:
            if capability != _font_capability_identity():
                errors.append("capability identity is stale")
        except AssetRegistryError:
            errors.append("capability identity is unavailable")

    validator = receipt.get("validator")
    if type(validator) is not dict or set(validator) != _FONT_VALIDATOR_KEYS:
        errors.append("validator evidence is invalid")
    else:
        validation_receipt = validator.get("receipt")
        metadata = validator.get("metadata")
        expected_validation_keys = {
            "receipt_version", "validator_version", "policy_version", "fonttools_version",
            "limits_sha256", "font_sha256", "container", "mime",
            "declared_mime_verified", "byte_length", "license_spdx", "axis_count",
            "instance_count", "embedding_fs_type", "required_text_nfc_sha256",
            "unicode_version", "unicode_coverage_count", "unicode_coverage_sha256",
            "required_coverage_sha256", "system_fallback_allowed", "hinting_executed",
        }
        if type(validation_receipt) is not dict or set(validation_receipt) != expected_validation_keys:
            errors.append("validator receipt is invalid")
        elif isinstance(font, dict) and isinstance(license_info, dict):
            static_expected = {
                "receipt_version": FONT_VALIDATION_RECEIPT_VERSION,
                "validator_version": FONT_VALIDATOR_VERSION,
                "policy_version": FONT_POLICY_VERSION,
                "fonttools_version": PINNED_FONTTOOLS_VERSION,
                "limits_sha256": FONT_LIMITS_SHA256,
                "font_sha256": font.get("sha256"),
                "container": font.get("container"),
                "mime": font.get("mime"),
                "byte_length": font.get("size"),
                "license_spdx": license_info.get("spdx"),
                "declared_mime_verified": True,
                "system_fallback_allowed": False,
                "hinting_executed": False,
            }
            if any(validation_receipt.get(key) != value for key, value in static_expected.items()):
                errors.append("validator receipt does not match artifacts")
        if type(metadata) is not dict or set(metadata) != _FONT_VALIDATOR_METADATA_KEYS:
            errors.append("validator metadata is invalid")
        else:
            required_text = metadata.get("required_text_nfc")
            if not isinstance(required_text, str) or len(required_text) > 100_000:
                errors.append("validator required text is invalid")
            elif isinstance(validation_receipt, dict) and validation_receipt.get(
                "required_text_nfc_sha256"
            ) != hashlib.sha256(required_text.encode("utf-8")).hexdigest():
                errors.append("validator required text hash does not match")
            for key in ("scripts", "unicode_ranges", "required_glyphs", "ignored_characters"):
                if not isinstance(metadata.get(key), list):
                    errors.append(f"validator metadata {key} is invalid")
    urls = _font_identity_urls(receipt)
    if urls is None:
        errors.append("font provider candidate identity is invalid")
    else:
        font_url, license_url, expected_query_hash = urls
        if receipt.get("query_hash") != expected_query_hash:
            errors.append("font query identity is invalid")
        if receipt.get("download_url_sha256") != hashlib.sha256(font_url.encode()).hexdigest():
            errors.append("font download URL identity is invalid")
        if isinstance(license_info, dict):
            expected_license_hash = hashlib.sha256(license_url.encode()).hexdigest()
            if (
                license_info.get("evidence_url") != license_url
                or license_info.get("download_url_sha256") != expected_license_hash
            ):
                errors.append("font license URL identity is invalid")
    return errors


def _load_provider_receipt(project_dir: Path, asset_id: str) -> dict:
    name = hashlib.sha256(asset_id.encode("utf-8")).hexdigest() + ".json"
    relative = (PROVIDER_RECEIPTS_REL / name).as_posix()
    path = _safe_existing_project_file(
        Path(project_dir), relative, "provider consistency evidence"
    )
    try:
        receipt = contract_registry.load_artifact_text(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AssetRegistryError("provider consistency evidence is invalid") from exc
    expected = {
        "schema_version",
        "evidence_id",
        "asset_id",
        "provider_id",
        "candidate_id",
        "path",
        "asset_sha256",
        "registry_item_sha256",
        "download_url_sha256",
        "license_spdx",
        "license_url",
        "issued_at",
    }
    if (
        not isinstance(receipt, dict)
        or set(receipt) != expected
        or receipt.get("schema_version") != 1
        or any(
            not isinstance(receipt.get(key), str) or not receipt[key]
            for key in expected - {"schema_version"}
        )
        or re.fullmatch(r"[0-9a-f]{64}", receipt["asset_sha256"]) is None
        or re.fullmatch(r"[0-9a-f]{64}", receipt["registry_item_sha256"]) is None
        or re.fullmatch(r"[0-9a-f]{64}", receipt["download_url_sha256"]) is None
    ):
        raise AssetRegistryError("provider consistency evidence is invalid")
    return receipt


def _load_svg_provider_receipt(project_dir: Path, asset_id: str) -> dict:
    name = hashlib.sha256(asset_id.encode("utf-8")).hexdigest() + ".json"
    path = _safe_existing_project_file(
        Path(project_dir),
        (PROVIDER_RECEIPTS_REL / name).as_posix(),
        "SVG provider consistency evidence",
    )
    try:
        receipt = contract_registry.load_artifact_text(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AssetRegistryError("SVG provider consistency evidence is invalid") from exc
    if _svg_receipt_shape_errors(receipt):
        raise AssetRegistryError("SVG provider consistency evidence is invalid")
    return receipt


def _load_font_provider_receipt(project_dir: Path, asset_id: str) -> dict:
    name = hashlib.sha256(asset_id.encode("utf-8")).hexdigest() + ".json"
    path = _safe_existing_project_file(
        Path(project_dir),
        (PROVIDER_RECEIPTS_REL / name).as_posix(),
        "font provider consistency evidence",
    )
    try:
        receipt = contract_registry.load_artifact_text(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AssetRegistryError("font provider consistency evidence is invalid") from exc
    if _font_receipt_shape_errors(receipt):
        raise AssetRegistryError("font provider consistency evidence is invalid")
    return receipt


def _font_receipt_consistency_errors(
    project_dir: Path, item: dict, receipt: dict, *, required_text: str = ""
) -> tuple[list[str], Any | None]:
    """Recompute v3 evidence, then validate current authoritative text."""
    if _font_receipt_shape_errors(receipt):
        return ["font provider consistency evidence is invalid"], None
    license_info = item.get("license") if isinstance(item, dict) else None
    if not isinstance(license_info, dict):
        return ["font provider license metadata is invalid"], None
    expected = {
        "asset_id": item.get("asset_id"),
        "provider_id": item.get("provider_id"),
        "registry_item_sha256": contract_registry.canonical_hash(item),
        "decision": item.get("review_status"),
        "issued_at": license_info.get("verified_at"),
    }
    if any(receipt.get(key) != value for key, value in expected.items()):
        return ["font provider consistency evidence does not match registry item"], None
    if (
        receipt["font"]["path"] != item.get("path")
        or receipt["font"]["sha256"] != item.get("sha256")
        or receipt["license"]["spdx"] != license_info.get("spdx")
        or receipt["license"]["evidence_url"] != license_info.get("evidence_url")
        or receipt["candidate"]["source_url"] != item.get("source_url")
    ):
        return ["font artifacts do not match registry item"], None
    candidate_hash = hashlib.sha256(receipt["candidate_id"].encode("utf-8")).hexdigest()[:16]
    expected_asset_id = (
        f"font-{item.get('provider_id')}-{candidate_hash}-{receipt['font']['sha256'][:16]}"
    )
    if item.get("asset_id") != expected_asset_id or re.fullmatch(
        r"font-(?:google-fonts|fontsource)-[0-9a-f]{16}-[0-9a-f]{16}",
        str(item.get("asset_id")),
    ) is None:
        return ["font asset_id does not match canonical identity"], None
    try:
        font_path = _safe_existing_project_file(
            Path(project_dir), receipt["font"]["path"], "font artifact"
        )
        license_path = _safe_existing_project_file(
            Path(project_dir), receipt["license"]["path"], "font license artifact"
        )
        raw = font_path.read_bytes()
        license_raw = license_path.read_bytes()
    except (OSError, AssetRegistryError):
        return ["font or license artifact is missing or unsafe"], None
    if (
        len(raw) != receipt["font"]["size"]
        or hashlib.sha256(raw).hexdigest() != receipt["font"]["sha256"]
        or len(license_raw) != receipt["license"]["size"]
        or hashlib.sha256(license_raw).hexdigest() != receipt["license"]["sha256"]
    ):
        return ["font or license artifact hash/size does not match receipt"], None
    if item.get("provider_id") == "google-fonts":
        expected_blob_sha, separator, _name = receipt["candidate_id"].partition(":")
        actual_blob_sha = hashlib.sha1(
            f"blob {len(raw)}\0".encode("ascii") + raw,
            usedforsecurity=False,
        ).hexdigest()
        if separator != ":" or actual_blob_sha != expected_blob_sha:
            return ["Google Fonts binary does not match pinned repository blob"], None
    try:
        _normalized, normalized_hash = validate_font_license_text(
            license_raw, receipt["license"]["spdx"]
        )
    except AssetRegistryError:
        return ["font license artifact is invalid"], None
    if normalized_hash != receipt["license"]["normalized_sha256"]:
        return ["font license normalized hash does not match receipt"], None
    if validate_font_bytes is None:
        return ["font validator capability is unavailable"], None
    try:
        import_result = validate_font_bytes(
            raw,
            receipt["validator"]["metadata"]["required_text_nfc"],
            license_spdx=receipt["license"]["spdx"],
            declared_mime=receipt["font"]["mime"],
        )
        if _font_validation_snapshot(import_result) != receipt["validator"]:
            return ["font validation evidence does not recompute"], None
        authority = project_required_font_text(Path(project_dir), item.get("asset_id"))
        if not isinstance(required_text, str):
            return ["font required text is invalid"], None
        import unicodedata

        current_text = unicodedata.normalize("NFC", authority + ("\n" if authority else "") + required_text)
        if len(current_text) > 100_000:
            return ["font required text exceeds validation limit"], None
        current_result = validate_font_bytes(
            raw,
            current_text,
            license_spdx=receipt["license"]["spdx"],
            declared_mime=receipt["font"]["mime"],
        )
    except (FontSecurityError, AssetRegistryError, UnicodeError, ValueError):
        return ["font validation or required glyph coverage failed"], None
    return [], current_result


def resolve_project_font(
    project_dir: Path, asset_id: str, required_text: str = ""
) -> dict[str, Any]:
    """Resolve one physically current v3 font; caller text can only expand coverage."""
    if not isinstance(asset_id, str) or re.fullmatch(
        r"font-(?:google-fonts|fontsource)-[0-9a-f]{16}-[0-9a-f]{16}", asset_id
    ) is None:
        raise AssetRegistryError("font asset_id is invalid")
    artifact = load_registry(Path(project_dir))
    matches = [item for item in artifact["items"] if item.get("asset_id") == asset_id]
    if len(matches) != 1 or matches[0].get("provider_id") not in _FONT_PROVIDER_IDS:
        raise AssetRegistryError("font asset is not registered")
    item = matches[0]
    static_errors = _provider_static_errors(item)
    if static_errors:
        raise AssetRegistryError("font provider item is invalid: " + "; ".join(static_errors))
    receipt = _load_font_provider_receipt(Path(project_dir), asset_id)
    errors, result = _font_receipt_consistency_errors(
        Path(project_dir), item, receipt, required_text=required_text
    )
    if errors or result is None:
        raise AssetRegistryError("font provider item is not current: " + "; ".join(errors))
    return {
        "asset_id": item["asset_id"],
        "path": item["path"],
        "sha256": item["sha256"],
        "family": result.family,
        "subfamily": result.subfamily,
        "style": result.style,
        "weight": result.weight,
        "coverage": {
            "unicode_coverage_count": result.receipt["unicode_coverage_count"],
            "unicode_coverage_sha256": result.receipt["unicode_coverage_sha256"],
            "required_coverage_sha256": result.receipt["required_coverage_sha256"],
        },
        "scripts": list(result.scripts),
        "license_spdx": item["license"]["spdx"],
        "provider_id": item["provider_id"],
        "receipt": receipt,
        "validation_receipt": dict(result.receipt),
        "required_glyphs": [dict(value) for value in result.required_glyphs],
        "ignored_characters": [dict(value) for value in result.ignored_characters],
    }


def list_project_fonts(project_dir: Path) -> list[dict[str, Any]]:
    """List only physically verified v3 fonts under current project text."""
    artifact = load_registry(Path(project_dir))
    fonts: list[dict[str, Any]] = []
    for item in artifact["items"]:
        if item.get("provider_id") not in _FONT_PROVIDER_IDS:
            continue
        try:
            fonts.append(resolve_project_font(Path(project_dir), item.get("asset_id", "")))
        except AssetRegistryError:
            continue
    return sorted(fonts, key=lambda value: value["asset_id"])


def current_font_provider_item(
    project_dir: Path,
    *,
    provider_id: str,
    candidate_id: str,
    query: str,
    download_url: str,
) -> dict | None:
    """Return an exact current v3 candidate, or fail on stale matching evidence."""
    if provider_id not in _FONT_PROVIDER_IDS:
        return None
    artifact = load_registry(Path(project_dir))
    expected_query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()
    expected_download_hash = hashlib.sha256(download_url.encode("utf-8")).hexdigest()
    for item in artifact["items"]:
        if item.get("provider_id") != provider_id:
            continue
        try:
            receipt = _load_font_provider_receipt(Path(project_dir), item["asset_id"])
        except (KeyError, AssetRegistryError):
            continue
        if (
            receipt.get("candidate_id") == candidate_id
            and receipt.get("query_hash") == expected_query_hash
            and receipt.get("download_url_sha256") == expected_download_hash
        ):
            resolve_project_font(Path(project_dir), item["asset_id"])
            return item
    return None


def _svg_receipt_consistency_errors(
    project_dir: Path, item: dict, receipt: dict
) -> list[str]:
    license_info = item.get("license") if isinstance(item, dict) else None
    if not isinstance(license_info, dict):
        return ["SVG provider license metadata is invalid"]
    expected = {
        "asset_id": item.get("asset_id"),
        "provider_id": item.get("provider_id"),
        "registry_item_sha256": contract_registry.canonical_hash(item),
        "license_spdx": license_info.get("spdx"),
        "license_url": license_info.get("evidence_url"),
        "decision": item.get("review_status"),
        "issued_at": license_info.get("verified_at"),
    }
    if any(receipt.get(key) != value for key, value in expected.items()):
        return ["SVG provider consistency evidence does not match registry item"]
    if (
        receipt["png"]["path"] != item.get("path")
        or receipt["png"]["sha256"] != item.get("sha256")
    ):
        return ["SVG PNG does not match registry item"]

    provider_id = item.get("provider_id")
    candidate_id = receipt["candidate_id"]
    safe_candidate = re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", candidate_id)
    if provider_id == "wikimedia-svg":
        safe_candidate = re.fullmatch(r"[1-9][0-9]{0,19}", candidate_id)
    if safe_candidate is None:
        return ["SVG candidate identity is invalid"]
    expected_asset_id = (
        f"provider-{provider_id}-{candidate_id}-{receipt['png']['sha256'][:16]}"
    )
    if item.get("asset_id") != expected_asset_id:
        return ["SVG candidate identity does not match registry item"]

    if provider_id in {"heroicons", "lucide", "tabler"}:
        try:
            from open_svg_providers import (
                heroicons_candidate,
                lucide_candidate,
                tabler_candidate,
            )

            builder = {
                "heroicons": heroicons_candidate,
                "lucide": lucide_candidate,
                "tabler": tabler_candidate,
            }[provider_id]
            candidate = builder(candidate_id)
        except Exception:
            return ["SVG repository candidate identity is invalid"]
        expected_download_hash = hashlib.sha256(
            candidate.download_url.encode("utf-8")
        ).hexdigest()
        expected_query_hash = hashlib.sha256(candidate_id.encode("utf-8")).hexdigest()
        if (
            receipt["download_url_sha256"] != expected_download_hash
            or receipt["query_hash"] != expected_query_hash
            or item.get("source_url") != candidate.landing_url
            or license_info.get("spdx") != candidate.license_spdx
            or license_info.get("evidence_url") != candidate.license_url
        ):
            return ["SVG repository candidate evidence does not match pinned provider"]

    for section in ("raw", "sanitized", "png"):
        data = receipt[section]
        try:
            artifact = _safe_existing_project_file(
                Path(project_dir), data["path"], f"SVG {section} artifact"
            )
            size = artifact.stat().st_size
            digest = _hash_file(artifact)
        except (OSError, AssetRegistryError):
            return [f"SVG {section} artifact is missing or unsafe"]
        if size != data["size"] or digest != data["sha256"]:
            return [f"SVG {section} artifact hash or size does not match consistency evidence"]
    try:
        png_bytes = _safe_existing_project_file(
            Path(project_dir), receipt["png"]["path"], "SVG PNG artifact"
        ).read_bytes()
        validate_png_bytes(
            png_bytes,
            expected_width=receipt["png"]["width"],
            expected_height=receipt["png"]["height"],
        )
    except (OSError, AssetRegistryError, SvgSecurityError):
        return ["SVG PNG artifact is invalid"]
    return []


def current_svg_provider_item(
    project_dir: Path,
    *,
    provider_id: str,
    candidate_id: str,
    query_hash: str,
    download_url: str,
) -> dict | None:
    """Return a physically current v2 SVG import without mutating the project."""
    if provider_id not in _SVG_PROVIDER_IDS:
        return None
    expected_download_hash = hashlib.sha256(download_url.encode("utf-8")).hexdigest()
    artifact = load_registry(Path(project_dir))
    for item in artifact["items"]:
        if item.get("provider_id") != provider_id:
            continue
        try:
            receipt = _load_svg_provider_receipt(Path(project_dir), item["asset_id"])
        except (KeyError, AssetRegistryError):
            continue
        if (
            receipt.get("candidate_id") == candidate_id
            and receipt.get("query_hash") == query_hash
            and receipt.get("download_url_sha256") == expected_download_hash
            and not _provider_identity_errors(item)
            and not _svg_receipt_consistency_errors(Path(project_dir), item, receipt)
        ):
            return item
    return None


def provider_consistency_errors(project_dir: Path, item: dict) -> list[str]:
    """Check built-in identity, path, license, and server-issued evidence."""
    if item.get("provider_id") in _FONT_PROVIDER_IDS:
        errors = _provider_static_errors(item)
        if errors:
            return errors
        try:
            receipt = _load_font_provider_receipt(Path(project_dir), item["asset_id"])
            receipt_errors, _result = _font_receipt_consistency_errors(
                Path(project_dir), item, receipt
            )
            return receipt_errors
        except (KeyError, OSError, ValueError, contract_registry.ContractError, AssetRegistryError):
            return ["font provider consistency evidence is invalid"]
    # SVG receipts use v2 because three separate files are security-relevant.
    if item.get("provider_id") in _SVG_PROVIDER_IDS:
        errors = _provider_static_errors(item)
        if errors:
            return errors
        try:
            receipt = _load_svg_provider_receipt(Path(project_dir), item["asset_id"])
            return _svg_receipt_consistency_errors(Path(project_dir), item, receipt)
        except (KeyError, OSError, ValueError, contract_registry.ContractError, AssetRegistryError):
            return ["SVG provider consistency evidence is invalid"]
    errors = _provider_static_errors(item)
    if errors:
        return errors
    try:
        receipt = _load_provider_receipt(Path(project_dir), item["asset_id"])
    except AssetRegistryError as exc:
        return [str(exc)]
    expected = {
        "asset_id": item["asset_id"],
        "provider_id": item["provider_id"],
        "path": item["path"],
        "asset_sha256": item["sha256"],
        "registry_item_sha256": contract_registry.canonical_hash(item),
        "license_spdx": item["license"]["spdx"],
        "license_url": item["license"]["evidence_url"],
        "issued_at": item["license"]["verified_at"],
    }
    if any(receipt.get(key) != value for key, value in expected.items()):
        return ["provider consistency evidence does not match registry item"]
    return []


def attribution_markdown(artifact: dict) -> str:
    """Render deterministic attribution for approved, attribution-required items."""
    _validate_artifact(artifact)
    items = [
        item
        for item in artifact["items"]
        if item.get("review_status") == "approved"
        and item.get("license", {}).get("attribution_required") is True
    ]
    items.sort(key=lambda value: _single_line(value.get("asset_id")))
    if not items:
        return _ATTRIBUTION_HEADER + _ATTRIBUTION_EMPTY

    lines = [_ATTRIBUTION_HEADER.rstrip("\n")]
    for item in items:
        license_info = item.get("license") or {}
        asset_id = _markdown_plain_text(item.get("asset_id"))
        spdx = _markdown_plain_text(license_info.get("spdx"))
        source_url = _markdown_plain_text(item.get("source_url")) or "(none)"
        attribution = _markdown_plain_text(license_info.get("attribution_text"))
        # All fields are single-line sanitized values; user text can therefore
        # never create a new Markdown bullet or heading.
        lines.extend(
            [
                f"- asset_id: `{asset_id}`",
                f"  - SPDX: {spdx}",
                f"  - Source URL: {source_url}",
                f"  - Attribution: {attribution}",
                "",
            ]
        )
    return "\n".join(lines)


def refresh_attribution(project_dir: Path, artifact: dict | None = None) -> Path:
    """Atomically refresh the project-root ``ATTRIBUTION.md`` projection."""
    project = Path(project_dir)
    if artifact is None:
        artifact = load_registry(project)
    else:
        _validate_artifact(artifact)
    path = _attribution_path(project)
    payload = attribution_markdown(artifact).encode("utf-8")
    _reject_symlink(path, "ATTRIBUTION.md")
    if path.exists() and path.is_file():
        try:
            if path.read_bytes() == payload:
                return path
        except OSError as exc:
            raise AssetRegistryError(f"cannot read ATTRIBUTION.md: {path}") from exc
    _atomic_write_bytes(path, payload, "ATTRIBUTION.md")
    return path


def attribution_errors(project_dir: Path, artifact: dict | None = None) -> list[str]:
    """Return final-gate errors for a missing, tampered, or symlinked projection."""
    project = Path(project_dir)
    try:
        if artifact is None:
            artifact = load_registry(project)
        else:
            _validate_artifact(artifact)
        expected = attribution_markdown(artifact).encode("utf-8")
    except AssetRegistryError as exc:
        return [str(exc)]

    path = _attribution_path(project)
    if path.is_symlink():
        return ["ATTRIBUTION.md must not be a symlink"]
    if not path.exists():
        return ["ATTRIBUTION.md is missing"]
    if not path.is_file():
        return ["ATTRIBUTION.md is not a regular file"]
    try:
        actual = path.read_bytes()
    except OSError as exc:
        return [f"cannot read ATTRIBUTION.md: {exc}"]
    if actual != expected:
        return ["ATTRIBUTION.md does not match deterministic attribution"]
    return []


__all__ = [
    "ATTRIBUTION_REL",
    "AssetRegistryError",
    "PROVENANCE_REL",
    "PROVIDER_RECEIPTS_REL",
    "attribution_errors",
    "attribution_markdown",
    "auto_license_errors",
    "current_font_provider_item",
    "current_svg_provider_item",
    "current_item",
    "list_project_fonts",
    "load_registry",
    "migrate_legacy_registry",
    "provider_consistency_errors",
    "project_required_font_text",
    "refresh_attribution",
    "restore_publication",
    "save_registry",
    "save_provider_receipt",
    "save_font_provider_receipt",
    "save_svg_provider_receipt",
    "snapshot_publication",
    "upsert_item",
    "validate_font_license_text",
    "resolve_project_font",
]
