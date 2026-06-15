#!/usr/bin/env bash
# tmux ターミナルアダプタ
# 使い方:
#   tmux.sh spawn <cwd> <worker_cmd>   → 新window起動、tmux pane IDをstdoutに返す
#   tmux.sh close <term_ref>           → pane IDでpaneをkill
#
# worker_cmdはshlex.quote等でエスケープ済みのシェルコマンド文字列を期待する。
# bash -c に直接渡すため、呼び出し元でのエスケープが必要。
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: tmux.sh spawn <cwd> <worker_cmd> | close <term_ref>" >&2
  exit 1
fi

ACTION="$1"
SESSION_NAME="ow-workers"

case "$ACTION" in
  spawn)
    if [[ $# -lt 3 ]]; then
      echo "Usage: tmux.sh spawn <cwd> <worker_cmd>" >&2
      exit 1
    fi
    CWD="$2"
    WORKER_CMD="$3"

    # 既存セッションがなければ新規作成 (detached)。window 0 は基準窓として残す
    if ! tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
      tmux new-session -d -s "$SESSION_NAME" -n "ow-base"
    fi

    # 新規windowを作成してworkerを起動、pane IDを返す
    PANE_ID=$(tmux new-window -t "$SESSION_NAME" -P -F "#{pane_id}" -c "$CWD" -- \
      bash -c "$WORKER_CMD")

    echo "$PANE_ID"
    ;;

  close)
    if [[ $# -lt 2 ]]; then
      echo "Usage: tmux.sh close <term_ref>" >&2
      exit 1
    fi
    TERM_REF="$2"
    # pane IDでpaneをkill (存在しない場合はエラーを無視)
    tmux kill-pane -t "$TERM_REF" 2>/dev/null || true
    ;;

  *)
    echo "Unknown action: $ACTION" >&2
    exit 1
    ;;
esac
