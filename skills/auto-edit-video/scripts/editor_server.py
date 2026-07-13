#!/usr/bin/env python3
"""Local review/editor server for an auto-edit-video project.

The server binds to loopback by default, serves media with HTTP Range support,
and only reads/writes inside the selected project directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


SKILL_DIR = Path(__file__).resolve().parents[1]
EDITOR_DIR = SKILL_DIR / "editor"
STATE_REL = Path("working/editor_state.json")
ALLOWED_ASSET_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".mov"}
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_ASSET_BYTES = 50 * 1024 * 1024
GATES = {"destructive_edit", "timeline", "final"}
VOICE_LANGUAGES = {"zh-TW", "zh-CN", "en-US", "en-GB"}
VOICE_GENDERS = {"female", "male"}

PLATFORM_PRESETS: dict[str, dict[str, Any]] = {
    "instagram-reels": {
        "label": "Instagram Reels",
        "width": 1080,
        "height": 1920,
        "aspect": "9:16",
        "fps": 30,
        "cover_width": 420,
        "cover_height": 654,
        "basis": "Meta allows 1.91:1–9:16; this is the editor's full-screen preset",
        "safe": {"top": 8, "right": 8, "bottom": 18, "left": 8},
    },
    "youtube-shorts": {
        "label": "YouTube Shorts",
        "width": 1080,
        "height": 1920,
        "aspect": "9:16",
        "fps": 30,
        "cover_width": 1080,
        "cover_height": 1920,
        "basis": "YouTube classifies square or vertical videos up to 3 minutes as Shorts",
        "safe": {"top": 7, "right": 11, "bottom": 17, "left": 7},
    },
    "youtube-landscape": {
        "label": "YouTube 16:9",
        "width": 1920,
        "height": 1080,
        "aspect": "16:9",
        "fps": 30,
        "cover_width": 1280,
        "cover_height": 720,
        "basis": "YouTube's standard desktop aspect ratio is 16:9",
        "safe": {"top": 6, "right": 6, "bottom": 10, "left": 6},
    },
    "tiktok": {
        "label": "TikTok",
        "width": 1080,
        "height": 1920,
        "aspect": "9:16",
        "fps": 30,
        "cover_width": 1080,
        "cover_height": 1920,
        "basis": "TikTok recommends full-screen 9:16 and at least 720p",
        "safe": {"top": 8, "right": 14, "bottom": 20, "left": 8},
    },
    "xiaohongshu-portrait": {
        "label": "小紅書 3:4",
        "width": 1080,
        "height": 1440,
        "aspect": "3:4",
        "fps": 30,
        "cover_width": 1080,
        "cover_height": 1440,
        "basis": "editorial working preset; current public official video-post spec not verified",
        "safe": {"top": 7, "right": 8, "bottom": 14, "left": 8},
    },
    "xiaohongshu-full": {
        "label": "小紅書 9:16",
        "width": 1080,
        "height": 1920,
        "aspect": "9:16",
        "fps": 30,
        "cover_width": 1080,
        "cover_height": 1440,
        "basis": "editorial working preset; current public official video-post spec not verified",
        "safe": {"top": 8, "right": 10, "bottom": 18, "left": 8},
    },
}

DIRECTOR_PRESETS: dict[str, dict[str, Any]] = {
    "teacher-punch": {
        "label": "專業教學",
        "description": "先講結論、拆解步驟，以高對比重點字協助理解。",
        "caption": {
            "font_family": "PingFang TC",
            "font_size": 58,
            "font_weight": 800,
            "color": "#f7f2e8",
            "emphasis_color": "#ffd447",
            "stroke_color": "#17130f",
            "stroke_width": 5,
            "x": 50,
            "y": 76,
            "max_width": 86,
            "animation": "pop",
        },
        "visual_density": "balanced",
    },
    "editorial-clean": {
        "label": "編輯精簡",
        "description": "小而準的字幕、克制字卡，保留人物與畫面呼吸。",
        "caption": {
            "font_family": "Avenir Next",
            "font_size": 44,
            "font_weight": 700,
            "color": "#f6f0e5",
            "emphasis_color": "#e94f37",
            "stroke_color": "#211d19",
            "stroke_width": 3,
            "x": 50,
            "y": 82,
            "max_width": 82,
            "animation": "fade",
        },
        "visual_density": "sparse",
    },
    "documentary": {
        "label": "八卦時事",
        "description": "快速交代背景與衝突，用暖色重點帶出討論焦點。",
        "caption": {
            "font_family": "Songti TC",
            "font_size": 48,
            "font_weight": 700,
            "color": "#f2eadb",
            "emphasis_color": "#d99a52",
            "stroke_color": "#262019",
            "stroke_width": 3,
            "x": 50,
            "y": 80,
            "max_width": 78,
            "animation": "fade",
        },
        "visual_density": "sparse",
    },
    "high-energy": {
        "label": "爆款短影音",
        "description": "強 Hook、大字、快進場，適合密集節奏與高張力片段。",
        "caption": {
            "font_family": "PingFang TC",
            "font_size": 68,
            "font_weight": 900,
            "color": "#fff5e6",
            "emphasis_color": "#ffb000",
            "stroke_color": "#17110d",
            "stroke_width": 6,
            "x": 50,
            "y": 72,
            "max_width": 90,
            "animation": "slide-up",
        },
        "visual_density": "dense",
    },
    "minimal": {
        "label": "POV 藏鏡人",
        "description": "低干擾字幕與沉浸節奏，讓第一視角與現場感主導。",
        "caption": {
            "font_family": "Avenir Next",
            "font_size": 40,
            "font_weight": 600,
            "color": "#f7f3ec",
            "emphasis_color": "#f7f3ec",
            "stroke_color": "#221e1a",
            "stroke_width": 2,
            "x": 50,
            "y": 86,
            "max_width": 76,
            "animation": "none",
        },
        "visual_density": "sparse",
    },
}


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path, fallback: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def editor_state_revision(state: dict[str, Any]) -> str:
    """Hash only fields that can change the rendered video."""
    canvas = state.get("canvas") if isinstance(state.get("canvas"), dict) else {}
    payload = {
        "schema_version": state.get("schema_version"),
        "project_id": state.get("project_id"),
        "canvas": {
            key: canvas.get(key)
            for key in ("platform_id", "width", "height", "fps", "fit")
        },
        "director_style": state.get("director_style"),
        "caption_defaults": state.get("caption_defaults"),
        "overlays": state.get("overlays"),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def project_path(project_dir: Path, relative: str) -> Path:
    candidate = (project_dir / relative).resolve()
    if project_dir.resolve() not in candidate.parents and candidate != project_dir.resolve():
        raise ValueError("path escapes project directory")
    return candidate


def scoped_project_path(project_dir: Path, relative: str, scope: str) -> Path:
    """Resolve a path while keeping it inside one project subdirectory."""
    candidate = project_path(project_dir, relative)
    scope_root = (project_dir / scope).resolve()
    if candidate != scope_root and scope_root not in candidate.parents:
        raise ValueError(f"path escapes {scope}/")
    return candidate


def project_entry_path(project_dir: Path, relative: str) -> Path:
    """Validate a project-relative directory entry without resolving its symlink target."""
    relative_path = Path(relative)
    if relative_path.is_absolute():
        raise ValueError("path must be project-relative")
    root = Path(os.path.abspath(project_dir))
    candidate = Path(os.path.abspath(project_dir / relative_path))
    if root not in candidate.parents and candidate != root:
        raise ValueError("path escapes project directory")
    return candidate


def artifact_plan_overlays(
    project_dir: Path,
    caption_style: dict[str, Any],
    duration_s: float,
) -> list[dict[str, Any]]:
    """Convert reviewed-plan artifacts into editable timeline proposals."""
    overlays: list[dict[str, Any]] = []
    emphasis_plan = read_json(project_dir / "working/emphasis_plan.json", {"items": []}) or {
        "items": []
    }
    for index, item in enumerate(emphasis_plan.get("items", []), start=1):
        try:
            start = max(0.0, float(item.get("start", 0.0)))
            end = min(duration_s, float(item.get("end", start + 0.5)))
        except (TypeError, ValueError):
            continue
        text = str(item.get("text", "")).strip()
        if not text or end <= start:
            continue
        style = dict(caption_style)
        style.update(
            {
                "font_size": max(68, int(float(style.get("font_size", 58))) + 12),
                "color": style.get("emphasis_color", "#ffd447"),
                "y": max(18, float(style.get("y", 76)) - 14),
                "max_width": min(78, float(style.get("max_width", 86))),
                "animation": "pop",
                "box": False,
            }
        )
        overlays.append(
            {
                "id": f"planned-emphasis-{index:04d}",
                "type": "emphasis",
                "start": round(start, 3),
                "end": round(end, 3),
                "text": text,
                "emphasis": [],
                "visible": True,
                "locked": False,
                "z_index": 35,
                "style": style,
                "source": "working/emphasis_plan.json",
                "provenance": str(
                    item.get("provenance")
                    or "local transcript-derived proposal; requires transcript review"
                ),
            }
        )

    visual_plan = read_json(project_dir / "working/visual_plan.json", {"items": []}) or {
        "items": []
    }
    type_map = {"title_card": "title", "data_card": "card", "animation": "animation"}
    for index, item in enumerate(visual_plan.get("items", []), start=1):
        planned_type = str(item.get("type", ""))
        overlay_type = type_map.get(planned_type)
        source = str(item.get("source") or "")
        if planned_type in {"asset", "broll"} and source.startswith("assets/"):
            overlay_type = "video" if Path(source).suffix.lower() in {".mp4", ".mov"} else "image"
        if overlay_type is None:
            continue
        try:
            start = max(0.0, float(item.get("start", 0.0)))
            end = min(duration_s, float(item.get("end", start + 1.5)))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        text = str(item.get("text") or item.get("transcript_evidence") or "").strip()
        if overlay_type not in {"image", "video"} and not text:
            continue
        style = dict(caption_style)
        if overlay_type in {"image", "video"}:
            style.update({"width": 34, "x": 50, "y": 46, "animation": "fade"})
        else:
            style.update(
                {
                    "font_size": max(54, int(float(style.get("font_size", 58)))),
                    "x": 50,
                    "y": 39 if overlay_type == "title" else 46,
                    "max_width": 82,
                    "animation": "slide-up" if overlay_type == "title" else "fade",
                    "box": True,
                    "box_color": "#201b17",
                }
            )
        overlay = {
            "id": f"planned-visual-{index:04d}",
            "type": overlay_type,
            "start": round(start, 3),
            "end": round(end, 3),
            "text": text,
            "emphasis": [],
            "visible": True,
            "locked": False,
            "z_index": 30,
            "style": style,
            "source": source if overlay_type in {"image", "video"} else "working/visual_plan.json",
            "provenance": str(
                item.get("provenance")
                or "local transcript-derived proposal; requires transcript review"
            ),
        }
        overlays.append(overlay)
    return overlays


def default_editor_state(project_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    transcript = read_json(project_dir / "working/transcript_words.json", {}) or {}
    director_id = "teacher-punch"
    director = DIRECTOR_PRESETS[director_id]
    caption_style = dict(director["caption"])
    overlays: list[dict[str, Any]] = []
    for index, segment in enumerate(transcript.get("segments", []), start=1):
        text = str(segment.get("text", "")).strip()
        if not text:
            continue
        overlays.append(
            {
                "id": f"caption-{index:04d}",
                "type": "caption",
                "start": round(float(segment.get("start", 0.0)), 3),
                "end": round(float(segment.get("end", 0.0)), 3),
                "text": text,
                "emphasis": [],
                "visible": True,
                "locked": False,
                "z_index": 20,
                "style": dict(caption_style),
                "source": "working/transcript_words.json",
                "provenance": "local-whisper draft; requires transcript review",
            }
        )
    overlays.extend(
        artifact_plan_overlays(
            project_dir,
            caption_style,
            float(manifest.get("source", {}).get("duration_s", 0.0)),
        )
    )
    state = {
        "schema_version": 1,
        "updated_at": now_utc(),
        "project_id": manifest.get("project_id"),
        "canvas": {
            "platform_id": "instagram-reels",
            "width": 1080,
            "height": 1920,
            "fps": 30,
            "fit": "cover",
            "show_safe_zones": True,
        },
        "director_style": director_id,
        "editing_brief": "",
        "caption_defaults": caption_style,
        "overlays": overlays,
        "publishing": {
            "platform_id": "instagram-reels",
            "draft_status": "not_generated",
            "title": "",
            "body": "",
            "hashtags": [],
            "cover": {
                "time": min(1.0, float(manifest.get("source", {}).get("duration_s", 0.0))),
                "text": "",
                "output": None,
            },
        },
        "review": {
            "selected_overlay_id": overlays[0]["id"] if overlays else None,
            "warnings_acknowledged": [],
        },
    }
    state["revision"] = editor_state_revision(state)
    atomic_write_json(project_dir / STATE_REL, state)
    return state


def validate_editor_state(state: Any, duration_s: float) -> list[str]:
    errors: list[str] = []
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        return ["editor state schema_version must be 1"]
    canvas = state.get("canvas")
    if not isinstance(canvas, dict):
        errors.append("canvas must be an object")
    else:
        platform = canvas.get("platform_id")
        if platform not in PLATFORM_PRESETS:
            errors.append("canvas platform_id is not supported")
        for key in ("width", "height"):
            value = canvas.get(key)
            if not isinstance(value, int) or not 240 <= value <= 4096:
                errors.append(f"canvas {key} must be an integer between 240 and 4096")
        if canvas.get("fit") not in {"cover", "contain"}:
            errors.append("canvas fit must be cover or contain")
    overlays = state.get("overlays")
    if not isinstance(overlays, list):
        errors.append("overlays must be an array")
        return errors
    if len(overlays) > 1000:
        errors.append("overlays cannot exceed 1000 items")
    seen: set[str] = set()
    allowed_types = {"caption", "emphasis", "title", "card", "image", "gif", "video", "animation"}
    for index, overlay in enumerate(overlays):
        if not isinstance(overlay, dict):
            errors.append(f"overlay {index} must be an object")
            continue
        overlay_id = str(overlay.get("id", ""))
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", overlay_id) or overlay_id in seen:
            errors.append(f"overlay {index} has an invalid or duplicate id")
        seen.add(overlay_id)
        if overlay.get("type") not in allowed_types:
            errors.append(f"overlay {overlay_id or index} has an unsupported type")
        try:
            start = float(overlay.get("start"))
            end = float(overlay.get("end"))
            if start < 0 or end <= start or end > duration_s + 0.05:
                errors.append(f"overlay {overlay_id or index} has invalid timing")
        except (TypeError, ValueError):
            errors.append(f"overlay {overlay_id or index} timing must be numeric")
        if len(str(overlay.get("text", ""))) > 1000:
            errors.append(f"overlay {overlay_id or index} text is too long")
        source = overlay.get("source")
        if source and overlay.get("type") in {"image", "gif", "video"}:
            try:
                scoped_project_path(
                    Path(state.get("project_dir", "/")),
                    str(source),
                    "assets",
                )
            except ValueError:
                errors.append(f"overlay {overlay_id or index} source must be under assets/")
    return errors


def transcript_text(project_dir: Path) -> str:
    transcript = read_json(project_dir / "working/transcript_words.json", {}) or {}
    return str(transcript.get("text", "")).strip()


def copy_draft(platform_id: str, text: str) -> dict[str, Any]:
    clean = re.sub(r"\s+", "", text)
    if not clean:
        clean = "這支影片的重點整理"
    title_limits = {
        "instagram-reels": 36,
        "youtube-shorts": 70,
        "youtube-landscape": 70,
        "tiktok": 32,
        "xiaohongshu-portrait": 20,
        "xiaohongshu-full": 20,
    }
    limit = title_limits.get(platform_id, 36)
    title = clean[:limit] + ("…" if len(clean) > limit else "")
    tag_map = {
        "instagram-reels": ["知識短影音", "重點整理", "Reels"],
        "youtube-shorts": ["Shorts", "知識", "重點整理"],
        "youtube-landscape": ["YouTube", "完整解析", "知識"],
        "tiktok": ["TikTok知識", "你知道嗎", "重點"],
        "xiaohongshu-portrait": ["知識分享", "乾貨", "小紅書影片"],
        "xiaohongshu-full": ["知識分享", "乾貨", "小紅書影片"],
    }
    body = f"{title}\n\n影片重點已整理在畫面中。看完後，你最想延伸哪一點？"
    return {
        "title": title,
        "body": body,
        "hashtags": tag_map.get(platform_id, ["短影音", "重點整理"]),
        "draft_status": "local_draft_requires_review",
        "generator": "deterministic-transcript-draft-v1",
    }


def load_voice_catalog() -> dict[str, Any]:
    command = [sys.executable, str(SKILL_DIR / "scripts/auto_edit.py"), "voices"]
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode != 0:
        return {
            "defaults": {},
            "voices": [],
            "warning": (result.stderr or result.stdout or "voice catalog unavailable")[-500:],
        }
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"defaults": {}, "voices": [], "warning": "voice catalog returned invalid JSON"}
    payload["cloud_consent_required"] = True
    payload["selection_only"] = True
    return payload


def voice_language_matches(entry_language: str, selected_language: str) -> bool:
    family = "zh" if selected_language.startswith("zh") else "en"
    entry = entry_language.lower()
    if entry == family:
        return True
    return entry == selected_language.lower()


class EditorServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], project_dir: Path):
        super().__init__(address, EditorHandler)
        self.project_dir = project_dir.resolve()
        self.voice_catalog = load_voice_catalog()
        self.render_lock = threading.Lock()
        self.render_status: dict[str, Any] = {
            "state": "idle",
            "message": "尚未輸出預覽",
            "output": None,
        }


class EditorHandler(BaseHTTPRequestHandler):
    server: EditorServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[editor] " + (fmt % args) + "\n")

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; "
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

    def request_host_allowed(self) -> bool:
        """Reject DNS-rebinding style Host headers on a loopback server."""
        bound_host = str(self.server.server_address[0]).lower()
        if bound_host not in {"127.0.0.1", "localhost", "::1"}:
            return True
        host_header = self.headers.get("Host", "")
        try:
            requested_host = urllib.parse.urlsplit(f"//{host_header}").hostname
        except ValueError:
            return False
        return (requested_host or "").lower() in {"127.0.0.1", "localhost", "::1"}

    def mutation_origin_allowed(self) -> bool:
        """Allow CLI calls without Origin and same-origin browser writes only."""
        if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True
        if origin == "null":
            return False
        try:
            parsed = urllib.parse.urlsplit(origin)
        except ValueError:
            return False
        return (
            parsed.scheme == "http"
            and parsed.netloc.lower() == self.headers.get("Host", "").lower()
        )

    def allow_request(self, mutation: bool = False) -> bool:
        if not self.request_host_allowed():
            self.close_connection = True
            self.send_json({"ok": False, "error": "invalid Host for local editor"}, status=403)
            return False
        if mutation and not self.mutation_origin_allowed():
            self.close_connection = True
            self.send_json({"ok": False, "error": "cross-origin writes are not allowed"}, status=403)
            return False
        return True

    def read_body(self, maximum: int) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0 or length > maximum:
            raise ValueError(f"request body exceeds {maximum} bytes")
        return self.rfile.read(length)

    def read_json_body(self) -> Any:
        raw = self.read_body(MAX_JSON_BYTES)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("request body must be valid UTF-8 JSON") from exc

    def serve_file(self, path: Path, allow_range: bool = False) -> None:
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        size = path.stat().st_size
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        start, end = 0, size - 1
        status = HTTPStatus.OK
        range_header = self.headers.get("Range") if allow_range else None
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match:
                self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                return
            first, last = match.groups()
            if first:
                start = int(first)
                end = int(last) if last else size - 1
            elif last:
                suffix = int(last)
                start = max(0, size - suffix)
            if start >= size or end < start:
                self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                return
            end = min(end, size - 1)
            status = HTTPStatus.PARTIAL_CONTENT
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        if allow_range:
            self.send_header("Accept-Ranges", "bytes")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining -= len(chunk)

    def route(self) -> tuple[str, dict[str, list[str]]]:
        parsed = urllib.parse.urlparse(self.path)
        return parsed.path, urllib.parse.parse_qs(parsed.query)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not self.allow_request():
            return
        path, _query = self.route()
        project = self.server.project_dir
        if path == "/api/health":
            self.send_json({"ok": True, "project": str(project)})
            return
        if path == "/api/project":
            manifest = read_json(project / "project.json", {}) or {}
            state = read_json(project / STATE_REL)
            if state is None:
                state = default_editor_state(project, manifest)
            else:
                revision = editor_state_revision(state)
                if state.get("revision") != revision:
                    state["revision"] = revision
                    atomic_write_json(project / STATE_REL, state)
            source_rel = str(manifest.get("source", {}).get("staged_path", ""))
            payload = {
                "manifest": manifest,
                "state": state,
                "platform_presets": PLATFORM_PRESETS,
                "director_presets": DIRECTOR_PRESETS,
                "voice_catalog": self.server.voice_catalog,
                "edit_candidates": read_json(project / "working/edit_candidates.json", {"items": []}),
                "edit_decisions": read_json(project / "working/edit_decisions.json", {"items": []}),
                "qa": read_json(project / "qa/source-qa.json", {}),
                "media_url": "/media/source" if source_rel else None,
                "render_status": self.server.render_status,
            }
            self.send_json(payload)
            return
        if path == "/api/render-status":
            self.send_json(self.server.render_status)
            return
        if path == "/media/source":
            manifest = read_json(project / "project.json", {}) or {}
            source_rel = str(manifest.get("source", {}).get("staged_path", ""))
            try:
                source = project_entry_path(project, source_rel)
            except ValueError:
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            self.serve_file(source, allow_range=True)
            return
        if path.startswith("/assets/"):
            relative = urllib.parse.unquote(path.removeprefix("/"))
            try:
                asset = scoped_project_path(project, relative, "assets")
            except ValueError:
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            self.serve_file(asset, allow_range=asset.suffix.lower() in {".mp4", ".mov"})
            return
        if path.startswith("/renders/"):
            relative = urllib.parse.unquote(path.removeprefix("/"))
            try:
                render = scoped_project_path(project, relative, "renders")
            except ValueError:
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            self.serve_file(render, allow_range=render.suffix.lower() == ".mp4")
            return
        static_name = "index.html" if path in {"/", "/index.html"} else path.removeprefix("/")
        if "/" in static_name or static_name.startswith("."):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.serve_file(EDITOR_DIR / static_name)

    def do_PUT(self) -> None:
        if not self.allow_request(mutation=True):
            return
        path, _query = self.route()
        if path == "/api/edit-decisions":
            self.handle_edit_decisions()
            return
        if path == "/api/voice-selection":
            self.handle_voice_selection()
            return
        if path != "/api/editor-state":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            state = self.read_json_body()
            manifest = read_json(self.server.project_dir / "project.json", {}) or {}
            duration = float(manifest.get("source", {}).get("duration_s", 0.0))
            state["project_dir"] = str(self.server.project_dir)
            errors = validate_editor_state(state, duration)
            state.pop("project_dir", None)
            if errors:
                self.send_json({"ok": False, "errors": errors}, status=422)
                return
            state["updated_at"] = now_utc()
            state["revision"] = editor_state_revision(state)
            atomic_write_json(self.server.project_dir / STATE_REL, state)
            invalidated_gates: list[str] = []
            approvals = manifest.setdefault("approvals", {})
            for gate in ("timeline", "final"):
                approval = approvals.get(gate)
                if not isinstance(approval, dict) or not approval.get("approved"):
                    continue
                if approval.get("state_revision") == state["revision"]:
                    continue
                approvals[gate] = {
                    "approved": False,
                    "confirmed_by": None,
                    "at": None,
                    "note": "Invalidated because render-affecting editor state changed",
                    "invalidated_at": now_utc(),
                }
                invalidated_gates.append(gate)
            if invalidated_gates:
                manifest["updated_at"] = now_utc()
                atomic_write_json(self.server.project_dir / "project.json", manifest)
        except (ValueError, TypeError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        self.send_json(
            {
                "ok": True,
                "updated_at": state["updated_at"],
                "revision": state["revision"],
                "invalidated_gates": invalidated_gates,
            }
        )

    def handle_voice_selection(self) -> None:
        try:
            body = self.read_json_body()
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        if not isinstance(body, dict):
            self.send_json({"ok": False, "error": "voice selection must be an object"}, status=422)
            return
        enabled = bool(body.get("enabled"))
        manifest_path = self.server.project_dir / "project.json"
        manifest = read_json(manifest_path, {}) or {}
        if not enabled:
            config = {
                "enabled": False,
                "mode": "off",
                "engine": None,
                "provider": None,
                "language": None,
                "gender": None,
                "voice_id": None,
                "speed": 1.0,
                "cloud": False,
                "selection_status": "disabled",
            }
        else:
            language = str(body.get("language", ""))
            gender = str(body.get("gender", ""))
            provider = str(body.get("provider", ""))
            voice_id = str(body.get("voice_id", ""))
            mode = str(body.get("mode", "replace"))
            try:
                speed = float(body.get("speed", 1.0))
            except (TypeError, ValueError):
                speed = 0.0
            if language not in VOICE_LANGUAGES or gender not in VOICE_GENDERS:
                self.send_json({"ok": False, "error": "unsupported voice language or gender"}, status=422)
                return
            if mode not in {"replace", "add"} or not 0.7 <= speed <= 1.3:
                self.send_json({"ok": False, "error": "invalid voice mode or speed"}, status=422)
                return
            entry = next(
                (
                    item
                    for item in self.server.voice_catalog.get("voices", [])
                    if str(item.get("voice_id")) == voice_id
                    and str(item.get("provider")) == provider
                    and str(item.get("gender")) == gender
                    and voice_language_matches(str(item.get("language", "")), language)
                ),
                None,
            )
            if entry is None:
                self.send_json({"ok": False, "error": "voice is not in the allowed shared catalog"}, status=422)
                return
            config = {
                "enabled": True,
                "mode": mode,
                "engine": "rumi-voice-system" if provider == "rumi" else "edge",
                "provider": provider,
                "language": language,
                "gender": gender,
                "voice_id": voice_id,
                "speed": round(speed, 2),
                "cloud": True,
                "cloud_consent_required": True,
                "selection_status": "resolved_not_generated",
            }
        manifest["voiceover"] = config
        manifest["updated_at"] = now_utc()
        atomic_write_json(manifest_path, manifest)
        self.send_json(
            {
                "ok": True,
                "voiceover": config,
                "generated": False,
                "message": "Voice selection saved; no cloud synthesis was called",
            }
        )

    def handle_edit_decisions(self) -> None:
        try:
            body = self.read_json_body()
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        items = body.get("items") if isinstance(body, dict) else None
        if not isinstance(items, list):
            self.send_json({"ok": False, "error": "items must be an array"}, status=422)
            return
        candidates = read_json(
            self.server.project_dir / "working/edit_candidates.json", {"items": []}
        ) or {"items": []}
        allowed = {str(item.get("id")) for item in candidates.get("items", [])}
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                self.send_json({"ok": False, "error": "each decision must be an object"}, status=422)
                return
            candidate_id = str(item.get("candidate_id", ""))
            action = str(item.get("action", ""))
            if candidate_id not in allowed or candidate_id in seen or action not in {"delete", "keep"}:
                self.send_json({"ok": False, "error": "invalid or duplicate edit decision"}, status=422)
                return
            seen.add(candidate_id)
            normalized.append(
                {
                    "candidate_id": candidate_id,
                    "action": action,
                    "review_status": "approved" if body.get("approved") else "pending",
                }
            )
        payload = {
            "schema_version": 1,
            "approved_from": now_utc() if body.get("approved") else None,
            "items": normalized,
        }
        atomic_write_json(self.server.project_dir / "working/edit_decisions.json", payload)
        self.send_json({"ok": True, "items": len(normalized)})

    def do_POST(self) -> None:
        if not self.allow_request(mutation=True):
            return
        path, query = self.route()
        if path == "/api/assets":
            self.handle_asset_upload(query)
            return
        if path == "/api/copy-draft":
            self.handle_copy_draft()
            return
        if path == "/api/approve":
            self.handle_approval()
            return
        if path == "/api/render":
            self.handle_render()
            return
        if path == "/api/cover":
            self.handle_cover()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def handle_asset_upload(self, query: dict[str, list[str]]) -> None:
        filename = (query.get("filename") or [""])[0]
        suffix = Path(filename).suffix.lower()
        if suffix not in ALLOWED_ASSET_EXTENSIONS:
            self.send_json(
                {"ok": False, "error": "asset type must be PNG, JPG, WEBP, GIF, MP4, or MOV"},
                status=415,
            )
            return
        try:
            data = self.read_body(MAX_ASSET_BYTES)
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=413)
            return
        if not data:
            self.send_json({"ok": False, "error": "asset file is empty"}, status=400)
            return
        stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(filename).stem).strip("-")[:36] or "asset"
        stored_name = f"{stem}-{uuid.uuid4().hex[:8]}{suffix}"
        output = self.server.project_dir / "assets" / stored_name
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(data)
        provenance_path = self.server.project_dir / "assets/provenance.json"
        provenance = read_json(provenance_path, {"items": []}) or {"items": []}
        provenance.setdefault("items", []).append(
            {
                "file": f"assets/{stored_name}",
                "original_name": Path(filename).name,
                "source": "user-uploaded-through-local-editor",
                "bytes": len(data),
                "uploaded_at": now_utc(),
            }
        )
        atomic_write_json(provenance_path, provenance)
        self.send_json(
            {
                "ok": True,
                "source": f"assets/{stored_name}",
                "url": f"/assets/{urllib.parse.quote(stored_name)}",
            }
        )

    def handle_copy_draft(self) -> None:
        try:
            body = self.read_json_body()
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        platform_id = str(body.get("platform_id", ""))
        if platform_id not in PLATFORM_PRESETS:
            self.send_json({"ok": False, "error": "unsupported platform"}, status=422)
            return
        draft = copy_draft(platform_id, transcript_text(self.server.project_dir))
        self.send_json({"ok": True, "draft": draft})

    def handle_approval(self) -> None:
        try:
            body = self.read_json_body()
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        gate = str(body.get("gate", ""))
        if gate not in GATES:
            self.send_json({"ok": False, "error": "unsupported approval gate"}, status=422)
            return
        manifest_path = self.server.project_dir / "project.json"
        manifest = read_json(manifest_path, {}) or {}
        approval = {
            "approved": True,
            "confirmed_by": str(body.get("confirmed_by") or "local-editor-user")[:120],
            "at": now_utc(),
            "note": str(body.get("note") or "Approved in local editor")[:500],
        }
        if gate in {"timeline", "final"}:
            state = read_json(self.server.project_dir / STATE_REL, {}) or {}
            approval["state_revision"] = editor_state_revision(state)
        manifest.setdefault("approvals", {})[gate] = approval
        manifest["updated_at"] = now_utc()
        atomic_write_json(manifest_path, manifest)
        self.send_json({"ok": True, "gate": gate, "approval": manifest["approvals"][gate]})

    def handle_render(self) -> None:
        try:
            body = self.read_json_body()
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        quality = str(body.get("quality", "preview"))
        if quality not in {"preview", "final"}:
            self.send_json({"ok": False, "error": "quality must be preview or final"}, status=422)
            return
        if quality == "final":
            manifest = read_json(self.server.project_dir / "project.json", {}) or {}
            state = read_json(self.server.project_dir / STATE_REL, {}) or {}
            approval = manifest.get("approvals", {}).get("timeline", {})
            if (
                not approval.get("approved")
                or approval.get("state_revision") != editor_state_revision(state)
            ):
                self.send_json(
                    {
                        "ok": False,
                        "error": "current timeline revision must be approved before final render",
                    },
                    status=409,
                )
                return
        with self.server.render_lock:
            if self.server.render_status.get("state") == "running":
                self.send_json({"ok": False, "error": "a render is already running"}, status=409)
                return
            self.server.render_status = {
                "state": "running",
                "message": "正在輸出預覽…" if quality == "preview" else "正在輸出最終影片…",
                "quality": quality,
                "output": None,
                "started_at": now_utc(),
            }
        threading.Thread(target=self.render_worker, args=(quality,), daemon=True).start()
        self.send_json({"ok": True, "status": self.server.render_status}, status=202)

    def render_worker(self, quality: str) -> None:
        script = SKILL_DIR / "scripts/render_editor_timeline.py"
        output_name = "editor-preview.mp4" if quality == "preview" else "final.mp4"
        output = self.server.project_dir / "renders" / output_name
        command = [
            sys.executable,
            str(script),
            "--project-dir",
            str(self.server.project_dir),
            "--quality",
            quality,
            "--output",
            str(output),
        ]
        result = subprocess.run(command, text=True, capture_output=True)
        if result.returncode == 0 and output.is_file():
            self.server.render_status = {
                "state": "complete",
                "message": "預覽已完成" if quality == "preview" else "最終影片已完成",
                "quality": quality,
                "output": f"/renders/{output_name}",
                "finished_at": now_utc(),
            }
        else:
            message = (result.stderr or result.stdout or "render failed").strip()[-1200:]
            self.server.render_status = {
                "state": "failed",
                "message": message,
                "quality": quality,
                "output": None,
                "finished_at": now_utc(),
            }

    def handle_cover(self) -> None:
        try:
            body = self.read_json_body()
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        platform_id = str(body.get("platform_id", ""))
        if platform_id not in PLATFORM_PRESETS:
            self.send_json({"ok": False, "error": "unsupported platform"}, status=422)
            return
        try:
            timestamp = max(0.0, float(body.get("time", 0.0)))
        except (TypeError, ValueError):
            self.send_json({"ok": False, "error": "cover time must be numeric"}, status=422)
            return
        text = str(body.get("text", ""))[:200]
        script = SKILL_DIR / "scripts/render_editor_timeline.py"
        output = self.server.project_dir / "renders/cover.png"
        result = subprocess.run(
            [
                sys.executable,
                str(script),
                "--project-dir",
                str(self.server.project_dir),
                "--cover",
                "--platform",
                platform_id,
                "--cover-time",
                str(timestamp),
                "--cover-text",
                text,
                "--output",
                str(output),
            ],
            text=True,
            capture_output=True,
        )
        if result.returncode != 0 or not output.is_file():
            self.send_json(
                {"ok": False, "error": (result.stderr or result.stdout or "cover failed")[-1200:]},
                status=500,
            )
            return
        self.send_json({"ok": True, "output": "/renders/cover.png", "created_at": now_utc()})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", dest="open_browser")
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="Allow a non-loopback bind. Use only on a trusted network.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    project_dir = Path(args.project_dir).expanduser().resolve()
    if not (project_dir / "project.json").is_file():
        print(f"project.json not found under {project_dir}", file=sys.stderr)
        return 2
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not args.allow_remote:
        print("Refusing non-loopback bind without --allow-remote", file=sys.stderr)
        return 2
    if not EDITOR_DIR.is_dir():
        print(f"editor assets missing: {EDITOR_DIR}", file=sys.stderr)
        return 2
    server = EditorServer((args.host, args.port), project_dir)
    url = f"http://{args.host}:{server.server_port}"
    print(json.dumps({"ok": True, "url": url, "project": str(project_dir)}, ensure_ascii=False))
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
