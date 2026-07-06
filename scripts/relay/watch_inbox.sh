#!/usr/bin/env bash
# セッションの relay inbox を tail -F で追う軽量 watcher。
#
# Usage:
#   scripts/relay/watch_inbox.sh <session_id>
#
# Monitor ツールなどの「1 行ずつ即時に読み進めたい」用途に、business logic なし
# の tail -F だけを提供する。inbox のパスは RELAY_STATE_DIR（未設定なら
# ~/.cc-memory/relay）配下の subscriptions/ / inbox/ を使うため、server 本体が
# 使う env と一致していれば追加設定は不要。
set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <session_id>" >&2
    exit 2
fi

session_id="$1"
state_dir="${RELAY_STATE_DIR:-$HOME/.cc-memory/relay}"
# session_id をファイル名に安全な形へ正規化する（src.services.relay.declarations
# の _safe_session_id と対応させる）。
safe_session_id=$(printf '%s' "$session_id" | LC_ALL=C sed 's/[^A-Za-z0-9._-]/_/g')
inbox_path="${state_dir}/inbox/session-${safe_session_id}.jsonl"

mkdir -p "$(dirname "$inbox_path")"
: >>"$inbox_path"  # 不在時に tail が即終了しないよう空 file を作る

exec tail -n 0 -F "$inbox_path"
