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
#   OW_DISABLE_MCP_SELF_EXIT
#                      "1" を渡すと self-exit を無効化（デバッグ・検証用）。

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
OW_DISABLE_MCP_SELF_EXIT="${OW_DISABLE_MCP_SELF_EXIT:-0}"

MCP_FAIL_COUNT_FILE="/tmp/ow-mcp-fail-$$"
HEARTBEAT_STARTED_AT=$(date +%s)

# B案: trap で PHASE_FILE を自殺時に掃除する。bg化された後の親 SIGHUP は
# macOS デフォルトで届かないため A案(PPID watchdog) の補助にすぎないが、
# claude が正常 exit するときの SIGTERM や、bash の自発的 EXIT には反応する。
cleanup() {
    rm -f "$PHASE_FILE" 2>/dev/null || true
    rm -f "$MCP_FAIL_COUNT_FILE" 2>/dev/null || true
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
    curl -sf --connect-timeout 2 --max-time 3 "${OW_MCP_URL}/health" > /dev/null 2>&1
}

# self-exit の安全条件: w-* handle かつ PHASE=ready かつ uptime>=閾値。
# 安全とみなせば 0、対象外なら 1 を返す。
is_safe_for_self_exit() {
    case "$HANDLE" in
        w-*) ;;
        *) return 1 ;;
    esac
    [ "$PHASE" = "ready" ] || return 1
    local now=$(date +%s)
    local elapsed=$(( now - HEARTBEAT_STARTED_AT ))
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

    BODY=$(printf '{"v":1,"kind":"event","from":"%s","to":"*","data":{"type":"heartbeat","phase":"%s"}}' \
        "$HANDLE" "$PHASE")

    # C案: curl にタイムアウトを付与。relay hang による heartbeat 停止を防ぐ。
    # --connect-timeout: TCP 接続確立まで / --max-time: 全体（接続+送受信）。
    curl -s -X POST \
        --connect-timeout "$OW_CURL_CONNECT_TIMEOUT" \
        --max-time "$OW_CURL_TIMEOUT" \
        -H "Content-Type: application/json" \
        -d "{\"channel\":\"${CHANNEL}\",\"handle\":\"${HANDLE}\",\"body\":${BODY}}" \
        "${RELAY_URL}/send" > /dev/null || true

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
