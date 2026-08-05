#!/usr/bin/env python3
"""A second caption line, for viewers who do not speak the first one.

Unlike a hook or a keyword, a translation cannot be checked against the
transcript — being different from what was said is the whole point. So what
is verified here is alignment rather than wording: one line out for one line
in, in the same order, none blank, none left as the original.

A missing translation is left missing. A caption that silently shows its
Chinese twice, or shows the line before's English, is worse than a caption
with no second line at all.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import editorial_planner
from editorial_planner import EditorialUnavailable, normalise

TRANSLATIONS_REL = Path("working/caption_translations.json")
MAX_SEGMENTS_PER_PASS = 120
LANGUAGE_NAMES = {
    "en": "英文",
    "ja": "日文",
    "ko": "韓文",
    "es": "西班牙文",
    "th": "泰文",
}


def build_prompt(lines: list[dict[str, Any]], language: str) -> str:
    target = LANGUAGE_NAMES.get(language, language)
    numbered = "\n".join(f'{item["n"]}. {item["text"]}' for item in lines)
    return f"""把下面每一行字幕翻成{target}，用在短影音的第二行字幕。

{numbered}

規則：
- 一行對一行，**數量與編號必須完全對應**，不可合併、不可拆開、不可跳過
- 口語、簡短，讓人一眼掃過就懂，不是逐字直譯
- 專有名詞（地名、店名、品牌）保留原樣
- 只翻譯，不要加註解

只回傳 JSON array，不要 markdown：
[{{"n":1,"text":"翻譯"}}]"""


def align(
    lines: list[dict[str, Any]], proposals: list[dict[str, Any]]
) -> tuple[dict[int, str], list[str]]:
    """Match translations back to their lines; report every one that is not usable."""
    notes: list[str] = []
    by_number: dict[int, str] = {}
    for item in proposals:
        try:
            number = int(item.get("n"))
        except (TypeError, ValueError):
            continue
        text = str(item.get("text") or "").strip()
        if text:
            by_number[number] = text

    aligned: dict[int, str] = {}
    for line in lines:
        number = line["n"]
        translated = by_number.get(number)
        if not translated:
            notes.append(f"line {number} came back with no translation")
            continue
        if normalise(translated) == normalise(line["text"]):
            # Showing the same sentence twice is not a translation, and it
            # costs the frame the same space as a real one.
            notes.append(f"line {number} came back unchanged")
            continue
        aligned[number] = translated
    extra = set(by_number) - {line["n"] for line in lines}
    if extra:
        notes.append(f"ignored {len(extra)} translations for lines that do not exist")
    return aligned, notes


def caption_lines(transcript: dict[str, Any]) -> list[dict[str, Any]]:
    segments = transcript.get("caption_segments") or transcript.get("segments") or []
    lines: list[dict[str, Any]] = []
    for index, segment in enumerate(segments, start=1):
        text = str(segment.get("text", "")).strip()
        if text:
            lines.append({"n": index, "text": text, "id": str(segment.get("id") or "")})
    return lines


def translate(
    transcript: dict[str, Any],
    language: str,
    *,
    provider: tuple[str, ...] = editorial_planner.DEFAULT_PROVIDER,
    timeout_s: int = editorial_planner.DEFAULT_TIMEOUT_S,
) -> tuple[dict[str, Any], list[str]]:
    """Translate every caption line, in batches, keeping the numbering."""
    lines = caption_lines(transcript)
    if not lines:
        raise EditorialUnavailable("transcript has no caption lines to translate")

    aligned: dict[int, str] = {}
    notes: list[str] = []
    for start in range(0, len(lines), MAX_SEGMENTS_PER_PASS):
        batch = lines[start : start + MAX_SEGMENTS_PER_PASS]
        reply = editorial_planner.call_provider(
            build_prompt(batch, language), provider=provider, timeout_s=timeout_s
        )
        batch_aligned, batch_notes = align(
            batch, editorial_planner.parse_json_payload(reply)
        )
        aligned.update(batch_aligned)
        notes.extend(batch_notes)

    if not aligned:
        raise EditorialUnavailable(
            "no line came back usable: " + "; ".join(notes[:4])
        )
    artifact = {
        "schema_version": 1,
        "language": language,
        "items": [
            {"n": line["n"], "id": line["id"], "source": line["text"],
             "text": aligned[line["n"]]}
            for line in lines
            if line["n"] in aligned
        ],
    }
    return artifact, notes


def load(project_dir: Path) -> dict[str, Any]:
    path = project_dir / TRANSLATIONS_REL
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def by_caption_text(project_dir: Path) -> dict[str, str]:
    """Spoken line -> its translation, for attaching to caption overlays."""
    artifact = load(project_dir)
    return {
        str(item.get("source") or ""): str(item.get("text") or "")
        for item in artifact.get("items", [])
        if item.get("source") and item.get("text")
    }


def save(project_dir: Path, artifact: dict[str, Any]) -> Path:
    path = project_dir / TRANSLATIONS_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(path.name + ".tmp")
    scratch.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    scratch.replace(path)
    return path


def main() -> int:
    import argparse
    import shlex

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--language", default="en")
    parser.add_argument("--provider", default="")
    parser.add_argument("--timeout", type=int, default=editorial_planner.DEFAULT_TIMEOUT_S)
    args = parser.parse_args()

    project_dir = Path(args.project_dir).expanduser().resolve()
    transcript = json.loads(
        (project_dir / "working/transcript_words.json").read_text(encoding="utf-8")
    )
    try:
        artifact, notes = translate(
            transcript,
            args.language,
            provider=tuple(shlex.split(args.provider))
            if args.provider
            else editorial_planner.DEFAULT_PROVIDER,
            timeout_s=args.timeout,
        )
    except (EditorialUnavailable, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    save(project_dir, artifact)
    print(
        json.dumps(
            {
                "ok": True,
                "language": args.language,
                "translated": len(artifact["items"]),
                "of": len(caption_lines(transcript)),
                "notes": notes,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
