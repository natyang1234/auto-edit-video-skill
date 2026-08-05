#!/usr/bin/env python3
"""Bring generated images into a project without regenerating what exists.

Generation happens through a local browser bridge, so the cost is time and
someone else's rate limit rather than money. What matters here is that asking
for the same picture twice does not produce two of them, and that a picture
which arrives can be found again later.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

BRIDGES = {
    "chatgpt-image-bridge": Path.home()
    / ".openclaw/workspace/scripts/chatgpt-image-bridge/cli.js",
    "gemini-image-bridge": Path.home()
    / ".openclaw/workspace/scripts/gemini-image-bridge/cli.js",
}
DEFAULT_CDP = "http://127.0.0.1:9224"
ASSET_PREFIX = "assets/generated/images/"
# The bridge writes a metadata sidecar next to each picture.
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
PROMPTS_REL = Path("working/generated_prompts")
LEDGER_REL = Path("working/generated_images.json")


def prompt_digest(prompt: str) -> str:
    return hashlib.sha256(prompt.strip().encode("utf-8")).hexdigest()


def read_ledger(project_dir: Path) -> dict[str, Any]:
    path = Path(project_dir) / LEDGER_REL
    if not path.is_file():
        return {"schema_version": 1, "items": []}
    try:
        data = json.loads(path.read_text("utf-8"))
    except (ValueError, OSError):
        return {"schema_version": 1, "items": []}
    return data if isinstance(data, dict) and isinstance(data.get("items"), list) else {
        "schema_version": 1,
        "items": [],
    }


def existing_for_beat(project_dir: Path, beat_id: str, prompt: str) -> dict[str, Any] | None:
    """What was already generated for this beat, if anything.

    Keyed on the beat rather than the prompt: rewording the same request is
    still the same picture being asked for twice.
    """
    digest = prompt_digest(prompt)
    for item in read_ledger(project_dir).get("items", []):
        if not isinstance(item, dict) or item.get("beat_id") != beat_id:
            continue
        if (Path(project_dir) / str(item.get("path", ""))).is_file():
            item = dict(item)
            item["prompt_changed"] = item.get("prompt_sha256") != digest
            return item
    return None


def bridge_status(bridge_id: str, cdp: str = DEFAULT_CDP) -> dict[str, Any]:
    """Whether this bridge could run right now, without running it."""
    script = BRIDGES.get(bridge_id)
    if script is None:
        return {"ready": False, "reason": f"unknown bridge {bridge_id!r}"}
    if not script.is_file():
        return {"ready": False, "reason": f"bridge is not installed at {script}"}
    if shutil.which("node") is None:
        return {"ready": False, "reason": "node is not available"}
    try:
        import urllib.request

        with urllib.request.urlopen(f"{cdp.rstrip('/')}/json/version", timeout=3) as response:
            browser = json.loads(response.read()).get("Browser", "")
    except Exception:
        return {
            "ready": False,
            "reason": f"no browser answering at {cdp}; open the logged-in Chrome first",
        }
    return {"ready": True, "browser": browser, "cdp": cdp}


def generate_image(
    project_dir: Path,
    beat_id: str,
    prompt: str,
    bridge_id: str = "chatgpt-image-bridge",
    cdp: str = DEFAULT_CDP,
    runner: Any = None,
) -> dict[str, Any]:
    """Generate one image for a beat, or return the one already made for it."""
    project_dir = Path(project_dir)
    previous = existing_for_beat(project_dir, beat_id, prompt)
    if previous is not None and not previous.get("prompt_changed"):
        return {**previous, "reused": True}

    status = bridge_status(bridge_id, cdp)
    if not status["ready"]:
        return {"ok": False, "reason": status["reason"], "beat_id": beat_id}

    out_dir = project_dir / ASSET_PREFIX
    out_dir.mkdir(parents=True, exist_ok=True)
    before = {path for path in out_dir.iterdir() if path.is_file()}
    command = [
        "node",
        str(BRIDGES[bridge_id]),
        "--prompt",
        prompt,
        "--out",
        str(out_dir),
        "--cdp",
        cdp,
    ]
    run = runner or (
        lambda cmd: subprocess.run(cmd, text=True, capture_output=True, timeout=10 * 60)
    )
    result = run(command)
    if getattr(result, "returncode", 1) != 0:
        return {
            "ok": False,
            "beat_id": beat_id,
            "reason": (getattr(result, "stderr", "") or "image generation failed").strip()[-400:],
        }
    arrived = sorted(
        (
            path
            for path in out_dir.iterdir()
            if path.is_file()
            and path not in before
            and path.suffix.lower() in IMAGE_SUFFIXES
        ),
        key=lambda path: path.stat().st_mtime,
    )
    if not arrived:
        return {"ok": False, "beat_id": beat_id, "reason": "bridge produced no image"}

    image = arrived[-1]
    digest = prompt_digest(prompt)
    prompts_dir = project_dir / PROMPTS_REL
    prompts_dir.mkdir(parents=True, exist_ok=True)
    # The prompt itself stays in the project: it can name a client or an
    # unreleased product, and provenance travels further than the project does.
    (prompts_dir / f"{digest}.txt").write_text(prompt.strip() + "\n", encoding="utf-8")

    record = {
        "beat_id": beat_id,
        "path": str(image.relative_to(project_dir)),
        "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
        "provider_id": bridge_id,
        "prompt_sha256": digest,
    }
    ledger = read_ledger(project_dir)
    ledger["items"] = [
        item for item in ledger["items"] if item.get("beat_id") != beat_id
    ] + [record]
    (project_dir / LEDGER_REL).parent.mkdir(parents=True, exist_ok=True)
    (project_dir / LEDGER_REL).write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {**record, "ok": True, "reused": False}
