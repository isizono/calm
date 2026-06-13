#!/usr/bin/env bash
# SSE購読 + recv_filter + 自動再接続
# 使い方: recv.sh <channel_code> <handle>
# SSE切断時に1秒間隔で自動再接続する（M#219 §2.5）

RELAY_URL="${RELAY_URL:-http://127.0.0.1:8765}"
CHANNEL="$1"
HANDLE="$2"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -z "$CHANNEL" ] || [ -z "$HANDLE" ]; then
    echo "Usage: recv.sh <channel_code> <handle>" >&2
    exit 1
fi

while true; do
    curl -sN --get \
        --data-urlencode "channel=${CHANNEL}" \
        --data-urlencode "handle=${HANDLE}" \
        "${RELAY_URL}/stream" \
        | python3 -u "${SCRIPT_DIR}/recv_filter.py" "${HANDLE}"
    sleep 1
done
