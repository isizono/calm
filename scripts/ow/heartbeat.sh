#!/usr/bin/env bash
# workerのheartbeat eventを定期送信するバックグラウンドループ
#
# 使い方:
#   PHASE_FILE=/tmp/ow_hb_phase_<alias> bash heartbeat.sh <channel_code> <handle> &
#   echo "loading" > $PHASE_FILE   # loading=10s
#   echo "ready"   > $PHASE_FILE   # ready/working/draining=30s
#   rm $PHASE_FILE                 # ファイル削除でループ終了
#
# 環境変数:
#   RELAY_URL          中継サーバーURL (default: http://127.0.0.1:8765)
#   PHASE_FILE         現在フェーズを記録するファイルパス (default: /tmp/ow_hb_phase_<PID>)
#   OW_PARENT_PID      監視対象の親PID。指定時は親プロセス消滅でループ即終了。
#                      ow_service.py の spawn 経路から注入される。未指定なら親監視は無効
#                      (claude本体死亡時にppid=1で生き残る旧挙動。後方互換)。
#   OW_CURL_TIMEOUT    relay /send 1回あたりの最大時間（秒）。curl hang による
#                      heartbeat 停止を防ぐ。default: 15
#   OW_CURL_CONNECT_TIMEOUT
#                      curl の connect timeout（秒）。default: 5
#   OW_MCP_URL         MCP server /health のURL (default: http://127.0.0.1:52837)
#   OW_MCP_FAIL_THRESHOLD
#                      MCP /health 連続失敗回数の閾値 (default: 5)。
#                      これを超えると safe state の worker は self-exit する。
#   OW_MCP_UPTIME_MIN_SEC
#                      self-exit を発火するために必要な heartbeat プロセスの最小経過秒
#                      (default: 300=5分)。spawn 直後の不安定期保護。
#   OW_MCP_CONNECT_TIMEOUT
#                      MCP /health curl の connect timeout（秒）。default: 2
#   OW_MCP_MAX_TIME    MCP /health curl の全体タイムアウト（秒）。default: 3
#   OW_DISABLE_MCP_SELF_EXIT
#                      "1" を渡すと self-exit を無効化（デバッグ・検証用）。
#   OW_HB_FAIL_THRESHOLD
#                      relay /send 連続失敗回数の閾値 (default: 5)。
#                      これを超えると idle-timeout として worker を kill する
#                      (D#2853 機構1: relay 不通検知)。
#   OW_DONE_TIMEOUT_SEC
#                      PHASE=done になってから close 未受領で kill する閾値秒
#                      (default: 600=10分)。dispatcher が done 検証 + close 送信
#                      を完了しないまま worker が滞留するのを防ぐ
#                      (D#2853 機構2: done 後タイマー)。
#   OW_DISABLE_IDLE_TIMEOUT
#                      "1" を渡すと idle-timeout (機構1+機構2) を無効化
#                      （デバッグ・検証用）。

set -euo pipefail

RELAY_URL="${RELAY_URL:-http://127.0.0.1:8765}"
CHANNEL="${1:?channel_code is required}"
HANDLE="${2:?handle is required}"
PHASE_FILE="${PHASE_FILE:-/tmp/ow_hb_phase_$$}"
OW_PARENT_PID="${OW_PARENT_PID:-}"
OW_CURL_TIMEOUT="${OW_CURL_TIMEOUT:-15}"
OW_CURL_CONNECT_TIMEOUT="${OW_CURL_CONNECT_TIMEOUT:-5}"
OW_MCP_URL="${OW_MCP_URL:-http://127.0.0.1:52837}"
OW_MCP_FAIL_THRESHOLD="${OW_MCP_FAIL_THRESHOLD:-5}"
OW_MCP_UPTIME_MIN_SEC="${OW_MCP_UPTIME_MIN_SEC:-300}"
OW_MCP_CONNECT_TIMEOUT="${OW_MCP_CONNECT_TIMEOUT:-2}"
OW_MCP_MAX_TIME="${OW_MCP_MAX_TIME:-3}"
OW_DISABLE_MCP_SELF_EXIT="${OW_DISABLE_MCP_SELF_EXIT:-0}"
OW_HB_FAIL_THRESHOLD="${OW_HB_FAIL_THRESHOLD:-5}"
OW_DONE_TIMEOUT_SEC="${OW_DONE_TIMEOUT_SEC:-600}"
OW_DISABLE_IDLE_TIMEOUT="${OW_DISABLE_IDLE_TIMEOUT:-0}"

MCP_FAIL_COUNT_FILE="/tmp/ow-mcp-fail-$$"
HB_FAIL_COUNT_FILE="/tmp/ow-hb-fail-$$"
DONE_SINCE_FILE="/tmp/ow-done-since-$$"
HEARTBEAT_STARTED_AT=$(date +%s)

