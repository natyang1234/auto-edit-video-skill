#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$REPO_ROOT/skills/auto-edit-video"
TARGET="all"
INSTALL_HOME="$HOME"
HOME_WAS_OVERRIDDEN=0
FORCE=0
DRY_RUN=0
CHECK=0

usage() {
  printf '%s\n' \
    'Usage: ./install.sh [--agent TARGET] [--home PATH] [--force] [--dry-run] [--check]' \
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
    --check)
      CHECK=1
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
  if [[ $CHECK -eq 0 && -e "$destination" && $FORCE -eq 0 ]]; then
    printf 'Destination exists for %s: %s (use --force to replace)\n' "$agent" "$destination" >&2
    exit 3
  fi
done

managed_file_list() {
  local tree="$1"
  (
    cd "$tree"
    find . -type f \
      ! -path '*/.pytest_cache/*' \
      ! -path '*/.ruff_cache/*' \
      ! -path '*/__pycache__/*' \
      ! -name '*.pyc' \
      -print | LC_ALL=C sort
  )
}

tree_manifest() {
  local tree="$1"
  (
    cd "$tree"
    managed_file_list . | while IFS= read -r relative_file; do
      digest="$(shasum -a 256 "$relative_file" | awk '{print $1}')"
      printf '%s\t%s\n' "$digest" "$relative_file"
    done
  )
}

check_destination() {
  local agent="$1"
  local destination="$2"
  if [[ ! -d "$destination" ]]; then
    printf '[drift] %s missing: %s\n' "$agent" "$destination" >&2
    return 1
  fi
  if ! diff -u <(tree_manifest "$SOURCE_DIR") <(tree_manifest "$destination"); then
    printf '[drift] %s differs from source: %s\n' "$agent" "$destination" >&2
    return 1
  fi
  printf '[check] %s matches source: %s\n' "$agent" "$destination"
}

if [[ $CHECK -eq 1 ]]; then
  check_failed=0
  for entry in "${DESTINATIONS[@]}"; do
    agent="${entry%%|*}"
    destination="${entry#*|}"
    check_destination "$agent" "$destination" || check_failed=1
  done
  exit "$check_failed"
fi

INSTALL_TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_ROOT="${AUTO_EDIT_BACKUP_ROOT:-$INSTALL_HOME/.skills-backup-quarantine/auto-edit-video}"
BACKUP_BATCH="$BACKUP_ROOT/$INSTALL_TIMESTAMP"
STAGED=()

# Stage and verify every target before replacing any installed copy. A staging
# failure therefore leaves all live skill directories untouched.
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
  find "$temporary" -type d -name '.pytest_cache' -prune -exec rm -rf {} + 2>/dev/null || true
  find "$temporary" -type d -name '.ruff_cache' -prune -exec rm -rf {} + 2>/dev/null || true
  find "$temporary" -type f -name '*.pyc' -delete 2>/dev/null || true
  if ! diff -u <(tree_manifest "$SOURCE_DIR") <(tree_manifest "$temporary"); then
    printf 'Staged copy differs from source for %s\n' "$agent" >&2
    exit 5
  fi
  STAGED+=("$agent|$destination|$temporary_root|$temporary")
done

if [[ $DRY_RUN -eq 1 ]]; then
  exit 0
fi

source_tree_sha="$(tree_manifest "$SOURCE_DIR" | shasum -a 256 | awk '{print $1}')"

for entry in "${STAGED[@]}"; do
  agent="${entry%%|*}"
  rest="${entry#*|}"
  destination="${rest%%|*}"
  rest="${rest#*|}"
  temporary_root="${rest%%|*}"
  temporary="${rest#*|}"
  if [[ -e "$destination" ]]; then
    unmanaged="$(comm -23 <(managed_file_list "$destination") <(managed_file_list "$SOURCE_DIR"))"
    if [[ -n "$unmanaged" ]]; then
      printf 'Refusing to replace %s because it contains files not present in source:\n%s\n' "$destination" "$unmanaged" >&2
      exit 4
    fi
    backup="$BACKUP_BATCH/$agent/auto-edit-video"
    mkdir -p "$(dirname "$backup")"
    mv "$destination" "$backup"
    printf 'Backed up %s to %s\n' "$agent" "$backup"
    mkdir -p "$BACKUP_BATCH"
    chmod 700 "$BACKUP_ROOT" "$BACKUP_BATCH" "$(dirname "$backup")"
    if [[ ! -f "$BACKUP_BATCH/MANIFEST.tsv" ]]; then
      printf 'agent\tdestination\tbackup\tsource_tree_sha256\n' > "$BACKUP_BATCH/MANIFEST.tsv"
      printf '%s\n' \
        'Independent restore procedure (does not call install.sh):' \
        '1. Move the current destination aside to a new, timestamped failed path.' \
        '2. Move the backup path from MANIFEST.tsv back to destination.' \
        '3. Run install.sh --agent TARGET --check and inspect the expected drift.' \
        > "$BACKUP_BATCH/RESTORE.txt"
      chmod 600 "$BACKUP_BATCH/MANIFEST.tsv" "$BACKUP_BATCH/RESTORE.txt"
    fi
    printf '%s\t%s\t%s\t%s\n' "$agent" "$destination" "$backup" "$source_tree_sha" >> "$BACKUP_BATCH/MANIFEST.tsv"
  fi
  mv "$temporary" "$destination"
  rmdir "$temporary_root"
  check_destination "$agent" "$destination"
  printf 'Installed for %s: %s\n' "$agent" "$destination"
done

if [[ -f "$BACKUP_BATCH/MANIFEST.tsv" ]]; then
  printf 'Recovery manifest: %s\n' "$BACKUP_BATCH/MANIFEST.tsv"
fi
printf '%s\n' 'Start a new agent session (or restart the CLI) so the skill catalog refreshes.'
