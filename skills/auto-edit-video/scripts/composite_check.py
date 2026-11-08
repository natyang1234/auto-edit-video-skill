#!/usr/bin/env python3
"""Bounded local-frame diagnostics for a frozen full-timeline preview.

The renderer owns the request contract.  This module deliberately keeps the
public result small and path-free; frame extraction and native probes are
implemented below without launching another process.
"""
from __future__ import annotations

import hashlib
import math
import numbers
import re
import time
from pathlib import Path
from typing import Any, Iterable, Mapping


MAX_SAMPLES = 8
MAX_FINDINGS = 32
SMALL_PREVIEW_SECONDS = 2.5
TEXT_OVERLAP_FRACTION = 0.15
VISIBLE_FACE_IOU = 0.25
_TIME_EPSILON = 1e-6
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_TIME_MAP_KEYS = frozenset(
    {"source_start_s", "source_end_s", "output_start_s", "output_end_s"}
)
_FINDING_KINDS = frozenset(
    {
        "text_subject_overlap",
        "visibility_uncertain",
        "source_subject_unmeasured",
        "ocr_unavailable",
        "reference_unavailable",
        "analysis_incomplete",
        "full_timeline_required",
    }
)


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _valid_sha(value: Any) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _zero_sha() -> str:
    return "0" * 64


def _as_path(value: Any) -> Path | None:
    if isinstance(value, (str, Path)):
        try:
            return Path(value)
        except (TypeError, ValueError, OSError):
            return None
    return None


def validate_time_map(
    time_map: Any,
    *,
    duration_s: float | None = None,
) -> list[dict[str, float]]:
    """Validate and normalize renderer time-map segments.

    Output segments must form one contiguous half-open output timeline.  Source
    segments are also ordered, but may have deleted gaps.  Keeping this pure is
    useful because the manager can run the same checks before publication.
    """
    if not isinstance(time_map, list) or not time_map or len(time_map) > 128:
        raise ValueError("time map is missing or bounded incorrectly")
    normalized: list[dict[str, float]] = []
    previous: dict[str, float] | None = None
    for raw in time_map:
        if not isinstance(raw, Mapping) or set(raw) != _TIME_MAP_KEYS:
            raise ValueError("time map entry is malformed")
        entry: dict[str, float] = {}
        for key in _TIME_MAP_KEYS:
            number = _finite(raw.get(key))
            if number is None or number < 0:
                raise ValueError("time map value is invalid")
            entry[key] = round(number, 6)
        if entry["source_end_s"] <= entry["source_start_s"]:
            raise ValueError("source segment is empty")
        if entry["output_end_s"] <= entry["output_start_s"]:
            raise ValueError("output segment is empty")
        if previous is not None:
            if entry["source_start_s"] < previous["source_end_s"] - _TIME_EPSILON:
                raise ValueError("source segments overlap")
            if abs(entry["output_start_s"] - previous["output_end_s"]) > 0.001:
                raise ValueError("output segments do not cover a contiguous timeline")
        normalized.append(entry)
        previous = entry
    if normalized[0]["output_start_s"] > 0.001:
        raise ValueError("output timeline does not start at zero")
    if duration_s is not None:
        duration = _finite(duration_s)
        if duration is None or duration <= 0:
            raise ValueError("duration is invalid")
        if normalized[-1]["output_end_s"] > duration + 0.001:
            raise ValueError("time map exceeds duration")
        if abs(normalized[-1]["output_end_s"] - duration) > 0.05:
            raise ValueError("time map does not cover duration")
    return normalized


def sample_output_timestamps(duration_s: float, *, max_samples: int = MAX_SAMPLES) -> list[float]:
    """Return evenly spaced, interior output timestamps.

    Five points are used for the normal short preview and eight for a longer
    preview.  The result is explicitly a heuristic, not an every-frame claim.
    """
    duration = _finite(duration_s)
    limit = int(max_samples) if isinstance(max_samples, int) and not isinstance(max_samples, bool) else MAX_SAMPLES
    limit = min(MAX_SAMPLES, max(1, limit))
    if duration is None or duration <= 0:
        return []
    target = 5 if duration <= SMALL_PREVIEW_SECONDS else MAX_SAMPLES
    count = min(limit, target)
    result: list[float] = []
    for index in range(count):
        timestamp = round(duration * (index + 0.5) / count, 3)
        if not result or timestamp > result[-1]:
            result.append(timestamp)
    return result


