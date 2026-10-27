#!/bin/sh
# Codex用のhook起動ラッパー。使い方: sh codex_run.sh hooks/<name>.py
# uvが無い環境ではPythonを起動できないため、ここで検知して発話を止めずに素通りさせる。
# Codexはexit 2だけをブロック扱いにし、他の非0は失敗表示で流すので、exit 0で終える。
cd "$(dirname "$0")/.." || exit 0
PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  # 案内は発話ごとに出れば足りる。ツール呼び出しごとのhookでは黙る。
  case "$1" in
    hooks/session_start_hook.py | hooks/user_prompt_submit_hook.py)
      printf '%s\n' '{"systemMessage":"CALMのhookにはuvが必要です。https://docs.astral.sh/uv/ からインストールしてCodexを再起動してください。それまでCALMは動きません。"}'
      ;;
  esac
  exit 0
fi
CALM_HARNESS=codex exec uv run python "$@"
