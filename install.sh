#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$REPO_ROOT/skills/auto-edit-video"
TARGET="all"
INSTALL_HOME="$HOME"
HOME_WAS_OVERRIDDEN=0
FORCE=0
DRY_RUN=0

usage() {
  printf '%s\n' \
    'Usage: ./install.sh [--agent TARGET] [--home PATH] [--force] [--dry-run]' \
    '' \
    'Targets: all, shared, codex, claude, grok, openclaw, hermes' \
    '  shared installs to ~/.agents/skills for Agent Skills compatible runtimes.'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --agent)
      [[ $# -ge 2 ]] || { printf 'Missing value for --agent\n' >&2; exit 2; }
      TARGET="$2"
      shift 2
      ;;
    --home)
      [[ $# -ge 2 ]] || { printf 'Missing value for --home\n' >&2; exit 2; }
      INSTALL_HOME="$2"
      HOME_WAS_OVERRIDDEN=1
      shift 2
      ;;
    --force)
      FORCE=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

[[ -f "$SOURCE_DIR/SKILL.md" ]] || { printf 'Skill source is missing: %s\n' "$SOURCE_DIR" >&2; exit 2; }

root_for() {
  case "$1" in
    shared) printf '%s\n' "$INSTALL_HOME/.agents/skills" ;;
    codex)
      if [[ $HOME_WAS_OVERRIDDEN -eq 0 && -n "${CODEX_HOME:-}" ]]; then
        printf '%s\n' "$CODEX_HOME/skills"
      else
        printf '%s\n' "$INSTALL_HOME/.codex/skills"
      fi
      ;;
    claude) printf '%s\n' "$INSTALL_HOME/.claude/skills" ;;
    grok)
      if [[ $HOME_WAS_OVERRIDDEN -eq 0 && -n "${GROK_HOME:-}" ]]; then
        printf '%s\n' "$GROK_HOME/skills"
      else
        printf '%s\n' "$INSTALL_HOME/.grok/skills"
      fi
      ;;
    openclaw)
      if [[ $HOME_WAS_OVERRIDDEN -eq 0 && -n "${OPENCLAW_HOME:-}" ]]; then
        printf '%s\n' "$OPENCLAW_HOME/skills"
      else
        printf '%s\n' "$INSTALL_HOME/.openclaw/skills"
      fi
      ;;
    hermes)
      if [[ $HOME_WAS_OVERRIDDEN -eq 0 && -n "${HERMES_HOME:-}" ]]; then
        printf '%s\n' "$HERMES_HOME/skills"
      else
        printf '%s\n' "$INSTALL_HOME/.hermes/skills"
      fi
      ;;
    *) return 2 ;;
  esac
}

case "$TARGET" in
  all) TARGETS=(shared codex claude grok openclaw hermes) ;;
  shared|codex|claude|grok|openclaw|hermes) TARGETS=("$TARGET") ;;
  *)
    printf 'Unknown agent target: %s\n' "$TARGET" >&2
    usage >&2
    exit 2
    ;;
esac

DESTINATIONS=()
for agent in "${TARGETS[@]}"; do
  root="$(root_for "$agent")"
  destination="$root/auto-edit-video"
  DESTINATIONS+=("$agent|$destination")
  if [[ -e "$destination" && $FORCE -eq 0 ]]; then
    printf 'Destination exists for %s: %s (use --force to replace)\n' "$agent" "$destination" >&2
    exit 3
  fi
done

for entry in "${DESTINATIONS[@]}"; do
  agent="${entry%%|*}"
  destination="${entry#*|}"
  if [[ $DRY_RUN -eq 1 ]]; then
    printf '[dry-run] %s -> %s\n' "$agent" "$destination"
    continue
  fi
  parent="$(dirname "$destination")"
  mkdir -p "$parent"
  temporary_root="$(mktemp -d "$parent/.auto-edit-video.install.XXXXXX")"
  temporary="$temporary_root/auto-edit-video"
  cp -R "$SOURCE_DIR" "$temporary"
  find "$temporary" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
  find "$temporary" -type f -name '*.pyc' -delete 2>/dev/null || true
  if [[ -e "$destination" ]]; then
    backup="$destination.backup-$(date -u +%Y%m%dT%H%M%SZ)"
    mv "$destination" "$backup"
    printf 'Backed up %s to %s\n' "$agent" "$backup"
  fi
  mv "$temporary" "$destination"
  rmdir "$temporary_root"
  printf 'Installed for %s: %s\n' "$agent" "$destination"
done

if [[ $DRY_RUN -eq 0 ]]; then
  printf '%s\n' 'Start a new agent session (or restart the CLI) so the skill catalog refreshes.'
fi
