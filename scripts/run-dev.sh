#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
venv_dir="$project_dir/.venv"

if [[ ! -x "$venv_dir/bin/zscribe-linux" ]]; then
  if ! "$venv_dir/bin/python" -m pip --version >/dev/null 2>&1; then
    if ! python3 -m venv --clear --system-site-packages "$venv_dir"; then
      echo "Warning: python3-venv is unavailable; opening the GTK UI directly." >&2
      echo "Install the README prerequisites to enable Live captions." >&2
      cd "$project_dir"
      exec python3 -m zscribe.app "$@"
    fi
  fi
  "$venv_dir/bin/python" -m pip install -e "$project_dir"
fi
exec "$venv_dir/bin/zscribe-linux" "$@"
