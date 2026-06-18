"""hooks/pretooluse_ow_spawn_worker.py のユニットテスト

PreToolUse hook が ow_spawn_worker 呼び出しに tmux_target_pane を自動 inject する
挙動を、tmux サブプロセスを mock した上でケース別に検証する。
"""
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from hooks import pretooluse_ow_spawn_worker as hook


def _run_hook(stdin_data: dict, env: dict, monkeypatch, capsys) -> dict:
    """hook を 1 回実行し、stdout を JSON parse して返す。

    env は os.environ に対する monkeypatch 上書き値。stdin は JSON エンコードして
    sys.stdin に差し込む。
    """
    monkeypatch.setattr(sys, "stdin", _make_stdin(stdin_data))
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    hook.main()
    captured = capsys.readouterr()
    return json.loads(captured.out)


def _make_stdin(payload: dict):
    import io
    return io.StringIO(json.dumps(payload))


class TestNoOp:
    """no-op になるべき条件 (空 JSON 出力)"""

    def test_ow_terminal_not_tmux(self, monkeypatch, capsys):
        result = _run_hook(
            {"tool_input": {"alias": "w-a"}},
            {"OW_TERMINAL": "iterm2", "TMUX_PANE": "%81"},
            monkeypatch, capsys,
        )
        assert result == {}

    def test_ow_terminal_unset(self, monkeypatch, capsys):
        result = _run_hook(
            {"tool_input": {"alias": "w-a"}},
            {"OW_TERMINAL": None, "TMUX_PANE": "%81"},
            monkeypatch, capsys,
        )
        assert result == {}

    def test_tmux_pane_unset(self, monkeypatch, capsys):
        result = _run_hook(
            {"tool_input": {"alias": "w-a"}},
            {"OW_TERMINAL": "tmux", "TMUX_PANE": None},
            monkeypatch, capsys,
        )
        assert result == {}

    def test_tmux_target_pane_already_specified(self, monkeypatch, capsys):
        """tool_input.tmux_target_pane が既に指定されていれば上書きしない"""
        result = _run_hook(
            {"tool_input": {"alias": "w-a", "tmux_target_pane": "%99"}},
            {"OW_TERMINAL": "tmux", "TMUX_PANE": "%81"},
            monkeypatch, capsys,
        )
        assert result == {}

    def test_empty_stdin(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "stdin", _make_stdin_raw(""))
        monkeypatch.setenv("OW_TERMINAL", "tmux")
        monkeypatch.setenv("TMUX_PANE", "%81")
        hook.main()
        captured = capsys.readouterr()
        assert json.loads(captured.out) == {}


def _make_stdin_raw(raw: str):
    import io
    return io.StringIO(raw)


