#!/usr/bin/env bash
# Start the PDF translation web app.
#
#   ./webapp/start.sh [PORT]
#
# Port selection:
#   - prefers $PORT / $1 / 8077
#   - if that port is held by a previous instance of THIS app, kill it and reuse
#     the port (so restarting after a code change keeps the URL stable)
#   - if it is held by something else, fall back to the first free port in
#     8000..9000
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="webapp.app:app"
APP_SIGNATURE="uvicorn ${APP}"   # how our own processes look in `ps`
DEFAULT_PORT="${PORT:-8077}"
PORT_MIN=8000
PORT_MAX=9000
HOST="${HOST:-127.0.0.1}"

listener_pids() { lsof -ti "tcp:$1" -sTCP:LISTEN 2>/dev/null || true; }

# True when every listener on the port is an instance of this app.
is_our_app() {
    local pids="$1" pid
    [ -n "$pids" ] || return 1
    for pid in $pids; do
        ps -p "$pid" -o command= 2>/dev/null | grep -qF "$APP_SIGNATURE" || return 1
    done
    return 0
}

pick_port() {
    local pids
    pids="$(listener_pids "$DEFAULT_PORT")"

    if [ -z "$pids" ]; then
        echo "$DEFAULT_PORT"; return
    fi

    if is_our_app "$pids"; then
        echo "端口 $DEFAULT_PORT 上有本应用的旧实例 (PID: $pids)，正在停止…" >&2
        kill $pids 2>/dev/null || true
        for _ in $(seq 1 20); do          # give it 10s to release the socket
            sleep 0.5
            [ -z "$(listener_pids "$DEFAULT_PORT")" ] && { echo "$DEFAULT_PORT"; return; }
        done
        echo "旧实例未退出，强制结束…" >&2
        kill -9 $pids 2>/dev/null || true
        sleep 1
        echo "$DEFAULT_PORT"; return
    fi

    echo "端口 $DEFAULT_PORT 被其他程序占用 (PID: $pids)，改用其他端口…" >&2
    local p
    for ((p = PORT_MIN; p <= PORT_MAX; p++)); do
        [ "$p" = "$DEFAULT_PORT" ] && continue
        [ -z "$(listener_pids "$p")" ] && { echo "$p"; return; }
    done
    echo "错误：$PORT_MIN-$PORT_MAX 之间没有可用端口。" >&2
    exit 1
}

cd "$REPO_DIR"

# Prefer the project venv; fall back to whatever python is active.
if [ -x .venv/bin/uvicorn ]; then
    UVICORN=(.venv/bin/uvicorn)
elif command -v uvicorn >/dev/null 2>&1; then
    UVICORN=(uvicorn)
else
    echo "错误：找不到 uvicorn。请先按 webapp/README.md 安装依赖。" >&2
    exit 1
fi

if [ $# -ge 1 ]; then DEFAULT_PORT="$1"; fi
port="$(pick_port)"

echo "启动中… 首次运行需要下载版面模型和字体，请稍候。"
echo "地址: http://${HOST}:${port}"
exec "${UVICORN[@]}" "$APP" --host "$HOST" --port "$port"
