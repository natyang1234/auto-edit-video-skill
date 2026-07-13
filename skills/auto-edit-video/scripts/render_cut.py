#!/usr/bin/env python3
"""Render approved edit decisions into a deterministic source_cut.mp4."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object at {path}")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def probe(path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise ValueError("ffprobe is required")
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type",
            "-of",
            "json",
            str(path),
        ],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or "ffprobe failed")
    data = json.loads(result.stdout)
    duration = float(data.get("format", {}).get("duration") or 0.0)
    if duration <= 0:
        raise ValueError("source duration must be positive")
    streams = data.get("streams", [])
    return {
        "duration": duration,
        "has_audio": any(item.get("codec_type") == "audio" for item in streams),
    }


def merge_ranges(ranges: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for start, end in sorted(ranges):
        if end - start < 0.005:
            continue
        if not merged or start > merged[-1][1] + 0.005:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(round(start, 6), round(end, 6)) for start, end in merged]


def keep_ranges(duration: float, deleted: list[tuple[float, float]]) -> list[tuple[float, float]]:
    kept: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in deleted:
        if start - cursor >= 0.005:
            kept.append((cursor, start))
        cursor = max(cursor, end)
    if duration - cursor >= 0.005:
        kept.append((cursor, duration))
    if not kept:
        raise ValueError("approved deletions remove the entire source")
    return kept


def source_path(project: Path, manifest: dict[str, Any]) -> Path:
    source = manifest.get("source", {})
    staged = str(source.get("staged_path") or "").strip()
    if staged:
        candidate = (project / staged).resolve()
        if candidate.is_file():
            return candidate
    candidate = Path(str(source.get("original_path") or "")).expanduser().resolve()
    if not candidate.is_file():
        raise ValueError(f"source video is missing: {candidate}")
    return candidate


def approved_deletions(
    project: Path,
    duration: float,
) -> list[tuple[float, float]]:
    candidates = read_json(project / "working/edit_candidates.json").get("items", [])
    decisions_payload = read_json(project / "working/edit_decisions.json")
    decisions = decisions_payload.get("items", [])
    if not isinstance(candidates, list) or not isinstance(decisions, list):
        raise ValueError("edit candidates and decisions must contain item arrays")
    if candidates and not decisions_payload.get("approved_from"):
        raise ValueError("edit decisions must be explicitly approved before rendering")
    candidate_by_id = {
        str(item.get("id")): item for item in candidates if isinstance(item, dict)
    }
    decision_by_id = {
        str(item.get("candidate_id")): item for item in decisions if isinstance(item, dict)
    }
    missing = sorted(set(candidate_by_id) - set(decision_by_id))
    if missing:
        raise ValueError(f"missing decisions for candidates: {', '.join(missing[:8])}")

    ranges: list[tuple[float, float]] = []
    for candidate_id, candidate in candidate_by_id.items():
        decision = decision_by_id[candidate_id]
        if decision.get("review_status") != "approved":
            raise ValueError(f"candidate {candidate_id} is not approved")
        action = decision.get("action")
        if action not in {"delete", "keep"}:
            raise ValueError(f"candidate {candidate_id} has invalid action: {action}")
        if action == "delete":
            start = max(0.0, float(candidate.get("start", 0.0)))
            end = min(duration, float(candidate.get("end", 0.0)))
            ranges.append((start, end))
    return merge_ranges(ranges)


def filter_graph(kept: list[tuple[float, float]], has_audio: bool) -> tuple[str, str, str | None]:
    filters: list[str] = []
    concat_inputs: list[str] = []
    for index, (start, end) in enumerate(kept):
        filters.append(
            f"[0:v]trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS[v{index}]"
        )
        concat_inputs.append(f"[v{index}]")
        if has_audio:
            filters.append(
                f"[0:a]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS[a{index}]"
            )
            concat_inputs.append(f"[a{index}]")
    if has_audio:
        filters.append(
            "".join(concat_inputs) + f"concat=n={len(kept)}:v=1:a=1[vout][aout]"
        )
        return ";".join(filters), "[vout]", "[aout]"
    filters.append("".join(concat_inputs) + f"concat=n={len(kept)}:v=1:a=0[vout]")
    return ";".join(filters), "[vout]", None


def render(manifest_path: Path, output: Path) -> dict[str, Any]:
    manifest = read_json(manifest_path)
    project = manifest_path.parent
    approval = manifest.get("approvals", {}).get("destructive_edit", {})
    if not isinstance(approval, dict) or not approval.get("approved"):
        raise ValueError("destructive_edit approval is required")
    source = source_path(project, manifest)
    media = probe(source)
    deleted = approved_deletions(project, media["duration"])
    kept = keep_ranges(media["duration"], deleted)
    graph, video_label, audio_label = filter_graph(kept, media["has_audio"])
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("ffmpeg is required")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.stem}.tmp-{os.getpid()}{output.suffix}")
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-filter_complex",
        graph,
        "-map",
        video_label,
    ]
    if audio_label:
        command.extend(["-map", audio_label])
    command.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
        ]
    )
    if audio_label:
        command.extend(["-c:a", "aac", "-b:a", "192k"])
    command.extend(["-movflags", "+faststart", str(temporary)])
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode != 0:
        temporary.unlink(missing_ok=True)
        raise ValueError((result.stderr or result.stdout or "ffmpeg failed")[-4000:])
    temporary.replace(output)

    output_cursor = 0.0
    mapping: list[dict[str, float]] = []
    for start, end in kept:
        length = end - start
        mapping.append(
            {
                "source_start": round(start, 6),
                "source_end": round(end, 6),
                "output_start": round(output_cursor, 6),
                "output_end": round(output_cursor + length, 6),
            }
        )
        output_cursor += length
    cut_map = {
        "schema_version": 1,
        "generated_at": now_utc(),
        "source": str(source),
        "output": str(output),
        "deleted": [{"start": start, "end": end} for start, end in deleted],
        "kept": mapping,
        "expected_duration_s": round(output_cursor, 6),
    }
    write_json(project / "working/cut_map.json", cut_map)
    manifest.setdefault("artifacts", {})["source_cut"] = str(output.relative_to(project))
    manifest.setdefault("source", {})["production_path"] = str(output.relative_to(project))
    stages = manifest.setdefault("stages", {})
    stages["edit_review"] = "complete"
    stages["cut"] = "complete"
    stages["retranscribe"] = "needs_review"
    manifest["updated_at"] = now_utc()
    write_json(manifest_path, manifest)
    return cut_map


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    manifest = Path(args.manifest).expanduser().resolve()
    output = (
        Path(args.output).expanduser().resolve()
        if args.output
        else manifest.parent / "renders/source_cut.mp4"
    )
    try:
        result = render(manifest, output)
    except (OSError, ValueError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
