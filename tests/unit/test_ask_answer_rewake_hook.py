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
import time
from unittest.mock import patch

import pytest

from hooks import ask_answer_rewake_hook as hook
from hooks.hook_state import HookState
from src.services import ask_service
from src.services.activity_service import add_activity
from src.services.topic_service import add_topic


ADD_ASK_TOOL_NAME = "mcp__plugin_calm_calm__add_ask"


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch, disable_embedding):
    """CALM_DB_PATH/HOOK_STATE_DIR/CALM_ASK_NOTIFY_DIRを一時パスへ向ける。

    hook.main()はHOOK_STATE_DIRが設定されているとHookState.BASE_DIRを
    クラス属性として直接書き換える（monkeypatch経由ではない）ので、ここで
    元の値を退避・復元しておかないとテスト後もBASE_DIRがtmp_pathを指した
    まま他のテストファイルへ漏れる。
    """
    monkeypatch.setenv("HOOK_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CALM_ASK_NOTIFY_DIR", str(tmp_path / "notify"))
    monkeypatch.delenv("CLAUDE_PID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ATTENDED", raising=False)
    original_base_dir = HookState.BASE_DIR
    yield
    HookState.BASE_DIR = original_base_dir


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


def _stub_sleep_and_clock(*side_effects, start: float = 1000.0):
    """main()のsleep/now引数をペアで差し替える。

    呼び出し順にside_effects（引数無しcallable）を1つずつ実行する。
    side_effectsを使い切った後もsleepが呼ばれ続けたら（想定外の待機＝回帰に
    よるhang）、nowを上限超過へ進めてループを強制終了させる（固定時計 +
    無時間sleepの組み合わせのまま回帰が起きるとpytestが実際にhangし、CI側に
    timeout設定も無いため）。戻り値のsleep関数はcalls属性で呼び出し回数を
    確認できる。
    """
    clock = [start]
    calls: list[float] = []

    def _sleep(seconds):
        calls.append(seconds)
        idx = len(calls) - 1
        if idx < len(side_effects):
            side_effects[idx]()
        else:
            clock[0] = start + hook.WAIT_LIMIT_SECONDS + 1.0

    def _now():
        return clock[0]

    _sleep.calls = calls
    return _sleep, _now


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
        sleep, now = _stub_sleep_and_clock(_answer)
        code, stderr = _run_hook(payload, sleep=sleep, now=now)

        assert code == 2
        assert f"#{ask_id}" in stderr
        assert "status: answered" in stderr
        assert "status=null" in stderr
        assert marker not in stderr
        assert "q?" not in stderr
        # 1回目のsleep内の回答で即座に検知できたこと（想定より多く回って
        # いないこと）を確認する。
        assert len(sleep.calls) == 1

    def test_dismissed_wakes(self, db):
        result = _make_ask(session_id="sess-1")
        ask_id = result["id"]

        def _resolve():
            ask_service.answer_ask(ask_id, "answer body")
            ask_service.triage_ask(ask_id, "dismiss", dismiss_reason="not relevant anymore")

        payload = _stdin_payload(session_id="sess-1", tool_response=json.dumps(result))
        sleep, now = _stub_sleep_and_clock(_resolve)
        code, stderr = _run_hook(payload, sleep=sleep, now=now)

        assert code == 2
        assert "status: dismissed" in stderr
        assert len(sleep.calls) == 1

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
        sleep, now = _stub_sleep_and_clock(_resolve)
        code, stderr = _run_hook(payload, sleep=sleep, now=now)

        assert code == 2
        assert "status: promoted" in stderr
        assert len(sleep.calls) == 1


