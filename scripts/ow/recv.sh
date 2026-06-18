#!/usr/bin/env bash
# SSE購読 + recv_filter + 自動再接続
# 使い方: recv.sh <channel_code> <handle>
# SSE切断時に1秒間隔で自動再接続する
#
# 環境変数:
#   RELAY_URL      中継サーバーURL (default: http://127.0.0.1:8765)
#   OW_PARENT_PID  監視対象の親PID。指定時は親プロセス消滅でループ即終了。
#                  ow_service.py の spawn 経路から注入される。未指定なら親監視は無効
#                  (claude本体死亡時にppid=1で生き残る旧挙動。後方互換)。

RELAY_URL="${RELAY_URL:-http://127.0.0.1:8765}"
CHANNEL="$1"
HANDLE="$2"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OW_PARENT_PID="${OW_PARENT_PID:-}"

if [ -z "$CHANNEL" ] || [ -z "$HANDLE" ]; then
    echo "Usage: recv.sh <channel_code> <handle>" >&2
    exit 1
fi

# B案: trap でシグナル受信時に curl を確実に殺してから exit。
# bg化された後の親 SIGHUP は macOS デフォルトで届かないため A案(PPID watchdog)
# の補助にすぎないが、claude が正常 exit する SIGTERM 経路には反応する。
cleanup() {
    if [ -n "${CURL_PID:-}" ]; then
        kill "$CURL_PID" 2>/dev/null || true
    fi
    exit 0
}
trap cleanup EXIT HUP INT TERM

# A案: 親プロセス監視。OW_PARENT_PID が指定されていれば、その PID が
# 生存していない時点でループを抜ける（presence ゾンビ防止）。
parent_alive() {
    if [ -z "$OW_PARENT_PID" ]; then
        return 0  # 未指定なら無効（後方互換）
    fi
    kill -0 "$OW_PARENT_PID" 2>/dev/null
}

while parent_alive; do
    # SSE は long-poll で意図的に hang させるため curl 自体には max-time を付けない。
    # 親死亡を素早く検出するために curl を bg で起動し、内側ループで親監視する。
    curl -sN --get \
        --data-urlencode "channel=${CHANNEL}" \
        --data-urlencode "handle=${HANDLE}" \
        "${RELAY_URL}/stream" \
        | python3 -u "${SCRIPT_DIR}/recv_filter.py" "${HANDLE}" &
    CURL_PID=$!

    # curl 終了 or 親死亡まで待つ。1秒ごとに親監視。
    while kill -0 "$CURL_PID" 2>/dev/null; do
        if ! parent_alive; then
            kill "$CURL_PID" 2>/dev/null || true
            wait "$CURL_PID" 2>/dev/null || true
            exit 0
        fi
        sleep 1
    done
    wait "$CURL_PID" 2>/dev/null || true
    unset CURL_PID

    # SSE 切断後は1秒待ってから再接続を試みる。親死亡なら次の while 条件で抜ける。
    sleep 1
done
