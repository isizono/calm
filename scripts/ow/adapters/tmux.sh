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

# worker pane (@ow-worker=1) を window 内で縦方向に均等再分配する (D#2830)。
# 同一カラム内の縦分割を前提に、pane_id 昇順 (= 上から下) で最後を除く各 worker
# pane の高さを target = (window_height - (count-1)) / count に揃える。最後の pane は
# 残り高さを自動で吸収する。window_height は N pane 縦積みで sum(pane_height) + (N-1)
# セパレータ行と等しいため、セパレータを引いた値を分母 count で割る必要がある (引かないと
# 最後の pane が geometric に小さくなる)。
# orch pane (水平分割側) の幅には触らない。worker 数 0/1 や window 高さ取得失敗時は no-op。
rebalance_worker_panes() {
  local window_id="$1"
  [[ -z "$window_id" ]] && return 0

  local panes
  panes=$(tmux list-panes -t "$window_id" -F "#{pane_id}|#{@ow-worker}" 2>/dev/null \
    | awk -F'|' '$2 == "1" { print $1 }' \
    | sort -t'%' -k2 -n)

  local count
  count=$(printf '%s\n' "$panes" | awk '/^%/ {n++} END {print n+0}')
  [[ "$count" -lt 2 ]] && return 0

  local win_h
  win_h=$(tmux display -t "$window_id" -p "#{window_height}" 2>/dev/null || true)
  [[ -z "$win_h" || "$win_h" -le 0 ]] && return 0

  local target=$(( (win_h - (count - 1)) / count ))
  [[ "$target" -le 0 ]] && return 0

  local i=0
  local last_idx=$(( count - 1 ))
  while IFS= read -r pane; do
    [[ -z "$pane" ]] && continue
    if [[ "$i" -lt "$last_idx" ]]; then
      tmux resize-pane -t "$pane" -y "$target" 2>/dev/null || true
    fi
    i=$((i + 1))
  done <<< "$panes"
}

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

      rebalance_worker_panes "$WINDOW_ID"
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
    # 注: pane が既に不在の場合 (worker が想定外に死亡 or 自己終了済み) は #{window_id}
    # を取得できないため、残存 worker pane の縦再分配は実行できない。再分配が必要なら
    # 呼び出し元が spawn 時に控えた window_id を close へ引き渡す必要がある。
    if ! tmux display -t "$TERM_REF" -p "#{pane_pid}" 2>/dev/null >/dev/null; then
      echo "closed"
      exit 0
    fi

    # 2. pane 内 claude の PID と所属 window_id を取得 (SIGKILL fallback / 再分配用に先に押さえる)。
    PANE_PID="$(tmux display -t "$TERM_REF" -p "#{pane_pid}" 2>/dev/null || true)"
    CLOSE_WINDOW_ID="$(tmux display -t "$TERM_REF" -p "#{window_id}" 2>/dev/null || true)"

    # 3. tmux kill-pane で SIGHUP 経由の正常 close を試みる。
    tmux kill-pane -t "$TERM_REF" 2>/dev/null || true

    # 4. pane 不在になるまで短時間リトライ (default: 0.5s × 6 = 最大 3s)。
    i=0
    while [[ $i -lt $FALLBACK_ITER ]]; do
      if ! tmux display -t "$TERM_REF" -p "#{pane_pid}" 2>/dev/null >/dev/null; then
        rebalance_worker_panes "$CLOSE_WINDOW_ID"
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
        rebalance_worker_panes "$CLOSE_WINDOW_ID"
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
