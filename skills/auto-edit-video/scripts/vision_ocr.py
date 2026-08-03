#!/usr/bin/env python3
"""macOS Vision OCR bridge — optional capability per ANALYSIS_ENGINE.md.

Primary OCR engine: Apple Vision (``VNRecognizeTextRequest``) reached through
pyobjc. Absence of pyobjc/Vision (or ``AUTO_EDIT_DISABLE_VISION=1``) degrades
to ``not_configured``; callers must then keep ``ocr_spans`` empty. The
tesseract fallback is contractually reserved but not implemented yet.
"""
from __future__ import annotations

import concurrent.futures
import os
import platform
from pathlib import Path

_VISION_MODULES: tuple | None = None


def _load_vision():
    global _VISION_MODULES
    if _VISION_MODULES is not None:
        return _VISION_MODULES
    if os.environ.get("AUTO_EDIT_DISABLE_VISION") == "1":
        _VISION_MODULES = ()
        return _VISION_MODULES
    try:
        import Foundation  # type: ignore
        import Quartz  # type: ignore
        import Vision  # type: ignore
    except Exception:  # pragma: no cover - depends on host setup
        _VISION_MODULES = ()
        return _VISION_MODULES
    _VISION_MODULES = (Foundation, Quartz, Vision)
    return _VISION_MODULES


def vision_engine() -> dict[str, str]:
    """Engine descriptor for the video_analysis contract."""
    available = bool(_load_vision())
    return {
        "name": "macos-vision",
        "version": f"macos-{platform.mac_ver()[0] or 'unknown'}" if available else "",
        "status": "present" if available else "not_configured",
    }


def _recognize_sync(image_path: Path, languages: tuple[str, ...]) -> list[dict]:
    foundation, quartz, vision = _load_vision()
    url = foundation.NSURL.fileURLWithPath_(str(image_path))
    source = quartz.CGImageSourceCreateWithURL(url, None)
    if source is None:
        raise ValueError(f"unreadable image: {image_path}")
    image = quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
    if image is None:
        raise ValueError(f"undecodable image: {image_path}")
    request = vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(False)
    request.setRecognitionLanguages_(list(languages))
    handler = vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, None)
    ok, error = handler.performRequests_error_([request], None)
    if not ok:
        raise ValueError(f"vision request failed: {error}")
    lines: list[dict] = []
    for observation in request.results() or []:
        candidate = observation.topCandidates_(1)
        if not candidate:
            continue
        best = candidate[0]
        text = str(best.string()).strip()
        if text:
            lines.append({"text": text, "confidence": float(best.confidence())})
    return lines


def recognize_text(
    image_path: Path,
    languages: tuple[str, ...] = ("zh-Hant", "en-US"),
    timeout_s: float = 10.0,
) -> list[dict]:
    """Recognise text lines in an image; raises on timeout or failure."""
    if not _load_vision():
        raise RuntimeError("macOS Vision OCR is not available")
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_recognize_sync, Path(image_path), languages)
        return future.result(timeout=timeout_s)
