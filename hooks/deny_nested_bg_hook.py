"""PreToolUse hook: bg セッションからの `claude --bg` 起動 (入れ子 bg) を拒否する。

決定事項「orch になれるのは窓口セッションだけ」により、bg セッションがさらに
bg を立てる入れ子は許可しない。Bash tool_input.command が `claude --bg` の
起動パターンを含む場合にだけ `claude agents --json` を実行し、hook 入力の
session_id と一致するエントリの kind が "background" なら deny する。それ以外の
tool・パターン非一致のコマンドでは agents コマンドを引かない。

`claude agents --json` の実行失敗・タイムアウト・非ゼロ終了・JSON parse 失敗・
該当セッション未検出は、いずれも fail-open (通す) とする。窓口からの起動を
誤って止めると運用が詰まるため。
"""
import json
import pathlib
import re
import subprocess
import sys

# プラグイン経由で `${CLAUDE_PLUGIN_ROOT}` を cwd として起動されるため、
# 同居ソースを import path に通す。
_PLUGIN_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from hooks.signal_capture import try_capture_signal  # noqa: E402
from src.harness import select_harness  # noqa: E402

# `claude --bg` の起動を拾う。`cd X && claude --bg`、複数空白、フラグの順序違い
# (`claude --model x --bg`) を拾う。`;`/`&`/`|`/改行を挟むと同一コマンドとは
# みなさず非マッチにする (別コマンドの `--bg` を誤って claude 起動と結びつけない
# ため)。変数に格納したバイナリ経由の起動 (`$CLAUDE --bg`) やスクリプトファイル内に
# 隠れた起動は静的検出できない (既知の限界)。`claude --bg` という文字列を echo
# しているだけの誤検知は、bg 内でだけ deny されるため許容する。
_BG_SPAWN_PATTERN = re.compile(r"\bclaude\b[^\n;&|]*?\s--bg\b")

_AGENTS_TIMEOUT_SECONDS = 5

_DENY_REASON = (
    "This is a background (bg) session, and bg sessions cannot spawn another "
    "`claude --bg` (no nested bg sessions allowed). Subagents (Agent tool) "
    "and Workflow are fine to use from here. If you need more hands, ask the "
    "orch (interactive) session to spawn a new bg."
)


def _is_background_session(session_id: str) -> bool:
    """`claude agents --json` を実行し、session_id が kind=background か判定する。

    実行失敗・非ゼロ終了・JSON parse 失敗・リスト以外の応答・該当セッション
    未検出は、いずれも fail-open (False = deny しない) として扱う。
    """
    try:
        result = subprocess.run(
            ["claude", "agents", "--json"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_AGENTS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    try:
        agents = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False
    if not isinstance(agents, list):
        return False
    for entry in agents:
        if isinstance(entry, dict) and entry.get("sessionId") == session_id:
            return entry.get("kind") == "background"
    return False


def main() -> None:
    harness = select_harness(hook_event_name="PreToolUse")
    try:
        event = harness.read_hook_input()
        if not event:
            harness.emit_empty()
            return

        if (event.get("tool_name") or "") != "Bash":
            harness.emit_empty()
            return

        tool_input = event.get("tool_input") or {}
        command = tool_input.get("command")
        if not isinstance(command, str) or not _BG_SPAWN_PATTERN.search(command):
            harness.emit_empty()
            return

        session_id = event.get("session_id") or ""
        if not session_id or not _is_background_session(session_id):
            harness.emit_empty()
            return

        harness.emit_permission_decision("deny", _DENY_REASON)

    except Exception as e:
        # hook 自体の不具合で全 tool を止めないため、例外時は素通し + stderr 通知
        print(f"deny_nested_bg_hook.py error: {e}", file=sys.stderr)
        try_capture_signal(kind="machine_error", source="hook:deny_nested_bg", summary=str(e)[:200])
        harness.emit_empty()


if __name__ == "__main__":
    main()
