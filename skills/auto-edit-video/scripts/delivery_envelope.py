#!/usr/bin/env python3
"""Hash-bound publication for the direct final renderer route.

The renderer produces private bytes first.  This module is the only place
that turns those bytes into a public direct delivery: a prepared envelope is
validated against staged bytes, every destination is journaled and published,
and a finalized envelope is written last.  A journal is deliberately kept
when recovery cannot prove that a destination is still ours.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
import hashlib
import fcntl
import json
import os
import re
import shutil
import stat
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import contract_registry


DELIVERY_REL = Path("working/delivery_envelopes")
STAGING_REL = DELIVERY_REL / ".staging"
LOCKS_REL = STAGING_REL / ".locks"
QUARANTINE_REL = DELIVERY_REL / ".quarantine"
JOURNAL_NAME = "publication_journal.json"
DEFERRED_NAME = "deferred_handoff.json"
DEFERRED_MARKER_KEYS = {
    "schema_version",
    "state",
    "render_id",
    "transaction_id",
    "expected_output",
    "journal_sha256",
    "output_sha256",
    "finalized_sha256",
    "binding_sha256",
}
DEFERRED_MARKER_STATES = {"pending", "committed"}
ROUTES = ("direct", "single", "batch")
PREPARED_NAME = "prepared.json"
FINALIZED_NAME = "{render_id}.json"
ARTIFACT_NAMES = (
    "output",
    "qa_report",
    "contact_sheet",
    "visual_evidence",
    "motion_evidence",
    "caption_v2",
    "audio_event_plan",
    "audio_catalog",
    "sfx_stem",
)
DEFAULT_DESTINATIONS = {
    "qa_report": "qa/{render_id}.json",
    "contact_sheet": "qa/{render_id}-contact.png",
    "visual_evidence": "working/render_visual_evidence/{render_id}.json",
    "motion_evidence": "working/render_visual_evidence/{render_id}.json",
    "caption_v2": "working/caption_delivery_v2.json",
    "audio_event_plan": "working/audio_event_plans/{render_id}.json",
    "audio_catalog": "working/audio_catalogs/{render_id}.json",
    "sfx_stem": "working/sfx_stems/{render_id}.wav",
}
STAGE_FILENAMES = {
    "output": "candidate.mp4",
    "qa_report": "qa_report.json",
    "contact_sheet": "contact_sheet.png",
    "visual_evidence": "visual_evidence.json",
    "motion_evidence": "visual_evidence.json",
    "caption_v2": "caption_v2.json",
    "audio_event_plan": "audio_event_plan.json",
    "audio_catalog": "audio_catalog.json",
    "sfx_stem": "sfx_stem.wav",
}
JOURNAL_KEYS = {"schema_version", "render_id", "entries"}
JOURNAL_ENTRY_KEYS = {
    "destination",
    "external",
    "new_sha256",
    "prior_exists",
    "prior_sha256",
    "backup",
}
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
RENDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,120}$")
RECOVERY_ARTIFACT_NAMES = (
    "output",
    "qa_report",
    "contact_sheet",
    "visual_evidence",
    "motion_evidence",
    "audio_event_plan",
    "audio_catalog",
    "sfx_stem",
)


@dataclass(frozen=True, slots=True)
class StagingAttempt(os.PathLike[str]):
    project_dir: Path
    render_id: str
    stage_dir: Path
    _owner_token: object = field(repr=False, compare=False)

    def __fspath__(self) -> str:
        return os.fspath(self.stage_dir)

    def __truediv__(self, other: str | os.PathLike[str]) -> Path:
        return self.stage_dir / other

    def __getattr__(self, name: str) -> Any:
        return getattr(self.stage_dir, name)


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    """One regular file observed through a stable no-follow descriptor."""

    path: Path
    label: str
    sha256: str
    size: int
    device: int
    inode: int
    mtime_ns: int
    ctime_ns: int
    payload: bytes | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class FinalizedDeliverySnapshot:
    """Context-bound bytes and identities accepted by a final consumer."""

    project_dir: Path
    expected_output: Path
    envelope_path: Path
    render_id: str
    envelope: dict[str, Any]
    envelope_sha256: str
    output_sha256: str
    state_revision: str
    profile_id: str
    resolved_profile_hash: str
    cut_map_sha256: str | None
    files: tuple[FileSnapshot, ...]
    editor_state: dict[str, Any] = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class DeferredPublication:
    """Uncommitted publication owned by one live render attempt.

    The owner token is an in-process object capability.  It is deliberately
    neither serializable nor included in renderer/public command payloads.
    """

    project_dir: Path
    render_id: str
    stage_dir: Path
    expected_output: Path
    finalized: dict[str, Any] = field(repr=False, compare=False)
    _transaction_id: str = field(repr=False, compare=False)
    _owner_token: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class _ActiveStagingLease:
    descriptor: int
    owner_token: object = field(repr=False, compare=False)


_ACTIVE_STAGING_LEASES: dict[tuple[str, str], _ActiveStagingLease] = {}


class DeliveryEnvelopeError(RuntimeError):
    """Raised when a direct delivery cannot be proven safe to publish."""


def _safe_directory(path: Path, *, create: bool) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if not create:
            raise DeliveryEnvelopeError(f"required delivery directory is missing: {path}")
        try:
            path.mkdir(mode=0o700)
        except OSError as exc:
            raise DeliveryEnvelopeError(
                f"delivery directory could not be created safely: {path}: {exc}"
            ) from exc
        metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise DeliveryEnvelopeError(f"delivery directory is not an owned directory: {path}")
    if path.resolve() != path:
        raise DeliveryEnvelopeError(f"delivery directory is aliased: {path}")


def _ensure_staging_trust_chain(project_dir: Path, *, create: bool) -> Path:
    root = project_dir.resolve()
    _safe_directory(root, create=False)
    for relative in (
        Path("working"),
        DELIVERY_REL,
        STAGING_REL,
        LOCKS_REL,
    ):
        _safe_directory(root / relative, create=create)
    return root


def _stage_identity(stage_dir: Path) -> tuple[Path, str, Path]:
    stage = Path(stage_dir)
    if not stage.is_absolute():
        stage = (Path.cwd() / stage).absolute()
    if len(stage.parents) < 4:
        raise DeliveryEnvelopeError("staging path has no canonical project parent")
    root = stage.parents[3]
    render_id = stage.name
    expected = root / STAGING_REL / render_id
    if stage != expected or staging_path(root, render_id) != stage:
        raise DeliveryEnvelopeError("staging path is not canonical")
    _ensure_staging_trust_chain(root, create=False)
    try:
        metadata = stage.lstat()
    except FileNotFoundError:
        return root, render_id, stage
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise DeliveryEnvelopeError("staging path is not an owned directory")
    if stage.resolve() != stage:
        raise DeliveryEnvelopeError("staging path is aliased")
    return root, render_id, stage


def _lease_key(project_dir: Path, render_id: str) -> tuple[str, str]:
    return (str(project_dir.resolve()), render_id)


def _acquire_staging_lease(project_dir: Path, render_id: str) -> object:
    root = _ensure_staging_trust_chain(project_dir, create=True)
    staging_path(root, render_id)
    key = _lease_key(root, render_id)
    if key in _ACTIVE_STAGING_LEASES:
        raise DeliveryEnvelopeError(f"render staging is already active: {render_id}")
    lock_path = root / LOCKS_REL / f"{render_id}.lock"
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise DeliveryEnvelopeError(f"render staging lock is unsafe: {lock_path}: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        linked = lock_path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(linked.st_mode)
            or not stat.S_ISREG(linked.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino)
        ):
            raise DeliveryEnvelopeError(f"render staging lock is not an owned regular file: {lock_path}")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _ensure_staging_trust_chain(root, create=False)
    except (OSError, DeliveryEnvelopeError) as exc:
        os.close(descriptor)
        if isinstance(exc, DeliveryEnvelopeError):
            raise
        raise DeliveryEnvelopeError(f"render staging is already active: {render_id}") from exc
    owner_token = object()
    _ACTIVE_STAGING_LEASES[key] = _ActiveStagingLease(
        descriptor=descriptor,
        owner_token=owner_token,
    )
    return owner_token


def _release_staging_lease(
    project_dir: Path,
    render_id: str,
    owner_token: object,
) -> None:
    key = _lease_key(project_dir, render_id)
    lease = _ACTIVE_STAGING_LEASES.get(key)
    if lease is None or lease.owner_token is not owner_token:
        raise DeliveryEnvelopeError(f"staging attempt authority is stale or invalid: {render_id}")
    _ACTIVE_STAGING_LEASES.pop(key)
    try:
        fcntl.flock(lease.descriptor, fcntl.LOCK_UN)
    finally:
        os.close(lease.descriptor)


def _require_staging_lease(
    project_dir: Path,
    render_id: str,
    owner_token: object,
) -> None:
    lease = _ACTIVE_STAGING_LEASES.get(_lease_key(project_dir, render_id))
    if lease is None or lease.owner_token is not owner_token:
        raise DeliveryEnvelopeError(f"staging attempt authority is stale or invalid: {render_id}")


def _validate_staging_attempt(
    project_dir: Path,
    render_id: str,
    authority: StagingAttempt | None,
) -> StagingAttempt:
    root = project_dir.resolve()
    if not isinstance(authority, StagingAttempt):
        raise DeliveryEnvelopeError(f"staging attempt authority is required: {render_id}")
    if (
        authority.project_dir != root
        or authority.render_id != render_id
        or authority.stage_dir != staging_path(root, render_id)
    ):
        raise DeliveryEnvelopeError(f"staging attempt authority does not match: {render_id}")
    _require_staging_lease(root, render_id, authority._owner_token)
    return authority


def _validate_deferred_publication(
    authority: DeferredPublication,
    *,
    expected_state: str = "pending",
) -> DeferredPublication:
    if not isinstance(authority, DeferredPublication):
        raise DeliveryEnvelopeError("deferred publication authority is required")
    root = authority.project_dir.resolve()
    if (
        authority.project_dir != root
        or authority.stage_dir != staging_path(root, authority.render_id)
        or not authority.expected_output.is_absolute()
    ):
        raise DeliveryEnvelopeError(
            f"deferred publication authority does not match: {authority.render_id}"
        )
    _require_staging_lease(root, authority.render_id, authority._owner_token)
    stage_root, stage_render_id, stage = _stage_identity(authority.stage_dir)
    if stage_root != root or stage_render_id != authority.render_id or stage != authority.stage_dir:
        raise DeliveryEnvelopeError(
            f"deferred publication authority does not match: {authority.render_id}"
        )
    marker, _plan = _validated_deferred_marker(
        root,
        stage,
        authority.render_id,
        expected_output=authority.expected_output,
        expected_state=expected_state,
    )
    if (
        marker["transaction_id"] != authority._transaction_id
        or marker["finalized_sha256"] != _sha256_bytes(_json_bytes(authority.finalized))
    ):
        raise DeliveryEnvelopeError(
            f"deferred publication marker authority is invalid: {authority.render_id}"
        )
    return authority


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_regular_file(
    path: Path,
    *,
    label: str,
    capture_bytes: bool = False,
) -> FileSnapshot:
    """Hash one pathname exactly once, rejecting link and in-read identity changes."""
    entry = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(entry, flags)
        before = os.fstat(descriptor)
        linked = entry.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(linked.st_mode)
            or not stat.S_ISREG(linked.st_mode)
            or (before.st_dev, before.st_ino) != (linked.st_dev, linked.st_ino)
        ):
            raise DeliveryEnvelopeError(f"{label} is not an owned regular file: {entry}")
        digest = hashlib.sha256()
        chunks: list[bytes] | None = [] if capture_bytes else None
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
            if chunks is not None:
                chunks.append(chunk)
        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if identity_after != identity_before or total != before.st_size:
            raise DeliveryEnvelopeError(f"{label} changed while it was read: {entry}")
        return FileSnapshot(
            path=entry,
            label=label,
            sha256=digest.hexdigest(),
            size=total,
            device=before.st_dev,
            inode=before.st_ino,
            mtime_ns=before.st_mtime_ns,
            ctime_ns=before.st_ctime_ns,
            payload=b"".join(chunks) if chunks is not None else None,
        )
    except DeliveryEnvelopeError:
        raise
    except OSError as exc:
        raise DeliveryEnvelopeError(
            f"{label} could not be opened safely: {entry}: {exc}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def revalidate_file_snapshot(snapshot: FileSnapshot) -> None:
    """Compare the current pathname to a previously captured file identity."""
    current = _snapshot_regular_file(snapshot.path, label=snapshot.label)
    if (
        current.sha256 != snapshot.sha256
        or current.size != snapshot.size
        or current.device != snapshot.device
        or current.inode != snapshot.inode
        or current.mtime_ns != snapshot.mtime_ns
        or current.ctime_ns != snapshot.ctime_ns
    ):
        raise DeliveryEnvelopeError(
            f"{snapshot.label} changed after verification: {snapshot.path}"
        )


def _decode_json_snapshot(snapshot: FileSnapshot) -> dict[str, Any]:
    if snapshot.payload is None:
        raise DeliveryEnvelopeError(f"{snapshot.label} bytes were not captured")
    try:
        value = contract_registry.load_artifact_text(snapshot.payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, contract_registry.ContractError) as exc:
        raise DeliveryEnvelopeError(f"{snapshot.label} JSON is unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise DeliveryEnvelopeError(f"{snapshot.label} must be a JSON object")
    return value


def snapshot_owned_json(
    project_dir: Path,
    relative: str,
    *,
    label: str,
) -> tuple[dict[str, Any], FileSnapshot]:
    """Capture a canonical project JSON file for a later compare-and-swap check."""
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise DeliveryEnvelopeError(f"{label} path is invalid")
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise DeliveryEnvelopeError(f"{label} path is invalid")
    root = project_dir.resolve()
    _safe_directory(root, create=False)
    current = root
    for part in parts[:-1]:
        current = current / part
        _safe_directory(current, create=False)
    path = root / Path(*parts)
    if path.resolve() != path:
        raise DeliveryEnvelopeError(f"{label} path is aliased")
    snapshot = _snapshot_regular_file(path, label=label, capture_bytes=True)
    return _decode_json_snapshot(snapshot), snapshot


def snapshot_project_file(
    project_dir: Path,
    relative: str,
    *,
    label: str,
    capture_bytes: bool = False,
) -> FileSnapshot:
    """Capture one project-relative regular file through the owned trust chain."""
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise DeliveryEnvelopeError(f"{label} path is invalid")
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise DeliveryEnvelopeError(f"{label} path is invalid")
    root = project_dir.resolve()
    _safe_directory(root, create=False)
    current = root
    for part in parts[:-1]:
        current = current / part
        _safe_directory(current, create=False)
    path = root / Path(*parts)
    if path.resolve() != path:
        raise DeliveryEnvelopeError(f"{label} path is aliased")
    return _snapshot_regular_file(path, label=label, capture_bytes=capture_bytes)


def validate_mosaic_registry_snapshot(
    registry: dict[str, Any], descriptors: list[dict[str, Any]]
) -> None:
    """Bind frozen mosaic descriptors to one valid, currently approved registry."""
    errors = contract_registry.validate_artifact("asset_provenance", registry)
    if errors:
        raise DeliveryEnvelopeError(
            "mosaic provenance registry failed validation: " + "; ".join(errors)
        )
    items = registry.get("items")
    if not isinstance(items, list):
        raise DeliveryEnvelopeError("mosaic provenance registry items are invalid")
    import asset_registry

    for descriptor in descriptors:
        matches = [
            item
            for item in items
            if isinstance(item, dict) and item.get("asset_id") == descriptor.get("asset_id")
        ]
        if (
            len(matches) != 1
            or matches[0].get("path") != descriptor.get("path")
            or matches[0].get("sha256") != descriptor.get("sha256")
        ):
            raise DeliveryEnvelopeError(
                "mosaic descriptor does not match the provenance registry"
            )
        license_errors = asset_registry.auto_license_errors(matches[0])
        if license_errors:
            raise DeliveryEnvelopeError(
                "mosaic asset is not approved: " + "; ".join(license_errors)
            )


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode(
        "utf-8"
    )


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(_json_bytes(payload))
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        os.fsync(descriptor)
    except OSError as exc:
        raise DeliveryEnvelopeError(
            f"delivery directory state could not be made durable: {path}: {exc}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = contract_registry.load_artifact_text(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DeliveryEnvelopeError(f"delivery envelope JSON is unreadable: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DeliveryEnvelopeError(f"delivery envelope must be an object: {path}")
    return value


def staging_path(project_dir: Path, render_id: str) -> Path:
    if not isinstance(render_id, str) or not RENDER_ID_PATTERN.fullmatch(render_id):
        raise DeliveryEnvelopeError("render_id is invalid")
    return project_dir.resolve() / STAGING_REL / render_id


def finalized_path(project_dir: Path, render_id: str) -> Path:
    return project_dir.resolve() / DELIVERY_REL / FINALIZED_NAME.format(render_id=render_id)


def _project_relative(project_dir: Path, path: Path, *, label: str) -> str:
    root = project_dir.resolve()
    entry = path.expanduser()
    if entry.is_symlink():
        raise DeliveryEnvelopeError(f"{label} must not be a symlink")
    resolved = entry.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise DeliveryEnvelopeError(f"{label} must be inside the project") from exc
    if not relative.parts or any(part in {".", ".."} for part in relative.parts):
        raise DeliveryEnvelopeError(f"{label} path is not normalized")
    return relative.as_posix()


def _destination_path(
    project_dir: Path,
    relative: str,
    *,
    allow_external: bool = False,
    allow_leaf_conflict: bool = False,
) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise DeliveryEnvelopeError("artifact path must be a project-relative POSIX path")
    if allow_external and relative.startswith("/"):
        entry = Path(relative).expanduser()
        if allow_leaf_conflict:
            if not entry.is_absolute():
                raise DeliveryEnvelopeError("external artifact path must be absolute")
            _safe_directory(entry.parent, create=False)
            return entry
        if entry.is_symlink():
            raise DeliveryEnvelopeError(f"artifact destination must not be a symlink: {relative}")
        destination = entry.resolve()
        return destination
    if relative.startswith("/"):
        raise DeliveryEnvelopeError("artifact path must be a project-relative POSIX path")
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise DeliveryEnvelopeError("artifact path must be normalized")
    root = project_dir.resolve()
    entry = root / Path(*parts)
    if allow_leaf_conflict:
        current = root
        for part in parts[:-1]:
            current = current / part
            _safe_directory(current, create=False)
        return entry
    if entry.is_symlink():
        raise DeliveryEnvelopeError(f"artifact destination must not be a symlink: {relative}")
    destination = entry.resolve()
    try:
        destination.relative_to(root)
    except ValueError as exc:
        raise DeliveryEnvelopeError("artifact path escapes the project") from exc
    if destination.is_symlink():
        raise DeliveryEnvelopeError(f"artifact destination must not be a symlink: {relative}")
    return destination


def _artifact_record(
    project_dir: Path,
    source: Path,
    destination: str,
    *,
    allow_external: bool = False,
) -> dict[str, Any]:
    if source.is_symlink() or not source.is_file():
        raise DeliveryEnvelopeError(f"staged artifact is not an owned regular file: {source}")
    _destination_path(project_dir, destination, allow_external=allow_external)
    return {
        "path": destination,
        "sha256": _sha256(source),
        "bytes": source.stat().st_size,
    }


def _validate_artifact_bytes(
    project_dir: Path,
    envelope: dict[str, Any],
    sources: Mapping[str, Path] | None,
) -> None:
    artifacts = envelope.get("artifacts")
    if not isinstance(artifacts, dict):
        raise DeliveryEnvelopeError("delivery envelope artifacts are invalid")
    for name in ARTIFACT_NAMES:
        item = artifacts.get(name)
        if item is None:
            continue
        if not isinstance(item, dict):
            raise DeliveryEnvelopeError(f"artifact {name} is invalid")
        path = item.get("path")
        if not isinstance(path, str):
            raise DeliveryEnvelopeError(f"artifact {name} path is invalid")
        source = (sources or {}).get(name)
        actual = source if source is not None else _destination_path(
            project_dir, path, allow_external=(name == "output")
        )
        if actual.is_symlink() or not actual.is_file():
            raise DeliveryEnvelopeError(f"artifact {name} bytes are missing: {actual}")
        actual_sha = _sha256(actual)
        actual_bytes = actual.stat().st_size
        if actual_sha != item.get("sha256") or actual_bytes != item.get("bytes"):
            raise DeliveryEnvelopeError(
                f"artifact {name} hash/size mismatch for {path}"
            )


def _validate_envelope_structure(
    envelope: dict[str, Any], *, expected_state: str | None = None
) -> None:
    errors = contract_registry.validate_artifact("delivery_envelope", envelope)
    if errors:
        raise DeliveryEnvelopeError("delivery envelope contract failed: " + "; ".join(errors))
    if expected_state is not None and envelope.get("state") != expected_state:
        raise DeliveryEnvelopeError(
            f"delivery envelope state must be {expected_state}, got {envelope.get('state')}"
        )
    if envelope.get("state") == "finalized":
        prepared = json.loads(json.dumps(envelope))
        prepared["state"] = "prepared"
        prepared["prepared_envelope_hash"] = None
        expected_prepared_hash = contract_registry.canonical_hash(prepared)
        if envelope.get("prepared_envelope_hash") != expected_prepared_hash:
            raise DeliveryEnvelopeError(
                "finalized delivery prepared envelope hash does not match its canonical lineage"
            )


def validate_envelope(
    project_dir: Path,
    envelope: dict[str, Any],
    *,
    sources: Mapping[str, Path] | None = None,
    expected_state: str | None = None,
) -> None:
    _validate_envelope_structure(envelope, expected_state=expected_state)
    _validate_artifact_bytes(project_dir, envelope, sources)


def _profile_binding(
    project_dir: Path, state: dict[str, Any]
) -> tuple[str, str, FileSnapshot | None]:
    import director_resolver

    director_id = str(state.get("director_style") or "teacher-punch")
    persisted_path = project_dir.resolve() / "working/resolved_director_profile.json"
    try:
        persisted_path.lstat()
    except FileNotFoundError:
        persisted = None
        persisted_snapshot = None
    else:
        persisted, persisted_snapshot = snapshot_owned_json(
            project_dir,
            "working/resolved_director_profile.json",
            label="resolved director profile",
        )
    try:
        if persisted is None:
            resolved = director_resolver.resolve_director_profile(director_id)
        else:
            if persisted.get("profile_id") != director_id:
                raise DeliveryEnvelopeError(
                    "persisted director profile does not match editor state"
                )
            overrides = persisted.get("overrides")
            if not isinstance(overrides, dict):
                raise DeliveryEnvelopeError("persisted director profile overrides are invalid")
            resolved = director_resolver.resolve_director_profile(
                director_id,
                overrides=overrides,
            )
            if resolved != persisted:
                raise DeliveryEnvelopeError(
                    "persisted director profile is stale or not canonical"
                )
    except Exception as exc:
        if isinstance(exc, DeliveryEnvelopeError):
            raise
        raise DeliveryEnvelopeError(f"director profile could not be resolved: {director_id}") from exc
    profile_hash = resolved.get("resolved_hash")
    if not isinstance(profile_hash, str):
        raise DeliveryEnvelopeError("director profile has no resolved hash")
    return director_id, profile_hash, persisted_snapshot


def _renderer_identity(renderer_script: Path, ffmpeg_executable: Path) -> dict[str, Any]:
    if renderer_script.is_symlink() or not renderer_script.is_file():
        raise DeliveryEnvelopeError("renderer script is missing or not owned")
    if ffmpeg_executable.is_symlink():
        ffmpeg_executable = ffmpeg_executable.resolve()
    if not ffmpeg_executable.is_file():
        raise DeliveryEnvelopeError("ffmpeg executable is missing")
    return {
        "name": "render_editor_timeline",
        "contract_version": 1,
        "script_sha256": _sha256(renderer_script),
        "ffmpeg_executable_sha256": _sha256(ffmpeg_executable),
    }


def _default_destination(name: str, render_id: str, output: Path, project_dir: Path) -> str:
    if name == "output":
        if output.is_symlink():
            raise DeliveryEnvelopeError("direct output must not be a symlink")
        return str(output.expanduser().resolve())
    template = DEFAULT_DESTINATIONS.get(name)
    if template is None:
        raise DeliveryEnvelopeError(f"no destination declared for artifact {name}")
    return template.format(render_id=render_id)


def build_prepared_envelope(
    project_dir: Path,
    render_id: str,
    output: Path,
    state: dict[str, Any],
    staged_sources: Mapping[str, Path],
    *,
    renderer_script: Path,
    ffmpeg_executable: Path,
    destinations: Mapping[str, str] | None = None,
    visual_authority: Mapping[str, Any] | None = None,
    route: str = "direct",
) -> dict[str, Any]:
    """Build a prepared v1 payload from observed private artifact bytes.

    ``route`` records which publisher minted the envelope.  Every route shares
    this staging/journal/CAS machinery; only the label differs.
    """
    if route not in ROUTES:
        raise DeliveryEnvelopeError(f"unsupported delivery route: {route}")
    root = project_dir.resolve()
    sfx_names = ("audio_event_plan", "audio_catalog", "sfx_stem")
    included_sfx = [name for name in sfx_names if staged_sources.get(name) is not None]
    if included_sfx and len(included_sfx) != len(sfx_names):
        raise DeliveryEnvelopeError("SFX delivery artifacts must be all-or-none")
    profile_id, profile_hash, _profile_snapshot = _profile_binding(root, state)
    import editor_server

    cut_map = root / "working/cut_map.json"
    if cut_map.is_symlink():
        raise DeliveryEnvelopeError("working/cut_map.json must not be a symlink")
    sfx_names = ("audio_event_plan", "audio_catalog", "sfx_stem")
    if all(staged_sources.get(name) is not None for name in sfx_names):
        import sfx_delivery

        cut_map_hash = sfx_delivery.effective_cut_map_sha256(root, state)
    else:
        cut_map_hash = _sha256(cut_map) if cut_map.is_file() else None
    artifact_payload: dict[str, Any] = {}
    source_snapshots: dict[Path, FileSnapshot] = {}
    visual_evidence_snapshot: FileSnapshot | None = None
    destination_overrides = dict(destinations or {})
    stage_root = staging_path(root, render_id).resolve()
    for name in ARTIFACT_NAMES:
        source = staged_sources.get(name)
        if source is None:
            artifact_payload[name] = None
            continue
        source_path = Path(source)
        if source_path.is_symlink() or not source_path.is_file():
            raise DeliveryEnvelopeError(f"staged artifact is not an owned regular file: {source_path}")
        try:
            source_path.resolve().relative_to(stage_root)
        except ValueError as exc:
            raise DeliveryEnvelopeError("staged artifact must be under the render staging directory") from exc
        destination = destination_overrides.get(name) or _default_destination(
            name, render_id, output, root
        )
        _destination_path(root, destination, allow_external=(name == "output"))
        source_snapshot = source_snapshots.get(source_path)
        if source_snapshot is None:
            source_snapshot = _snapshot_regular_file(
                source_path,
                label=f"staged artifact {name}",
                capture_bytes=(name == "visual_evidence"),
            )
            source_snapshots[source_path] = source_snapshot
        artifact_payload[name] = {
            "path": destination,
            "sha256": source_snapshot.sha256,
            "bytes": source_snapshot.size,
        }
        if name == "visual_evidence":
            visual_evidence_snapshot = source_snapshot

    verified_visual_authority: dict[str, Any] | None = None
    if visual_authority is not None:
        if visual_evidence_snapshot is None:
            raise DeliveryEnvelopeError(
                "visual authority requires staged visual evidence"
            )
        visual_report = _decode_json_snapshot(visual_evidence_snapshot)
        reported_authority = visual_report.get("authority")
        if not isinstance(reported_authority, dict):
            raise DeliveryEnvelopeError(
                "staged visual authority is missing or malformed"
            )
        try:
            supplied_authority = dict(visual_authority)
        except (TypeError, ValueError) as exc:
            raise DeliveryEnvelopeError("supplied visual authority is invalid") from exc
        if _json_bytes(reported_authority) != _json_bytes(supplied_authority):
            raise DeliveryEnvelopeError(
                "staged visual authority differs from frozen render authority"
            )
        declared_authority_hash = reported_authority.get("authority_hash")
        authority_material = {
            key: value
            for key, value in reported_authority.items()
            if key != "authority_hash"
        }
        if (
            not isinstance(declared_authority_hash, str)
            or contract_registry.canonical_hash(authority_material)
            != declared_authority_hash
        ):
            raise DeliveryEnvelopeError(
                "staged visual authority canonical hash is invalid"
            )
        verified_visual_authority = reported_authority

    prepared = {
        "schema_version": 1,
        "route": route,
        "render_id": render_id,
        "state": "prepared",
        "quality": "final",
        "profile": {"id": profile_id, "resolved_profile_hash": profile_hash},
        "timeline": {
            "editor_state_revision": editor_server.editor_state_revision(state),
            "cut_map_sha256": cut_map_hash,
        },
        "artifacts": artifact_payload,
        "renderer_identity": _renderer_identity(renderer_script, ffmpeg_executable),
        "prepared_envelope_hash": None,
    }
    if verified_visual_authority is not None:
        prepared["visual_authority"] = {
            key: verified_visual_authority.get(key)
            for key in (
                "schema_version",
                "source",
                "visual_plan_revision",
                "visual_plan_sha256",
                "structured_layers_sha256",
                "artifact_index_sha256",
                "authority_hash",
            )
        }
    validate_envelope(root, prepared, sources=staged_sources, expected_state="prepared")
    return prepared


def build_batch_envelope(
    project_dir: Path,
    batch_id: str,
    archive: Path,
    receipt_relative: str,
    state: dict[str, Any],
    members: Sequence[Mapping[str, Any]],
    *,
    renderer_script: Path,
    ffmpeg_executable: Path,
) -> dict[str, Any]:
    """Bind member order, member output bytes and archive contents.

    ``members`` carries the batch order itself: each entry declares a
    ``clip_id``, a ``render_id`` and its ``archive_name``, and the 1-based
    index is taken from that sequence, not from the caller.  Every binding
    in the returned envelope is read back off disk here — the caller's own
    digests are never trusted — so a member whose published bytes, whose
    finalized member envelope, or whose stored archive entry disagrees with
    the batch can never reach a finalized aggregate.
    """
    root = project_dir.resolve()
    if not RENDER_ID_PATTERN.fullmatch(batch_id):
        raise DeliveryEnvelopeError(f"batch identity is invalid: {batch_id}")
    if not members:
        raise DeliveryEnvelopeError("a batch envelope requires at least one member")
    import editor_server

    archive_path = _canonical_expected_output(Path(archive))
    if not archive_path.is_file():
        raise DeliveryEnvelopeError("batch archive bytes are missing")

    member_payload: list[dict[str, Any]] = []
    expected_names: list[str] = []
    for position, member in enumerate(members):
        render_id = str(member.get("render_id") or "")
        if not RENDER_ID_PATTERN.fullmatch(render_id):
            raise DeliveryEnvelopeError(f"batch member identity is invalid: {render_id}")
        member_envelope_path = finalized_path(root, render_id)
        member_envelope = _read_json(member_envelope_path)
        _validate_envelope_structure(member_envelope, expected_state="finalized")
        if member_envelope.get("route") != "batch":
            raise DeliveryEnvelopeError(
                f"batch member {render_id} was not published through the batch route"
            )
        output_item = (member_envelope.get("artifacts") or {}).get("output")
        if not isinstance(output_item, dict) or not isinstance(output_item.get("path"), str):
            raise DeliveryEnvelopeError(f"batch member {render_id} binds no output")
        published = _destination_path(root, str(output_item["path"]), allow_external=True)
        published_snapshot = _snapshot_regular_file(
            published, label=f"batch member output {render_id}"
        )
        if (
            published_snapshot.sha256 != output_item.get("sha256")
            or published_snapshot.size != output_item.get("bytes")
        ):
            raise DeliveryEnvelopeError(
                f"batch member {render_id} published output does not match its envelope"
            )
        archive_name = str(member.get("archive_name") or "")
        expected_names.append(archive_name)
        member_payload.append(
            {
                "index": position + 1,
                "clip_id": str(member.get("clip_id") or ""),
                "render_id": render_id,
                "envelope_sha256": _sha256(member_envelope_path),
                "output": {
                    "path": _project_relative(
                        root, published, label=f"batch member output {render_id}"
                    ),
                    "sha256": published_snapshot.sha256,
                    "bytes": published_snapshot.size,
                },
                "archive_name": archive_name,
                "archive_sha256": published_snapshot.sha256,
            }
        )

    with zipfile.ZipFile(archive_path, "r") as bundle:
        stored = [info.filename for info in bundle.infolist() if not info.is_dir()]
        if stored != expected_names:
            raise DeliveryEnvelopeError(
                "batch archive contents do not match the batch member order"
            )
        for entry in member_payload:
            digest = hashlib.sha256()
            with bundle.open(str(entry["archive_name"]), "r") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != entry["archive_sha256"]:
                raise DeliveryEnvelopeError(
                    f"batch archive member {entry['archive_name']} does not match its published output"
                )

    receipt = _destination_path(root, receipt_relative, allow_external=False)
    profile_id, profile_hash, _snapshot = _profile_binding(root, state)
    cut_map = root / "working/cut_map.json"
    if cut_map.is_symlink():
        raise DeliveryEnvelopeError("working/cut_map.json must not be a symlink")
    prepared = {
        "schema_version": 1,
        "route": "batch",
        "render_id": batch_id,
        "state": "prepared",
        "quality": "final",
        "profile": {"id": profile_id, "resolved_profile_hash": profile_hash},
        "timeline": {
            "editor_state_revision": editor_server.editor_state_revision(state),
            "cut_map_sha256": _sha256(cut_map) if cut_map.is_file() else None,
        },
        "batch": {
            "schema_version": 1,
            "batch_id": batch_id,
            "members": member_payload,
        },
        "artifacts": {
            "output": _artifact_record(
                root, archive_path, str(archive_path), allow_external=True
            ),
            "qa_report": _artifact_record(root, receipt, receipt_relative),
            "contact_sheet": None,
            "visual_evidence": None,
            "motion_evidence": None,
            "caption_v2": None,
            "audio_event_plan": None,
            "audio_catalog": None,
            "sfx_stem": None,
        },
        "renderer_identity": _renderer_identity(renderer_script, ffmpeg_executable),
        "prepared_envelope_hash": None,
    }
    validate_envelope(root, prepared, expected_state="prepared")
    return prepared


def finalize_batch_envelope(
    project_dir: Path, prepared: dict[str, Any]
) -> dict[str, Any]:
    """Publish the aggregate binding; nothing may cite a batch before this."""
    root = project_dir.resolve()
    batch_id = str(prepared.get("render_id") or "")
    finalized = json.loads(json.dumps(prepared))
    finalized["state"] = "finalized"
    finalized["prepared_envelope_hash"] = contract_registry.canonical_hash(prepared)
    validate_envelope(root, finalized, expected_state="finalized")
    destination = finalized_path(root, batch_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _safe_directory(destination.parent, create=False)
    if destination.is_symlink() or destination.exists():
        # A finalized envelope is the delivery's identity; never overwrite one.
        raise DeliveryEnvelopeError(
            f"a finalized delivery envelope already exists for {batch_id}"
        )
    _atomic_write_json(destination, finalized)
    if _read_json(destination) != finalized:
        raise DeliveryEnvelopeError("batch delivery envelope changed during write")
    return finalized


def write_prepared_envelope(stage_dir: Path, envelope: dict[str, Any]) -> Path:
    """Write and re-read the private prepared envelope atomically."""
    if envelope.get("state") != "prepared":
        raise DeliveryEnvelopeError("only prepared envelopes may be staged")
    path = stage_dir / PREPARED_NAME
    _atomic_write_json(path, envelope)
    written = _read_json(path)
    if written != envelope:
        raise DeliveryEnvelopeError("prepared envelope changed during write")
    return path


def _stage_sources(stage_dir: Path, envelope: dict[str, Any]) -> dict[str, Path]:
    sources: dict[str, Path] = {}
    artifacts = envelope.get("artifacts", {})
    for name, filename in STAGE_FILENAMES.items():
        if artifacts.get(name) is not None:
            sources[name] = stage_dir / filename
    return sources


def _remove_stage(stage_dir: Path) -> None:
    _root, _render_id, stage = _stage_identity(stage_dir)
    try:
        stage.lstat()
    except FileNotFoundError:
        return
    shutil.rmtree(stage)


def _destination_state(
    project_dir: Path, relative: str, *, allow_external: bool = False
) -> dict[str, Any]:
    path = _destination_path(project_dir, relative, allow_external=allow_external)
    if not path.exists():
        return {"exists": False, "sha256": None}
    if path.is_symlink() or not path.is_file():
        raise DeliveryEnvelopeError(f"destination is not an owned regular file: {relative}")
    return {"exists": True, "sha256": _sha256(path)}


def _observed_destination_sha(path: Path, *, label: str) -> str | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise DeliveryEnvelopeError(f"destination is not an owned regular file: {label}")
    return _snapshot_regular_file(path, label=f"destination {label}").sha256


def _copy_atomic(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise DeliveryEnvelopeError(f"publication source is missing: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.delivery")
    try:
        shutil.copyfile(source, temporary)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _journal_path(stage_dir: Path) -> Path:
    return stage_dir / JOURNAL_NAME


def _canonical_expected_output(
    expected_output: Path, *, allow_leaf_conflict: bool = False
) -> Path:
    entry = Path(expected_output).expanduser()
    if allow_leaf_conflict:
        if not entry.is_absolute():
            raise DeliveryEnvelopeError("expected output must be absolute")
        _safe_directory(entry.parent, create=False)
        return entry
    if entry.is_symlink():
        raise DeliveryEnvelopeError("expected output must not be a symlink")
    return entry.resolve()


def _phase0b_destinations(
    project_dir: Path,
    render_id: str,
    expected_output: Path,
    *,
    include_caption_v2: bool = False,
    include_sfx: bool = False,
    allow_leaf_conflicts: bool = False,
) -> dict[str, tuple[str, bool]]:
    root = project_dir.resolve()
    output = _canonical_expected_output(
        expected_output,
        allow_leaf_conflict=allow_leaf_conflicts,
    )
    destinations = {
        "output": (str(output), True),
        "qa_report": (f"qa/{render_id}.json", False),
        "contact_sheet": (f"qa/{render_id}-contact.png", False),
        "visual_evidence": (
            f"working/render_visual_evidence/{render_id}.json",
            False,
        ),
        "motion_evidence": (
            f"working/render_visual_evidence/{render_id}.json",
            False,
        ),
    }
    if include_caption_v2:
        destinations["caption_v2"] = ("working/caption_delivery_v2.json", False)
    if include_sfx:
        destinations.update({
            "audio_event_plan": (f"working/audio_event_plans/{render_id}.json", False),
            "audio_catalog": (f"working/audio_catalogs/{render_id}.json", False),
            "sfx_stem": (f"working/sfx_stems/{render_id}.wav", False),
        })
    for relative, external in destinations.values():
        destination = _destination_path(
            root,
            relative,
            allow_external=external,
            allow_leaf_conflict=allow_leaf_conflicts,
        )
        if external:
            if destination != output or relative != str(output):
                raise DeliveryEnvelopeError("expected output path is not canonical")
        else:
            lexical = root / Path(*relative.split("/"))
            if destination != lexical:
                raise DeliveryEnvelopeError(
                    f"delivery destination is aliased through a symlink: {relative}"
                )
    return destinations


def _validate_phase0b_artifact_destinations(
    project_dir: Path,
    envelope: dict[str, Any],
    render_id: str,
    expected_output: Path,
    *,
    allow_leaf_conflicts: bool = False,
) -> dict[str, tuple[str, bool]]:
    if envelope.get("render_id") != render_id:
        raise DeliveryEnvelopeError("delivery envelope render_id does not match recovery target")
    artifacts = envelope.get("artifacts")
    if not isinstance(artifacts, dict):
        raise DeliveryEnvelopeError("delivery envelope artifacts are invalid")
    sfx_names = ("audio_event_plan", "audio_catalog", "sfx_stem")
    present_sfx = [name for name in sfx_names if artifacts.get(name) is not None]
    if present_sfx and len(present_sfx) != len(sfx_names):
        raise DeliveryEnvelopeError("SFX delivery artifacts must be all-or-none")
    destinations = _phase0b_destinations(
        project_dir,
        render_id,
        expected_output,
        include_caption_v2=artifacts.get("caption_v2") is not None,
        include_sfx=bool(present_sfx),
        allow_leaf_conflicts=allow_leaf_conflicts,
    )
    for name, (expected_path, _external) in destinations.items():
        item = artifacts.get(name)
        if not isinstance(item, dict) or item.get("path") != expected_path:
            raise DeliveryEnvelopeError(
                f"delivery artifact {name} destination does not match this render"
            )
    recovery_names = set(RECOVERY_ARTIFACT_NAMES) | {"caption_v2"}
    for name in set(ARTIFACT_NAMES) - recovery_names:
        if artifacts.get(name) is not None:
            raise DeliveryEnvelopeError(
                f"delivery artifact {name} is outside the Phase 0b recovery allowlist"
            )
    visual = artifacts["visual_evidence"]
    motion = artifacts["motion_evidence"]
    if visual != motion:
        raise DeliveryEnvelopeError(
            "visual and motion evidence must be one deduplicated artifact"
        )
    return destinations


def snapshot_finalized_delivery(
    project_dir: Path,
    expected_output: Path,
    *,
    expected_profile_id: str | None = None,
    expected_profile_hash: str | None = None,
    required_artifacts: tuple[str, ...] = (),
) -> FinalizedDeliverySnapshot:
    """Accept one current direct delivery from stable, context-bound file snapshots."""
    root = _ensure_staging_trust_chain(project_dir, create=False)
    state, state_snapshot = snapshot_owned_json(
        root,
        "working/editor_state.json",
        label="current editor state",
    )
    from editor_server import editor_state_revision
    from render_editor_timeline import direct_final_render_id

    output = _canonical_expected_output(expected_output)
    render_id = direct_final_render_id(state, output)
    envelope_path = finalized_path(root, render_id)
    if envelope_path.resolve() != envelope_path:
        raise DeliveryEnvelopeError("finalized envelope path is aliased")
    envelope_snapshot = _snapshot_regular_file(
        envelope_path,
        label="finalized envelope",
        capture_bytes=True,
    )
    envelope = _decode_json_snapshot(envelope_snapshot)
    _validate_envelope_structure(envelope, expected_state="finalized")
    if envelope.get("render_id") != render_id:
        raise DeliveryEnvelopeError(
            "finalized envelope render_id does not match the current direct render"
        )
    if envelope.get("route") != "direct":
        raise DeliveryEnvelopeError(
            "finalized envelope was not published by the direct renderer route"
        )

    profile_id, profile_hash, profile_snapshot = _profile_binding(root, state)
    if expected_profile_id is not None and profile_id != expected_profile_id:
        raise DeliveryEnvelopeError(
            "current director profile does not match the requested cut profile"
        )
    if expected_profile_hash is not None and profile_hash != expected_profile_hash:
        raise DeliveryEnvelopeError(
            "current resolved director profile hash changed after cut selection"
        )
    if envelope.get("profile") != {
        "id": profile_id,
        "resolved_profile_hash": profile_hash,
    }:
        raise DeliveryEnvelopeError(
            "finalized envelope profile does not match the current resolved profile"
        )

    timeline_revision = editor_state_revision(state)
    timeline = envelope.get("timeline")
    if not isinstance(timeline, dict) or timeline.get("editor_state_revision") != timeline_revision:
        raise DeliveryEnvelopeError(
            "finalized envelope timeline does not match the current editor state"
        )
    destinations = _validate_phase0b_artifact_destinations(
        root,
        envelope,
        render_id,
        output,
    )
    artifacts = envelope.get("artifacts")
    if not isinstance(artifacts, dict):
        raise DeliveryEnvelopeError("finalized envelope artifacts are invalid")
    for name in required_artifacts:
        if name not in ARTIFACT_NAMES or not isinstance(artifacts.get(name), dict):
            raise DeliveryEnvelopeError(
                f"finalized delivery artifact is required: {name}"
            )

    cut_map_path = root / "working/cut_map.json"
    cut_snapshot: FileSnapshot | None = None
    try:
        cut_map_path.lstat()
    except FileNotFoundError:
        pass
    else:
        cut_snapshot = _snapshot_regular_file(
            cut_map_path,
            label="current cut map",
        )
    sfx_names = ("audio_event_plan", "audio_catalog", "sfx_stem")
    has_sfx = all(isinstance(artifacts.get(name), dict) for name in sfx_names)
    if cut_snapshot is not None:
        cut_map_sha256: str | None = cut_snapshot.sha256
    elif has_sfx:
        segments = state.get("segments") if isinstance(state.get("segments"), list) else []
        cut_map_sha256 = contract_registry.canonical_hash({"segments": segments})
    else:
        cut_map_sha256 = None
    if timeline.get("cut_map_sha256") != cut_map_sha256:
        raise DeliveryEnvelopeError(
            "finalized envelope cut map does not match the current timeline"
        )

    captured_by_path: dict[Path, FileSnapshot] = {}
    artifact_snapshots: dict[str, FileSnapshot] = {}
    for name in ARTIFACT_NAMES:
        item = artifacts.get(name)
        if item is None:
            continue
        if not isinstance(item, dict):
            raise DeliveryEnvelopeError(f"artifact {name} is invalid")
        relative, external = destinations.get(name, (None, None))
        if relative is None or type(external) is not bool:
            raise DeliveryEnvelopeError(
                f"delivery artifact {name} has no canonical destination"
            )
        path = _destination_path(root, relative, allow_external=external)
        snapshot = captured_by_path.get(path)
        if snapshot is None:
            snapshot = _snapshot_regular_file(
                path,
                label=f"artifact {name}",
                capture_bytes=(name in {"qa_report", "visual_evidence"}),
            )
            captured_by_path[path] = snapshot
        artifact_snapshots[name] = snapshot
        if snapshot.sha256 != item.get("sha256") or snapshot.size != item.get("bytes"):
            raise DeliveryEnvelopeError(
                f"artifact {name} hash/size mismatch for {relative}"
            )
    output_snapshot = artifact_snapshots.get("output")
    qa_snapshot = artifact_snapshots.get("qa_report")
    if output_snapshot is None or qa_snapshot is None:
        raise DeliveryEnvelopeError("finalized delivery output or QA artifact is missing")
    qa_report = _decode_json_snapshot(qa_snapshot)
    if qa_report.get("status") != "pass":
        raise DeliveryEnvelopeError(
            f"finalized delivery QA said {qa_report.get('status')}"
        )

    visual_source_snapshots: list[FileSnapshot] = []
    visual_binding = envelope.get("visual_authority")
    if visual_binding is not None:
        visual_snapshot = artifact_snapshots.get("visual_evidence")
        if visual_snapshot is None:
            raise DeliveryEnvelopeError("visual authority has no delivered evidence")
        visual_report = _decode_json_snapshot(visual_snapshot)
        reported_authority = visual_report.get("authority")
        if not isinstance(reported_authority, dict):
            raise DeliveryEnvelopeError("delivered evidence has no visual authority")
        binding_keys = set(visual_binding)
        if {key: reported_authority.get(key) for key in binding_keys} != visual_binding:
            raise DeliveryEnvelopeError("delivered visual authority differs from envelope")
        plan, plan_snapshot = snapshot_owned_json(
            root, "working/visual_plan_v2.json", label="current visual plan v2"
        )
        layers, layers_snapshot = snapshot_owned_json(
            root, "working/structured_layers.json", label="current structured layers"
        )
        index, index_snapshot = snapshot_owned_json(
            root,
            "working/structured_layer_artifacts.json",
            label="current structured artifact index",
        )
        if (
            plan_snapshot.sha256 != visual_binding.get("visual_plan_sha256")
            or layers_snapshot.sha256 != visual_binding.get("structured_layers_sha256")
            or index_snapshot.sha256 != visual_binding.get("artifact_index_sha256")
            or plan.get("revision") != visual_binding.get("visual_plan_revision")
            or plan.get("revision")
            != contract_registry.canonical_hash(plan.get("items"))
        ):
            raise DeliveryEnvelopeError("current visual authority differs from delivery")
        visual_source_snapshots.extend((plan_snapshot, layers_snapshot, index_snapshot))
        for item in index.get("items", []):
            if not isinstance(item, dict) or not isinstance(item.get("artifact_id"), str):
                raise DeliveryEnvelopeError("structured artifact index item is invalid")
            artifact_snapshot = snapshot_project_file(
                root,
                item["artifact_id"],
                label=f"current structured artifact {item.get('layer_id')}",
            )
            if artifact_snapshot.sha256 != item.get("artifact_hash"):
                raise DeliveryEnvelopeError("current structured artifact hash changed")
            visual_source_snapshots.append(artifact_snapshot)
        mosaic_assets: list[dict[str, Any]] = []
        for layer in layers.get("items", []):
            if not isinstance(layer, dict) or layer.get("type") != "mosaic":
                continue
            payload = layer.get("payload")
            assets = payload.get("assets") if isinstance(payload, dict) else None
            if not isinstance(assets, list):
                raise DeliveryEnvelopeError("current mosaic asset list is invalid")
            mosaic_assets.extend(item for item in assets if isinstance(item, dict))
        if mosaic_assets:
            registry, registry_snapshot = snapshot_owned_json(
                root, "assets/provenance.json", label="current mosaic provenance registry"
            )
            validate_mosaic_registry_snapshot(registry, mosaic_assets)
            visual_source_snapshots.append(registry_snapshot)
            for descriptor in mosaic_assets:
                relative = descriptor.get("path")
                if not isinstance(relative, str):
                    raise DeliveryEnvelopeError("current mosaic asset path is invalid")
                asset_snapshot = snapshot_project_file(
                    root,
                    relative,
                    label=f"current mosaic asset {descriptor.get('asset_id')}",
                )
                if asset_snapshot.sha256 != descriptor.get("sha256"):
                    raise DeliveryEnvelopeError("current mosaic asset hash changed")
                visual_source_snapshots.append(asset_snapshot)

    files: list[FileSnapshot] = [envelope_snapshot, state_snapshot]
    if profile_snapshot is not None:
        files.append(profile_snapshot)
    if cut_snapshot is not None:
        files.append(cut_snapshot)
    files.extend(captured_by_path.values())
    files.extend(visual_source_snapshots)
    snapshot = FinalizedDeliverySnapshot(
        project_dir=root,
        expected_output=output,
        envelope_path=envelope_path,
        render_id=render_id,
        envelope=envelope,
        envelope_sha256=envelope_snapshot.sha256,
        output_sha256=output_snapshot.sha256,
        state_revision=timeline_revision,
        profile_id=profile_id,
        resolved_profile_hash=profile_hash,
        cut_map_sha256=cut_map_sha256,
        files=tuple(files),
        editor_state=state,
    )
    revalidate_finalized_delivery(snapshot)
    return snapshot


def revalidate_finalized_delivery(snapshot: FinalizedDeliverySnapshot) -> None:
    """Fail if any accepted pathname or current profile changed before handoff."""
    if not isinstance(snapshot, FinalizedDeliverySnapshot):
        raise DeliveryEnvelopeError("finalized delivery snapshot authority is required")
    for file_snapshot in snapshot.files:
        revalidate_file_snapshot(file_snapshot)
    if not any(item.label == "current cut map" for item in snapshot.files):
        cut_map = snapshot.project_dir / "working/cut_map.json"
        try:
            cut_map.lstat()
        except FileNotFoundError:
            pass
        else:
            raise DeliveryEnvelopeError(
                "current cut map changed after finalized delivery verification"
            )
    profile_id, profile_hash, _profile_snapshot = _profile_binding(
        snapshot.project_dir,
        snapshot.editor_state,
    )
    if (
        profile_id != snapshot.profile_id
        or profile_hash != snapshot.resolved_profile_hash
    ):
        raise DeliveryEnvelopeError(
            "resolved director profile changed after finalized delivery verification"
        )


@contextmanager
def finalized_delivery_handoff(
    snapshot: FinalizedDeliverySnapshot,
) -> Iterator[None]:
    """Hold the render publisher lease across final validation and handoff.

    Repo-owned publication and recovery for this render use the same flock and
    therefore cannot overlap the yielded section.  The lease is cooperative;
    it does not constrain arbitrary same-user filesystem writers.
    """
    owner_token = _acquire_staging_lease(snapshot.project_dir, snapshot.render_id)
    try:
        revalidate_finalized_delivery(snapshot)
        yield
    finally:
        _release_staging_lease(
            snapshot.project_dir,
            snapshot.render_id,
            owner_token,
        )


def _recovery_expected_entries(
    project_dir: Path,
    stage_dir: Path,
    render_id: str,
    expected_output: Path,
    *,
    allow_leaf_conflicts: bool = False,
) -> list[dict[str, Any]]:
    prepared = _decode_json_snapshot(
        _snapshot_regular_file(
            stage_dir / PREPARED_NAME,
            label=f"prepared delivery envelope {render_id}",
            capture_bytes=True,
        )
    )
    destinations = _validate_phase0b_artifact_destinations(
        project_dir,
        prepared,
        render_id,
        expected_output,
        allow_leaf_conflicts=allow_leaf_conflicts,
    )
    sources = _stage_sources(stage_dir, prepared)
    validate_envelope(
        project_dir,
        prepared,
        sources=sources,
        expected_state="prepared",
    )
    prepared_hash = contract_registry.canonical_hash(prepared)
    finalized = json.loads(json.dumps(prepared))
    finalized["state"] = "finalized"
    finalized["prepared_envelope_hash"] = prepared_hash
    validate_envelope(
        project_dir,
        finalized,
        sources=sources,
        expected_state="finalized",
    )

    expected: list[dict[str, Any]] = []
    seen: set[str] = set()
    artifacts = prepared["artifacts"]
    recovery_allowlist = set(RECOVERY_ARTIFACT_NAMES) | {"caption_v2"}
    recovery_names = [
        name
        for name in ARTIFACT_NAMES
        if name in recovery_allowlist and artifacts.get(name) is not None
    ]
    for name in recovery_names:
        destination, external = destinations[name]
        if destination in seen:
            continue
        seen.add(destination)
        expected.append(
            {
                "destination": destination,
                "external": external,
                "new_sha256": artifacts[name]["sha256"],
                "new_source": sources[name],
                "new_payload": None,
            }
        )
    final_relative = finalized_path(project_dir, render_id).relative_to(
        project_dir.resolve()
    ).as_posix()
    expected.append(
        {
            "destination": final_relative,
            "external": False,
            "new_sha256": _sha256_bytes(_json_bytes(finalized)),
            "new_source": None,
            "new_payload": finalized,
        }
    )
    return expected


def _validated_backup_path(
    stage_dir: Path, backup: str, expected_backup: str
) -> Path:
    if backup != expected_backup or "\\" in backup:
        raise DeliveryEnvelopeError("publication journal backup path is invalid")
    parts = backup.split("/")
    if any(part in {"", ".", ".."} for part in parts) or parts[0] != "backup":
        raise DeliveryEnvelopeError("publication journal backup path is not normalized")
    lexical = stage_dir.resolve() / Path(*parts)
    if lexical.is_symlink() or lexical.resolve() != lexical:
        raise DeliveryEnvelopeError("publication journal backup path is aliased")
    return lexical


def _validate_restore_plan(
    project_dir: Path,
    stage_dir: Path,
    journal: dict[str, Any],
    *,
    render_id: str,
    expected_output: Path,
    allow_external_conflicts: bool = False,
) -> list[dict[str, Any]]:
    if set(journal) != JOURNAL_KEYS or type(journal.get("schema_version")) is not int:
        raise DeliveryEnvelopeError("publication journal shape is invalid")
    if journal["schema_version"] != 1 or journal.get("render_id") != render_id:
        raise DeliveryEnvelopeError("publication journal identity is invalid")
    entries = journal.get("entries")
    if not isinstance(entries, list):
        raise DeliveryEnvelopeError("publication journal entries are invalid")
    expected_entries = _recovery_expected_entries(
        project_dir,
        stage_dir,
        render_id,
        expected_output,
        allow_leaf_conflicts=allow_external_conflicts,
    )
    if len(entries) != len(expected_entries):
        raise DeliveryEnvelopeError("publication journal destination set is incomplete")

    destinations = [
        entry.get("destination") if isinstance(entry, dict) else None for entry in entries
    ]
    if len(set(destinations)) != len(destinations):
        raise DeliveryEnvelopeError("publication journal has duplicate destinations")

    plan: list[dict[str, Any]] = []
    for index, (entry, expected) in enumerate(zip(entries, expected_entries)):
        if not isinstance(entry, dict) or set(entry) != JOURNAL_ENTRY_KEYS:
            raise DeliveryEnvelopeError("publication journal entry is invalid")
        relative = expected["destination"]
        external = expected["external"]
        new_sha = expected["new_sha256"]
        if entry.get("destination") != relative:
            raise DeliveryEnvelopeError("publication journal destination is not allowlisted")
        if type(entry.get("external")) is not bool or entry["external"] is not external:
            raise DeliveryEnvelopeError("publication journal external flag is invalid")
        if entry.get("new_sha256") != new_sha or not SHA256_PATTERN.fullmatch(new_sha):
            raise DeliveryEnvelopeError("publication journal new hash is invalid")
        if type(entry.get("prior_exists")) is not bool:
            raise DeliveryEnvelopeError("publication journal prior_exists is invalid")
        prior_exists = entry["prior_exists"]
        prior_sha = entry.get("prior_sha256")
        backup_value = entry.get("backup")
        backup: Path | None = None
        if prior_exists:
            if not isinstance(prior_sha, str) or not SHA256_PATTERN.fullmatch(prior_sha):
                raise DeliveryEnvelopeError("publication journal prior hash is invalid")
            extension = "envelope" if index == len(entries) - 1 else "bin"
            if not isinstance(backup_value, str):
                raise DeliveryEnvelopeError("publication journal backup is missing")
            backup = _validated_backup_path(
                stage_dir,
                backup_value,
                f"backup/{index:03d}.{extension}",
            )
            if not backup.is_file() or _sha256(backup) != prior_sha:
                raise DeliveryEnvelopeError(
                    f"journal backup is missing or changed: {relative}"
                )
        elif prior_sha is not None or backup_value is not None:
            raise DeliveryEnvelopeError("publication journal absent prior has backup data")

        destination = _destination_path(
            project_dir,
            relative,
            allow_external=external,
            allow_leaf_conflict=allow_external_conflicts,
        )
        current_conflict = False
        try:
            current_sha = _observed_destination_sha(destination, label=relative)
        except DeliveryEnvelopeError:
            if not allow_external_conflicts:
                raise
            current_sha = None
            current_conflict = True
        permitted = {new_sha, prior_sha} if prior_exists else {new_sha, None}
        if (
            (current_conflict or current_sha not in permitted)
            and not allow_external_conflicts
        ):
            raise DeliveryEnvelopeError(
                f"journal destination was externally changed; refusing overwrite: {relative}"
            )
        plan.append(
            {
                "relative": relative,
                "destination": destination,
                "current_sha256": current_sha,
                "current_conflict": current_conflict,
                "prior_exists": prior_exists,
                "prior_sha256": prior_sha,
                "backup": backup,
                "new_sha256": new_sha,
                "new_source": expected["new_source"],
                "new_payload": expected["new_payload"],
            }
        )
    return plan


def _deferred_publication_binding(
    project_dir: Path,
    stage_dir: Path,
    render_id: str,
    *,
    expected_output: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    root = project_dir.resolve()
    output_entry = Path(expected_output).expanduser()
    allow_external_conflicts = output_entry.is_absolute() and output_entry.parent.is_dir()
    if allow_external_conflicts:
        canonical_parent = output_entry.parent.resolve()
        _safe_directory(canonical_parent, create=False)
        output = canonical_parent / output_entry.name
    else:
        output = _canonical_expected_output(output_entry)
    journal_snapshot = _snapshot_regular_file(
        _journal_path(stage_dir),
        label=f"deferred publication journal {render_id}",
        capture_bytes=True,
    )
    journal = _decode_json_snapshot(journal_snapshot)
    plan = _validate_restore_plan(
        root,
        stage_dir,
        journal,
        render_id=render_id,
        expected_output=output,
        allow_external_conflicts=allow_external_conflicts,
    )
    output_entries = [item for item in plan if item["destination"] == output]
    final_destination = finalized_path(root, render_id)
    finalized_entries = [
        item for item in plan if item["destination"] == final_destination
    ]
    if len(output_entries) != 1 or len(finalized_entries) != 1:
        raise DeliveryEnvelopeError(
            f"deferred publication binding is incomplete: {render_id}"
        )
    binding = {
        "render_id": render_id,
        "expected_output": str(output),
        "journal_sha256": journal_snapshot.sha256,
        "output_sha256": output_entries[0]["new_sha256"],
        "finalized_sha256": finalized_entries[0]["new_sha256"],
    }
    return binding, plan


def _deferred_marker_payload(
    binding: dict[str, Any],
    *,
    state: str,
    transaction_id: str,
) -> dict[str, Any]:
    """Bind repo-owned recovery state; this checksum is not same-user authentication.

    The live owner token controls repo-writer state transitions.  The persisted,
    unkeyed binding detects stale, mismatched, and partially edited markers, but
    cannot authenticate against an OS principal able to rewrite the whole stage.
    """
    if state not in DEFERRED_MARKER_STATES:
        raise DeliveryEnvelopeError("deferred publication marker state is invalid")
    if not re.fullmatch(r"[0-9a-f]{32}", transaction_id):
        raise DeliveryEnvelopeError("deferred publication transaction id is invalid")
    payload = {
        "schema_version": 2,
        "state": state,
        "transaction_id": transaction_id,
        **binding,
    }
    payload["binding_sha256"] = contract_registry.canonical_hash(payload)
    return payload


def _validated_deferred_marker(
    project_dir: Path,
    stage_dir: Path,
    render_id: str,
    *,
    expected_output: Path,
    expected_state: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    marker = _decode_json_snapshot(
        _snapshot_regular_file(
            stage_dir / DEFERRED_NAME,
            label=f"deferred publication marker {render_id}",
            capture_bytes=True,
        )
    )
    if (
        set(marker) != DEFERRED_MARKER_KEYS
        or type(marker.get("schema_version")) is not int
        or marker.get("schema_version") != 2
        or marker.get("state") not in DEFERRED_MARKER_STATES
        or not isinstance(marker.get("transaction_id"), str)
    ):
        raise DeliveryEnvelopeError(
            f"deferred publication marker is invalid: {render_id}"
        )
    if expected_state is not None and marker["state"] != expected_state:
        raise DeliveryEnvelopeError(
            f"deferred publication marker state is not {expected_state}: {render_id}"
        )
    binding, plan = _deferred_publication_binding(
        project_dir,
        stage_dir,
        render_id,
        expected_output=expected_output,
    )
    expected = _deferred_marker_payload(
        binding,
        state=marker["state"],
        transaction_id=marker["transaction_id"],
    )
    if marker != expected:
        raise DeliveryEnvelopeError(
            f"deferred publication marker binding is invalid: {render_id}"
        )
    return marker, plan


def _write_deferred_marker(
    project_dir: Path,
    stage_dir: Path,
    render_id: str,
    *,
    expected_output: Path,
    state: str,
    transaction_id: str,
) -> dict[str, Any]:
    binding, plan = _deferred_publication_binding(
        project_dir,
        stage_dir,
        render_id,
        expected_output=expected_output,
    )
    if state == "committed" and any(
        item["current_conflict"] or item["current_sha256"] != item["new_sha256"]
        for item in plan
    ):
        raise DeliveryEnvelopeError(
            f"committed publication changed before marker transition: {render_id}"
        )
    marker = _deferred_marker_payload(
        binding,
        state=state,
        transaction_id=transaction_id,
    )
    marker_path = stage_dir / DEFERRED_NAME
    _atomic_write_json(marker_path, marker)
    _fsync_directory(stage_dir)
    written, written_plan = _validated_deferred_marker(
        project_dir,
        stage_dir,
        render_id,
        expected_output=expected_output,
        expected_state=state,
    )
    if written != marker:
        raise DeliveryEnvelopeError(
            f"deferred publication marker changed during write: {render_id}"
        )
    if state == "committed" and any(
        item["current_conflict"] or item["current_sha256"] != item["new_sha256"]
        for item in written_plan
    ):
        raise DeliveryEnvelopeError(
            f"committed publication changed during marker transition: {render_id}"
        )
    return marker


def _deferred_restore_plan(authority: DeferredPublication) -> list[dict[str, Any]]:
    validated = _validate_deferred_publication(authority)
    journal = _decode_json_snapshot(
        _snapshot_regular_file(
            _journal_path(validated.stage_dir),
            label=f"deferred publication journal {validated.render_id}",
            capture_bytes=True,
        )
    )
    return _validate_restore_plan(
        validated.project_dir,
        validated.stage_dir,
        journal,
        render_id=validated.render_id,
        expected_output=validated.expected_output,
        allow_external_conflicts=True,
    )


def validate_deferred_publication(authority: DeferredPublication) -> None:
    """Require every pending destination to still be this attempt's bytes."""
    for item in _deferred_restore_plan(authority):
        if item["current_sha256"] != item["new_sha256"]:
            raise DeliveryEnvelopeError(
                "deferred publication changed before handoff: "
                f"{item['relative']}"
            )


