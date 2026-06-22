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

# set -euo pipefail は意図的に未設定。
# このスクリプトは「SSE 切断 → 1秒待って再接続」を while で回す再接続ループを
# 持ち、curl 終了や非ゼロ終了は正常パスとして扱う必要がある。set -e を入れると
# curl 失敗や `kill ... || true` のような部分エラーで再接続前にループを抜けてしまい
# (≒ watchdog 機能を壊す)、heartbeat.sh のような「1ショット send + sleep」の
# 構造とは要件が異なる。pipefail も同様に pipeline 中の curl 失敗を errno 化して
# しまうため未設定。未定義参照は `${VAR:-}` で個別に防御している。

RELAY_URL="${RELAY_URL:-http://127.0.0.1:8765}"
CHANNEL="$1"
HANDLE="$2"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OW_PARENT_PID="${OW_PARENT_PID:-}"

if [ -z "$CHANNEL" ] || [ -z "$HANDLE" ]; then
    echo "Usage: recv.sh <channel_code> <handle>" >&2
    exit 1
fi

# curl と python3 を mkfifo 経由で個別の bg job として起動することで、
# curl と python3 双方の PID を `$!` で個別に取得できる。pipeline
# `curl | python3` だと `$!` は末尾の python3 PID しか取れず、SSE 無音時に
# curl が SIGPIPE 連鎖死しないケースで孤児として残るリスクが残る (M#412 §B-3)。
#
# cleanup trap は PIPE_PID (python3) と CURL_PID (curl) の両方を明示 kill し、
# 名前付きパイプ (FIFO) も削除する。EXIT/HUP/INT/TERM のいずれで起こされても
# 同じ後始末経路に流れる。
cleanup() {
    if [ -n "${PIPE_PID:-}" ]; then
        kill "$PIPE_PID" 2>/dev/null || true
    fi
    if [ -n "${CURL_PID:-}" ]; then
        kill "$CURL_PID" 2>/dev/null || true
    fi
    if [ -n "${FIFO_DIR:-}" ] && [ -d "$FIFO_DIR" ]; then
        rm -rf "$FIFO_DIR"
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
    # mkfifo 経由で curl の stdout を python3 の stdin に繋ぐことで、両プロセスを
    # 別々の bg job として起動できる (= 個別 PID を `$!` で取得可能)。
    #
    # `mktemp -d` で専用ディレクトリを作り、その下に FIFO を mkfifo する。
    # `mktemp -u` (パス名のみ生成) は TOCTOU 競合があり deprecated 的な用法。
    # -d はディレクトリ自体を atomic に作成するため安全。
    FIFO_DIR="$(mktemp -d -t ow_recv.XXXXXX)"
    FIFO="$FIFO_DIR/fifo"
    if ! mkfifo "$FIFO" 2>/dev/null; then
        rm -rf "$FIFO_DIR"
        unset FIFO_DIR FIFO
        sleep 1
        continue
    fi

    curl -sN --get \
        --data-urlencode "channel=${CHANNEL}" \
        --data-urlencode "handle=${HANDLE}" \
        "${RELAY_URL}/stream" \
        > "$FIFO" &
    CURL_PID=$!

    python3 -u "${SCRIPT_DIR}/recv_filter.py" "${HANDLE}" < "$FIFO" &
    PIPE_PID=$!

    # pipeline 終了 or 親死亡まで待つ。1秒ごとに親監視。
    # PIPE_PID (python3) 死亡時に CURL_PID も明示的に kill する。
    # SSE 無音時に curl が SIGPIPE 連鎖死しないケースの保険。
    while kill -0 "$PIPE_PID" 2>/dev/null; do
        if ! parent_alive; then
            kill "$PIPE_PID" 2>/dev/null || true
            kill "$CURL_PID" 2>/dev/null || true
            wait "$PIPE_PID" 2>/dev/null || true
            wait "$CURL_PID" 2>/dev/null || true
            exit 0
        fi
        sleep 1
    done
    kill "$CURL_PID" 2>/dev/null || true
    wait "$PIPE_PID" 2>/dev/null || true
    wait "$CURL_PID" 2>/dev/null || true
    unset PIPE_PID CURL_PID

    rm -rf "$FIFO_DIR"
    unset FIFO_DIR FIFO

    # SSE 切断後は1秒待ってから再接続を試みる。親死亡なら次の while 条件で抜ける。
    sleep 1
done
