#!/usr/bin/env python3
"""Independently attribute declared motion to frozen graphic bytes.

The renderer supplies a private, lossless base visual from the same FFmpeg
invocation, split before structured major graphics.  QA rebuilds each sampled
composite from that base plus hash-bound graphic bytes and the exact declared
transform.  Background pixels therefore cannot earn graphic-motion credit.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import hashlib
import json
import math
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any, Iterator


SAMPLE_RATE = 48_000
SAMPLE_FRACTIONS = (0.1, 0.5, 0.9)
SSIM_MAX = 0.985
CHANGED_PIXEL_MIN = 0.02


class VisualMotionProbeError(ValueError):
    """The video or its declared motion evidence cannot be measured safely."""


def _tool(name: str) -> str:
    resolved = shutil.which(name)
    if resolved is None:
        raise VisualMotionProbeError(f"required media tool is unavailable: {name}")
    return resolved


@contextmanager
def _bound_file(path: Path, expected_sha256: str | None, label: str) -> Iterator[int]:
    entry = path.expanduser()
    try:
        linked = os.lstat(entry)
    except OSError as exc:
        raise VisualMotionProbeError(f"{label} cannot be inspected: {entry}") from exc
    if stat.S_ISLNK(linked.st_mode) or not stat.S_ISREG(linked.st_mode):
        raise VisualMotionProbeError(f"{label} must be a regular non-symlink file")
    resolved = entry.resolve()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(resolved, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise VisualMotionProbeError(f"{label} must be a regular file")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
        after = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or total != before.st_size
        ):
            raise VisualMotionProbeError(f"{label} changed while it was read")
        if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
            raise VisualMotionProbeError(f"{label} bytes do not match frozen SHA-256")
        os.lseek(descriptor, 0, os.SEEK_SET)
        yield descriptor
    except VisualMotionProbeError:
        raise
    except OSError as exc:
        raise VisualMotionProbeError(f"{label} could not be opened safely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _run(command: list[str], descriptors: tuple[int, ...], timeout: int = 60) -> bytes:
    for descriptor in descriptors:
        os.lseek(descriptor, 0, os.SEEK_SET)
    result = subprocess.run(
        command,
        capture_output=True,
        timeout=timeout,
        pass_fds=descriptors,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace")[-500:]
        raise VisualMotionProbeError(f"motion sample could not be decoded: {detail}")
    return result.stdout


def _video_dimensions(descriptor: int) -> tuple[int, int]:
    result = _run(
        [
            _tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "json",
            f"/dev/fd/{descriptor}",
        ],
        (descriptor,),
        timeout=30,
    )
    try:
        stream = json.loads(result)["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise VisualMotionProbeError("video has no measurable visual stream") from exc
    if width <= 0 or height <= 0:
        raise VisualMotionProbeError("video dimensions must be positive")
    return width, height


def _finite_fraction(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VisualMotionProbeError(f"{path} must be a finite number")
    resolved = float(value)
    if not math.isfinite(resolved):
        raise VisualMotionProbeError(f"{path} must be a finite number")
    return resolved


def _crop_geometry(
    roi: Any, frame_width: int, frame_height: int, path: str
) -> tuple[int, int, int, int]:
    if not isinstance(roi, dict) or set(roi) != {"x", "y", "width", "height"}:
        raise VisualMotionProbeError(f"{path} must be an exact normalized rectangle")
    x = _finite_fraction(roi["x"], f"{path}.x")
    y = _finite_fraction(roi["y"], f"{path}.y")
    width = _finite_fraction(roi["width"], f"{path}.width")
    height = _finite_fraction(roi["height"], f"{path}.height")
    if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
        raise VisualMotionProbeError(f"{path} must stay inside the frame")
    left = max(0, min(frame_width - 1, math.floor(x * frame_width)))
    top = max(0, min(frame_height - 1, math.floor(y * frame_height)))
    right = max(left + 1, min(frame_width, math.ceil((x + width) * frame_width)))
    bottom = max(top + 1, min(frame_height, math.ceil((y + height) * frame_height)))
    return left, top, right - left, bottom - top


def _exact_sample(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise VisualMotionProbeError(f"{path} must be a non-negative integer sample")
    return value


def _sample_positions(start: int, end: int, fps_value: Any) -> list[int]:
    fps = _exact_sample(fps_value, "motion_input.fps")
    if fps <= 0 or SAMPLE_RATE % fps:
        raise VisualMotionProbeError(
            "motion input fps must divide the evidence sample rate"
        )
    frame_samples = SAMPLE_RATE // fps
    first_frame = (start + frame_samples - 1) // frame_samples
    last_frame = end // frame_samples
    if last_frame - first_frame + 1 < len(SAMPLE_FRACTIONS):
        raise VisualMotionProbeError(
            "motion window contains fewer than three candidate frames"
        )
    positions: list[int] = []
    for fraction in SAMPLE_FRACTIONS:
        target = start + int((end - start) * fraction)
        frame_index = (target + frame_samples // 2) // frame_samples
        frame_index = max(first_frame, min(last_frame, frame_index))
        position = frame_index * frame_samples
        if positions and position <= positions[-1]:
            position = positions[-1] + frame_samples
        positions.append(position)
    if positions[-1] > last_frame * frame_samples:
        positions = [
            (last_frame - len(SAMPLE_FRACTIONS) + 1 + index) * frame_samples
            for index in range(len(SAMPLE_FRACTIONS))
        ]
    return positions


def _pair_metrics(first: bytes, second: bytes) -> tuple[float, float]:
    if len(first) != len(second) or not first:
        raise VisualMotionProbeError("motion sample shapes disagree")
    count = len(first)
    mean_a, mean_b = sum(first) / count, sum(second) / count
    variance_a = sum((value - mean_a) ** 2 for value in first) / count
    variance_b = sum((value - mean_b) ** 2 for value in second) / count
    covariance = sum(
        (left - mean_a) * (right - mean_b)
        for left, right in zip(first, second, strict=True)
    ) / count
    c1, c2 = (0.01 * 255.0) ** 2, (0.03 * 255.0) ** 2
    denominator = (mean_a**2 + mean_b**2 + c1) * (variance_a + variance_b + c2)
    if denominator <= 0 or not math.isfinite(denominator):
        raise VisualMotionProbeError("motion SSIM denominator is invalid")
    ssim = ((2 * mean_a * mean_b + c1) * (2 * covariance + c2)) / denominator
    changed = sum(left != right for left, right in zip(first, second, strict=True)) / count
    return max(-1.0, min(1.0, ssim)), changed


def _graphic_pair_metrics(first: bytes, second: bytes) -> tuple[float, float]:
    """Measure only pixels supported by either frozen RGBA graphic state."""
    if len(first) != len(second) or not first or len(first) % 4:
        raise VisualMotionProbeError("graphic motion sample shapes disagree")
    first_values: list[int] = []
    second_values: list[int] = []
    changed_pixels = 0
    supported_pixels = 0
    for offset in range(0, len(first), 4):
        left = first[offset : offset + 4]
        right = second[offset : offset + 4]
        if left[3] == 0 and right[3] == 0:
            continue
        supported_pixels += 1
        if left != right:
            changed_pixels += 1
        first_values.extend(left)
        second_values.extend(right)
    if supported_pixels == 0:
        raise VisualMotionProbeError("frozen graphic has no alpha-supported pixels")
    ssim, _component_changed = _pair_metrics(
        bytes(first_values), bytes(second_values)
    )
    return ssim, changed_pixels / supported_pixels


def _presence_metrics(
    candidate: bytes,
    rebuilt: bytes,
    frozen_states: list[bytes],
) -> tuple[float, float]:
    """Compare every sample on the union support of all frozen graphic states."""
    if len(candidate) != len(rebuilt) or not candidate or not frozen_states:
        raise VisualMotionProbeError("graphic presence sample shapes disagree")
    if any(len(state) != len(candidate) * 4 for state in frozen_states):
        raise VisualMotionProbeError("graphic presence alpha shapes disagree")
    candidate_values = bytearray()
    rebuilt_values = bytearray()
    for pixel_index in range(len(candidate)):
        alpha_offset = pixel_index * 4 + 3
        if not any(state[alpha_offset] > 0 for state in frozen_states):
            continue
        candidate_values.append(candidate[pixel_index])
        rebuilt_values.append(rebuilt[pixel_index])
    if not candidate_values:
        raise VisualMotionProbeError("frozen graphic has no alpha-supported pixels")
    return _pair_metrics(bytes(candidate_values), bytes(rebuilt_values))


def _frame_from_fd(
    descriptor: int,
    sample_position: int,
    crop: tuple[int, int, int, int],
) -> bytes:
    left, top, width, height = crop
    payload = _run(
        [
            _tool("ffmpeg"), "-hide_banner", "-loglevel", "error",
            "-i", f"/dev/fd/{descriptor}", "-ss", f"{sample_position / SAMPLE_RATE:.9f}",
            "-frames:v", "1", "-vf", f"crop={width}:{height}:{left}:{top},format=gray",
            "-f", "rawvideo", "pipe:1",
        ],
        (descriptor,),
    )
    if len(payload) != width * height:
        raise VisualMotionProbeError("motion sample did not decode to the exact ROI")
    return payload


def _declared_overlay(binding: dict[str, Any]) -> dict[str, Any]:
    placement = binding.get("placement")
    if not isinstance(placement, dict):
        raise VisualMotionProbeError("frozen graphic placement is invalid")
    return {
        "id": "motion-attribution",
        "type": binding.get("source_kind"),
        "start": _exact_sample(binding.get("source_start_sample"), "source_start_sample") / SAMPLE_RATE,
        "end": _exact_sample(binding.get("source_end_sample"), "source_end_sample") / SAMPLE_RATE,
        "style": {
            "width": _finite_fraction(placement.get("width_percent"), "width_percent"),
            "x": _finite_fraction(placement.get("x_percent"), "x_percent"),
            "y": _finite_fraction(placement.get("y_percent"), "y_percent"),
            "animation": str(placement.get("animation") or "none"),
        },
    }


def _rebuilt_frame(
    base_descriptor: int,
    source_descriptor: int,
    binding: dict[str, Any],
    sample_position: int,
    crop: tuple[int, int, int, int],
    fps: int,
    *,
    graphic_only: bool,
) -> bytes:
    from render_editor_timeline import image_filter

    canvas_width = _exact_sample(binding.get("canvas_width"), "canvas_width")
    canvas_height = _exact_sample(binding.get("canvas_height"), "canvas_height")
    left, top, width, height = crop
    command = [_tool("ffmpeg"), "-hide_banner", "-loglevel", "error"]
    descriptors: tuple[int, ...]
    if graphic_only:
        command.extend(
            [
                "-f", "lavfi", "-i",
                f"color=c=black@0.0:s={canvas_width}x{canvas_height}:r={fps},format=rgba",
            ]
        )
        descriptors = (source_descriptor,)
    else:
        command.extend(["-i", f"/dev/fd/{base_descriptor}"])
        descriptors = (base_descriptor, source_descriptor)
    if binding.get("source_kind") == "image":
        command.extend(["-loop", "1", "-framerate", str(fps)])
    else:
        command.extend(["-stream_loop", "-1"])
    command.extend(["-i", f"/dev/fd/{source_descriptor}"])
    overlay = _declared_overlay(binding)
    graph = image_filter("0:v", "rebuilt", "1:v", overlay, canvas_width, canvas_height)
    pixel_format = "rgba" if graphic_only else "gray"
    graph += (
        f";[rebuilt]crop={width}:{height}:{left}:{top},"
        f"format={pixel_format}[out]"
    )
    command.extend(
        [
            "-filter_complex", graph, "-map", "[out]",
            "-ss", f"{sample_position / SAMPLE_RATE:.9f}", "-frames:v", "1",
            "-f", "rawvideo", "pipe:1",
        ]
    )
    payload = _run(command, descriptors)
    expected_size = width * height * (4 if graphic_only else 1)
    if len(payload) != expected_size:
        raise VisualMotionProbeError("rebuilt motion sample has the wrong ROI shape")
    return payload


def measure_declared_motion(
    video_path: str | Path,
    renderer_evidence: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Return independently rebuilt final-pixel motion keyed by major scene ID."""
    if not isinstance(renderer_evidence, dict):
        raise VisualMotionProbeError("renderer evidence must be an object")
    items = renderer_evidence.get("items")
    if not isinstance(items, list):
        raise VisualMotionProbeError("renderer evidence items must be a list")
    if not any(
        isinstance(item, dict) and item.get("major_graphic") is True
        for item in items
    ):
        return {}
    motion_input = renderer_evidence.get("motion_input")
    frozen_graphics = renderer_evidence.get("frozen_graphics")
    motion_attribution = renderer_evidence.get("motion_attribution")
    if (
        not isinstance(motion_input, dict)
        or not isinstance(frozen_graphics, dict)
        or not isinstance(motion_attribution, dict)
    ):
        raise VisualMotionProbeError("renderer evidence lacks private motion inputs")
    base_path = Path(str(motion_input.get("base_path") or ""))
    base_sha256 = str(motion_input.get("base_sha256") or "")
    with ExitStack() as stack:
        candidate_fd = stack.enter_context(_bound_file(Path(video_path), None, "candidate video"))
        base_fd = stack.enter_context(_bound_file(base_path, base_sha256, "motion base visual"))
        frame_width, frame_height = _video_dimensions(candidate_fd)
        if (frame_width, frame_height) != (
            motion_input.get("canvas_width"), motion_input.get("canvas_height")
        ):
            raise VisualMotionProbeError("motion base canvas differs from candidate")
        measured: dict[str, dict[str, Any]] = {}
        for index, item in enumerate(items):
            path = f"items[{index}]"
            if not isinstance(item, dict):
                raise VisualMotionProbeError(f"{path} must be an object")
            if item.get("major_graphic") is not True:
                continue
            if item.get("static_fallback") is not False:
                raise VisualMotionProbeError(f"{path} major graphic is a static fallback")
            scene_id = item.get("id")
            if not isinstance(scene_id, str) or not scene_id or scene_id in measured:
                raise VisualMotionProbeError(f"{path}.id must be unique and non-empty")
            start = _exact_sample(item.get("motion_window_start_sample"), f"{path}.motion_window_start_sample")
            end = _exact_sample(item.get("motion_window_end_sample"), f"{path}.motion_window_end_sample")
            if end <= start:
                raise VisualMotionProbeError(f"{path} motion window must have positive duration")
            binding = frozen_graphics.get(scene_id)
            public_binding = motion_attribution.get(scene_id)
            if not isinstance(binding, dict):
                raise VisualMotionProbeError(f"{path} has no frozen graphic input")
            if (
                not isinstance(public_binding, dict)
                or {key: value for key, value in binding.items() if key != "source_path"}
                != public_binding
                or binding.get("artifact_sha256") != item.get("artifact_hash")
            ):
                raise VisualMotionProbeError(f"{path} frozen graphic is not authority-bound")
            source_fd = stack.enter_context(
                _bound_file(
                    Path(str(binding.get("source_path") or "")),
                    str(binding.get("source_sha256") or ""),
                    f"frozen graphic {scene_id}",
                )
            )
            positions = _sample_positions(start, end, motion_input.get("fps"))
            crop = _crop_geometry(item.get("graphic_roi"), frame_width, frame_height, f"{path}.graphic_roi")
            candidate_frames = [_frame_from_fd(candidate_fd, position, crop) for position in positions]
            fps = _exact_sample(motion_input.get("fps"), "motion_input.fps")
            rebuilt_frames = [
                _rebuilt_frame(
                    base_fd,
                    source_fd,
                    binding,
                    position,
                    crop,
                    fps,
                    graphic_only=False,
                )
                for position in positions
            ]
            graphic_frames = [
                _rebuilt_frame(
                    base_fd,
                    source_fd,
                    binding,
                    position,
                    crop,
                    fps,
                    graphic_only=True,
                )
                for position in positions
            ]
            candidate_matches: list[dict[str, Any]] = []
            all_matched = True
            for position, candidate, rebuilt in zip(
                positions, candidate_frames, rebuilt_frames, strict=True
            ):
                candidate_ssim, _changed = _presence_metrics(
                    candidate, rebuilt, graphic_frames
                )
                matched = candidate_ssim >= SSIM_MAX
                all_matched = all_matched and matched
                candidate_matches.append(
                    {"sample": position, "ssim": round(candidate_ssim, 6), "matched": matched}
                )
            pairs: list[dict[str, Any]] = []
            detected = False
            for left_index, right_index in ((0, 1), (1, 2), (0, 2)):
                raw_ssim, raw_changed = _graphic_pair_metrics(
                    graphic_frames[left_index], graphic_frames[right_index]
                )
                pair_detected = (
                    all_matched
                    and raw_ssim < SSIM_MAX
                    and raw_changed >= CHANGED_PIXEL_MIN
                )
                detected = detected or pair_detected
                pairs.append(
                    {
                        "left_sample": positions[left_index],
                        "right_sample": positions[right_index],
                        "ssim": round(raw_ssim, 6),
                        "changed_pixel_fraction": round(raw_changed, 6),
                        "detected": pair_detected,
                    }
                )
            measured[scene_id] = {
                "sample_positions": positions,
                "graphic_roi": dict(item["graphic_roi"]),
                "candidate_matches": candidate_matches,
                "pairs": pairs,
                "detected": detected,
            }
        if set(frozen_graphics) != set(measured) or set(motion_attribution) != set(
            measured
        ):
            raise VisualMotionProbeError(
                "motion input scene identities differ from measured major graphics"
            )
        return measured
