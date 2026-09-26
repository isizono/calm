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
import shlex
import subprocess
import sys

# プラグイン経由で `${CLAUDE_PLUGIN_ROOT}` を cwd として起動されるため、
# 同居ソースを import path に通す。
_PLUGIN_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from hooks.signal_capture import try_capture_signal  # noqa: E402
from src.harness import select_harness  # noqa: E402

# `claude --bg` の実起動を、コマンド先頭語としての `claude` の位置でのみ検出する。
# 対象になるのはコマンド先頭、または `;`/`&&`/`||`/`|`/`&`/`(`/改行の直後に来る
# `claude`。変数代入 (`FOO=bar`) や `exec`/`command`/`nohup` の前置きは読み飛ばし、
# `/usr/local/bin/claude` のようなパス指定は basename で判定する。`bash -c '...'`
# / `sh -c "..."` / `zsh -c ...` はその引数を同じ判定に再帰的にかける。
# shlex (posix クォート解釈) でトークン化するため、`grep "claude --bg" file` の
# ような文字列としての参照はマッチしない。変数に格納したバイナリ経由の起動
# (`$CLAUDE --bg`) やスクリプトファイル内に隠れた起動、1行内でクォートが閉じず
# トークン化に失敗するコマンドは静的検出できない (いずれも既知の限界、fail-open)。
_SEGMENT_BOUNDARY_TOKENS = frozenset({";", "&&", "||", "|", "&", "("})
_LEADING_SKIP_WORDS = frozenset({"exec", "command", "nohup"})
_SHELL_INTERPRETERS = frozenset({"bash", "sh", "zsh"})
_VAR_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _basename(token: str) -> str:
    return token.rsplit("/", 1)[-1]


def _segment_spawns_bg(tokens: list[str]) -> bool:
    """`;`/`&&`等で区切られた1コマンド分のトークン列が `claude --bg` 起動かどうか。"""
    idx = 0
    while idx < len(tokens) and (
        tokens[idx] in _LEADING_SKIP_WORDS or _VAR_ASSIGNMENT_RE.match(tokens[idx])
    ):
        idx += 1
    if idx >= len(tokens):
        return False
    leading = _basename(tokens[idx])
    rest = tokens[idx + 1 :]
    if leading == "claude":
        return any(tok == "--bg" or tok.startswith("--bg=") for tok in rest)
    if leading in _SHELL_INTERPRETERS and "-c" in rest:
        c_idx = rest.index("-c")
        if c_idx + 1 < len(rest):
            return _command_spawns_bg(rest[c_idx + 1])
    return False


def _line_spawns_bg(line: str) -> bool:
    """改行を含まない1行分のコマンドを shlex でトークン化し、セグメントごとに判定する。"""
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""  # `#` を含む引数 (URL fragment 等) を欠落させないため
    segment: list[str] = []
    for token in lexer:
        if token in _SEGMENT_BOUNDARY_TOKENS:
            if _segment_spawns_bg(segment):
                return True
            segment = []
        else:
            segment.append(token)
    return _segment_spawns_bg(segment)


def _command_spawns_bg(command: str) -> bool:
    """`claude --bg` の実起動を検出する。改行は常にコマンド境界として扱う。"""
    for line in command.split("\n"):
        try:
            if _line_spawns_bg(line):
                return True
        except ValueError:
            # クォートが閉じない等でトークン化できない行は fail-open (非該当) とする
            continue
    return False


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
        if not isinstance(command, str) or not _command_spawns_bg(command):
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
