#!/usr/bin/env python3
"""Transcribe Taiwanese Mandarin with a model that knows the place names.

General Whisper checkpoints mishear what a Taiwanese delivery puts on screen:
a metro station becomes a homophone, a department store becomes a function
word, and booking a table becomes positioning. This backend runs MediaTek
Research's Taiwan-tuned fine-tune and emits the JSON shape the Whisper CLI
produces, so the rest of the pipeline cannot tell which engine ran.

It executes inside its own interpreter because the runtime it needs cannot be
installed into a managed system Python, and because loading it in-process
alongside the pipeline's own dependencies aborts on duplicate OpenMP runtimes.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

MODEL_REPO = "eoleedi/Breeze-ASR-25-mlx"
# Where the isolated runtime lives. Created by scripts/install_breeze.sh.
ENV_DIR = Path(os.environ.get("AUTO_EDIT_BREEZE_ENV", Path.home() / ".auto-edit/breeze-env"))
RUNNER = ENV_DIR / "bin" / "python"
TIMEOUT_SECONDS = 3600


def available() -> tuple[bool, str]:
    """Whether this backend can run, and why not when it cannot."""
    if not RUNNER.is_file():
        return False, f"Breeze runtime is not installed at {ENV_DIR}"
    probe = subprocess.run(
        [str(RUNNER), "-c", "import mlx_whisper"], capture_output=True, text=True
    )
    if probe.returncode != 0:
        return False, "Breeze runtime is missing mlx-whisper"
    return True, ""


_SCRIPT = """
import json, sys
import mlx_whisper

source, repo, language = sys.argv[1], sys.argv[2], sys.argv[3]
result = mlx_whisper.transcribe(
    source, path_or_hf_repo=repo, language=language, word_timestamps=True
)
segments = []
for index, segment in enumerate(result.get("segments", [])):
    words = []
    for word in segment.get("words", []) or []:
        text = str(word.get("word", ""))
        if not text.strip():
            continue
        words.append(
            {
                "word": text,
                "start": round(float(word["start"]), 3),
                "end": round(float(word["end"]), 3),
                # This runtime reports no per-word probability. Leaving it
                # unset keeps an unmeasured value from reading as a confident
                # one downstream.
                "probability": None,
            }
        )
    segments.append(
        {
            "id": index,
            "start": round(float(segment.get("start", 0.0)), 3),
            "end": round(float(segment.get("end", 0.0)), 3),
            "text": str(segment.get("text", "")).strip(),
            "words": words,
        }
    )
print(json.dumps(
    {
        "text": str(result.get("text", "")).strip(),
        "segments": segments,
        "language": language,
        "engine": "breeze-asr-25",
    },
    ensure_ascii=False,
))
"""


def transcribe(source: Path, language: str = "zh") -> dict[str, Any]:
    """Return Whisper-CLI-shaped JSON with word timings for one media file."""
    ok, reason = available()
    if not ok:
        raise RuntimeError(reason)
    environment = dict(os.environ)
    # torch and its neighbours each ship an OpenMP runtime on macOS; loading
    # two aborts the process before the model is read.
    environment.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
    result = subprocess.run(
        [str(RUNNER), "-c", _SCRIPT, str(source), MODEL_REPO, language],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        env=environment,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "Breeze transcription failed").strip()[-800:])
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError(f"Breeze returned unreadable output: {exc}") from exc
