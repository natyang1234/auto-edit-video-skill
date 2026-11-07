#!/usr/bin/env python3
"""Shared, fail-closed contract helpers for Studio FX previews.

This module is intentionally independent from ``editor_server`` so both the
HTTP/job layer and the renderer worker can enforce the same project-asset and
request invariants without introducing an import cycle.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any, Callable

import asset_registry


SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
ASSET_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".m4v"})
MATERIAL_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS


class FxPreviewContractError(ValueError):
    """A stable internal contract error safe to map to a public API code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def canonical_sha256(payload: Any) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def normalize_preview_options(value: Any) -> dict[str, str]:
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - {"mode", "quality"}:
        raise FxPreviewContractError(
            "preview_options_invalid", "preview_options 欄位不符合 API 規格"
        )
    mode = value.get("mode", "single")
    quality = value.get("quality", "standard")
    if mode not in {"single", "timeline"} or quality not in {"draft", "standard"}:
        raise FxPreviewContractError(
            "preview_options_invalid", "預覽模式或畫質不受支援"
        )
    return {"mode": str(mode), "quality": str(quality)}


def normalize_fx_opacity(value: Any) -> float:
    """Return the canonical FX-layer opacity, rejecting coercion and NaN/Inf."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FxPreviewContractError(
            "opacity_invalid", "特效透明度必須是 0 到 1 的有限數字"
        )
    try:
        opacity = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise FxPreviewContractError(
            "opacity_invalid", "特效透明度必須是 0 到 1 的有限數字"
        ) from exc
    if not math.isfinite(opacity) or not 0.0 <= opacity <= 1.0:
        raise FxPreviewContractError(
            "opacity_invalid", "特效透明度必須是 0 到 1 的有限數字"
        )
    # JSON preserves the sign of negative zero, but it has no visual meaning;
    # collapse it so equivalent requests share one cache/confirm identity.
    return 0.0 if opacity == 0.0 else opacity


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FxPreviewContractError("window_invalid", f"{label}必須是有限數字")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise FxPreviewContractError("window_invalid", f"{label}必須是有限數字") from exc
    if not math.isfinite(number):
        raise FxPreviewContractError("window_invalid", f"{label}必須是有限數字")
    return round(number, 3)


def normalize_window(value: Any, *, label: str = "時間窗") -> dict[str, float]:
    if not isinstance(value, dict) or set(value) != {"start_s", "duration_s"}:
        raise FxPreviewContractError("window_invalid", f"{label}必須包含 start_s 與 duration_s")
    return {
        "start_s": _finite_number(value.get("start_s"), label),
        "duration_s": _finite_number(value.get("duration_s"), label),
    }


def normalize_sample_window(
    window: Any,
    apply_window: Any,
    source_duration_s: Any,
) -> tuple[dict[str, float], dict[str, float]]:
    sample = normalize_window(window, label="預覽時間窗")
    apply = normalize_window(apply_window if apply_window is not None else window, label="套用時間窗")
    source_duration = _finite_number(source_duration_s, "來源影片長度")
    sample_start, sample_duration = sample["start_s"], sample["duration_s"]
    apply_start, apply_duration = apply["start_s"], apply["duration_s"]
    sample_end = sample_start + sample_duration
    apply_end = apply_start + apply_duration
    epsilon = 1e-6
    if (
        source_duration <= 0
        or sample_start < 0
        or not 0.1 <= sample_duration <= 4.0
        or apply_start < 0
        or apply_duration < 0.1
        or sample_end > source_duration + epsilon
        or apply_end > source_duration + epsilon
    ):
        raise FxPreviewContractError(
            "window_out_of_range",
            "時間窗必須位於來源影片內，預覽樣本長度為 0.1 到 4 秒",
        )
    if sample_start < apply_start - epsilon or sample_end > apply_end + epsilon:
        raise FxPreviewContractError(
            "sample_outside_apply", "預覽樣本必須完整位於實際套用範圍內"
        )
    return sample, apply


def _magic_matches(header: bytes, suffix: str) -> bool:
    if suffix == ".png":
        return header.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in {".jpg", ".jpeg"}:
        return header.startswith(b"\xff\xd8\xff")
    if suffix == ".webp":
        return len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WEBP"
    if suffix == ".gif":
        return header.startswith((b"GIF87a", b"GIF89a"))
    return len(header) >= 8 and header[4:8] in {
        b"ftyp", b"moov", b"wide", b"mdat", b"free", b"skip"
    }


def _default_decode_probe(path: Path) -> bool:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return False
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_type,width,height",
                "-of", "json",
                str(path),
            ],
            text=True,
            capture_output=True,
            timeout=15,
        )
        payload = json.loads(result.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return False
    streams = payload.get("streams") if isinstance(payload, dict) else None
    if result.returncode != 0 or not isinstance(streams, list) or not streams:
        return False
    stream = streams[0] if isinstance(streams[0], dict) else {}
    width, height = stream.get("width"), stream.get("height")
    return (
        stream.get("codec_type") == "video"
        and isinstance(width, int) and not isinstance(width, bool) and width > 0
        and isinstance(height, int) and not isinstance(height, bool) and height > 0
    )


def _open_owned_asset(project_dir: Path, relative: str) -> tuple[Path, bytes, str, int]:
    """Open one assets/ file without following any path component symlink."""
    rel = Path(relative)
    if (
        rel.is_absolute()
        or len(rel.parts) < 2
        or rel.parts[0] != "assets"
        or any(part in {"", ".", ".."} for part in rel.parts)
    ):
        raise FxPreviewContractError("material_unsafe", "素材不屬於目前專案")
    root = Path(project_dir).resolve()
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    file_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        file_flags |= os.O_NOFOLLOW
    descriptors: list[int] = []
    handle = None
    try:
        current_fd = os.open(root, directory_flags)
        descriptors.append(current_fd)
        for part in rel.parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=current_fd)
            descriptors.append(next_fd)
            current_fd = next_fd
        file_fd = os.open(rel.parts[-1], file_flags, dir_fd=current_fd)
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            os.close(file_fd)
            raise FxPreviewContractError("material_unsafe", "素材必須是專案獨占的一般檔案")
        handle = os.fdopen(file_fd, "rb")
        header = handle.read(64)
        digest = hashlib.sha256()
        digest.update(header)
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
        path = root / rel
        # Keep a second identity check after reading to detect replacement
        # during validation.  Render/confirm perform this validation again.
        path_info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(path_info.st_mode) or (
            path_info.st_dev, path_info.st_ino, path_info.st_size
        ) != (info.st_dev, info.st_ino, info.st_size):
            raise FxPreviewContractError("material_changed", "素材在驗證期間已變更")
        return path, header, digest.hexdigest(), int(info.st_size)
    except FxPreviewContractError:
        raise
    except OSError as exc:
        raise FxPreviewContractError("material_unsafe", "素材路徑不安全或已不存在") from exc
    finally:
        if handle is not None:
            handle.close()
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _registry_by_id(project_dir: Path) -> dict[str, dict[str, Any]]:
    try:
        registry = asset_registry.load_registry(project_dir)
    except asset_registry.AssetRegistryError as exc:
        raise FxPreviewContractError("material_registry_invalid", "專案素材登錄檔無效") from exc
    result: dict[str, dict[str, Any]] = {}
    for item in registry.get("items", []):
        if not isinstance(item, dict):
            continue
        asset_id = item.get("asset_id")
        if isinstance(asset_id, str) and ASSET_ID_RE.fullmatch(asset_id):
            result[asset_id] = item
    return result


def list_material_assets(
    project_dir: Path,
    *,
    decode_probe: Callable[[Path], bool] | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    project = Path(project_dir).resolve()
    registry = _registry_by_id(project)
    registry_by_path = {
        str(item.get("path")): item for item in registry.values() if isinstance(item.get("path"), str)
    }
    assets_root = project / "assets"
    if assets_root.is_symlink() or not assets_root.is_dir():
        return []
    probe = decode_probe or _default_decode_probe
    result: list[dict[str, Any]] = []
    for candidate in sorted(assets_root.rglob("*")):
        if len(result) >= limit:
            break
        try:
            relative = candidate.relative_to(project).as_posix()
        except ValueError:
            continue
        suffix = candidate.suffix.lower()
        if suffix not in MATERIAL_EXTENSIONS or candidate.name.startswith("."):
            continue
        try:
            path, header, digest, size = _open_owned_asset(project, relative)
        except FxPreviewContractError:
            continue
        if not _magic_matches(header, suffix) or not probe(path):
            continue
        registry_item = registry_by_path.get(relative)
        asset_id = (
            str(registry_item["asset_id"])
            if registry_item is not None and registry_item.get("sha256") == digest
            else None
        )
        result.append(
            {
                "asset_id": asset_id,
                "path": relative,
                "sha256": digest,
                "bytes": size,
                "kind": "video" if suffix in VIDEO_EXTENSIONS else "image",
                "registry_item": registry_item,
            }
        )
    return result


def resolve_material_binding(
    project_dir: Path,
    material: Any,
    *,
    decode_probe: Callable[[Path], bool] | None = None,
) -> dict[str, str]:
    if not isinstance(material, dict) or set(material) != {"asset_id", "sha256"}:
        raise FxPreviewContractError(
            "material_invalid", "B1 素材只能使用 asset_id 與 sha256"
        )
    asset_id, expected_digest = material.get("asset_id"), material.get("sha256")
    if not isinstance(asset_id, str) or ASSET_ID_RE.fullmatch(asset_id) is None:
        raise FxPreviewContractError("material_invalid", "素材 asset_id 無效")
    if not isinstance(expected_digest, str) or SHA256_RE.fullmatch(expected_digest) is None:
        raise FxPreviewContractError("material_invalid", "素材 sha256 無效")
    registry_item = _registry_by_id(Path(project_dir)).get(asset_id)
    if registry_item is None:
        raise FxPreviewContractError("material_unavailable", "找不到這個專案素材")
    relative = registry_item.get("path")
    if not isinstance(relative, str):
        raise FxPreviewContractError("material_registry_invalid", "專案素材登錄檔無效")
    if registry_item.get("sha256") != expected_digest:
        raise FxPreviewContractError("material_changed", "專案素材內容已變更")
    try:
        path, header, digest, _size = _open_owned_asset(Path(project_dir), relative)
    except FxPreviewContractError as exc:
        if exc.code == "material_unsafe":
            raise FxPreviewContractError(
                "material_unavailable", "找不到這個專案素材"
            ) from exc
        raise
    suffix = path.suffix.lower()
    kind = (
        "video" if suffix in VIDEO_EXTENSIONS
        else "image" if suffix in IMAGE_EXTENSIONS
        else None
    )
    if kind is None or not _magic_matches(header, suffix):
        raise FxPreviewContractError("material_invalid", "素材類型與內容不一致")
    if digest != expected_digest:
        raise FxPreviewContractError("material_changed", "專案素材內容已變更")
    if not (decode_probe or _default_decode_probe)(path):
        raise FxPreviewContractError("material_undecodable", "專案素材無法解碼")
    return {
        "asset_id": asset_id,
        "path": relative,
        "sha256": expected_digest,
        "kind": kind,
    }


def _project_file_digest(project_dir: Path, relative: str) -> str:
    """Hash one project-relative regular file through a no-follow FD chain."""
    rel = Path(relative)
    if (
        rel.is_absolute()
        or not rel.parts
        or rel.as_posix() != relative
        or any(part in {"", ".", ".."} for part in rel.parts)
    ):
        raise FxPreviewContractError("material_registry_invalid", "素材權威路徑無效")
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    file_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        file_flags |= os.O_NOFOLLOW
    current_fd = -1
    descriptor = -1
    try:
        current_fd = os.open(Path(project_dir).resolve(), directory_flags)
        for part in rel.parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        descriptor = os.open(rel.parts[-1], file_flags, dir_fd=current_fd)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise OSError("authority input is not an owned regular file")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev, before.st_ino, before.st_size,
            before.st_mtime_ns, before.st_ctime_ns,
        ) != (
            after.st_dev, after.st_ino, after.st_size,
            after.st_mtime_ns, after.st_ctime_ns,
        ):
            raise OSError("authority input changed while hashing")
        return digest.hexdigest()
    except OSError as exc:
        raise FxPreviewContractError(
            "material_registry_invalid", "素材權威檔案不存在或不安全"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if current_fd >= 0:
            os.close(current_fd)


def material_authority_digests(
    project_dir: Path, state: Any
) -> dict[str, str]:
    """Freeze registry and applicable provider receipts for all B1 bindings."""
    if not isinstance(state, dict):
        raise FxPreviewContractError("material_invalid", "時間軸素材狀態無效")
    bindings = [
        overlay.get("material")
        for overlay in state.get("overlays", [])
        if isinstance(overlay, dict)
        and overlay.get("type") == "fx"
        and overlay.get("code") == "B1"
    ]
    if not bindings:
        return {}
    project = Path(project_dir).resolve()
    registry = _registry_by_id(project)
    registry_relative = asset_registry.PROVENANCE_REL.as_posix()
    result = {registry_relative: _project_file_digest(project, registry_relative)}
    for binding in bindings:
        current = verify_material_binding(project, binding)
        item = registry.get(current["asset_id"])
        if (
            item is None
            or item.get("path") != current["path"]
            or item.get("sha256") != current["sha256"]
        ):
            raise FxPreviewContractError(
                "material_changed", "專案素材登錄或路徑已變更"
            )
        if item.get("origin") == "provider":
            receipt_name = hashlib.sha256(current["asset_id"].encode("utf-8")).hexdigest() + ".json"
            receipt_relative = (asset_registry.PROVIDER_RECEIPTS_REL / receipt_name).as_posix()
            result[receipt_relative] = _project_file_digest(project, receipt_relative)
    return dict(sorted(result.items()))


def verify_material_binding(
    project_dir: Path,
    binding: Any,
    *,
    decode_probe: Callable[[Path], bool] | None = None,
) -> dict[str, str]:
    if not isinstance(binding, dict) or set(binding) != {"asset_id", "path", "sha256", "kind"}:
        raise FxPreviewContractError("material_invalid", "可信素材綁定格式無效")
    asset_id = binding.get("asset_id")
    relative = binding.get("path")
    expected_digest = binding.get("sha256")
    kind = binding.get("kind")
    if (
        not isinstance(asset_id, str)
        or ASSET_ID_RE.fullmatch(asset_id) is None
        or not isinstance(relative, str)
        or not isinstance(expected_digest, str)
        or SHA256_RE.fullmatch(expected_digest) is None
        or kind not in {"image", "video"}
    ):
        raise FxPreviewContractError("material_invalid", "可信素材綁定格式無效")
    path, header, digest, _size = _open_owned_asset(Path(project_dir), relative)
    suffix = path.suffix.lower()
    actual_kind = "video" if suffix in VIDEO_EXTENSIONS else "image" if suffix in IMAGE_EXTENSIONS else None
    if actual_kind != kind or not _magic_matches(header, suffix):
        raise FxPreviewContractError("material_invalid", "素材類型與內容不一致")
    if digest != expected_digest:
        raise FxPreviewContractError("material_changed", "專案素材內容已變更")
    if not (decode_probe or _default_decode_probe)(path):
        raise FxPreviewContractError("material_undecodable", "專案素材無法解碼")
    current = resolve_material_binding(
        project_dir,
        {"asset_id": asset_id, "sha256": expected_digest},
        decode_probe=decode_probe,
    )
    if current != dict(binding):
        raise FxPreviewContractError("material_changed", "專案素材登錄或路徑已變更")
    return current


def visual_overlay_projection(overlay: Any) -> dict[str, Any]:
    """Return only fields that can alter a single-effect preview's pixels."""
    if not isinstance(overlay, dict):
        raise FxPreviewContractError("overlay_invalid", "FX 圖層格式無效")
    keys = (
        "code", "start", "end", "values", "placement", "scheme", "font_ids",
        "style", "visible", "subject", "text_fit", "material",
    )
    return {key: overlay[key] for key in keys if key in overlay}


