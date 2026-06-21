"""PreToolUse hook: ow_spawn_worker 呼び出しに tmux_target_pane を自動 inject する。

クライアント側 (Claude Code) は TMUX_PANE 環境変数を持っているが、常駐 MCP server
(`launchd com.isizono.cc-memory-remote` 等) は env がフリーズしているためサーバー側
で参照できない (tag note ow / SKILL.md L373 の既知制約)。orch が毎 spawn で
手動指定するのは LLM コンテキスト揮発により失念しがちで、worker が
`ow-workers` 別 session に隔離され不可視化する事故が頻発する (本 hook 起票理由)。

本 hook は PreToolUse の updatedInput 機能で tool_input を上書きし、
`OW_TERMINAL=tmux` 環境かつ TMUX_PANE が存在する場合のみ、`tmux display-message`
で正規化した pane_id を `tmux_target_pane` に注入する。tmux 以外の環境や
既に明示指定されている呼び出しでは no-op。

参考: https://code.claude.com/docs/en/hooks (PreToolUse hookSpecificOutput.updatedInput)
"""
import json
import os
import subprocess
import sys


def _resolve_pane_id(tmux_pane: str) -> str:
    """TMUX_PANE 環境変数の値から実 pane_id を取得する。

    tmux statusbar の表示文字列 (例: `0:2.1.181`) や古い値が紛れていても、
    `tmux display-message` で現在の正規 pane_id (`%<n>`) に正規化する。
    """
    try:
        return subprocess.check_output(
            ["tmux", "display-message", "-p", "-t", tmux_pane, "#{pane_id}"],
            text=True,
            timeout=2,
        ).strip()
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return tmux_pane


def main() -> None:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            print("{}")
            return

        event = json.loads(raw)
        tool_input = event.get("tool_input") or {}

        if "tmux_target_pane" in tool_input:
            print("{}")
            return

        # OW_TERMINAL 未設定時のデフォルトは ow_service.py 側で "tmux"。
        # hook も同じデフォルトに揃え、未設定ユーザーでも tmux_target_pane が
        # 自動注入されるようにする (注入失敗時 worker が ow-workers 別 session に
        # 隔離される事故を防ぐ目的)。
        if os.environ.get("OW_TERMINAL", "tmux") != "tmux":
            print("{}")
            return

        tmux_pane = os.environ.get("TMUX_PANE")
        if not tmux_pane:
            print("{}")
            return

        pane_id = _resolve_pane_id(tmux_pane)
        if not pane_id:
            print("{}")
            return

        new_input = dict(tool_input)
        new_input["tmux_target_pane"] = pane_id

        output = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": new_input,
                "additionalContext": f"auto-injected tmux_target_pane={pane_id}",
            }
        }
        print(json.dumps(output, ensure_ascii=False))

    except Exception as e:
        print(f"pretooluse_ow_spawn_worker.py error: {e}", file=sys.stderr)
        print("{}")


if __name__ == "__main__":
    main()
