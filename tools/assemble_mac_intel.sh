#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")/.."
source_zip="$PWD/release/ci-artifacts/macos-intel/WorkCount-macOS-x86_64-no-templates.zip"
output_root="$PWD/release/WorkCount-macOS-x86_64"
output_zip="$output_root.zip"

if [ ! -f "$source_zip" ]; then
  echo "找不到 Intel Mac 构建产物：$source_zip" >&2
  exit 1
fi
if [ -e "$output_root" ] || [ -e "$output_zip" ]; then
  echo "完整压缩包已存在，不覆盖：$output_zip" >&2
  exit 1
fi

ditto -x -k "$source_zip" "$PWD/release"
cp templates/*.xlsx "$output_root/templates/"
chmod +x "$output_root/启动工作量系统.command"
ditto -c -k --keepParent "$output_root" "$output_zip"
echo "已生成 $output_zip"
