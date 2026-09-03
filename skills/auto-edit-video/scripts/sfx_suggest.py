#!/usr/bin/env python3
"""Advisory SFX suggestion scanner.

Reads a project's final captions, substring-matches them against a keyword
table, and writes candidate content-hint suggestions. Purely advisory: it
never writes ``working/sfx_hints.json`` and never touches the delivered
timeline.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from sfx_mapping import SFX_CATEGORIES  # noqa: E402

SAMPLE_RATE = 48000
SCHEMA_VERSION = 1


def load_keyword_table(path: Path) -> dict[str, list[str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    categories = payload.get("categories")
    if not isinstance(categories, dict):
        raise ValueError(f"invalid keyword table (no categories): {path}")
    table: dict[str, list[str]] = {}
    for category, entry in categories.items():
        if category not in SFX_CATEGORIES or not isinstance(entry, dict):
            continue
        keywords = entry.get("keywords")
        if isinstance(keywords, list):
            table[category] = [str(word) for word in keywords if str(word)]
    return table


def _state_segments(state: dict[str, Any]) -> list[dict[str, float]]:
    raw = state.get("segments")
    if not isinstance(raw, list) or not raw:
        return []
    output: list[dict[str, float]] = []
    offset = 0.0
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item["source_start"])
            end = float(item["source_end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        output.append({"source_start": start, "source_end": end, "final_start": offset})
        offset += end - start
    return output


def _map_to_final_seconds(source_time: float, segments: list[dict[str, float]]) -> float | None:
    if not segments:
        return source_time
    for segment in segments:
        if segment["source_start"] <= source_time <= segment["source_end"]:
            return segment["final_start"] + (source_time - segment["source_start"])
    return None


def _caption_segments(project_dir: Path) -> list[dict[str, Any]]:
    transcript_path = project_dir / "working/transcript_words.json"
    try:
        transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(transcript, dict):
        return []
    segments = transcript.get("caption_segments")
    if not isinstance(segments, list):
        segments = transcript.get("segments")
    if not isinstance(segments, list):
        return []
    return [item for item in segments if isinstance(item, dict)]


def _best_match(text: str, table: dict[str, list[str]]) -> tuple[str, str] | None:
    lowered = text.lower()
    best: tuple[str, str] | None = None
    best_len = -1
    for category, keywords in table.items():
        for keyword in keywords:
            needle = keyword.lower()
            if needle and needle in lowered and len(keyword) > best_len:
                best = (category, keyword)
                best_len = len(keyword)
    return best


def build_hints(
    caption_segments: list[dict[str, Any]],
    state_segments: list[dict[str, float]],
    table: dict[str, list[str]],
) -> list[dict[str, Any]]:
    hints: list[dict[str, Any]] = []
    last_onset: int | None = None
    for segment in caption_segments:
        text = segment.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        match = _best_match(text, table)
        if match is None:
            continue
        category, keyword = match
        try:
            start_source = float(segment.get("start"))
        except (TypeError, ValueError):
            continue
        final_seconds = _map_to_final_seconds(start_source, state_segments)
        if final_seconds is None or final_seconds < 0:
            continue
        onset_sample = round(final_seconds * SAMPLE_RATE)
        if last_onset is not None and onset_sample <= last_onset:
            continue
        hints.append({
            "onset_sample": onset_sample,
            "category": category,
            "reason": f"keyword:{keyword}",
            "matched_keyword": keyword,
            "caption_text": text,
        })
        last_onset = onset_sample
    return hints


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--keywords", default="references/sfx_keywords.json")
    parser.add_argument("--out", default="working/sfx_suggestions.json")
    return parser


def run(project_dir: Path, keywords_path: Path, out_path: Path) -> dict[str, Any]:
    """Scan captions and write advisory hints. Never prints; callers decide."""
    if not project_dir.is_dir():
        raise ValueError(f"no such project directory: {project_dir}")
    if not keywords_path.is_file():
        raise ValueError(f"no such keyword table: {keywords_path}")

    table = load_keyword_table(keywords_path)

    state_path = project_dir / "working/editor_state.json"
    try:
        state_raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state_raw = {}
    state_segments = _state_segments(state_raw if isinstance(state_raw, dict) else {})

    caption_segments = _caption_segments(project_dir)
    hints = build_hints(caption_segments, state_segments, table)

    if not out_path.is_absolute():
        out_path = (project_dir / out_path).resolve()
    payload = {"schema_version": SCHEMA_VERSION, "hints": hints}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {"ok": True, "output": str(out_path), "hint_count": len(hints)}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    project_dir = Path(args.project_dir).expanduser().resolve()
    keywords_path = Path(args.keywords).expanduser().resolve()
    out_path = Path(args.out)

    try:
        result = run(project_dir, keywords_path, out_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
