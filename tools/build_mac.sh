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

mkdir -p "$output_root/templates"
rm -f "$output_root/templates/张子豪-表1：人工智能学院（部）2026-2027学年第一学期工作量预算汇总表.xlsx"
cp -R "$build_root/dist/WorkCountServer" "$output_root/"
cp -R "$build_root/dist/PlatformConnector" "$output_root/"
cp -R static "$output_root/"
cp templates/*.xlsx "$output_root/templates/"
cp '启动工作量系统.command' '离线使用说明.txt' "$output_root/"
chmod +x "$output_root/启动工作量系统.command"
ditto -c -k --keepParent "$output_root" "$source_root/release/$(basename "$output_root").zip"
echo "已生成 $source_root/release/$(basename "$output_root").zip"