def _quarantine_conflicting_destination(
    authority: DeferredPublication,
    item: dict[str, Any],
    *,
    index: int,
    quarantine_dir: Path,
) -> None:
    destination = item["destination"]
    relative = item["relative"]
    expected_sha = item["current_sha256"]
    try:
        before = destination.lstat()
    except FileNotFoundError as exc:
        raise DeliveryEnvelopeError(
            f"deferred publication conflict disappeared before quarantine: {relative}"
        ) from exc
    current: FileSnapshot | None = None
    if stat.S_ISREG(before.st_mode):
        current = _snapshot_regular_file(
            destination,
            label=f"deferred publication conflict {relative}",
        )
        if current.sha256 != expected_sha:
            raise DeliveryEnvelopeError(
                f"deferred publication conflict changed before quarantine: {relative}"
            )
    quarantine_path = quarantine_dir / f"{index:03d}.conflict"
    os.replace(destination, quarantine_path)
    moved_metadata = quarantine_path.lstat()
    if (
        moved_metadata.st_dev != before.st_dev
        or moved_metadata.st_ino != before.st_ino
        or stat.S_IFMT(moved_metadata.st_mode) != stat.S_IFMT(before.st_mode)
    ):
        raise DeliveryEnvelopeError(
            f"deferred publication conflict quarantine changed identity: {relative}"
        )
    if current is not None:
        moved = _snapshot_regular_file(
            quarantine_path,
            label=f"quarantined deferred publication conflict {relative}",
        )
        if moved.sha256 != current.sha256 or moved.size != current.size:
            raise DeliveryEnvelopeError(
                f"deferred publication conflict quarantine changed bytes: {relative}"
            )


