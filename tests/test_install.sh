#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMP_HOME="$(mktemp -d)"
trap 'rm -rf "$TEMP_HOME"' EXIT

"$ROOT/install.sh" --agent all --home "$TEMP_HOME" --dry-run >/dev/null
"$ROOT/install.sh" --agent all --home "$TEMP_HOME" >/dev/null
for destination in \
  "$TEMP_HOME/.agents/skills" \
  "$TEMP_HOME/.codex/skills" \
  "$TEMP_HOME/.claude/skills" \
  "$TEMP_HOME/.grok/skills" \
  "$TEMP_HOME/.openclaw/skills" \
  "$TEMP_HOME/.hermes/skills"; do
  test -f "$destination/auto-edit-video/SKILL.md"
done
python3 "$TEMP_HOME/.codex/skills/auto-edit-video/scripts/auto_edit.py" preflight >/dev/null

if "$ROOT/install.sh" --agent codex --home "$TEMP_HOME" >/dev/null 2>&1; then
  printf 'installer unexpectedly overwrote an existing skill\n' >&2
  exit 1
fi

"$ROOT/install.sh" --agent codex --home "$TEMP_HOME" --force >/dev/null
test -f "$TEMP_HOME/.codex/skills/auto-edit-video/SKILL.md"
printf 'installer tests passed\n'