# B案: trap で PHASE_FILE を自殺時に掃除する。bg化された後の親 SIGHUP は
# macOS デフォルトで届かないため A案(PPID watchdog) の補助にすぎないが、
# claude が正常 exit するときの SIGTERM や、bash の自発的 EXIT には反応する。
cleanup() {
    rm -f "$PHASE_FILE" 2>/dev/null || true
    rm -f "$MCP_FAIL_COUNT_FILE" 2>/dev/null || true
    rm -f "$HB_FAIL_COUNT_FILE" 2>/dev/null || true
    rm -f "$DONE_SINCE_FILE" 2>/dev/null || true
}
trap cleanup EXIT HUP INT TERM

# 初期フェーズを loading に設定（呼び出し側が事前に書いていない場合）
[ -f "$PHASE_FILE" ] || echo "loading" > "$PHASE_FILE"

# A案: 親プロセス監視。OW_PARENT_PID が指定されていれば、その PID が
# 生存していない時点でループを抜ける（presence ゾンビ防止）。
parent_alive() {
    if [ -z "$OW_PARENT_PID" ]; then
        return 0  # 未指定なら無効（後方互換）
    fi
    kill -0 "$OW_PARENT_PID" 2>/dev/null
}

# MCP /health の死活確認。成功=0 / 失敗=1。
mcp_health_check() {
    curl -sf \
        --connect-timeout "$OW_MCP_CONNECT_TIMEOUT" \
        --max-time "$OW_MCP_MAX_TIME" \
        "${OW_MCP_URL}/health" > /dev/null 2>&1
}

# self-exit の安全条件: w-* handle かつ PHASE=ready かつ uptime>=閾値。
# 安全とみなせば 0、対象外なら 1 を返す。
is_safe_for_self_exit() {
    case "$HANDLE" in
        w-*) ;;
        *) return 1 ;;
    esac
    [ "$PHASE" = "ready" ] || return 1
    # `local var=$(cmd)` は local の exit code が常に 0 になり set -e をすり抜ける。
    # 宣言と代入は分ける。
    local now
    now=$(date +%s)
    local elapsed
    elapsed=$(( now - HEARTBEAT_STARTED_AT ))
    [ "$elapsed" -ge "$OW_MCP_UPTIME_MIN_SEC" ] || return 1
    return 0
}

# self-exit: relay に通知してから tmux pane を kill する。
# relay が死んでいる場合は通知失敗を許容して kill のみ進める。
self_exit_due_to_mcp_loss() {
    local fail_count="$1"
    local body
    body=$(printf '{"v":1,"kind":"event","from":"%s","to":"*","data":{"type":"self-exit","reason":"mcp-loss","fail_count":%d}}' \
        "$HANDLE" "$fail_count")
    curl -s -X POST \
        --connect-timeout "$OW_CURL_CONNECT_TIMEOUT" \
        --max-time "$OW_CURL_TIMEOUT" \
        -H "Content-Type: application/json" \
        -d "{\"channel\":\"${CHANNEL}\",\"handle\":\"${HANDLE}\",\"body\":${body}}" \
        "${RELAY_URL}/send" > /dev/null 2>&1 || true
    if [ -n "${TMUX_PANE:-}" ]; then
        tmux kill-pane -t "$TMUX_PANE" 2>/dev/null || true
    elif [ -n "$OW_PARENT_PID" ]; then
        # tmux pane が不明な場合は worker (claude) 本体を親PID経由で kill する。
        # 主目的「累積メモリ解放」は worker プロセス終了で達成されるため、ここを抜くと
        # heartbeat.sh だけ消えて worker が居残り、PR の意図を満たさなくなる。
        echo "heartbeat.sh: TMUX_PANE unset; killing OW_PARENT_PID=$OW_PARENT_PID" >&2
        kill -TERM "$OW_PARENT_PID" 2>/dev/null || true
    else
        echo "heartbeat.sh: TMUX_PANE and OW_PARENT_PID both unset; worker process will leak" >&2
    fi
    exit 0
}

