#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")/.."
source_root="$PWD"
python_bin="${PYTHON_BIN:-/opt/anaconda3/bin/python}"
build_root="$source_root/build/portable-mac"
output_root="$source_root/release/WorkCount-macOS-$(uname -m)"
export PYINSTALLER_CONFIG_DIR="$build_root/config"
mkdir -p "$PYINSTALLER_CONFIG_DIR"

"$python_bin" -m PyInstaller --noconfirm --clean --onedir --name WorkCountServer \
  --distpath "$build_root/dist" --workpath "$build_root/work" --specpath "$build_root/spec" app.py
"$python_bin" -m PyInstaller --noconfirm --clean --onedir --name PlatformConnector \
  --collect-all selenium --distpath "$build_root/dist" --workpath "$build_root/work" \
  --specpath "$build_root/spec" tools/platform_sync.py

rm -rf "$output_root"
mkdir -p "$output_root/templates"
cp -R "$build_root/dist/WorkCountServer" "$output_root/"
cp -R "$build_root/dist/PlatformConnector" "$output_root/"
cp -R static "$output_root/"
cp templates/*.xlsx "$output_root/templates/"
cp '启动工作量系统.command' '离线使用说明.txt' "$output_root/"
chmod +x "$output_root/启动工作量系统.command"
archive="$source_root/release/$(basename "$output_root").zip"
rm -f "$archive"
"$python_bin" - "$output_root" "$archive" <<'PY'
import os
import sys
import zipfile

source = os.path.abspath(sys.argv[1])
archive = os.path.abspath(sys.argv[2])
root = os.path.basename(source)
with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
    for directory, _, files in os.walk(source):
        relative_directory = os.path.relpath(directory, source)
        if relative_directory != "." and not files:
            output.write(directory, f"{root}/{relative_directory}/")
        for filename in files:
            path = os.path.join(directory, filename)
            relative = os.path.relpath(path, source)
            output.write(path, f"{root}/{relative}")
PY
echo "已生成 $source_root/release/$(basename "$output_root").zip"
