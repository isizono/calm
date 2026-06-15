#!/usr/bin/env bash
# tmux ターミナルアダプタ
# 使い方:
#   tmux.sh spawn <cwd> <worker_cmd>   → 新window起動、tmux pane IDをstdoutに返す
#   tmux.sh close <term_ref>           → pane IDでpaneをkill
#
# worker_cmdはshlex.quote等でエスケープ済みのシェルコマンド文字列を期待する。
# base64エンコード経由でコマンドを渡すため、特殊文字のインジェクション対策済み。
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: tmux.sh spawn <cwd> <worker_cmd> | close <term_ref>" >&2
  exit 1
fi

ACTION="$1"
SESSION_NAME="${OW_TMUX_SESSION:-ow-workers}"

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

    # シェルインジェクション対策: base64エンコードしてCWDとCMDを安全に渡す
    CWD_B64=$(printf '%s' "$CWD" | base64 | tr -d '\n')
    CMD_B64=$(printf '%s' "$WORKER_CMD" | base64 | tr -d '\n')

    # 新規windowを作成してworkerを起動、pane IDを返す
    PANE_ID=$(tmux new-window -t "$SESSION_NAME" -n "ow-worker" -P -F "#{pane_id}" -- \
      bash -c "cd \$(echo $CWD_B64 | base64 -d) && \$(echo $CMD_B64 | base64 -d)")

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
