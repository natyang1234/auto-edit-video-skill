#!/usr/bin/env python3
"""Repository-local validation for the Agent Skill package."""

from __future__ import annotations

import argparse
import py_compile
import re
import sys
from pathlib import Path


NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        raise ValueError("SKILL.md must begin with YAML frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError("SKILL.md frontmatter is not closed")
    block = text[4:end]
    values: dict[str, str] = {}
    current: str | None = None
    for raw in block.splitlines():
        if raw.startswith(" ") and current:
            values[current] = f"{values[current]} {raw.strip()}".strip()
            continue
        if ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        current = key.strip()
        values[current] = value.strip().strip('"').strip("'")
    return values


def validate(skill: Path) -> list[str]:
    errors: list[str] = []
    skill_md = skill / "SKILL.md"
    if not skill_md.is_file():
        return [f"missing {skill_md}"]
    text = skill_md.read_text(encoding="utf-8")
    try:
        meta = frontmatter(text)
    except ValueError as exc:
        return [str(exc)]
    name = meta.get("name", "")
    description = meta.get("description", "")
    if not NAME_RE.fullmatch(name) or len(name) > 64:
        errors.append("name must be lowercase kebab-case and at most 64 characters")
    if not description or len(description) > 1024:
        errors.append("description must contain 1-1024 characters")
    if skill.name != name:
        errors.append("skill directory name must match frontmatter name")
    if len(text.splitlines()) > 500:
        errors.append("SKILL.md must remain under 500 lines")

    required = [
        "scripts/auto_edit.py",
        "scripts/editor_server.py",
        "scripts/render_editor_timeline.py",
        "scripts/render_cut.py",
        "scripts/qa_video.py",
        "references/AGENT_FIRST.zh-TW.md",
        "editor/index.html",
        "agents/openai.yaml",
    ]
    for relative in required:
        if not (skill / relative).is_file():
            errors.append(f"missing required file: {relative}")

    for path in skill.rglob("*.py"):
        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as exc:
            errors.append(f"Python compile failed for {path.relative_to(skill)}: {exc.msg}")

    forbidden = re.compile(
        r"(/Users/[^/\s]+|BEGIN (?:RSA|OPENSSH|EC) PRIVATE KEY|sk-[A-Za-z0-9]{16,})"
    )
    for path in skill.rglob("*"):
        if not path.is_file() or path.suffix.lower() in {".png", ".jpg", ".jpeg", ".mp4"}:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if forbidden.search(content):
            errors.append(f"portable/security pattern found in {path.relative_to(skill)}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "skill",
        nargs="?",
        default=str(Path(__file__).resolve().parents[1] / "skills/auto-edit-video"),
    )
    skill = Path(parser.parse_args().skill).expanduser().resolve()
    errors = validate(skill)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 2
    print(f"Skill is valid: {skill}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
