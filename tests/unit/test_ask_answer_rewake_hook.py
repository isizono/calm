"""hooks/ask_answer_rewake_hook.py の単体テスト。

DBは実SQLite（temp_db、全migration適用）を使い、askの状態遷移はすべて
ask_serviceの実関数（add_ask/answer_ask/triage_ask/withdraw_ask/
unsubscribe_ask）で作る。main()のsleep/now引数を差し替え、sleepの副作用
としてDBを変化させることで「待機中に回答された」を再現する。
"""
import fcntl
import io
import json
import os
import sqlite3
from unittest.mock import patch

import pytest

from hooks import ask_answer_rewake_hook as hook
from src.services import ask_service
from src.services.activity_service import add_activity
from src.services.topic_service import add_topic


ADD_ASK_TOOL_NAME = "mcp__plugin_calm_calm__add_ask"


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch, disable_embedding):
    """CALM_DB_PATH/HOOK_STATE_DIR/CALM_ASK_NOTIFY_DIRを一時パスへ向ける。"""
    monkeypatch.setenv("HOOK_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CALM_ASK_NOTIFY_DIR", str(tmp_path / "notify"))
    monkeypatch.delenv("CLAUDE_PID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ATTENDED", raising=False)


@pytest.fixture
def db(tmp_path, monkeypatch, temp_db):
    """temp_dbはDISCUSSION_DB_PATHを設定するので、hookが読むCALM_DB_PATHへも
    明示的に向ける（env_compatのフォールバック順に頼らない）。"""
    monkeypatch.setenv("CALM_DB_PATH", temp_db)
    return temp_db


def _make_activity() -> int:
    return add_activity(
        title="a1", description="d", tags=["domain:test"], check_in=False
    )["activity_id"]


def _make_ask(session_id: str = "sess-1", notify: bool = True, question: str = "q?") -> dict:
    act = _make_activity()
    return ask_service.add_ask(
        question, blocks=[act], tags=["domain:test"], session_id=session_id, notify=notify
    )


def _stdin_payload(
    *,
    session_id: str,
    tool_name: str = ADD_ASK_TOOL_NAME,
    notify: bool | None = None,
    tool_response,
    agent_type: str | None = None,
) -> dict:
    tool_input = {"question": "q?", "blocks": [1], "tags": ["domain:test"]}
    if notify is not None:
        tool_input["notify"] = notify
    payload = {
        "session_id": session_id,
        "hook_event_name": "PostToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_response": tool_response,
    }
    if agent_type is not None:
        payload["agent_type"] = agent_type
    return payload


def _sleep_stub(*side_effects):
    """呼び出し順にside_effects（引数無し callable）を1つずつ実行するsleep差し替え。

    side_effectsを使い切った後の呼び出しは何もしない。
    """
    calls: list[float] = []

    def _sleep(seconds):
        calls.append(seconds)
        idx = len(calls) - 1
        if idx < len(side_effects):
            side_effects[idx]()

    _sleep.calls = calls
    return _sleep


def _run_hook(payload: dict, *, sleep=None, now=None) -> tuple[int, str]:
    fake_stdin = io.StringIO(json.dumps(payload))
    fake_stderr = io.StringIO()
    kwargs = {}
    if sleep is not None:
        kwargs["sleep"] = sleep
    if now is not None:
        kwargs["now"] = now
    with patch.object(hook.sys, "stdin", fake_stdin), \
         patch.object(hook.sys, "stderr", fake_stderr):
        code = hook.main(**kwargs)
    return code, fake_stderr.getvalue()


def _fixed_clock(value: float = 1000.0):
    return lambda: value


def _row(db_path: str, ask_id: int) -> tuple:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT status, notify_wanted FROM asks WHERE id = ?", (ask_id,)
        ).fetchone()
    finally:
        conn.close()


class TestWakesOnResolution:
    def test_answered_wakes_with_fixed_message(self, db):
        result = _make_ask(session_id="sess-1")
        ask_id = result["id"]
        marker = "MARKER_ANSWER_BODY_SHOULD_NOT_LEAK"

        def _answer():
            ask_service.answer_ask(ask_id, marker)

        payload = _stdin_payload(
            session_id="sess-1",
            tool_response=json.dumps(result),
        )
        sleep = _sleep_stub(_answer)
        code, stderr = _run_hook(payload, sleep=sleep, now=_fixed_clock())

        assert code == 2
        assert f"#{ask_id}" in stderr
        assert "status: answered" in stderr
        assert "status=null" in stderr
        assert marker not in stderr
        assert "q?" not in stderr

    def test_dismissed_wakes(self, db):
        result = _make_ask(session_id="sess-1")
        ask_id = result["id"]

        def _resolve():
            ask_service.answer_ask(ask_id, "answer body")
            ask_service.triage_ask(ask_id, "dismiss", dismiss_reason="not relevant anymore")

        payload = _stdin_payload(session_id="sess-1", tool_response=json.dumps(result))
        sleep = _sleep_stub(_resolve)
        code, stderr = _run_hook(payload, sleep=sleep, now=_fixed_clock())

        assert code == 2
        assert "status: dismissed" in stderr

    def test_promoted_wakes(self, db):
        """promoteはtriage_ask_with_connが_notify_wantedを立てない経路(promote
        docstring参照)。notify_wanted列自体はanswer_ask時点でTrueのまま変化
        しないので、hookはDBの列を直接見ており待機が続くことを確かめる。"""
        result = _make_ask(session_id="sess-1")
        ask_id = result["id"]

        topic_id = add_topic(title="t1", description="d", tags=["domain:test"])["topic_id"]

        def _resolve():
            ask_service.answer_ask(ask_id, "answer body")
            ask_service.triage_ask(
                ask_id, "promote", decision="決定した内容", reason="判断理由", topic_id=topic_id
            )

        payload = _stdin_payload(session_id="sess-1", tool_response=json.dumps(result))
        sleep = _sleep_stub(_resolve)
        code, stderr = _run_hook(payload, sleep=sleep, now=_fixed_clock())

        assert code == 2
        assert "status: promoted" in stderr


