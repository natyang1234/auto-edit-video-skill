#!/usr/bin/env bash
# Install the Taiwan-tuned speech recogniser used for zh-TW projects.
#
# It lives in its own interpreter for two reasons: the system Python on macOS
# is externally managed and refuses package installs, and the runtime links an
# OpenMP library that aborts when loaded beside the pipeline's own.
#
# Usage: install_breeze.sh [env-dir]        (default: ~/.auto-edit/breeze-env)
set -euo pipefail

ENV_DIR="${1:-$HOME/.auto-edit/breeze-env}"

command -v uv >/dev/null 2>&1 || {
  echo "uv is required: https://docs.astral.sh/uv/" >&2
  exit 1
}

mkdir -p "$(dirname "$ENV_DIR")"
if [ -L "$ENV_DIR" ]; then
  # uv would follow the link and build the environment wherever it points,
  # leaving this path still borrowed from somewhere that may not persist.
  echo "$ENV_DIR is a symlink to $(readlink "$ENV_DIR")." >&2
  echo "Remove it first so the runtime installs here for real." >&2
  exit 1
fi
uv venv "$ENV_DIR"
uv pip install --python "$ENV_DIR/bin/python" mlx-whisper

"$ENV_DIR/bin/python" -c "import mlx_whisper" || {
  echo "install completed but mlx-whisper is not importable" >&2
  exit 1
}

echo "Breeze runtime ready at $ENV_DIR"
echo "The model itself downloads on first use (about 3 GB)."
