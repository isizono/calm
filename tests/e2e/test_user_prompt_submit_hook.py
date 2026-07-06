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
    """record nudge文言: repeat段階に応じてtierが変わり、実測ターン数(turns_since)が
    文中に埋め込まれる（旧: 同一文言をrepeat回連結する仕様だった）"""

    def test_no_repeat_field_defaults_to_1(self, state_dir):
        """repeatフィールドなし → tier=lowの文言が1回だけ出力され、反復連結は発生しない"""
        _write_events(
            [{"e": "nudge", "type": "record", "turn": 2}],
            state_dir,
        )

        result = _run_hook({"session_id": _SESSION_ID}, state_dir)
        output = json.loads(result.stdout)
        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert ctx.count("直近の応答で記録ツール") == 1

    def test_repeat_3_uses_mid_tier_with_turns_since_embedded(self, state_dir):
        """repeat=3 → tier=midの文言が使われ、turns_sinceの実測値が本文に埋め込まれる"""
        _write_events(
            [{"e": "nudge", "type": "record", "turn": 6, "repeat": 3, "turns_since": 6}],
            state_dir,
        )

        result = _run_hook({"session_id": _SESSION_ID}, state_dir)
        output = json.loads(result.stdout)
        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert "6ターン記録ツール" in ctx
        assert "経緯が失われつつあります" in ctx
        # tier=lowの文言(旧仕様の単純反復)は混入しない
        assert "該当なしなら無視してOK" not in ctx

    def test_repeat_5_uses_high_tier_with_turns_since_embedded(self, state_dir):
        """repeat=5（上限到達） → tier=highの強い文言が使われ、turns_sinceが埋め込まれる"""
        _write_events(
            [{"e": "nudge", "type": "record", "turn": 10, "repeat": 5, "turns_since": 10}],
            state_dir,
        )

        result = _run_hook({"session_id": _SESSION_ID}, state_dir)
        output = json.loads(result.stdout)
        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert "10ターン以上記録ツールが呼ばれていません" in ctx
        assert "セッションの経緯が失われる可能性が高い" in ctx

    def test_turns_since_missing_falls_back_to_repeat_times_two(self, state_dir):
        """turns_sinceフィールドがない旧形式のnudgeイベント（後方互換）でも例外にならず、
        repeat*2の近似値で文言が生成される"""
        _write_events(
            [{"e": "nudge", "type": "record", "turn": 6, "repeat": 3}],
            state_dir,
        )

        result = _run_hook({"session_id": _SESSION_ID}, state_dir)
        assert result.returncode == 0
        output = json.loads(result.stdout)
        ctx = output["hookSpecificOutput"]["additionalContext"]
        assert "6ターン記録ツール" in ctx  # repeat(3) * 2 = 6 で近似


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
