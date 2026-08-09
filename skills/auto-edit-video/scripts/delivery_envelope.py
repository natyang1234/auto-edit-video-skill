#!/usr/bin/env python3
"""Hash-bound publication for the direct final renderer route.

The renderer produces private bytes first.  This module is the only place
that turns those bytes into a public direct delivery: a prepared envelope is
validated against staged bytes, every destination is journaled and published,
and a finalized envelope is written last.  A journal is deliberately kept
when recovery cannot prove that a destination is still ours.
"""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
import re
import shutil
import stat
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import contract_registry


DELIVERY_REL = Path("working/delivery_envelopes")
STAGING_REL = DELIVERY_REL / ".staging"
LOCKS_REL = STAGING_REL / ".locks"
JOURNAL_NAME = "publication_journal.json"
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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
    project_dir: Path, relative: str, *, allow_external: bool = False
) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise DeliveryEnvelopeError("artifact path must be a project-relative POSIX path")
    if allow_external and relative.startswith("/"):
        entry = Path(relative).expanduser()
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


def validate_envelope(
    project_dir: Path,
    envelope: dict[str, Any],
    *,
    sources: Mapping[str, Path] | None = None,
    expected_state: str | None = None,
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
    _validate_artifact_bytes(project_dir, envelope, sources)


def _profile_binding(state: dict[str, Any]) -> tuple[str, str]:
    import director_resolver

    director_id = str(state.get("director_style") or "teacher-punch")
    try:
        resolved = director_resolver.resolve_director_profile(director_id)
    except Exception as exc:
        raise DeliveryEnvelopeError(f"director profile could not be resolved: {director_id}") from exc
    profile_hash = resolved.get("resolved_hash")
    if not isinstance(profile_hash, str):
        raise DeliveryEnvelopeError("director profile has no resolved hash")
    return director_id, profile_hash


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
) -> dict[str, Any]:
    """Build a prepared v1 payload from observed private artifact bytes."""
    root = project_dir.resolve()
    profile_id, profile_hash = _profile_binding(state)
    import editor_server

    cut_map = root / "working/cut_map.json"
    if cut_map.is_symlink():
        raise DeliveryEnvelopeError("working/cut_map.json must not be a symlink")
    cut_map_hash = _sha256(cut_map) if cut_map.is_file() else None
    artifact_payload: dict[str, Any] = {}
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
        artifact_payload[name] = _artifact_record(
            root,
            Path(source),
            destination,
            allow_external=(name == "output"),
        )

    prepared = {
        "schema_version": 1,
        "route": "direct",
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
    validate_envelope(root, prepared, sources=staged_sources, expected_state="prepared")
    return prepared


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
    return _sha256(path)


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


def _canonical_expected_output(expected_output: Path) -> Path:
    entry = Path(expected_output).expanduser()
    if entry.is_symlink():
        raise DeliveryEnvelopeError("expected output must not be a symlink")
    return entry.resolve()


def _phase0b_destinations(
    project_dir: Path,
    render_id: str,
    expected_output: Path,
    *,
    include_caption_v2: bool = False,
) -> dict[str, tuple[str, bool]]:
    root = project_dir.resolve()
    output = _canonical_expected_output(expected_output)
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
    for relative, external in destinations.values():
        destination = _destination_path(root, relative, allow_external=external)
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
) -> dict[str, tuple[str, bool]]:
    if envelope.get("render_id") != render_id:
        raise DeliveryEnvelopeError("delivery envelope render_id does not match recovery target")
    artifacts = envelope.get("artifacts")
    if not isinstance(artifacts, dict):
        raise DeliveryEnvelopeError("delivery envelope artifacts are invalid")
    destinations = _phase0b_destinations(
        project_dir,
        render_id,
        expected_output,
        include_caption_v2=artifacts.get("caption_v2") is not None,
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


def _recovery_expected_entries(
    project_dir: Path,
    stage_dir: Path,
    render_id: str,
    expected_output: Path,
) -> list[dict[str, Any]]:
    prepared = _read_json(stage_dir / PREPARED_NAME)
    destinations = _validate_phase0b_artifact_destinations(
        project_dir, prepared, render_id, expected_output
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
    recovery_names = list(RECOVERY_ARTIFACT_NAMES)
    if artifacts.get("caption_v2") is not None:
        recovery_names.append("caption_v2")
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
) -> list[dict[str, Any]]:
    if set(journal) != JOURNAL_KEYS or type(journal.get("schema_version")) is not int:
        raise DeliveryEnvelopeError("publication journal shape is invalid")
    if journal["schema_version"] != 1 or journal.get("render_id") != render_id:
        raise DeliveryEnvelopeError("publication journal identity is invalid")
    entries = journal.get("entries")
    if not isinstance(entries, list):
        raise DeliveryEnvelopeError("publication journal entries are invalid")
    expected_entries = _recovery_expected_entries(
        project_dir, stage_dir, render_id, expected_output
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
            project_dir, relative, allow_external=external
        )
        current_sha = _observed_destination_sha(destination, label=relative)
        permitted = {new_sha, prior_sha} if prior_exists else {new_sha, None}
        if current_sha not in permitted:
            raise DeliveryEnvelopeError(
                f"journal destination was externally changed; refusing overwrite: {relative}"
            )
        plan.append(
            {
                "relative": relative,
                "destination": destination,
                "current_sha256": current_sha,
                "prior_exists": prior_exists,
                "prior_sha256": prior_sha,
                "backup": backup,
                "new_sha256": new_sha,
                "new_source": expected["new_source"],
                "new_payload": expected["new_payload"],
            }
        )
    return plan


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
    project_dir: Path, entry: dict[str, Any]
) -> Path:
    external = entry.get("external")
    if type(external) is not bool:
        raise DeliveryEnvelopeError("publication journal external flag is invalid")
    relative = entry.get("destination")
    if not isinstance(relative, str):
        raise DeliveryEnvelopeError("publication journal destination is invalid")
    return _destination_path(project_dir, relative, allow_external=external)


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
    published_entries: list[dict[str, Any]] = []
    attempted_entry: dict[str, Any] | None = None
    try:
        # The journal is durable before publication.  Re-check the entire
        # recorded prior state so an edit in that gap stops with zero writes.
        _validate_publication_prior_states(root, entries)
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
    try:
        _remove_stage(stage)
    except (OSError, DeliveryEnvelopeError):
        # The finalized envelope is already durable and validated.  Leaving the
        # journaled staging directory lets the next matching run retry cleanup
        # without falsely reporting that publication itself failed.
        pass
    return finalized


def publish_direct_delivery(
    project_dir: Path,
    authority: StagingAttempt,
    *,
    staged_sources: Mapping[str, Path] | None = None,
    expected_output: Path | None = None,
) -> dict[str, Any]:
    root = project_dir.resolve()
    if not isinstance(authority, StagingAttempt):
        raise DeliveryEnvelopeError("staging attempt authority is required for publication")
    render_id = authority.render_id
    attempt = _validate_staging_attempt(root, render_id, authority)
    try:
        return _publish_direct_delivery_locked(
            root,
            attempt.stage_dir,
            owner_token=attempt._owner_token,
            staged_sources=staged_sources,
            expected_output=expected_output,
        )
    except Exception:
        try:
            stage = attempt.stage_dir
            if stage.is_dir() and not _journal_path(stage).exists():
                _remove_stage(stage)
        except DeliveryEnvelopeError:
            pass
        raise
    finally:
        _release_staging_lease(root, render_id, attempt._owner_token)