def sample_timestamps(duration_s: float, *, max_samples: int = MAX_SAMPLES) -> list[float]:
    """Compatibility alias for the bounded output sampling helper."""
    return sample_output_timestamps(duration_s, max_samples=max_samples)


def map_output_to_source(time_map: Any, output_at: float) -> float | None:
    """Map an output timestamp through half-open cut-aware segments."""
    timestamp = _finite(output_at)
    if timestamp is None or timestamp < 0:
        return None
    try:
        segments = validate_time_map(time_map)
    except ValueError:
        return None
    for segment in segments:
        start = segment["output_start_s"]
        end = segment["output_end_s"]
        # Deliberately half-open: at a deleted-gap boundary the next segment
        # wins, never the previous source segment.
        if start <= timestamp < end:
            ratio = (timestamp - start) / (end - start)
            return round(segment["source_start_s"] + ratio * (segment["source_end_s"] - segment["source_start_s"]), 6)
    return None


def map_time(time_map: Any, output_at: float) -> float | None:
    """Compatibility alias used by callers that call this operation `map_time`."""
    return map_output_to_source(time_map, output_at)


def output_to_source(time_map: Any, output_at: float) -> float | None:
    """Compatibility alias for cut-aware output-to-source mapping."""
    return map_output_to_source(time_map, output_at)


def _rect(value: Any) -> tuple[float, float, float, float] | None:
    if isinstance(value, Mapping):
        values = [value.get(key) for key in ("x", "y", "width", "height")]
    elif isinstance(value, (list, tuple)) and len(value) >= 4:
        values = list(value[:4])
    else:
        return None
    numbers = [_finite(item) for item in values]
    if any(number is None for number in numbers):
        return None
    x, y, width, height = (float(number) for number in numbers)
    if width <= 0 or height <= 0:
        return None
    return x, y, width, height


def _rect_public(value: Any, frame_width: int, frame_height: int) -> dict[str, float] | None:
    parsed = _rect(value)
    if parsed is None or frame_width <= 0 or frame_height <= 0:
        return None
    x, y, width, height = parsed
    left = min(float(frame_width), max(0.0, x))
    top = min(float(frame_height), max(0.0, y))
    right = min(float(frame_width), max(left, x + width))
    bottom = min(float(frame_height), max(top, y + height))
    if right <= left or bottom <= top:
        return None
    return {
        "x": round(left, 4),
        "y": round(top, 4),
        "width": round(right - left, 4),
        "height": round(bottom - top, 4),
    }


def rect_intersection_fraction(subject: Any, text: Any) -> float:
    """Return intersection area divided by the smaller rectangle area."""
    first = _rect(subject)
    second = _rect(text)
    if first is None or second is None:
        return 0.0
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    intersection_width = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    intersection_height = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    intersection = intersection_width * intersection_height
    smaller = min(aw * ah, bw * bh)
    return intersection / smaller if smaller > 0 else 0.0


def _strongest_text_overlap(
    subject: Mapping[str, Any], text_boxes: Iterable[Mapping[str, Any]]
) -> Mapping[str, Any] | None:
    """Keep one deterministic representative text box for one subject/sample."""
    strongest: Mapping[str, Any] | None = None
    strongest_fraction = -1.0
    for text_box in text_boxes:
        fraction = rect_intersection_fraction(subject, text_box)
        if fraction >= TEXT_OVERLAP_FRACTION and fraction > strongest_fraction:
            strongest = text_box
            strongest_fraction = fraction
    return strongest


def normalized_intersection(subject: Any, text: Any) -> float:
    """Readable alias for the conservative text-over-subject rule."""
    return rect_intersection_fraction(subject, text)


def rect_iou(first: Any, second: Any) -> float:
    """Return rectangle intersection-over-union."""
    left = _rect(first)
    right = _rect(second)
    if left is None or right is None:
        return 0.0
    ax, ay, aw, ah = left
    bx, by, bw, bh = right
    intersection_width = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    intersection_height = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    intersection = intersection_width * intersection_height
    union = aw * ah + bw * bh - intersection
    return intersection / union if union > 0 else 0.0


