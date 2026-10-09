"""hooks/user_prompt_submit_hook.py のE2Eテスト（イベント駆動アーキテクチャ版）

user_prompt_submit_hook.pyを呼び出し、stdin→stdoutの入出力をテスト。
nudge判定はevents.jsonl内のnudgeイベントに基づく。
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

from hooks import user_prompt_submit_hook
from hooks.hook_state import HookState
from src.infra import file_ops
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

    def test_record_before_finish_nudge_injected_once(self, state_dir):
        _write_events([{"e": "nudge", "type": "record_before_finish", "turn": 2}], state_dir)

        first = json.loads(_run_hook({"session_id": _SESSION_ID}, state_dir).stdout)
        ctx = first["hookSpecificOutput"]["additionalContext"]
        assert "<system-reminder>" in ctx
        assert "add_logs" in ctx

        second = _run_hook({"session_id": _SESSION_ID}, state_dir)
        assert json.loads(second.stdout) == {}

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


class TestNonhumanTurnSuppressesNudge:
    """promptが人間の発話でないターン（中継・通知）のときはnudgeを配達しない。
    イベントはconsumedマークされず温存され、次の人間の発話で改めて配達される。"""

    def test_cross_session_relay_prompt_suppresses_nudge(self, state_dir):
        _write_events([{"e": "nudge", "type": "record", "turn": 2}], state_dir)
        prompt = (
            'Another Claude session sent a message:\n'
            '<agent-message from="abc">report body</agent-message>'
        )

        result = _run_hook({"session_id": _SESSION_ID, "prompt": prompt}, state_dir)
        assert result.returncode == 0
        assert json.loads(result.stdout) == {}

        # 温存されているので、次の人間の発話で配達される
        result2 = _run_hook({"session_id": _SESSION_ID, "prompt": "次のふつうの発話"}, state_dir)
        ctx2 = json.loads(result2.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "直近の応答で記録ツール" in ctx2

    def test_task_notification_prompt_suppresses_nudge(self, state_dir):
        _write_events([{"e": "nudge", "type": "follow_up", "turn": 3}], state_dir)
        prompt = "<task-notification>\n<status>completed</status>\n</task-notification>"

        result = _run_hook({"session_id": _SESSION_ID, "prompt": prompt}, state_dir)
        assert result.returncode == 0
        assert json.loads(result.stdout) == {}

    def test_plain_human_prompt_still_delivers_nudge(self, state_dir):
        """マーカーを含まない通常の発話では、従来通り配達される（回帰確認）。"""
        _write_events([{"e": "nudge", "type": "record", "turn": 2}], state_dir)

        result = _run_hook({"session_id": _SESSION_ID, "prompt": "ふつうの発話です"}, state_dir)
        ctx = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "直近の応答で記録ツール" in ctx


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


class TestNeighborAskNotify:
    """check_in先と隣の作業を止めていたaskの知らせ。自分のaskの行と同じ1回の出力にまとまり、
    早期returnは自分のaskの行を出したときだけ起きる（隣の作業の行は記録の促しを押し出さない）。
    """

    def _answered_other_ask(self, state_dir: Path, question: str = "別セッションの問い") -> tuple[int, HookState]:
        from src.services import ask_service as ak
        from src.services.activity_service import add_activity

        act = add_activity(title="a1", description="d", tags=["domain:test"], check_in=False)["activity_id"]
        ask_id = ak.add_ask(question, tags=["domain:test"], blocks=[act])["id"]
        ak.answer_ask(ask_id, "回答本文")
        state = HookState(_SESSION_ID)
        state.set_checked_in_activity(act)
        state.set_checked_in_at("2000-01-01 00:00:00")
        return ask_id, state

    def _run(self, state_dir: Path, temp_db: str, prompt: str | None = None) -> str:
        payload = {"session_id": _SESSION_ID}
        if prompt is not None:
            payload["prompt"] = prompt
        result = _run_hook(payload, state_dir, extra_env={"DISCUSSION_DB_PATH": temp_db})
        return json.loads(result.stdout).get("hookSpecificOutput", {}).get("additionalContext", "")

    def test_neighbor_line_is_injected_once_without_answer_body(self, state_dir, temp_db):
        ask_id, state = self._answered_other_ask(state_dir)

        ctx = self._run(state_dir, temp_db)

        assert "別セッションの問い" in ctx
        assert "回答本文" not in ctx
        assert state.get_notified_ask_ids() == {ask_id}
        assert self._run(state_dir, temp_db) == ""

    def test_neighbor_line_does_not_push_out_record_nudge(self, state_dir, temp_db):
        self._answered_other_ask(state_dir)
        _write_events([{"e": "nudge", "type": "record", "turn": 2}], state_dir)

        ctx = self._run(state_dir, temp_db)

        assert "別セッションの問い" in ctx
        assert "直近の応答で記録ツール" in ctx

    def test_own_ask_line_and_neighbor_line_share_one_output_and_nudge_is_kept(self, state_dir, temp_db):
        from src.services import ask_service as ak

        _ask_id, state = self._answered_other_ask(state_dir)
        own = ak.add_ask(
            "自分の問い", tags=["domain:test"], blocks=[state.get_checked_in_activity()]
        )["id"]
        ak.answer_ask(own, "自分の回答")
        state.add_tracked_ask_ids([own])
        _write_events([{"e": "nudge", "type": "record", "turn": 2}], state_dir)

        ctx = self._run(state_dir, temp_db)

        assert "askの回答が届いています" in ctx and "自分の問い" in ctx
        assert "別セッションの問い" in ctx
        assert ctx.count("自分の問い") == 1
        assert "直近の応答で記録ツール" not in ctx
        assert "直近の応答で記録ツール" in self._run(state_dir, temp_db)

    def test_neighbor_line_is_held_on_nonhuman_turn(self, state_dir, temp_db):
        ask_id, state = self._answered_other_ask(state_dir)
        prompt = "<task-notification>\n<status>completed</status>\n</task-notification>"

        assert self._run(state_dir, temp_db, prompt) == ""
        assert state.get_notified_ask_ids() == set()
        assert "別セッションの問い" in self._run(state_dir, temp_db)


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

    def test_ask_notify_delivered_even_on_nonhuman_turn(self, state_dir, temp_db):
        """promptが人間の発話でないターン（中継・通知）でも、ask通知は配達される
        （3.5節は非人間ターン判定より前に処理され、識別には一切触れない）。"""
        from src.services import ask_service as ak

        act = self._seed_activity()
        r1 = ak.add_ask("非人間ターンでも届くはずの質問", tags=["domain:test"], blocks=[act])
        ak.answer_ask(r1["id"], "回答")

        state = HookState(_SESSION_ID)
        state.add_tracked_ask_ids([r1["id"]])

        prompt = "<task-notification>\n<status>completed</status>\n</task-notification>"
        result = _run_hook(
            {"session_id": _SESSION_ID, "prompt": prompt},
            state_dir,
            extra_env={"DISCUSSION_DB_PATH": temp_db},
        )
        ctx = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "askの回答が届いています" in ctx
        assert "非人間ターンでも届くはずの質問" in ctx

    def test_questions_exceeding_session_start_budget_are_shown_in_full_and_consumed(
        self, state_dir, temp_db
    ):
        """本経路はcompose()を経由せず文字数予算を持たない。回答本文は
        hook経由で注入しないため行の長さはquestionだけで決まるが、
        2件を同時に追跡し合計がSessionStart側の予算（既定600字）を超える
        組み合わせでも、本経路は予算を意識せず両方とも全文表示・消費される
        （対照: 同じ2件をSessionStart hook経由で処理すると1件しか表示され
        ないことをtests/e2e/test_session_start_hook.py::
        test_questions_exceeding_budget_are_deferred_not_lost_across_calls
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


