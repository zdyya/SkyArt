#!/usr/bin/env bash
# 一键启动 SkyArt 后端（开发模式，支持 Ctrl+C 正常退出）
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 如果 8764 端口已被占用，先清掉
if lsof -ti :8764 &>/dev/null; then
  echo "⚠️  端口 8764 已被占用，正在清理..."
  kill -9 $(lsof -ti :8764) 2>/dev/null || true
  sleep 0.5
fi

# 创建虚拟环境（首次运行）
if [ ! -d ".venv" ]; then
  echo "🔧  创建 Python 虚拟环境..."
  python3.12 -m venv .venv
fi

source .venv/bin/activate

# 安装依赖（有变化时自动更新）
pip install -q -r requirements.txt

# 复制 .env（首次运行）
if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "📝  已创建 .env 文件，请填写 API Key 后重启"
fi

echo "🚀  SkyArt Backend 启动中... http://127.0.0.1:8764"
echo "📚  API 文档: http://127.0.0.1:8764/docs"
echo "👉  按 Ctrl+C 可正常退出"

# 捕获 Ctrl+C，确保杀掉整个进程组
trap 'echo ""; echo "👋 正在关闭后端..."; kill 0; exit 0' INT TERM

# --reload 仅在开发时使用（单独启动时）
# Electron 内嵌启动走 main.js，不经过此脚本，不带 --reload
uvicorn main:app --host 127.0.0.1 --port 8764 --reload &
UVICORN_PID=$!

wait $UVICORN_PID