def iou(first: Any, second: Any) -> float:
    """Compatibility alias for rectangle IoU."""
    return rect_iou(first, second)


class _FindingCollector:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self.overflowed = False

    def add(self, finding: Mapping[str, Any]) -> None:
        kind = finding.get("kind")
        if kind not in _FINDING_KINDS:
            return
        clean: dict[str, Any] = {"kind": kind}
        for key in ("output_at", "source_at"):
            number = _finite(finding.get(key))
            if number is not None:
                clean[key] = round(number, 3)
        for key in ("subject_box", "text_box"):
            box = finding.get(key)
            if isinstance(box, Mapping):
                parsed = _rect(box)
                if parsed is not None:
                    clean[key] = {
                        "x": round(parsed[0], 4),
                        "y": round(parsed[1], 4),
                        "width": round(parsed[2], 4),
                        "height": round(parsed[3], 4),
                    }
        if len(self.items) < MAX_FINDINGS:
            self.items.append(clean)
        else:
            self.overflowed = True

    def finalize(self) -> list[dict[str, Any]]:
        if self.overflowed:
            return [*self.items[: MAX_FINDINGS - 1], {"kind": "analysis_incomplete"}]
        return self.items[:MAX_FINDINGS]


def _hash_file(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except (OSError, ValueError):
        return None
    return digest.hexdigest()


def _frame_size(width: Any, height: Any) -> tuple[int, int]:
    width_number = _finite(width)
    height_number = _finite(height)
    if width_number is None or height_number is None:
        return 1, 1
    width_int, height_int = int(round(width_number)), int(round(height_number))
    if width_int <= 0 or height_int <= 0 or width_int > 20000 or height_int > 20000:
        return 1, 1
    return width_int, height_int


class _VideoReader:
    """Small OpenCV reader; it never starts an ffmpeg/subprocess child."""

    def __init__(self, path: Path, cv2_module: Any) -> None:
        self.path = path
        self.cv2 = cv2_module
        self.capture = cv2_module.VideoCapture(str(path))
        if not self.capture.isOpened():
            self.capture.release()
            raise ValueError("video could not be opened")
        self.width, self.height = _frame_size(
            self.capture.get(cv2_module.CAP_PROP_FRAME_WIDTH),
            self.capture.get(cv2_module.CAP_PROP_FRAME_HEIGHT),
        )
        self.fps = _finite(self.capture.get(cv2_module.CAP_PROP_FPS)) or 0.0
        self.count = _finite(self.capture.get(cv2_module.CAP_PROP_FRAME_COUNT)) or 0.0

    @property
    def duration(self) -> float | None:
        if self.fps > 0 and self.count > 0:
            value = self.count / self.fps
            return value if math.isfinite(value) and value > 0 else None
        return None

    def read(self, timestamp: float) -> Any | None:
        number = _finite(timestamp)
        if number is None or number < 0:
            return None
        try:
            if self.fps > 0 and self.count > 0:
                index = min(max(0, int(round(number * self.fps))), max(0, int(self.count) - 1))
                self.capture.set(self.cv2.CAP_PROP_POS_FRAMES, index)
            else:
                self.capture.set(self.cv2.CAP_PROP_POS_MSEC, number * 1000.0)
            ok, frame = self.capture.read()
            if not ok or frame is None:
                return None
            return frame
        except Exception:
            return None

    def close(self) -> None:
        try:
            self.capture.release()
        except Exception:
            pass


class _YuNetProbe:
    def __init__(self, model: Path | None, cv2_module: Any) -> None:
        self.model = model
        self.cv2 = cv2_module
        self.detector: Any | None = None
        self.failed = model is None or not model.is_file()

    def detect(self, frame: Any) -> tuple[bool, list[dict[str, float]]]:
        if self.failed or frame is None:
            return False, []
        try:
            height, width = frame.shape[:2]
            if width <= 0 or height <= 0:
                return False, []
            if self.detector is None:
                factory = getattr(self.cv2, "FaceDetectorYN_create", None)
                if factory is None:
                    face_detector = getattr(getattr(self.cv2, "FaceDetectorYN", None), "create", None)
                    factory = face_detector
                if factory is None:
                    self.failed = True
                    return False, []
                self.detector = factory(
                    str(self.model),
                    "",
                    (int(width), int(height)),
                    0.6,
                    0.3,
                    50,
                )
            else:
                setter = getattr(self.detector, "setInputSize", None)
                if setter is not None:
                    setter((int(width), int(height)))
            result = self.detector.detect(frame)
            faces = result[1] if isinstance(result, tuple) and len(result) >= 2 else result
            if faces is None:
                return True, []
            boxes: list[dict[str, float]] = []
            for row in faces:
                values = list(row)
                if len(values) < 4:
                    continue
                box = _rect_public(values[:4], int(width), int(height))
                if box is not None:
                    boxes.append(box)
            return True, boxes
        except Exception:
            return False, []


def _vision_ocr_boxes(frame_path: Path, width: int, height: int) -> tuple[str, list[dict[str, float]]]:
    """Return ``ok``, ``empty`` or ``failed`` without exposing recognized text."""
    try:
        import vision_ocr  # type: ignore

        lines = vision_ocr.recognize_text(frame_path, timeout_s=1.5)
    except Exception:
        return "failed", []
    boxes: list[dict[str, float]] = []
    for line in lines if isinstance(lines, list) else []:
        if not isinstance(line, Mapping):
            continue
        text = line.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        raw_box = line.get("box")
        if not isinstance(raw_box, Mapping):
            continue
        x = _finite(raw_box.get("x"))
        y = _finite(raw_box.get("y"))
        box_width = _finite(raw_box.get("width"))
        box_height = _finite(raw_box.get("height"))
        if None in (x, y, box_width, box_height) or box_width <= 0 or box_height <= 0:
            continue
        # Vision uses a normalized bottom-left origin.  The receipt uses the
        # canvas' pixel coordinates with a top-left origin.
        pixel_box = {
            "x": x * width,
            "y": (1.0 - y - box_height) * height,
            "width": box_width * width,
            "height": box_height * height,
        }
        clean = _rect_public(pixel_box, width, height)
        if clean is not None:
            boxes.append(clean)
    return ("ok", boxes) if boxes else ("empty", [])


def _scale_box(box: Mapping[str, Any], from_size: tuple[int, int], to_size: tuple[int, int]) -> dict[str, float] | None:
    parsed = _rect(box)
    if parsed is None or from_size[0] <= 0 or from_size[1] <= 0:
        return None
    scale_x = to_size[0] / from_size[0]
    scale_y = to_size[1] / from_size[1]
    x, y, width, height = parsed
    return _rect_public(
        {"x": x * scale_x, "y": y * scale_y, "width": width * scale_x, "height": height * scale_y},
        to_size[0],
        to_size[1],
    )


def _base_receipt(
    *,
    scope: str,
    source_sha256: str,
    media_sha256: str,
    frame_size: tuple[int, int],
    elapsed_s: float = 0.0,
    samples: Iterable[Mapping[str, Any]] = (),
    findings: Iterable[Mapping[str, Any]] = (),
    status: str = "unmeasured",
    engines: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    sample_list = [dict(item) for item in samples][:MAX_SAMPLES]
    finding_list = [dict(item) for item in findings][:MAX_FINDINGS]
    measured_count = sum(1 for sample in sample_list if sample.get("measured") is True)
    width, height = _frame_size(*frame_size)
    safe_scope = scope if scope in {"full-timeline", "single-effect"} else "full-timeline"
    safe_status = status if status in {"pass", "warn", "unmeasured"} else "unmeasured"
    safe_engines = {
        "faces": "unavailable",
        "ocr": "unavailable",
    }
    if isinstance(engines, Mapping):
        for key in safe_engines:
            value = engines.get(key)
            if value in {"opencv-yunet", "macos-vision", "unavailable"}:
                safe_engines[key] = value
    return {
        "version": 1,
        "scope": safe_scope,
        "measurement": "sampled-local-frames",
        "status": safe_status,
        "source_sha256": source_sha256 if _valid_sha(source_sha256) else _zero_sha(),
        "media_sha256": media_sha256 if _valid_sha(media_sha256) else _zero_sha(),
        "frame_size": {"width": width, "height": height},
        "sample_count": len(sample_list),
        "measured_count": measured_count,
        "elapsed_s": round(max(0.0, float(elapsed_s)), 3),
        "engines": safe_engines,
        "samples": sample_list,
        "findings": finding_list,
    }


def _safe_sample(output_at: float, source_at: float) -> dict[str, Any]:
    return {
        "output_at": round(float(output_at), 3),
        "source_at": round(float(source_at), 3),
        "measured": False,
        "source_face_count": 0,
        "reference_face_count": 0,
        "visible_face_count": 0,
        "text_box_count": 0,
    }


def _expected_text_at(expected_text: Any, index: int) -> bool:
    """Resolve the conservative per-sample typography expectation.

    Missing or malformed expectations remain conservative (text expected).
    An explicit ``False`` is the only state in which a successful empty OCR
    result can still measure a plain source/reference/person frame.
    """
    if isinstance(expected_text, bool):
        return expected_text
    if isinstance(expected_text, (list, tuple)):
        if 0 <= index < len(expected_text) and isinstance(expected_text[index], bool):
            return expected_text[index]
        return True
    return True


def _load_cv2() -> Any | None:
    try:
        import cv2  # type: ignore

        return cv2
    except Exception:
        return None


def analyze(request: dict[str, Any]) -> dict[str, Any]:
    """Analyze local source/reference/composite frames and return a bounded receipt.

    Input paths are internal and never copied into the returned object.  Any
    capability, decode, mapping, or binding uncertainty is conservative: it
    yields ``unmeasured`` and a generic allowed finding instead of a pass.
    """
    started = time.perf_counter()
    if not isinstance(request, dict):
        return _base_receipt(
            scope="full-timeline",
            source_sha256=_zero_sha(),
            media_sha256=_zero_sha(),
            frame_size=(1, 1),
            elapsed_s=time.perf_counter() - started,
            findings=({"kind": "analysis_incomplete"},),
        )

    scope_value = request.get("scope")
    scope = scope_value if scope_value in {"full-timeline", "single-effect"} else "full-timeline"
    source_path = _as_path(request.get("source"))
    composite_path = _as_path(request.get("composite"))
    reference_path = _as_path(request.get("reference")) if request.get("reference") is not None else None
    model_path = _as_path(request.get("face_model"))
    scratch_path = _as_path(request.get("scratch"))
    requested_source_sha = request.get("source_sha256")
    # The renderer has already hashed the project source, verified that digest
    # while copying the bytes into its owned shadow, and bound both values into
    # this internal request.  Re-reading the complete source here would spend
    # the optional probe's hard deadline without strengthening publication:
    # the manager independently rebinds this field before exposing a receipt.
    source_sha = requested_source_sha if _valid_sha(requested_source_sha) else _zero_sha()
    media_sha = _hash_file(composite_path) or _zero_sha()
    findings = _FindingCollector()
    if not _valid_sha(requested_source_sha):
        findings.add({"kind": "analysis_incomplete"})

    if scope != "full-timeline":
        findings.add({"kind": "full_timeline_required"})
        return _base_receipt(
            scope=scope,
            source_sha256=source_sha,
            media_sha256=media_sha,
            frame_size=(1, 1),
            elapsed_s=time.perf_counter() - started,
            findings=findings.finalize(),
        )

    try:
        segments = validate_time_map(request.get("time_map"))
    except ValueError:
        findings.add({"kind": "analysis_incomplete"})
        return _base_receipt(
            scope=scope,
            source_sha256=source_sha,
            media_sha256=media_sha,
            frame_size=(1, 1),
            elapsed_s=time.perf_counter() - started,
            findings=findings.finalize(),
        )

    duration = segments[-1]["output_end_s"]
    output_timestamps = sample_output_timestamps(duration)
    cv2_module = _load_cv2()
    if cv2_module is None:
        findings.add({"kind": "analysis_incomplete"})
        samples = [_safe_sample(at, map_output_to_source(segments, at) or 0.0) for at in output_timestamps]
        return _base_receipt(
            scope=scope,
            source_sha256=source_sha,
            media_sha256=media_sha,
            frame_size=(1, 1),
            elapsed_s=time.perf_counter() - started,
            samples=samples,
            findings=findings.finalize(),
        )

    scratch: Path | None = None
    readers: list[_VideoReader] = []
    samples: list[dict[str, Any]] = []
    frame_size = (1, 1)
    face_probe_succeeded = False
    ocr_probe_succeeded = False
    try:
        if scratch_path is None:
            findings.add({"kind": "analysis_incomplete"})
        else:
            try:
                scratch_path.mkdir(parents=True, exist_ok=True)
                if not scratch_path.is_dir():
                    raise OSError("scratch is not a directory")
                scratch = scratch_path
            except (OSError, ValueError):
                findings.add({"kind": "analysis_incomplete"})

        composite_reader: _VideoReader | None = None
        source_reader: _VideoReader | None = None
        reference_reader: _VideoReader | None = None
        if composite_path is not None:
            try:
                composite_reader = _VideoReader(composite_path, cv2_module)
                readers.append(composite_reader)
                frame_size = (composite_reader.width, composite_reader.height)
            except (OSError, ValueError):
                findings.add({"kind": "analysis_incomplete"})
        if source_path is not None:
            try:
                source_reader = _VideoReader(source_path, cv2_module)
                readers.append(source_reader)
            except (OSError, ValueError):
                findings.add({"kind": "source_subject_unmeasured"})
        else:
            findings.add({"kind": "source_subject_unmeasured"})
        if reference_path is not None:
            try:
                reference_reader = _VideoReader(reference_path, cv2_module)
                readers.append(reference_reader)
            except (OSError, ValueError):
                findings.add({"kind": "reference_unavailable"})
        else:
            findings.add({"kind": "reference_unavailable"})

        detector = _YuNetProbe(model_path, cv2_module)
        if detector.failed:
            findings.add({"kind": "analysis_incomplete"})

        for index, output_at in enumerate(output_timestamps):
            source_at = map_output_to_source(segments, output_at)
            if source_at is None:
                findings.add({"kind": "analysis_incomplete"})
                continue
            sample = _safe_sample(output_at, source_at)
            composite_frame = composite_reader.read(output_at) if composite_reader is not None else None
            source_frame = source_reader.read(source_at) if source_reader is not None else None
            reference_frame = reference_reader.read(output_at) if reference_reader is not None else None
            if composite_frame is None or source_frame is None or reference_frame is None:
                if composite_frame is None:
                    findings.add({"kind": "analysis_incomplete", "output_at": output_at, "source_at": source_at})
                if source_frame is None:
                    findings.add({"kind": "source_subject_unmeasured", "output_at": output_at, "source_at": source_at})
                if reference_frame is None:
                    findings.add({"kind": "reference_unavailable", "output_at": output_at, "source_at": source_at})
                samples.append(sample)
                continue

            try:
                composite_size = _frame_size(composite_frame.shape[1], composite_frame.shape[0])
                reference_size = _frame_size(reference_frame.shape[1], reference_frame.shape[0])
                if frame_size == (1, 1):
                    frame_size = composite_size
                source_ok, source_boxes = detector.detect(source_frame)
                reference_ok, reference_boxes_raw = detector.detect(reference_frame)
                visible_ok, visible_boxes = detector.detect(composite_frame)
                face_probe_succeeded = face_probe_succeeded or source_ok or reference_ok or visible_ok
                reference_boxes = [
                    scaled
                    for box in reference_boxes_raw
                    if (scaled := _scale_box(box, reference_size, frame_size)) is not None
                ]
                sample["source_face_count"] = len(source_boxes)
                sample["reference_face_count"] = len(reference_boxes)
                sample["visible_face_count"] = len(visible_boxes)
                source_unmeasured = not source_ok or not source_boxes
                if source_unmeasured:
                    findings.add({"kind": "source_subject_unmeasured", "output_at": output_at, "source_at": source_at})
                if not reference_ok:
                    findings.add({"kind": "reference_unavailable", "output_at": output_at, "source_at": source_at})
                elif not reference_boxes and not source_unmeasured:
                    findings.add({"kind": "source_subject_unmeasured", "output_at": output_at, "source_at": source_at})
                if not visible_ok:
                    findings.add({"kind": "analysis_incomplete", "output_at": output_at, "source_at": source_at})
                elif reference_boxes and not visible_boxes:
                    findings.add({"kind": "visibility_uncertain", "output_at": output_at, "source_at": source_at})
                elif reference_boxes:
                    for subject_box in reference_boxes:
                        matched = any(rect_iou(subject_box, visible_box) >= VISIBLE_FACE_IOU for visible_box in visible_boxes)
                        if not matched:
                            findings.add({
                                "kind": "visibility_uncertain",
                                "output_at": output_at,
                                "source_at": source_at,
                                "subject_box": subject_box,
                            })

                ocr_status = "failed"
                text_boxes: list[dict[str, float]] = []
                if scratch is not None:
                    frame_file = scratch / f"composite-check-{index:02d}.png"
                    try:
                        if bool(cv2_module.imwrite(str(frame_file), composite_frame)):
                            ocr_status, text_boxes = _vision_ocr_boxes(frame_file, frame_size[0], frame_size[1])
                            ocr_probe_succeeded = ocr_probe_succeeded or ocr_status in {"ok", "empty"}
                        else:
                            ocr_status = "failed"
                    except Exception:
                        ocr_status = "failed"
                    finally:
                        try:
                            frame_file.unlink(missing_ok=True)
                        except OSError:
                            pass
                expected_text = _expected_text_at(request.get("expected_text"), index)
                if ocr_status == "failed" or (ocr_status == "empty" and expected_text):
                    findings.add({"kind": "ocr_unavailable", "output_at": output_at, "source_at": source_at})
                sample["text_box_count"] = len(text_boxes)
                if reference_boxes and text_boxes:
                    for subject_box in reference_boxes:
                        text_box = _strongest_text_overlap(subject_box, text_boxes)
                        if text_box is not None:
                            findings.add({
                                "kind": "text_subject_overlap",
                                "output_at": output_at,
                                "source_at": source_at,
                                "subject_box": subject_box,
                                "text_box": text_box,
                            })
                sample["measured"] = bool(
                    source_ok
                    and reference_ok
                    and visible_ok
                    and source_boxes
                    and reference_boxes
                    and visible_boxes
                    and ((ocr_status == "ok" and bool(text_boxes)) or (ocr_status == "empty" and not expected_text))
                    and all(any(rect_iou(subject, visible) >= VISIBLE_FACE_IOU for visible in visible_boxes) for subject in reference_boxes)
                )
            except Exception:
                findings.add({"kind": "analysis_incomplete", "output_at": output_at, "source_at": source_at})
            samples.append(sample)
    finally:
        for reader in readers:
            reader.close()
        if scratch is not None:
            # Only generated names are touched; the manager owns everything
            # else in the scratch directory.
            for frame_file in scratch.glob("composite-check-*.png"):
                try:
                    frame_file.unlink()
                except OSError:
                    pass

    finalized_findings = findings.finalize()
    unmeasured_kinds = {
        "visibility_uncertain",
        "source_subject_unmeasured",
        "ocr_unavailable",
        "reference_unavailable",
        "analysis_incomplete",
        "full_timeline_required",
    }
    if not samples or any(sample.get("measured") is not True for sample in samples):
        status = "unmeasured"
    elif any(item.get("kind") in unmeasured_kinds for item in finalized_findings):
        status = "unmeasured"
    elif finalized_findings:
        status = "warn"
    else:
        status = "pass"
    return _base_receipt(
        scope=scope,
        source_sha256=source_sha,
        media_sha256=media_sha,
        frame_size=frame_size,
        elapsed_s=time.perf_counter() - started,
        samples=samples,
        findings=finalized_findings,
        status=status,
        engines={
            "faces": "opencv-yunet" if face_probe_succeeded else "unavailable",
            "ocr": "macos-vision" if ocr_probe_succeeded else "unavailable",
        },
    )

