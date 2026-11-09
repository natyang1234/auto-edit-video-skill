"""Durable, project-local named editor versions.

This module owns only bounded JSON records and the three-file restore journal.
Editor-specific validation (source/assets/plans) remains in ``editor_server``.
Immutable no-overwrite publication requires same-filesystem hard-link support;
there is deliberately no rename or direct-write fallback that could replace or
partially expose a formal record.
The threat model is a cooperative local writer; records are integrity checked,
but their SHA-256 values are not authentication against a malicious account.
"""

from __future__ import annotations

import base64
import copy
import errno
import hashlib
import json
import os
import re
import stat
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RECORDS_REL = Path("working/project_versions/records")
JOURNAL_REL = Path("working/.project-version-restore-journal.json")
MAX_RECORDS = 100
MAX_RECORD_BYTES = 32 * 1024 * 1024
MAX_JOURNAL_BYTES = 128 * 1024 * 1024
VERSION_ID_RE = re.compile(r"pv_[0-9a-f]{32}")
RECORD_TEMP_RE = re.compile(r"\.pv-record-[0-9a-f]{32}\.tmp")
UNSUPPORTED_LINK_ERRNOS = frozenset(
    {
        errno.ENOTSUP,
        errno.EOPNOTSUPP,
        errno.ENOSYS,
        errno.EPERM,
    }
)

RESTORABLE_STATE_FIELDS = frozenset(
    {
        "schema_version",
        "project_id",
        "source_sha256",
        "caption_generation",
        "segments",
        "variants",
        "highlight_plan_revision",
        "active_highlight_id",
        "highlights",
        "canvas",
        "director_style",
        "visual_quality_mode",
        "graphic_package_style",
        "style_pack",
        "video_template",
        "editing_brief",
        "caption_defaults",
        "caption_delivery",
        "audio_event_edits",
        "overlays",
        "qa_policy",
        "publishing",
        "review",
        "rights",
        "migrated_from",
    }
)
DERIVED_STATE_FIELDS = frozenset({"updated_at", "revision", "asset_digests"})
ALLOWED_STATE_FIELDS = RESTORABLE_STATE_FIELDS | DERIVED_STATE_FIELDS

RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "version_id",
        "name",
        "kind",
        "created_at",
        "project_id",
        "captured_state_digest",
        "captured_render_revision",
        "snapshot_state",
        "snapshot_state_digest",
        "snapshot_render_revision",
        "source_binding",
        "asset_bindings",
        "dependency_bindings",
        "record_digest",
    }
)
TARGET_ORDER = (
    "project.json",
    "working/rights_assertion.json",
    "working/editor_state.json",
)


class ProjectVersionError(RuntimeError):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


class ProjectVersionRecoveryError(RuntimeError):
    """A retained restore journal cannot be safely completed."""


class UnsafeFilePermissionsError(ValueError):
    def __init__(self, mode: int):
        super().__init__("file permissions are not owner-only read/write")
        self.mode = mode


class ProjectVersionInventory:
    """A validated, request-local view of one store's immutable records."""

    def __init__(self, owner_key: object, records: list[dict[str, Any]]):
        self._owner_key = owner_key
        self.records = tuple(records)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")


def validate_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ProjectVersionError(422, "validation_error", "版本名稱必須是文字")
    name = value.strip()
    if any(
        unicodedata.category(character) in {"Cc", "Zl", "Zp"}
        for character in value
    ) or not 1 <= len(name) <= 80:
        raise ProjectVersionError(
            422,
            "validation_error",
            "版本名稱需為 1 到 80 字，且不可含控制字元",
        )
    return name