# idle-timeout self-shutdown (D#2853): relay に terminated(cause:idle-timeout) を
# 通知してから worker を kill する。reason は "hb-fail" (機構1) または
# "done-stall" (機構2) を渡す。
shutdown_for_idle_timeout() {
    local reason="$1"
    local detail="$2"
    local body
    body=$(printf '{"v":1,"kind":"event","from":"%s","to":"dispatcher","data":{"type":"state","state":"terminated","cause":"idle-timeout","reason":"%s","detail":"%s"}}' \
        "$HANDLE" "$reason" "$detail")
    curl -s -X POST \
        --connect-timeout "$OW_CURL_CONNECT_TIMEOUT" \
        --max-time "$OW_CURL_TIMEOUT" \
        -H "Content-Type: application/json" \
        -d "{\"channel\":\"${CHANNEL}\",\"handle\":\"${HANDLE}\",\"body\":${body}}" \
        "${RELAY_URL}/send" > /dev/null 2>&1 || true
    if [ -n "${TMUX_PANE:-}" ]; then
        tmux kill-pane -t "$TMUX_PANE" 2>/dev/null || true
    elif [ -n "$OW_PARENT_PID" ]; then
        echo "heartbeat.sh: idle-timeout (reason=$reason); killing OW_PARENT_PID=$OW_PARENT_PID" >&2
        kill -TERM "$OW_PARENT_PID" 2>/dev/null || true
    else
        echo "heartbeat.sh: idle-timeout (reason=$reason); TMUX_PANE and OW_PARENT_PID both unset; worker process will leak" >&2
    fi
    exit 0
}

while [ -f "$PHASE_FILE" ] && parent_alive; do
    PHASE=$(cat "$PHASE_FILE" 2>/dev/null || echo "ready")

    if [ "$PHASE" = "loading" ]; then
        INTERVAL="${HEARTBEAT_INTERVAL_LOADING:-10}"
    else
        INTERVAL="${HEARTBEAT_INTERVAL_DEFAULT:-30}"
    fi

    # 機構2 (D#2853): PHASE=done が継続している秒数を計測し、閾値超過で
    # worker を kill する。done 以外に戻ったらカウンタファイルを消してリセット。
    if [ "$OW_DISABLE_IDLE_TIMEOUT" != "1" ]; then
        if [ "$PHASE" = "done" ]; then
            if [ ! -f "$DONE_SINCE_FILE" ]; then
                date +%s > "$DONE_SINCE_FILE"
            fi
            done_since=$(cat "$DONE_SINCE_FILE" 2>/dev/null || echo 0)
            done_now=$(date +%s)
            done_elapsed=$(( done_now - done_since ))
            if [ "$done_elapsed" -ge "$OW_DONE_TIMEOUT_SEC" ]; then
                shutdown_for_idle_timeout "done-stall" "${done_elapsed}s in done phase (threshold ${OW_DONE_TIMEOUT_SEC}s)"
            fi
        else
            rm -f "$DONE_SINCE_FILE" 2>/dev/null || true
        fi
    fi

    BODY=$(printf '{"v":1,"kind":"event","from":"%s","to":"*","data":{"type":"heartbeat","phase":"%s"}}' \
        "$HANDLE" "$PHASE")

    # C案: curl にタイムアウトを付与。relay hang による heartbeat 停止を防ぐ。
    # --connect-timeout: TCP 接続確立まで / --max-time: 全体（接続+送受信）。
    # 機構1 (D#2853): 送信成否で連続失敗カウンタを更新、閾値超過で worker を kill。
    if curl -s -X POST \
        --connect-timeout "$OW_CURL_CONNECT_TIMEOUT" \
        --max-time "$OW_CURL_TIMEOUT" \
        -H "Content-Type: application/json" \
        -d "{\"channel\":\"${CHANNEL}\",\"handle\":\"${HANDLE}\",\"body\":${BODY}}" \
        "${RELAY_URL}/send" > /dev/null 2>&1; then
        echo 0 > "$HB_FAIL_COUNT_FILE"
    else
        if [ "$OW_DISABLE_IDLE_TIMEOUT" != "1" ]; then
            hb_n=$(( $(cat "$HB_FAIL_COUNT_FILE" 2>/dev/null || echo 0) + 1 ))
            echo "$hb_n" > "$HB_FAIL_COUNT_FILE"
            if [ "$hb_n" -ge "$OW_HB_FAIL_THRESHOLD" ]; then
                shutdown_for_idle_timeout "hb-fail" "${hb_n} consecutive heartbeat send failures"
            fi
        fi
    fi

    # MCP /health チェックと self-exit 判定。
    # OW_DISABLE_MCP_SELF_EXIT=1 で無効化可能。
    if [ "$OW_DISABLE_MCP_SELF_EXIT" != "1" ]; then
        if mcp_health_check; then
            echo 0 > "$MCP_FAIL_COUNT_FILE"
        else
            n=$(( $(cat "$MCP_FAIL_COUNT_FILE" 2>/dev/null || echo 0) + 1 ))
            echo "$n" > "$MCP_FAIL_COUNT_FILE"
            if [ "$n" -ge "$OW_MCP_FAIL_THRESHOLD" ] && is_safe_for_self_exit; then
                self_exit_due_to_mcp_loss "$n"
            fi
        fi
    fi

    sleep "$INTERVAL"
done
