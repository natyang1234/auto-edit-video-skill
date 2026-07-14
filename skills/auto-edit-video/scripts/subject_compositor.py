#!/usr/bin/env python3
"""Locally extract a foreground subject and composite it over a project background.

This worker is launched with the dedicated rembg virtualenv. It never calls a
remote service and deliberately writes a video-only intermediate; the final
timeline renderer retains audio from the original source.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, BinaryIO


def calculate_working_size(width: int, height: int, maximum_side: int = 640) -> tuple[int, int]:
    if width <= 0 or height <= 0 or maximum_side < 64:
        raise ValueError("source dimensions and working size must be positive")
    scale = min(1.0, maximum_side / max(width, height))
    result_width = max(2, round(width * scale / 2) * 2)
    result_height = max(2, round(height * scale / 2) * 2)
    return result_width, result_height


def background_video_filter(width: int, height: int, fit: str, blur: float) -> str:
    if fit == "cover":
        chain = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={width}:{height}"
        )
    elif fit == "contain":
        chain = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
        )
    else:
        raise ValueError("background fit must be cover or contain")
    if blur > 0:
        chain += f",gblur=sigma={min(float(blur), 20.0):.3f}"
    return chain


def subject_paste_box(
    *,
    crop_size: tuple[int, int],
    canvas_size: tuple[int, int],
    x_percent: float,
    y_percent: float,
    user_scale: float,
) -> tuple[int, int, int, int]:
    crop_width, crop_height = crop_size
    canvas_width, canvas_height = canvas_size
    if min(crop_width, crop_height, canvas_width, canvas_height) <= 0:
        raise ValueError("subject and canvas dimensions must be positive")
    target_height = max(2, round(canvas_height * 0.82 * float(user_scale)))
    target_width = max(2, round(target_height * crop_width / crop_height))
    center_x = canvas_width * float(x_percent) / 100
    center_y = canvas_height * float(y_percent) / 100
    left = round(center_x - target_width / 2)
    top = round(center_y - target_height / 2)
    return left, top, target_width, target_height


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _probe_dimensions(ffprobe: str, source: Path) -> tuple[int, int]:
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "json",
            str(source),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "ffprobe could not read source dimensions")[-2000:])
    payload = json.loads(result.stdout)
    streams = payload.get("streams") or []
    if not streams:
        raise RuntimeError("source has no visual stream")
    return int(streams[0]["width"]), int(streams[0]["height"])


def _decoder_command(
    ffmpeg: str,
    source: Path,
    *,
    start: float,
    duration: float,
    fps: float,
    width: int,
    height: int,
) -> list[str]:
    return [
        ffmpeg,
        "-v",
        "error",
        "-ss",
        f"{start:.4f}",
        "-t",
        f"{duration:.4f}",
        "-i",
        str(source),
        "-vf",
        f"fps={fps:.6f},scale={width}:{height}:flags=lanczos",
        "-an",
        "-pix_fmt",
        "rgb24",
        "-f",
        "rawvideo",
        "pipe:1",
    ]


def _background_decoder_command(
    ffmpeg: str,
    source: Path,
    *,
    mode: str,
    duration: float,
    fps: float,
    width: int,
    height: int,
    fit: str,
    blur: float,
) -> list[str]:
    loop_args = ["-loop", "1"] if mode == "image" else ["-stream_loop", "-1"]
    return [
        ffmpeg,
        "-v",
        "error",
        *loop_args,
        "-i",
        str(source),
        "-t",
        f"{duration:.4f}",
        "-vf",
        f"fps={fps:.6f},{background_video_filter(width, height, fit, blur)}",
        "-an",
        "-pix_fmt",
        "rgb24",
        "-f",
        "rawvideo",
        "pipe:1",
    ]


def _mask_image(value: Any, image_module: Any) -> Any:
    if hasattr(value, "convert"):
        return value.convert("L")
    if isinstance(value, bytes):
        return image_module.open(io.BytesIO(value)).convert("L")
    raise RuntimeError("local cutout engine returned an unsupported mask")


def _padded_bbox(mask: Any) -> tuple[int, int, int, int] | None:
    bbox = mask.getbbox()
    if not bbox:
        return None
    left, top, right, bottom = bbox
    padding = max(2, round(max(right - left, bottom - top) * 0.035))
    return (
        max(0, left - padding),
        max(0, top - padding),
        min(mask.width, right + padding),
        min(mask.height, bottom + padding),
    )


def run_compositor(args: argparse.Namespace) -> int:
    try:
        import numpy as np
        from PIL import Image, ImageFilter
        from rembg import new_session, remove
    except ImportError as exc:
        raise RuntimeError("subject compositor must run inside the local rembg environment") from exc

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("ffmpeg and ffprobe are required for subject compositing")
    source = Path(args.source).resolve()
    output = Path(args.output).resolve()
    if not source.is_file():
        raise ValueError("subject source is missing")
    model_home = Path(args.model_home).expanduser().resolve()
    model = model_home / "isnet-general-use.onnx"
    if not model.is_file():
        raise ValueError("local isnet-general-use model is missing; remote download is disabled")
    os.environ["U2NET_HOME"] = str(model_home)
    background_source = Path(args.background_source).resolve() if args.background_source else None
    if args.background_mode in {"image", "video"} and (
        background_source is None or not background_source.is_file()
    ):
        raise ValueError(f"{args.background_mode} background source is missing")

    source_width, source_height = _probe_dimensions(ffprobe, source)
    working_width, working_height = calculate_working_size(
        source_width, source_height, args.working_size
    )
    source_decoder = subprocess.Popen(
        _decoder_command(
            ffmpeg,
            source,
            start=args.start,
            duration=args.duration,
            fps=args.fps,
            width=working_width,
            height=working_height,
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    background_decoder: subprocess.Popen[bytes] | None = None
    if background_source is not None:
        background_decoder = subprocess.Popen(
            _background_decoder_command(
                ffmpeg,
                background_source,
                mode=args.background_mode,
                duration=args.duration,
                fps=args.fps,
                width=args.width,
                height=args.height,
                fit=args.background_fit,
                blur=args.background_blur,
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    encoder_log = tempfile.TemporaryFile(mode="w+b")
    encoder = subprocess.Popen(
        [
            ffmpeg,
            "-y",
            "-v",
            "error",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-s:v",
            f"{args.width}x{args.height}",
            "-r",
            f"{args.fps:.6f}",
            "-i",
            "pipe:0",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-g",
            str(max(1, round(args.fps))),
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=encoder_log,
    )
    source_frame_bytes = working_width * working_height * 3
    canvas_frame_bytes = args.width * args.height * 3
    solid = Image.new("RGB", (args.width, args.height), args.background_color)
    session = new_session("isnet-general-use")
    last_mask = None
    frame_count = 0
    try:
        if source_decoder.stdout is None or encoder.stdin is None:
            raise RuntimeError("could not open local video pipes")
        while True:
            raw_source = _read_exact(source_decoder.stdout, source_frame_bytes)
            if len(raw_source) != source_frame_bytes:
                break
            source_image = Image.frombytes(
                "RGB", (working_width, working_height), raw_source
            )
            if last_mask is None or frame_count % args.mask_stride == 0:
                mask_value = remove(
                    source_image,
                    session=session,
                    only_mask=True,
                    post_process_mask=True,
                )
                last_mask = _mask_image(mask_value, Image)
                if args.feather > 0:
                    last_mask = last_mask.filter(ImageFilter.GaussianBlur(args.feather))
            if background_decoder is not None:
                if background_decoder.stdout is None:
                    raise RuntimeError("could not open background video pipe")
                raw_background = _read_exact(background_decoder.stdout, canvas_frame_bytes)
                if len(raw_background) != canvas_frame_bytes:
                    raise RuntimeError("background ended before the selected clip")
                canvas = Image.frombytes("RGB", (args.width, args.height), raw_background)
            else:
                canvas = solid.copy()
            bbox = _padded_bbox(last_mask)
            if bbox:
                subject_rgb = source_image.crop(bbox)
                subject_mask = last_mask.crop(bbox)
                left, top, target_width, target_height = subject_paste_box(
                    crop_size=subject_rgb.size,
                    canvas_size=(args.width, args.height),
                    x_percent=args.subject_x,
                    y_percent=args.subject_y,
                    user_scale=args.subject_scale,
                )
                subject_rgb = subject_rgb.resize(
                    (target_width, target_height), Image.Resampling.LANCZOS
                )
                subject_mask = subject_mask.resize(
                    (target_width, target_height), Image.Resampling.LANCZOS
                )
                rgba = subject_rgb.convert("RGBA")
                rgba.putalpha(subject_mask)
                canvas.paste(rgba, (left, top), rgba)
            encoder.stdin.write(np.asarray(canvas, dtype=np.uint8).tobytes())
            frame_count += 1
            if frame_count % max(1, round(args.fps)) == 0:
                print(json.dumps({"frames": frame_count}), flush=True)
        encoder.stdin.close()
        encoder.stdin = None
        encoder_return = encoder.wait(timeout=15 * 60)
        source_return = source_decoder.wait(timeout=60)
        if background_decoder is not None:
            background_return = background_decoder.wait(timeout=60)
        else:
            background_return = 0
        if frame_count == 0 or source_return != 0 or background_return != 0 or encoder_return != 0:
            encoder_log.seek(0)
            detail = encoder_log.read().decode("utf-8", errors="replace")[-3000:]
            raise RuntimeError(detail or "local subject compositor failed")
    finally:
        for process in (source_decoder, background_decoder, encoder):
            if process is not None and process.poll() is None:
                process.kill()
        encoder_log.close()
    print(json.dumps({"ok": True, "frames": frame_count, "output": str(output)}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local video subject compositor")
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--width", type=int, default=1080)
    parser.add_argument("--height", type=int, default=1920)
    parser.add_argument("--working-size", type=int, default=640)
    parser.add_argument("--model-home", required=True)
    parser.add_argument("--background-mode", choices=("solid", "image", "video"), required=True)
    parser.add_argument("--background-source")
    parser.add_argument("--background-color", default="#17251d")
    parser.add_argument("--background-fit", choices=("cover", "contain"), default="cover")
    parser.add_argument("--background-blur", type=float, default=0.0)
    parser.add_argument("--subject-x", type=float, default=50.0)
    parser.add_argument("--subject-y", type=float, default=54.0)
    parser.add_argument("--subject-scale", type=float, default=1.0)
    parser.add_argument("--feather", type=float, default=2.0)
    parser.add_argument("--mask-stride", type=int, default=3)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if (
        args.start < 0
        or args.duration <= 0
        or not 1 <= args.fps <= 240
        or min(args.width, args.height) < 240
        or max(args.width, args.height) > 4096
        or not 64 <= args.working_size <= 1920
    ):
        parser.error("duration, fps, and canvas dimensions must be positive")
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", args.background_color):
        parser.error("background color must be a six-digit hex color")
    if not 0.15 <= args.subject_scale <= 3 or not -50 <= args.subject_x <= 150 or not -50 <= args.subject_y <= 150:
        parser.error("subject position or scale is out of bounds")
    if (
        not 0 <= args.feather <= 30
        or not 1 <= args.mask_stride <= 12
        or not 0 <= args.background_blur <= 40
    ):
        parser.error("feather, blur, or mask stride is out of bounds")
    try:
        return run_compositor(args)
    except Exception as exc:
        print(f"subject compositor failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