def _valid_optional_utc_iso_timestamp(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        return False
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(parsed)


def sanitize_state(state: Any) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise ProjectVersionError(409, "unsupported_snapshot", "目前編輯內容格式無效")
    unknown = sorted(set(state) - ALLOWED_STATE_FIELDS)
    if unknown:
        raise ProjectVersionError(
            409,
            "unsupported_snapshot",
            "目前編輯內容包含尚未支援的欄位",
            details={"unsupported_fields": unknown},
        )
    snapshot = {
        key: copy.deepcopy(state[key])
        for key in RESTORABLE_STATE_FIELDS
        if key in state
    }

    variants = snapshot.get("variants")
    if isinstance(variants, list):
        for variant in variants:
            if not isinstance(variant, dict) or set(variant) - {
                "variant_id",
                "preset_id",
                "overrides",
            }:
                raise ProjectVersionError(
                    409,
                    "unsupported_snapshot",
                    "輸出變體包含尚未支援的欄位",
                )

    highlights = snapshot.get("highlights")
    if isinstance(highlights, list):
        for highlight in highlights:
            if not isinstance(highlight, dict):
                continue
            review_status = highlight.get("review_status")
            if review_status == "approved":
                highlight["review_status"] = "pending"

    overlays = snapshot.get("overlays")
    if isinstance(overlays, list):
        overlay_ids = {
            str(item.get("id"))
            for item in overlays
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        for overlay in overlays:
            if not isinstance(overlay, dict):
                continue
            semantic = overlay.get("semantic_review")
            if not isinstance(semantic, dict):
                continue
            if set(semantic) - {
                "status",
                "unit_id",
                "candidates",
                "reviewer",
                "approved_at",
                "checked_at",
                "resolved_at",
            }:
                raise ProjectVersionError(
                    409,
                    "unsupported_snapshot",
                    "字幕語意審核包含尚未支援的欄位",
                )
            status_value = semantic.get("status")
            if not isinstance(status_value, str) or status_value not in {
                "checked",
                "approved",
                "pending",
                "rejected",
                "resolved_manual",
            }:
                raise ProjectVersionError(
                    409,
                    "unsupported_snapshot",
                    "字幕語意審核狀態無法安全還原",
                )
            if "resolved_at" in semantic and not _valid_optional_utc_iso_timestamp(
                semantic["resolved_at"]
            ):
                raise ProjectVersionError(
                    409,
                    "unsupported_snapshot",
                    "字幕語意審核時間無法安全還原",
                )
            if status_value in {"checked", "approved"}:
                semantic["status"] = "pending"
            for key in ("reviewer", "approved_at", "checked_at"):
                semantic.pop(key, None)
    else:
        overlay_ids = set()

    review = snapshot.get("review")
    if isinstance(review, dict) and set(review) - {
        "selected_overlay_id",
        "warnings_acknowledged",
    }:
        raise ProjectVersionError(
            409, "unsupported_snapshot", "審核面板包含尚未支援的欄位"
        )
    if review is not None and not isinstance(review, dict):
        raise ProjectVersionError(
            409, "unsupported_snapshot", "審核面板格式無法安全還原"
        )
    selected = review.get("selected_overlay_id") if isinstance(review, dict) else None
    snapshot["review"] = {
        "selected_overlay_id": selected if selected in overlay_ids else None,
        "warnings_acknowledged": [],
    }

    publishing = snapshot.get("publishing")
    if isinstance(publishing, dict):
        if set(publishing) - {
            "platform_id",
            "draft_status",
            "title",
            "body",
            "hashtags",
            "cover",
            "generator",
        }:
            raise ProjectVersionError(
                409, "unsupported_snapshot", "發布草稿包含尚未支援的欄位"
            )
        title = str(publishing.get("title") or "")
        body = str(publishing.get("body") or "")
        hashtags = copy.deepcopy(publishing.get("hashtags") or [])
        if (
            "generator" in publishing
            and publishing["generator"] != "deterministic-transcript-draft-v1"
        ):
            raise ProjectVersionError(
                409,
                "unsupported_snapshot",
                "發布草稿產生器無法安全還原",
            )
        cover = publishing.get("cover") if isinstance(publishing.get("cover"), dict) else {}
        if publishing.get("cover") is not None and not isinstance(publishing.get("cover"), dict):
            raise ProjectVersionError(
                409, "unsupported_snapshot", "封面草稿格式無法安全還原"
            )
        if set(cover) - {"time", "text", "output"}:
            raise ProjectVersionError(
                409, "unsupported_snapshot", "封面草稿包含尚未支援的欄位"
            )
        snapshot["publishing"] = {
            "platform_id": publishing.get("platform_id"),
            "draft_status": (
                "local_draft_requires_review"
                if title.strip() or body.strip() or hashtags
                else "not_generated"
            ),
            "title": title,
            "body": body,
            "hashtags": hashtags,
            "cover": {
                "time": cover.get("time"),
                "text": str(cover.get("text") or ""),
                "output": None,
            },
        }
        if publishing.get("generator") == "deterministic-transcript-draft-v1":
            snapshot["publishing"]["generator"] = publishing["generator"]
    elif publishing is not None:
        raise ProjectVersionError(
            409, "unsupported_snapshot", "發布草稿格式無法安全還原"
        )
    rights = snapshot.get("rights")
    if isinstance(rights, dict) and set(rights) - {"asserted", "assertion_revision"}:
        raise ProjectVersionError(
            409, "unsupported_snapshot", "權利狀態包含尚未支援的欄位"
        )
    if rights is not None and not isinstance(rights, dict):
        raise ProjectVersionError(
            409, "unsupported_snapshot", "權利狀態格式無法安全還原"
        )
    snapshot["rights"] = {"asserted": False, "assertion_revision": None}
    return snapshot


def sanitize_manifest(
    manifest: dict[str, Any], gates: tuple[str, ...], *, timestamp: str
) -> tuple[dict[str, Any], list[str]]:
    target = copy.deepcopy(manifest)
    old = manifest.get("approvals") if isinstance(manifest.get("approvals"), dict) else {}

    def was_approved(key: Any, value: Any) -> bool:
        if str(key).endswith("_by_variant"):
            return isinstance(value, dict) and any(
                isinstance(slot, dict) and slot.get("approved") is True
                for slot in value.values()
            )
        return isinstance(value, dict) and bool(value.get("approved"))

    invalidated = sorted(
        str(key)
        for key, value in old.items()
        if was_approved(key, value)
    )
    target["approvals"] = {
        gate: {
            "approved": False,
            "confirmed_by": None,
            "at": None,
            "note": "Invalidated because a project version was restored",
            "invalidated_at": timestamp,
        }
        for gate in gates
    }
    stages = target.setdefault("stages", {})
    for stage in ("edit_review", "highlight_plan", "timeline_review"):
        stages[stage] = "needs_review"
    stages["render"] = "pending"
    stages["qa"] = "pending"
    target["updated_at"] = timestamp
    return target, invalidated


def empty_rights_assertion() -> dict[str, Any]:
    value: dict[str, Any] = {"schema_version": 1, "items": []}
    value["revision"] = canonical_digest(value)
    return value


def metadata(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "version_id": record["version_id"],
        "name": record["name"],
        "kind": record["kind"],
        "created_at": record["created_at"],
        "state_digest": record["captured_state_digest"],
        "render_revision": record["captured_render_revision"],
    }


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ProjectVersionError(503, "version_recovery_required", "版本儲存區格式不安全")


def _remove_created_directory_if_unchanged(
    path: Path, identity: tuple[int, int]
) -> None:
    try:
        info = path.lstat()
    except OSError:
        return
    if (
        not stat.S_ISDIR(info.st_mode)
        or (info.st_dev, info.st_ino) != identity
    ):
        return
    try:
        path.rmdir()
    except OSError:
        pass


def _create_owned_directory_exact(path: Path) -> tuple[int, int]:
    os.mkdir(path, 0o700)
    descriptor = -1
    identity: tuple[int, int] | None = None
    try:
        path_info = path.lstat()
        if not stat.S_ISDIR(path_info.st_mode):
            raise OSError("new owned path is not a directory")
        identity = (path_info.st_dev, path_info.st_ino)
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        descriptor_info = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(descriptor_info.st_mode)
            or (descriptor_info.st_dev, descriptor_info.st_ino) != identity
        ):
            raise OSError("new owned directory identity changed")
        os.fchmod(descriptor, 0o700)
        return identity
    except BaseException:
        if identity is not None:
            _remove_created_directory_if_unchanged(path, identity)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _assert_owned_parents(project_dir: Path, path: Path, *, create: bool) -> None:
    root = project_dir.resolve()
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise OSError("target escapes project") from exc
    cursor = root
    created_directories: list[tuple[Path, tuple[int, int]]] = []
    try:
        for part in relative.parts[:-1]:
            cursor = cursor / part
            if not os.path.lexists(cursor):
                if not create:
                    raise OSError("target parent is missing")
                identity = _create_owned_directory_exact(cursor)
                created_directories.append((cursor, identity))
            info = cursor.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise OSError("target parent is not an owned directory")
    except BaseException:
        for created_path, identity in reversed(created_directories):
            _remove_created_directory_if_unchanged(created_path, identity)
        raise


def _read_regular(path: Path, maximum: int) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except PermissionError as open_error:
        # Diagnostic only: a write-only owner mode can prevent open() before
        # fstat() reaches the explicit permission classification. Never use
        # this path to read, write, follow links, or claim content validity.
        try:
            path_info = path.lstat()
        except OSError:
            raise open_error
        mode = stat.S_IMODE(path_info.st_mode)
        if (
            stat.S_ISREG(path_info.st_mode)
            and path_info.st_size <= maximum
            and mode != 0o600
        ):
            raise UnsafeFilePermissionsError(mode) from open_error
        raise
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError("unsafe file")
        mode = stat.S_IMODE(info.st_mode)
        if mode != 0o600:
            raise UnsafeFilePermissionsError(mode)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65536, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise ValueError("file too large")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _atomic_replace_exact(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise OSError("target directory must not be a symlink")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("restore write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


class ProjectVersionStore:
    def __init__(self, project_dir: Path):
        self.project_dir = project_dir.resolve()
        self.records_dir = self.project_dir / RECORDS_REL
        self._inventory_key = object()

    def _record_path(self, version_id: str) -> Path:
        if VERSION_ID_RE.fullmatch(version_id) is None:
            raise ProjectVersionError(404, "version_not_found", "找不到指定版本")
        return self.records_dir / f"{version_id}.json"

    def _validate_record(self, record: Any) -> dict[str, Any]:
        if not isinstance(record, dict) or set(record) != RECORD_FIELDS:
            raise ValueError("version record schema is invalid")
        if record.get("schema_version") != 1:
            raise ValueError("version record schema version is invalid")
        version_id = record.get("version_id")
        if not isinstance(version_id, str) or VERSION_ID_RE.fullmatch(version_id) is None:
            raise ValueError("version record id is invalid")
        if record.get("kind") not in {"named", "auto_before_restore"}:
            raise ValueError("version record kind is invalid")
        try:
            validate_name(record.get("name"))
        except ProjectVersionError as exc:
            raise ValueError("version record name is invalid") from exc
        if not isinstance(record.get("project_id"), str) or not record["project_id"]:
            raise ValueError("version record project id is invalid")
        if not isinstance(record.get("snapshot_state"), dict):
            raise ValueError("version snapshot state is invalid")
        for key in (
            "captured_state_digest",
            "captured_render_revision",
            "snapshot_state_digest",
            "snapshot_render_revision",
            "record_digest",
        ):
            if not isinstance(record.get(key), str) or re.fullmatch(
                r"[0-9a-f]{64}", record[key]
            ) is None:
                raise ValueError("version record digest is invalid")
        if canonical_digest(record["snapshot_state"]) != record["snapshot_state_digest"]:
            raise ValueError("version snapshot digest does not match")
        try:
            sanitized_snapshot = sanitize_state(record["snapshot_state"])
        except ProjectVersionError as exc:
            raise ValueError("version snapshot fields are invalid") from exc
        if canonical_digest(sanitized_snapshot) != canonical_digest(record["snapshot_state"]):
            raise ValueError("version snapshot is not the sanitized restore projection")
        material = {key: value for key, value in record.items() if key != "record_digest"}
        if canonical_digest(material) != record["record_digest"]:
            raise ValueError("version record digest does not match")
        source = record.get("source_binding")
        if (
            not isinstance(source, dict)
            or set(source) != {"path", "sha256"}
            or not isinstance(source.get("path"), str)
            or not source["path"]
            or Path(source["path"]).is_absolute()
            or ".." in Path(source["path"]).parts
            or not isinstance(source.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", source["sha256"]) is None
        ):
            raise ValueError("version source binding is invalid")
        assets = record.get("asset_bindings")
        if not isinstance(assets, dict) or any(
            not isinstance(path, str)
            or not path
            or Path(path).is_absolute()
            or ".." in Path(path).parts
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            for path, digest in assets.items()
        ):
            raise ValueError("version asset bindings are invalid")
        dependencies = record.get("dependency_bindings")
        if not isinstance(dependencies, dict) or any(
            not isinstance(path, str)
            or Path(path).is_absolute()
            or ".." in Path(path).parts
            or not isinstance(binding, dict)
            or set(binding) != {"exists", "sha256"}
            or type(binding["exists"]) is not bool
            or (
                binding["exists"] is True
                and (
                    not isinstance(binding["sha256"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", binding["sha256"]) is None
                )
            )
            or (binding["exists"] is False and binding["sha256"] is not None)
            for path, binding in dependencies.items()
        ):
            raise ValueError("version dependency bindings are invalid")
        return record

    def load(self, version_id: str) -> dict[str, Any]:
        path = self._record_path(version_id)
        try:
            raw = _read_regular(path, MAX_RECORD_BYTES)
            record = json.loads(raw.decode("utf-8"))
            validated = self._validate_record(record)
            if validated["version_id"] != version_id:
                raise ValueError("version id does not match filename")
            return validated
        except FileNotFoundError as exc:
            raise ProjectVersionError(404, "version_not_found", "找不到指定版本") from exc
        except ProjectVersionError:
            raise
        except UnsafeFilePermissionsError as exc:
            raise ProjectVersionError(
                503,
                "version_permissions_repair_required",
                "版本記錄的分享權限不安全，請設為僅本人可讀寫後重試",
                details={
                    "record_id": version_id,
                    "expected_mode": "0600",
                    "current_mode": f"{exc.mode:04o}",
                    "relative_path": (RECORDS_REL / f"{version_id}.json").as_posix(),
                    "repair_action": "restrict_to_owner",
                },
            ) from exc
        except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProjectVersionError(
                503,
                "version_recovery_required",
                "版本記錄損毀，已停止版本操作",
            ) from exc

    def list_records(self) -> list[dict[str, Any]]:
        if not os.path.lexists(self.records_dir):
            return []
        try:
            _assert_owned_parents(
                self.project_dir, self.records_dir / "placeholder.json", create=False
            )
        except OSError as exc:
            raise ProjectVersionError(
                503, "version_recovery_required", "版本儲存區格式不安全"
            ) from exc
        if self.records_dir.is_symlink() or not self.records_dir.is_dir():
            raise ProjectVersionError(503, "version_recovery_required", "版本儲存區格式不安全")
        records: list[dict[str, Any]] = []
        removed_temporary = False
        for entry in sorted(os.scandir(self.records_dir), key=lambda item: item.name):
            if entry.name == ".DS_Store":
                try:
                    metadata_info = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise ProjectVersionError(
                        503,
                        "version_recovery_required",
                        "版本儲存區包含無法驗證的記錄",
                    ) from exc
                if not stat.S_ISREG(metadata_info.st_mode):
                    raise ProjectVersionError(
                        503,
                        "version_recovery_required",
                        "版本儲存區包含無法驗證的記錄",
                    )
                # Known Finder metadata is not a version record. Never read,
                # remove, validate, or include it in capacity accounting.
                continue
            if RECORD_TEMP_RE.fullmatch(entry.name) is not None:
                try:
                    info = entry.stat(follow_symlinks=False)
                    if (
                        entry.is_symlink()
                        or not stat.S_ISREG(info.st_mode)
                        or stat.S_IMODE(info.st_mode) & 0o077
                    ):
                        raise ValueError("unsafe project-version temporary file")
                    os.unlink(entry.path)
                    removed_temporary = True
                except (OSError, ValueError) as exc:
                    raise ProjectVersionError(
                        503,
                        "version_storage_unavailable",
                        "版本暫存記錄無法安全清理",
                    ) from exc
                continue
            match = re.fullmatch(r"(pv_[0-9a-f]{32})\.json", entry.name)
            if match is None or entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                raise ProjectVersionError(
                    503,
                    "version_recovery_required",
                    "版本儲存區包含無法驗證的記錄",
                )
            records.append(self.load(match.group(1)))
        if removed_temporary:
            try:
                _fsync_directory(self.records_dir)
            except OSError as exc:
                raise ProjectVersionError(
                    503,
                    "version_storage_unavailable",
                    "版本暫存記錄清理結果無法確認",
                ) from exc
        if len(records) > MAX_RECORDS:
            raise ProjectVersionError(503, "version_recovery_required", "版本數量超出安全上限")
        return sorted(records, key=lambda item: (item["created_at"], item["version_id"]), reverse=True)

    def inventory(self) -> ProjectVersionInventory:
        return ProjectVersionInventory(self._inventory_key, self.list_records())

    def _inventory_records(
        self, inventory: ProjectVersionInventory | None
    ) -> tuple[dict[str, Any], ...]:
        if inventory is None:
            inventory = self.inventory()
        if (
            not isinstance(inventory, ProjectVersionInventory)
            or inventory._owner_key is not self._inventory_key
        ):
            raise ValueError("project-version inventory belongs to another store")
        return inventory.records

    def capacity(
        self, inventory: ProjectVersionInventory | None = None
    ) -> dict[str, Any]:
        used = len(self._inventory_records(inventory))
        return {
            "used": used,
            "max": MAX_RECORDS,
            "named_save_available": used <= 98,
            "restore_backup_available": used <= 99,
        }

    def create(
        self,
        *,
        name: str,
        kind: str,
        state: dict[str, Any],
        captured_render_revision: str,
        snapshot_state: dict[str, Any],
        snapshot_render_revision: str,
        source_binding: dict[str, Any],
        asset_bindings: dict[str, str],
        dependency_bindings: dict[str, Any],
        inventory: ProjectVersionInventory | None = None,
    ) -> dict[str, Any]:
        records = self._inventory_records(inventory)
        used = len(records)
        if kind == "named" and used >= 99:
            raise ProjectVersionError(
                409,
                "version_capacity_reserved",
                "版本空間需保留給還原前自動備份",
            )
        if kind == "auto_before_restore" and used >= MAX_RECORDS:
            raise ProjectVersionError(
                507,
                "version_capacity_exhausted",
                "版本空間已滿，無法先建立還原前備份",
            )
        if kind not in {"named", "auto_before_restore"}:
            raise ValueError("invalid version kind")
        normalized_name = validate_name(name)
        _assert_owned_parents(
            self.project_dir, self.records_dir / "placeholder.json", create=True
        )
        _ensure_directory(self.records_dir)
        for _attempt in range(8):
            version_id = f"pv_{uuid.uuid4().hex}"
            path = self._record_path(version_id)
            record: dict[str, Any] = {
                "schema_version": 1,
                "version_id": version_id,
                "name": normalized_name,
                "kind": kind,
                "created_at": now_utc(),
                "project_id": state.get("project_id"),
                "captured_state_digest": canonical_digest(state),
                "captured_render_revision": captured_render_revision,
                "snapshot_state": copy.deepcopy(snapshot_state),
                "snapshot_state_digest": canonical_digest(snapshot_state),
                "snapshot_render_revision": snapshot_render_revision,
                "source_binding": copy.deepcopy(source_binding),
                "asset_bindings": copy.deepcopy(asset_bindings),
                "dependency_bindings": copy.deepcopy(dependency_bindings),
                "record_digest": "",
            }
            material = {key: value for key, value in record.items() if key != "record_digest"}
            record["record_digest"] = canonical_digest(material)
            self._validate_record(record)
            raw = json_bytes(record)
            if len(raw) > MAX_RECORD_BYTES:
                raise ProjectVersionError(409, "unsupported_snapshot", "版本內容超出安全上限")
            temporary = self.records_dir / f".pv-record-{uuid.uuid4().hex}.tmp"
            descriptor = -1
            published = False
            temporary_created = False
            try:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                temporary_created = True
                os.fchmod(descriptor, 0o600)
                view = memoryview(raw)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("version record write made no progress")
                    view = view[written:]
                os.fsync(descriptor)
                os.close(descriptor)
                descriptor = -1

                # A hard link publishes the already-fsynced inode atomically
                # without ever replacing an existing immutable record.
                try:
                    os.link(temporary, path, follow_symlinks=False)
                except OSError as exc:
                    if exc.errno in UNSUPPORTED_LINK_ERRNOS:
                        raise ProjectVersionError(
                            503,
                            "version_storage_unsupported",
                            "目前專案位置不支援或不允許安全儲存版本；"
                            "請將完整專案移到本機支援位置後重試",
                        ) from exc
                    raise
                published = True
                _fsync_directory(self.records_dir)
            except FileExistsError:
                # Only the final id collision is retryable. A temporary-name
                # collision is not expected with a server-generated UUID and
                # is surfaced instead of risking an ambiguous publication.
                if descriptor >= 0:
                    os.close(descriptor)
                    descriptor = -1
                if temporary_created:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
                if temporary_created and os.path.lexists(path):
                    continue
                raise
            except BaseException:
                if descriptor >= 0:
                    os.close(descriptor)
                    descriptor = -1
                # The reserved temporary name is never a formal record. Keep
                # a complete published final record even if directory fsync
                # fails; cleanup is limited to this pre-publication alias.
                if temporary_created:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
                raise
            try:
                temporary.unlink()
                _fsync_directory(self.records_dir)
            except OSError:
                # The final link is complete and immutable. Never remove it to
                # make cleanup look successful; list_records can safely clean
                # a reserved leftover temporary alias on the next request.
                raise
            if not published:
                raise OSError("version record was not published")
            return self.load(version_id)
        raise OSError("could not allocate a unique version id")


def ensure_restore_ready(project_dir: Path) -> None:
    if os.path.lexists(project_dir / JOURNAL_REL):
        raise ProjectVersionRecoveryError("project version restore recovery is required")


def _journal_payload(auto_record: dict[str, Any], targets: dict[str, bytes]) -> dict[str, Any]:
    if set(targets) != set(TARGET_ORDER):
        raise ValueError("restore targets are not the fixed authority set")
    payload: dict[str, Any] = {
        "schema_version": 1,
        "phase": "prepared",
        "automatic_backup": {
            "version_id": auto_record["version_id"],
            "record_digest": auto_record["record_digest"],
        },
        "targets": {
            relative: {
                "sha256": hashlib.sha256(targets[relative]).hexdigest(),
                "data_base64": base64.b64encode(targets[relative]).decode("ascii"),
            }
            for relative in TARGET_ORDER
        },
    }
    return payload


def _read_journal(project_dir: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    path = project_dir / JOURNAL_REL
    raw = _read_regular(path, MAX_JOURNAL_BYTES)
    journal = json.loads(raw.decode("utf-8"))
    if (
        not isinstance(journal, dict)
        or set(journal) != {"schema_version", "phase", "automatic_backup", "targets"}
        or journal.get("schema_version") != 1
        or journal.get("phase") not in {"prepared", "committed"}
        or not isinstance(journal.get("automatic_backup"), dict)
        or set(journal["automatic_backup"]) != {"version_id", "record_digest"}
        or not isinstance(journal.get("targets"), dict)
        or set(journal["targets"]) != set(TARGET_ORDER)
    ):
        raise ValueError("restore journal schema is invalid")
    backup = journal["automatic_backup"]
    if (
        not isinstance(backup.get("version_id"), str)
        or VERSION_ID_RE.fullmatch(backup["version_id"]) is None
        or not isinstance(backup.get("record_digest"), str)
        or re.fullmatch(r"[0-9a-f]{64}", backup["record_digest"]) is None
    ):
        raise ValueError("restore journal backup binding is invalid")
    decoded: dict[str, bytes] = {}
    for relative in TARGET_ORDER:
        item = journal["targets"][relative]
        if not isinstance(item, dict) or set(item) != {"sha256", "data_base64"}:
            raise ValueError("restore target schema is invalid")
        digest = item["sha256"]
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("restore target digest is invalid")
        data = base64.b64decode(item["data_base64"], validate=True)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("restore target digest does not match")
        decoded[relative] = data
    return journal, decoded


def _verify_targets(project_dir: Path, journal: dict[str, Any]) -> None:
    for relative in TARGET_ORDER:
        path = project_dir / relative
        if hashlib.sha256(_read_regular(path, MAX_RECORD_BYTES)).hexdigest() != journal["targets"][relative]["sha256"]:
            raise OSError("restore target verification failed")


def _cleanup_committed_restore_journal(
    journal_path: Path, committed_raw: bytes
) -> None:
    """Durably remove a committed marker or re-establish it once and gate."""
    cleanup_error: Exception
    try:
        journal_path.unlink()
    except Exception as exc:
        if os.path.lexists(journal_path):
            raise ProjectVersionRecoveryError(
                "committed restore journal cleanup failed; marker retained"
            ) from exc
        cleanup_error = exc
    else:
        try:
            _fsync_directory(journal_path.parent)
            return
        except Exception as exc:
            cleanup_error = exc

    # The three authority targets are already exact and committed. Never roll
    # them back. Make one bounded attempt to restore a durable committed marker
    # so restart can verify those targets and retry cleanup.
    try:
        _atomic_replace_exact(journal_path, committed_raw)
    except Exception as rebuild_error:
        raise ProjectVersionRecoveryError(
            "committed restore journal cleanup failed and marker rebuild failed"
        ) from rebuild_error
    raise ProjectVersionRecoveryError(
        "committed restore journal cleanup failed; marker rebuilt"
    ) from cleanup_error


def execute_restore(
    project_dir: Path,
    auto_record: dict[str, Any],
    targets: dict[str, bytes],
) -> None:
    ensure_restore_ready(project_dir)
    for relative in (*TARGET_ORDER, JOURNAL_REL.as_posix()):
        _assert_owned_parents(project_dir.resolve(), project_dir.resolve() / relative, create=True)
    journal = _journal_payload(auto_record, targets)
    journal_path = project_dir / JOURNAL_REL
    journal_raw = json_bytes(journal)
    if len(journal_raw) > MAX_JOURNAL_BYTES:
        raise ValueError("restore journal exceeds the bounded size")
    try:
        _atomic_replace_exact(journal_path, journal_raw)
    except Exception as exc:
        # If publication reached the fixed journal path, every later writer
        # must enter recovery even when the final directory fsync reported an
        # error. A pre-publication failure leaves no journal and is retryable.
        if os.path.lexists(journal_path):
            raise ProjectVersionRecoveryError(
                "project version restore journal publication is uncertain"
            ) from exc
        raise
    try:
        for relative in TARGET_ORDER:
            _atomic_replace_exact(project_dir / relative, targets[relative])
        _verify_targets(project_dir, journal)
        journal["phase"] = "committed"
        committed_raw = json_bytes(journal)
        _atomic_replace_exact(journal_path, committed_raw)
        _cleanup_committed_restore_journal(journal_path, committed_raw)
    except ProjectVersionRecoveryError:
        raise
    except Exception as exc:
        if os.path.lexists(journal_path):
            raise ProjectVersionRecoveryError(
                "project version restore could not complete; journal retained"
            ) from exc
        raise


def recover_restore_transaction(project_dir: Path) -> None:
    journal_path = project_dir / JOURNAL_REL
    if not os.path.lexists(journal_path):
        return
    try:
        for relative in (*TARGET_ORDER, JOURNAL_REL.as_posix()):
            _assert_owned_parents(project_dir.resolve(), project_dir.resolve() / relative, create=False)
        journal, targets = _read_journal(project_dir)
        store = ProjectVersionStore(project_dir)
        backup = store.load(journal["automatic_backup"]["version_id"])
        if backup["record_digest"] != journal["automatic_backup"]["record_digest"]:
            raise ValueError("restore journal backup digest does not match")
        original_phase = journal["phase"]
        if original_phase == "prepared":
            for relative in TARGET_ORDER:
                _atomic_replace_exact(project_dir / relative, targets[relative])
        _verify_targets(project_dir, journal)
        journal["phase"] = "committed"
        committed_raw = json_bytes(journal)
        if original_phase != "committed":
            _atomic_replace_exact(journal_path, committed_raw)
        _cleanup_committed_restore_journal(journal_path, committed_raw)
    except ProjectVersionRecoveryError:
        raise
    except Exception as exc:
        raise ProjectVersionRecoveryError(
            "project version restore recovery failed; recovery remains required"
        ) from exc


def _preview(value: Any) -> str:
    try:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        rendered = str(value)
    if len(rendered) > 160:
        return rendered[:157] + "…"
    return rendered


def diff_values(current: Any, saved: Any, *, limit: int = 200) -> dict[str, Any]:
    changes: list[dict[str, Any]] = []
    total = 0

    def escape(key: str) -> str:
        return key.replace("~", "~0").replace("/", "~1")

    def walk(before: Any, after: Any, path: str) -> None:
        nonlocal total
        if isinstance(before, dict) and isinstance(after, dict):
            for key in sorted(set(before) | set(after)):
                child = f"{path}/{escape(str(key))}"
                if key not in before:
                    add(child, "added", None, after[key])
                elif key not in after:
                    add(child, "removed", before[key], None)
                else:
                    walk(before[key], after[key], child)
            return
        if isinstance(before, list) and isinstance(after, list):
            for index in range(max(len(before), len(after))):
                child = f"{path}/{index}"
                if index >= len(before):
                    add(child, "added", None, after[index])
                elif index >= len(after):
                    add(child, "removed", before[index], None)
                else:
                    walk(before[index], after[index], child)
            return
        if before != after:
            add(path or "/", "changed", before, after)

    def add(path: str, kind: str, before: Any, after: Any) -> None:
        nonlocal total
        total += 1
        if len(changes) < limit:
            changes.append(
                {
                    "path": path,
                    "kind": kind,
                    "saved_preview": _preview(after),
                    "current_preview": _preview(before),
                }
            )

    walk(current, saved, "")
    return {
        "changed": total > 0,
        "changes": changes,
        "truncated": total > limit,
        "total_changes": total,
    }