class TestDoesNotWake:
    def test_withdrawn_does_not_wake(self, db):
        result = _make_ask(session_id="sess-1")
        ask_id = result["id"]

        def _withdraw():
            ask_service.withdraw_ask(ask_id, "no longer needed")

        payload = _stdin_payload(session_id="sess-1", tool_response=json.dumps(result))
        sleep = _sleep_stub(_withdraw)
        code, stderr = _run_hook(payload, sleep=sleep, now=_fixed_clock())

        assert code == 0
        assert stderr == ""

    def test_unsubscribe_stops_waiting(self, db):
        result = _make_ask(session_id="sess-1")
        ask_id = result["id"]

        def _unsubscribe():
            ask_service.unsubscribe_ask(ask_id)

        payload = _stdin_payload(session_id="sess-1", tool_response=json.dumps(result))
        sleep = _sleep_stub(_unsubscribe)
        code, stderr = _run_hook(payload, sleep=sleep, now=_fixed_clock())

        assert code == 0
        assert stderr == ""

    def test_notify_false_does_not_wait(self, db):
        result = _make_ask(session_id="sess-1", notify=False)
        payload = _stdin_payload(
            session_id="sess-1", notify=False, tool_response=json.dumps(result)
        )
        sleep = _sleep_stub()
        code, stderr = _run_hook(payload, sleep=sleep, now=_fixed_clock())

        assert code == 0
        assert stderr == ""
        assert sleep.calls == []

    def test_dedup_notify_false_does_not_wait_even_if_row_stays_notify_wanted(self, db):
        first = _make_ask(session_id="sess-1", question="same question", notify=True)
        ask_id = first["id"]
        # dedup: 同じ質問文でnotify=Falseを渡しても、DBのnotify_wantedは初回の
        # 値(1)のまま保たれる(ask_service.add_askのdocstring参照)。
        second = ask_service.add_ask(
            "same question", blocks=[1], tags=["domain:test"], session_id="sess-1", notify=False
        )
        assert second["id"] == ask_id
        assert second["deduped"] is True
        row = _row(db, ask_id)
        assert row[1] == 1  # notify_wanted は 1 のまま

        payload = _stdin_payload(
            session_id="sess-1", notify=False, tool_response=json.dumps(second)
        )
        sleep = _sleep_stub()
        code, stderr = _run_hook(payload, sleep=sleep, now=_fixed_clock())

        assert code == 0
        assert sleep.calls == []


class TestToolResponseShapes:
    def test_id_from_plain_json_string(self, db):
        result = _make_ask(session_id="sess-1")
        ask_id = result["id"]
        payload = _stdin_payload(session_id="sess-1", tool_response=json.dumps(result))
        sleep = _sleep_stub(lambda: ask_service.answer_ask(ask_id, "answer body"))
        code, stderr = _run_hook(payload, sleep=sleep, now=_fixed_clock())
        # code==2かつask_idが一致することで、この形からidを正しく取り出せた
        # ことを確認する(単にexit 0でないことだけでは、別のidを誤って拾った
        # 場合も見逃してしまう)。
        assert code == 2
        assert f"#{ask_id}" in stderr

    def test_id_from_dict_content(self, db):
        result = _make_ask(session_id="sess-1")
        ask_id = result["id"]
        payload = _stdin_payload(
            session_id="sess-1", tool_response={"content": json.dumps(result)}
        )
        sleep = _sleep_stub(lambda: ask_service.answer_ask(ask_id, "answer body"))
        code, stderr = _run_hook(payload, sleep=sleep, now=_fixed_clock())
        assert code == 2
        assert f"#{ask_id}" in stderr

    def test_error_only_does_not_wait(self, db):
        payload = _stdin_payload(
            session_id="sess-1", tool_response=json.dumps({"error": {"code": "VALIDATION_ERROR"}})
        )
        sleep = _sleep_stub()
        code, stderr = _run_hook(payload, sleep=sleep, now=_fixed_clock())
        assert code == 0
        assert sleep.calls == []

    def test_error_with_id_waits_for_that_id(self, db):
        result = _make_ask(session_id="sess-1")
        ask_id = result["id"]
        payload = _stdin_payload(
            session_id="sess-1",
            tool_response=json.dumps({"error": {"code": "TAG_ERROR"}, "id": ask_id}),
        )

        def _answer():
            ask_service.answer_ask(ask_id, "answer body")

        sleep = _sleep_stub(_answer)
        code, stderr = _run_hook(payload, sleep=sleep, now=_fixed_clock())
        assert code == 2
        assert f"#{ask_id}" in stderr


