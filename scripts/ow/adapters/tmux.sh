#!/usr/bin/env bash
# tmux ターミナルアダプタ
# 使い方:
#   tmux.sh spawn <cwd> <worker_cmd> [target_pane] [is_thinking]
#     target_pane 未指定 + is_thinking=0/未指定: 従来通り ow-workers セッションに新windowで起動
#     target_pane 指定 + is_thinking=0/未指定:   target_paneと同じwindow内で split-window
#                                                - window内に pane user option @ow-worker=1 のpaneが0個 → 右に30%水平分割
#                                                - 1個以上 → 最新worker paneを垂直分割
#     is_thinking=1:                              思考worker (D#2601)。target_pane と同じセッションに `tmux new-window`
#                                                で別タブ (window) を開く。target_pane 未指定なら ow-workers セッションに
#                                                新タブで起動。新window名は "ow-worker-thinking" を設定し、pane user
#                                                option @ow-worker=1 で識別マーカーを付ける。
#   tmux.sh close <term_ref>           → pane IDでpaneをkill
#
# worker_cmdはshlex.quote等でエスケープ済みのシェルコマンド文字列を期待する。
# base64エンコード経由でコマンドを渡すため、特殊文字のインジェクション対策済み。
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: tmux.sh spawn <cwd> <worker_cmd> [target_pane] [is_thinking] | close <term_ref>" >&2
  exit 1
fi

ACTION="$1"
SESSION_NAME="${OW_TMUX_SESSION:-ow-workers}"
WORKER_MARKER_OPT="@ow-worker"
THINKING_WINDOW_NAME="ow-worker-thinking"

