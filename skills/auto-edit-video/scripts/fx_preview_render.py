#!/usr/bin/env python3
"""Render one strict, source-faithful Studio FX preview to a browser MP4.

This is deliberately a small worker boundary: it reads one manager-created
request, writes one atomic result, and never mutates editor state or approvals.
The API owns process groups, authorization and path containment.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import fx_overlay
import fx_preview_contract
import hashlib
import text_fit


MAX_SECONDS = 4.0
MAX_REQUEST_JSON_BYTES = 16 * 1024 * 1024
RENDERER_MAINTENANCE_LOCK = ".renderer-maintenance.lock"


class PreviewError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


def progress(phase: str, message: str) -> None:
    print(json.dumps({"phase": phase, "message": message}, ensure_ascii=False), file=sys.stderr, flush=True)


def _directory_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _open_directory_no_follow(path: Path) -> int:
    """Canonicalize once, then pin the directory through a no-follow walk."""
    canonical = path.resolve(strict=True)
    if not canonical.is_absolute():
        raise OSError("directory path must be absolute")
    current_fd = os.open("/", _directory_flags())
    try:
        for part in canonical.parts[1:]:
            next_fd = os.open(part, _directory_flags(), dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _open_parent(path: Path) -> tuple[int, str]:
    absolute = path.absolute()
    if not absolute.name or Path(absolute.name).name != absolute.name:
        raise OSError("unsafe result entry name")
    return _open_directory_no_follow(absolute.parent), absolute.name


def _unlink_lease_if_same(directory_fd: int, name: str, descriptor: int) -> bool:
    """Unlink only the still-named regular inode held and locked by this worker."""
    try:
        opened = os.fstat(descriptor)
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            return False
        os.unlink(name, dir_fd=directory_fd)
        return True
    except OSError:
        return False


def _acquire_renderer_maintenance_shared(cache: Path) -> tuple[int, int]:
    """Hold the fixed shared guard before any renderer-cache read or write."""
    cache_fd = _open_directory_no_follow(cache)
    descriptor = -1
    try:
        inherited = os.environ.get("FX_PREVIEW_MAINTENANCE_FD")
        if inherited:
            descriptor = os.dup(int(inherited))
        else:
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(
                RENDERER_MAINTENANCE_LOCK,
                flags,
                0o600,
                dir_fd=cache_fd,
            )
        opened = os.fstat(descriptor)
        named = os.stat(
            RENDERER_MAINTENANCE_LOCK,
            dir_fd=cache_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise OSError("unsafe renderer maintenance lock")
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        return cache_fd, descriptor
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(cache_fd)
        raise


def _open_regular_at(directory_fd: int, name: str) -> int:
    if Path(name).name != name:
        raise OSError("unsafe request entry name")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(descriptor)
        raise OSError("request is not an owned regular file")
    return descriptor


def _read_json_fd(descriptor: int) -> tuple[dict[str, Any], str]:
    before = os.fstat(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size < 0
        or before.st_size > MAX_REQUEST_JSON_BYTES
    ):
        raise ValueError("renderer request file is unsafe")
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    total = 0
    digest = hashlib.sha256()
    while True:
        chunk = os.read(descriptor, min(65536, MAX_REQUEST_JSON_BYTES + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        digest.update(chunk)
        if total > MAX_REQUEST_JSON_BYTES:
            raise ValueError("renderer request is too large")
    after = os.fstat(descriptor)
    if (
        before.st_dev, before.st_ino, before.st_size,
        before.st_mtime_ns, before.st_ctime_ns,
    ) != (
        after.st_dev, after.st_ino, after.st_size,
        after.st_mtime_ns, after.st_ctime_ns,
    ):
        raise ValueError("renderer request changed while reading")
    payload = json.loads(b"".join(chunks).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("renderer request must be an object")
    return payload, digest.hexdigest()


def atomic_json_at(directory_fd: int, name: str, data: dict[str, Any]) -> None:
    if Path(name).name != name:
        raise OSError("unsafe result entry name")
    temporary = f".{name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
        payload = (
            json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            + "\n"
        ).encode("utf-8")
        with os.fdopen(descriptor, "wb") as out:
            descriptor = -1
            out.write(payload)
            out.flush()
            os.fsync(out.fileno())
        os.replace(
            temporary,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    directory_fd, name = _open_parent(path)
    try:
        atomic_json_at(directory_fd, name, data)
    finally:
        os.close(directory_fd)


def media_ok(path: Path, expected: float) -> bool:
    """Validate frames, not just a possibly stale container duration header."""
    try:
        r = subprocess.run([
            "ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,pix_fmt,avg_frame_rate,nb_read_frames:format=duration",
            "-of", "json", str(path),
        ], capture_output=True, text=True, timeout=60, check=False)
        data = json.loads(r.stdout or "{}")
        stream = (data.get("streams") or [{}])[0]
        n, _, d = str(stream.get("avg_frame_rate") or "0/1").partition("/")
        fps = float(n) / max(float(d or 1), 1)
        frames = int(stream.get("nb_read_frames") or 0)
        duration = float((data.get("format") or {}).get("duration") or 0)
        return (stream.get("codec_name") == "h264" and stream.get("pix_fmt") == "yuv420p"
                and frames >= max(1, int(expected * fps * .85))
                and abs(duration - expected) <= max(.25, expected * .1))
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return False


def request_paths(data: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    try:
        project = Path(data["project_dir"]).resolve()
        source = Path(data["source"]).resolve()
        cache = Path(data["cache_dir"]).resolve()
        output = Path(data["output"]).resolve()
    except (KeyError, TypeError, OSError) as exc:
        raise PreviewError("invalid_request", "預覽請求缺少有效的專案、原片、快取或輸出路徑") from exc
    if not project.is_dir() or not source.is_file():
        raise PreviewError("invalid_source", "找不到可讀取的原片或專案目錄")
    for key in ("source", "cache_dir", "output"):
        entry = Path(data[key]).absolute()
        for part in [entry, *entry.parents]:
            if part.resolve() == project: break
            if part.is_symlink():
                raise PreviewError("invalid_request", "預覽路徑不可含符號連結")
    preview_root = (project / "working" / "fx-preview").resolve()
    try:
        preview_root.relative_to(project)
    except ValueError as exc:
        raise PreviewError("invalid_request", "預覽目錄必須在專案內") from exc
    try:
        source.relative_to(project)
        cache.relative_to(preview_root)
        output.relative_to(preview_root)
    except ValueError as exc:
        raise PreviewError("invalid_request", "預覽來源或輸出不在專案允許的目錄內") from exc
    return project, source, cache, output


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_relative_parts(relative: str) -> tuple[str, ...]:
    if not isinstance(relative, str):
        raise PreviewError("invalid_request", "預覽依賴路徑格式無效")
    rel = Path(relative)
    if (
        rel.is_absolute()
        or not rel.parts
        or any(part in {"", ".", ".."} for part in rel.parts)
        or rel.as_posix() != relative
    ):
        raise PreviewError("unsafe_project_path", "依賴路徑不在專案內")
    return tuple(rel.parts)


def _open_project_file(project: Path, relative: str) -> tuple[int, os.stat_result]:
    """Open a project file without following any ancestor or leaf symlink."""
    parts = _safe_relative_parts(relative)
    dir_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        dir_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        dir_flags |= os.O_NOFOLLOW
    file_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        file_flags |= os.O_NOFOLLOW
    current_fd = os.open(project, dir_flags)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, dir_flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        descriptor = os.open(parts[-1], file_flags, dir_fd=current_fd)
    except OSError as exc:
        raise PreviewError("dependency_changed", "預覽輸入與權威快照不一致") from exc
    finally:
        os.close(current_fd)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        os.close(descriptor)
        raise PreviewError("unsafe_project_path", "預覽輸入不是一般檔案")
    return descriptor, info


def _digest_open_file(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _project_file_digest(project: Path, relative: str) -> str:
    descriptor, before = _open_project_file(project, relative)
    try:
        digest = _digest_open_file(descriptor)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        before.st_dev, before.st_ino, before.st_size,
        before.st_mtime_ns, before.st_ctime_ns,
    ) != (
        after.st_dev, after.st_ino, after.st_size,
        after.st_mtime_ns, after.st_ctime_ns,
    ):
        raise PreviewError("dependency_changed", "預覽輸入在驗證期間已變更")
    return digest


def verified_project_copy(project: Path, relative: str, dst: Path, expected: str) -> str:
    """Copy one snapshotted project input from an already-opened no-follow FD."""
    if not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        raise PreviewError("invalid_request", "預覽依賴摘要格式無效")
    descriptor, before = _open_project_file(project, relative)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink():
        os.close(descriptor)
        raise PreviewError("unsafe_project_path", "預覽影子輸出路徑不安全")
    digest = hashlib.sha256()
    try:
        with dst.open("wb") as output_stream:
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                output_stream.write(chunk)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    copied = digest.hexdigest()
    if (
        copied != expected
        or file_digest(dst) != expected
        or (
            before.st_dev, before.st_ino, before.st_size,
            before.st_mtime_ns, before.st_ctime_ns,
        ) != (
            after.st_dev, after.st_ino, after.st_size,
            after.st_mtime_ns, after.st_ctime_ns,
        )
    ):
        raise PreviewError("dependency_changed", "預覽輸入在複製期間已變更")
    return str(dst)


def verified_copy(src, dst):
    src, dst = Path(src), Path(dst)
    if src.is_symlink() or not src.is_file():
        raise PreviewError("unsafe_project_path", "預覽輸入不是一般檔案")
    before = file_digest(src)
    shutil.copy2(src, dst)
    if file_digest(src) != before or file_digest(dst) != before:
        raise PreviewError("dependency_changed", "預覽輸入在複製期間已變更")
    return str(dst)


def verify_inputs(project: Path, deps: dict[str, Any]) -> None:
    if "timeline_project_inputs" in deps:
        current = {}
        for name in ("structured_layers.json", "visual_plan_v2.json", "caption_v2.json", "caption_delivery.json", "graphic_packages"):
            root = project / "working" / name
            paths = [root] if root.is_file() or root.is_symlink() else list(root.rglob("*")) if root.is_dir() else []
            for path in paths:
                if path.is_symlink():
                    raise PreviewError("unsafe_project_path", "時間軸輸入含符號連結")
                if path.is_file():
                    relative = path.relative_to(project).as_posix()
                    current[relative] = _project_file_digest(project, relative)
        if current != deps["timeline_project_inputs"]:
            raise PreviewError("dependency_changed", "時間軸輸入集合與快照不一致")
    source = deps.get("source") or {}
    expected = dict(deps.get("timeline_project_inputs") or {})
    if not source.get("relative") or not source.get("sha256"):
        raise PreviewError("invalid_request", "缺少原片依賴快照")
    expected[source["relative"]] = source["sha256"]
    expected.update(deps.get("asset_digests") or {})
    expected.update(deps.get("provenance_digests") or {})
    for relative, digest in expected.items():
        if _project_file_digest(project, relative) != digest:
            raise PreviewError("dependency_changed", "預覽輸入與權威快照不一致")


def dependency_files(deps: dict[str, Any]) -> dict[str, str]:
    source = deps.get("source") if isinstance(deps.get("source"), dict) else {}
    relative, digest = source.get("relative"), source.get("sha256")
    if not relative or not digest:
        raise PreviewError("invalid_request", "缺少原片依賴快照")
    expected: dict[str, str] = {}
    for collection in (
        {relative: digest},
        deps.get("asset_digests") or {},
        deps.get("provenance_digests") or {},
        deps.get("timeline_project_inputs") or {},
    ):
        if not isinstance(collection, dict):
            raise PreviewError("invalid_request", "預覽依賴快照格式無效")
        for path, sha256 in collection.items():
            _safe_relative_parts(path)
            if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
                raise PreviewError("invalid_request", "預覽依賴摘要格式無效")
            if path in expected and expected[path] != sha256:
                raise PreviewError("invalid_request", "預覽依賴快照互相矛盾")
            expected[path] = sha256
    return expected


def _broll_workspace_media(project: Path, rendered: dict[str, Any]) -> Path:
    """Resolve the bound B1 media actually referenced by the rendered block."""
    source = rendered.get("source")
    material = rendered.get("material")
    if not isinstance(source, str) or not isinstance(material, dict):
        raise PreviewError("broll_render_unverified", "B1 預覽缺少素材渲染證據")
    relative_source = Path(source)
    if relative_source.is_absolute() or any(part in {"", ".", ".."} for part in relative_source.parts):
        raise PreviewError("broll_render_unverified", "B1 預覽渲染證據路徑無效")
    lab = (project / relative_source).parent
    composition = lab / "compositions" / "fx-broll.html"
    try:
        html = composition.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PreviewError("broll_render_unverified", "B1 預覽缺少元件渲染證據") from exc
    element_id = "br-i" if material.get("kind") == "image" else "br-v"
    tag = re.search(
        rf"<(?:img|video)\b[^>]*\bid=[\"']{re.escape(element_id)}[\"'][^>]*>",
        html,
        flags=re.IGNORECASE,
    )
    src = re.search(r"\bsrc=[\"']assets/([^\"']+)[\"']", tag.group(0), flags=re.IGNORECASE) if tag else None
    if src is None or Path(src.group(1)).name != src.group(1):
        raise PreviewError("broll_render_unverified", "B1 預覽未綁定已選素材")
    media = lab / "assets" / src.group(1)
    try:
        info = media.lstat()
    except OSError as exc:
        raise PreviewError("broll_render_unverified", "B1 預覽未產生已選素材畫面") from exc
    if media.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size <= 0:
        raise PreviewError("broll_render_unverified", "B1 預覽素材畫面不安全或不完整")
    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=codec_type,width,height", "-of", "json", str(media),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        payload = json.loads(probe.stdout or "{}")
        stream = (payload.get("streams") or [{}])[0]
        if (
            probe.returncode != 0
            or stream.get("codec_type") != "video"
            or int(stream.get("width") or 0) <= 0
            or int(stream.get("height") or 0) <= 0
        ):
            raise ValueError("B1 material derivative is not decodable")
    except (OSError, TypeError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        raise PreviewError("broll_render_unverified", "B1 預覽素材畫面無法解碼") from exc
    return media


def timeline_preview(project: Path, cache: Path, output: Path, snapshot: dict[str, Any], sample_start: float, duration: float, dependencies: dict[str, Any], quality: str = "standard", scratch_owner: str | None = None) -> dict[str, Any]:
    """Render frozen editor state, then take the post-cut windows for a source sample."""
    import hashlib
    import render_editor_timeline as timeline
    manifest = snapshot.get("manifest") if isinstance(snapshot.get("manifest"), dict) else {}
    state = snapshot.get("state") if isinstance(snapshot.get("state"), dict) else {}
    segments = timeline.state_segments(state, float(manifest.get("source", {}).get("duration_s") or 0))
    raw_windows = timeline.map_source_range_to_post_cut(segments, sample_start, sample_start + duration)
    windows = timeline.merge_contiguous_windows(raw_windows)
    if not windows:
        raise PreviewError("empty_timeline_window", "預覽時窗完全落在已剪除片段")
    # Source gaps removed by the edit are adjacent on the final axis, so a
    # source 3..7 window spanning a removed 5..6 becomes one 3-second output.
    start, end = windows[0][0], windows[-1][1]
    key = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
    # render_project creates captions/graphic intermediates.  Never let a preview
    # write them into the live project: clone immutable inputs into the preview
    # cache. Copy bytes, not hard links: cache cleanup/utime must not alter live mtimes.
    verify_inputs(project, dependencies)
    if scratch_owner is None:
        scratch_owner = secrets.token_hex(16)
    if re.fullmatch(r"[0-9a-f]{32}", scratch_owner) is None:
        raise PreviewError("invalid_request", "預覽暫存 ownership 格式無效")
    cache_fd = _open_directory_no_follow(cache)
    lease_name = f".timeline-owner-{scratch_owner}.lock"
    lease_fd = -1
    owns_lease_path = False
    lease_locked = False
    shadow: Path | None = None
    try:
        inherited_lease = os.environ.get("FX_PREVIEW_SCRATCH_LEASE_FD")
        if inherited_lease:
            lease_fd = os.dup(int(inherited_lease))
        else:
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            lease_fd = os.open(lease_name, flags, 0o600, dir_fd=cache_fd)
            owns_lease_path = True
        lease_info = os.fstat(lease_fd)
        named_lease = os.stat(lease_name, dir_fd=cache_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(lease_info.st_mode)
            or lease_info.st_nlink != 1
            or (lease_info.st_dev, lease_info.st_ino) != (named_lease.st_dev, named_lease.st_ino)
        ):
            raise OSError("unsafe renderer scratch lease")
        fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lease_locked = True
        shadow = Path(tempfile.mkdtemp(prefix=f"timeline-project-{scratch_owner}-{key}-", dir=cache))
    except Exception:
        if owns_lease_path and lease_locked and lease_fd >= 0:
            _unlink_lease_if_same(cache_fd, lease_name, lease_fd)
        if lease_fd >= 0:
            os.close(lease_fd)
        os.close(cache_fd)
        raise
    assert shadow is not None
    path = shadow / "working" / "render_snapshots" / f"fx-preview-{key}.json"
    # Keep every unpublished timeline artifact under the renderer-owned shadow.
    # The manager removes this exact directory prefix through its pinned cache
    # descriptor after cancellation/timeout, even when SIGKILL prevents this
    # worker's finally block from running.  Only the validated MP4 crosses into
    # runtime, under the manager-owned job filename.
    full = shadow / "working" / "fx-preview-output.mp4"
    layout_reports: dict[str, Any] = {}
    target_material = dependencies.get("material")
    expected_broll = None
    if isinstance(target_material, dict):
        for candidate in state.get("overlays", []):
            if (
                isinstance(candidate, dict)
                and candidate.get("type") == "fx"
                and candidate.get("code") == "B1"
                and candidate.get("material") == target_material
                and float(candidate.get("start", -1)) <= sample_start + 1e-6
                and float(candidate.get("end", -1)) >= sample_start + duration - 1e-6
            ):
                expected_broll = candidate
                break
    observed_broll = False
    try:
        # Copy exactly the files frozen by the manager.  Historical renders,
        # unreferenced assets and unrelated symlinks never enter the shadow.
        for relative, expected_digest in dependency_files(dependencies).items():
            verified_project_copy(
                project,
                relative,
                shadow / Path(relative),
                expected_digest,
            )
        atomic_json(shadow / "project.json", manifest)
        verify_inputs(project, dependencies)
        verify_inputs(shadow, dependencies)
        atomic_json(path, snapshot)
        original_fx_render = fx_overlay.render_for_timeline

        def audited_fx_render(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
            nonlocal observed_broll
            overlay = args[1] if len(args) > 1 and isinstance(args[1], dict) else {}
            kwargs["progress"] = lambda phase: progress(phase, "正在準備效果與圖層")
            rendered = original_fx_render(*args, **kwargs)
            if expected_broll is not None and overlay.get("id") == expected_broll.get("id"):
                if rendered is None:
                    raise PreviewError("broll_render_failed", "B1 預覽素材渲染失敗")
                actual_material = rendered.get("material")
                for key in ("asset_id", "sha256", "kind"):
                    if not isinstance(actual_material, dict) or actual_material.get(key) != target_material.get(key):
                        raise PreviewError("broll_render_unverified", "B1 預覽未使用已凍結素材")
                _broll_workspace_media(Path(args[0]), rendered)
                observed_broll = True
            return rendered

        fx_overlay.render_for_timeline = audited_fx_render
        try:
            timeline.render_project(
                shadow,
                full,
                "preview",
                snapshot_path=path,
                preview_fx_quality=quality,
                preview_output_window=(start, end - start),
                preview_progress=lambda phase: progress(phase, "正在處理時間軸預覽"),
            )
        finally:
            fx_overlay.render_for_timeline = original_fx_render
        if expected_broll is not None and not observed_broll:
            raise PreviewError("broll_render_unverified", "B1 預覽未包含已選素材")
        layout_path = shadow / "working" / "fx_text_layout.json"
        layout_reports = json.loads(layout_path.read_text()) if layout_path.is_file() else {}
        # A template can move the source person after the FX DOM was measured.
        # Do not recycle full-source face coordinates as final composite evidence.
        for report in layout_reports.values():
            if not isinstance(report, dict): continue
            report["analysis_scope"] = "individual-fx-layout"
            report["face_measurement"] = "unavailable"
            report["subject_samples"] = []
            report["findings"] = [f for f in report.get("findings", []) if f.get("kind") not in {"subject_overlap", "subject_measurement_unavailable"}]
            report["findings"].append({"kind": "composite_subject_measurement_unavailable"})
            report["status"] = "warn"
        atomic_json(cache / f"timeline-{key}-text-layout.json", layout_reports)
        progress("validating", "正在驗證時間軸預覽")
        if not media_ok(full, end - start):
            raise PreviewError("output_invalid", "時間軸預覽 MP4 未完整解碼")
        os.replace(full, output)
    except PreviewError:
        raise
    except Exception as exc:
        raise PreviewError("timeline_render_failed", "完整時間軸預覽無法渲染") from exc
    finally:
        path.unlink(missing_ok=True)
        shutil.rmtree(shadow, ignore_errors=True)
        if owns_lease_path and lease_locked:
            _unlink_lease_if_same(cache_fd, lease_name, lease_fd)
        os.close(lease_fd)
        os.close(cache_fd)
    time_map, out_at = [], 0.0
    for seg_start, seg_end in segments:
        left, right = max(sample_start, seg_start), min(sample_start + duration, seg_end)
        if right > left:
            length = right - left
            time_map.append({"source_start_s": left, "source_end_s": right, "output_start_s": round(out_at, 3), "output_end_s": round(out_at + length, 3)})
            out_at += length
    return {"render_duration_s": round(end - start, 3), "time_map": time_map,
            "text_layout": {"version": 2, "measurement": "composition-dom" if layout_reports else "unavailable",
                            "status": "warn" if not layout_reports or any((r or {}).get("status") != "pass" for r in layout_reports.values()) else "pass",
                            "analysis_scope": "individual-fx-layout", "face_measurement": "unavailable", "layers": layout_reports, "findings": [dict(f, layer_id=k) for k,r in layout_reports.items() for f in (r or {}).get("findings", [])]}}


def render(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise PreviewError("invalid_request", "預覽請求必須是 JSON 物件")
    _project, _source, cache, _output = request_paths(data)
    cache.mkdir(parents=True, exist_ok=True)
    cache_fd, maintenance_fd = _acquire_renderer_maintenance_shared(cache)
    try:
        return _render(data)
    finally:
        os.close(maintenance_fd)
        os.close(cache_fd)


def _render(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise PreviewError("invalid_request", "預覽請求必須是 JSON 物件")
    project, source, cache, output = request_paths(data)
    try:
        checked = fx_preview_contract.validate_renderer_request(project, data)
    except fx_preview_contract.FxPreviewContractError as exc:
        raise PreviewError(exc.code, exc.message) from exc
    data = dict(data, **checked)
    dependencies = data.get("dependency_snapshot") or {}
    verify_inputs(project, dependencies)
    if source != (project / dependencies["source"]["relative"]).resolve():
        raise PreviewError("invalid_source", "原片不符合權威快照")
    overlay = data.get("overlay")
    canvas = data.get("canvas") or {}
    if not isinstance(overlay, dict) or overlay.get("type") != "fx":
        raise PreviewError("invalid_request", "預覽只接受單一 fx 圖層")
    try:
        width, height = int(canvas["width"]), int(canvas["height"])
        apply_start, apply_end = float(overlay["start"]), float(overlay["end"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PreviewError("invalid_request", "預覽缺少有效畫布或效果時間") from exc
    if not fx_overlay.canvas_ok((width, height)):
        raise PreviewError("invalid_canvas", "特效預覽只支援 9:16 畫布")
    fit = str(canvas.get("fit") or "cover")
    if fit not in {"cover", "contain"}:
        raise PreviewError("invalid_canvas", "特效預覽畫面適配只支援 cover 或 contain")
    apply_duration = round(apply_end - apply_start, 3)
    if apply_duration <= .05:
        raise PreviewError("invalid_window", "特效套用時間必須大於 0")
    sample = data.get("sample_window") or {}
    try:
        sample_start = float(sample.get("start_s", apply_start))
        duration = round(float(sample.get("duration_s", min(MAX_SECONDS, apply_duration))), 3)
    except (TypeError, ValueError) as exc:
        raise PreviewError("invalid_window", "預覽取樣時間無效") from exc
    if duration < .1 or duration > MAX_SECONDS + 1e-6 or sample_start < apply_start - .001 or sample_start + duration > apply_end + .001:
        raise PreviewError("invalid_window", "預覽取樣必須在效果套用範圍內且不超過 4 秒")
    options = data.get("preview_options") or {}
    mode, requested_quality = str(options.get("mode") or "single"), str(options.get("quality") or "standard")
    if mode not in {"single", "timeline"} or requested_quality not in {"draft", "standard"}:
        raise PreviewError("invalid_request", "預覽模式或品質無效")
    code = str(overlay.get("code") or "")
    material = overlay.get("material")
    if code == "B1" and not isinstance(material, dict):
        raise PreviewError("broll_requires_asset", "B1 需要已驗證的專案補畫面素材")
    if code not in fx_overlay.codes():
        raise PreviewError("invalid_fx", "找不到指定的特效")
    try:
        info = fx_overlay._compose().probe(str(source))
    except SystemExit as exc:
        raise PreviewError("invalid_source", str(exc)) from exc
    if apply_start < 0 or apply_end > float(info["duration"]) + .001:
        raise PreviewError("invalid_window", "特效時間超出原片範圍")

    cache.mkdir(parents=True, exist_ok=True)
    if mode == "timeline":
        snapshot = data.get("timeline_snapshot")
        if not isinstance(snapshot, dict):
            raise PreviewError("invalid_request", "完整時間軸預覽需要凍結快照")
        progress("rendering", "正在以凍結快照渲染完整時間軸")
        output.parent.mkdir(parents=True, exist_ok=True)
        rep = timeline_preview(
            project,
            cache,
            output,
            snapshot,
            sample_start,
            duration,
            dependencies,
            requested_quality,
            scratch_owner=data.get("scratch_owner"),
        )
        return {"ok": True, "out": str(output), "code": code, "mode": mode, "quality": requested_quality, "apply_window": {"start_s": apply_start, "duration_s": apply_duration}, "sample_window": {"start_s": sample_start, "duration_s": duration}, **rep}
    progress("preparing", "正在準備原片時窗")
    effect = next((x for x in fx_overlay.catalog().get("effects", []) if x["code"] == code), {})
    progress("composing", f"正在以原片時窗渲染 {code}")
    # Render the whole apply window.  The MP4 below seeks its *absolute* phase;
    # rendering a 2 s replacement for an 8 s layer changes GSAP timing.
    try:
        rendered = fx_overlay.render_for_timeline(cache, overlay, source, str(data.get("pack_id") or ""),
                                                   requested_quality, canvas=(width, height), fit=fit,
                                                   material=material, asset_project=project,
                                                   progress=lambda phase: progress(phase, "正在準備效果"))
    except fx_overlay.SubjectTrackingError as exc:
        raise PreviewError("subject_tracking_lost", str(exc)) from exc
    if rendered is None:
        reason = "person_unavailable" if effect.get("needs_person") or code == "P5" else "render_failed"
        raise PreviewError(reason, "此時窗無法取得可用人物去背" if reason == "person_unavailable" else "特效渲染失敗")

    alpha = cache / rendered["source"]
    if not alpha.is_file():
        raise PreviewError("render_failed", "特效渲染檔未產生")
    progress("encoding", "正在合成可播放預覽")
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(f".{output.stem}.{os.getpid()}.partial.mp4")
    vf = ("scale=1080:1920:force_original_aspect_ratio=decrease,"
          "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=0x171512,setsar=1" if fit == "contain"
          else "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1")
    cmd = [
        "ffmpeg", "-v", "error", "-y", "-ss", f"{sample_start:.3f}", "-i", str(source), "-ss", f"{sample_start - apply_start:.3f}", "-i", str(alpha),
        "-t", f"{duration:.3f}", "-filter_complex", f"[0:v]{vf}[base];[base][1:v]overlay=0:0:shortest=1[v]",
        "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", "-shortest", str(tmp),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=20 * 60, check=False)
    progress("validating", "正在驗證預覽")
    if r.returncode or not media_ok(tmp, duration):
        tmp.unlink(missing_ok=True)
        raise PreviewError("output_invalid", "預覽 MP4 未完整解碼或格式不相容")
    os.replace(tmp, output)
    # The alpha MOV and HyperFrames lab are per-effect intermediates (often
    # hundreds of MB). The reusable person/mask pair lives next to this key
    # directory, so remove only the workspace after publishing the MP4.
    try:
        shutil.rmtree(alpha.parent)
    except OSError:
        pass
    text_layout = rendered.get("text_layout") or text_fit.layout_report([])
    verify_inputs(project, dependencies)
    return {"ok": True, "out": str(output), "duration_s": duration, "code": code,
            "apply_window": {"start_s": apply_start, "duration_s": apply_duration},
            "sample_window": {"start_s": sample_start, "duration_s": duration}, "mode": mode,
            "quality": requested_quality, "render_duration_s": duration,
            "time_map": [{"source_start_s": sample_start, "source_end_s": sample_start + duration,
                          "output_start_s": 0.0, "output_end_s": duration}], "text_layout": text_layout,
            "subject_track": rendered.get("spotlight"), "cached": bool(rendered.get("cached")), "effect_seconds": rendered.get("seconds")}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--request", required=True)
    p.add_argument("--result", required=True)
    args = p.parse_args()
    request_env = os.environ.get("FX_PREVIEW_REQUEST_FD")
    result_dir_env = os.environ.get("FX_PREVIEW_RESULT_DIR_FD")
    result_dir_fd = -1
    request_fd = -1
    try:
        if bool(request_env) != bool(result_dir_env):
            raise ValueError("renderer IPC descriptors are incomplete")
        if result_dir_env:
            result_dir_fd = os.dup(int(result_dir_env))
            result_info = os.fstat(result_dir_fd)
            if not stat.S_ISDIR(result_info.st_mode):
                raise ValueError("renderer result descriptor is not a directory")
            result_name = Path(args.result).name
            request_fd = os.dup(int(request_env))
        else:
            result_dir_fd, result_name = _open_parent(Path(args.result))
            request_dir_fd, request_name = _open_parent(Path(args.request))
            try:
                request_fd = _open_regular_at(request_dir_fd, request_name)
            finally:
                os.close(request_dir_fd)
        data, request_sha256 = _read_json_fd(request_fd)
        render_request_sha256 = fx_preview_contract.renderer_request_digest(data)
        rep = render(data)
        rep["request_sha256"] = request_sha256
        rep["render_request_sha256"] = render_request_sha256
        atomic_json_at(result_dir_fd, result_name, rep)
        return 0
    except PreviewError as exc:
        if result_dir_fd >= 0:
            atomic_json_at(result_dir_fd, result_name, {"ok": False, "error": exc.message, "code": exc.code})
        return 1
    except (OSError, ValueError, subprocess.SubprocessError, SystemExit) as exc:
        if result_dir_fd >= 0:
            atomic_json_at(result_dir_fd, result_name, {"ok": False, "error": "特效預覽發生未預期錯誤", "code": "render_failed"})
        progress("failed", str(exc)[-300:])
        return 1
    finally:
        if request_fd >= 0:
            os.close(request_fd)
        if result_dir_fd >= 0:
            os.close(result_dir_fd)


if __name__ == "__main__":
    raise SystemExit(main())
