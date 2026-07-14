#!/usr/bin/env python3
"""Video-layout templates and local subject-cutout capability detection.

Director presets intentionally do not live here: a director controls copy,
captions, and motion language, while a video template controls source framing,
camera motion, subject extraction, and background composition.
"""

from __future__ import annotations

import copy
import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any


DEFAULT_VIDEO_TEMPLATE_ID = "dynamic-craft"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov"}


VIDEO_TEMPLATES: dict[str, dict[str, Any]] = {
    "fixed-full": {
        "label": "固定全畫面",
        "short_label": "全畫面",
        "group": "fixed",
        "description": "來源畫面全程固定；只讓字幕與字卡進出。",
        "camera_motion": "none",
        "subject_mode": "source",
        "background_mode": "stage",
        "needs_background_asset": False,
        "frame_defaults": {"x": 50, "y": 50, "width": 100, "height": 100, "fit": "cover"},
    },
    "fixed-stage": {
        "label": "固定講台",
        "short_label": "講台",
        "group": "fixed",
        "description": "人物固定在上半部講台框，圖卡在下方說明。",
        "camera_motion": "none",
        "subject_mode": "source",
        "background_mode": "stage",
        "needs_background_asset": False,
        "frame_defaults": {"x": 50, "y": 34, "width": 93, "height": 40, "fit": "cover"},
    },
    "fixed-stack": {
        "label": "固定上下分屏",
        "short_label": "上下分屏",
        "group": "fixed",
        "description": "影片固定在上方，重點卡與字幕保留獨立區域。",
        "camera_motion": "none",
        "subject_mode": "source",
        "background_mode": "stage",
        "needs_background_asset": False,
        "frame_defaults": {"x": 50, "y": 24, "width": 85, "height": 30, "fit": "cover"},
    },
    "dynamic-craft": {
        "label": "動態手帳",
        "short_label": "手帳",
        "group": "dynamic",
        "description": "沿用目前圖卡構圖，依內容重新安排人物畫面。",
        "camera_motion": "reframe",
        "subject_mode": "source",
        "background_mode": "stage",
        "needs_background_asset": False,
        "frame_defaults": {"x": 50, "y": 40, "width": 93, "height": 33, "fit": "cover"},
    },
    "dynamic-punch": {
        "label": "節奏推近",
        "short_label": "推近",
        "group": "dynamic",
        "description": "畫面維持主構圖，只在重點句做克制推近。",
        "camera_motion": "punch",
        "subject_mode": "source",
        "background_mode": "stage",
        "needs_background_asset": False,
        "frame_defaults": {"x": 50, "y": 50, "width": 100, "height": 100, "fit": "cover"},
    },
    "cutout-solid": {
        "label": "人物去背・棚拍色",
        "short_label": "純色背景",
        "group": "cutout",
        "description": "本機摳出人物，放到可選純色棚拍背景。",
        "camera_motion": "none",
        "subject_mode": "cutout",
        "background_mode": "solid",
        "needs_background_asset": False,
        "frame_defaults": {"x": 50, "y": 50, "width": 100, "height": 100, "fit": "cover"},
    },
    "cutout-image": {
        "label": "人物去背・圖片背景",
        "short_label": "圖片背景",
        "group": "cutout",
        "description": "本機摳出人物，合成到專案內的背景圖片。",
        "camera_motion": "none",
        "subject_mode": "cutout",
        "background_mode": "image",
        "needs_background_asset": True,
        "frame_defaults": {"x": 50, "y": 50, "width": 100, "height": 100, "fit": "cover"},
    },
    "cutout-video": {
        "label": "人物去背・動態背景",
        "short_label": "影片背景",
        "group": "cutout",
        "description": "本機摳出人物，合成到循環播放的背景影片。",
        "camera_motion": "none",
        "subject_mode": "cutout",
        "background_mode": "video",
        "needs_background_asset": True,
        "frame_defaults": {"x": 50, "y": 50, "width": 100, "height": 100, "fit": "cover"},
    },
}


def default_video_template_state(template_id: str = DEFAULT_VIDEO_TEMPLATE_ID) -> dict[str, Any]:
    if template_id not in VIDEO_TEMPLATES:
        raise ValueError(f"unsupported video template: {template_id}")
    template = VIDEO_TEMPLATES[template_id]
    return {
        "id": template_id,
        "frame": copy.deepcopy(template["frame_defaults"]),
        "subject": {
            "x": 50,
            "y": 54,
            "scale": 1.0,
            "feather": 2,
            "mask_stride": 3,
        },
        "background": {
            "color": "#17251d",
            "source": None,
            "fit": "cover",
            "blur": 0,
        },
    }


def _merge_missing(target: dict[str, Any], defaults: dict[str, Any]) -> bool:
    changed = False
    for key, value in defaults.items():
        if key not in target:
            target[key] = copy.deepcopy(value)
            changed = True
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            changed = _merge_missing(target[key], value) or changed
    return changed


def upgrade_video_template_state(state: dict[str, Any]) -> bool:
    """Add template state to legacy projects without replacing reviewed choices."""
    current = state.get("video_template")
    if not isinstance(current, dict):
        state["video_template"] = default_video_template_state()
        return True
    template_id = str(current.get("id") or "")
    if not template_id:
        current["id"] = DEFAULT_VIDEO_TEMPLATE_ID
        template_id = DEFAULT_VIDEO_TEMPLATE_ID
    if template_id not in VIDEO_TEMPLATES:
        return False
    return _merge_missing(current, default_video_template_state(template_id))


def _finite_number(
    value: Any,
    *,
    minimum: float,
    maximum: float,
    integer: bool = False,
) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    number = float(value)
    if number != number or number in {float("inf"), float("-inf")}:
        return False
    if integer and not isinstance(value, int):
        return False
    return minimum <= number <= maximum