class TestDoubleWaitPrevention:
    def test_lock_already_held_does_not_wait(self, db, tmp_path):
        result = _make_ask(session_id="sess-1")
        ask_id = result["id"]
        # HOOK_STATE_DIRは_isolate_env(autouse)がtmp_path/"state"に設定済み。
        # hookが実際に使うのと同じパス規則でロックファイルを先取りする。
        lock_dir = tmp_path / "state" / "ask_rewake"
        lock_dir.mkdir(parents=True)
        lock_path = lock_dir / f"sess-1_{ask_id}.lock"
        lock_fh = open(lock_path, "a+")
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            payload = _stdin_payload(session_id="sess-1", tool_response=json.dumps(result))
            sleep = _sleep_stub()
            code, stderr = _run_hook(payload, sleep=sleep, now=_fixed_clock())
            assert code == 0
            assert sleep.calls == []
        finally:
            lock_fh.close()


class TestWaitLimitAndFailures:
    def test_wait_limit_exceeded_gives_up_while_open(self, db):
        result = _make_ask(session_id="sess-1")
        clock_values = iter([0.0, hook.WAIT_LIMIT_SECONDS + 1.0])
        code, stderr = _run_hook(
            _stdin_payload(session_id="sess-1", tool_response=json.dumps(result)),
            sleep=_sleep_stub(),
            now=lambda: next(clock_values),
        )
        assert code == 0
        assert stderr == ""
        row = _row(db, result["id"])
        assert row[0] == "open"

    def test_db_failure_does_not_wake(self, db, monkeypatch):
        result = _make_ask(session_id="sess-1")
        monkeypatch.setenv("CALM_DB_PATH", "/nonexistent/path/does-not-exist.db")
        clock_values = iter([0.0, hook.WAIT_LIMIT_SECONDS + 1.0])
        code, stderr = _run_hook(
            _stdin_payload(session_id="sess-1", tool_response=json.dumps(result)),
            sleep=_sleep_stub(),
            now=lambda: next(clock_values),
        )
        assert code == 0
        assert stderr == ""

    def test_claude_code_exit_stops_waiting(self, db, monkeypatch):
        result = _make_ask(session_id="sess-1")
        monkeypatch.setenv("CLAUDE_PID", "424242")

        def _raise_lookup(pid, sig):
            raise ProcessLookupError()

        monkeypatch.setattr(hook.os, "kill", _raise_lookup)
        code, stderr = _run_hook(
            _stdin_payload(session_id="sess-1", tool_response=json.dumps(result)),
            sleep=_sleep_stub(),
            now=_fixed_clock(),
        )
        assert code == 0
        assert stderr == ""


class TestToolNameFiltering:
    def test_non_add_ask_calm_tool_does_not_wait(self, db):
        payload = _stdin_payload(
            session_id="sess-1",
            tool_name="mcp__plugin_calm_calm__get_asks",
            tool_response=json.dumps({"id": 1}),
        )
        sleep = _sleep_stub()
        code, _ = _run_hook(payload, sleep=sleep, now=_fixed_clock())
        assert code == 0
        assert sleep.calls == []

    def test_non_calm_tool_does_not_wait(self, db):
        payload = _stdin_payload(
            session_id="sess-1", tool_name="Bash", tool_response=json.dumps({"id": 1})
        )
        sleep = _sleep_stub()
        code, _ = _run_hook(payload, sleep=sleep, now=_fixed_clock())
        assert code == 0
        assert sleep.calls == []


class TestCallerFiltering:
    def test_subagent_call_does_not_wait(self, db):
        result = _make_ask(session_id="sess-1")
        payload = _stdin_payload(
            session_id="sess-1", tool_response=json.dumps(result), agent_type="general-purpose"
        )
        sleep = _sleep_stub()
        code, _ = _run_hook(payload, sleep=sleep, now=_fixed_clock())
        assert code == 0
        assert sleep.calls == []

    def test_unattended_session_does_not_wait(self, db, monkeypatch):
        result = _make_ask(session_id="sess-1")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ATTENDED", "0")
        payload = _stdin_payload(session_id="sess-1", tool_response=json.dumps(result))
        sleep = _sleep_stub()
        code, _ = _run_hook(payload, sleep=sleep, now=_fixed_clock())
        assert code == 0
        assert sleep.calls == []
