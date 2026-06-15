#!/usr/bin/env bash
# tmux ターミナルアダプタ
# 使い方:
#   tmux.sh spawn <cwd> <worker_cmd> [target_pane]
#     target_pane 未指定: 従来通り ow-workers セッションに新windowで起動
#     target_pane 指定:   target_paneと同じwindow内で split-window
#                         - window内に pane-title=ow-worker のpaneが0個 → 右に30%水平分割
#                         - 1個以上 → 最新worker paneを垂直分割
#   tmux.sh close <term_ref>           → pane IDでpaneをkill
#
# worker_cmdはshlex.quote等でエスケープ済みのシェルコマンド文字列を期待する。
# base64エンコード経由でコマンドを渡すため、特殊文字のインジェクション対策済み。
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: tmux.sh spawn <cwd> <worker_cmd> [target_pane] | close <term_ref>" >&2
  exit 1
fi

ACTION="$1"
SESSION_NAME="${OW_TMUX_SESSION:-ow-workers}"
WORKER_TITLE="ow-worker"

case "$ACTION" in
  spawn)
    if [[ $# -lt 3 ]]; then
      echo "Usage: tmux.sh spawn <cwd> <worker_cmd> [target_pane]" >&2
      exit 1
    fi
    CWD="$2"
    WORKER_CMD="$3"
    TARGET_PANE="${4:-}"

    # シェルインジェクション対策: base64エンコードしてCWDとCMDを安全に渡す
    CWD_B64=$(printf '%s' "$CWD" | base64 | tr -d '\n')
    CMD_B64=$(printf '%s' "$WORKER_CMD" | base64 | tr -d '\n')
    SHELL_CMD="cd \$(echo $CWD_B64 | base64 -d) && \$(echo $CMD_B64 | base64 -d)"

    if [[ -n "$TARGET_PANE" ]]; then
      # split-window方式: target_paneと同じwindowに分割して入れる
      WINDOW_ID=$(tmux display -t "$TARGET_PANE" -p "#{window_id}" 2>/dev/null || true)
      if [[ -z "$WINDOW_ID" ]]; then
        echo "target_pane not found: $TARGET_PANE" >&2
        exit 1
      fi

      # window内の既存worker pane (pane-title=ow-worker) を pane_id 昇順で取得し、末尾を「最新」とする
      EXISTING_WORKER=$(tmux list-panes -t "$WINDOW_ID" -F "#{pane_id}|#{pane_title}" 2>/dev/null \
        | awk -F'|' -v t="$WORKER_TITLE" '$2 == t { print $1 }' \
        | sort -t'%' -k2 -n \
        | tail -1)

      if [[ -z "$EXISTING_WORKER" ]]; then
        # 最初: target_paneの右に30%水平分割（-d でフォーカスを呼び出し元paneに残す）
        PANE_ID=$(tmux split-window -h -d -t "$TARGET_PANE" -l "30%" -P -F "#{pane_id}" -- \
          bash -c "$SHELL_CMD")
      else
        # 2個目以降: 最新worker paneを垂直分割（下に新workerが入る、-d でフォーカス維持）
        PANE_ID=$(tmux split-window -v -d -t "$EXISTING_WORKER" -P -F "#{pane_id}" -- \
          bash -c "$SHELL_CMD")
      fi

      # pane-titleで識別用マーカーを設定（pane-border-status未有効でも内部値は保持される）
      # -T はtmux 2.0+のみ対応。未対応環境では pane-title が空になり次回spawnで「最初」扱いに
      # なり続けるため、stderr に警告を出して診断可能にする（subprocess.runのcapture_outputで拾える）。
      tmux select-pane -t "$PANE_ID" -T "$WORKER_TITLE" 2>/dev/null \
        || echo "warn: tmux select-pane -T unsupported (requires tmux 2.0+), pane-title not set" >&2
    else
      # フォールバック: 従来の ow-workers 別session方式
      if ! tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
        tmux new-session -d -s "$SESSION_NAME" -n "ow-base"
      fi

      PANE_ID=$(tmux new-window -t "$SESSION_NAME" -n "ow-worker" -P -F "#{pane_id}" -- \
        bash -c "$SHELL_CMD")
    fi

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
