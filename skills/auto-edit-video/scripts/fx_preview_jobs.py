#!/usr/bin/env python3
"""Project-scoped, fail-closed job manager for short FX preview renders.

The HTTP layer owns authentication and persistence.  This module accepts no
client paths: it derives the source, renderer, cache and output paths from the
server-owned project directory, freezes a validated candidate, and publishes
only a content-addressed MP4 after the worker and source dependencies pass
post-render checks.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import signal
import shutil
import stat
import subprocess
import sys
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

import fx_overlay


JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
OVERLAY_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
TERMINAL_STATES = {"ready", "failed", "cancelled"}
RUNNING_STATES = {"running"}
MAX_REQUEST_VALUES = 32
MAX_VALUE_CHARS = 500
MAX_TOTAL_VALUE_CHARS = 8192
MAX_PREVIEW_BYTES = 256 * 1024 * 1024
RENDER_TIMEOUT_S = 15 * 60
TERMINAL_TTL_S = 30 * 60
CACHE_IDLE_TTL_S = 24 * 60 * 60
CACHE_MAX_ITEMS = 32
CACHE_MAX_BYTES = 512 * 1024 * 1024
RENDER_CONTRACT_VERSION = "fx-preview-render-v1"

_PHASE_MESSAGES = {
    "queued": "已排入本機預覽工作",
    "preparing": "正在準備來源片段",
    "matte": "正在分析人物畫面",
    "rendering": "正在產生動態特效預覽",
    "validating": "正在驗證預覽檔案",
    "ready": "動態預覽已完成",
    "failed": "動態預覽產生失敗",
    "cancelled": "動態預覽已取消",
}


class FxPreviewJobError(RuntimeError):
    """Safe, user-facing API error."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(payload: Any) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    data = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, allow_nan=False
    ).encode("utf-8")
    with temporary.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _regular_nonsymlink(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and not path.is_symlink()


class FxPreviewJobManager:
    """Own one project's preview jobs, worker process and private cache."""

    def __init__(
        self,
        project_dir: Path,
        *,
        context_provider: Callable[[], dict[str, Any]],
        renderer_script: Path | None = None,
        artifact_validator: Callable[[Path, float], dict[str, Any]] | None = None,
        render_timeout_s: float = RENDER_TIMEOUT_S,
    ) -> None:
        self.project_dir = project_dir.resolve()
        self.context_provider = context_provider
        self.renderer_script = (
            renderer_script
            if renderer_script is not None
            else Path(__file__).with_name("fx_preview_render.py")
        ).resolve()
        self.artifact_validator = artifact_validator or self._validate_mp4
        self.render_timeout_s = float(render_timeout_s)
        self._lock = threading.RLock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._active_job_id: str | None = None

        working = self._safe_project_entry("working", must_exist=True)
        self.root = working / "fx-preview"
        if self.root.exists() and (self.root.is_symlink() or not self.root.is_dir()):
            raise ValueError("FX preview cache path is unsafe")
        self.root.mkdir(mode=0o700, parents=False, exist_ok=True)
        self.cache_dir = self.root / "cache"
        self.renderer_cache_dir = self.root / "renderer-cache"
        self.runtime_dir = self.root / "runtime"
        for directory in (self.cache_dir, self.renderer_cache_dir, self.runtime_dir):
            if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
                raise ValueError("FX preview cache path is unsafe")
            directory.mkdir(mode=0o700, exist_ok=True)
        self._cleanup_orphan_partials()

    def _safe_project_entry(self, relative: str, *, must_exist: bool) -> Path:
        rel = Path(relative)
        if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
            raise FxPreviewJobError(409, "unsafe_project_path", "專案來源路徑不安全")
        entry = self.project_dir / rel
        if entry.is_symlink():
            raise FxPreviewJobError(409, "unsafe_project_path", "專案來源路徑不安全")
        resolved = entry.resolve()
        if self.project_dir not in resolved.parents:
            raise FxPreviewJobError(409, "unsafe_project_path", "專案來源路徑不安全")
        if must_exist and not resolved.exists():
            raise FxPreviewJobError(409, "missing_project_file", "專案來源檔案不存在")
        return resolved

    def _source_identity(self, manifest: dict[str, Any]) -> dict[str, Any]:
        source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
        relative = source.get("staged_path")
        if not isinstance(relative, str) or not relative:
            raise FxPreviewJobError(409, "source_unavailable", "目前專案沒有可預覽的來源影片")
        path = self._safe_project_entry(relative, must_exist=True)
        if not _regular_nonsymlink(path):
            raise FxPreviewJobError(409, "unsafe_source", "來源影片不是安全的專案檔案")
        info = path.stat()
        digest = _file_sha256(path)
        declared = source.get("sha256")
        if declared is not None and (
            not isinstance(declared, str)
            or SHA256_RE.fullmatch(declared) is None
            or declared != digest
        ):
            raise FxPreviewJobError(409, "source_changed", "來源影片已變更，請重新載入專案")
        duration = source.get("duration_s")
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(float(duration))
            or float(duration) <= 0
        ):
            raise FxPreviewJobError(409, "source_duration_invalid", "來源影片長度無效")
        return {
            "path": path,
            "relative": str(path.relative_to(self.project_dir)),
            "sha256": digest,
            "bytes": info.st_size,
            "duration_s": float(duration),
        }

    @staticmethod
    def _state_revision(context: dict[str, Any]) -> str:
        revision = context.get("state_revision")
        if not isinstance(revision, str) or SHA256_RE.fullmatch(revision) is None:
            raise FxPreviewJobError(409, "state_revision_invalid", "編輯狀態版本無效，請重新載入")
        return revision

    @staticmethod
    def _pack_id(state: dict[str, Any]) -> str:
        style_pack = state.get("style_pack")
        if not isinstance(style_pack, dict):
            return ""
        pack_id = style_pack.get("project_default")
        return str(pack_id) if isinstance(pack_id, str) else ""

    @staticmethod
    def _canvas(state: dict[str, Any]) -> dict[str, Any]:
        canvas = state.get("canvas") if isinstance(state.get("canvas"), dict) else {}
        try:
            width = int(canvas.get("width"))
            height = int(canvas.get("height"))
        except (TypeError, ValueError):
            raise FxPreviewJobError(422, "canvas_invalid", "專案畫布尺寸無效") from None
        if width <= 0 or height <= 0 or not fx_overlay.canvas_ok((width, height)):
            raise FxPreviewJobError(422, "canvas_unsupported", "FX 預覽目前只支援 9:16 畫布")
        fit = canvas.get("fit", "cover")
        if fit not in {"contain", "cover"}:
            raise FxPreviewJobError(422, "canvas_invalid", "專案畫布縮放模式無效")
        return {"width": width, "height": height, "fit": fit}

    @staticmethod
    def _validate_keys(payload: dict[str, Any], allowed: set[str], required: set[str], label: str) -> None:
        unknown = set(payload) - allowed
        missing = required - set(payload)
        if unknown or missing:
            raise FxPreviewJobError(400, "malformed_request", f"{label}欄位不符合 API 規格")

    def _normalize_candidate(
        self, body: Any, context: dict[str, Any]
    ) -> tuple[dict[str, Any], str | None, dict[str, Any]]:
        if not isinstance(body, dict):
            raise FxPreviewJobError(400, "malformed_request", "請求內容必須是 JSON 物件")
        self._validate_keys(
            body,
            {"schema_version", "expected_revision", "candidate", "replace_overlay_id"},
            {"schema_version", "expected_revision", "candidate"},
            "請求",
        )
        if body.get("schema_version") != 1:
            raise FxPreviewJobError(400, "unsupported_schema", "不支援的 FX 預覽 API 版本")
        revision = self._state_revision(context)
        if body.get("expected_revision") != revision:
            raise FxPreviewJobError(409, "revision_conflict", "時間軸已變更，請重新載入後再預覽")
        replace_overlay_id = body.get("replace_overlay_id")
        if replace_overlay_id is not None and (
            not isinstance(replace_overlay_id, str)
            or OVERLAY_ID_RE.fullmatch(replace_overlay_id) is None
        ):
            raise FxPreviewJobError(422, "replace_overlay_invalid", "要替換的 FX 圖層識別碼無效")

        candidate = body.get("candidate")
        if not isinstance(candidate, dict):
            raise FxPreviewJobError(400, "malformed_candidate", "FX candidate 必須是物件")
        self._validate_keys(
            candidate,
            {"code", "window", "values", "placement", "scheme", "font_ids"},
            {"code", "window", "values", "placement", "scheme", "font_ids"},
            "candidate",
        )
        code = candidate.get("code")
        if not isinstance(code, str) or re.fullmatch(r"[A-Z][0-9]{1,3}", code) is None:
            raise FxPreviewJobError(422, "effect_invalid", "FX 效果代碼無效")
        if code == "B1":
            raise FxPreviewJobError(422, "material_required", "B1 需要先選擇本專案素材，這版暫不提供動態預覽")
        catalog = fx_overlay.catalog(refresh=True)
        effect = next(
            (
                item
                for item in catalog.get("effects", [])
                if isinstance(item, dict) and item.get("code") == code
            ),
            None,
        )
        if effect is None or effect.get("available") is False:
            raise FxPreviewJobError(422, "effect_unavailable", "這個 FX 效果目前無法使用")

        window = candidate.get("window")
        if not isinstance(window, dict):
            raise FxPreviewJobError(400, "window_invalid", "預覽時間窗必須是物件")
        self._validate_keys(window, {"start_s", "duration_s"}, {"start_s", "duration_s"}, "window")
        start = window.get("start_s")
        duration = window.get("duration_s")
        normalized_window: list[float] = []
        try:
            for value in (start, duration):
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise TypeError
                normalized = float(value)
                if not math.isfinite(normalized):
                    raise ValueError
                normalized_window.append(normalized)
        except (OverflowError, TypeError, ValueError):
            raise FxPreviewJobError(
                422,
                "window_invalid",
                "預覽時間必須是有限數字",
            ) from None
        start = round(normalized_window[0], 3)
        duration = round(normalized_window[1], 3)
        source_identity = self._source_identity(context.get("manifest") or {})
        if start < 0 or not 0.1 <= duration <= 4.0 or start + duration > source_identity["duration_s"] + 1e-6:
            raise FxPreviewJobError(422, "window_out_of_range", "預覽時間窗必須位於來源影片內，長度為 0.1 到 4 秒")
        media_max = effect.get("media_max")
        if isinstance(media_max, (int, float)) and not isinstance(media_max, bool) and duration > float(media_max) + 1e-6:
            raise FxPreviewJobError(422, "window_too_long", "這個 FX 效果需要更短的來源片段")

        values = candidate.get("values")
        if not isinstance(values, dict) or len(values) > MAX_REQUEST_VALUES:
            raise FxPreviewJobError(422, "values_invalid", "FX 文字參數格式無效")
        allowed_values = {
            str(item.get("id"))
            for item in effect.get("vars", [])
            if isinstance(item, dict) and item.get("kind") == "text"
        }
        normalized_values: dict[str, str | int | float] = {}
        total_chars = 0
        for key, value in values.items():
            if not isinstance(key, str) or key not in allowed_values:
                raise FxPreviewJobError(422, "values_invalid", "FX 文字參數含有未支援欄位")
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise FxPreviewJobError(422, "values_invalid", "FX 文字參數必須是文字或有限數字")
            if isinstance(value, float) and not math.isfinite(value):
                raise FxPreviewJobError(422, "values_invalid", "FX 文字參數必須是有限數字")
            rendered = str(value)
            if len(rendered) > MAX_VALUE_CHARS:
                raise FxPreviewJobError(422, "values_too_large", "FX 單一文字參數過長")
            total_chars += len(rendered)
            normalized_values[key] = value
        if total_chars > MAX_TOTAL_VALUE_CHARS:
            raise FxPreviewJobError(422, "values_too_large", "FX 文字參數總長度過長")

        placement = candidate.get("placement")
        scheme = candidate.get("scheme")
        if not isinstance(placement, str) or not isinstance(scheme, str):
            raise FxPreviewJobError(422, "style_invalid", "FX 位置或色卡格式無效")
        font_ids = candidate.get("font_ids")
        if not isinstance(font_ids, dict):
            raise FxPreviewJobError(422, "fonts_invalid", "font_ids 必須是物件")
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in font_ids.items()):
            raise FxPreviewJobError(422, "fonts_invalid", "font_ids 格式無效")
        font_errors = fx_overlay.validate_font_ids(font_ids)
        if font_errors:
            raise FxPreviewJobError(422, "fonts_invalid", "選取的 FX 字型未通過本機驗證")

        candidate_fields = {
            "code": code,
            "start": start,
            "end": round(start + duration, 3),
            "values": normalized_values,
            "placement": placement,
            "scheme": scheme,
            "font_ids": dict(sorted(font_ids.items())),
            "text": str(effect.get("name") or code)[:200],
            "provenance": "FX dynamic preview confirmation",
        }
        overlay = {
            "id": "fx-preview",
            "type": "fx",
            "style": {"x": 50, "y": 50, "width": 100, "animation": "none", "opacity": 1},
            "visible": True,
            "locked": False,
            "z_index": 25,
            "source": None,
            "highlight_id": None,
            "emphasis": [],
        }
        state = context.get("state") if isinstance(context.get("state"), dict) else {}
        if replace_overlay_id is not None:
            existing = next(
                (
                    item
                    for item in state.get("overlays", [])
                    if isinstance(item, dict)
                    and item.get("type") == "fx"
                    and item.get("id") == replace_overlay_id
                ),
                None,
            )
            if existing is None:
                raise FxPreviewJobError(
                    409,
                    "replace_target_stale",
                    "要替換的 FX 圖層已不存在",
                )
            # Replacement is a field-level edit, not a delete-and-recreate.
            # Preserve server-owned stacking, visibility, lock, linkage, style
            # and metadata while freezing only candidate-owned FX controls.
            overlay = deepcopy(existing)
        overlay.update(candidate_fields)
        errors = fx_overlay.validate(overlay, state)
        if errors:
            raise FxPreviewJobError(422, "candidate_invalid", "FX candidate 未通過伺服器驗證")
        return overlay, replace_overlay_id, source_identity

    def _dependency_snapshot(
        self,
        context: dict[str, Any],
        overlay: dict[str, Any],
        source_identity: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = context.get("state") if isinstance(context.get("state"), dict) else {}
        source = source_identity or self._source_identity(context.get("manifest") or {})
        pack_id = self._pack_id(state)
        theme, main = fx_overlay.theme_for_pack(pack_id)
        skill = fx_overlay.skill_dir().resolve()
        try:
            effect = next(
                item
                for item in fx_overlay.catalog(refresh=True).get("effects", [])
                if isinstance(item, dict) and item.get("code") == overlay["code"]
            )
        except StopIteration:
            raise FxPreviewJobError(
                503,
                "renderer_unavailable",
                "本機 FX 預覽元件已變更，請重新載入",
            ) from None
        block = effect.get("block")
        if not isinstance(block, str) or not block or Path(block).name != block:
            raise FxPreviewJobError(503, "renderer_unavailable", "本機 FX 預覽元件不完整")
        dependency_paths = {
            "effects": skill / "menu/effects.json",
            "compose": skill / "tools/compose.py",
            "schemes": skill / "tools/card_schemes.py",
            "block": skill / "hyperframes-blocks/compositions" / f"{block}.html",
            "fx_overlay": Path(fx_overlay.__file__).resolve(),
            "renderer": self.renderer_script,
        }
        theme_path = skill / "menu/themes" / f"{theme}.json"
        if theme_path.is_file():
            dependency_paths["theme"] = theme_path
        digests: dict[str, str] = {}
        for name, path in dependency_paths.items():
            if not _regular_nonsymlink(path):
                raise FxPreviewJobError(503, "renderer_unavailable", "本機 FX 預覽元件不完整")
            digests[name] = _file_sha256(path)
        canvas = self._canvas(state)
        return {
            "contract": RENDER_CONTRACT_VERSION,
            "project_namespace": hashlib.sha256(str(self.project_dir).encode("utf-8")).hexdigest(),
            "state_revision": self._state_revision(context),
            "source": {key: source[key] for key in ("relative", "sha256", "bytes")},
            "pack_id": pack_id,
            "theme": theme,
            "main": main,
            "canvas": canvas,
            "fonts_digest": fx_overlay.fonts_digest_for({"overlays": [overlay]}),
            "dependencies": digests,
            "tools": self._tool_identities(),
        }

    @staticmethod
    def _tool_identities() -> dict[str, Any]:
        identities: dict[str, Any] = {}
        for name in ("ffmpeg", "ffprobe", "hyperframes"):
            resolved = shutil.which(name)
            if not resolved:
                identities[name] = None
                continue
            path = Path(resolved).resolve()
            try:
                info = path.stat()
                identities[name] = {
                    "path": str(path),
                    "bytes": info.st_size,
                    "mtime_ns": info.st_mtime_ns,
                }
            except OSError:
                identities[name] = None
        return identities

    def submit(self, body: Any) -> dict[str, Any]:
        self.prune()
        context = self.context_provider()
        overlay, replace_overlay_id, source_identity = self._normalize_candidate(body, context)
        dependencies = self._dependency_snapshot(context, overlay, source_identity)
        # State revision remains part of the job's post-render and confirm CAS
        # snapshot, but is not a media dependency.  Excluding only that field
        # lets an identical candidate reuse an artifact after an unrelated
        # timeline edit without weakening stale-confirm protection.
        cache_dependencies = {
            key: value for key, value in dependencies.items() if key != "state_revision"
        }
        cache_key = _canonical_sha256(
            {
                "dependencies": cache_dependencies,
                "overlay": overlay,
                "window_ms": [round(float(overlay["start"]) * 1000), round((float(overlay["end"]) - float(overlay["start"])) * 1000)],
            }
        )
        job_id = secrets.token_hex(16)
        now = time.time()
        job = {
            "id": job_id,
            "state": "running",
            "phase": "queued",
            "message": _PHASE_MESSAGES["queued"],
            "cache_hit": False,
            "created_monotonic": now,
            "updated_monotonic": now,
            "state_revision": dependencies["state_revision"],
            "overlay": overlay,
            "replace_overlay_id": replace_overlay_id,
            "dependencies": dependencies,
            "dependency_hash": _canonical_sha256(dependencies),
            "source_identity": source_identity,
            "cache_key": cache_key,
            "process": None,
            "cancel_requested": False,
            "media": None,
            "error": None,
        }
        cache_media = self.cache_dir / f"{cache_key}.mp4"
        cache_receipt = self.cache_dir / f"{cache_key}.json"
        cached = self._valid_cache_hit(cache_media, cache_receipt, cache_key)
        with self._lock:
            if self._active_job_id is not None:
                active = self._jobs.get(self._active_job_id)
                if active and active.get("state") in RUNNING_STATES:
                    raise FxPreviewJobError(409, "preview_busy", "同一專案已有 FX 預覽正在產生")
                self._active_job_id = None
            if cached is not None:
                job.update(
                    {
                        "state": "ready",
                        "phase": "ready",
                        "message": _PHASE_MESSAGES["ready"],
                        "cache_hit": True,
                        "media": cached,
                    }
                )
                self._jobs[job_id] = job
                return self.public_job(job_id)
            self._jobs[job_id] = job
            self._active_job_id = job_id

        thread = threading.Thread(
            target=self._run_job,
            args=(job_id, cache_media, cache_receipt),
            daemon=True,
        )
        try:
            thread.start()
        except RuntimeError as exc:
            with self._lock:
                self._finish_failed(job_id, "worker_start_failed", "無法啟動本機 FX 預覽工作")
            raise FxPreviewJobError(500, "worker_start_failed", "無法啟動本機 FX 預覽工作") from exc
        return self.public_job(job_id)

    def _valid_cache_hit(self, media: Path, receipt_path: Path, cache_key: str) -> dict[str, Any] | None:
        receipt = _read_json(receipt_path)
        if not isinstance(receipt, dict) or receipt.get("cache_key") != cache_key:
            return None
        if not _regular_nonsymlink(media):
            return None
        expected = receipt.get("sha256")
        if not isinstance(expected, str) or SHA256_RE.fullmatch(expected) is None:
            return None
        if media.stat().st_size <= 0 or media.stat().st_size > MAX_PREVIEW_BYTES:
            return None
        if _file_sha256(media) != expected:
            return None
        try:
            duration = float(receipt.get("duration_s"))
            self.artifact_validator(media, duration)
        except (TypeError, ValueError, OSError, subprocess.SubprocessError):
            return None
        now = time.time()
        try:
            os.utime(media, (now, now), follow_symlinks=False)
            os.utime(receipt_path, (now, now), follow_symlinks=False)
        except OSError:
            return None
        return {
            "path": media,
            "sha256": expected,
            "bytes": media.stat().st_size,
            "duration_s": duration,
        }

    def _run_job(self, job_id: str, cache_media: Path, cache_receipt: Path) -> None:
        request_path = self.runtime_dir / f"{job_id}.request.json"
        result_path = self.runtime_dir / f"{job_id}.result.json"
        output_path = self.runtime_dir / f"{job_id}.part.mp4"
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            source = dict(job["source_identity"])
            overlay = dict(job["overlay"])
            deps = dict(job["dependencies"])
        request_payload = {
            "project_dir": str(self.project_dir),
            "source": str(source["path"]),
            "cache_dir": str(self.renderer_cache_dir),
            "output": str(output_path),
            "overlay": overlay,
            "pack_id": deps["pack_id"],
            "canvas": deps["canvas"],
        }
        try:
            _atomic_json(request_path, request_payload)
            env = self._worker_environment()
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(self.renderer_script),
                    "--request",
                    str(request_path),
                    "--result",
                    str(result_path),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
                env=env,
            )
            with self._lock:
                current = self._jobs.get(job_id)
                if current is None or current.get("cancel_requested"):
                    self._terminate_process(process)
                    self._finish_cancelled(job_id)
                    return
                current["process"] = process
                current["phase"] = "preparing"
                current["message"] = _PHASE_MESSAGES["preparing"]
            progress = threading.Thread(
                target=self._consume_progress,
                args=(job_id, process.stderr),
                daemon=True,
            )
            progress.start()
            deadline = time.monotonic() + self.render_timeout_s
            while process.poll() is None:
                with self._lock:
                    current = self._jobs.get(job_id)
                    cancelled = current is None or bool(current.get("cancel_requested"))
                if cancelled:
                    self._terminate_process(process)
                    with self._lock:
                        self._finish_cancelled(job_id)
                    return
                if time.monotonic() >= deadline:
                    self._terminate_process(process)
                    with self._lock:
                        self._finish_failed(job_id, "render_timeout", "FX 預覽超過本機時間上限")
                    return
                time.sleep(0.05)
            progress.join(timeout=1.0)
            result = _read_json(result_path)
            if process.returncode != 0 or not isinstance(result, dict) or result.get("ok") is not True:
                code = result.get("code") if isinstance(result, dict) else None
                safe_code = code if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,40}", code) else "render_failed"
                safe_messages = {
                    "person_unavailable": "來源片段找不到可用的人物，未使用測試人物代替",
                    "invalid_source": "來源影片無法用於 FX 預覽",
                    "invalid_canvas": "目前專案畫布無法產生 FX 預覽",
                    "broll_requires_asset": "B1 需要先選擇本專案素材",
                    "output_invalid": "FX 預覽輸出未通過媒體驗證",
                    "invalid_request": "本機 FX 預覽工作收到無效資料",
                }
                with self._lock:
                    self._finish_failed(job_id, safe_code, safe_messages.get(safe_code, "FX 預覽產生失敗"))
                return
            out_value = result.get("out")
            if not isinstance(out_value, str) or Path(out_value).resolve() != output_path.resolve():
                with self._lock:
                    self._finish_failed(job_id, "worker_contract_invalid", "FX 預覽工作回傳了無效檔案")
                return
            with self._lock:
                current = self._jobs.get(job_id)
                if current is None or current.get("cancel_requested"):
                    self._finish_cancelled(job_id)
                    return
                current["phase"] = "validating"
                current["message"] = _PHASE_MESSAGES["validating"]
            try:
                current_context = self.context_provider()
                current_source = self._source_identity(current_context.get("manifest") or {})
                current_deps = self._dependency_snapshot(current_context, overlay, current_source)
            except FxPreviewJobError:
                with self._lock:
                    self._finish_failed(
                        job_id,
                        "preview_stale",
                        "預覽期間來源或樣式已變更，請重新產生",
                    )
                return
            with self._lock:
                current = self._jobs.get(job_id)
                if current is None or current.get("cancel_requested"):
                    self._finish_cancelled(job_id)
                    return
                if _canonical_sha256(current_deps) != current["dependency_hash"]:
                    self._finish_failed(job_id, "preview_stale", "預覽期間來源或樣式已變更，請重新產生")
                    return
            duration = round(float(overlay["end"]) - float(overlay["start"]), 3)
            metadata = self.artifact_validator(output_path, duration)
            if not isinstance(metadata, dict):
                raise ValueError("artifact validator returned no metadata")
            size = output_path.stat().st_size
            if size <= 0 or size > MAX_PREVIEW_BYTES:
                raise ValueError("preview size invalid")
            digest = _file_sha256(output_path)
            receipt = {
                "schema_version": 1,
                "cache_key": job["cache_key"],
                "sha256": digest,
                "bytes": size,
                "duration_s": metadata.get("duration_s", duration),
                "created_at": time.time(),
            }
            self._touch_renderer_cache()
            os.replace(output_path, cache_media)
            _atomic_json(cache_receipt, receipt)
            with self._lock:
                current = self._jobs.get(job_id)
                if current is None or current.get("cancel_requested"):
                    cache_media.unlink(missing_ok=True)
                    cache_receipt.unlink(missing_ok=True)
                    self._finish_cancelled(job_id)
                    return
                current.update(
                    {
                        "state": "ready",
                        "phase": "ready",
                        "message": _PHASE_MESSAGES["ready"],
                        "updated_monotonic": time.time(),
                        "process": None,
                        "media": {
                            "path": cache_media,
                            "sha256": digest,
                            "bytes": size,
                            "duration_s": receipt["duration_s"],
                        },
                        "error": None,
                    }
                )
                if self._active_job_id == job_id:
                    self._active_job_id = None
            self.prune()
        except (FxPreviewJobError, OSError, ValueError, subprocess.SubprocessError):
            with self._lock:
                self._finish_failed(job_id, "render_failed", "FX 預覽產生失敗")
        finally:
            output_path.unlink(missing_ok=True)
            request_path.unlink(missing_ok=True)
            result_path.unlink(missing_ok=True)

    @staticmethod
    def _worker_environment() -> dict[str, str]:
        allowed = {
            "PATH",
            "LANG",
            "LC_ALL",
            "TMPDIR",
            "HOME",
            "USER",
            "LOGNAME",
            "SHELL",
            "PYTHONPATH",
            "EDIT_STYLE_PROFILES_SKILL_DIR",
            "AUTO_EDIT_DISABLE_FX",
        }
        env = {key: value for key, value in os.environ.items() if key in allowed}
        env.update(
            {
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "HYPERFRAMES_RENDER_DETACHED": "1",
            }
        )
        return env

    def _consume_progress(self, job_id: str, stream: Any) -> None:
        if stream is None:
            return
        try:
            for line in stream:
                if len(line) > 4096:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                phase = payload.get("phase") if isinstance(payload, dict) else None
                if phase not in _PHASE_MESSAGES or phase in TERMINAL_STATES:
                    continue
                with self._lock:
                    job = self._jobs.get(job_id)
                    if job is None or job.get("state") != "running":
                        return
                    job["phase"] = phase
                    job["message"] = _PHASE_MESSAGES[phase]
                    job["updated_monotonic"] = time.time()
        finally:
            try:
                stream.close()
            except OSError:
                pass

    @staticmethod
    def _process_group_exists(pgid: int) -> bool:
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # The group still exists.  This should not occur for our own
            # start_new_session worker, but fail closed if platform policy
            # changes rather than assuming descendants are gone.
            return True

    @classmethod
    def _terminate_process(cls, process: subprocess.Popen[str]) -> None:
        pgid = process.pid
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
            return

        deadline = time.monotonic() + 3.0
        while cls._process_group_exists(pgid) and time.monotonic() < deadline:
            # Reap the direct child as soon as it exits, but continue checking
            # its process group: a grandchild may ignore TERM after the parent
            # has already died.
            try:
                process.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                pass
            time.sleep(0.05)

        if cls._process_group_exists(pgid):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            kill_deadline = time.monotonic() + 2.0
            while cls._process_group_exists(pgid) and time.monotonic() < kill_deadline:
                time.sleep(0.05)
        try:
            process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            pass

    @staticmethod
    def _validate_mp4(path: Path, expected_duration: float) -> dict[str, Any]:
        if not _regular_nonsymlink(path) or path.suffix.lower() != ".mp4":
            raise ValueError("preview artifact is not a regular MP4")
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name:format=duration",
                "-of",
                "json",
                str(path),
            ],
            text=True,
            capture_output=True,
            timeout=30,
        )
        payload = json.loads(result.stdout or "{}")
        duration = float((payload.get("format") or {}).get("duration") or 0)
        if result.returncode != 0 or not payload.get("streams") or not math.isfinite(duration):
            raise ValueError("preview MP4 has no visual stream")
        if abs(duration - expected_duration) > max(0.25, expected_duration * 0.1):
            raise ValueError("preview MP4 duration mismatch")
        return {"duration_s": duration}

    def _finish_failed(self, job_id: str, code: str, message: str) -> None:
        job = self._jobs.get(job_id)
        if job is None or job.get("state") in TERMINAL_STATES:
            return
        job.update(
            {
                "state": "failed",
                "phase": "failed",
                "message": message,
                "error": {"code": code, "message": message},
                "process": None,
                "updated_monotonic": time.time(),
            }
        )
        if self._active_job_id == job_id:
            self._active_job_id = None

    def _finish_cancelled(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is None or job.get("state") == "cancelled":
            return
        job.update(
            {
                "state": "cancelled",
                "phase": "cancelled",
                "message": _PHASE_MESSAGES["cancelled"],
                "error": None,
                "process": None,
                "updated_monotonic": time.time(),
            }
        )
        if self._active_job_id == job_id:
            self._active_job_id = None

    def has_active_job(self) -> bool:
        with self._lock:
            if self._active_job_id is None:
                return False
            job = self._jobs.get(self._active_job_id)
            return bool(job and job.get("state") in RUNNING_STATES)

    def cancel(self, job_id: str) -> dict[str, Any]:
        self.prune()
        if JOB_ID_RE.fullmatch(job_id) is None:
            raise FxPreviewJobError(404, "job_not_found", "找不到這個 FX 預覽工作")
        process: subprocess.Popen[str] | None = None
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise FxPreviewJobError(404, "job_not_found", "找不到這個 FX 預覽工作")
            if job.get("state") in TERMINAL_STATES:
                return self._public_job_locked(job)
            job["cancel_requested"] = True
            process = job.get("process")
        if process is not None:
            self._terminate_process(process)
        with self._lock:
            self._finish_cancelled(job_id)
            return self._public_job_locked(self._jobs[job_id])

    def public_job(self, job_id: str) -> dict[str, Any]:
        self.prune()
        if JOB_ID_RE.fullmatch(job_id) is None:
            raise FxPreviewJobError(404, "job_not_found", "找不到這個 FX 預覽工作")
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise FxPreviewJobError(404, "job_not_found", "找不到這個 FX 預覽工作")
            return self._public_job_locked(job)

    @staticmethod
    def _public_job_locked(job: dict[str, Any]) -> dict[str, Any]:
        public = {
            "id": job["id"],
            "state": job["state"],
            "phase": job["phase"],
            "message": job["message"],
            "status_url": f"/api/fx-previews/{job['id']}",
            "cache_hit": bool(job.get("cache_hit")),
        }
        media = job.get("media")
        if job.get("state") == "ready" and isinstance(media, dict):
            public["media_url"] = f"/fx-preview-media/{job['id']}/{media['sha256']}.mp4"
        if isinstance(job.get("error"), dict):
            public["error"] = dict(job["error"])
        return public

    def response(self, job_id: str) -> dict[str, Any]:
        job = self.public_job(job_id)
        with self._lock:
            revision = str(self._jobs[job_id]["state_revision"])
        return {"ok": True, "job": job, "state_revision": revision}

    def confirmable(self, job_id: str, expected_revision: Any) -> dict[str, Any]:
        if not isinstance(expected_revision, str) or SHA256_RE.fullmatch(expected_revision) is None:
            raise FxPreviewJobError(409, "revision_required", "confirm 必須帶目前的時間軸版本")
        context = self.context_provider()
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise FxPreviewJobError(404, "job_not_found", "找不到這個 FX 預覽工作")
            if job.get("state") != "ready":
                raise FxPreviewJobError(409, "preview_not_ready", "FX 預覽尚未完成，不能確認套用")
            frozen = {
                "overlay": json.loads(json.dumps(job["overlay"], ensure_ascii=False)),
                "replace_overlay_id": job.get("replace_overlay_id"),
                "dependency_hash": job["dependency_hash"],
            }
        if expected_revision != self._state_revision(context) or expected_revision != job["state_revision"]:
            raise FxPreviewJobError(409, "preview_stale", "預覽後時間軸已變更，請重新產生再套用")
        current_deps = self._dependency_snapshot(context, frozen["overlay"])
        if _canonical_sha256(current_deps) != frozen["dependency_hash"]:
            raise FxPreviewJobError(409, "preview_stale", "預覽依賴的來源、主題或字型已變更")
        return frozen

    def media_path(self, job_id: str, digest: str) -> Path:
        if JOB_ID_RE.fullmatch(job_id) is None or SHA256_RE.fullmatch(digest) is None:
            raise FxPreviewJobError(404, "media_not_found", "找不到這個 FX 預覽檔")
        with self._lock:
            job = self._jobs.get(job_id)
            media = job.get("media") if job else None
            if not job or job.get("state") != "ready" or not isinstance(media, dict) or media.get("sha256") != digest:
                raise FxPreviewJobError(404, "media_not_found", "找不到這個 FX 預覽檔")
            path = media.get("path")
        if not isinstance(path, Path) or not _regular_nonsymlink(path):
            raise FxPreviewJobError(404, "media_not_found", "找不到這個 FX 預覽檔")
        resolved = path.resolve()
        if self.cache_dir.resolve() not in resolved.parents or _file_sha256(resolved) != digest:
            raise FxPreviewJobError(404, "media_not_found", "找不到這個 FX 預覽檔")
        try:
            now = time.time()
            os.utime(resolved, (now, now), follow_symlinks=False)
            cache_key = str(job.get("cache_key") or "")
            receipt = self.cache_dir / f"{cache_key}.json"
            if SHA256_RE.fullmatch(cache_key) and _regular_nonsymlink(receipt):
                os.utime(receipt, (now, now), follow_symlinks=False)
        except OSError:
            raise FxPreviewJobError(404, "media_not_found", "找不到這個 FX 預覽檔") from None
        return resolved

    def prune(self) -> None:
        now = time.time()
        protected: set[str] = set()
        expired: list[dict[str, Any]] = []
        with self._lock:
            for job_id, job in list(self._jobs.items()):
                if job.get("state") in TERMINAL_STATES and now - float(job.get("updated_monotonic") or now) > TERMINAL_TTL_S:
                    expired.append(job)
                    self._jobs.pop(job_id, None)
                elif job.get("state") in RUNNING_STATES and job.get("cache_key"):
                    protected.add(str(job["cache_key"]))
        for job in expired:
            for suffix in ("request.json", "result.json", "part.mp4"):
                (self.runtime_dir / f"{job['id']}.{suffix}").unlink(missing_ok=True)
        entries: list[tuple[float, int, str, Path]] = []
        for path in self.cache_dir.iterdir():
            if path.is_symlink() or not path.is_file() or not re.fullmatch(r"[0-9a-f]{64}\.mp4", path.name):
                continue
            info = path.stat()
            entries.append((info.st_mtime, info.st_size, path.stem, path))
        total = sum(item[1] for item in entries)
        count = len(entries)
        for mtime, size, key, media in sorted(entries):
            expired_by_ttl = now - mtime > CACHE_IDLE_TTL_S
            if not expired_by_ttl and count <= CACHE_MAX_ITEMS and total <= CACHE_MAX_BYTES:
                break
            if key in protected:
                continue
            with self._lock:
                for job in self._jobs.values():
                    if job.get("cache_key") != key or job.get("state") != "ready":
                        continue
                    message = "FX 預覽快取已依容量上限清理，請重新產生"
                    job.update(
                        {
                            "state": "failed",
                            "phase": "failed",
                            "message": message,
                            "media": None,
                            "error": {"code": "cache_evicted", "message": message},
                            "updated_monotonic": now,
                        }
                    )
            media.unlink(missing_ok=True)
            (self.cache_dir / f"{key}.json").unlink(missing_ok=True)
            count -= 1
            total -= size

        renderer_entries: list[tuple[float, int, Path]] = []
        for path in self.renderer_cache_dir.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            resolved = path.resolve()
            if self.renderer_cache_dir.resolve() not in resolved.parents:
                continue
            info = path.stat()
            renderer_entries.append((info.st_mtime, info.st_size, path))
        renderer_total = sum(item[1] for item in renderer_entries)
        if self.has_active_job():
            return
        for mtime, size, path in sorted(renderer_entries):
            expired_by_ttl = now - mtime > CACHE_IDLE_TTL_S
            if not expired_by_ttl and total + renderer_total <= CACHE_MAX_BYTES:
                break
            path.unlink(missing_ok=True)
            renderer_total -= size
        for directory in sorted(
            (path for path in self.renderer_cache_dir.rglob("*") if path.is_dir() and not path.is_symlink()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass

    def _touch_renderer_cache(self) -> None:
        """Record successful use so the 24-hour idle TTL is meaningful."""
        now = time.time()
        for path in self.renderer_cache_dir.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            resolved = path.resolve()
            if self.renderer_cache_dir.resolve() not in resolved.parents:
                continue
            try:
                os.utime(resolved, (now, now), follow_symlinks=False)
            except OSError:
                continue

    def close(self) -> None:
        with self._lock:
            active = self._active_job_id
        if active is not None:
            try:
                self.cancel(active)
            except FxPreviewJobError:
                pass

    def _cleanup_orphan_partials(self) -> None:
        for path in self.runtime_dir.iterdir():
            if path.is_symlink() or not path.is_file():
                continue
            if re.fullmatch(r"[0-9a-f]{32}\.(?:request\.json|result\.json|part\.mp4)", path.name):
                path.unlink(missing_ok=True)