class TestRewriteEventsRetry:
    """_rewrite_eventsはsubprocess経由のsignal_eventsと無関係なため、
    直接importしてos.replaceの一時失敗を再現する（in-process）。"""

    def test_recovers_after_transient_replace_error(self, state_dir, monkeypatch):
        """os.replaceの一時失敗（Windowsの共有違反相当）を再試行で乗り越え、
        書き換えを完了する。呼び出し側がreplace_retryingを経由せずos.replaceへ
        直書きする退行を検知する。"""
        state = HookState(_SESSION_ID)
        state.append_events([{"e": "nudge", "turn": 1}])

        real_replace = os.replace
        calls = {"count": 0}

        def flaky_replace(a, b):
            calls["count"] += 1
            if calls["count"] == 1:
                raise OSError(32, "The process cannot access the file")
            return real_replace(a, b)

        monkeypatch.setattr(file_ops.os, "replace", flaky_replace)
        monkeypatch.setattr(file_ops.time, "sleep", lambda _: None)

        user_prompt_submit_hook._rewrite_events(state, [{"e": "nudge", "turn": 1, "consumed": True}])

        assert calls["count"] == 2
        assert state.read_events() == [{"e": "nudge", "turn": 1, "consumed": True}]


class TestRecorderReviveWiring:
    """人の発話のときだけ記録役の起こし直しを試み、失敗しても他の配達を止めない。

    CLAUDE_PIDには存在しないpidを渡すので、起こし直しの子プロセス（scripts/recorder.py start）は
    本体の起動時刻を取れずにすぐ終わり、tmux・claudeは起動しない。試みた印は実行ディレクトリの
    revive_attemptに残る。
    """

    _ENV = {"CALM_RECORDER": "1", "CLAUDE_CODE_SESSION_ATTENDED": "1", "CLAUDE_PID": "999999"}

    def _attempt_file(self, state_dir: Path) -> Path:
        return state_dir / "recorder_runs" / _SESSION_ID / "revive_attempt"

    def test_human_prompt_attempts_revive(self, state_dir):
        _run_hook({"session_id": _SESSION_ID, "prompt": "続けて", "transcript_path": "/nonexistent"},
                  state_dir, self._ENV)
        assert self._attempt_file(state_dir).exists()

    def test_relayed_message_does_not_attempt_revive(self, state_dir):
        prompt = '<cross-session-message from="x">周知</cross-session-message>'
        _run_hook({"session_id": _SESSION_ID, "prompt": prompt, "transcript_path": "/nonexistent"},
                  state_dir, self._ENV)
        assert not self._attempt_file(state_dir).exists()

    def test_revive_failure_does_not_stop_nudge_delivery(self, state_dir):
        (state_dir / "recorder_runs").write_text("壊れた置き場", encoding="utf-8")
        _write_events([{"e": "nudge", "type": "record", "turn": 1, "repeat": 1}], state_dir)

        result = _run_hook({"session_id": _SESSION_ID, "prompt": "続けて", "transcript_path": "/nonexistent"},
                           state_dir, self._ENV)

        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "記録ツール" in context
