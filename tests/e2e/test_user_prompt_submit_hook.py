"""hooks/user_prompt_submit_hook.py のE2Eテスト（イベント駆動アーキテクチャ版）

user_prompt_submit_hook.pyを呼び出し、stdin→stdoutの入出力をテスト。
nudge判定はevents.jsonl内のnudgeイベントに基づく。
"""
import json
import subprocess
from pathlib import Path

import pytest

from hooks.hook_state import HookState
from tests.helpers import run_hook_subprocess

_SESSION_ID = "e2e-test-session-001"


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """テスト用のstateディレクトリを返し、HookStateのBASE_DIRもオーバーライド"""
    monkeypatch.setattr(HookState, "BASE_DIR", tmp_path)
    return tmp_path


def _run_hook(
    input_data: dict, state_dir: Path, extra_env: dict | None = None
) -> subprocess.CompletedProcess:
    """user_prompt_submit_hook.pyをサブプロセスで実行する"""
    env = {"HOOK_STATE_DIR": str(state_dir)}
    if extra_env:
        env.update(extra_env)
    return run_hook_subprocess(
        "hooks/user_prompt_submit_hook.py", json.dumps(input_data), extra_env=env
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


class TestAskNotify:
    """add_ask通知の二重網（3.5節）のE2Eテスト。

    tracked_ask_idsはHookState経由で直接書き込む（Stop hookが通常書く経路の
    代わりに、本テストではUserPromptSubmit hook単体の消費側だけを検証する
    ため）。identity解決には一切触れない。
    """

    def _seed_activity(self) -> int:
        from src.db import get_connection

        conn = get_connection()
        try:
            cursor = conn.execute(
                "INSERT INTO activities (title, description, status) VALUES (?, ?, ?)",
                ("a1", "desc", "pending"),
            )
            activity_id = cursor.lastrowid
            tag_row = conn.execute(
                "SELECT id FROM tags WHERE namespace = 'domain' AND name = ?", ("test",)
            ).fetchone()
            if tag_row:
                tag_id = tag_row["id"]
            else:
                cursor = conn.execute(
                    "INSERT INTO tags (namespace, name) VALUES ('domain', ?)", ("test",)
                )
                tag_id = cursor.lastrowid
            conn.execute(
                "INSERT INTO activity_tags (activity_id, tag_id) VALUES (?, ?)",
                (activity_id, tag_id),
            )
            conn.commit()
            return activity_id
        finally:
            conn.close()

    def test_resolved_tracked_ask_is_injected_and_consumed(self, state_dir, temp_db):
        from src.services import ask_service as ak

        act = self._seed_activity()
        r1 = ak.add_ask("何色にする?", tags=["domain:test"], blocks=[act])
        ak.answer_ask(r1["id"], "青にしよう")

        state = HookState(_SESSION_ID)
        state.add_tracked_ask_ids([r1["id"]])

        result = _run_hook(
            {"session_id": _SESSION_ID}, state_dir, extra_env={"DISCUSSION_DB_PATH": temp_db}
        )
        output = json.loads(result.stdout)
        ctx = output["hookSpecificOutput"]["additionalContext"]

        assert "<system-reminder>" in ctx
        assert "askの回答が届いています" in ctx
        assert "何色にする?" in ctx
        assert "get_asks" in ctx
        assert "青にしよう" not in ctx  # 回答本文はhook経由で注入しない
        # 消費済み: 追跡対象から外れている
        assert state.get_tracked_ask_ids() == []

    def test_ask_notify_takes_priority_over_record_nudge(self, state_dir, temp_db):
        """record nudgeイベントと解決済みaskが同時に存在する場合、3.5節の
        returnで以降のnudge判定（手順4）がスキップされ、ask通知が優先される。
        record nudgeイベント自体はconsumedマークされずに温存される。"""
        from src.services import ask_service as ak

        act = self._seed_activity()
        r1 = ak.add_ask("優先されるはずの質問", tags=["domain:test"], blocks=[act])
        ak.answer_ask(r1["id"], "優先されるはずの回答")

        state = HookState(_SESSION_ID)
        state.add_tracked_ask_ids([r1["id"]])
        _write_events([{"e": "nudge", "type": "record", "turn": 2}], state_dir)

        result = _run_hook(
            {"session_id": _SESSION_ID}, state_dir, extra_env={"DISCUSSION_DB_PATH": temp_db}
        )
        ctx = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "askの回答が届いています" in ctx
        assert "直近の応答で記録ツール" not in ctx

        # askは既に消費済みなので、2回目の呼び出しでは温存されていたrecord nudgeが出る
        result2 = _run_hook(
            {"session_id": _SESSION_ID}, state_dir, extra_env={"DISCUSSION_DB_PATH": temp_db}
        )
        ctx2 = json.loads(result2.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "直近の応答で記録ツール" in ctx2

    def test_no_tracked_asks_falls_through_to_nudge_check(self, state_dir, temp_db):
        """追跡中askが無ければ3.5節は素通りし、従来通り手順4のnudge判定が動作する。"""
        _write_events([{"e": "nudge", "type": "record", "turn": 2}], state_dir)

        result = _run_hook(
            {"session_id": _SESSION_ID}, state_dir, extra_env={"DISCUSSION_DB_PATH": temp_db}
        )
        ctx = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "askの回答が届いています" not in ctx
        assert "直近の応答で記録ツール" in ctx

    def test_long_answer_exceeding_session_start_budget_is_shown_in_full_and_consumed(
        self, state_dir, temp_db
    ):
        """本経路はcompose()を経由せず文字数予算を持たない。回答本文は
        hook経由で注入しないため行の長さはquestionだけで決まるが、
        2件を同時に追跡し合計がSessionStart側の予算（既定600字）を超える
        組み合わせでも、本経路は予算を意識せず両方とも全文表示・消費される
        （対照: 同じ2件をSessionStart hook経由で処理すると1件しか表示され
        ないことをtests/e2e/test_session_start_hook.py::
        test_answer_exceeding_budget_stays_tracked_across_repeated_calls
        で確認している）。"""
        from src.services import ask_service as ak

        act = self._seed_activity()
        long_q_a = "A" * 500  # 質問はサービス層で500字上限
        long_q_b = "B" * 500
        r_a = ak.add_ask(long_q_a, tags=["domain:test"], blocks=[act])
        r_b = ak.add_ask(long_q_b, tags=["domain:test"], blocks=[act])
        ak.answer_ask(r_a["id"], "answer a")
        ak.answer_ask(r_b["id"], "answer b")

        state = HookState(_SESSION_ID)
        state.add_tracked_ask_ids([r_a["id"], r_b["id"]])

        result = _run_hook(
            {"session_id": _SESSION_ID}, state_dir, extra_env={"DISCUSSION_DB_PATH": temp_db}
        )
        ctx = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]

        assert "askの回答が届いています" in ctx
        assert long_q_a in ctx
        assert long_q_b in ctx
        assert state.get_tracked_ask_ids() == []


class TestEmptyStdin:
    """stdin空/空白のみ → 空JSON、machine_errorシグナルは記録しない"""

    def test_whitespace_only_stdin_returns_empty_json(self, state_dir):
        proc = run_hook_subprocess(
            "hooks/user_prompt_submit_hook.py",
            "   \n\t",
            extra_env={"HOOK_STATE_DIR": str(state_dir)},
        )
        assert proc.returncode == 0
        assert json.loads(proc.stdout) == {}

    def test_empty_stdin_does_not_record_signal(self, state_dir, temp_db):
        """空stdinはjson.loadsの例外経路に入らず、signal_eventsへ記録されない"""
        from src.db import get_connection

        proc = run_hook_subprocess(
            "hooks/user_prompt_submit_hook.py",
            "",
            extra_env={"HOOK_STATE_DIR": str(state_dir), "DISCUSSION_DB_PATH": temp_db},
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
        assert row is None


class TestFailOpen:
    """例外→空JSON（フェイルオープン）"""

    def test_invalid_json_input(self, state_dir, temp_db):
        proc = run_hook_subprocess(
            "hooks/user_prompt_submit_hook.py",
            "not valid json",
            extra_env={"HOOK_STATE_DIR": str(state_dir), "DISCUSSION_DB_PATH": temp_db},
        )
        assert proc.returncode == 0
        assert json.loads(proc.stdout) == {}
        assert "error" in proc.stderr.lower()

    def test_invalid_json_input_records_machine_error_signal(self, state_dir, temp_db):
        """top-level except到達時にsignal_eventsへmachine_errorが記録される"""
        from src.db import get_connection

        proc = run_hook_subprocess(
            "hooks/user_prompt_submit_hook.py",
            "not valid json",
            extra_env={"HOOK_STATE_DIR": str(state_dir), "DISCUSSION_DB_PATH": temp_db},
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
