#!/usr/bin/env bash
# get_term_ref.sh — worker 自身の term_ref を取得して stdout に echo する。
#
# 用途: worker が event:identity を append する際、自身の安定 ID (term_ref)
#       を identity bundle の term_ref フィールドに乗せるために使う。
#       term_ref の値は ow_spawn_worker が adapter から取得する spawn 戻り値の
#       term_ref と同一形式で揃える (tmux:pane_id、iterm2:session UUID)。
#
# 使い方:
#   TERM_REF=$(bash scripts/ow/get_term_ref.sh)
#
# 環境変数:
#   OW_TERMINAL  tmux | iterm2 | manual (default: tmux)
#
# 出力:
#   stdout に term_ref を1行 echo する。取得不能時は空文字を返す (exit 0)。

set -euo pipefail

ADAPTER="${OW_TERMINAL:-tmux}"

case "$ADAPTER" in
  tmux)
    # TMUX_PANE は tmux 配下のシェルで自動 export される pane_id (例: %5)
    printf '%s\n' "${TMUX_PANE:-}"
    ;;
  iterm2)
    # current session の id (UUID) を取得。iTerm2 が起動していない / セッション無しなら空
    /usr/bin/osascript -e 'tell application "iTerm2" to return id of current session of current window' 2>/dev/null \
      || printf '\n'
    ;;
  manual)
    # manual モードでは安定 ID を提供できないため空を返す。
    # ($$ はこのスクリプト実行ごとに別 bash プロセスとなるため、worker terminal
    #  peer の逆引き ID として安定しない。呼び出し側で term_ref フィールドを省略させる。)
    printf '\n'
    ;;
  *)
    printf '\n'
    ;;
esac
