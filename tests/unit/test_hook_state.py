"""hooks/hook_state.py のユニットテスト"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hooks.hook_state import HookState


@pytest.fixture
def hook_state(tmp_path, monkeypatch):
    monkeypatch.setattr(HookState, "BASE_DIR", tmp_path)
    return HookState("test-session-123")


class TestBlockCount:
    def test_get_returns_zero_when_no_file(self, hook_state):
        assert hook_state.get_block_count() == 0

    def test_increment(self, hook_state):
        assert hook_state.increment_block_count() == 1
        assert hook_state.increment_block_count() == 2

    def test_reset_then_get(self, hook_state):
        hook_state.increment_block_count()
        hook_state.increment_block_count()
        hook_state.reset_block_count()
        assert hook_state.get_block_count() == 0

    def test_corrupted_file_returns_zero(self, hook_state):
        path = hook_state._path("block_count")
        path.write_text("abc")
        assert hook_state.get_block_count() == 0


class TestIdLeakCount:
    def test_get_returns_zero_when_no_file(self, hook_state):
        assert hook_state.get_id_leak_count() == 0

    def test_increment_default_by_one(self, hook_state):
        assert hook_state.increment_id_leak_count() == 1
        assert hook_state.increment_id_leak_count() == 2

    def test_increment_with_by(self, hook_state):
        assert hook_state.increment_id_leak_count(3) == 3
        assert hook_state.increment_id_leak_count(2) == 5

    def test_reset_then_get(self, hook_state):
        hook_state.increment_id_leak_count(4)
        hook_state.reset_id_leak_count()
        assert hook_state.get_id_leak_count() == 0

    def test_corrupted_file_returns_zero(self, hook_state):
        path = hook_state._path("id_leak_count")
        path.write_text("abc")
        assert hook_state.get_id_leak_count() == 0

    def test_cleared_by_clear_session(self, hook_state):
        hook_state.increment_id_leak_count(2)
        HookState.clear_session("test-session-123")
        assert hook_state.get_id_leak_count() == 0


class TestMonitorStarted:
    def test_get_returns_false_when_no_file(self, hook_state):
        assert hook_state.get_monitor_started() is False

    def test_set_then_get(self, hook_state):
        hook_state.set_monitor_started()
        assert hook_state.get_monitor_started() is True

    def test_cleared_by_clear_session(self, hook_state):
        hook_state.set_monitor_started()
        HookState.clear_session("test-session-123")
        assert hook_state.get_monitor_started() is False


class TestTranscriptOffset:
    def test_get_returns_zero_when_no_file(self, hook_state):
        assert hook_state.get_transcript_offset() == 0

    def test_set_then_get(self, hook_state):
        hook_state.set_transcript_offset(12345)
        assert hook_state.get_transcript_offset() == 12345

    def test_corrupted_file_returns_zero(self, hook_state):
        path = hook_state._path("transcript_offset")
        path.write_text("abc")
        assert hook_state.get_transcript_offset() == 0


class TestSanitizeOffset:
    def test_get_returns_zero_when_no_file(self, hook_state):
        assert hook_state.get_sanitize_offset() == 0

    def test_set_then_get(self, hook_state):
        hook_state.set_sanitize_offset(67890)
        assert hook_state.get_sanitize_offset() == 67890

    def test_corrupted_file_returns_zero(self, hook_state):
        path = hook_state._path("sanitize_offset")
        path.write_text("not-an-int")
        assert hook_state.get_sanitize_offset() == 0

    def test_independent_of_transcript_offset(self, hook_state):
        hook_state.set_transcript_offset(100)
        hook_state.set_sanitize_offset(200)
        assert hook_state.get_transcript_offset() == 100
        assert hook_state.get_sanitize_offset() == 200


class TestSanitizeFailureCount:
    def test_get_returns_zero_when_no_file(self, hook_state):
        assert hook_state.get_sanitize_failure_count() == 0

    def test_set_then_get(self, hook_state):
        hook_state.set_sanitize_failure_count(2)
        assert hook_state.get_sanitize_failure_count() == 2

    def test_corrupted_file_returns_zero(self, hook_state):
        path = hook_state._path("sanitize_failure_count")
        path.write_text("not-an-int")
        assert hook_state.get_sanitize_failure_count() == 0

    def test_cleared_by_clear_session(self, hook_state, tmp_path):
        hook_state.set_sanitize_failure_count(3)
        HookState.clear_session("test-session-123")
        assert hook_state.get_sanitize_failure_count() == 0


class TestCurrentTurn:
    def test_get_returns_zero_when_no_file(self, hook_state):
        assert hook_state.get_current_turn() == 0

    def test_set_then_get(self, hook_state):
        hook_state.set_current_turn(5)
        assert hook_state.get_current_turn() == 5

    def test_corrupted_file_returns_zero(self, hook_state):
        path = hook_state._path("current_turn")
        path.write_text("abc")
        assert hook_state.get_current_turn() == 0


class TestCheckedInActivity:
    def test_get_returns_none_when_no_file(self, hook_state):
        assert hook_state.get_checked_in_activity() is None

    def test_set_then_get(self, hook_state):
        hook_state.set_checked_in_activity(42)
        assert hook_state.get_checked_in_activity() == 42


class TestEventsJsonl:
    def test_read_returns_empty_when_no_file(self, hook_state):
        assert hook_state.read_events() == []

    def test_append_then_read(self, hook_state):
        events = [
            {"e": "tool", "name": "add_decisions", "turn": 1},
            {"e": "meta", "topic": "test-topic", "turn": 1},
        ]
        hook_state.append_events(events)
        result = hook_state.read_events()
        assert len(result) == 2
        assert result[0]["e"] == "tool"
        assert result[1]["e"] == "meta"

    def test_append_is_additive(self, hook_state):
        hook_state.append_events([{"e": "tool", "name": "search", "turn": 1}])
        hook_state.append_events([{"e": "meta", "topic": "topic-a", "turn": 2}])
        result = hook_state.read_events()
        assert len(result) == 2

    def test_empty_list_does_not_create_file(self, hook_state):
        hook_state.append_events([])
        assert not hook_state.events_path.exists()

    def test_malformed_json_line_skipped(self, hook_state):
        with open(hook_state.events_path, "w") as f:
            f.write('{"e": "tool", "name": "search", "turn": 1}\n')
            f.write("not json\n")
            f.write('{"e": "meta", "topic": "t", "turn": 2}\n')
        result = hook_state.read_events()
        assert len(result) == 2

    def test_events_path(self, hook_state):
        assert hook_state.events_path.name == "events_test-session-123.jsonl"


class TestClearSession:
    def test_clears_all_state_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(HookState, "BASE_DIR", tmp_path)
        state = HookState("sess-abc")

        # 全種類の状態ファイルを作成
        state.increment_block_count()
        state.set_transcript_offset(100)
        state.set_current_turn(3)
        state.set_checked_in_activity(42)
        state.append_events([{"e": "tool", "name": "search", "turn": 1}])

        # clear
        HookState.clear_session("sess-abc")

        # 全ファイルが消えている
        assert state.get_block_count() == 0
        assert state.get_transcript_offset() == 0
        assert state.get_current_turn() == 0
        assert state.get_checked_in_activity() is None
        assert state.read_events() == []

    def test_clears_events_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(HookState, "BASE_DIR", tmp_path)
        state = HookState("sess-events")
        state.append_events([{"e": "meta", "topic": "t", "turn": 1}])
        assert state.events_path.exists()

        HookState.clear_session("sess-events")
        assert not state.events_path.exists()


class TestSessionIdSlash:
    def test_slash_replaced_with_underscore(self, tmp_path, monkeypatch):
        """session_idに含まれる '/' がファイル名では '_' に置換される"""
        monkeypatch.setattr(HookState, "BASE_DIR", tmp_path)
        state = HookState("user/session/123")
        state.set_current_turn(5)

        # ファイル名に '/' が含まれず '_' に置換されている
        expected_file = tmp_path / "current_turn_user_session_123"
        assert expected_file.exists()
        assert expected_file.read_text().strip() == "5"


class TestMainCli:
    def test_clear_via_cli(self, tmp_path, monkeypatch):
        """CLIのclearコマンドで全状態ファイルが削除される"""
        monkeypatch.setattr(HookState, "BASE_DIR", tmp_path)

        # 状態ファイルを作成
        state = HookState("cli-test-sess")
        state.set_current_turn(3)
        state.increment_block_count()

        # ファイルが存在する
        assert state.get_current_turn() == 3
        assert state.get_block_count() == 1

        # CLIで clear を実行（HOOK_STATE_DIR環境変数でBASE_DIRをオーバーライド）
        project_root = Path(__file__).resolve().parents[2]
        input_json = json.dumps({"session_id": "cli-test-sess"})
        result = subprocess.run(
            [sys.executable, "hooks/hook_state.py", "clear"],
            input=input_json,
            capture_output=True,
            text=True,
            cwd=str(project_root),
            env={**os.environ, "HOOK_STATE_DIR": str(tmp_path)},
        )
        assert result.returncode == 0

        # CLIで実際にファイルが削除されたことを確認
        assert state.get_current_turn() == 0
        assert state.get_block_count() == 0