def _project_asset_source(source: Any, extensions: set[str]) -> bool:
    if not isinstance(source, str) or not source or len(source) > 300:
        return False
    path = PurePosixPath(source)
    if path.is_absolute() or ".." in path.parts or len(path.parts) != 2 or path.parts[0] != "assets":
        return False
    return path.suffix.lower() in extensions


def validate_video_template_state(value: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["video_template must be an object"]
    template_id = str(value.get("id") or "")
    template = VIDEO_TEMPLATES.get(template_id)
    if template is None:
        return ["video_template id is not supported"]

    frame = value.get("frame")
    if not isinstance(frame, dict):
        errors.append("video_template frame must be an object")
    else:
        for key, minimum, maximum in (
            ("x", 0, 100),
            ("y", 0, 100),
            ("width", 5, 100),
            ("height", 5, 100),
        ):
            if not _finite_number(frame.get(key), minimum=minimum, maximum=maximum):
                errors.append(f"video_template frame {key} is invalid")
        if frame.get("fit") not in {"cover", "contain"}:
            errors.append("video_template frame fit must be cover or contain")

    subject = value.get("subject")
    if not isinstance(subject, dict):
        errors.append("video_template subject must be an object")
    else:
        if not _finite_number(subject.get("x"), minimum=-50, maximum=150):
            errors.append("video_template subject x is invalid")
        if not _finite_number(subject.get("y"), minimum=-50, maximum=150):
            errors.append("video_template subject y is invalid")
        if not _finite_number(subject.get("scale"), minimum=0.15, maximum=3.0):
            errors.append("video_template subject scale is invalid")
        if not _finite_number(subject.get("feather"), minimum=0, maximum=30):
            errors.append("video_template subject feather is invalid")
        if not _finite_number(
            subject.get("mask_stride"), minimum=1, maximum=12, integer=True
        ):
            errors.append("video_template subject mask_stride must be an integer between 1 and 12")

    background = value.get("background")
    if not isinstance(background, dict):
        errors.append("video_template background must be an object")
        return errors
    color = str(background.get("color") or "")
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
        errors.append("video_template background color is invalid")
    if background.get("fit") not in {"cover", "contain"}:
        errors.append("video_template background fit must be cover or contain")
    if not _finite_number(background.get("blur"), minimum=0, maximum=40):
        errors.append("video_template background blur is invalid")

    source = background.get("source")
    mode = str(template["background_mode"])
    if mode == "image" and source not in {None, ""} and not _project_asset_source(source, IMAGE_EXTENSIONS):
        errors.append("video_template requires an image background asset under assets/")
    elif mode == "video" and source not in {None, ""} and not _project_asset_source(source, VIDEO_EXTENSIONS):
        errors.append("video_template requires a video background asset under assets/")
    elif source not in {None, ""} and not _project_asset_source(
        source, IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
    ):
        errors.append("video_template background source must stay under assets/")
    return errors


def template_readiness_errors(value: Any) -> list[str]:
    """Return render blockers that are allowed while a template is being configured."""
    errors = validate_video_template_state(value)
    if errors or not isinstance(value, dict):
        return errors
    template = VIDEO_TEMPLATES[str(value["id"])]
    source = value.get("background", {}).get("source")
    if template["background_mode"] == "image" and not _project_asset_source(source, IMAGE_EXTENSIONS):
        errors.append("video_template requires an image background asset under assets/")
    elif template["background_mode"] == "video" and not _project_asset_source(source, VIDEO_EXTENSIONS):
        errors.append("video_template requires a video background asset under assets/")
    return errors


def _candidate_rembg_pythons() -> list[Path]:
    configured = os.environ.get("AUTO_EDIT_REMBG_PYTHON", "").strip()
    candidates = [Path(configured).expanduser()] if configured else []
    candidates.extend(
        [
            Path.home() / ".local/pipx/venvs/rembg/bin/python",
            Path.home() / ".local/share/pipx/venvs/rembg/bin/python",
        ]
    )
    return candidates


@lru_cache(maxsize=1)
def cutout_capability() -> dict[str, Any]:
    # Keep the virtualenv launcher path intact. Resolving its python symlink would
    # discard the venv prefix and import packages from the system interpreter.
    python = next((item.absolute() for item in _candidate_rembg_pythons() if item.is_file()), None)
    model_home = Path(os.environ.get("U2NET_HOME", str(Path.home() / ".u2net"))).expanduser()
    model = model_home / "isnet-general-use.onnx"
    if python is None:
        return {
            "available": False,
            "engine": "rembg-local",
            "reason": "找不到本機 rembg 執行環境",
        }
    if not model.is_file():
        return {
            "available": False,
            "engine": "rembg-local",
            "reason": "找不到已下載的本機人物去背模型",
        }
    try:
        check = subprocess.run(
            [str(python), "-c", "import rembg, PIL, numpy"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        check = None
    if check is None or check.returncode != 0:
        return {
            "available": False,
            "engine": "rembg-local",
            "reason": "本機人物去背環境無法載入",
        }
    return {
        "available": True,
        "engine": "rembg-local",
        "model": "isnet-general-use",
        "python": str(python),
        "model_path": str(model.resolve()),
        "reason": None,
    }


def public_template_catalog(capability: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    cutout = capability or cutout_capability()
    result = copy.deepcopy(VIDEO_TEMPLATES)
    for template in result.values():
        is_cutout = template["subject_mode"] == "cutout"
        template["available"] = bool(cutout.get("available")) if is_cutout else True
        template["unavailable_reason"] = cutout.get("reason") if is_cutout else None
    return result
