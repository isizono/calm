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
#   RELAY_URL   中継サーバーURL (default: http://127.0.0.1:8765)
#   PHASE_FILE  現在フェーズを記録するファイルパス (default: /tmp/ow_hb_phase_<PID>)

set -euo pipefail

RELAY_URL="${RELAY_URL:-http://127.0.0.1:8765}"
CHANNEL="${1:?channel_code is required}"
HANDLE="${2:?handle is required}"
PHASE_FILE="${PHASE_FILE:-/tmp/ow_hb_phase_$$}"

# 入力バリデーション: " や \ などのJSONインジェクションを防ぐため、
# channel/handle は英数字・アンダースコア・ハイフンのみに制限する。
_id_re='^[A-Za-z0-9_-]+$'
if ! [[ "$CHANNEL" =~ $_id_re ]]; then
    echo "heartbeat.sh: invalid channel format (expected [A-Za-z0-9_-]+): $CHANNEL" >&2
    exit 2
fi
if ! [[ "$HANDLE" =~ $_id_re ]]; then
    echo "heartbeat.sh: invalid handle format (expected [A-Za-z0-9_-]+): $HANDLE" >&2
    exit 2
fi

# 初期フェーズを loading に設定（呼び出し側が事前に書いていない場合）
[ -f "$PHASE_FILE" ] || echo "loading" > "$PHASE_FILE"

while [ -f "$PHASE_FILE" ]; do
    # cat 失敗（ファイル削除との競合）は fallback せずループ終了させる。
    # 旧実装の `|| echo ready` は terminated 後の「余分な ready heartbeat 送出」を招いていた。
    PHASE=$(cat "$PHASE_FILE" 2>/dev/null) || break

    if [ "$PHASE" = "loading" ]; then
        INTERVAL="${HEARTBEAT_INTERVAL_LOADING:-10}"
    else
        INTERVAL="${HEARTBEAT_INTERVAL_DEFAULT:-30}"
    fi

    BODY=$(printf '{"v":1,"kind":"event","from":"%s","to":"*","data":{"type":"heartbeat","phase":"%s"}}' \
        "$HANDLE" "$PHASE")

    curl -s -X POST \
        -H "Content-Type: application/json" \
        -d "{\"channel\":\"${CHANNEL}\",\"handle\":\"${HANDLE}\",\"body\":${BODY}}" \
        "${RELAY_URL}/send" > /dev/null || true

    sleep "$INTERVAL"
done
