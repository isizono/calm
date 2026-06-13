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

    SESSION_UUID=$(osascript <<APPLESCRIPT
      tell application "iTerm2"
        tell current window
          set originalTab to current tab
          set newTab to (create tab with default profile)
          tell current session of newTab
            set its name to "ow-worker"
            write text "cd ${CWD} && ${WORKER_CMD}"
            set newSessionId to id
          end tell
          select originalTab
          return newSessionId
        end tell
      end tell
APPLESCRIPT
    )
    echo "$SESSION_UUID"
    ;;

  close)
    TERM_REF="$2"

    osascript <<APPLESCRIPT
      tell application "iTerm2"
        repeat with aWindow in windows
          repeat with aTab in tabs of aWindow
            repeat with aSession in sessions of aTab
              if id of aSession is "${TERM_REF}" then
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
