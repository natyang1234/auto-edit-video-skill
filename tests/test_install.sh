#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMP_HOME="$(mktemp -d)"
trap 'rm -rf "$TEMP_HOME"' EXIT

"$ROOT/install.sh" --agent all --home "$TEMP_HOME" --dry-run >/dev/null
"$ROOT/install.sh" --agent all --home "$TEMP_HOME" >/dev/null
"$ROOT/install.sh" --agent all --home "$TEMP_HOME" --check >/dev/null
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

printf '\nrestore-drill-marker\n' >> "$TEMP_HOME/.codex/skills/auto-edit-video/SKILL.md"
if "$ROOT/install.sh" --agent codex --home "$TEMP_HOME" --check >/dev/null 2>&1; then
  printf 'check unexpectedly accepted a drifted skill\n' >&2
  exit 1
fi

"$ROOT/install.sh" --agent codex --home "$TEMP_HOME" --force >/dev/null
test -f "$TEMP_HOME/.codex/skills/auto-edit-video/SKILL.md"
"$ROOT/install.sh" --agent codex --home "$TEMP_HOME" --check >/dev/null

if find "$TEMP_HOME/.codex/skills" -maxdepth 1 -type d -name 'auto-edit-video.backup-*' | rg -q .; then
  printf 'installer polluted the active skill tree with a backup directory\n' >&2
  exit 1
fi

MANIFEST="$(find "$TEMP_HOME/.skills-backup-quarantine/auto-edit-video" -name MANIFEST.tsv -type f | head -n 1)"
test -n "$MANIFEST"
test -f "$(dirname "$MANIFEST")/RESTORE.txt"
BACKUP="$(awk -F '\t' '$1 == "codex" { print $3 }' "$MANIFEST")"
test -f "$BACKUP/SKILL.md"

# Exercise an installer-independent restore: move the new copy aside, restore
# the quarantined directory directly, and prove the old bytes came back.
mv "$TEMP_HOME/.codex/skills/auto-edit-video" "$TEMP_HOME/.codex/skills/auto-edit-video.failed-test"
mv "$BACKUP" "$TEMP_HOME/.codex/skills/auto-edit-video"
rg -q 'restore-drill-marker' "$TEMP_HOME/.codex/skills/auto-edit-video/SKILL.md"
mv "$TEMP_HOME/.codex/skills/auto-edit-video" "$BACKUP"
mv "$TEMP_HOME/.codex/skills/auto-edit-video.failed-test" "$TEMP_HOME/.codex/skills/auto-edit-video"
"$ROOT/install.sh" --agent codex --home "$TEMP_HOME" --check >/dev/null
printf 'installer tests passed\n'
