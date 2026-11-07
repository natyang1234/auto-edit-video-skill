#!/usr/bin/env python3
"""Render one strict, source-faithful Studio FX preview to a browser MP4.

This is deliberately a small worker boundary: it reads one manager-created
request, writes one atomic result, and never mutates editor state or approvals.
The API owns process groups, authorization and path containment.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import fx_overlay


MAX_SECONDS = 4.0


class PreviewError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


def progress(phase: str, message: str) -> None:
    print(json.dumps({"phase": phase, "message": message}, ensure_ascii=False), file=sys.stderr, flush=True)


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(data, out, ensure_ascii=False, separators=(",", ":"))
            out.write("\n")
            out.flush(); os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


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
    preview_root = (project / "working" / "fx-preview").resolve()
    try:
        source.relative_to(project)
        cache.relative_to(preview_root)
        output.relative_to(preview_root)
    except ValueError as exc:
        raise PreviewError("invalid_request", "預覽來源或輸出不在專案允許的目錄內") from exc
    return project, source, cache, output


def render(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise PreviewError("invalid_request", "預覽請求必須是 JSON 物件")
    _, source, cache, output = request_paths(data)
    overlay = data.get("overlay")
    canvas = data.get("canvas") or {}
    if not isinstance(overlay, dict) or overlay.get("type") != "fx":
        raise PreviewError("invalid_request", "預覽只接受單一 fx 圖層")
    try:
        width, height = int(canvas["width"]), int(canvas["height"])
        start, end = float(overlay["start"]), float(overlay["end"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PreviewError("invalid_request", "預覽缺少有效畫布或效果時間") from exc
    if not fx_overlay.canvas_ok((width, height)):
        raise PreviewError("invalid_canvas", "特效預覽只支援 9:16 畫布")
    fit = str(canvas.get("fit") or "cover")
    if fit not in {"cover", "contain"}:
        raise PreviewError("invalid_canvas", "特效預覽畫面適配只支援 cover 或 contain")
    duration = round(end - start, 3)
    if duration <= .05 or duration > MAX_SECONDS + 1e-6:
        raise PreviewError("invalid_window", "特效預覽時間必須大於 0 且不超過 4 秒")
    code = str(overlay.get("code") or "")
    if code == "B1":
        raise PreviewError("broll_requires_asset", "B1 需要指定補畫面素材，不能以原片假裝補畫面")
    if code not in fx_overlay.codes():
        raise PreviewError("invalid_fx", "找不到指定的特效")
    try:
        info = fx_overlay._compose().probe(str(source))
    except SystemExit as exc:
        raise PreviewError("invalid_source", str(exc)) from exc
    if start < 0 or end > float(info["duration"]) + .001:
        raise PreviewError("invalid_window", "特效時間超出原片範圍")

    cache.mkdir(parents=True, exist_ok=True)
    progress("preparing", "正在準備原片時窗")
    effect = next((x for x in fx_overlay.catalog().get("effects", []) if x["code"] == code), {})
    if effect.get("needs_person") or code == "P5":
        progress("matte", "正在準備人物去背")
    progress("rendering", f"正在以原片時窗渲染 {code}")
    rendered = fx_overlay.render_for_timeline(cache, overlay, source, str(data.get("pack_id") or ""),
                                               "draft", canvas=(width, height), fit=fit)
    if rendered is None:
        reason = "person_unavailable" if effect.get("needs_person") or code == "P5" else "render_failed"
        raise PreviewError(reason, "此時窗無法取得可用人物去背" if reason == "person_unavailable" else "特效渲染失敗")

    alpha = cache / rendered["source"]
    if not alpha.is_file():
        raise PreviewError("render_failed", "特效渲染檔未產生")
    progress("rendering", "正在合成可播放預覽")
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(f".{output.stem}.{os.getpid()}.partial.mp4")
    vf = ("scale=1080:1920:force_original_aspect_ratio=decrease,"
          "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=0x171512,setsar=1" if fit == "contain"
          else "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1")
    cmd = [
        "ffmpeg", "-v", "error", "-y", "-ss", f"{start:.3f}", "-i", str(source), "-i", str(alpha),
        "-t", f"{duration:.3f}", "-filter_complex", f"[0:v]{vf}[base];[base][1:v]overlay=0:0:shortest=1[v]",
        "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", "-shortest", str(tmp),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=20 * 60, check=False)
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
    return {"ok": True, "out": str(output), "duration_s": duration, "code": code,
            "cached": bool(rendered.get("cached")), "effect_seconds": rendered.get("seconds")}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--request", required=True)
    p.add_argument("--result", required=True)
    args = p.parse_args()
    result = Path(args.result).resolve()
    try:
        with open(args.request, encoding="utf-8") as src:
            data = json.load(src)
        rep = render(data)
        atomic_json(result, rep)
        return 0
    except PreviewError as exc:
        atomic_json(result, {"ok": False, "error": exc.message, "code": exc.code})
        return 1
    except (OSError, ValueError, subprocess.SubprocessError, SystemExit) as exc:
        atomic_json(result, {"ok": False, "error": "特效預覽發生未預期錯誤", "code": "render_failed"})
        progress("failed", str(exc)[-300:])
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