def abort_deferred_publication(authority: DeferredPublication) -> None:
    """Restore exact prior destinations, quarantining conflicting current bytes."""
    validated = _validate_deferred_publication(authority)
    try:
        plan = _deferred_restore_plan(validated)
        conflicts = [
            item
            for item in plan
            if item["current_conflict"]
            or item["current_sha256"]
            not in {
                    item["new_sha256"],
                    item["prior_sha256"] if item["prior_exists"] else None,
                }
        ]
        quarantine_dir: Path | None = None
        if conflicts:
            quarantine_root = validated.project_dir / QUARANTINE_REL
            quarantine_root.parent.mkdir(parents=True, exist_ok=True)
            _safe_directory(quarantine_root, create=True)
            quarantine_dir = quarantine_root / (
                f"{validated.render_id}-{uuid.uuid4().hex}"
            )
            quarantine_dir.mkdir(mode=0o700)
            _safe_directory(quarantine_dir, create=False)

        for index, item in enumerate(plan):
            destination = item["destination"]
            relative = item["relative"]
            if item["current_conflict"]:
                current_sha = None
            else:
                current_sha = _observed_destination_sha(destination, label=relative)
                if current_sha != item["current_sha256"]:
                    raise DeliveryEnvelopeError(
                        f"deferred publication changed during abort: {relative}"
                    )
            permitted = {
                item["new_sha256"],
                item["prior_sha256"] if item["prior_exists"] else None,
            }
            if item["current_conflict"] or current_sha not in permitted:
                if quarantine_dir is None:
                    raise DeliveryEnvelopeError(
                        f"deferred publication conflict has no quarantine: {relative}"
                    )
                _quarantine_conflicting_destination(
                    validated,
                    item,
                    index=index,
                    quarantine_dir=quarantine_dir,
                )
                current_sha = None
            if item["prior_exists"]:
                if current_sha != item["prior_sha256"]:
                    backup = item["backup"]
                    if not isinstance(backup, Path) or _sha256(backup) != item["prior_sha256"]:
                        raise DeliveryEnvelopeError(
                            f"deferred publication backup changed: {relative}"
                        )
                    _copy_atomic(backup, destination)
                expected_after = item["prior_sha256"]
            else:
                if current_sha is not None:
                    destination.unlink(missing_ok=True)
                expected_after = None
            if _observed_destination_sha(destination, label=relative) != expected_after:
                raise DeliveryEnvelopeError(
                    f"deferred publication abort verification failed: {relative}"
                )
        _remove_stage(validated.stage_dir)
    finally:
        _release_staging_lease(
            validated.project_dir,
            validated.render_id,
            validated._owner_token,
        )


