#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
venv_dir="${XDG_DATA_HOME:-$HOME/.local/share}/zscribe/venv"
bin_dir="${HOME}/.local/bin"
desktop_dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
icon_root="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"

python3 -m venv --system-site-packages "$venv_dir"
"$venv_dir/bin/python" -m pip install --upgrade pip
"$venv_dir/bin/python" -m pip install "$project_dir"
mkdir -p "$bin_dir" "$desktop_dir"
ln -sfn "$venv_dir/bin/zscribe-linux" "$bin_dir/zscribe-linux"
install -m 0644 "$project_dir/data/com.tanchunsiong.ZScribeLinux.desktop" \
  "$desktop_dir/com.tanchunsiong.ZScribeLinux.desktop"
for icon_size in 32 48 64 128 256 512 1024; do
  icon_dir="$icon_root/${icon_size}x${icon_size}/apps"
  mkdir -p "$icon_dir"
  install -m 0644 \
    "$project_dir/data/icons/hicolor/${icon_size}x${icon_size}/apps/com.tanchunsiong.ZScribeLinux.png" \
    "$icon_dir/com.tanchunsiong.ZScribeLinux.png"
done
command -v update-desktop-database >/dev/null && update-desktop-database "$desktop_dir" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -f -t "$icon_root" || true

echo "Z Scribe is installed. Open it from the Ubuntu app grid or run: $bin_dir/zscribe-linux"
