#!/usr/bin/env bash
# iTerm2 ターミナルアダプタ
# 使い方:
#   iterm2.sh spawn <cwd> <worker_cmd>   → 新タブ起動、セッションUUIDをstdoutに返す
#   iterm2.sh close <term_ref>           → セッションUUIDでタブをクローズ
set -euo pipefail

ACTION="$1"

case "$ACTION" in
  spawn)
    CWD="$2"
    WORKER_CMD="$3"

    # AppleScriptインジェクション対策: base64エンコードしてheredoc展開時の特殊文字混入を防ぐ
    CWD_B64=$(printf '%s' "$CWD" | base64 | tr -d '\n')
    CMD_B64=$(printf '%s' "$WORKER_CMD" | base64 | tr -d '\n')

    # iTerm2 にウィンドウが1つも無い場合 (起動直後など) `tell current window` が落ちる。
    # その場合は新規ウィンドウを作って起動する。フォーカス戻し (D#2447) は通常分岐のみで
    # 行う (新規ウィンドウ作成パスにはフォーカスを奪う前画面が無い)。
    SESSION_UUID=$(osascript <<APPLESCRIPT
      tell application "iTerm2"
        activate
        if (count of windows) is 0 then
          set newWindow to (create window with default profile)
          tell current session of current tab of newWindow
            set its name to "ow-worker"
            set theCwd to do shell script "echo ${CWD_B64} | base64 --decode"
            set theCmd to do shell script "echo ${CMD_B64} | base64 --decode"
            write text "cd " & quoted form of theCwd & " && " & theCmd
            return id
          end tell
        else
          tell current window
            set originalTab to current tab
            set newTab to (create tab with default profile)
            tell current session of newTab
              set its name to "ow-worker"
              set theCwd to do shell script "echo ${CWD_B64} | base64 --decode"
              set theCmd to do shell script "echo ${CMD_B64} | base64 --decode"
              write text "cd " & quoted form of theCwd & " && " & theCmd
              set newSessionId to id
            end tell
            select originalTab
            return newSessionId
          end tell
        end if
      end tell
APPLESCRIPT
    )
    echo "$SESSION_UUID"
    ;;

  close)
    TERM_REF="$2"

    # AppleScriptインジェクション対策: base64エンコードしてheredoc展開時の特殊文字混入を防ぐ
    TERM_REF_B64=$(printf '%s' "$TERM_REF" | base64 | tr -d '\n')

    osascript <<APPLESCRIPT
      tell application "iTerm2"
        set theRef to do shell script "echo ${TERM_REF_B64} | base64 --decode"
        repeat with aWindow in windows
          repeat with aTab in tabs of aWindow
            repeat with aSession in sessions of aTab
              if id of aSession is theRef then
                tell aSession to close
                return
              end if
            end repeat
          end repeat
        end repeat
      end tell
APPLESCRIPT
    ;;

  *)
    echo "Unknown action: $ACTION" >&2
    exit 1
    ;;
esac