def commit_deferred_publication(authority: DeferredPublication) -> None:
    """Persist a process-recoverable commit, then best-effort discard rollback data.

    This protocol covers process death.  It does not claim host/power-loss
    ordering for every destination-directory rename.
    """
    validated = _validate_deferred_publication(authority, expected_state="pending")
    # The lease is this attempt's only authority to roll back, so it is held
    # until the commit is durable.  Releasing it on a failed commit would strand
    # the publication public: abort_deferred_publication would then reject its
    # own authority as stale and the caller could never undo the handoff.
    validate_deferred_publication(validated)
    _write_deferred_marker(
        validated.project_dir,
        validated.stage_dir,
        validated.render_id,
        expected_output=validated.expected_output,
        state="committed",
        transaction_id=validated._transaction_id,
    )
    try:
        _remove_stage(validated.stage_dir)
    except (OSError, DeliveryEnvelopeError):
        # The persisted committed marker makes cleanup retryable after
        # process death. Publication is already public and must not be
        # reclassified as rollback-only.
        pass
    _release_staging_lease(
        validated.project_dir,
        validated.render_id,
        validated._owner_token,
    )


def _compensate_restore_attempt(applied: list[dict[str, Any]]) -> None:
    for item in reversed(applied):
        destination = item["destination"]
        after_restore = item["prior_sha256"] if item["prior_exists"] else None
        current_sha = _observed_destination_sha(destination, label=item["relative"])
        if current_sha != after_restore:
            raise DeliveryEnvelopeError(
                "recovery compensation blocked by a concurrent external edit: "
                f"{item['relative']}"
            )
        before_restore = item["current_sha256"]
        if before_restore is None:
            destination.unlink(missing_ok=True)
        elif before_restore == item["new_sha256"]:
            source = item["new_source"]
            payload = item["new_payload"]
            if isinstance(source, Path):
                if source.is_symlink() or not source.is_file() or _sha256(source) != before_restore:
                    raise DeliveryEnvelopeError(
                        f"recovery compensation source changed: {item['relative']}"
                    )
                _copy_atomic(source, destination)
            elif isinstance(payload, dict):
                _atomic_write_json(destination, payload)
            else:
                raise DeliveryEnvelopeError(
                    f"recovery compensation source is unavailable: {item['relative']}"
                )
        elif item["prior_exists"] and before_restore == item["prior_sha256"]:
            backup = item["backup"]
            if not isinstance(backup, Path):
                raise DeliveryEnvelopeError(
                    f"recovery compensation backup is unavailable: {item['relative']}"
                )
            _copy_atomic(backup, destination)
        else:
            raise DeliveryEnvelopeError(
                f"recovery compensation state is invalid: {item['relative']}"
            )
        if _observed_destination_sha(destination, label=item["relative"]) != before_restore:
            raise DeliveryEnvelopeError(
                f"recovery compensation hash check failed: {item['relative']}"
            )