case "$ACTION" in
  spawn)
    if [[ $# -lt 3 ]]; then
      echo "Usage: tmux.sh spawn <cwd> <worker_cmd> [target_pane] [is_thinking]" >&2
      exit 1
    fi
    CWD="$2"
    WORKER_CMD="$3"
    TARGET_PANE="${4:-}"
    IS_THINKING="${5:-0}"

    # シェルインジェクション対策: base64エンコードしてCWDとCMDを安全に渡す
    # cd側はダブルクォートでword splittingを抑止、worker_cmd側はevalでシェル構文として
    # 再パースさせる（shlex.quoteのリテラル引用符を正しく解釈させるため）。
    # evalに渡すソースはow_serviceが組み立てた信頼コマンド文字列のみ（base64で運搬）。
    #
    # 注: worker_cmd 内の `OW_PARENT_PID=$$` の `$$` は ow_service.py 側では
    # クォートされた文字列としてそのまま運搬され、ここの `eval` 実行時に
    # 「この bash プロセスの PID」へ展開される。直後の `exec claude ...` で
    # claude プロセスはこの bash の PID を継承するため、`$$` の値は claude
    # 本体の PID と一致する。これが recv.sh/heartbeat.sh の親監視 (OW_PARENT_PID)
    # の基準 PID になる。
    CWD_B64=$(printf '%s' "$CWD" | base64 | tr -d '\n')
    CMD_B64=$(printf '%s' "$WORKER_CMD" | base64 | tr -d '\n')
    SHELL_CMD="cd \"\$(echo $CWD_B64 | base64 -d)\" && eval \"\$(echo $CMD_B64 | base64 -d)\""

    if [[ "$IS_THINKING" == "1" ]]; then
      # 思考worker: 別タブ (new-window) で開く (D#2601)
      # target_pane があればそのセッションに、なければ ow-workers セッションに新windowを足す。
      # 通常worker と pane user option (@ow-worker=1) を共有 — 別window配置のため
      # list-panes 検出競合は発生しない (split-window 検出は同一window内に閉じる)。
      if [[ -n "$TARGET_PANE" ]]; then
        SESSION_ID=$(tmux display -t "$TARGET_PANE" -p "#{session_id}" 2>/dev/null || true)
        if [[ -z "$SESSION_ID" ]]; then
          echo "target_pane not found: $TARGET_PANE" >&2
          exit 1
        fi
        PANE_ID=$(tmux new-window -t "$SESSION_ID" -n "$THINKING_WINDOW_NAME" -d -P -F "#{pane_id}" -- \
          bash -c "$SHELL_CMD")
      else
        if ! tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
          tmux new-session -d -s "$SESSION_NAME" -n "ow-base"
        fi
        PANE_ID=$(tmux new-window -t "$SESSION_NAME" -n "$THINKING_WINDOW_NAME" -P -F "#{pane_id}" -- \
          bash -c "$SHELL_CMD")
      fi
      tmux set-option -p -t "$PANE_ID" "$WORKER_MARKER_OPT" 1 \
        || echo "warn: tmux set-option -p @ow-worker failed (possibly tmux <1.8 or pane gone), worker marker not set" >&2
    elif [[ -n "$TARGET_PANE" ]]; then
      # 通常worker + split-window方式: target_paneと同じwindowに分割して入れる
      WINDOW_ID=$(tmux display -t "$TARGET_PANE" -p "#{window_id}" 2>/dev/null || true)
      if [[ -z "$WINDOW_ID" ]]; then
        echo "target_pane not found: $TARGET_PANE" >&2
        exit 1
      fi

      # window内の既存worker pane (pane user option @ow-worker=1) を pane_id 昇順で取得し、末尾を「最新」とする。
      # pane-title はclaudeセッションが ANSI escape sequence (\e]2;...\a) で動的に上書きするため
      # マーカーとして使えない。pane user option (@プレフィックス) は tmux server 内部の属性で
      # クライアントから escape 経由で書き換え不可なので、安定したマーカーとして利用できる。
      EXISTING_WORKER=$(tmux list-panes -t "$WINDOW_ID" -F "#{pane_id}|#{@ow-worker}" 2>/dev/null \
        | awk -F'|' '$2 == "1" { print $1 }' \
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

      # pane user option で識別用マーカーを設定（claudeの escape sequence では上書き不可）。
      # set-option -p は pane-local オプション、@<name> カスタムオプションは tmux 1.8+ 対応。
      # マーカー未設定では次回spawnで「最初」扱いになり続けるため、失敗時は警告を出して診断可能にする
      # （非対応バージョン以外にも pane消失・tmux server断などが要因になりうるため、原因は断定しない）。
      tmux set-option -p -t "$PANE_ID" "$WORKER_MARKER_OPT" 1 \
        || echo "warn: tmux set-option -p @ow-worker failed (possibly tmux <1.8 or pane gone), worker marker not set" >&2
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
    # close 契約: stdout に "closed" / "killed" / "failed" を 1 行返す。
    # ow_close_worker (src/services/ow_service.py) はこの最終行を読んで
    # closed/killed bool を組み立てる。
    #
    # 環境変数で fallback 待機を調整可能 (テスト・運用調整用):
    #   OW_CLOSE_FALLBACK_ITER     pane 不在確認のリトライ回数 (default: 6)
    #   OW_CLOSE_FALLBACK_INTERVAL リトライ間隔の秒数 (default: 0.5)
    FALLBACK_ITER="${OW_CLOSE_FALLBACK_ITER:-6}"
    FALLBACK_INTERVAL="${OW_CLOSE_FALLBACK_INTERVAL:-0.5}"

    # 1. pane の生存確認。既に不在ならそのまま closed 扱い。
    if ! tmux display -t "$TERM_REF" -p "#{pane_pid}" 2>/dev/null >/dev/null; then
      echo "closed"
      exit 0
    fi

    # 2. pane 内 claude の PID を取得 (SIGKILL fallback 用に先に押さえる)。
    PANE_PID="$(tmux display -t "$TERM_REF" -p "#{pane_pid}" 2>/dev/null || true)"

    # 3. tmux kill-pane で SIGHUP 経由の正常 close を試みる。
    tmux kill-pane -t "$TERM_REF" 2>/dev/null || true

    # 4. pane 不在になるまで短時間リトライ (default: 0.5s × 6 = 最大 3s)。
    i=0
    while [[ $i -lt $FALLBACK_ITER ]]; do
      if ! tmux display -t "$TERM_REF" -p "#{pane_pid}" 2>/dev/null >/dev/null; then
        echo "closed"
        exit 0
      fi
      sleep "$FALLBACK_INTERVAL"
      i=$((i + 1))
    done

    # 5. SIGKILL fallback: pane 内 PID に直接 SIGKILL を送って再度 kill-pane。
    if [[ -n "$PANE_PID" ]]; then
      kill -KILL "$PANE_PID" 2>/dev/null || true
    fi
    tmux kill-pane -t "$TERM_REF" 2>/dev/null || true

    # 6. 最終確認。SIGKILL 直後に tmux 内部の pane 消滅処理が完了するまで
    # 短時間リトライ (default: 0.5s × 2 = 最大 1s)。step 4 と同パラメータ。
    j=0
    final_iter=2
    while [[ $j -lt $final_iter ]]; do
      if ! tmux display -t "$TERM_REF" -p "#{pane_pid}" 2>/dev/null >/dev/null; then
        echo "killed"
        exit 0
      fi
      sleep "$FALLBACK_INTERVAL"
      j=$((j + 1))
    done

    echo "failed" >&2
    echo "failed"
    exit 1
    ;;

  *)
    echo "Unknown action: $ACTION" >&2
    exit 1
    ;;
esac