def renderer_request_digest(payload: Any) -> str:
    """Bind the exact frozen inputs the worker is authorized to render."""
    if not isinstance(payload, dict):
        raise FxPreviewContractError("invalid_request", "renderer request 必須是物件")
    keys = (
        "project_dir", "source", "cache_dir", "output", "overlay",
        "sample_window", "preview_options", "state_snapshot",
        "manifest_snapshot", "timeline_snapshot", "dependency_snapshot",
        "pack_id", "canvas",
    )
    try:
        return canonical_sha256({key: payload.get(key) for key in keys})
    except (TypeError, ValueError) as exc:
        raise FxPreviewContractError("invalid_request", "renderer request 無法凍結") from exc


def validate_renderer_request(
    project_dir: Path,
    payload: Any,
    *,
    decode_probe: Callable[[Path], bool] | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise FxPreviewContractError("invalid_request", "renderer request 必須是物件")
    overlay = payload.get("overlay")
    if not isinstance(overlay, dict):
        raise FxPreviewContractError("invalid_request", "renderer overlay 無效")
    style = overlay.get("style", {})
    if not isinstance(style, dict):
        raise FxPreviewContractError("invalid_request", "renderer FX 樣式無效")
    try:
        opacity = normalize_fx_opacity(style.get("opacity", 1.0))
    except FxPreviewContractError as exc:
        raise FxPreviewContractError("invalid_request", exc.message) from exc
    overlay = dict(overlay)
    overlay["style"] = {**style, "opacity": opacity}
    state_snapshot = payload.get("state_snapshot")
    manifest_snapshot = payload.get("manifest_snapshot")
    if not isinstance(state_snapshot, dict) or not isinstance(manifest_snapshot, dict):
        raise FxPreviewContractError("invalid_request", "renderer snapshot 無效")
    start = overlay.get("start")
    end = overlay.get("end")
    if (
        isinstance(start, bool)
        or not isinstance(start, (int, float))
        or isinstance(end, bool)
        or not isinstance(end, (int, float))
    ):
        raise FxPreviewContractError("invalid_request", "renderer apply window 無效")
    try:
        apply_duration = float(end) - float(start)
    except (OverflowError, TypeError, ValueError) as exc:
        raise FxPreviewContractError("invalid_request", "renderer apply window 無效") from exc
    options = normalize_preview_options(payload.get("preview_options"))
    sample, apply = normalize_sample_window(
        payload.get("sample_window"),
        {
            "start_s": start,
            "duration_s": apply_duration,
        },
        (manifest_snapshot.get("source") or {}).get("duration_s")
        if isinstance(manifest_snapshot.get("source"), dict)
        else None,
    )
    if options["mode"] == "timeline":
        timeline_snapshot = payload.get("timeline_snapshot")
        if (
            not isinstance(timeline_snapshot, dict)
            or timeline_snapshot.get("schema_version") != 1
            or timeline_snapshot.get("quality") != "preview"
            or timeline_snapshot.get("state") != state_snapshot
            or timeline_snapshot.get("manifest") != manifest_snapshot
            or timeline_snapshot.get("project_id") != manifest_snapshot.get("project_id")
        ):
            raise FxPreviewContractError(
                "invalid_request", "timeline renderer snapshot 無效"
            )
    if overlay.get("code") == "B1":
        overlay = dict(overlay)
        overlay["material"] = verify_material_binding(
            project_dir, overlay.get("material"), decode_probe=decode_probe
        )
    return {
        "overlay": overlay,
        "sample_window": sample,
        "apply_window": apply,
        "preview_options": options,
        "state_snapshot": state_snapshot,
        "manifest_snapshot": manifest_snapshot,
    }
