#!/usr/bin/env python3
"""Loopback-only import launcher for Auto Edit Studio.

The Studio server never accepts a client filesystem path. It receives one raw
browser File stream, validates it, creates an owned project copy atomically, and
then launches the existing fixed-project editor on another loopback port.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
import uuid
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from auto_edit import (
    DURATION_PROFILES,
    EDIT_PRESETS,
    PLATFORMS,
    SUBTITLE_MODES,
    initialize_project,
    probe_media,
)
from editor_server import EDITOR_DIR, EditorServer, atomic_write_json
from highlight_planner import DIRECTOR_PROFILES
from local_http_security import (
    csrf_token_matches,
    host_header_allowed,
    is_loopback_host,
    mutation_origin_allowed,
)


MAX_JSON_BYTES = 256 * 1024
DEFAULT_MAX_IMPORT_BYTES = 20 * 1024 * 1024 * 1024
DEFAULT_MAX_DURATION_S = 6 * 60 * 60
DEFAULT_MAX_SOURCE_PIXELS = 7680 * 4320
MAX_SOURCE_FPS = 240.0
MAX_STREAMS = 16
SOURCE_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}
SOURCE_MIME_TYPES = {
    ".mp4": {"video/mp4", "application/mp4"},
    ".mov": {"video/quicktime"},
    ".m4v": {"video/x-m4v", "video/mp4"},
    ".webm": {"video/webm"},
}
SOURCE_LANGUAGES = {"auto", "zh-TW", "zh-CN", "en-US", "en-GB"}
AUTO_EDIT_SCRIPT = Path(__file__).with_name("auto_edit.py")


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class StudioRequestError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def safe_project_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    return (slug or "video")[:28].strip("-") or "video"


def source_magic_matches(path: Path, suffix: str) -> bool:
    with path.open("rb") as handle:
        head = handle.read(64)
    if suffix == ".webm":
        return head.startswith(b"\x1a\x45\xdf\xa3")
    return len(head) >= 8 and head[4:8] in {
        b"ftyp",
        b"moov",
        b"wide",
        b"mdat",
        b"free",
        b"skip",
    }


def import_public_payload(session: dict[str, Any]) -> dict[str, Any]:
    return {
        key: session.get(key)
        for key in (
            "id",
            "state",
            "upload_url",
            "status_url",
            "bytes_expected",
            "bytes_received",
            "created_at",
            "updated_at",
            "error_code",
            "message",
        )
        if session.get(key) is not None
    }


class StudioServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        projects_root: Path,
        *,
        max_import_bytes: int = DEFAULT_MAX_IMPORT_BYTES,
        max_duration_s: float = DEFAULT_MAX_DURATION_S,
        max_source_pixels: int = DEFAULT_MAX_SOURCE_PIXELS,
        auto_process: bool = False,
    ):
        bound_host = str(address[0])
        if not is_loopback_host(bound_host):
            raise ValueError("Studio import server is loopback-only")
        root = projects_root.expanduser().resolve()
        if root.exists() and root.is_symlink():
            raise ValueError("projects root must not be a symlink")
        root.mkdir(parents=True, exist_ok=True)
        if not root.is_dir():
            raise ValueError("projects root must be a directory")
        super().__init__(address, StudioHandler)
        self.projects_root = root
        self.max_import_bytes = int(max_import_bytes)
        self.max_duration_s = float(max_duration_s)
        self.max_source_pixels = int(max_source_pixels)
        self.auto_process = bool(auto_process)
        self.csrf_token = secrets.token_urlsafe(32)
        self.imports: dict[str, dict[str, Any]] = {}
        self.import_lock = threading.Lock()
        self.active_upload_id: str | None = None
        self.active_pipeline_project: Path | None = None
        self.editor_servers: list[tuple[EditorServer, threading.Thread]] = []
        self.pipeline_lock = threading.Lock()
        self.pipeline_processes: set[subprocess.Popen[str]] = set()
        self.pipeline_threads: list[threading.Thread] = []
        self.stop_event = threading.Event()
        self.cleanup_stale_creating_dirs()

    def cleanup_stale_creating_dirs(self) -> None:
        cutoff = time.time() - 24 * 60 * 60
        for candidate in self.projects_root.glob(".creating-*"):
            try:
                if candidate.is_dir() and not candidate.is_symlink() and candidate.stat().st_mtime < cutoff:
                    shutil.rmtree(candidate)
            except OSError:
                continue

    def start_editor(self, project_dir: Path) -> str:
        server = EditorServer(("127.0.0.1", 0), project_dir)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.editor_servers.append((server, thread))
        return f"http://127.0.0.1:{server.server_port}/"

    @staticmethod
    def terminate_pipeline_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            try:
                process.terminate()
            except OSError:
                return
        try:
            process.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                process.kill()
            except OSError:
                return
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass

    def run_pipeline_command(
        self,
        project_dir: Path,
        arguments: list[str],
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        if self.stop_event.is_set():
            raise RuntimeError("Studio is shutting down")
        process = subprocess.Popen(
            [sys.executable, str(AUTO_EDIT_SCRIPT), *arguments],
            cwd=str(project_dir),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        with self.pipeline_lock:
            self.pipeline_processes.add(process)
        try:
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                self.terminate_pipeline_process(process)
                stdout, stderr = process.communicate()
                raise RuntimeError("local processing step timed out") from exc
            return subprocess.CompletedProcess(
                process.args,
                int(process.returncode or 0),
                stdout,
                stderr,
            )
        finally:
            with self.pipeline_lock:
                self.pipeline_processes.discard(process)

    def pipeline_worker(
        self,
        project_dir: Path,
        director_profile: str,
        editing_brief: str,
    ) -> None:
        status_path = project_dir / "working/pipeline_status.json"
        started_at = now_utc()

        def write_status(
            state: str,
            phase: str,
            message: str,
            *,
            error_code: str | None = None,
            detail: str | None = None,
        ) -> None:
            payload: dict[str, Any] = {
                "schema_version": 1,
                "state": state,
                "phase": phase,
                "message": message,
                "started_at": started_at,
                "updated_at": now_utc(),
            }
            if error_code:
                payload["error_code"] = error_code
            if detail:
                payload["detail"] = detail[-600:]
            atomic_write_json(status_path, payload)

        manifest = project_dir / "project.json"
        steps = [
            (
                "transcribe",
                "正在用本機 Whisper 轉錄影片…",
                ["transcribe-local", "--manifest", str(manifest), "--model", "base"],
                21660,
            ),
            (
                "edit_analysis",
                "正在分析可剪除片段…",
                ["analyze-edits", "--manifest", str(manifest)],
                300,
            ),
            (
                "overlay_plan",
                "正在建立字幕、字卡與動畫提案…",
                ["plan-overlays", "--manifest", str(manifest)],
                300,
            ),
            (
                "highlight_plan",
                "正在挑選最多 10 段精華…",
                [
                    "plan-highlights",
                    "--manifest",
                    str(manifest),
                    "--director",
                    director_profile,
                    "--count",
                    "10",
                    "--brief",
                    editing_brief,
                ],
                300,
            ),
        ]
        try:
            for phase, message, arguments, timeout in steps:
                write_status("running", phase, message)
                result = self.run_pipeline_command(project_dir, arguments, timeout=timeout)
                if result.returncode == 0:
                    continue
                if phase == "highlight_plan" and result.returncode == 3:
                    write_status(
                        "needs_attention",
                        phase,
                        "轉錄完成，但沒有足夠語音內容可建立精華。",
                        error_code="no_highlight_candidates",
                    )
                    return
                write_status(
                    "failed",
                    phase,
                    "本機自動處理未完成；原始影片與專案已安全保留。",
                    error_code=f"{phase}_failed",
                    detail=(result.stderr or result.stdout or "").strip(),
                )
                return
            write_status(
                "needs_review",
                "human_review",
                "已產生精華提案；請人工確認片段、字幕與時間線。",
            )
        except (OSError, RuntimeError) as exc:
            if self.stop_event.is_set():
                write_status("stopped", "shutdown", "Studio 已停止；可重新啟動後再處理。")
            else:
                write_status(
                    "failed",
                    "pipeline",
                    "本機自動處理無法啟動；原始影片與專案已安全保留。",
                    error_code="pipeline_failed",
                    detail=str(exc),
                )
        finally:
            with self.pipeline_lock:
                if self.active_pipeline_project == project_dir:
                    self.active_pipeline_project = None

    def start_local_pipeline(
        self,
        project_dir: Path,
        director_profile: str,
        editing_brief: str,
    ) -> None:
        status_path = project_dir / "working/pipeline_status.json"
        if not self.auto_process:
            atomic_write_json(
                status_path,
                {
                    "schema_version": 1,
                    "state": "pending",
                    "phase": "transcribe",
                    "message": "等待啟動本機自動處理。",
                    "updated_at": now_utc(),
                },
            )
            return
        with self.pipeline_lock:
            if self.active_pipeline_project is not None:
                atomic_write_json(
                    status_path,
                    {
                        "schema_version": 1,
                        "state": "needs_attention",
                        "phase": "queue",
                        "message": "另一個影片仍在處理；請稍後重新啟動這個專案。",
                        "error_code": "pipeline_busy",
                        "updated_at": now_utc(),
                    },
                )
                return
            self.active_pipeline_project = project_dir
        thread = threading.Thread(
            target=self.pipeline_worker,
            args=(project_dir, director_profile, editing_brief),
            daemon=False,
            name=f"auto-edit-{project_dir.name}",
        )
        self.pipeline_threads.append(thread)
        thread.start()

    def server_close(self) -> None:
        self.stop_event.set()
        with self.pipeline_lock:
            processes = list(self.pipeline_processes)
        for process in processes:
            self.terminate_pipeline_process(process)
        for thread in list(self.pipeline_threads):
            thread.join(timeout=6)
        self.pipeline_threads.clear()
        for server, thread in list(self.editor_servers):
            try:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
            except OSError:
                pass
        self.editor_servers.clear()
        super().server_close()


class StudioHandler(BaseHTTPRequestHandler):
    server: StudioServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[studio] " + (fmt % args) + "\n")

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; media-src 'self' blob:; "
            "style-src 'self'; script-src 'self'; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        super().end_headers()

    def send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_problem(self, error: StudioRequestError) -> None:
        self.send_json(
            {"ok": False, "error": error.message, "error_code": error.code},
            status=error.status,
        )

    def allow_request(self, mutation: bool = False) -> bool:
        if not host_header_allowed(
            str(self.server.server_address[0]),
            self.headers.get("Host", ""),
        ):
            self.close_connection = True
            self.send_json({"ok": False, "error": "invalid Host for local Studio"}, status=403)
            return False
        if mutation and not mutation_origin_allowed(self.headers):
            self.close_connection = True
            self.send_json({"ok": False, "error": "cross-origin writes are not allowed"}, status=403)
            return False
        if mutation and not csrf_token_matches(self.headers, self.server.csrf_token):
            self.close_connection = True
            self.send_json({"ok": False, "error": "missing or invalid Studio request token"}, status=403)
            return False
        return True

    def route(self) -> str:
        return urllib.parse.urlparse(self.path).path

    def content_length(self, *, required: bool = True) -> int:
        raw = self.headers.get("Content-Length")
        if raw is None:
            if required:
                raise StudioRequestError(411, "length_required", "Content-Length is required")
            return 0
        try:
            value = int(raw)
        except ValueError as exc:
            raise StudioRequestError(400, "invalid_length", "Content-Length is invalid") from exc
        if value < 0:
            raise StudioRequestError(400, "invalid_length", "Content-Length is invalid")
        return value

    def read_json_body(self) -> Any:
        length = self.content_length()
        if length > MAX_JSON_BYTES:
            raise StudioRequestError(413, "metadata_too_large", "Import metadata is too large")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise StudioRequestError(400, "short_body", "Import metadata body was truncated")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StudioRequestError(400, "invalid_json", "Request body must be valid UTF-8 JSON") from exc

    def serve_static(self, path: Path) -> None:
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
        }.get(path.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not self.allow_request():
            return
        path = self.route()
        if path == "/api/studio":
            self.send_json(
                {
                    "ok": True,
                    "csrf_token": self.server.csrf_token,
                    "max_import_bytes": self.server.max_import_bytes,
                    "source_extensions": sorted(SOURCE_EXTENSIONS),
                    "loopback_only": True,
                }
            )
            return
        match = re.fullmatch(r"/api/imports/(imp_[0-9a-f]{32})", path)
        if match:
            with self.server.import_lock:
                session = self.server.imports.get(match.group(1))
                payload = import_public_payload(session) if session else None
            if payload is None:
                self.send_json({"ok": False, "error": "import session not found"}, status=404)
            else:
                self.send_json({"ok": True, "import": payload})
            return
        if path in {"/", "/index.html", "/import.html"}:
            self.serve_static(EDITOR_DIR / "import.html")
            return
        if path == "/import.js":
            self.serve_static(EDITOR_DIR / "import.js")
            return
        if path == "/styles.css":
            self.serve_static(EDITOR_DIR / "styles.css")
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if not self.allow_request(mutation=True):
            return
        if self.route() != "/api/imports":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            self.handle_create_import()
        except StudioRequestError as exc:
            self.send_problem(exc)

    def do_PUT(self) -> None:
        if not self.allow_request(mutation=True):
            return
        match = re.fullmatch(r"/api/imports/(imp_[0-9a-f]{32})/content", self.route())
        if not match:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.handle_upload(match.group(1))

    def handle_create_import(self) -> None:
        body = self.read_json_body()
        if not isinstance(body, dict) or not isinstance(body.get("file"), dict):
            raise StudioRequestError(422, "invalid_metadata", "file metadata is required")
        file_meta = body["file"]
        filename = str(file_meta.get("name", ""))
        if (
            not filename
            or len(filename) > 255
            or "\x00" in filename
            or "/" in filename
            or "\\" in filename
            or Path(filename).name != filename
        ):
            raise StudioRequestError(422, "invalid_filename", "video filename is invalid")
        suffix = Path(filename).suffix.lower()
        if suffix not in SOURCE_EXTENSIONS:
            raise StudioRequestError(415, "unsupported_extension", "video must be MP4, MOV, M4V, or WEBM")
        declared_type = str(file_meta.get("type", "")).lower()
        if declared_type not in SOURCE_MIME_TYPES[suffix]:
            raise StudioRequestError(415, "mime_mismatch", "declared video type does not match its extension")
        size = file_meta.get("size_bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise StudioRequestError(422, "invalid_size", "video size must be a positive integer")
        if size > self.server.max_import_bytes:
            raise StudioRequestError(413, "video_too_large", "video exceeds the Studio import limit")

        settings = body.get("settings") or {}
        if not isinstance(settings, dict):
            raise StudioRequestError(422, "invalid_settings", "settings must be an object")
        normalized_settings = {
            "source_language": str(settings.get("source_language", "auto")),
            "subtitle_mode": str(settings.get("subtitle_mode", "source")),
            "platform": str(settings.get("platform", "auto")),
            "duration_profile": str(settings.get("duration_profile", "auto")),
            "edit_preset": str(settings.get("edit_preset", "balanced")),
            "director_profile": str(settings.get("director_profile", "teacher-punch")),
            "editing_brief": str(settings.get("editing_brief", "")).strip(),
        }
        if normalized_settings["source_language"] not in SOURCE_LANGUAGES:
            raise StudioRequestError(422, "invalid_settings", "source language is unsupported")
        if normalized_settings["subtitle_mode"] not in SUBTITLE_MODES:
            raise StudioRequestError(422, "invalid_settings", "subtitle mode is unsupported")
        if normalized_settings["platform"] not in PLATFORMS:
            raise StudioRequestError(422, "invalid_settings", "platform is unsupported")
        if normalized_settings["duration_profile"] not in DURATION_PROFILES:
            raise StudioRequestError(422, "invalid_settings", "duration profile is unsupported")
        if normalized_settings["edit_preset"] not in EDIT_PRESETS:
            raise StudioRequestError(422, "invalid_settings", "edit preset is unsupported")
        if normalized_settings["director_profile"] not in DIRECTOR_PROFILES:
            raise StudioRequestError(422, "invalid_settings", "director profile is unsupported")
        if len(normalized_settings["editing_brief"]) > 2000:
            raise StudioRequestError(422, "invalid_settings", "editing brief is too long")

        last_modified = file_meta.get("last_modified_ms")
        if last_modified is not None and (isinstance(last_modified, bool) or not isinstance(last_modified, int)):
            raise StudioRequestError(422, "invalid_metadata", "last_modified_ms must be an integer")
        import_id = f"imp_{secrets.token_hex(16)}"
        created = now_utc()
        session = {
            "id": import_id,
            "state": "awaiting_upload",
            "upload_url": f"/api/imports/{import_id}/content",
            "status_url": f"/api/imports/{import_id}",
            "bytes_expected": size,
            "bytes_received": 0,
            "filename": filename,
            "suffix": suffix,
            "declared_type": declared_type,
            "last_modified_ms": last_modified,
            "project_name": str(body.get("project_name") or Path(filename).stem)[:120],
            "settings": normalized_settings,
            "created_at": created,
            "updated_at": created,
        }
        with self.server.import_lock:
            with self.server.pipeline_lock:
                pipeline_busy = self.server.active_pipeline_project is not None
            if self.server.active_upload_id is not None or pipeline_busy:
                raise StudioRequestError(409, "import_busy", "another video import or pipeline is already active")
            self.server.imports[import_id] = session
            self.server.active_upload_id = import_id
        self.send_json({"ok": True, "import": import_public_payload(session)}, status=201)

    def handle_upload(self, import_id: str) -> None:
        incoming: Path | None = None
        creating: Path | None = None
        finalized = False
        with self.server.import_lock:
            session = self.server.imports.get(import_id)
            if not session:
                self.close_connection = True
                self.send_json({"ok": False, "error": "import session not found"}, status=404)
                return
            if session.get("state") != "awaiting_upload":
                self.close_connection = True
                self.send_json({"ok": False, "error": "import session cannot be reused"}, status=409)
                return
            session["state"] = "uploading"
            session["updated_at"] = now_utc()

        try:
            expected = int(session["bytes_expected"])
            length = self.content_length()
            if length != expected or length > self.server.max_import_bytes:
                self.close_connection = True
                raise StudioRequestError(413, "length_mismatch", "uploaded byte count does not match metadata")
            request_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if request_type not in SOURCE_MIME_TYPES[str(session["suffix"])]:
                self.close_connection = True
                raise StudioRequestError(415, "mime_mismatch", "uploaded video type does not match its extension")
            disk = shutil.disk_usage(self.server.projects_root)
            if disk.free < length + 64 * 1024 * 1024:
                raise StudioRequestError(507, "insufficient_storage", "not enough free disk space for this import")

            incoming_dir = self.server.projects_root / ".incoming"
            if incoming_dir.exists() and incoming_dir.is_symlink():
                raise StudioRequestError(409, "unsafe_workspace", "Studio incoming directory is unsafe")
            incoming_dir.mkdir(exist_ok=True)
            incoming = incoming_dir / f"{import_id}{session['suffix']}"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(incoming, flags, 0o600)
            digest = hashlib.sha256()
            received = 0
            with os.fdopen(descriptor, "wb") as handle:
                while received < length:
                    chunk = self.rfile.read(min(1024 * 1024, length - received))
                    if not chunk:
                        raise StudioRequestError(400, "short_upload", "video upload was truncated")
                    handle.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            session["bytes_received"] = received
            session["state"] = "verifying"
            session["updated_at"] = now_utc()

            if not source_magic_matches(incoming, str(session["suffix"])):
                raise StudioRequestError(415, "invalid_video_magic", "file content is not a supported video container")
            try:
                media = probe_media(incoming)
            except ValueError as exc:
                raise StudioRequestError(415, "invalid_video", "file does not contain a valid video stream") from exc
            width = int(media.get("width") or 0)
            height = int(media.get("height") or 0)
            fps = float(media.get("fps") or 0.0)
            if float(media["duration_s"]) > self.server.max_duration_s:
                raise StudioRequestError(422, "duration_limit", "video duration exceeds the Studio limit")
            if width <= 0 or height <= 0 or width * height > self.server.max_source_pixels:
                raise StudioRequestError(422, "resolution_limit", "video resolution exceeds the Studio limit")
            if not math.isfinite(fps) or fps <= 0 or fps > MAX_SOURCE_FPS:
                raise StudioRequestError(422, "fps_limit", "video frame rate is unsupported")
            if int(media.get("stream_count") or 0) > MAX_STREAMS:
                raise StudioRequestError(422, "stream_limit", "video contains too many media streams")

            checksum = digest.hexdigest()
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%Sz").lower()
            slug = safe_project_slug(str(session["project_name"]))
            project_id = f"{timestamp}-{slug}-{checksum[:8]}-{uuid.uuid4().hex[:4]}"[:80].rstrip("-")
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{8,80}", project_id):
                raise StudioRequestError(500, "project_id_error", "Studio could not allocate a project ID")
            final_project = self.server.projects_root / project_id
            creating = self.server.projects_root / f".creating-{project_id}"
            if final_project.exists() or creating.exists():
                raise StudioRequestError(409, "project_collision", "Studio project ID collision")

            session["state"] = "creating_project"
            session["updated_at"] = now_utc()
            settings = session["settings"]
            initialize_project(
                incoming,
                creating,
                source_language=settings["source_language"],
                subtitle_mode=settings["subtitle_mode"],
                platform=settings["platform"],
                duration_profile=settings["duration_profile"],
                edit_preset=settings["edit_preset"],
                source_mode="move",
                ingest_method="browser_upload",
                original_name=session["filename"],
                browser_last_modified_ms=session.get("last_modified_ms"),
                source_sha256=checksum,
                project_id=project_id,
                manifest_project_dir=final_project,
            )
            incoming = None
            os.replace(creating, final_project)
            finalized = True
            editor_url = self.server.start_editor(final_project)
            pipeline_message = "影片已建立；正在排程本機轉錄與精華提案"
            try:
                self.server.start_local_pipeline(
                    final_project,
                    settings["director_profile"],
                    settings["editing_brief"],
                )
            except OSError as exc:
                self.log_error("local pipeline could not start: %s", exc)
                pipeline_message = "影片已建立；本機自動處理尚未啟動"
            session.update(
                {
                    "state": "ready",
                    "updated_at": now_utc(),
                    "project_id": project_id,
                    "editor_url": editor_url,
                    "message": pipeline_message,
                }
            )
            self.send_json(
                {
                    "ok": True,
                    "import": import_public_payload(session),
                    "project": {"id": project_id},
                    "editor_url": editor_url,
                },
                status=201,
            )
        except StudioRequestError as exc:
            session.update(
                {
                    "state": "failed",
                    "updated_at": now_utc(),
                    "error_code": exc.code,
                    "message": exc.message,
                }
            )
            self.send_problem(exc)
        except (OSError, ValueError) as exc:
            self.log_error("import failed: %s", exc)
            session.update(
                {
                    "state": "failed",
                    "updated_at": now_utc(),
                    "error_code": "import_failed",
                    "message": "Studio could not create the video project",
                }
            )
            self.send_json(
                {"ok": False, "error": session["message"], "error_code": "import_failed"},
                status=500,
            )
        finally:
            if incoming is not None:
                try:
                    incoming.unlink(missing_ok=True)
                except OSError:
                    pass
            if creating is not None and not finalized and creating.exists() and not creating.is_symlink():
                shutil.rmtree(creating, ignore_errors=True)
            with self.server.import_lock:
                if self.server.active_upload_id == import_id:
                    self.server.active_upload_id = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--projects-root", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", dest="open_browser")
    parser.add_argument("--max-import-bytes", type=int, default=DEFAULT_MAX_IMPORT_BYTES)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not is_loopback_host(args.host):
        print("Studio import server is loopback-only", file=sys.stderr)
        return 2
    try:
        server = StudioServer(
            (args.host, args.port),
            Path(args.projects_root),
            max_import_bytes=args.max_import_bytes,
            auto_process=True,
        )
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    url = f"http://{args.host}:{server.server_port}/"
    print(json.dumps({"ok": True, "url": url}, ensure_ascii=False))
    if args.open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
