#!/usr/bin/env python3
"""Follow the speaker so a landscape frame survives a vertical crop.

A 16:9 lesson cropped to 9:16 down the middle loses more than half its
width. On this footage that is the blackboard, and whichever side the
teacher is standing on. Centre-cropping is not a neutral default; it is a
decision to discard whatever is not in the middle.

So sample the cut, find the person, and move the crop window with them.
Detection is macOS Vision — the same framework the OCR stage already uses,
so no new dependency — with faces preferred over bodies because a face is
what the eye tracks and it survives arm movement.

The track is deliberately lazy: it holds still while the subject stays
inside a comfortable band and only then eases across. A window that chases
every detection jitters, which reads worse than not tracking at all.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any

import vision_ocr

SAMPLE_FPS = 2.0
MAX_SAMPLES = 240
# The subject may drift this far from the window centre before it moves.
DEADZONE = 0.12
# Per-sample easing towards the target, in fractions of window width.
MAX_STEP = 0.035
# A face this much smaller than the largest is a bystander, not the speaker.
MIN_RELATIVE_AREA = 0.35


def _detect(image_path: Path, timeout_s: float = 8.0) -> list[dict[str, float]]:
    """Boxes for faces if any, else people, in Vision's bottom-left space."""
    modules = vision_ocr._load_vision()  # same guarded loader as the OCR stage
    if not modules:
        return []
    foundation, quartz, vision = modules
    url = foundation.NSURL.fileURLWithPath_(str(image_path))
    source = quartz.CGImageSourceCreateWithURL(url, None)
    if source is None:
        return []
    image = quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
    if image is None:
        return []
    handler = vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, None)
    for factory in (
        vision.VNDetectFaceRectanglesRequest,
        vision.VNDetectHumanRectanglesRequest,
    ):
        request = factory.alloc().init()
        ok, _ = handler.performRequests_error_([request], None)
        if not ok:
            continue
        boxes: list[dict[str, float]] = []
        for observation in request.results() or []:
            box = observation.boundingBox()
            boxes.append(
                {
                    "x": float(box.origin.x),
                    "y": float(box.origin.y),
                    "width": float(box.size.width),
                    "height": float(box.size.height),
                }
            )
        if boxes:
            return boxes
    return []


def primary_center_x(boxes: list[dict[str, float]]) -> float | None:
    """Centre of the subject: the biggest box, ignoring smaller bystanders."""
    if not boxes:
        return None
    areas = [(box["width"] * box["height"], box) for box in boxes]
    largest = max(area for area, _ in areas)
    if largest <= 0:
        return None
    kept = [box for area, box in areas if area >= largest * MIN_RELATIVE_AREA]
    return sum(box["x"] + box["width"] / 2.0 for box in kept) / len(kept)


def sample_track(
    source: Path,
    start: float,
    end: float,
    *,
    fps: float = SAMPLE_FPS,
    ffmpeg: str | None = None,
) -> list[tuple[float, float]]:
    """[(seconds_into_the_cut, subject_centre_x)] for frames with a subject."""
    from video_analyzer import extract_frame, ffmpeg_path  # local: import cycle

    _ = ffmpeg or ffmpeg_path()
    span = max(0.0, end - start)
    if span <= 0:
        return []
    count = min(MAX_SAMPLES, max(2, int(span * fps)))
    track: list[tuple[float, float]] = []
    with tempfile.TemporaryDirectory(prefix="auto-edit-track-") as scratch:
        for index in range(count):
            offset = span * (index + 0.5) / count
            frame = Path(scratch) / f"t{index:04d}.png"
            try:
                extract_frame(source, start + offset, frame)
            except (OSError, subprocess.SubprocessError, ValueError):
                continue
            center = primary_center_x(_detect(frame))
            if center is not None:
                track.append((round(offset, 3), round(center, 5)))
    return track


def smooth_track(
    track: list[tuple[float, float]],
    window_width_fraction: float,
    *,
    deadzone: float = DEADZONE,
    max_step: float = MAX_STEP,
) -> list[tuple[float, float]]:
    """Turn detections into a lazy window path, in normalised window centres.

    Holds still while the subject stays within the deadzone, then eases —
    a window that chases every detection jitters, and jitter reads worse
    than a fixed frame.
    """
    if not track:
        return []
    half = window_width_fraction / 2.0
    lower, upper = half, 1.0 - half
    if lower >= upper:  # window is the whole frame; nothing to track
        return []
    current = min(max(track[0][1], lower), upper)
    path: list[tuple[float, float]] = [(track[0][0], current)]
    for moment, target in track[1:]:
        desired = min(max(target, lower), upper)
        drift = desired - current
        if abs(drift) > deadzone * window_width_fraction:
            step = max(-max_step, min(max_step, drift))
            current = min(max(current + step, lower), upper)
        path.append((moment, round(current, 5)))
    return path


