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
import os
import re
import uuid
from pathlib import Path
from typing import Any

import contract_registry
from open_asset_providers import canonical_license_url


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
        "Unlicense",
        "OFL-1.1",
        "internal-original",
    }
)
_BUILTIN_PROVIDER_PREFIXES = {
    "openverse": "assets/providers/openverse/",
    "wikimedia": "assets/providers/wikimedia/",
}
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


def _provider_static_errors(item: dict) -> list[str]:
    errors = auto_license_errors(item)
    provider_id = item.get("provider_id")
    prefix = _BUILTIN_PROVIDER_PREFIXES.get(provider_id)
    if prefix is None:
        errors.append("provider_id is not a built-in provider")
    elif not isinstance(item.get("path"), str) or not item["path"].startswith(prefix):
        errors.append("provider asset path does not match provider_id")
    license_info = item.get("license") or {}
    if canonical_license_url(
        license_info.get("evidence_url"), str(license_info.get("spdx") or "")
    ) is None:
        errors.append("provider license evidence is not canonical")
    return errors


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


def _load_provider_receipt(project_dir: Path, asset_id: str) -> dict:
    path = _provider_receipt_path(Path(project_dir), asset_id)
    _reject_parent_symlink(path, "provider consistency evidence")
    _reject_symlink(path, "provider consistency evidence")
    if not path.is_file():
        raise AssetRegistryError("provider consistency evidence is missing")
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


def provider_consistency_errors(project_dir: Path, item: dict) -> list[str]:
    """Check built-in identity, path, license, and server-issued evidence."""
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
    "current_item",
    "load_registry",
    "migrate_legacy_registry",
    "provider_consistency_errors",
    "refresh_attribution",
    "save_registry",
    "save_provider_receipt",
    "upsert_item",
]
