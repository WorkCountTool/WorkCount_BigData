#!/bin/bash
set -u
cd "$(dirname "$0")" || exit 1

if [ ! -x "WorkCountServer/WorkCountServer" ] || [ ! -x "PlatformConnector/PlatformConnector" ]; then
  echo "程序文件不完整，请重新解压整个文件夹。"
  read -r -p "按回车键关闭..." _
  exit 1
fi

if [ ! -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]; then
  echo "请先安装 Google Chrome 浏览器，再启动工作量系统。"
  read -r -p "按回车键关闭..." _
  exit 1
fi

export WORKCOUNT_HOST=127.0.0.1
export WORKCOUNT_PORT=8765
export WORKCOUNT_DB="$PWD/data/workcount.db"
export WORKCOUNT_CHROME_BINARY="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
mkdir -p data

if curl --silent --fail --max-time 1 http://127.0.0.1:8765/health >/dev/null; then
  echo "8765 端口已有工作量系统运行，已打开现有页面。"
  open http://127.0.0.1:8765/
  exit 0
fi

"./WorkCountServer/WorkCountServer" &
server_pid=$!
trap 'kill "$server_pid" 2>/dev/null || true' EXIT INT TERM

ready=0
for _ in {1..30}; do
  if curl --silent --fail --max-time 1 http://127.0.0.1:8765/ready >/dev/null; then
    ready=1
    break
  fi
  if ! kill -0 "$server_pid" 2>/dev/null; then
    break
  fi
  sleep 1
done

if [ "$ready" -eq 1 ]; then
  open http://127.0.0.1:8765/
  echo "工作量系统已启动。关闭此窗口将停止服务。"
  wait "$server_pid"
else
  echo "启动失败：请检查程序文件和三个 Excel 模板是否完整，或确认 8765 端口未被占用。"
  read -r -p "按回车键关闭..." _
fi