class TestInject:
    """正常 inject 条件"""

    def test_inject_pane_id_normalized(self, monkeypatch, capsys):
        """tmux display-message で正規化された pane_id が注入される"""
        def fake_check_output(args, text=False, timeout=None):
            assert args == ["tmux", "display-message", "-p", "-t", "%81", "#{pane_id}"]
            return "%81\n"

        with patch.object(subprocess, "check_output", side_effect=fake_check_output):
            result = _run_hook(
                {"tool_input": {"alias": "w-a", "channel": "C1"}},
                {"OW_TERMINAL": "tmux", "TMUX_PANE": "%81"},
                monkeypatch, capsys,
            )

        spec = result["hookSpecificOutput"]
        assert spec["hookEventName"] == "PreToolUse"
        assert spec["permissionDecision"] == "allow"
        assert spec["updatedInput"] == {
            "alias": "w-a",
            "channel": "C1",
            "tmux_target_pane": "%81",
        }
        assert "auto-injected" in spec["additionalContext"]
        assert "%81" in spec["additionalContext"]

    def test_inject_with_normalization_diff(self, monkeypatch, capsys):
        """env の値 (例: statusbar 文字列) と実 pane_id が異なる場合、正規化結果を注入する"""
        def fake_check_output(args, text=False, timeout=None):
            return "%81\n"

        with patch.object(subprocess, "check_output", side_effect=fake_check_output):
            result = _run_hook(
                {"tool_input": {"alias": "w-a"}},
                {"OW_TERMINAL": "tmux", "TMUX_PANE": "0:2.1.181"},
                monkeypatch, capsys,
            )

        assert result["hookSpecificOutput"]["updatedInput"]["tmux_target_pane"] == "%81"

    def test_inject_preserves_other_args(self, monkeypatch, capsys):
        """tool_input の他フィールドは保持される"""
        def fake_check_output(args, text=False, timeout=None):
            return "%88\n"

        with patch.object(subprocess, "check_output", side_effect=fake_check_output):
            result = _run_hook(
                {
                    "tool_input": {
                        "alias": "w-b",
                        "channel": "C2",
                        "cwd": "/tmp",
                        "model": "claude-opus-4-7",
                        "effort": "high",
                    }
                },
                {"OW_TERMINAL": "tmux", "TMUX_PANE": "%88"},
                monkeypatch, capsys,
            )

        updated = result["hookSpecificOutput"]["updatedInput"]
        assert updated == {
            "alias": "w-b",
            "channel": "C2",
            "cwd": "/tmp",
            "model": "claude-opus-4-7",
            "effort": "high",
            "tmux_target_pane": "%88",
        }


class TestFallback:
    """tmux display-message 失敗時のフォールバック"""

    def test_subprocess_error_falls_back_to_env(self, monkeypatch, capsys):
        """tmux display-message 失敗時は TMUX_PANE 値をそのまま使う"""
        def fake_check_output(args, text=False, timeout=None):
            raise subprocess.CalledProcessError(1, args, "")

        with patch.object(subprocess, "check_output", side_effect=fake_check_output):
            result = _run_hook(
                {"tool_input": {"alias": "w-a"}},
                {"OW_TERMINAL": "tmux", "TMUX_PANE": "%81"},
                monkeypatch, capsys,
            )

        assert result["hookSpecificOutput"]["updatedInput"]["tmux_target_pane"] == "%81"

    def test_file_not_found_falls_back_to_env(self, monkeypatch, capsys):
        """tmux コマンド不在時 (FileNotFoundError) も TMUX_PANE 値で代用する"""
        def fake_check_output(args, text=False, timeout=None):
            raise FileNotFoundError("tmux not found")

        with patch.object(subprocess, "check_output", side_effect=fake_check_output):
            result = _run_hook(
                {"tool_input": {"alias": "w-a"}},
                {"OW_TERMINAL": "tmux", "TMUX_PANE": "%81"},
                monkeypatch, capsys,
            )

        assert result["hookSpecificOutput"]["updatedInput"]["tmux_target_pane"] == "%81"

    def test_timeout_falls_back_to_env(self, monkeypatch, capsys):
        def fake_check_output(args, text=False, timeout=None):
            raise subprocess.TimeoutExpired(args, timeout)

        with patch.object(subprocess, "check_output", side_effect=fake_check_output):
            result = _run_hook(
                {"tool_input": {"alias": "w-a"}},
                {"OW_TERMINAL": "tmux", "TMUX_PANE": "%81"},
                monkeypatch, capsys,
            )

        assert result["hookSpecificOutput"]["updatedInput"]["tmux_target_pane"] == "%81"


class TestFailOpen:
    """例外時はフェイルオープン (空 JSON + stderr ログ)"""

    def test_invalid_json_stdin(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "stdin", _make_stdin_raw("not valid json"))
        monkeypatch.setenv("OW_TERMINAL", "tmux")
        monkeypatch.setenv("TMUX_PANE", "%81")
        hook.main()
        captured = capsys.readouterr()
        assert json.loads(captured.out) == {}
        assert "error" in captured.err