def _restore_journal(
    project_dir: Path,
    stage_dir: Path,
    journal: dict[str, Any],
    *,
    render_id: str,
    expected_output: Path,
) -> None:
    plan = _validate_restore_plan(
        project_dir,
        stage_dir,
        journal,
        render_id=render_id,
        expected_output=expected_output,
    )
    applied: list[dict[str, Any]] = []
    try:
        for item in plan:
            destination = item["destination"]
            current_sha = _observed_destination_sha(
                destination,
                label=item["relative"],
            )
            if current_sha != item["current_sha256"]:
                raise DeliveryEnvelopeError(
                    f"journal destination changed during recovery: {item['relative']}"
                )
            if item["prior_exists"] and current_sha == item["prior_sha256"]:
                continue
            if not item["prior_exists"] and current_sha is None:
                continue
            if item["prior_exists"]:
                backup = item["backup"]
                if not isinstance(backup, Path):
                    raise DeliveryEnvelopeError("validated journal backup is unavailable")
                _copy_atomic(backup, destination)
            else:
                destination.unlink(missing_ok=True)
            applied.append(item)
    except Exception as exc:
        if applied:
            try:
                _compensate_restore_attempt(applied)
            except Exception as compensation_exc:
                raise DeliveryEnvelopeError(
                    "journal recovery failed and compensation was blocked; "
                    f"journal kept: {compensation_exc}"
                ) from exc
            raise DeliveryEnvelopeError(
                f"journal recovery conflict; earlier restores were compensated: {exc}"
            ) from exc
        raise


