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
# (判断保留: 将来 set -u だけ局所的に有効化する余地はあるが、現状の `${PIPE_PID:-}`
#  パターンで実害なし。)

RELAY_URL="${RELAY_URL:-http://127.0.0.1:8765}"
CHANNEL="$1"
HANDLE="$2"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OW_PARENT_PID="${OW_PARENT_PID:-}"

if [ -z "$CHANNEL" ] || [ -z "$HANDLE" ]; then
    echo "Usage: recv.sh <channel_code> <handle>" >&2
    exit 1
fi

# B案: trap でシグナル受信時に pipeline 末尾プロセスを殺してから exit。
# bg化された後の親 SIGHUP は macOS デフォルトで届かないため A案(PPID watchdog)
# の補助にすぎないが、claude が正常 exit する SIGTERM 経路には反応する。
#
# PIPE_PID は `curl ... | python3 ... &` で取得した `$!` の値で、これは
# パイプライン末尾の python3 の PID。これを kill すると python3 が死に、
# 上流の curl は stdout への書き込み時 SIGPIPE で連鎖死する。
# (TODO: SSE 無音時など stdout に書き込みが起きない状況では curl が孤児として
#  生き残るリスクがある。根本対策は別 issue 扱い。)
#
# exit 0 は HUP/INT/TERM 経由で呼ばれた際の終了コードを明示するためのもの。
# EXIT 経由では冗長だが、シグナル経路で「子の状態に引きずられない 0 終了」を
# 確定させる目的で残している。
cleanup() {
    if [ -n "${PIPE_PID:-}" ]; then
        kill "$PIPE_PID" 2>/dev/null || true
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
    # $! はパイプライン末尾 (python3) の PID。これを kill すると python3 が落ち、
    # 上流の curl は SIGPIPE で連鎖死する想定。変数名 PIPE_PID で意図を明示する。
    PIPE_PID=$!

    # pipeline 終了 or 親死亡まで待つ。1秒ごとに親監視。
    while kill -0 "$PIPE_PID" 2>/dev/null; do
        if ! parent_alive; then
            kill "$PIPE_PID" 2>/dev/null || true
            wait "$PIPE_PID" 2>/dev/null || true
            exit 0
        fi
        sleep 1
    done
    wait "$PIPE_PID" 2>/dev/null || true
    unset PIPE_PID

    # SSE 切断後は1秒待ってから再接続を試みる。親死亡なら次の while 条件で抜ける。
    sleep 1
done