class TestDoesNotWake:
    def test_withdrawn_does_not_wake(self, db):
        result = _make_ask(session_id="sess-1")
        ask_id = result["id"]

        def _withdraw():
            ask_service.withdraw_ask(ask_id, "no longer needed")

        payload = _stdin_payload(session_id="sess-1", tool_response=json.dumps(result))
        sleep, now = _stub_sleep_and_clock(_withdraw)
        code, stderr = _run_hook(payload, sleep=sleep, now=now)

        assert code == 0
        assert stderr == ""
        # withdrawnを検知して1回のsleepで抜けたことを確認する。ここが2回以上
        # になっているなら、withdrawnをopen扱いする回帰などで上限到達待ちに
        # 落ちている（結果だけ見るとcode/stderrは同じになるため区別できない）。
        assert len(sleep.calls) == 1

    def test_unsubscribe_stops_waiting(self, db):
        result = _make_ask(session_id="sess-1")
        ask_id = result["id"]

        def _unsubscribe():
            ask_service.unsubscribe_ask(ask_id)

        payload = _stdin_payload(session_id="sess-1", tool_response=json.dumps(result))
        sleep, now = _stub_sleep_and_clock(_unsubscribe)
        code, stderr = _run_hook(payload, sleep=sleep, now=now)

        assert code == 0
        assert stderr == ""
        assert len(sleep.calls) == 1

    def test_notify_false_does_not_wait(self, db):
        result = _make_ask(session_id="sess-1", notify=False)
        payload = _stdin_payload(
            session_id="sess-1", notify=False, tool_response=json.dumps(result)
        )
        sleep, now = _stub_sleep_and_clock()
        code, stderr = _run_hook(payload, sleep=sleep, now=now)

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
        sleep, now = _stub_sleep_and_clock()
        code, stderr = _run_hook(payload, sleep=sleep, now=now)

        assert code == 0
        assert sleep.calls == []


