#!/usr/bin/env python3
"""Turn a chatty tool's output into the one line a human needs.

ffmpeg reports progress and libx264 reports encoding statistics on stderr,
so tailing stderr after a failed render hands back a wall of block-mode
percentages and hides whatever actually went wrong. This module keeps the
lines that carry a diagnosis and drops the ones that only describe work
that succeeded.
"""

from __future__ import annotations

import re

__all__ = ["is_progress_noise", "summarize_tool_failure"]


# Lines that only narrate progress or restate encoder statistics.
_NOISE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*\[libx264\b"),
    re.compile(r"^\s*\[libx265\b"),
    re.compile(r"^\s*\[aac\b"),
    re.compile(r"^\s*\[out#\d"),
    re.compile(r"^\s*\[in#\d"),
    re.compile(r"^\s*\[vost#\d"),
    re.compile(r"^\s*\[aost#\d"),
    re.compile(r"^\s*\[mp4\s*@[^\]]*\]\s*Starting second pass"),
    re.compile(r"^\s*frame=\s*\d"),
    re.compile(r"^\s*size=\s*\d"),
    re.compile(r"^\s*video:\s*\d+\S*\s+audio:"),
    re.compile(r"^\s*kb/s:"),
    re.compile(r"^\s*(?:Press|Stream mapping:|Output #|Input #|\s+Stream #|\s+Metadata:|\s+Side data:)"),
    re.compile(r"^\s*(?:ffmpeg|ffprobe) version \d"),
    re.compile(r"^\s*(?:built with|configuration:|lib(?:av|sw|postproc)\w*\s+\d)"),
    re.compile(r"^\s*(?:major_brand|minor_version|compatible_brands|encoder|handler_name|vendor_id|creation_time|com\.apple\.)"),
    re.compile(r"^\s*(?:duration|bitrate|start)\s*[:=]", re.IGNORECASE),
    re.compile(r"^\s*(?:Ambient viewing environment|CPB properties):"),
)

# Lines that always survive, even when they also match a noise pattern.
_SIGNAL_PATTERN = re.compile(
    r"(?:^\s*Traceback\b)"
    r"|(?:\b(?:Error|error|ERROR|Invalid|invalid|failed|Failed|FAILED|Unable|unable|"
    r"cannot|Cannot|No such file|Permission denied|Conversion failed|"
    r"Killed|Aborted|Segmentation fault|timed out|timeout)\b)"
    r"|(?:^\s*\w*(?:Error|Exception)\b\s*:)"
    r"|(?:\bexit(?:ed)?\s+(?:with\s+)?(?:code|status)\b)"
    r"|(?:^\s*File \"[^\"]+\", line \d)"  # a traceback frame quoted on its own
)


# Openers of ffmpeg's indented media-inventory blocks.
_STREAM_HEADER_PATTERN = re.compile(r"^\s*(?:Input #\d|Output #\d|Stream mapping:)")


def is_progress_noise(line: str) -> bool:
    """True when a line only reports progress or encoder statistics."""
    if _SIGNAL_PATTERN.search(line):
        return False
    return any(pattern.search(line) for pattern in _NOISE_PATTERNS)


def summarize_tool_failure(
    text: str | None,
    *,
    fallback: str = "the tool failed without reporting a reason",
    limit: int = 1200,
) -> str:
    """Return the readable cause hidden inside a tool's combined output.

    Keeps the tail of the diagnosis so the final error remains visible, and
    falls back to the noisiest available evidence rather than saying nothing.
    """
    raw = (text or "").strip()
    if not raw:
        return fallback[-limit:] if limit > 0 else fallback

    kept: list[str] = []
    in_traceback = False
    in_stream_header = False
    for line in raw.splitlines():
        if line.lstrip().startswith("Traceback (most recent call last)"):
            in_traceback = True
            in_stream_header = False
            kept.append(line)
            continue
        if not in_traceback:
            # ffmpeg prints each input's and output's stream inventory as an
            # indented block. The whole block describes media, never failure.
            if _STREAM_HEADER_PATTERN.search(line):
                in_stream_header = True
                continue
            if in_stream_header:
                if line.startswith((" ", "\t")) and not _SIGNAL_PATTERN.search(line):
                    continue
                in_stream_header = False
        if in_traceback:
            # A traceback ends at the first line that is neither indented
            # body nor the final exception line.
            if line.startswith((" ", "\t")) or line.strip():
                kept.append(line)
                if not line.startswith((" ", "\t")) and line.strip():
                    in_traceback = False
                continue
            in_traceback = False
            continue
        if not line.strip():
            continue
        if is_progress_noise(line):
            continue
        kept.append(line)

    summary = "\n".join(kept).strip()
    if not summary:
        # Nothing survived the filter: the caller still deserves evidence,
        # so hand back the raw tail rather than an empty toast.
        summary = fallback
    return summary[-limit:] if limit > 0 else summary
