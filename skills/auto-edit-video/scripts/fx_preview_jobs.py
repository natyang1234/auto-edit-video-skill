#!/usr/bin/env python3
"""Project-scoped, fail-closed job manager for short FX preview renders.

The HTTP layer owns authentication and persistence.  This module accepts no
client paths: it derives the source, renderer, cache and output paths from the
server-owned project directory, freezes a validated candidate, and publishes
only a content-addressed MP4 after the worker and source dependencies pass
post-render checks.
"""

from __future__ import annotations

import ast
import fcntl
import hashlib
import importlib.metadata
import json
import math
import os
import platform
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
import fx_preview_contract


JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
OVERLAY_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
TERMINAL_STATES = {"ready", "failed", "cancelled"}
RUNNING_STATES = {"running"}
MAX_REQUEST_VALUES = 32
MAX_VALUE_CHARS = 500
MAX_TOTAL_VALUE_CHARS = 8192
MAX_PREVIEW_BYTES = 256 * 1024 * 1024
MAX_RESULT_JSON_BYTES = 2 * 1024 * 1024
RENDER_TIMEOUT_S = 15 * 60
TERMINAL_TTL_S = 30 * 60
CACHE_IDLE_TTL_S = 24 * 60 * 60
CACHE_MAX_ITEMS = 32
CACHE_MAX_BYTES = 512 * 1024 * 1024
RENDER_CONTRACT_VERSION = "fx-preview-render-v2"
TIMELINE_SCRATCH_RE = re.compile(
    r"^timeline-project-([0-9a-f]{32})-[0-9a-f]{16}-[A-Za-z0-9_-]+$"
)
TIMELINE_LEASE_RE = re.compile(r"^\.timeline-owner-([0-9a-f]{32})\.lock$")
RENDERER_MAINTENANCE_LOCK = ".renderer-maintenance.lock"
RENDERER_TOUCH_RETRY_ATTEMPTS = 6
RENDERER_TOUCH_RETRY_DELAY_S = 0.01
RENDERER_TOUCH_RETRY_BUDGET_S = 0.05
RUNTIME_ARTIFACT_SUFFIXES = ("request.json", "result.json", "part.mp4")