def crop_x_expression(
    path: list[tuple[float, float]],
    *,
    scaled_width: float,
    window_width: float,
) -> str | None:
    """A piecewise ffmpeg expression for the crop's left edge over time.

    Nested conditionals rather than a lookup table because the crop filter
    evaluates an expression per frame and has nowhere to hold a track.
    """
    if not path:
        return None
    travel = max(0.0, scaled_width - window_width)
    if travel <= 1.0:
        return None

    def left_edge(center: float) -> float:
        return min(max(center * scaled_width - window_width / 2.0, 0.0), travel)

    # Collapse runs that resolve to the same pixel; a static stretch does not
    # need a branch, and the expression is evaluated for every frame.
    points: list[tuple[float, int]] = []
    for moment, center in path:
        value = int(round(left_edge(center)))
        if points and points[-1][1] == value:
            continue
        points.append((moment, value))
    if len(points) == 1:
        return str(points[0][1])
    expression = str(points[-1][1])
    for moment, value in reversed(points[:-1]):
        expression = f"if(lt(t,{moment:.3f}),{value},{expression})"
    return expression


def subject_head_top(source: Path, start: float, end: float, *, fps: float = 1.0) -> float | None:
    """Highest point the subject reaches, as a fraction from the top.

    A standing speaker fills the frame from the head down, so the only
    reliably empty region is above them. This returns where that region
    ends — the topmost head position across the cut, not the average, so a
    card placed above it stays clear even at the subject's tallest moment.
    """
    from video_analyzer import extract_frame  # local: import cycle

    span = max(0.0, end - start)
    if span <= 0:
        return None
    count = min(MAX_SAMPLES, max(2, int(span * fps)))
    tops: list[float] = []
    with tempfile.TemporaryDirectory(prefix="auto-edit-head-") as scratch:
        for index in range(count):
            frame = Path(scratch) / f"h{index:04d}.png"
            try:
                extract_frame(source, start + span * (index + 0.5) / count, frame)
            except (OSError, subprocess.SubprocessError, ValueError):
                continue
            boxes = _detect(frame)
            if not boxes:
                continue
            primary = max(boxes, key=lambda box: box["width"] * box["height"])
            # Vision measures from the bottom; screen coordinates run down.
            tops.append(1.0 - (primary["y"] + primary["height"]))
    if not tops:
        return None
    return round(min(tops), 5)


def card_y_percent(
    head_top: float | None,
    *,
    card_height_fraction: float,
    caption_top: float,
    default: float,
) -> tuple[float, str]:
    """Where the card's centre sits, as a percentage from the top.

    Returns the default and a reason when there is nowhere better; a caller
    that cannot tell "placed deliberately" from "left where it was" will
    report a collision as a layout choice.
    """
    if head_top is None:
        return default, "no_subject_found"
    margin = card_height_fraction * 0.35
    # Centre the card in the clear band above the head, if it fits there.
    if head_top >= card_height_fraction + margin * 2:
        return round(max(margin, (head_top - card_height_fraction) / 2) * 100 + card_height_fraction * 50, 2), "above_subject"
    # Otherwise sit between the subject and the captions, still clear of both.
    below = caption_top - card_height_fraction - margin
    if below > head_top:
        return round(below * 100, 2), "above_captions"
    return default, "no_clear_band"


def tracked_crop_x(
    source: Path,
    start: float,
    end: float,
    *,
    scaled_width: float,
    window_width: float,
    fps: float = SAMPLE_FPS,
) -> tuple[str | None, dict[str, Any]]:
    """(crop x expression or None, a report of what was seen).

    None means "centre the crop": no subject found, or the window already
    covers the frame. The report says which, because silently centring looks
    identical to tracking a subject who never moved.
    """
    if window_width >= scaled_width - 1.0:
        return None, {"status": "no_crop_needed", "samples": 0, "detections": 0}
    track = sample_track(source, start, end, fps=fps)
    total = max(1, min(MAX_SAMPLES, max(2, int(max(0.0, end - start) * fps))))
    if not track:
        return None, {"status": "no_subject_found", "samples": total, "detections": 0}
    path = smooth_track(track, window_width / scaled_width)
    expression = crop_x_expression(
        path, scaled_width=scaled_width, window_width=window_width
    )
    if expression is None:
        return None, {
            "status": "subject_static",
            "samples": total,
            "detections": len(track),
        }
    return expression, {
        "status": "tracked",
        "samples": total,
        "detections": len(track),
        "keyframes": len(path),
    }
