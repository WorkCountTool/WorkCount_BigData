#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")/.."
build_root="$PWD/build/portable-mac"
output_root="$PWD/release/WorkCount-macOS-$(uname -m)"
export PYINSTALLER_CONFIG_DIR="$build_root/config"
mkdir -p "$PYINSTALLER_CONFIG_DIR"

python -m PyInstaller --noconfirm --clean --onedir --name WorkCountServer \
  --distpath "$build_root/dist" --workpath "$build_root/work" --specpath "$build_root/spec" app.py
python -m PyInstaller --noconfirm --clean --onedir --name PlatformConnector --collect-all selenium \
  --distpath "$build_root/dist" --workpath "$build_root/work" --specpath "$build_root/spec" tools/platform_sync.py
mkdir -p "$output_root/templates"
cp -R "$build_root/dist/WorkCountServer" "$output_root/"
cp -R "$build_root/dist/PlatformConnector" "$output_root/"
cp -R static "$output_root/"
cp '启动工作量系统.command' '离线使用说明.txt' "$output_root/"
chmod +x "$output_root/启动工作量系统.command"
ditto -c -k --keepParent "$output_root" "$PWD/release/WorkCount-macOS-$(uname -m)-no-templates.zip"
