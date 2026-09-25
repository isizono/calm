"""deny_nested_bg_hook.py の単体テスト。

PreToolUse hook の入出力 (stdin から event JSON 受領、stdout に
permissionDecision JSON 出力 or 空 dict 出力) と、各補助関数の挙動を検証する。

検証対象:
- _command_spawns_bg: `claude --bg` 起動パターンの検出 (揺れの許容・非対象コマンドの除外・
  文字列としての参照との区別)
- _is_background_session: `claude agents --json` の実行結果からの kind 判定、fail-open 経路
- main: stdin event → deny / allow 判定の総合フロー、agents コマンドを引くか否か
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

_HOOKS_DIR = Path(__file__).resolve().parents[2] / "hooks"
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

import deny_nested_bg_hook  # type: ignore  # noqa: E402


# ---------------------------------------------------------------------------
# _command_spawns_bg
# ---------------------------------------------------------------------------


class TestCommandSpawnsBg:
    @pytest.mark.parametrize(
        "command",
        [
            'claude --bg "prompt" --permission-mode auto --model claude-sonnet-5',
            'cd /path && claude --bg "x"',
            "claude  --bg",  # 複数空白
            "claude --model foo --bg",  # フラグ順序違い
            "FOO=1 claude --bg",  # 環境変数の前置き
            "exec claude --bg",  # exec 前置き
        ],
    )
    def test_matches(self, command):
        assert deny_nested_bg_hook._command_spawns_bg(command)

    @pytest.mark.parametrize(
        "command",
        [
            "claude agents --json",
            "claude respawn",
            "claude stop",
            "claude logs",
            "claude attach",
            "claude --background-task foo",  # --bg の word boundary 非一致
            "echo hello",
            "",
            "echo claude --bg",  # claude がコマンド先頭語ではない
            'grep -n "claude --bg" skills/board/SKILL.md',  # クォート内の文字列参照
            'echo "claude --bg"',
            "cat file | grep 'claude --bg'",
        ],
    )
    def test_does_not_match(self, command):
        assert not deny_nested_bg_hook._command_spawns_bg(command)

    def test_separate_commands_via_semicolon_not_matched(self):
        # `claude foo; other --bg` は別コマンドの --bg であり claude 起動とは
        # 結び付けない (`;`/`&`/`|`/改行を挟むと非マッチにする設計)
        assert not deny_nested_bg_hook._command_spawns_bg(
            "claude foo; other --bg"
        )


# ---------------------------------------------------------------------------
# _is_background_session
# ---------------------------------------------------------------------------


AGENTS_JSON = json.dumps(
    [
        {"sessionId": "bg-session-1", "kind": "background"},
        {"sessionId": "interactive-session-1", "kind": "interactive"},
    ]
)


class _FakeCompletedProcess:
    def __init__(self, stdout: str, returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode


class TestIsBackgroundSession:
    def test_background_session_returns_true(self, monkeypatch):
        monkeypatch.setattr(
            deny_nested_bg_hook.subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(AGENTS_JSON),
        )
        assert deny_nested_bg_hook._is_background_session("bg-session-1") is True

    def test_interactive_session_returns_false(self, monkeypatch):
        monkeypatch.setattr(
            deny_nested_bg_hook.subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(AGENTS_JSON),
        )
        assert (
            deny_nested_bg_hook._is_background_session("interactive-session-1")
            is False
        )

    def test_session_not_found_returns_false(self, monkeypatch):
        monkeypatch.setattr(
            deny_nested_bg_hook.subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(AGENTS_JSON),
        )
        assert deny_nested_bg_hook._is_background_session("unknown-session") is False

    def test_command_not_found_fails_open(self, monkeypatch):
        def _raise(*a, **k):
            raise FileNotFoundError("claude not found")

        monkeypatch.setattr(deny_nested_bg_hook.subprocess, "run", _raise)
        assert deny_nested_bg_hook._is_background_session("bg-session-1") is False

    def test_timeout_fails_open(self, monkeypatch):
        def _raise(*a, **k):
            raise subprocess.TimeoutExpired(cmd="claude", timeout=5)

        monkeypatch.setattr(deny_nested_bg_hook.subprocess, "run", _raise)
        assert deny_nested_bg_hook._is_background_session("bg-session-1") is False

    def test_nonzero_exit_fails_open(self, monkeypatch):
        monkeypatch.setattr(
            deny_nested_bg_hook.subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess("", returncode=1),
        )
        assert deny_nested_bg_hook._is_background_session("bg-session-1") is False

    def test_invalid_json_fails_open(self, monkeypatch):
        monkeypatch.setattr(
            deny_nested_bg_hook.subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess("not json"),
        )
        assert deny_nested_bg_hook._is_background_session("bg-session-1") is False

    def test_non_list_json_fails_open(self, monkeypatch):
        monkeypatch.setattr(
            deny_nested_bg_hook.subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(json.dumps({"not": "a list"})),
        )
        assert deny_nested_bg_hook._is_background_session("bg-session-1") is False


# ---------------------------------------------------------------------------
# main フロー
# ---------------------------------------------------------------------------


def _run_main_with_event(event: dict, capsys) -> dict:
    """stdin に event を流して main() を呼び、stdout 出力を dict として返す。"""
    sys.stdin = io.StringIO(json.dumps(event))
    try:
        deny_nested_bg_hook.main()
    finally:
        sys.stdin = sys.__stdin__
    captured = capsys.readouterr()
    text = captured.out.strip()
    if not text or text == "{}":
        return {}
    return json.loads(text)


@pytest.fixture
def agents_spy(monkeypatch):
    """subprocess.run 呼び出しを記録しつつ AGENTS_JSON を返す spy。"""
    calls: list[tuple] = []

    def _spy(*args, **kwargs):
        calls.append((args, kwargs))
        return _FakeCompletedProcess(AGENTS_JSON)

    monkeypatch.setattr(deny_nested_bg_hook.subprocess, "run", _spy)
    return calls


class TestMainFlow:
    def test_empty_stdin_passes_through(self, capsys):
        sys.stdin = io.StringIO("")
        try:
            deny_nested_bg_hook.main()
        finally:
            sys.stdin = sys.__stdin__
        assert capsys.readouterr().out.strip() == "{}"

    def test_non_bash_tool_does_not_call_agents(self, capsys, agents_spy):
        out = _run_main_with_event(
            {
                "tool_name": "Write",
                "tool_input": {"command": "claude --bg foo"},
                "session_id": "bg-session-1",
            },
            capsys,
        )
        assert out == {}
        assert agents_spy == []

    def test_bash_without_bg_spawn_does_not_call_agents(self, capsys, agents_spy):
        out = _run_main_with_event(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "echo hello"},
                "session_id": "bg-session-1",
            },
            capsys,
        )
        assert out == {}
        assert agents_spy == []

    def test_bash_bg_spawn_from_background_session_denies(self, capsys, agents_spy):
        out = _run_main_with_event(
            {
                "tool_name": "Bash",
                "tool_input": {"command": 'claude --bg "task"'},
                "session_id": "bg-session-1",
            },
            capsys,
        )
        spec = out["hookSpecificOutput"]
        assert spec["hookEventName"] == "PreToolUse"
        assert spec["permissionDecision"] == "deny"
        assert "claude --bg" in spec["permissionDecisionReason"]
        assert "Agent" in spec["permissionDecisionReason"]
        assert len(agents_spy) == 1

    def test_bash_bg_spawn_from_interactive_session_allows(self, capsys, agents_spy):
        out = _run_main_with_event(
            {
                "tool_name": "Bash",
                "tool_input": {"command": 'claude --bg "task"'},
                "session_id": "interactive-session-1",
            },
            capsys,
        )
        assert out == {}
        assert len(agents_spy) == 1

    def test_agents_command_failure_allows(self, capsys, monkeypatch):
        def _raise(*a, **k):
            raise FileNotFoundError("claude not found")

        monkeypatch.setattr(deny_nested_bg_hook.subprocess, "run", _raise)
        out = _run_main_with_event(
            {
                "tool_name": "Bash",
                "tool_input": {"command": 'claude --bg "task"'},
                "session_id": "bg-session-1",
            },
            capsys,
        )
        assert out == {}