def _matching_finalized_envelope(
    project_dir: Path, render_id: str, expected_output: Path
) -> bool:
    path = finalized_path(project_dir, render_id)
    if not path.is_file() or path.is_symlink():
        return False
    try:
        envelope = _read_json(path)
        if envelope.get("render_id") != render_id or envelope.get("state") != "finalized":
            return False
        _validate_phase0b_artifact_destinations(
            project_dir, envelope, render_id, expected_output
        )
        validate_envelope(project_dir, envelope, expected_state="finalized")
    except DeliveryEnvelopeError:
        return False
    return True


def _has_deferred_marker(stage_dir: Path) -> bool:
    marker_path = stage_dir / DEFERRED_NAME
    try:
        marker_path.lstat()
    except FileNotFoundError:
        return False
    return True


def _rollback_pending_publication_locked(
    project_dir: Path,
    render_id: str,
    *,
    expected_output: Path,
    owner_token: object,
) -> None:
    """Rollback an acquisition-gap publication without a public authority."""
    root = _ensure_staging_trust_chain(project_dir, create=False)
    _require_staging_lease(root, render_id, owner_token)
    stage = staging_path(root, render_id)
    _stage_identity(stage)
    _validated_deferred_marker(
        root,
        stage,
        render_id,
        expected_output=expected_output,
        expected_state="pending",
    )
    journal = _decode_json_snapshot(
        _snapshot_regular_file(
            _journal_path(stage),
            label=f"deferred publication journal {render_id}",
            capture_bytes=True,
        )
    )
    _restore_journal(
        root,
        stage,
        journal,
        render_id=render_id,
        expected_output=expected_output,
    )
    _remove_stage(stage)


