"""Claude Code CLI session file 読み取り（src.infra.cli_session）のユニットテスト。

`~/.claude/sessions/<pid>.json` は CLI 内部形式であり公開契約ではないため、
全読み取りが型チェック付きの `.get()` で失敗を吸収し None へ倒すことを検証する。
書き込みは一切行わない（本モジュールに書き込み関数自体が存在しない）。
"""
import json

import pytest

from src.infra import cli_session


@pytest.fixture
def sessions_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(cli_session.CLAUDE_SESSIONS_DIR_ENV, str(tmp_path))
    return tmp_path


def _write(sessions_dir, pid: int, payload: dict | str) -> None:
    path = sessions_dir / f"{pid}.json"
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")


class TestSessionsDir:
    def test_defaults_to_claude_sessions_dir(self, monkeypatch):
        monkeypatch.delenv(cli_session.CLAUDE_SESSIONS_DIR_ENV, raising=False)
        assert cli_session.sessions_dir().parts[-2:] == (".claude", "sessions")

    def test_env_override_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv(cli_session.CLAUDE_SESSIONS_DIR_ENV, str(tmp_path))
        assert cli_session.sessions_dir() == tmp_path


class TestReadCliSession:
    def test_reads_valid_file(self, sessions_dir, monkeypatch):
        monkeypatch.setattr(cli_session, "is_process_alive", lambda pid: True)
        _write(
            sessions_dir,
            111,
            {
                "pid": 111,
                "sessionId": "cli-uuid-1",
                "cwd": "/Users/x/workspace",
                "name": "workspace-a2",
                "status": "busy",
            },
        )
        result = cli_session.read_cli_session(111)
        assert result == {
            "cli_pid": 111,
            "name": "workspace-a2",
            "cli_session_id": "cli-uuid-1",
            "cwd": "/Users/x/workspace",
            "cli_status": "busy",
        }

    def test_strips_whitespace_from_name(self, sessions_dir, monkeypatch):
        monkeypatch.setattr(cli_session, "is_process_alive", lambda pid: True)
        _write(sessions_dir, 111, {"pid": 111, "name": "  workspace-a2  "})
        assert cli_session.read_cli_session(111)["name"] == "workspace-a2"

    def test_missing_file_returns_none(self, sessions_dir):
        assert cli_session.read_cli_session(999) is None

    def test_broken_json_returns_none(self, sessions_dir):
        _write(sessions_dir, 111, "not json")
        assert cli_session.read_cli_session(111) is None

    def test_non_dict_json_returns_none(self, sessions_dir):
        _write(sessions_dir, 111, "[1, 2, 3]")
        assert cli_session.read_cli_session(111) is None

    def test_missing_name_returns_none(self, sessions_dir, monkeypatch):
        monkeypatch.setattr(cli_session, "is_process_alive", lambda pid: True)
        _write(sessions_dir, 111, {"pid": 111})
        assert cli_session.read_cli_session(111) is None

    def test_empty_name_returns_none(self, sessions_dir, monkeypatch):
        monkeypatch.setattr(cli_session, "is_process_alive", lambda pid: True)
        _write(sessions_dir, 111, {"pid": 111, "name": "   "})
        assert cli_session.read_cli_session(111) is None

    def test_pid_mismatch_returns_none(self, sessions_dir, monkeypatch):
        """ファイル内pidとファイル名のpidが不一致なら取り違え防止でNone"""
        monkeypatch.setattr(cli_session, "is_process_alive", lambda pid: True)
        _write(sessions_dir, 111, {"pid": 222, "name": "workspace-a2"})
        assert cli_session.read_cli_session(111) is None

    def test_dead_process_returns_none(self, sessions_dir, monkeypatch):
        """PID再利用対策: プロセスが生存していなければstale扱いでNone"""
        monkeypatch.setattr(cli_session, "is_process_alive", lambda pid: False)
        _write(sessions_dir, 111, {"pid": 111, "name": "workspace-a2"})
        assert cli_session.read_cli_session(111) is None

    def test_missing_optional_fields_degrade_to_none(self, sessions_dir, monkeypatch):
        """sessionId/cwd/statusが欠落・非文字列でも例外にならずNoneに落ちる"""
        monkeypatch.setattr(cli_session, "is_process_alive", lambda pid: True)
        _write(sessions_dir, 111, {"pid": 111, "name": "workspace-a2", "sessionId": 123})
        result = cli_session.read_cli_session(111)
        assert result["cli_session_id"] is None
        assert result["cwd"] is None
        assert result["cli_status"] is None


class TestFindCliSession:
    def test_returns_first_resolvable_pid_in_order(self, sessions_dir, monkeypatch):
        monkeypatch.setattr(cli_session, "is_process_alive", lambda pid: True)
        _write(sessions_dir, 200, {"pid": 200, "name": "workspace-b2"})
        result = cli_session.find_cli_session([100, 200, 300])
        assert result["cli_pid"] == 200
        assert result["name"] == "workspace-b2"

    def test_prefers_nearest_pid_when_multiple_match(self, sessions_dir, monkeypatch):
        """入れ子claudeケース: 先頭に近い方(直近の親)を優先して返す"""
        monkeypatch.setattr(cli_session, "is_process_alive", lambda pid: True)
        _write(sessions_dir, 100, {"pid": 100, "name": "inner-session"})
        _write(sessions_dir, 300, {"pid": 300, "name": "outer-session"})
        result = cli_session.find_cli_session([100, 300])
        assert result["name"] == "inner-session"

    def test_returns_none_when_nothing_resolves(self, sessions_dir):
        assert cli_session.find_cli_session([1, 2, 3]) is None

    def test_returns_none_for_empty_pid_list(self, sessions_dir):
        assert cli_session.find_cli_session([]) is None
