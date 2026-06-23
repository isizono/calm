#!/usr/bin/env bash
# /history を周期 pull して自分宛メッセージを Monitor に流す fallback wrapper。
# 使い方: recv_poll.sh <channel> <handle>
#
# recv.sh (SSE push) の補完経路。push 不発症状に対し、定期 pull で確実に
# 取りこぼしを Monitor (= Claude Code セッションの push 通知) まで届ける。
# recv.sh と並列起動する想定。重複配送は呼び出し側 (Claude Code セッション) の
# msg_id ベース dedup で吸収する。
#
# 環境変数:
#   RELAY_URL              中継サーバーURL (default: http://127.0.0.1:8765)
#   OW_PARENT_PID          監視対象の親PID。指定時は親プロセス消滅でループ即終了。
#                          未指定なら親監視は無効 (recv.sh と同じ後方互換)。
#   OW_POLL_INTERVAL_SEC   pull 間隔 (default: 60)
#   OW_POLL_STATE_FILE     last_msg_id 永続化先
#                          (default: /tmp/ow_recv_poll_<handle>_<channel>.last_msg_id)

RELAY_URL="${RELAY_URL:-http://127.0.0.1:8765}"
INTERVAL="${OW_POLL_INTERVAL_SEC:-60}"
CHANNEL="$1"
HANDLE="$2"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OW_PARENT_PID="${OW_PARENT_PID:-}"
STATE_FILE="${OW_POLL_STATE_FILE:-/tmp/ow_recv_poll_${HANDLE}_${CHANNEL}.last_msg_id}"

if [ -z "$CHANNEL" ] || [ -z "$HANDLE" ]; then
    echo "Usage: recv_poll.sh <channel_code> <handle>" >&2
    exit 1
fi

parent_alive() {
    if [ -z "$OW_PARENT_PID" ]; then
        return 0
    fi
    kill -0 "$OW_PARENT_PID" 2>/dev/null
}

# /history JSON を relay SSE wire format に擬装する整形スクリプト。
# 既存 recv_filter.py の入力フォーマット ("data: {msg_id, body, handle, created_at}")
# に揃えることで、schema 検証と to filter を二重実装せず再利用する。
# 取得済み msg のうち最大 msg_id を state file に書き戻して次周期の since にする。
SSE_FORMAT_SCRIPT='
import json
import sys
from pathlib import Path

state_path = Path(sys.argv[1])
try:
    resp = json.load(sys.stdin)
except json.JSONDecodeError:
    sys.exit(0)
messages = resp.get("messages") or []
if not messages:
    sys.exit(0)
max_id = 0
for msg in messages:
    msg_id = msg.get("msg_id")
    if msg_id is None:
        continue
    if msg_id > max_id:
        max_id = msg_id
    line_payload = {
        "msg_id": msg_id,
        "body": msg.get("body", "{}"),
        "handle": msg.get("handle", ""),
        "created_at": msg.get("created_at", ""),
    }
    print(f"data: {json.dumps(line_payload, ensure_ascii=False)}", flush=True)
if max_id > 0:
    state_path.write_text(str(max_id))
'

while parent_alive; do
    LAST=$(cat "$STATE_FILE" 2>/dev/null || echo 0)
    # curl 失敗時は黙ってスキップ。relay 一時不調でもループ続行。
    RESP=$(curl -s --get \
        --data-urlencode "channel=${CHANNEL}" \
        --data-urlencode "since=${LAST}" \
        --data-urlencode "limit=200" \
        --max-time 10 \
        "${RELAY_URL}/history" 2>/dev/null)
    if [ -n "$RESP" ]; then
        printf '%s' "$RESP" \
            | python3 -c "$SSE_FORMAT_SCRIPT" "$STATE_FILE" \
            | python3 -u "${SCRIPT_DIR}/recv_filter.py" "$HANDLE"
    fi
    sleep "$INTERVAL"
done