class TestToolResponseShapes:
    def test_id_from_plain_json_string(self, db):
        result = _make_ask(session_id="sess-1")
        ask_id = result["id"]
        payload = _stdin_payload(session_id="sess-1", tool_response=json.dumps(result))
        sleep, now = _stub_sleep_and_clock(lambda: ask_service.answer_ask(ask_id, "answer body"))
        code, stderr = _run_hook(payload, sleep=sleep, now=now)
        # code==2かつask_idが一致することで、この形からidを正しく取り出せた
        # ことを確認する(単にexit 0でないことだけでは、別のidを誤って拾った
        # 場合も見逃してしまう)。
        assert code == 2
        assert f"#{ask_id}" in stderr
        assert len(sleep.calls) == 1

    def test_id_from_dict_content(self, db):
        result = _make_ask(session_id="sess-1")
        ask_id = result["id"]
        payload = _stdin_payload(
            session_id="sess-1", tool_response={"content": json.dumps(result)}
        )
        sleep, now = _stub_sleep_and_clock(lambda: ask_service.answer_ask(ask_id, "answer body"))
        code, stderr = _run_hook(payload, sleep=sleep, now=now)
        assert code == 2
        assert f"#{ask_id}" in stderr
        assert len(sleep.calls) == 1

    def test_error_only_does_not_wait(self, db):
        payload = _stdin_payload(
            session_id="sess-1", tool_response=json.dumps({"error": {"code": "VALIDATION_ERROR"}})
        )
        sleep, now = _stub_sleep_and_clock()
        code, stderr = _run_hook(payload, sleep=sleep, now=now)
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

        sleep, now = _stub_sleep_and_clock(_answer)
        code, stderr = _run_hook(payload, sleep=sleep, now=now)
        assert code == 2
        assert f"#{ask_id}" in stderr
        assert len(sleep.calls) == 1


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
            sleep, now = _stub_sleep_and_clock()
            code, stderr = _run_hook(payload, sleep=sleep, now=now)
            assert code == 0
            assert sleep.calls == []
        finally:
            lock_fh.close()

    def test_lock_held_throughout_wait(self, db, tmp_path):
        """待機ループの間ずっと同じロックを保持し続けることを確かめる。

        _acquire_lockが取得直後にflockを解放する実装（例えばwithブロックで
        すぐ閉じる書き方）に差し替えても、既存テストは「先に取られていたら
        待たない」方向しか見ていないため19件すべてpassしてしまう。ここでは
        待機中（sleepの副作用の中）に同じロックファイルへ別プロセスのふりを
        してflockを試み、取得できない（＝hookがまだ保持している）ことを
        外側のリストへ記録し、テスト本体でassertする。main()の中で例外を
        投げさせて確かめる形は、main()のexcept Exceptionに握りつぶされて
        しまうため使えない。
        """
        result = _make_ask(session_id="sess-1")
        ask_id = result["id"]
        lock_path = tmp_path / "state" / "ask_rewake" / f"sess-1_{ask_id}.lock"

        could_acquire: list[bool] = []

        def _probe_lock_then_answer():
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            probe_fh = open(lock_path, "a+")
            try:
                fcntl.flock(probe_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                could_acquire.append(False)
            else:
                could_acquire.append(True)
                fcntl.flock(probe_fh.fileno(), fcntl.LOCK_UN)
            finally:
                probe_fh.close()
            ask_service.answer_ask(ask_id, "answer body")

        payload = _stdin_payload(session_id="sess-1", tool_response=json.dumps(result))
        sleep, now = _stub_sleep_and_clock(_probe_lock_then_answer)
        code, stderr = _run_hook(payload, sleep=sleep, now=now)

        assert code == 2
        # 外部プロセスからの二重取得を試みて失敗した(=hook自身がまだ保持して
        # いた)ことを確認する。
        assert could_acquire == [False]


def _clock_start_then_expired(start: float = 0.0):
    """1回目はstartを、2回目以降は常にWAIT_LIMIT_SECONDSを超える値を返す。

    `iter([0.0, LIMIT+1])`のように値を2つだけ用意する形だと、回帰で3回目の
    now()呼び出しが発生した場合にStopIterationが送出される。それはmain()の
    `except Exception: return 0`にそのまま握りつぶされ、テストは
    code==0/stderr==""の期待通りの結果を誤った理由（例外の握り潰し）で
    得てしまい、pass/failで区別が付かない。2回目以降を上限超過に固定して
    おけば、この経路は起こらない。
    """
    calls = {"n": 0}

    def _now():
        calls["n"] += 1
        return start if calls["n"] == 1 else start + hook.WAIT_LIMIT_SECONDS + 1.0

    return _now


class TestWaitLimitAndFailures:
    def test_wait_limit_exceeded_gives_up_while_open(self, db):
        result = _make_ask(session_id="sess-1")
        sleep, _ = _stub_sleep_and_clock()
        code, stderr = _run_hook(
            _stdin_payload(session_id="sess-1", tool_response=json.dumps(result)),
            sleep=sleep,
            now=_clock_start_then_expired(),
        )
        assert code == 0
        assert stderr == ""
        assert sleep.calls == []
        row = _row(db, result["id"])
        assert row[0] == "open"

    def test_db_failure_does_not_wake(self, db, monkeypatch):
        result = _make_ask(session_id="sess-1")
        monkeypatch.setenv("CALM_DB_PATH", "/nonexistent/path/does-not-exist.db")
        sleep, _ = _stub_sleep_and_clock()
        code, stderr = _run_hook(
            _stdin_payload(session_id="sess-1", tool_response=json.dumps(result)),
            sleep=sleep,
            now=_clock_start_then_expired(),
        )
        assert code == 0
        assert stderr == ""
        assert sleep.calls == []

    def test_claude_code_exit_stops_waiting(self, db, monkeypatch):
        result = _make_ask(session_id="sess-1")
        monkeypatch.setenv("CLAUDE_PID", "424242")

        def _raise_lookup(pid, sig):
            raise ProcessLookupError()

        monkeypatch.setattr(hook.os, "kill", _raise_lookup)
        sleep, now = _stub_sleep_and_clock()
        code, stderr = _run_hook(
            _stdin_payload(session_id="sess-1", tool_response=json.dumps(result)),
            sleep=sleep,
            now=now,
        )
        assert code == 0
        assert stderr == ""
        # claude_pidの死亡はループ先頭・sleep前に判定されるので、sleepは
        # 一度も呼ばれない。ここで0回でないなら、その判定が抜けて通常の
        # DBポーリング経路に落ちている（結果のcode/stderrだけでは区別
        # できない）。
        assert sleep.calls == []


class TestToolNameFiltering:
    def test_non_add_ask_calm_tool_does_not_wait(self, db):
        payload = _stdin_payload(
            session_id="sess-1",
            tool_name="mcp__plugin_calm_calm__get_asks",
            tool_response=json.dumps({"id": 1}),
        )
        sleep, now = _stub_sleep_and_clock()
        code, _ = _run_hook(payload, sleep=sleep, now=now)
        assert code == 0
        assert sleep.calls == []

    def test_non_calm_tool_does_not_wait(self, db):
        payload = _stdin_payload(
            session_id="sess-1", tool_name="Bash", tool_response=json.dumps({"id": 1})
        )
        sleep, now = _stub_sleep_and_clock()
        code, _ = _run_hook(payload, sleep=sleep, now=now)
        assert code == 0
        assert sleep.calls == []


class TestStaleLockCleanup:
    def test_stale_lock_removed_fresh_and_own_locks_kept(self, db, tmp_path):
        lock_dir = tmp_path / "state" / "ask_rewake"
        lock_dir.mkdir(parents=True)

        stale_path = lock_dir / "sess-old_999.lock"
        stale_path.write_text("")
        stale_mtime = time.time() - hook.STALE_LOCK_AGE_SECONDS - 3600
        os.utime(stale_path, (stale_mtime, stale_mtime))

        fresh_path = lock_dir / "sess-fresh_1.lock"
        fresh_path.write_text("")

        result = _make_ask(session_id="sess-1")
        ask_id = result["id"]
        payload = _stdin_payload(session_id="sess-1", tool_response=json.dumps(result))
        sleep, now = _stub_sleep_and_clock(lambda: ask_service.answer_ask(ask_id, "answer body"))
        code, stderr = _run_hook(payload, sleep=sleep, now=now)

        assert code == 2
        assert f"#{ask_id}" in stderr
        assert not stale_path.exists()
        assert fresh_path.exists()
        assert (lock_dir / f"sess-1_{ask_id}.lock").exists()

    def test_reacquiring_existing_lock_refreshes_mtime(self, db, tmp_path):
        """openだけではmtimeが動かないため、取得時に明示的に更新している
        ことを確かめる。更新していないと、同じ(session_id, ask_id)の
        ロックファイルを長時間保持し続けた場合に「古い」と誤判定され、
        まだ生きているのに掃除で消される恐れがある。"""
        result = _make_ask(session_id="sess-1")
        ask_id = result["id"]
        lock_dir = tmp_path / "state" / "ask_rewake"
        lock_dir.mkdir(parents=True)
        own_lock_path = lock_dir / f"sess-1_{ask_id}.lock"
        own_lock_path.write_text("")
        # STALE_LOCK_AGE_SECONDS未満（sweepでは消えない年齢）だが十分古い
        old_mtime = time.time() - hook.STALE_LOCK_AGE_SECONDS + 3600
        os.utime(own_lock_path, (old_mtime, old_mtime))

        payload = _stdin_payload(session_id="sess-1", tool_response=json.dumps(result))
        sleep, now = _stub_sleep_and_clock(lambda: ask_service.answer_ask(ask_id, "answer body"))
        code, stderr = _run_hook(payload, sleep=sleep, now=now)

        assert code == 2
        assert own_lock_path.stat().st_mtime > old_mtime + 3000


class TestCallerFiltering:
    def test_subagent_call_does_not_wait(self, db):
        """agent_type（サブエージェント発の呼び出しに実機で確認済みの値。
        ask_answer_rewake_hook.pyのモジュールdocstring参照）で判定する。"""
        result = _make_ask(session_id="sess-1")
        payload = _stdin_payload(
            session_id="sess-1", tool_response=json.dumps(result), agent_type="scout"
        )
        sleep, now = _stub_sleep_and_clock()
        code, _ = _run_hook(payload, sleep=sleep, now=now)
        assert code == 0
        assert sleep.calls == []

    def test_unattended_session_does_not_wait(self, db, monkeypatch):
        result = _make_ask(session_id="sess-1")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ATTENDED", "0")
        payload = _stdin_payload(session_id="sess-1", tool_response=json.dumps(result))
        sleep, now = _stub_sleep_and_clock()
        code, _ = _run_hook(payload, sleep=sleep, now=now)
        assert code == 0
        assert sleep.calls == []