_PHASE_MESSAGES = {
    "queued": "已排入本機預覽工作",
    "preparing": "正在準備來源片段",
    "matte": "正在分析人物畫面",
    "rendering": "正在產生動態特效預覽",
    "composing": "正在準備效果與圖層",
    "encoding": "正在編碼預覽影片",
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


def _read_json_limited(path: Path, maximum: int) -> Any:
    try:
        if path.stat().st_size > maximum:
            return None
    except OSError:
        return None
    return _read_json(path)


_TEXT_LAYOUT_MEASUREMENTS = {"composition-dom", "unavailable", "disabled"}
_TEXT_LAYOUT_STATUSES = {"warn", "pass", "skipped"}
_TEXT_LAYOUT_SCOPES = {"single-fx-and-source-frame", "individual-fx-layout"}
_FACE_MEASUREMENTS = {
    "source-person-mask", "source-opencv-haar", "source-opencv-yunet",
    "source-yunet", "unavailable",
}
_TEXT_FINDINGS = {
    "safe_area", "text_overflow", "subject_overlap", "face_overlap",
    "subject_measurement_unavailable", "dom_measurement_unavailable",
    "composite_subject_measurement_unavailable", "unrecognized_warning",
}
_COMPOSITION_SCOPES = {"full-timeline", "single-effect"}
_COMPOSITION_STATUSES = {"pass", "warn", "unmeasured"}
_COMPOSITION_FINDINGS = {
    "text_subject_overlap",
    "visibility_uncertain",
    "source_subject_unmeasured",
    "ocr_unavailable",
    "reference_unavailable",
    "analysis_incomplete",
    "full_timeline_required",
}
_COMPOSITION_FACE_ENGINES = {"opencv-yunet", "unavailable"}
_COMPOSITION_OCR_ENGINES = {"macos-vision", "unavailable"}


def _bounded_text(value: Any, maximum: int = 160) -> str | None:
    return value[:maximum] if isinstance(value, str) else None


def _bounded_number(
    value: Any, *, minimum: float = -1_000_000, maximum: float = 1_000_000
) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(number) or not minimum <= number <= maximum:
        return None
    return round(number, 6)


def _sanitize_layout_box(value: Any, budget: dict[str, int]) -> dict[str, Any] | None:
    if not isinstance(value, dict) or budget["boxes"] <= 0:
        return None
    output: dict[str, Any] = {}
    for key in ("id", "fontFamily"):
        text_value = _bounded_text(value.get(key), 120 if key == "id" else 160)
        if text_value is not None:
            output[key] = text_value
    for key in (
        "x", "y", "width", "height", "fontSize", "scrollWidth",
        "clientWidth", "scrollHeight", "clientHeight", "at",
    ):
        number = _bounded_number(value.get(key))
        if number is not None:
            output[key] = number
    for key in ("visible", "fitted"):
        if isinstance(value.get(key), bool):
            output[key] = value[key]
    budget["boxes"] -= 1
    return output


def _sanitize_subject_box(value: Any, budget: dict[str, int]) -> dict[str, float] | None:
    if not isinstance(value, dict) or budget["subject_boxes"] <= 0:
        return None
    output: dict[str, float] = {}
    for key in ("x", "y", "width", "height"):
        number = _bounded_number(value.get(key))
        if number is None:
            return None
        output[key] = number
    budget["subject_boxes"] -= 1
    return output


def _sanitize_text_layout(
    value: Any,
    *,
    budget: dict[str, int] | None = None,
    allow_layers: bool = True,
) -> dict[str, Any] | None:
    """Project renderer metadata to a small public/cache-safe v2 receipt."""
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("version") != 2:
        raise ValueError("renderer text_layout is invalid")
    budget = budget or {"boxes": 512, "subject_boxes": 128, "findings": 128, "layers": 64}
    measurement = value.get("measurement")
    status = value.get("status")
    face_measurement = value.get("face_measurement")
    analysis_scope = value.get("analysis_scope")
    output: dict[str, Any] = {
        "version": 2,
        "measurement": measurement if measurement in _TEXT_LAYOUT_MEASUREMENTS else "unavailable",
        "status": status if status in _TEXT_LAYOUT_STATUSES else "skipped",
        "face_measurement": face_measurement if face_measurement in _FACE_MEASUREMENTS else "unavailable",
        "analysis_scope": analysis_scope if analysis_scope in _TEXT_LAYOUT_SCOPES else "individual-fx-layout",
    }
    samples: list[dict[str, Any]] = []
    raw_samples = value.get("samples") if isinstance(value.get("samples"), list) else []
    for sample in raw_samples[:8]:
        if not isinstance(sample, dict):
            continue
        at = _bounded_number(sample.get("at"))
        if at is None:
            continue
        boxes = []
        raw_boxes = sample.get("boxes") if isinstance(sample.get("boxes"), list) else []
        for box in raw_boxes[:64]:
            clean = _sanitize_layout_box(box, budget)
            if clean is not None:
                boxes.append(clean)
        samples.append({"at": at, "boxes": boxes})
    output["samples"] = samples
    boxes = []
    raw_boxes = value.get("boxes") if isinstance(value.get("boxes"), list) else []
    for box in raw_boxes[:320]:
        clean = _sanitize_layout_box(box, budget)
        if clean is not None:
            boxes.append(clean)
    output["boxes"] = boxes
    findings = []
    raw_findings = value.get("findings") if isinstance(value.get("findings"), list) else []
    bounded_findings = raw_findings[:128]
    unknown_or_truncated_finding = len(raw_findings) > len(bounded_findings) or any(
        not isinstance(finding, dict) or finding.get("kind") not in _TEXT_FINDINGS
        for finding in bounded_findings
    )
    if raw_findings and output["status"] == "pass":
        output["status"] = "warn"
    for finding in bounded_findings:
        if not isinstance(finding, dict) or budget["findings"] <= 0:
            continue
        kind = finding.get("kind")
        if kind not in _TEXT_FINDINGS:
            continue
        clean_finding: dict[str, Any] = {"kind": kind}
        for key in ("id", "layer_id"):
            text_value = _bounded_text(finding.get(key), 120)
            if text_value is not None:
                clean_finding[key] = text_value
        at = _bounded_number(finding.get("at"))
        if at is not None:
            clean_finding["at"] = at
        subject_box = _sanitize_subject_box(finding.get("subject_box"), budget)
        if subject_box is not None:
            clean_finding["subject_box"] = subject_box
        findings.append(clean_finding)
        budget["findings"] -= 1
    if unknown_or_truncated_finding and budget["findings"] > 0:
        findings.append({"kind": "unrecognized_warning"})
        budget["findings"] -= 1
    output["findings"] = findings
    subject_samples = []
    raw_subject_samples = value.get("subject_samples") if isinstance(value.get("subject_samples"), list) else []
    for sample in raw_subject_samples[:8]:
        if not isinstance(sample, dict):
            continue
        at = _bounded_number(sample.get("at"))
        source_at = _bounded_number(sample.get("source_at"))
        if at is None or source_at is None:
            continue
        clean_boxes = []
        sample_boxes = sample.get("boxes") if isinstance(sample.get("boxes"), list) else []
        for box in sample_boxes[:16]:
            clean = _sanitize_subject_box(box, budget)
            if clean is not None:
                clean_boxes.append(clean)
        subject_samples.append({"at": at, "source_at": source_at, "boxes": clean_boxes})
    output["subject_samples"] = subject_samples
    if allow_layers and isinstance(value.get("layers"), dict):
        layers: dict[str, Any] = {}
        inspected_layers = 0
        layers_incomplete = False
        for layer_id, report in value["layers"].items():
            if budget["layers"] <= 0:
                break
            inspected_layers += 1
            clean_id = _bounded_text(layer_id, 120)
            if clean_id is None:
                layers_incomplete = True
                continue
            clean_report = _sanitize_text_layout(report, budget=budget, allow_layers=False)
            if clean_report is not None:
                layers[clean_id] = clean_report
                budget["layers"] -= 1
            else:
                layers_incomplete = True
        if inspected_layers < len(value["layers"]):
            layers_incomplete = True
        output["layers"] = layers
        if output["status"] == "pass" and (
            layers_incomplete
            or any(report.get("status") != "pass" for report in layers.values())
        ):
            output["status"] = "warn"
    return output


def _composition_fallback(
    *,
    scope: str,
    source_sha256: str,
    media_sha256: str,
    frame_size: dict[str, int],
) -> dict[str, Any]:
    finding = (
        "full_timeline_required"
        if scope == "single-effect"
        else "analysis_incomplete"
    )
    return {
        "version": 1,
        "scope": scope,
        "measurement": "sampled-local-frames",
        "status": "unmeasured",
        "source_sha256": source_sha256,
        "media_sha256": media_sha256,
        "frame_size": dict(frame_size),
        "sample_count": 0,
        "measured_count": 0,
        "elapsed_s": 0.0,
        "engines": {"faces": "unavailable", "ocr": "unavailable"},
        "samples": [],
        "findings": [{"kind": finding}],
    }


def _composition_source_at(
    output_at: float, time_map: list[dict[str, float]]
) -> float | None:
    for index, segment in enumerate(time_map):
        output_start = float(segment["output_start_s"])
        output_end = float(segment["output_end_s"])
        is_last = index == len(time_map) - 1
        if output_at < output_start - 1e-6:
            continue
        if output_at > output_end + 1e-6 or (
            not is_last and output_at >= output_end - 1e-6
        ):
            continue
        span = output_end - output_start
        if span <= 0:
            return None
        progress_value = min(1.0, max(0.0, (output_at - output_start) / span))
        source_start = float(segment["source_start_s"])
        source_end = float(segment["source_end_s"])
        return source_start + progress_value * (source_end - source_start)
    return None


def _sanitize_composition_box(
    value: Any, frame_size: dict[str, int]
) -> dict[str, float] | None:
    if not isinstance(value, dict) or set(value) != {"x", "y", "width", "height"}:
        return None
    output: dict[str, float] = {}
    for key in ("x", "y", "width", "height"):
        number = _bounded_number(value.get(key), minimum=0, maximum=16384)
        if number is None:
            return None
        output[key] = number
    if (
        output["width"] <= 0
        or output["height"] <= 0
        or output["x"] + output["width"] > frame_size["width"] + 0.5
        or output["y"] + output["height"] > frame_size["height"] + 0.5
    ):
        return None
    return output


def _sanitize_composition_check(
    value: Any,
    *,
    scope: str,
    source_sha256: str,
    media_sha256: str,
    frame_size: dict[str, int],
    render_duration_s: float,
    time_map: list[dict[str, float]],
) -> dict[str, Any]:
    fallback = _composition_fallback(
        scope=scope,
        source_sha256=source_sha256,
        media_sha256=media_sha256,
        frame_size=frame_size,
    )
    top_keys = {
        "version", "scope", "measurement", "status", "source_sha256",
        "media_sha256", "frame_size", "sample_count", "measured_count",
        "elapsed_s", "engines", "samples", "findings",
    }
    if not isinstance(value, dict) or set(value) != top_keys:
        return fallback
    if (
        value.get("version") != 1
        or value.get("scope") != scope
        or value.get("scope") not in _COMPOSITION_SCOPES
        or value.get("measurement") != "sampled-local-frames"
        or value.get("status") not in _COMPOSITION_STATUSES
        or value.get("source_sha256") != source_sha256
        or value.get("media_sha256") != media_sha256
    ):
        return fallback
    raw_size = value.get("frame_size")
    if (
        not isinstance(raw_size, dict)
        or set(raw_size) != {"width", "height"}
        or any(
            isinstance(raw_size.get(key), bool)
            or not isinstance(raw_size.get(key), int)
            or not 0 < raw_size[key] <= 16384
            for key in ("width", "height")
        )
        or raw_size != frame_size
    ):
        return fallback
    sample_count = value.get("sample_count")
    measured_count = value.get("measured_count")
    elapsed = _bounded_number(value.get("elapsed_s"), minimum=0, maximum=60)
    raw_samples = value.get("samples")
    raw_findings = value.get("findings")
    if (
        isinstance(sample_count, bool)
        or not isinstance(sample_count, int)
        or not 0 <= sample_count <= 8
        or isinstance(measured_count, bool)
        or not isinstance(measured_count, int)
        or not 0 <= measured_count <= sample_count
        or elapsed is None
        or not isinstance(raw_samples, list)
        or len(raw_samples) != sample_count
        or not isinstance(raw_findings, list)
        or len(raw_findings) > 32
    ):
        return fallback
    engines = value.get("engines")
    if (
        not isinstance(engines, dict)
        or set(engines) != {"faces", "ocr"}
        or engines.get("faces") not in _COMPOSITION_FACE_ENGINES
        or engines.get("ocr") not in _COMPOSITION_OCR_ENGINES
    ):
        return fallback
    samples: list[dict[str, Any]] = []
    declared_measured_seen = 0
    semantic_unmeasured = False
    sample_keys = {
        "output_at", "source_at", "measured", "source_face_count",
        "reference_face_count", "visible_face_count", "text_box_count",
    }
    for sample in raw_samples:
        if not isinstance(sample, dict) or set(sample) != sample_keys:
            return fallback
        output_at = _bounded_number(
            sample.get("output_at"), minimum=0, maximum=render_duration_s
        )
        source_at = _bounded_number(sample.get("source_at"), minimum=0)
        if output_at is None or source_at is None:
            return fallback
        mapped_source = _composition_source_at(output_at, time_map)
        if mapped_source is None or abs(mapped_source - source_at) > 0.03:
            return fallback
        measured = sample.get("measured")
        if not isinstance(measured, bool):
            return fallback
        counts: dict[str, int] = {}
        for key in (
            "source_face_count", "reference_face_count",
            "visible_face_count", "text_box_count",
        ):
            count = sample.get(key)
            if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 10000:
                return fallback
            counts[key] = count
        declared_measured_seen += int(measured)
        if measured and any(
            counts[key] <= 0
            for key in (
                "source_face_count", "reference_face_count", "visible_face_count"
            )
        ):
            measured = False
            semantic_unmeasured = True
        samples.append(
            {
                "output_at": output_at,
                "source_at": source_at,
                "measured": measured,
                **counts,
            }
        )
    if declared_measured_seen != measured_count:
        return fallback
    findings: list[dict[str, Any]] = []
    finding_keys = {"kind", "output_at", "source_at", "subject_box", "text_box"}
    for finding in raw_findings:
        if (
            not isinstance(finding, dict)
            or not set(finding).issubset(finding_keys)
            or finding.get("kind") not in _COMPOSITION_FINDINGS
        ):
            return fallback
        has_output = "output_at" in finding
        has_source = "source_at" in finding
        if has_output != has_source:
            return fallback
        clean: dict[str, Any] = {"kind": finding["kind"]}
        if has_output:
            output_at = _bounded_number(
                finding.get("output_at"), minimum=0, maximum=render_duration_s
            )
            source_at = _bounded_number(finding.get("source_at"), minimum=0)
            if output_at is None or source_at is None:
                return fallback
            mapped_source = _composition_source_at(output_at, time_map)
            if mapped_source is None or abs(mapped_source - source_at) > 0.03:
                return fallback
            clean.update({"output_at": output_at, "source_at": source_at})
        for key in ("subject_box", "text_box"):
            if key in finding:
                box = _sanitize_composition_box(finding.get(key), frame_size)
                if box is None:
                    return fallback
                clean[key] = box
        if finding["kind"] == "text_subject_overlap" and not {
            "output_at", "source_at", "subject_box", "text_box"
        }.issubset(clean):
            return fallback
        findings.append(clean)
    sanitized_measured_count = sum(int(sample["measured"]) for sample in samples)
    capability_findings = {
        "visibility_uncertain",
        "source_subject_unmeasured",
        "ocr_unavailable",
        "reference_unavailable",
        "analysis_incomplete",
        "full_timeline_required",
    }
    status = value["status"]
    incomplete = sanitized_measured_count != sample_count
    unavailable_engine = engines != {
        "faces": "opencv-yunet", "ocr": "macos-vision"
    }
    has_capability_finding = any(
        finding["kind"] in capability_findings for finding in findings
    )
    legitimate_warn = bool(
        sample_count
        and not incomplete
        and not unavailable_engine
        and findings
        and all(finding["kind"] == "text_subject_overlap" for finding in findings)
    )
    if status == "pass" and (
        scope != "full-timeline"
        or sample_count == 0
        or incomplete
        or findings
        or unavailable_engine
    ):
        status = "unmeasured"
        semantic_unmeasured = True
    elif status == "warn" and not legitimate_warn:
        status = "unmeasured"
        semantic_unmeasured = True
    elif status == "unmeasured":
        semantic_unmeasured = True
    if incomplete or unavailable_engine or has_capability_finding:
        status = "unmeasured"
        semantic_unmeasured = True
    if semantic_unmeasured and not findings:
        findings.append({"kind": "analysis_incomplete"})
    return {
        "version": 1,
        "scope": scope,
        "measurement": "sampled-local-frames",
        "status": status,
        "source_sha256": source_sha256,
        "media_sha256": media_sha256,
        "frame_size": dict(frame_size),
        "sample_count": sample_count,
        "measured_count": sanitized_measured_count,
        "elapsed_s": elapsed,
        "engines": dict(engines),
        "samples": samples,
        "findings": findings,
    }


def _regular_nonsymlink(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and not path.is_symlink()


class _PinnedDirectory:
    """An opened directory whose pathname identity must never drift."""

    def __init__(self, path: Path, label: str):
        self.path = path
        self.label = label
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            self.fd = os.open(path, flags)
            info = os.fstat(self.fd)
        except OSError as exc:
            raise ValueError(f"{label} directory is unsafe") from exc
        if not stat.S_ISDIR(info.st_mode):
            os.close(self.fd)
            raise ValueError(f"{label} is not a directory")
        self.identity = (info.st_dev, info.st_ino)

    def verify(self) -> None:
        try:
            opened = os.fstat(self.fd)
            named = self.path.lstat()
        except OSError as exc:
            raise FxPreviewJobError(409, "preview_storage_changed", "FX 預覽工作目錄已變更") from exc
        if (
            self.path.is_symlink()
            or not stat.S_ISDIR(named.st_mode)
            or (opened.st_dev, opened.st_ino) != self.identity
            or (named.st_dev, named.st_ino) != self.identity
        ):
            raise FxPreviewJobError(409, "preview_storage_changed", "FX 預覽工作目錄已變更")

    def close(self) -> None:
        if self.fd < 0:
            return
        try:
            os.close(self.fd)
        except OSError:
            pass
        finally:
            self.fd = -1


class FxPreviewJobManager:
    """Own one project's preview jobs, worker process and private cache."""

    def __init__(
        self,
        project_dir: Path,
        *,
        context_provider: Callable[[], dict[str, Any]],
        timeline_snapshot_builder: Callable[
            [dict[str, Any], dict[str, Any], dict[str, Any], str | None],
            dict[str, Any],
        ] | None = None,
        renderer_script: Path | None = None,
        artifact_validator: Callable[[Path, float], dict[str, Any]] | None = None,
        render_timeout_s: float = RENDER_TIMEOUT_S,
    ) -> None:
        self.project_dir = project_dir.resolve()
        self.context_provider = context_provider
        self.timeline_snapshot_builder = timeline_snapshot_builder
        self.renderer_script = (
            renderer_script
            if renderer_script is not None
            else Path(__file__).with_name("fx_preview_render.py")
        ).resolve()
        self.artifact_validator = artifact_validator or self._validate_mp4
        self.render_timeout_s = float(render_timeout_s)
        self._lock = threading.RLock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._timing_samples: dict[tuple[str, str, str, float, float], list[float]] = {}
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
        self._storage_pins = {
            "working": _PinnedDirectory(working, "working"),
            "root": _PinnedDirectory(self.root, "FX preview root"),
            "cache": _PinnedDirectory(self.cache_dir, "FX preview cache"),
            "renderer": _PinnedDirectory(self.renderer_cache_dir, "FX renderer cache"),
            "runtime": _PinnedDirectory(self.runtime_dir, "FX preview runtime"),
        }
        self._cleanup_orphan_partials()
        self._cleanup_renderer_scratch()

    def _assert_storage_safe(self, *names: str) -> None:
        selected = names or tuple(self._storage_pins)
        for name in selected:
            self._storage_pins[name].verify()

    @staticmethod
    def _scratch_lease_name(owner: str) -> str:
        if JOB_ID_RE.fullmatch(owner) is None:
            raise OSError("unsafe renderer scratch owner")
        return f".timeline-owner-{owner}.lock"

    def _try_acquire_scratch_lease(self, owner: str, *, create: bool) -> int | None:
        """Acquire one pinned per-job lease, or return None when not provably idle."""
        self._assert_storage_safe("working", "root", "renderer")
        name = self._scratch_lease_name(owner)
        flags = os.O_RDWR | (os.O_CREAT if create else 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(
                name,
                flags,
                0o600,
                dir_fd=self._storage_pins["renderer"].fd,
            )
        except (FileNotFoundError, OSError):
            if create:
                raise
            return None
        try:
            opened = os.fstat(descriptor)
            named = os.stat(
                name,
                dir_fd=self._storage_pins["renderer"].fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            ):
                raise OSError("unsafe renderer scratch lease")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                os.close(descriptor)
                return None
            return descriptor
        except Exception:
            os.close(descriptor)
            if create:
                raise
            return None

    def _unlink_scratch_lease_if_same(self, owner: str, descriptor: int) -> bool:
        """Unlink only the still-named regular inode held by this cleanup pass."""
        name = self._scratch_lease_name(owner)
        try:
            opened = os.fstat(descriptor)
            named = os.stat(
                name,
                dir_fd=self._storage_pins["renderer"].fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            ):
                return False
            os.unlink(name, dir_fd=self._storage_pins["renderer"].fd)
            return True
        except OSError:
            return False

    def _acquire_renderer_maintenance_lock(self, *, exclusive: bool) -> int | None:
        """Take the fixed renderer guard; exclusive callers skip while a worker is live."""
        self._assert_storage_safe("working", "root", "renderer")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = -1
        try:
            descriptor = os.open(
                RENDERER_MAINTENANCE_LOCK,
                flags,
                0o600,
                dir_fd=self._storage_pins["renderer"].fd,
            )
            opened = os.fstat(descriptor)
            named = os.stat(
                RENDERER_MAINTENANCE_LOCK,
                dir_fd=self._storage_pins["renderer"].fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            ):
                raise OSError("unsafe renderer maintenance lock")
            operation = fcntl.LOCK_EX | fcntl.LOCK_NB if exclusive else fcntl.LOCK_SH
            try:
                fcntl.flock(descriptor, operation)
            except (BlockingIOError, OSError):
                os.close(descriptor)
                return None
            return descriptor
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
            if exclusive:
                return None
            raise

    @staticmethod
    def _open_regular_at(directory_fd: int, name: str) -> tuple[int, os.stat_result]:
        if Path(name).name != name:
            raise OSError("unsafe preview entry name")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(name, flags, dir_fd=directory_fd)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            os.close(descriptor)
            raise OSError("preview entry is not an owned regular file")
        return descriptor, info

    @classmethod
    def _sha256_at(cls, directory_fd: int, name: str) -> tuple[str, os.stat_result]:
        descriptor, info = cls._open_regular_at(directory_fd, name)
        digest = hashlib.sha256()
        try:
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(descriptor)
            if (
                after.st_dev, after.st_ino, after.st_size,
                after.st_mtime_ns, after.st_ctime_ns,
            ) != (
                info.st_dev, info.st_ino, info.st_size,
                info.st_mtime_ns, info.st_ctime_ns,
            ):
                raise OSError("preview entry changed while hashing")
        finally:
            os.close(descriptor)
        return digest.hexdigest(), info

    @classmethod
    def _read_json_at(cls, directory_fd: int, name: str, max_bytes: int) -> Any:
        descriptor, info = cls._open_regular_at(directory_fd, name)
        try:
            if info.st_size < 0 or info.st_size > max_bytes:
                raise ValueError("preview JSON is too large")
            data = b""
            while len(data) <= max_bytes:
                chunk = os.read(descriptor, min(65536, max_bytes + 1 - len(data)))
                if not chunk:
                    break
                data += chunk
            after = os.fstat(descriptor)
            if len(data) > max_bytes or (
                after.st_dev, after.st_ino, after.st_size,
                after.st_mtime_ns, after.st_ctime_ns,
            ) != (
                info.st_dev, info.st_ino, info.st_size,
                info.st_mtime_ns, info.st_ctime_ns,
            ):
                raise ValueError("preview JSON changed while reading")
            return json.loads(data.decode("utf-8"))
        finally:
            os.close(descriptor)

    @staticmethod
    def _remove_tree_at(parent_fd: int, name: str) -> None:
        """Remove one tree without ever following a symlink or renamed root."""
        if Path(name).name != name:
            raise OSError("unsafe preview entry name")
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode):
            os.unlink(name, dir_fd=parent_fd)
            return
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        child_fd = os.open(name, flags, dir_fd=parent_fd)
        try:
            opened = os.fstat(child_fd)
            if not stat.S_ISDIR(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise OSError("preview directory changed while opening")
            for entry in list(os.scandir(child_fd)):
                FxPreviewJobManager._remove_tree_at(child_fd, entry.name)
        finally:
            os.close(child_fd)
        os.rmdir(name, dir_fd=parent_fd)

    @staticmethod
    def _walk_files_at(
        directory_fd: int,
        callback: Callable[[int, str, os.stat_result, tuple[str, ...]], None],
        prefix: tuple[str, ...] = (),
    ) -> None:
        """Visit regular files under an opened directory without following links."""
        for entry in list(os.scandir(directory_fd)):
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                callback(directory_fd, entry.name, info, prefix + (entry.name,))
                continue
            if not stat.S_ISDIR(info.st_mode):
                continue
            flags = os.O_RDONLY
            if hasattr(os, "O_DIRECTORY"):
                flags |= os.O_DIRECTORY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                child_fd = os.open(entry.name, flags, dir_fd=directory_fd)
            except OSError:
                continue
            try:
                opened = os.fstat(child_fd)
                if (opened.st_dev, opened.st_ino) == (info.st_dev, info.st_ino):
                    FxPreviewJobManager._walk_files_at(
                        child_fd, callback, prefix + (entry.name,)
                    )
            finally:
                os.close(child_fd)

    @staticmethod
    def _unlink_relative_at(root_fd: int, parts: tuple[str, ...]) -> None:
        if not parts or any(Path(part).name != part for part in parts):
            raise OSError("unsafe preview relative path")
        current_fd = os.dup(root_fd)
        try:
            flags = os.O_RDONLY
            if hasattr(os, "O_DIRECTORY"):
                flags |= os.O_DIRECTORY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            for part in parts[:-1]:
                next_fd = os.open(part, flags, dir_fd=current_fd)
                os.close(current_fd)
                current_fd = next_fd
            os.unlink(parts[-1], dir_fd=current_fd)
        finally:
            os.close(current_fd)

    @staticmethod
    def _remove_empty_dirs_at(directory_fd: int) -> None:
        for entry in list(os.scandir(directory_fd)):
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISDIR(info.st_mode):
                continue
            flags = os.O_RDONLY
            if hasattr(os, "O_DIRECTORY"):
                flags |= os.O_DIRECTORY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                child_fd = os.open(entry.name, flags, dir_fd=directory_fd)
            except OSError:
                continue
            try:
                FxPreviewJobManager._remove_empty_dirs_at(child_fd)
            finally:
                os.close(child_fd)
            try:
                os.rmdir(entry.name, dir_fd=directory_fd)
            except OSError:
                pass

    def _atomic_json_in(self, directory_name: str, name: str, payload: Any) -> str:
        if Path(name).name != name:
            raise FxPreviewJobError(409, "preview_storage_changed", "FX 預覽檔名不安全")
        self._assert_storage_safe("working", "root", directory_name)
        pin = self._storage_pins[directory_name]
        temporary = f".{name}.{secrets.token_hex(8)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = -1
        try:
            descriptor = os.open(temporary, flags, 0o600, dir_fd=pin.fd)
            data = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, allow_nan=False
            ).encode("utf-8")
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            self._assert_storage_safe("working", "root", directory_name)
            os.rename(
                temporary,
                name,
                src_dir_fd=pin.fd,
                dst_dir_fd=pin.fd,
            )
            return hashlib.sha256(data).hexdigest()
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=pin.fd)
            except FileNotFoundError:
                pass

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

    @staticmethod
    def _comparison_anchor_id(body: Any) -> str | None:
        if not isinstance(body, dict) or "comparison_anchor_job_id" not in body:
            return None
        anchor_id = body.get("comparison_anchor_job_id")
        if not isinstance(anchor_id, str) or JOB_ID_RE.fullmatch(anchor_id) is None:
            raise FxPreviewJobError(
                422,
                "comparison_anchor_invalid",
                "比較錨點工作識別碼無效",
            )
        return anchor_id

    @staticmethod
    def _comparison_anchor_usable(job: dict[str, Any] | None) -> bool:
        if job is None or job.get("state") == "cancelled":
            return False
        error = job.get("error")
        return not (
            job.get("state") == "failed"
            and isinstance(error, dict)
            and error.get("code") == "preview_stale"
        )

    def _normalize_candidate(
        self, body: Any, context: dict[str, Any]
    ) -> tuple[
        dict[str, Any], str | None, dict[str, Any], dict[str, float],
        dict[str, str], dict[str, Any], dict[str, Any], dict[str, Any] | None,
    ]:
        if not isinstance(body, dict):
            raise FxPreviewJobError(400, "malformed_request", "請求內容必須是 JSON 物件")
        self._validate_keys(
            body,
            {
                "schema_version", "expected_revision", "candidate",
                "replace_overlay_id", "preview_options", "opacity",
                "comparison_anchor_job_id",
            },
            {"schema_version", "expected_revision", "candidate"},
            "請求",
        )
        if body.get("schema_version") != 1:
            raise FxPreviewJobError(400, "unsupported_schema", "不支援的 FX 預覽 API 版本")
        self._comparison_anchor_id(body)
        try:
            preview_options = fx_preview_contract.normalize_preview_options(
                body.get("preview_options")
            )
            opacity = (
                fx_preview_contract.normalize_fx_opacity(body["opacity"])
                if "opacity" in body
                else None
            )
        except fx_preview_contract.FxPreviewContractError as exc:
            raise FxPreviewJobError(422, exc.code, exc.message) from exc
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
            {
                "code", "window", "apply_window", "values", "placement",
                "scheme", "font_ids", "subject", "text_fit", "material",
            },
            {"code", "window", "values", "placement", "scheme", "font_ids"},
            "candidate",
        )
        code = candidate.get("code")
        if not isinstance(code, str) or re.fullmatch(r"[A-Z][0-9]{1,3}", code) is None:
            raise FxPreviewJobError(422, "effect_invalid", "FX 效果代碼無效")
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

        source_identity = self._source_identity(context.get("manifest") or {})
        try:
            sample_window, apply_window = fx_preview_contract.normalize_sample_window(
                candidate.get("window"),
                candidate.get("apply_window"),
                source_identity["duration_s"],
            )
        except fx_preview_contract.FxPreviewContractError as exc:
            raise FxPreviewJobError(422, exc.code, exc.message) from exc
        start = apply_window["start_s"]
        duration = apply_window["duration_s"]
        media_max = effect.get("media_max")
        if (
            code != "B1"
            and isinstance(media_max, (int, float))
            and not isinstance(media_max, bool)
            and duration > float(media_max) + 1e-6
        ):
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

        text_fit = candidate.get("text_fit", True)
        if not isinstance(text_fit, bool):
            raise FxPreviewJobError(422, "text_fit_invalid", "text_fit 必須是布林值")
        subject = candidate.get("subject")
        if code != "P5" and "subject" in candidate:
            raise FxPreviewJobError(422, "subject_invalid", "只有 P5 可以指定主角位置")
        if code == "P5" and subject is not None:
            if not isinstance(subject, dict) or set(subject) != {"x", "y"}:
                raise FxPreviewJobError(422, "subject_invalid", "主角位置必須包含 x 與 y")
            normalized_subject: dict[str, float] = {}
            for axis in ("x", "y"):
                value = subject.get(axis)
                try:
                    numeric_value = float(value)
                except (OverflowError, TypeError, ValueError):
                    numeric_value = math.nan
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(numeric_value)
                    or not 0.0 <= numeric_value <= 1.0
                ):
                    raise FxPreviewJobError(422, "subject_invalid", "主角位置必須介於 0 到 1")
                normalized_subject[axis] = round(numeric_value, 6)
            subject = normalized_subject

        material_binding: dict[str, str] | None = None
        if code == "B1":
            if "material" not in candidate:
                raise FxPreviewJobError(422, "material_required", "B1 需要先選擇本專案素材")
            try:
                material_binding = fx_preview_contract.resolve_material_binding(
                    self.project_dir, candidate.get("material")
                )
            except fx_preview_contract.FxPreviewContractError as exc:
                raise FxPreviewJobError(422, exc.code, exc.message) from exc
        elif "material" in candidate:
            raise FxPreviewJobError(422, "material_invalid", "只有 B1 可以綁定專案素材")

        candidate_fields = {
            "code": code,
            "start": start,
            "end": round(start + duration, 3),
            "values": normalized_values,
            "placement": placement,
            "scheme": scheme,
            "font_ids": dict(sorted(font_ids.items())),
            "text_fit": text_fit,
            "text": str(effect.get("name") or code)[:200],
            "provenance": "FX dynamic preview confirmation",
        }
        if code == "P5":
            candidate_fields["subject"] = subject
        if material_binding is not None:
            candidate_fields["material"] = material_binding
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
        style = overlay.get("style") if isinstance(overlay.get("style"), dict) else {}
        try:
            normalized_opacity = (
                fx_preview_contract.normalize_fx_opacity(style.get("opacity", 1.0))
                if opacity is None
                else opacity
            )
        except fx_preview_contract.FxPreviewContractError as exc:
            raise FxPreviewJobError(422, exc.code, exc.message) from exc
        overlay["style"] = {
            **style,
            "opacity": normalized_opacity,
        }
        errors = fx_overlay.validate(overlay, state)
        if errors:
            raise FxPreviewJobError(422, "candidate_invalid", "FX candidate 未通過伺服器驗證")
        state_snapshot = deepcopy(state)
        manifest_snapshot = deepcopy(context.get("manifest") or {})
        timeline_snapshot = None
        if self.timeline_snapshot_builder is not None:
            timeline_snapshot = self.timeline_snapshot_builder(
                state_snapshot,
                manifest_snapshot,
                overlay,
                replace_overlay_id,
            )
            frozen_state = timeline_snapshot.get("state")
            frozen_manifest = timeline_snapshot.get("manifest")
            if not isinstance(frozen_state, dict) or not isinstance(frozen_manifest, dict):
                raise FxPreviewJobError(
                    500, "snapshot_invalid", "無法建立完整時間軸預覽快照"
                )
            state_snapshot = deepcopy(frozen_state)
            manifest_snapshot = deepcopy(frozen_manifest)
        return (
            overlay,
            replace_overlay_id,
            source_identity,
            sample_window,
            preview_options,
            state_snapshot,
            manifest_snapshot,
            timeline_snapshot,
        )

    def _dependency_snapshot(
        self,
        context: dict[str, Any],
        overlay: dict[str, Any],
        source_identity: dict[str, Any] | None = None,
        *,
        preview_options: dict[str, str] | None = None,
        frozen_state: dict[str, Any] | None = None,
        frozen_manifest: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        options = fx_preview_contract.normalize_preview_options(preview_options)
        state = (
            frozen_state
            if options["mode"] == "timeline" and isinstance(frozen_state, dict)
            else context.get("state") if isinstance(context.get("state"), dict) else {}
        )
        manifest = (
            frozen_manifest
            if options["mode"] == "timeline" and isinstance(frozen_manifest, dict)
            else context.get("manifest") if isinstance(context.get("manifest"), dict) else {}
        )
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
            "fx_preview_contract": Path(fx_preview_contract.__file__).resolve(),
            "text_fit": Path(__file__).with_name("text_fit.py").resolve(),
            "spotlight_track": skill / "tools/spotlight_track.py",
            "renderer": self.renderer_script,
            "yunet_model": Path(__file__).resolve().parents[1]
            / "assets/models/yunet/face_detection_yunet_2026may.onnx",
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
        material = None
        if overlay.get("code") == "B1":
            try:
                material = fx_preview_contract.verify_material_binding(
                    self.project_dir, overlay.get("material")
                )
            except fx_preview_contract.FxPreviewContractError as exc:
                raise FxPreviewJobError(409, exc.code, exc.message) from exc
        snapshot = {
            "contract": RENDER_CONTRACT_VERSION,
            "project_namespace": hashlib.sha256(str(self.project_dir).encode("utf-8")).hexdigest(),
            "state_revision": self._state_revision(context),
            "source": {key: source[key] for key in ("relative", "sha256", "bytes")},
            "pack_id": pack_id,
            "theme": theme,
            "main": main,
            "canvas": canvas,
            # fonts_digest_for historically includes the overlay id.  Pin a
            # synthetic visual identity so add/replace of the same pixels can
            # share cache while font ids and their verified bytes still bind it.
            "fonts_digest": fx_overlay.fonts_digest_for(
                {
                    "overlays": [
                        {
                            "id": "fx-preview-visual",
                            "type": "fx",
                            "font_ids": deepcopy(overlay.get("font_ids") or {}),
                        }
                    ]
                }
            ),
            "preview_options": options,
            "material": material,
            "dependencies": digests,
            "tools": self._tool_identities(),
            "composition_analysis": self._composition_analysis_identities(),
        }
        if options["mode"] == "timeline":
            snapshot["state_snapshot"] = deepcopy(state)
            snapshot["manifest_snapshot"] = deepcopy(manifest)
            asset_digests = state.get("asset_digests")
            snapshot["asset_digests"] = (
                deepcopy(asset_digests) if isinstance(asset_digests, dict) else {}
            )
            try:
                snapshot["provenance_digests"] = (
                    fx_preview_contract.material_authority_digests(
                        self.project_dir, state
                    )
                )
            except fx_preview_contract.FxPreviewContractError as exc:
                raise FxPreviewJobError(409, exc.code, exc.message) from exc
            snapshot["timeline_project_inputs"] = self._timeline_project_input_digests()
            snapshot["timeline_python_dependencies"] = self._local_python_dependency_digests(
                [Path(__file__).with_name("render_editor_timeline.py")]
            )
            snapshot["timeline_fx_blocks"] = self._timeline_fx_block_digests(
                state, overlay, skill
            )
            snapshot["timeline_fonts_digest"] = fx_overlay.fonts_digest_for(state)
        return snapshot

    def _comparison_fingerprint(
        self,
        context: dict[str, Any],
        source_identity: dict[str, Any],
        sample_window: dict[str, float],
        overlay: dict[str, Any],
        replace_overlay_id: str | None,
        preview_options: dict[str, str],
        dependencies: dict[str, Any],
    ) -> dict[str, Any]:
        """Freeze only inputs that must be identical across one comparison round.

        The candidate overlay, its effect block, fonts, subject and material are
        intentionally absent.  Timeline mode also uses the raw editor context,
        not the candidate-injected renderer snapshot.
        """
        dependency_digests = dependencies.get("dependencies")
        common_dependency_digests = {
            key: value
            for key, value in (
                dependency_digests.items()
                if isinstance(dependency_digests, dict)
                else ()
            )
            if key != "block"
        }
        common_renderer: dict[str, Any] = {
            key: deepcopy(dependencies[key])
            for key in (
                "contract",
                "project_namespace",
                "pack_id",
                "theme",
                "main",
                "canvas",
                "timeline_project_inputs",
                "timeline_python_dependencies",
                "composition_analysis",
            )
            if key in dependencies
        }
        common_renderer["dependencies"] = common_dependency_digests
        common_renderer["tools"] = deepcopy(dependencies.get("tools") or {})
        if preview_options.get("mode") == "timeline":
            base_state = deepcopy(
                context.get("state")
                if isinstance(context.get("state"), dict)
                else {}
            )
            overlays = base_state.get("overlays")
            if isinstance(overlays, list) and replace_overlay_id is not None:
                base_state["overlays"] = [
                    item
                    for item in overlays
                    if not (
                        isinstance(item, dict)
                        and item.get("type") == "fx"
                        and item.get("id") == replace_overlay_id
                    )
                ]
            base_codes = {
                str(item.get("code"))
                for item in base_state.get("overlays", [])
                if isinstance(item, dict)
                and item.get("type") == "fx"
                and isinstance(item.get("code"), str)
            }
            rendered_blocks = dependencies.get("timeline_fx_blocks")
            if not isinstance(rendered_blocks, dict) or any(
                code not in rendered_blocks for code in base_codes
            ):
                raise FxPreviewJobError(
                    503,
                    "renderer_unavailable",
                    "時間軸 FX 元件依賴不完整",
                )
            common_renderer["timeline_fx_blocks"] = {
                code: rendered_blocks[code] for code in sorted(base_codes)
            }
            common_renderer["timeline_fonts_digest"] = fx_overlay.fonts_digest_for(
                base_state
            )
            try:
                common_renderer["timeline_material_authority"] = (
                    fx_preview_contract.material_authority_digests(
                        self.project_dir, base_state
                    )
                )
            except fx_preview_contract.FxPreviewContractError as exc:
                raise FxPreviewJobError(409, exc.code, exc.message) from exc
        apply_start = float(overlay["start"])
        apply_duration = float(overlay["end"]) - apply_start
        return {
            "version": 1,
            "state_revision": self._state_revision(context),
            "source": {
                key: source_identity[key]
                for key in ("relative", "sha256", "bytes", "duration_s")
            },
            "context": {
                "state": deepcopy(
                    context.get("state")
                    if isinstance(context.get("state"), dict)
                    else {}
                ),
                "manifest": deepcopy(
                    context.get("manifest")
                    if isinstance(context.get("manifest"), dict)
                    else {}
                ),
            },
            "sample_window": deepcopy(sample_window),
            "apply_window": {
                "start_s": round(apply_start, 3),
                "duration_s": round(apply_duration, 3),
            },
            "preview_options": deepcopy(preview_options),
            "replace_overlay_id": replace_overlay_id,
            "renderer": common_renderer,
        }

    def _timeline_project_input_digests(self) -> dict[str, str]:
        """Digest every live working input the isolated timeline renderer copies."""
        self._assert_storage_safe("working", "root", "renderer", "runtime")
        result: dict[str, str] = {}
        working = self.project_dir / "working"
        for name in (
            "structured_layers.json",
            "visual_plan_v2.json",
            "caption_v2.json",
            "caption_delivery.json",
            "graphic_packages",
        ):
            root = working / name
            if root.is_symlink():
                raise FxPreviewJobError(409, "unsafe_project_path", "時間軸預覽依賴路徑不安全")
            if root.is_file():
                result[root.relative_to(self.project_dir).as_posix()] = _file_sha256(root)
                continue
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if path.is_symlink():
                    raise FxPreviewJobError(409, "unsafe_project_path", "時間軸預覽依賴路徑不安全")
                if not path.is_file():
                    continue
                result[path.relative_to(self.project_dir).as_posix()] = _file_sha256(path)
        return result

    @staticmethod
    def _local_python_dependency_digests(roots: list[Path]) -> dict[str, str]:
        """Recursively bind local Python imports used by the timeline renderer."""
        scripts = Path(__file__).resolve().parent
        pending = [path.resolve() for path in roots]
        seen: set[Path] = set()
        result: dict[str, str] = {}
        while pending:
            path = pending.pop()
            if path in seen:
                continue
            if scripts != path.parent or not _regular_nonsymlink(path):
                raise FxPreviewJobError(503, "renderer_unavailable", "完整預覽渲染器依賴不完整")
            seen.add(path)
            result[path.name] = _file_sha256(path)
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (OSError, UnicodeDecodeError, SyntaxError) as exc:
                raise FxPreviewJobError(503, "renderer_unavailable", "完整預覽渲染器依賴無法解析") from exc
            module_names: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    module_names.update(alias.name.split(".", 1)[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    module_names.add(node.module.split(".", 1)[0])
            for module in module_names:
                candidate = scripts / f"{module}.py"
                if candidate.is_file():
                    pending.append(candidate.resolve())
        return dict(sorted(result.items()))

    @staticmethod
    def _timeline_fx_block_digests(
        state: dict[str, Any], overlay: dict[str, Any], skill: Path
    ) -> dict[str, str]:
        catalog = fx_overlay.catalog(refresh=True)
        block_by_code = {
            str(item.get("code")): str(item.get("block"))
            for item in catalog.get("effects", [])
            if isinstance(item, dict) and isinstance(item.get("block"), str)
        }
        codes = {
            str(item.get("code"))
            for item in state.get("overlays", [])
            if isinstance(item, dict) and item.get("type") == "fx"
        }
        codes.add(str(overlay.get("code")))
        result: dict[str, str] = {}
        root = skill / "hyperframes-blocks/compositions"
        for code in sorted(codes):
            block = block_by_code.get(code)
            if not block or Path(block).name != block:
                raise FxPreviewJobError(503, "renderer_unavailable", "時間軸 FX 元件依賴不完整")
            path = root / f"{block}.html"
            if not _regular_nonsymlink(path):
                raise FxPreviewJobError(503, "renderer_unavailable", "時間軸 FX 元件依賴不完整")
            result[code] = _file_sha256(path)
        return result

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
                    "sha256": _file_sha256(path) if _regular_nonsymlink(path) else None,
                }
            except OSError:
                identities[name] = None
        return identities

    @staticmethod
    def _composition_analysis_identities() -> dict[str, Any]:
        scripts = Path(__file__).resolve().parent
        files: dict[str, str | None] = {}
        for name in (
            "composite_check.py",
            "composite_check_worker.py",
            "vision_ocr.py",
        ):
            path = scripts / name
            files[name] = _file_sha256(path) if _regular_nonsymlink(path) else None

        def distribution_version(*names: str) -> str | None:
            for name in names:
                try:
                    return importlib.metadata.version(name)
                except importlib.metadata.PackageNotFoundError:
                    continue
            return None

        return {
            "files": files,
            "native": {
                "python": platform.python_version(),
                "macos": platform.mac_ver()[0] or None,
                "opencv": distribution_version(
                    "opencv-python", "opencv-contrib-python", "opencv-python-headless"
                ),
                "pyobjc_core": distribution_version("pyobjc-core"),
                "pyobjc_vision": distribution_version("pyobjc-framework-Vision"),
            },
        }

    def submit(self, body: Any) -> dict[str, Any]:
        self.prune()
        comparison_anchor_job_id = self._comparison_anchor_id(body)
        if comparison_anchor_job_id is not None:
            with self._lock:
                anchor = self._jobs.get(comparison_anchor_job_id)
                if not self._comparison_anchor_usable(anchor):
                    raise FxPreviewJobError(
                        409,
                        "comparison_stale",
                        "比較錨點已失效，請重新產生這輪候選",
                    )
        context = self.context_provider()
        try:
            (
                overlay, replace_overlay_id, source_identity, sample_window,
                preview_options, state_snapshot, manifest_snapshot, timeline_snapshot,
            ) = self._normalize_candidate(body, context)
        except FxPreviewJobError as exc:
            if comparison_anchor_job_id is not None and exc.code in {
                "source_changed",
                "source_missing",
                "source_invalid",
                "source_duration_invalid",
            }:
                raise FxPreviewJobError(
                    409,
                    "comparison_stale",
                    "比較期間來源影片已變更，請重新產生這輪候選",
                ) from exc
            raise
        dependencies = self._dependency_snapshot(
            context,
            overlay,
            source_identity,
            preview_options=preview_options,
            frozen_state=state_snapshot,
            frozen_manifest=manifest_snapshot,
        )
        comparison_fingerprint = self._comparison_fingerprint(
            context,
            source_identity,
            sample_window,
            overlay,
            replace_overlay_id,
            preview_options,
            dependencies,
        )
        comparison_fingerprint_hash = _canonical_sha256(comparison_fingerprint)
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
                "overlay": fx_preview_contract.visual_overlay_projection(overlay),
                "sample_window": sample_window,
                "preview_options": preview_options,
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
            "timing_started": time.monotonic(),
            "timing_phase_started": time.monotonic(),
            "phase_timings_s": {},
            "updated_monotonic": now,
            "state_revision": dependencies["state_revision"],
            "overlay": overlay,
            "sample_window": sample_window,
            "preview_options": preview_options,
            "state_snapshot": state_snapshot,
            "manifest_snapshot": manifest_snapshot,
            "timeline_snapshot": timeline_snapshot,
            "replace_overlay_id": replace_overlay_id,
            "dependencies": dependencies,
            "dependency_hash": _canonical_sha256(dependencies),
            "source_identity": source_identity,
            "cache_key": cache_key,
            "comparison_anchor_job_id": comparison_anchor_job_id,
            "comparison_fingerprint": comparison_fingerprint,
            "comparison_fingerprint_hash": comparison_fingerprint_hash,
            "process": None,
            "cancel_requested": False,
            "media": None,
            "error": None,
        }
        cache_media = self.cache_dir / f"{cache_key}.mp4"
        cache_receipt = self.cache_dir / f"{cache_key}.json"
        cached = self._valid_cache_hit(
            cache_media,
            cache_receipt,
            cache_key,
            source_sha256=source_identity["sha256"],
            sample_window=sample_window,
            preview_options=preview_options,
            frame_size=dependencies["canvas"],
        )
        with self._lock:
            if comparison_anchor_job_id is not None:
                anchor = self._jobs.get(comparison_anchor_job_id)
                if (
                    not self._comparison_anchor_usable(anchor)
                    or not isinstance(anchor.get("comparison_fingerprint"), dict)
                    or anchor.get("comparison_fingerprint_hash")
                    != comparison_fingerprint_hash
                    or _canonical_sha256(anchor["comparison_fingerprint"])
                    != comparison_fingerprint_hash
                ):
                    raise FxPreviewJobError(
                        409,
                        "comparison_stale",
                        "比較條件已變更，請重新產生這輪候選",
                    )
            if self._active_job_id is not None:
                active = self._jobs.get(self._active_job_id)
                if active and active.get("state") in RUNNING_STATES:
                    raise FxPreviewJobError(409, "preview_busy", "同一專案已有 FX 預覽正在產生")
                self._active_job_id = None
            if cached is not None:
                self._set_timing_phase(job, "ready")
                job["timing_finished"] = time.monotonic()
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

    def _valid_cache_hit(
        self,
        media: Path,
        receipt_path: Path,
        cache_key: str,
        *,
        source_sha256: str,
        sample_window: dict[str, float],
        preview_options: dict[str, str],
        frame_size: dict[str, int],
    ) -> dict[str, Any] | None:
        self._assert_storage_safe("working", "root", "cache")
        cache_fd = self._storage_pins["cache"].fd
        try:
            receipt = self._read_json_at(cache_fd, receipt_path.name, MAX_RESULT_JSON_BYTES)
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(receipt, dict) or receipt.get("cache_key") != cache_key:
            return None
        expected = receipt.get("sha256")
        if not isinstance(expected, str) or SHA256_RE.fullmatch(expected) is None:
            return None
        try:
            actual, media_info = self._sha256_at(cache_fd, media.name)
        except OSError:
            return None
        if media_info.st_size <= 0 or media_info.st_size > MAX_PREVIEW_BYTES or actual != expected:
            return None
        try:
            self._assert_storage_safe("working", "root", "cache")
            duration = float(receipt.get("duration_s"))
            metadata = self.artifact_validator(media, duration)
            self._assert_storage_safe("working", "root", "cache")
        except (TypeError, ValueError, OSError, subprocess.SubprocessError):
            return None
        try:
            time_map = self._normalized_time_map(
                receipt.get("time_map"),
                sample_window,
                duration,
                mode=preview_options["mode"],
            )
        except (KeyError, TypeError, ValueError):
            return None
        actual_frame_size = {
            "width": int(metadata.get("width", frame_size["width"])),
            "height": int(metadata.get("height", frame_size["height"])),
        }
        composition_check = _sanitize_composition_check(
            receipt.get("composition_check"),
            scope=(
                "full-timeline"
                if preview_options["mode"] == "timeline"
                else "single-effect"
            ),
            source_sha256=source_sha256,
            media_sha256=actual,
            frame_size=actual_frame_size,
            render_duration_s=duration,
            time_map=time_map,
        )
        now = time.time()
        try:
            os.utime(media.name, (now, now), dir_fd=cache_fd, follow_symlinks=False)
            os.utime(receipt_path.name, (now, now), dir_fd=cache_fd, follow_symlinks=False)
        except OSError:
            return None
        return {
            "path": media,
            "sha256": expected,
            "bytes": media_info.st_size,
            "duration_s": duration,
            "time_map": time_map,
            "text_layout": _sanitize_text_layout(receipt.get("text_layout")),
            "composition_check": composition_check,
        }

    @staticmethod
    def _normalized_time_map(
        value: Any,
        sample_window: dict[str, float],
        render_duration_s: float,
        *,
        mode: str,
    ) -> list[dict[str, float]]:
        sample_start = float(sample_window["start_s"])
        sample_end = sample_start + float(sample_window["duration_s"])
        if value is None and mode == "single":
            value = [
                {
                    "source_start_s": sample_start,
                    "source_end_s": sample_end,
                    "output_start_s": 0.0,
                    "output_end_s": render_duration_s,
                }
            ]
        if not isinstance(value, list) or not value:
            raise ValueError("renderer time map is missing")
        result: list[dict[str, float]] = []
        previous_source_end = sample_start
        previous_output_end = 0.0
        keys = {"source_start_s", "source_end_s", "output_start_s", "output_end_s"}
        for entry in value:
            if not isinstance(entry, dict) or set(entry) != keys:
                raise ValueError("renderer time map entry is malformed")
            normalized: dict[str, float] = {}
            for key in keys:
                raw = entry.get(key)
                if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                    raise ValueError("renderer time map value is invalid")
                try:
                    number = float(raw)
                except (OverflowError, TypeError, ValueError) as exc:
                    raise ValueError("renderer time map value is invalid") from exc
                if not math.isfinite(number):
                    raise ValueError("renderer time map value is invalid")
                normalized[key] = round(number, 6)
            if (
                normalized["source_start_s"] < sample_start - 1e-6
                or normalized["source_end_s"] > sample_end + 1e-6
                or normalized["source_start_s"] < previous_source_end - 1e-6
                or normalized["source_end_s"] <= normalized["source_start_s"]
                or abs(normalized["output_start_s"] - previous_output_end) > 1e-6
                or normalized["output_end_s"] <= normalized["output_start_s"]
                or normalized["output_end_s"] > render_duration_s + 1e-6
            ):
                raise ValueError("renderer time map is out of range")
            source_span = normalized["source_end_s"] - normalized["source_start_s"]
            output_span = normalized["output_end_s"] - normalized["output_start_s"]
            if abs(source_span - output_span) > 0.02:
                raise ValueError("renderer time map changes playback speed")
            result.append(normalized)
            previous_source_end = normalized["source_end_s"]
            previous_output_end = normalized["output_end_s"]
        if abs(previous_output_end - render_duration_s) > 0.05:
            raise ValueError("renderer time map does not cover the output")
        return result

    def _run_job(self, job_id: str, cache_media: Path, cache_receipt: Path) -> None:
        try:
            self._assert_storage_safe()
        except FxPreviewJobError:
            with self._lock:
                self._finish_failed(job_id, "preview_storage_changed", "FX 預覽工作目錄已變更")
            return
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
            sample_window = dict(job["sample_window"])
            preview_options = dict(job["preview_options"])
            state_snapshot = deepcopy(job["state_snapshot"])
            manifest_snapshot = deepcopy(job["manifest_snapshot"])
            timeline_snapshot = deepcopy(job.get("timeline_snapshot"))
            replace_overlay_id = job.get("replace_overlay_id")
        request_payload = {
            "project_dir": str(self.project_dir),
            "source": str(source["path"]),
            "cache_dir": str(self.renderer_cache_dir),
            "output": str(output_path),
            "overlay": overlay,
            "sample_window": sample_window,
            "preview_options": preview_options,
            "state_snapshot": state_snapshot,
            "manifest_snapshot": manifest_snapshot,
            "timeline_snapshot": timeline_snapshot,
            "dependency_snapshot": deps,
            "pack_id": deps["pack_id"],
            "canvas": deps["canvas"],
            # Operational ownership only.  This is bound by request_sha256 but
            # deliberately excluded from visual cache/reuse digests.
            "scratch_owner": job_id,
        }
        request_fd = -1
        lease_fd = -1
        maintenance_fd = -1
        try:
            acquired_maintenance = self._acquire_renderer_maintenance_lock(exclusive=False)
            if acquired_maintenance is None:
                raise OSError("renderer maintenance guard unavailable")
            maintenance_fd = acquired_maintenance
            acquired_lease = self._try_acquire_scratch_lease(job_id, create=True)
            if acquired_lease is None:
                raise OSError("preview scratch lease is already active")
            lease_fd = acquired_lease
            request_sha256 = self._atomic_json_in(
                "runtime", request_path.name, request_payload
            )
            runtime_fd = self._storage_pins["runtime"].fd
            request_fd, _request_info = self._open_regular_at(
                runtime_fd, request_path.name
            )
            request_digest = hashlib.sha256()
            while True:
                chunk = os.read(request_fd, 1024 * 1024)
                if not chunk:
                    break
                request_digest.update(chunk)
            if request_digest.hexdigest() != request_sha256:
                raise OSError("preview request changed before worker launch")
            os.lseek(request_fd, 0, os.SEEK_SET)
            render_request_sha256 = fx_preview_contract.renderer_request_digest(
                request_payload
            )
            env = self._worker_environment()
            env["FX_PREVIEW_REQUEST_FD"] = str(request_fd)
            env["FX_PREVIEW_RESULT_DIR_FD"] = str(runtime_fd)
            env["FX_PREVIEW_SCRATCH_LEASE_FD"] = str(lease_fd)
            env["FX_PREVIEW_MAINTENANCE_FD"] = str(maintenance_fd)
            with self._lock:
                current = self._jobs.get(job_id)
                if current is None or current.get("cancel_requested"):
                    # cancel() published the request while no worker process
                    # existed; holding this lock prevents any later launch.
                    self._finish_cancelled(job_id, process_absence_verified=True)
                    return
                # Popen and job registration are one cancellation critical
                # section.  cancel() can therefore never mistake an already
                # created but not-yet-registered process for "no process".
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
                    pass_fds=(request_fd, runtime_fd, lease_fd, maintenance_fd),
                )
                current["process"] = process
                self._set_timing_phase(current, "preparing")
                current["message"] = _PHASE_MESSAGES["preparing"]
            os.close(request_fd)
            request_fd = -1
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
                    terminated = self._terminate_process(process)
                    with self._lock:
                        if terminated:
                            self._finish_cancelled(job_id, process_absence_verified=True)
                        else:
                            self._mark_termination_unverified(job_id)
                    return
                if time.monotonic() >= deadline:
                    terminated = self._terminate_process(process)
                    with self._lock:
                        if terminated:
                            self._finish_failed(job_id, "render_timeout", "FX 預覽超過本機時間上限")
                        else:
                            self._mark_termination_unverified(job_id)
                    return
                time.sleep(0.05)
            progress.join(timeout=1.0)
            os.close(maintenance_fd)
            maintenance_fd = -1
            self._assert_storage_safe("working", "root", "runtime")
            try:
                result = self._read_json_at(
                    self._storage_pins["runtime"].fd,
                    result_path.name,
                    MAX_RESULT_JSON_BYTES,
                )
            except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
                result = None
            if process.returncode != 0 or not isinstance(result, dict) or result.get("ok") is not True:
                code = result.get("code") if isinstance(result, dict) else None
                safe_code = code if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,40}", code) else "render_failed"
                safe_messages = {
                    "person_unavailable": "來源片段找不到可用的人物，未使用測試人物代替",
                    "invalid_source": "來源影片無法用於 FX 預覽",
                    "invalid_canvas": "目前專案畫布無法產生 FX 預覽",
                    "broll_requires_asset": "B1 需要先選擇本專案素材",
                    "broll_render_failed": "B1 已選素材無法產生預覽畫面",
                    "broll_render_unverified": "B1 預覽未能證明已包含所選素材",
                    "output_invalid": "FX 預覽輸出未通過媒體驗證",
                    "invalid_request": "本機 FX 預覽工作收到無效資料",
                    "subject_tracking_lost": "選取的主角已失去追蹤，請換取樣時窗或重新選人",
                }
                with self._lock:
                    self._finish_failed(job_id, safe_code, safe_messages.get(safe_code, "FX 預覽產生失敗"))
                return
            if (
                result.get("request_sha256") != request_sha256
                or result.get("render_request_sha256") != render_request_sha256
            ):
                with self._lock:
                    self._finish_failed(
                        job_id,
                        "worker_contract_invalid",
                        "FX 預覽工作未使用已凍結的請求",
                    )
                return
            out_value = result.get("out")
            self._assert_storage_safe("working", "root", "runtime")
            if not isinstance(out_value, str) or Path(out_value).resolve() != output_path.resolve():
                with self._lock:
                    self._finish_failed(job_id, "worker_contract_invalid", "FX 預覽工作回傳了無效檔案")
                return
            with self._lock:
                current = self._jobs.get(job_id)
                if self._worker_cancel_pending(current):
                    return
                self._set_timing_phase(current, "validating")
                current["message"] = _PHASE_MESSAGES["validating"]
            try:
                current_context = self.context_provider()
                current_source = self._source_identity(current_context.get("manifest") or {})
                current_frozen_state = None
                current_frozen_manifest = None
                if preview_options["mode"] == "timeline":
                    if self.timeline_snapshot_builder is None:
                        raise FxPreviewJobError(
                            500, "snapshot_invalid", "無法建立完整時間軸預覽快照"
                        )
                    current_timeline_snapshot = self.timeline_snapshot_builder(
                        deepcopy(current_context.get("state") or {}),
                        deepcopy(current_context.get("manifest") or {}),
                        deepcopy(overlay),
                        replace_overlay_id,
                    )
                    current_frozen_state = current_timeline_snapshot.get("state")
                    current_frozen_manifest = current_timeline_snapshot.get("manifest")
                    if not isinstance(current_frozen_state, dict) or not isinstance(
                        current_frozen_manifest, dict
                    ):
                        raise FxPreviewJobError(
                            500, "snapshot_invalid", "無法建立完整時間軸預覽快照"
                        )
                current_deps = self._dependency_snapshot(
                    current_context,
                    overlay,
                    current_source,
                    preview_options=preview_options,
                    frozen_state=current_frozen_state,
                    frozen_manifest=current_frozen_manifest,
                )
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
                if self._worker_cancel_pending(current):
                    return
                if _canonical_sha256(current_deps) != current["dependency_hash"]:
                    self._finish_failed(job_id, "preview_stale", "預覽期間來源或樣式已變更，請重新產生")
                    return
            raw_render_duration = result.get("render_duration_s")
            if raw_render_duration is None and preview_options["mode"] == "single":
                raw_render_duration = sample_window["duration_s"]
            try:
                numeric_render_duration = float(raw_render_duration)
            except (OverflowError, TypeError, ValueError):
                numeric_render_duration = math.nan
            if (
                isinstance(raw_render_duration, bool)
                or not isinstance(raw_render_duration, (int, float))
                or not math.isfinite(numeric_render_duration)
                or numeric_render_duration <= 0
                or numeric_render_duration > float(sample_window["duration_s"]) + 0.05
            ):
                raise ValueError("renderer duration is invalid")
            render_duration = round(numeric_render_duration, 3)
            time_map = self._normalized_time_map(
                result.get("time_map"),
                sample_window,
                render_duration,
                mode=preview_options["mode"],
            )
            text_layout = _sanitize_text_layout(result.get("text_layout"))
            metadata = self.artifact_validator(output_path, render_duration)
            self._assert_storage_safe("working", "root", "runtime")
            if not isinstance(metadata, dict):
                raise ValueError("artifact validator returned no metadata")
            digest, output_info = self._sha256_at(
                self._storage_pins["runtime"].fd,
                output_path.name,
            )
            size = output_info.st_size
            if size <= 0 or size > MAX_PREVIEW_BYTES:
                raise ValueError("preview size invalid")
            actual_frame_size = {
                "width": int(metadata.get("width", deps["canvas"]["width"])),
                "height": int(metadata.get("height", deps["canvas"]["height"])),
            }
            composition_check = _sanitize_composition_check(
                result.get("composition_check"),
                scope=(
                    "full-timeline"
                    if preview_options["mode"] == "timeline"
                    else "single-effect"
                ),
                source_sha256=source["sha256"],
                media_sha256=digest,
                frame_size=actual_frame_size,
                render_duration_s=render_duration,
                time_map=time_map,
            )
            receipt = {
                "schema_version": 1,
                "cache_key": job["cache_key"],
                "sha256": digest,
                "bytes": size,
                "duration_s": metadata.get("duration_s", render_duration),
                "time_map": time_map,
                "text_layout": text_layout,
                "composition_check": composition_check,
                "created_at": time.time(),
            }
            self._touch_renderer_cache()
            self._assert_storage_safe("working", "root", "cache", "runtime")
            os.rename(
                output_path.name,
                cache_media.name,
                src_dir_fd=self._storage_pins["runtime"].fd,
                dst_dir_fd=self._storage_pins["cache"].fd,
            )
            self._atomic_json_in("cache", cache_receipt.name, receipt)
            with self._lock:
                current = self._jobs.get(job_id)
                if self._worker_cancel_pending(current):
                    self._assert_storage_safe("working", "root", "cache")
                    for name in (cache_media.name, cache_receipt.name):
                        try:
                            os.unlink(name, dir_fd=self._storage_pins["cache"].fd)
                        except FileNotFoundError:
                            pass
                    return
                self._set_timing_phase(current, "ready")
                elapsed = max(0.0, time.monotonic() - current["timing_started"])
                group = self._timing_group(current)
                self._record_timing_sample(group, elapsed)
                current["timing_finished"] = time.monotonic()
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
                            "time_map": time_map,
                            "text_layout": text_layout,
                            "composition_check": composition_check,
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
            if request_fd >= 0:
                try:
                    os.close(request_fd)
                except OSError:
                    pass
            try:
                self._assert_storage_safe("working", "root", "runtime")
                for suffix in ("part.mp4", "request.json", "result.json"):
                    try:
                        os.unlink(
                            f"{job_id}.{suffix}",
                            dir_fd=self._storage_pins["runtime"].fd,
                        )
                    except OSError:
                        pass
            except FxPreviewJobError:
                pass
            if lease_fd >= 0:
                try:
                    os.close(lease_fd)
                except OSError:
                    pass
            if maintenance_fd >= 0:
                try:
                    os.close(maintenance_fd)
                except OSError:
                    pass
            try:
                self._cleanup_renderer_scratch(job_id)
            except (FxPreviewJobError, OSError):
                pass

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
                    if (
                        job is None
                        or job.get("state") != "running"
                        or job.get("cancel_requested")
                        or job.get("error")
                    ):
                        return
                    self._set_timing_phase(job, phase)
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
        except OSError:
            return True

    @classmethod
    def _terminate_process(cls, process: subprocess.Popen[str]) -> bool:
        pgid = process.pid
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
            return process.poll() is not None
        except (PermissionError, OSError):
            # Continue into the verification loop.  Never let a signal race
            # disconnect the HTTP handler or be mistaken for cancellation.
            pass

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
            except (ProcessLookupError, PermissionError, OSError):
                pass
            kill_deadline = time.monotonic() + 2.0
            while cls._process_group_exists(pgid) and time.monotonic() < kill_deadline:
                time.sleep(0.05)
        try:
            process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            pass
        return not cls._process_group_exists(pgid) and process.poll() is not None

    def _mark_termination_unverified(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is None or job.get("state") in TERMINAL_STATES:
            return
        message = "無法確認 FX 預覽程序已停止，請重試取消"
        self._set_timing_phase(job, "cancelling")
        job.update(
            {
                "state": "running",
                "phase": "cancelling",
                "message": message,
                "error": {"code": "termination_unverified", "message": message},
                "updated_monotonic": time.time(),
            }
        )

    @staticmethod
    def _worker_cancel_pending(job: dict[str, Any] | None) -> bool:
        """Stop worker callbacks without preempting the cancel handler's verdict."""
        return job is None or bool(job.get("cancel_requested"))

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
                "stream=codec_name,width,height:format=duration",
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
        stream = payload["streams"][0]
        width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
        if not 0 < width <= 16384 or not 0 < height <= 16384:
            raise ValueError("preview MP4 frame size is invalid")
        return {"duration_s": duration, "width": width, "height": height}

    def _finish_failed(self, job_id: str, code: str, message: str) -> None:
        job = self._jobs.get(job_id)
        if job is None or job.get("state") in TERMINAL_STATES:
            return
        # Every caller holds _lock.  A cancel handler owns process-group
        # verification outside this lock, so suppress the competing failure
        # transition here but let that handler alone choose cancelled versus
        # termination_unverified.  Never claim cancellation merely because the
        # request flag raced with a worker error.
        if job.get("cancel_requested"):
            return
        self._set_timing_phase(job, "failed")
        job["timing_finished"] = time.monotonic()
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

    def _finish_cancelled(
        self, job_id: str, *, process_absence_verified: bool = False
    ) -> None:
        job = self._jobs.get(job_id)
        if (
            not process_absence_verified
            or job is None
            or job.get("state") in TERMINAL_STATES
        ):
            return
        self._set_timing_phase(job, "cancelled")
        job["timing_finished"] = time.monotonic()
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
            terminated = self._terminate_process(process)
        else:
            terminated = True
        with self._lock:
            if terminated:
                self._finish_cancelled(job_id, process_absence_verified=True)
            else:
                self._mark_termination_unverified(job_id)
            public_job = self._public_job_locked(self._jobs[job_id])
        if terminated:
            # A termination_unverified worker may have outlived _run_job's
            # finally block while inheriting its lease.  Once a later cancel
            # proves the process group is gone, retry only this job's pinned,
            # leased shadow cleanup.  A still-held lease fails closed; the
            # worker's own finally remains the subsequent cleanup path.
            try:
                self._cleanup_renderer_scratch(job_id)
            except (FxPreviewJobError, OSError):
                pass
        return public_job

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
    def _timing_group(job: dict[str, Any]) -> tuple[str, str, str, float, float]:
        options = job.get("preview_options") or {}
        overlay = job.get("overlay") or {}
        duration = max(0.0, float(overlay.get("end", 0)) - float(overlay.get("start", 0)))
        sample = float((job.get("sample_window") or {}).get("duration_s", 0))
        return (str(options.get("mode", "single")), str(options.get("quality", "standard")),
                str(overlay.get("code", "")), round(duration, 1), round(sample, 1))

    def _record_timing_sample(self, group: tuple[str, str, str, float, float], elapsed: float) -> None:
        samples = self._timing_samples.pop(group, None)
        if samples is None:
            if len(self._timing_samples) >= 100:
                del self._timing_samples[next(iter(self._timing_samples))]
            samples = []
        # Dict insertion order is the recency index: every successful sample
        # moves its group to the end so an actively used early group is not
        # evicted ahead of colder groups.
        self._timing_samples[group] = samples
        samples.append(elapsed)
        del samples[:-20]

    @staticmethod
    def _set_timing_phase(job: dict[str, Any], phase: str) -> None:
        now = time.monotonic()
        previous = job.get("phase")
        if previous != phase and "timing_phase_started" in job:
            timings = job.setdefault("phase_timings_s", {})
            timings[previous] = timings.get(previous, 0.0) + max(0.0, now - job["timing_phase_started"])
            job["timing_phase_started"] = now
        job["phase"] = phase

    def _public_job_locked(self, job: dict[str, Any]) -> dict[str, Any]:
        now = job.get("timing_finished", time.monotonic())
        elapsed = max(0.0, now - job.get("timing_started", now))
        timings = dict(job.get("phase_timings_s") or {})
        if job.get("state") == "running":
            phase = job.get("phase")
            timings[phase] = timings.get(phase, 0.0) + max(0.0, now - job.get("timing_phase_started", now))
        samples = self._timing_samples.get(self._timing_group(job), [])
        estimate = sorted(samples)[len(samples) // 2] if len(samples) >= 3 else None
        eta_visible = (
            job.get("state") == "running"
            and job.get("phase") != "cancelling"
            and not job.get("error")
        )
        public = {
            "id": job["id"],
            "state": job["state"],
            "phase": job["phase"],
            "message": job["message"],
            "status_url": f"/api/fx-previews/{job['id']}",
            "cache_hit": bool(job.get("cache_hit")),
            "preview_options": deepcopy(job.get("preview_options") or {}),
            "elapsed_s": round(elapsed, 2),
            "phase_timings_s": {k: round(v, 2) for k, v in timings.items()},
            "eta_remaining_s": round(estimate - elapsed, 2)
            if estimate is not None and elapsed < estimate and eta_visible
            else None,
            "eta_sample_count": len(samples),
        }
        media = job.get("media")
        if job.get("state") == "ready" and isinstance(media, dict):
            public["media_url"] = f"/fx-preview-media/{job['id']}/{media['sha256']}.mp4"
            public["render_duration_s"] = media.get("duration_s")
            public["time_map"] = deepcopy(media.get("time_map") or [])
            if media.get("text_layout") is not None:
                public["text_layout"] = deepcopy(media["text_layout"])
            if media.get("composition_check") is not None:
                public["composition_check"] = deepcopy(media["composition_check"])
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
                "preview_options": dict(job["preview_options"]),
            }
        if expected_revision != self._state_revision(context) or expected_revision != job["state_revision"]:
            raise FxPreviewJobError(409, "preview_stale", "預覽後時間軸已變更，請重新產生再套用")
        current_frozen_state = None
        current_frozen_manifest = None
        if frozen["preview_options"]["mode"] == "timeline":
            if self.timeline_snapshot_builder is None:
                raise FxPreviewJobError(
                    500, "snapshot_invalid", "無法建立完整時間軸預覽快照"
                )
            current_timeline_snapshot = self.timeline_snapshot_builder(
                deepcopy(context.get("state") or {}),
                deepcopy(context.get("manifest") or {}),
                deepcopy(frozen["overlay"]),
                frozen["replace_overlay_id"],
            )
            current_frozen_state = current_timeline_snapshot.get("state")
            current_frozen_manifest = current_timeline_snapshot.get("manifest")
            if not isinstance(current_frozen_state, dict) or not isinstance(
                current_frozen_manifest, dict
            ):
                raise FxPreviewJobError(
                    500, "snapshot_invalid", "無法建立完整時間軸預覽快照"
                )
        current_deps = self._dependency_snapshot(
            context,
            frozen["overlay"],
            preview_options=frozen["preview_options"],
            frozen_state=current_frozen_state,
            frozen_manifest=current_frozen_manifest,
        )
        if _canonical_sha256(current_deps) != frozen["dependency_hash"]:
            raise FxPreviewJobError(409, "preview_stale", "預覽依賴的來源、主題或字型已變更")
        return frozen

    def media_path(self, job_id: str, digest: str) -> Path:
        self._assert_storage_safe("working", "root", "cache")
        if JOB_ID_RE.fullmatch(job_id) is None or SHA256_RE.fullmatch(digest) is None:
            raise FxPreviewJobError(404, "media_not_found", "找不到這個 FX 預覽檔")
        with self._lock:
            job = self._jobs.get(job_id)
            media = job.get("media") if job else None
            if not job or job.get("state") != "ready" or not isinstance(media, dict) or media.get("sha256") != digest:
                raise FxPreviewJobError(404, "media_not_found", "找不到這個 FX 預覽檔")
            path = media.get("path")
        if not isinstance(path, Path) or path.name != f"{job.get('cache_key')}.mp4":
            raise FxPreviewJobError(404, "media_not_found", "找不到這個 FX 預覽檔")
        cache_fd = self._storage_pins["cache"].fd
        try:
            actual, _info = self._sha256_at(cache_fd, path.name)
        except OSError:
            raise FxPreviewJobError(404, "media_not_found", "找不到這個 FX 預覽檔") from None
        if actual != digest:
            raise FxPreviewJobError(404, "media_not_found", "找不到這個 FX 預覽檔")
        try:
            now = time.time()
            os.utime(path.name, (now, now), dir_fd=cache_fd, follow_symlinks=False)
            cache_key = str(job.get("cache_key") or "")
            if SHA256_RE.fullmatch(cache_key):
                try:
                    descriptor, _receipt_info = self._open_regular_at(cache_fd, f"{cache_key}.json")
                    os.close(descriptor)
                    os.utime(
                        f"{cache_key}.json",
                        (now, now),
                        dir_fd=cache_fd,
                        follow_symlinks=False,
                    )
                except OSError:
                    pass
        except OSError:
            raise FxPreviewJobError(404, "media_not_found", "找不到這個 FX 預覽檔") from None
        self._assert_storage_safe("working", "root", "cache")
        return path

    def prune(self) -> None:
        self._assert_storage_safe()
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
                try:
                    os.unlink(
                        f"{job['id']}.{suffix}",
                        dir_fd=self._storage_pins["runtime"].fd,
                    )
                except FileNotFoundError:
                    pass
        cache_fd = self._storage_pins["cache"].fd
        entries: list[tuple[float, int, str, str]] = []
        for entry in list(os.scandir(cache_fd)):
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or not re.fullmatch(r"[0-9a-f]{64}\.mp4", entry.name)
            ):
                continue
            entries.append((info.st_mtime, info.st_size, entry.name[:-4], entry.name))
        total = sum(item[1] for item in entries)
        count = len(entries)
        for mtime, size, key, media_name in sorted(entries):
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
            for name in (media_name, f"{key}.json"):
                try:
                    os.unlink(name, dir_fd=cache_fd)
                except FileNotFoundError:
                    pass
            count -= 1
            total -= size

        renderer_guard = self._acquire_renderer_maintenance_lock(exclusive=True)
        if renderer_guard is None:
            return
        renderer_fd = self._storage_pins["renderer"].fd
        renderer_entries: list[tuple[float, int, tuple[str, ...]]] = []

        def collect_renderer(
            _parent_fd: int,
            _name: str,
            info: os.stat_result,
            parts: tuple[str, ...],
        ) -> None:
            if (
                not parts
                or parts[0].startswith("timeline-project-")
                or TIMELINE_LEASE_RE.fullmatch(parts[0]) is not None
                or parts[0] == RENDERER_MAINTENANCE_LOCK
            ):
                return
            if info.st_nlink == 1:
                renderer_entries.append((info.st_mtime, info.st_size, parts))

        try:
            self._walk_files_at(renderer_fd, collect_renderer)
            renderer_total = sum(item[1] for item in renderer_entries)
            if self.has_active_job():
                return
            for mtime, size, parts in sorted(renderer_entries):
                expired_by_ttl = now - mtime > CACHE_IDLE_TTL_S
                if not expired_by_ttl and total + renderer_total <= CACHE_MAX_BYTES:
                    break
                try:
                    self._unlink_relative_at(renderer_fd, parts)
                except FileNotFoundError:
                    pass
                renderer_total -= size
            self._remove_renderer_empty_dirs()
        finally:
            os.close(renderer_guard)

    def _remove_renderer_empty_dirs(self) -> None:
        """Remove ordinary cache directories without entering live scratch roots."""
        renderer_fd = self._storage_pins["renderer"].fd
        for entry in list(os.scandir(renderer_fd)):
            if (
                entry.name.startswith("timeline-project-")
                or TIMELINE_LEASE_RE.fullmatch(entry.name) is not None
                or entry.name == RENDERER_MAINTENANCE_LOCK
            ):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISDIR(info.st_mode):
                continue
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                child_fd = os.open(entry.name, flags, dir_fd=renderer_fd)
            except OSError:
                continue
            try:
                opened = os.fstat(child_fd)
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    continue
                self._remove_empty_dirs_at(child_fd)
            finally:
                os.close(child_fd)
            try:
                os.rmdir(entry.name, dir_fd=renderer_fd)
            except OSError:
                pass

    def _touch_renderer_cache(self) -> None:
        """Record successful use so the 24-hour idle TTL is meaningful."""
        self._assert_storage_safe("working", "root", "renderer")
        deadline = time.monotonic() + RENDERER_TOUCH_RETRY_BUDGET_S
        renderer_guard = None
        for attempt in range(RENDERER_TOUCH_RETRY_ATTEMPTS):
            renderer_guard = self._acquire_renderer_maintenance_lock(exclusive=True)
            if renderer_guard is not None:
                break
            now = time.monotonic()
            if attempt + 1 >= RENDERER_TOUCH_RETRY_ATTEMPTS or now >= deadline:
                return
            time.sleep(min(RENDERER_TOUCH_RETRY_DELAY_S, max(0.0, deadline - now)))
        if renderer_guard is None:
            return
        now_ns = time.time_ns()

        def touch(
            parent_fd: int,
            name: str,
            info: os.stat_result,
            parts: tuple[str, ...],
        ) -> None:
            if (
                not parts
                or parts[0].startswith("timeline-project-")
                or TIMELINE_LEASE_RE.fullmatch(parts[0]) is not None
                or parts[0] == RENDERER_MAINTENANCE_LOCK
            ):
                return
            try:
                # A killed renderer from an older build may have left hard
                # links to live project inputs.  Touching such a cache entry
                # would mutate the live inode's mtime.
                if info.st_nlink > 1:
                    return
                os.utime(
                    name,
                    ns=(now_ns, now_ns),
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except OSError:
                return

        try:
            self._walk_files_at(self._storage_pins["renderer"].fd, touch)
        finally:
            os.close(renderer_guard)

    def _owned_runtime_entries_absent(self, owner: str) -> bool:
        """Return true only when every exact owner runtime name is provably absent."""
        if JOB_ID_RE.fullmatch(owner) is None:
            raise FxPreviewJobError(
                409, "preview_storage_changed", "FX 預覽暫存 ownership 無效"
            )
        self._assert_storage_safe("working", "root", "runtime")
        runtime_fd = self._storage_pins["runtime"].fd
        for suffix in RUNTIME_ARTIFACT_SUFFIXES:
            try:
                os.stat(
                    f"{owner}.{suffix}",
                    dir_fd=runtime_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            except OSError:
                return False
            return False
        return True

    def _cleanup_renderer_scratch(self, owner: str | None = None) -> None:
        """Remove only idle, leased shadow projects through the pinned cache."""
        self._assert_storage_safe("working", "root", "renderer")
        renderer_fd = self._storage_pins["renderer"].fd
        owners: set[str] = set()
        if owner is not None:
            if JOB_ID_RE.fullmatch(owner) is None:
                raise FxPreviewJobError(
                    409, "preview_storage_changed", "FX 預覽暫存 ownership 無效"
                )
            owners.add(owner)
        for entry in list(os.scandir(renderer_fd)):
            scratch_match = TIMELINE_SCRATCH_RE.fullmatch(entry.name)
            lease_match = TIMELINE_LEASE_RE.fullmatch(entry.name)
            match = scratch_match or lease_match
            found_owner = match.group(1) if match is not None else None
            if found_owner is not None and (owner is None or found_owner == owner):
                owners.add(found_owner)
        for scratch_owner in owners:
            lease_fd = self._try_acquire_scratch_lease(scratch_owner, create=False)
            if lease_fd is None:
                continue
            try:
                removed_all = True
                for entry in list(os.scandir(renderer_fd)):
                    match = TIMELINE_SCRATCH_RE.fullmatch(entry.name)
                    if match is None or match.group(1) != scratch_owner:
                        continue
                    try:
                        self._remove_tree_at(renderer_fd, entry.name)
                    except FileNotFoundError:
                        continue
                    except OSError:
                        removed_all = False
                if removed_all:
                    removed_all = not any(
                        (match := TIMELINE_SCRATCH_RE.fullmatch(entry.name)) is not None
                        and match.group(1) == scratch_owner
                        for entry in os.scandir(renderer_fd)
                    )
                if removed_all:
                    try:
                        runtime_absent = self._owned_runtime_entries_absent(scratch_owner)
                    except (FxPreviewJobError, OSError):
                        runtime_absent = False
                    if runtime_absent:
                        self._unlink_scratch_lease_if_same(scratch_owner, lease_fd)
            finally:
                os.close(lease_fd)

    def close(self) -> None:
        with self._lock:
            active = self._active_job_id
        if active is not None:
            try:
                self.cancel(active)
            except FxPreviewJobError:
                pass
        for pin in reversed(tuple(self._storage_pins.values())):
            pin.close()

    def _cleanup_orphan_partials(self) -> None:
        self._assert_storage_safe("working", "root", "runtime")
        runtime_fd = self._storage_pins["runtime"].fd
        owners: set[str] = set()
        for entry in list(os.scandir(runtime_fd)):
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            match = re.fullmatch(
                r"([0-9a-f]{32})\.(?:request\.json|result\.json|part\.mp4)",
                entry.name,
            )
            if match is not None:
                owners.add(match.group(1))
        for scratch_owner in owners:
            lease_fd = self._try_acquire_scratch_lease(scratch_owner, create=False)
            if lease_fd is None:
                continue
            try:
                for suffix in RUNTIME_ARTIFACT_SUFFIXES:
                    try:
                        os.unlink(
                            f"{scratch_owner}.{suffix}",
                            dir_fd=runtime_fd,
                        )
                    except OSError:
                        pass
            finally:
                os.close(lease_fd)