def _recover_stale_staging_locked(
    project_dir: Path,
    render_id: str,
    *,
    expected_output: Path,
    owner_token: object,
) -> None:
    """Recover a crashed publication, refusing to overwrite external edits."""
    root = _ensure_staging_trust_chain(project_dir, create=False)
    _require_staging_lease(root, render_id, owner_token)
    stage = staging_path(root, render_id)
    try:
        stage.lstat()
    except FileNotFoundError:
        return
    _stage_identity(stage)
    if _has_deferred_marker(stage):
        marker, plan = _validated_deferred_marker(
            root,
            stage,
            render_id,
            expected_output=expected_output,
        )
        if marker["state"] == "committed":
            for item in plan:
                if item["current_conflict"] or item["current_sha256"] != item["new_sha256"]:
                    raise DeliveryEnvelopeError(
                        "committed publication changed; refusing cleanup or overwrite: "
                        f"{item['relative']}"
                    )
            _remove_stage(stage)
            return
        journal_file = _journal_path(stage)
        journal = _decode_json_snapshot(
            _snapshot_regular_file(
                journal_file,
                label=f"deferred publication journal {render_id}",
                capture_bytes=True,
            )
        )
        _restore_journal(
            root,
            stage,
            journal,
            render_id=render_id,
            expected_output=expected_output,
        )
        _remove_stage(stage)
        return
    if _matching_finalized_envelope(root, render_id, expected_output):
        _remove_stage(stage)
        return
    journal_file = _journal_path(stage)
    if journal_file.is_file():
        journal = _read_json(journal_file)
        _restore_journal(
            root,
            stage,
            journal,
            render_id=render_id,
            expected_output=expected_output,
        )
        _remove_stage(stage)
        return
    _remove_stage(stage)


def recover_stale_staging(
    project_dir: Path, render_id: str, *, expected_output: Path
) -> None:
    root = project_dir.resolve()
    owner_token = _acquire_staging_lease(root, render_id)
    try:
        _recover_stale_staging_locked(
            root,
            render_id,
            expected_output=expected_output,
            owner_token=owner_token,
        )
    finally:
        _release_staging_lease(root, render_id, owner_token)


def begin_staging(
    project_dir: Path, render_id: str, *, expected_output: Path
) -> StagingAttempt:
    root = project_dir.resolve()
    owner_token = _acquire_staging_lease(root, render_id)
    try:
        _recover_stale_staging_locked(
            root,
            render_id,
            expected_output=expected_output,
            owner_token=owner_token,
        )
        stage = staging_path(root, render_id)
        stage.mkdir(mode=0o700, exist_ok=False)
        _stage_identity(stage)
        return StagingAttempt(
            project_dir=root,
            render_id=render_id,
            stage_dir=stage,
            _owner_token=owner_token,
        )
    except Exception:
        _release_staging_lease(root, render_id, owner_token)
        raise


def discard_staging(
    project_dir: Path,
    render_id: str,
    *,
    authority: StagingAttempt | None = None,
) -> None:
    root = project_dir.resolve()
    attempt = _validate_staging_attempt(root, render_id, authority)
    try:
        stage = staging_path(root, render_id)
        try:
            metadata = stage.lstat()
        except FileNotFoundError:
            return
        if (
            stat.S_ISDIR(metadata.st_mode)
            and not stat.S_ISLNK(metadata.st_mode)
            and not _journal_path(stage).exists()
        ):
            _remove_stage(stage)
    finally:
        _release_staging_lease(root, render_id, attempt._owner_token)


