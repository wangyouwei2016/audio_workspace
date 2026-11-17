#!/usr/bin/env bash
#
# 字幕配音小助手 — 启动脚本
#
# 用法:
#   ./start.sh              # 前台启动 (Ctrl+C 停止)
#   ./start.sh --bg         # 后台启动 (日志写入 app_runtime.log)
#   ./start.sh --stop       # 停止后台进程
#   ./start.sh --port 8080  # 指定端口 (默认 5000)
#
set -euo pipefail

# 切换到脚本所在目录，确保相对路径正确
cd "$(dirname "$0")"

PORT=5000
MODE="foreground"
PID_FILE=".app.pid"
LOG_FILE="app_runtime.log"

# ── 解析参数 ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --bg)       MODE="background"; shift ;;
    --stop)     MODE="stop"; shift ;;
    --port)     PORT="${2:?--port 需要一个端口号}"; shift 2 ;;
    -h|--help)
      sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
done

# ── 停止模式 ────────────────────────────────────────────────────────────────
if [[ "$MODE" == "stop" ]]; then
  if [[ -f "$PID_FILE" ]]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
      kill "$PID" && echo "已停止进程 $PID"
    else
      echo "进程 $PID 不存在，清理 PID 文件"
    fi
    rm -f "$PID_FILE"
  else
    echo "未找到 PID 文件，尝试按端口停止..."
    PIDS=$(lsof -ti "tcp:$PORT" || true)
    [[ -n "$PIDS" ]] && kill $PIDS && echo "已停止端口 $PORT 上的进程: $PIDS" || echo "端口 $PORT 上无进程"
  fi
  exit 0
fi

# ── 定位 Python 解释器 ──────────────────────────────────────────────────────
PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "错误: 找不到 $PYTHON，请先安装 Python 3.10+" >&2
  exit 1
fi

# ── 依赖检查 ────────────────────────────────────────────────────────────────
echo "==> 检查依赖..."
if ! "$PYTHON" -c "import flask, dashscope" 2>/dev/null; then
  echo "缺少 Python 依赖，正在安装 flask 和 dashscope..."
  "$PYTHON" -m pip install flask dashscope
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "警告: 未检测到 ffmpeg，音频处理功能将不可用。" >&2
  echo "      macOS 可通过 'brew install ffmpeg' 安装。" >&2
fi

# ── 环境变量检查 ────────────────────────────────────────────────────────────
if [[ ! -f .env ]]; then
  echo "警告: 未找到 .env 文件，将从 .env.example 复制一份，请填入 API Key。" >&2
  [[ -f .env.example ]] && cp .env.example .env
fi

if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  set -a; source .env; set +a
fi

if [[ -z "${DASHSCOPE_API_KEY:-}" || "${DASHSCOPE_API_KEY}" == "your_dashscope_api_key_here" ]]; then
  echo "警告: DASHSCOPE_API_KEY 未配置，转录与 CosyVoice/Qwen3-TTS 功能将不可用。" >&2
fi

# ── 释放端口 ────────────────────────────────────────────────────────────────
if lsof -ti "tcp:$PORT" >/dev/null 2>&1; then
  echo "==> 端口 $PORT 被占用，正在释放..."
  kill $(lsof -ti "tcp:$PORT") 2>/dev/null || true
  sleep 1
fi

URL="http://localhost:$PORT"
echo "==> 启动字幕配音小助手，访问地址: $URL"

# ── 启动 ────────────────────────────────────────────────────────────────────
if [[ "$MODE" == "background" ]]; then
  nohup "$PYTHON" app.py > "$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"
  sleep 2
  if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "已在后台启动 (PID $(cat "$PID_FILE"))，日志: $LOG_FILE"
    echo "停止请运行: ./start.sh --stop"
  else
    echo "启动失败，请查看日志:" >&2
    tail -n 20 "$LOG_FILE" >&2
    rm -f "$PID_FILE"
    exit 1
  fi
else
  exec "$PYTHON" app.py
fi
