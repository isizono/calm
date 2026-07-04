"""hooks/user_prompt_submit_hook.py のE2Eテスト（イベント駆動アーキテクチャ版）

subprocess.runでuser_prompt_submit_hook.pyを呼び出し、stdin→stdoutの入出力をテスト。
nudge判定はevents.jsonl内のnudgeイベントに基づく。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hooks.hook_state import HookState

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SESSION_ID = "e2e-test-session-001"


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """テスト用のstateディレクトリを返し、HookStateのBASE_DIRもオーバーライド"""
    monkeypatch.setattr(HookState, "BASE_DIR", tmp_path)
    return tmp_path


def _run_hook(input_data: dict, state_dir: Path) -> subprocess.CompletedProcess:
    """user_prompt_submit_hook.pyをサブプロセスで実行する"""
    return subprocess.run(
        [sys.executable, "hooks/user_prompt_submit_hook.py"],
        input=json.dumps(input_data),
        capture_output=True,
        text=True,
        cwd=str(_PROJECT_ROOT),
        env={**os.environ, "HOOK_STATE_DIR": str(state_dir)},
    )


def _write_events(events: list[dict], state_dir: Path) -> None:
    """events.jsonlをpre-seedする"""
    state = HookState(_SESSION_ID)
    state.append_events(events)


class TestNoNudge:
    """nudgeイベントなし → 空JSON"""

    def test_empty_json_when_no_events(self, state_dir):
        result = _run_hook({"session_id": _SESSION_ID}, state_dir)
        assert result.returncode == 0
        assert json.loads(result.stdout) == {}

    def test_empty_json_when_no_nudge_events(self, state_dir):
        _write_events(
            [
                {"e": "tool", "name": "get_topics", "turn": 1},
                {"e": "meta", "topic": "test", "turn": 1},
            ],
            state_dir,
        )
        result = _run_hook({"session_id": _SESSION_ID}, state_dir)
        assert result.returncode == 0
        assert json.loads(result.stdout) == {}


class TestRecordNudge:
    """record nudgeイベント → system-reminder注入（hookEventName="UserPromptSubmit"）"""

    def test_record_nudge_injection(self, state_dir):
        _write_events(
            [{"e": "nudge", "type": "record", "turn": 2}],
            state_dir,
        )

        result = _run_hook({"session_id": _SESSION_ID}, state_dir)
        assert result.returncode == 0

        output = json.loads(result.stdout)
        assert "hookSpecificOutput" in output
        assert output["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"

        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert "<system-reminder>" in ctx
        assert "直近の応答で記録ツール" in ctx
        assert "add_decisions" in ctx

    def test_nudge_consumed_after_injection(self, state_dir):
        """nudge消費後は空JSON"""
        _write_events(
            [{"e": "nudge", "type": "record", "turn": 2}],
            state_dir,
        )

        _run_hook({"session_id": _SESSION_ID}, state_dir)

        # 2回目は空JSON
        result = _run_hook({"session_id": _SESSION_ID}, state_dir)
        assert json.loads(result.stdout) == {}


class TestRecordNudgeMultiplication:
    """record nudge増殖: repeatフィールドに応じてメッセージが繰り返される"""

    def test_repeat_2_doubles_message(self, state_dir):
        """repeat=2 → メッセージが2回注入される"""
        _write_events(
            [{"e": "nudge", "type": "record", "turn": 4, "repeat": 2}],
            state_dir,
        )

        result = _run_hook({"session_id": _SESSION_ID}, state_dir)
        output = json.loads(result.stdout)
        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert ctx.count("直近の応答で記録ツール") == 2

    def test_repeat_5_quintuples_message(self, state_dir):
        """repeat=5 → メッセージが5回注入される（上限到達）"""
        _write_events(
            [{"e": "nudge", "type": "record", "turn": 10, "repeat": 5}],
            state_dir,
        )

        result = _run_hook({"session_id": _SESSION_ID}, state_dir)
        output = json.loads(result.stdout)
        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert ctx.count("直近の応答で記録ツール") == 5

    def test_no_repeat_field_defaults_to_1(self, state_dir):
        """repeatフィールドなし → メッセージ1回（デフォルト値）"""
        _write_events(
            [{"e": "nudge", "type": "record", "turn": 2}],
            state_dir,
        )

        result = _run_hook({"session_id": _SESSION_ID}, state_dir)
        output = json.loads(result.stdout)
        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert ctx.count("直近の応答で記録ツール") == 1


class TestFollowUpNudge:
    """follow_up nudgeイベント → system-reminder注入"""

    def test_follow_up_nudge_injection(self, state_dir):
        _write_events(
            [{"e": "nudge", "type": "follow_up", "turn": 3}],
            state_dir,
        )

        result = _run_hook({"session_id": _SESSION_ID}, state_dir)
        assert result.returncode == 0

        output = json.loads(result.stdout)
        assert "hookSpecificOutput" in output
        assert output["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert "add_decisions" in ctx
        assert "topic" in ctx
        assert "material" in ctx
        assert "tag_notes" in ctx

    def test_follow_up_nudge_takes_priority(self, state_dir):
        """follow_up nudgeが最新なら、record nudgeより優先"""
        _write_events(
            [
                {"e": "nudge", "type": "record", "turn": 2},
                {"e": "nudge", "type": "follow_up", "turn": 3},
            ],
            state_dir,
        )

        result = _run_hook({"session_id": _SESSION_ID}, state_dir)
        output = json.loads(result.stdout)
        ctx = output["hookSpecificOutput"]["additionalContext"]
        # follow_up nudgeが注入される（最新のnudgeが先に消費される）
        assert "補完すべき記録" in ctx

        # record nudgeはまだ残っている
        result2 = _run_hook({"session_id": _SESSION_ID}, state_dir)
        output2 = json.loads(result2.stdout)
        ctx2 = output2["hookSpecificOutput"]["additionalContext"]
        assert "直近の応答で記録ツール" in ctx2


class TestEmptySessionId:
    """session_id空 → 空JSON"""

    def test_empty_session_id(self, state_dir):
        result = _run_hook({"session_id": ""}, state_dir)
        assert result.returncode == 0
        assert json.loads(result.stdout) == {}

    def test_null_session_id(self, state_dir):
        result = _run_hook({"session_id": None}, state_dir)
        assert result.returncode == 0
        assert json.loads(result.stdout) == {}


class TestIdLeakNudge:
    """id_leak_count > 0 → 英語 system-reminder 注入 + count reset"""

    def _set_id_leak_count(self, count: int) -> None:
        state = HookState(_SESSION_ID)
        for _ in range(count):
            state.increment_id_leak_count()

    def test_id_leak_injection(self, state_dir):
        self._set_id_leak_count(1)

        result = _run_hook({"session_id": _SESSION_ID}, state_dir)
        assert result.returncode == 0

        output = json.loads(result.stdout)
        assert "hookSpecificOutput" in output
        assert output["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"

        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert "<system-reminder>" in ctx
        assert "internal IDs" in ctx
        assert "natural language" in ctx

    def test_id_leak_count_reset_after_injection(self, state_dir):
        self._set_id_leak_count(3)

        _run_hook({"session_id": _SESSION_ID}, state_dir)

        # count リセット
        assert HookState(_SESSION_ID).get_id_leak_count() == 0

        # 2 回目は空JSON
        result = _run_hook({"session_id": _SESSION_ID}, state_dir)
        assert json.loads(result.stdout) == {}

    def test_zero_count_no_injection(self, state_dir):
        # count=0 (state file 不在) なら空JSON
        result = _run_hook({"session_id": _SESSION_ID}, state_dir)
        assert json.loads(result.stdout) == {}

    def test_existing_nudge_takes_priority(self, state_dir):
        """既存 nudge (record/follow_up) があれば優先、id_leak は次ターンに繰り越す"""
        _write_events(
            [{"e": "nudge", "type": "record", "turn": 2}],
            state_dir,
        )
        self._set_id_leak_count(1)

        # 1 回目: record nudge 注入、id_leak count はリセットされない
        result = _run_hook({"session_id": _SESSION_ID}, state_dir)
        ctx = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "直近の応答で記録ツール" in ctx
        assert HookState(_SESSION_ID).get_id_leak_count() == 1

        # 2 回目: id_leak 注入 + count リセット
        result2 = _run_hook({"session_id": _SESSION_ID}, state_dir)
        ctx2 = json.loads(result2.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "internal IDs" in ctx2
        assert HookState(_SESSION_ID).get_id_leak_count() == 0


class TestFailOpen:
    """例外→空JSON（フェイルオープン）"""

    def test_invalid_json_input(self, state_dir):
        proc = subprocess.run(
            [sys.executable, "hooks/user_prompt_submit_hook.py"],
            input="not valid json",
            capture_output=True,
            text=True,
            cwd=str(_PROJECT_ROOT),
            env={**os.environ, "HOOK_STATE_DIR": str(state_dir)},
        )
        assert proc.returncode == 0
        assert json.loads(proc.stdout) == {}
        assert "error" in proc.stderr.lower()

    def test_invalid_json_input_records_machine_error_signal(self, state_dir, temp_db):
        """top-level except到達時にsignal_eventsへmachine_errorが記録される"""
        from src.db import get_connection

        proc = subprocess.run(
            [sys.executable, "hooks/user_prompt_submit_hook.py"],
            input="not valid json",
            capture_output=True,
            text=True,
            cwd=str(_PROJECT_ROOT),
            env={**os.environ, "HOOK_STATE_DIR": str(state_dir), "DISCUSSION_DB_PATH": temp_db},
        )
        assert proc.returncode == 0
        assert json.loads(proc.stdout) == {}

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM signal_events WHERE source = 'hook:user_prompt_submit'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row["kind"] == "machine_error"