def _journal_entries(
    project_dir: Path,
    prepared: dict[str, Any],
    finalized: dict[str, Any],
    stage_dir: Path,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    artifacts = prepared.get("artifacts", {})
    for name in ARTIFACT_NAMES:
        item = artifacts.get(name)
        if item is None:
            continue
        relative = item["path"]
        if relative in seen:
            continue
        seen.add(relative)
        prior = _destination_state(project_dir, relative, allow_external=(name == "output"))
        entry = {
            "destination": relative,
            "external": name == "output",
            "new_sha256": item["sha256"],
            "prior_exists": prior["exists"],
            "prior_sha256": prior["sha256"],
            "backup": None,
        }
        if prior["exists"]:
            backup_name = f"backup/{len(entries):03d}.bin"
            backup = stage_dir / backup_name
            backup.parent.mkdir(parents=True, exist_ok=True)
            _copy_atomic(
                _destination_path(project_dir, relative, allow_external=(name == "output")),
                backup,
            )
            entry["backup"] = backup_name
        entries.append(entry)
    final_relative = finalized_path(project_dir, prepared["render_id"]).relative_to(
        project_dir.resolve()
    ).as_posix()
    if final_relative not in seen:
        prior = _destination_state(project_dir, final_relative)
        entry = {
            "destination": final_relative,
            "external": False,
            "new_sha256": _sha256_bytes(_json_bytes(finalized)),
            "prior_exists": prior["exists"],
            "prior_sha256": prior["sha256"],
            "backup": None,
        }
        if prior["exists"]:
            backup_name = f"backup/{len(entries):03d}.envelope"
            backup = stage_dir / backup_name
            backup.parent.mkdir(parents=True, exist_ok=True)
            _copy_atomic(_destination_path(project_dir, final_relative), backup)
            entry["backup"] = backup_name
        entries.append(entry)
    return entries


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _journal_entry_destination(
    project_dir: Path,
    entry: dict[str, Any],
    *,
    allow_leaf_conflict: bool = False,
) -> Path:
    external = entry.get("external")
    if type(external) is not bool:
        raise DeliveryEnvelopeError("publication journal external flag is invalid")
    relative = entry.get("destination")
    if not isinstance(relative, str):
        raise DeliveryEnvelopeError("publication journal destination is invalid")
    return _destination_path(
        project_dir,
        relative,
        allow_external=external,
        allow_leaf_conflict=allow_leaf_conflict,
    )


def _assert_publication_prior_state(
    project_dir: Path, entry: dict[str, Any]
) -> None:
    destination = _journal_entry_destination(project_dir, entry)
    prior_exists = entry.get("prior_exists")
    if type(prior_exists) is not bool:
        raise DeliveryEnvelopeError("publication journal prior_exists is invalid")
    expected_sha = entry.get("prior_sha256") if prior_exists else None
    current_sha = _observed_destination_sha(
        destination,
        label=str(entry.get("destination")),
    )
    if current_sha != expected_sha:
        raise DeliveryEnvelopeError(
            "publication destination changed after journal preparation: "
            f"{entry.get('destination')}"
        )


def _validate_publication_prior_states(
    project_dir: Path, entries: list[dict[str, Any]]
) -> None:
    for entry in entries:
        _assert_publication_prior_state(project_dir, entry)


def _publication_compensation_plan(
    project_dir: Path,
    stage_dir: Path,
    published_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    stage = stage_dir.resolve()
    for entry in reversed(published_entries):
        destination = _journal_entry_destination(project_dir, entry)
        relative = str(entry.get("destination"))
        new_sha = entry.get("new_sha256")
        if not isinstance(new_sha, str) or not SHA256_PATTERN.fullmatch(new_sha):
            raise DeliveryEnvelopeError(
                f"publication compensation new hash is invalid: {relative}"
            )
        if _observed_destination_sha(destination, label=relative) != new_sha:
            raise DeliveryEnvelopeError(
                "publication compensation blocked by a concurrent external edit: "
                f"{relative}"
            )
        prior_exists = entry.get("prior_exists")
        if type(prior_exists) is not bool:
            raise DeliveryEnvelopeError(
                f"publication compensation prior state is invalid: {relative}"
            )
        prior_sha = entry.get("prior_sha256")
        backup: Path | None = None
        if prior_exists:
            if not isinstance(prior_sha, str) or not SHA256_PATTERN.fullmatch(prior_sha):
                raise DeliveryEnvelopeError(
                    f"publication compensation prior hash is invalid: {relative}"
                )
            backup_value = entry.get("backup")
            if not isinstance(backup_value, str):
                raise DeliveryEnvelopeError(
                    f"publication compensation backup is missing: {relative}"
                )
            parts = backup_value.split("/")
            if (
                "\\" in backup_value
                or any(part in {"", ".", ".."} for part in parts)
                or parts[0] != "backup"
            ):
                raise DeliveryEnvelopeError(
                    f"publication compensation backup path is invalid: {relative}"
                )
            backup = stage / Path(*parts)
            if (
                backup.is_symlink()
                or backup.resolve() != backup
                or not backup.is_file()
                or _sha256(backup) != prior_sha
            ):
                raise DeliveryEnvelopeError(
                    f"publication compensation backup changed: {relative}"
                )
        elif prior_sha is not None or entry.get("backup") is not None:
            raise DeliveryEnvelopeError(
                f"publication compensation absent prior has backup data: {relative}"
            )
        plan.append(
            {
                "destination": destination,
                "relative": relative,
                "new_sha256": new_sha,
                "prior_exists": prior_exists,
                "prior_sha256": prior_sha,
                "backup": backup,
            }
        )
    return plan


def _compensate_published_entries(
    project_dir: Path,
    stage_dir: Path,
    published_entries: list[dict[str, Any]],
) -> None:
    # Validate every entry first so an already-visible conflict causes zero
    # compensation writes.  Re-check each item immediately before mutation.
    plan = _publication_compensation_plan(
        project_dir,
        stage_dir,
        published_entries,
    )
    for item in plan:
        destination = item["destination"]
        if _observed_destination_sha(destination, label=item["relative"]) != item["new_sha256"]:
            raise DeliveryEnvelopeError(
                "publication compensation blocked by a concurrent external edit: "
                f"{item['relative']}"
            )
        if item["prior_exists"]:
            backup = item["backup"]
            if not isinstance(backup, Path) or _sha256(backup) != item["prior_sha256"]:
                raise DeliveryEnvelopeError(
                    f"publication compensation backup changed: {item['relative']}"
                )
            _copy_atomic(backup, destination)
            expected_after = item["prior_sha256"]
        else:
            destination.unlink(missing_ok=True)
            expected_after = None
        if _observed_destination_sha(destination, label=item["relative"]) != expected_after:
            raise DeliveryEnvelopeError(
                f"publication compensation verification failed: {item['relative']}"
            )


def _publish_direct_delivery_locked(
    project_dir: Path,
    stage_dir: Path,
    *,
    owner_token: object,
    staged_sources: Mapping[str, Path] | None = None,
    expected_output: Path | None = None,
    defer_commit: bool = False,
    deferred_transaction_id: str | None = None,
    revalidate_authority: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Publish one prepared direct envelope and return its finalized payload."""
    root = _ensure_staging_trust_chain(project_dir, create=False)
    stage_root, stage_render_id, stage = _stage_identity(stage_dir)
    if stage_root != root:
        raise DeliveryEnvelopeError("staging directory is not project-owned")
    _require_staging_lease(root, stage_render_id, owner_token)
    prepared = _read_json(stage / PREPARED_NAME)
    output_item = (prepared.get("artifacts") or {}).get("output")
    if expected_output is not None:
        if not isinstance(output_item, dict) or output_item.get("path") != str(expected_output.expanduser().resolve()):
            raise DeliveryEnvelopeError("prepared output destination does not match the CLI output")
    sources = dict(staged_sources or _stage_sources(stage, prepared))
    for source in sources.values():
        source_path = Path(source)
        try:
            source_path.resolve().relative_to(stage)
        except ValueError as exc:
            raise DeliveryEnvelopeError("publication source must be under the render staging directory") from exc
    validate_envelope(root, prepared, sources=sources, expected_state="prepared")
    prepared_hash = contract_registry.canonical_hash(prepared)
    finalized = json.loads(json.dumps(prepared))
    finalized["state"] = "finalized"
    finalized["prepared_envelope_hash"] = prepared_hash
    validate_envelope(root, finalized, sources=sources, expected_state="finalized")

    entries = _journal_entries(root, prepared, finalized, stage)
    journal = {
        "schema_version": 1,
        "render_id": prepared["render_id"],
        "entries": entries,
    }
    journal_file = _journal_path(stage)
    _atomic_write_json(journal_file, journal)
    if defer_commit:
        if not isinstance(output_item, dict) or not isinstance(output_item.get("path"), str):
            raise DeliveryEnvelopeError("deferred publication output binding is invalid")
        if deferred_transaction_id is None:
            raise DeliveryEnvelopeError("deferred publication transaction id is missing")
        journal_snapshot = _snapshot_regular_file(
            journal_file,
            label=f"deferred publication journal {prepared['render_id']}",
        )
        pending_binding = {
            "render_id": prepared["render_id"],
            "expected_output": str(
                _canonical_expected_output(Path(output_item["path"]))
            ),
            "journal_sha256": journal_snapshot.sha256,
            "output_sha256": output_item["sha256"],
            "finalized_sha256": _sha256_bytes(_json_bytes(finalized)),
        }
        pending_marker = _deferred_marker_payload(
            pending_binding,
            state="pending",
            transaction_id=deferred_transaction_id,
        )
        marker_path = stage / DEFERRED_NAME
        _atomic_write_json(marker_path, pending_marker)
        _fsync_directory(stage)
        written_marker = _decode_json_snapshot(
            _snapshot_regular_file(
                marker_path,
                label=f"deferred publication marker {prepared['render_id']}",
                capture_bytes=True,
            )
        )
        if written_marker != pending_marker:
            raise DeliveryEnvelopeError(
                "deferred publication marker changed during pending write"
            )
    elif deferred_transaction_id is not None:
        raise DeliveryEnvelopeError("immediate publication has a deferred transaction id")
    published_entries: list[dict[str, Any]] = []
    attempted_entry: dict[str, Any] | None = None
    try:
        # The journal is persisted for process-level recovery before publication.
        # Re-check the recorded prior state so an edit in that gap stops with
        # zero writes. Host/power-loss directory ordering is not claimed here.
        _validate_publication_prior_states(root, entries)
        if revalidate_authority is not None:
            revalidate_authority()
        entries_by_destination = {entry["destination"]: entry for entry in entries}
        artifacts = prepared["artifacts"]
        published_destinations: set[str] = set()
        for name in ARTIFACT_NAMES:
            item = artifacts.get(name)
            if item is None:
                continue
            if item["path"] in published_destinations:
                continue
            published_destinations.add(item["path"])
            source = sources.get(name)
            if source is None:
                raise DeliveryEnvelopeError(f"staged source missing for artifact {name}")
            entry = entries_by_destination.get(item["path"])
            if not isinstance(entry, dict):
                raise DeliveryEnvelopeError(f"publication journal entry is missing for {name}")
            _assert_publication_prior_state(root, entry)
            attempted_entry = entry
            _copy_atomic(
                Path(source),
                _destination_path(root, item["path"], allow_external=(name == "output")),
            )
            published_entries.append(entry)
            attempted_entry = None
        # Re-check every destination before exposing the finalized envelope.
        validate_envelope(root, prepared, expected_state="prepared")
        if revalidate_authority is not None:
            revalidate_authority()
        final_path = finalized_path(root, prepared["render_id"])
        final_relative = final_path.relative_to(root).as_posix()
        final_entry = entries_by_destination.get(final_relative)
        if not isinstance(final_entry, dict):
            raise DeliveryEnvelopeError("publication journal final envelope entry is missing")
        _assert_publication_prior_state(root, final_entry)
        attempted_entry = final_entry
        _atomic_write_json(final_path, finalized)
        published_entries.append(final_entry)
        attempted_entry = None
        written = _read_json(final_path)
        validate_envelope(root, written, expected_state="finalized")
        if written.get("prepared_envelope_hash") != prepared_hash:
            raise DeliveryEnvelopeError("finalized envelope prepared hash changed")
    except Exception as exc:
        if attempted_entry is not None:
            attempted_destination = _journal_entry_destination(root, attempted_entry)
            if _observed_destination_sha(
                attempted_destination,
                label=str(attempted_entry.get("destination")),
            ) == attempted_entry.get("new_sha256"):
                published_entries.append(attempted_entry)
        if not published_entries:
            if isinstance(exc, DeliveryEnvelopeError):
                raise
            raise DeliveryEnvelopeError(
                f"direct publication stopped before mutation; journal kept: {exc}"
            ) from exc
        try:
            _compensate_published_entries(
                root,
                stage,
                published_entries,
            )
        except Exception as compensation_exc:
            raise DeliveryEnvelopeError(
                "direct publication failed and compensation is blocked; "
                f"journal kept: {compensation_exc}"
            ) from exc
        try:
            _validate_restore_plan(
                root,
                stage,
                journal,
                render_id=prepared["render_id"],
                expected_output=Path(output_item["path"]),
            )
        except DeliveryEnvelopeError:
            # A later, unpublished destination contains an external edit.  It
            # was never touched; keep the journal/stage for explicit recovery.
            pass
        else:
            _remove_stage(stage)
        if isinstance(exc, DeliveryEnvelopeError):
            raise
        raise DeliveryEnvelopeError(f"direct publication failed: {exc}") from exc
    if not defer_commit:
        try:
            _remove_stage(stage)
        except (OSError, DeliveryEnvelopeError):
            # The finalized envelope is already published and validated. Leaving
            # the journaled staging directory lets the next matching process retry
            # cleanup without falsely reporting that publication itself failed.
            pass
    return finalized


def publish_direct_delivery(
    project_dir: Path,
    authority: StagingAttempt,
    *,
    staged_sources: Mapping[str, Path] | None = None,
    expected_output: Path | None = None,
    defer_commit: bool = False,
    revalidate_authority: Callable[[], None] | None = None,
) -> dict[str, Any] | DeferredPublication:
    root = project_dir.resolve()
    if not isinstance(authority, StagingAttempt):
        raise DeliveryEnvelopeError("staging attempt authority is required for publication")
    render_id = authority.render_id
    attempt = _validate_staging_attempt(root, render_id, authority)
    deferred_transaction_id = uuid.uuid4().hex if defer_commit else None
    release_lease = True
    try:
        finalized = _publish_direct_delivery_locked(
            root,
            attempt.stage_dir,
            owner_token=attempt._owner_token,
            staged_sources=staged_sources,
            expected_output=expected_output,
            defer_commit=defer_commit,
            deferred_transaction_id=deferred_transaction_id,
            revalidate_authority=revalidate_authority,
        )
        if not defer_commit:
            return finalized
        output_item = (finalized.get("artifacts") or {}).get("output")
        if not isinstance(output_item, dict) or not isinstance(output_item.get("path"), str):
            raise DeliveryEnvelopeError("deferred publication output binding is invalid")
        authority = DeferredPublication(
            project_dir=root,
            render_id=render_id,
            stage_dir=attempt.stage_dir,
            expected_output=_canonical_expected_output(Path(output_item["path"])),
            finalized=finalized,
            _transaction_id=str(deferred_transaction_id),
            _owner_token=attempt._owner_token,
        )
        _validate_deferred_publication(authority)
        release_lease = False
        return authority
    except Exception as exc:
        try:
            stage = attempt.stage_dir
            journal_exists = stage.is_dir() and _journal_path(stage).exists()
            if defer_commit and journal_exists:
                recovery_output = expected_output
                if recovery_output is None:
                    prepared = _read_json(stage / PREPARED_NAME)
                    prepared_output = (prepared.get("artifacts") or {}).get("output")
                    if not isinstance(prepared_output, dict) or not isinstance(
                        prepared_output.get("path"), str
                    ):
                        raise DeliveryEnvelopeError(
                            "deferred publication recovery output binding is invalid"
                        )
                    recovery_output = Path(prepared_output["path"])
                # No DeferredPublication authority exists yet.  Recover under
                # the still-live staging lease so post-publication validation
                # failures cannot orphan public bytes or rollback authority.
                _rollback_pending_publication_locked(
                    root,
                    render_id,
                    expected_output=Path(recovery_output),
                    owner_token=attempt._owner_token,
                )
            elif stage.is_dir() and not journal_exists:
                _remove_stage(stage)
        except (OSError, DeliveryEnvelopeError) as recovery_exc:
            raise DeliveryEnvelopeError(
                "deferred publication acquisition failed and locked rollback is "
                f"blocked; recovery state kept: {recovery_exc}"
            ) from exc
        raise
    finally:
        if release_lease:
            _release_staging_lease(root, render_id, attempt._owner_token)
