"""hooks/recorder_watch.py の単体テスト。

DBはtopic候補の取得だけが対象で、実SQLite（temp_db、全migration適用）と
実サービス関数（add_activity/add_topic/add_relation）で作る。それ以外の
状態（run.json・cursor.json・片ファイル・transcript）はすべて実際に
書かれうる形のファイルとして用意する。目印ファイルは`write_marker`で作る。

外部境界としてmonkeypatchするのは次の3つだけ: `hook.process_start_signature`
（メインの生死判定）・`subprocess.run`（ps・tmux呼び出し。psは`write_marker`
経由でも呼ばれるため、コマンド種別で振り分けて両方を成立させる）・
main()に注入する`sleep`/`now`。
"""
import fcntl
import io
import json
import subprocess
from unittest.mock import patch

import pytest

from hooks import recorder_watch as hook
from hooks.hook_state import HookState
from hooks.recorder_marker import marker_path, write_marker
from src.harness.claude_code import ClaudeCodeHarness

_FAKE_PS_STARTED_AT = "Thu Jul 24 09:32:04 2026"
_MAIN_PID = 999


# --- 隔離・外部境界のfixture ---


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    monkeypatch.setattr(HookState, "BASE_DIR", tmp_path / "state")
    monkeypatch.delenv("CLAUDE_PID", raising=False)
    # topic候補取得が実DBに触れないよう、明示的にdbフィクスチャを使わない
    # テストでは存在しないパスへ向け、接続失敗（取得失敗）に倒す。
    monkeypatch.setenv("CALM_DB_PATH", str(tmp_path / "no-such-db.sqlite"))


@pytest.fixture(autouse=True)
def _mock_subprocess(monkeypatch):
    """subprocess.runをコマンド種別で振り分ける。

    psはwrite_marker/process_start_signature、tmuxは見張りの終了処理が呼ぶ。
    同じsubprocessモジュールを両経路が参照するため、1つのfixtureで両方
    面倒を見る（別々にmonkeypatchすると片方が片方を上書きしてしまう）。
    """
    tmux_calls: list[list[str]] = []

    def _fake_run(cmd, **kwargs):
        if cmd and cmd[0] == "tmux":
            tmux_calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)
        if cmd and cmd[0] == "ps":
            return subprocess.CompletedProcess(cmd, 0, stdout=_FAKE_PS_STARTED_AT + "\n", stderr="")
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    return tmux_calls


def _mock_main_alive(monkeypatch, started_at: str = _FAKE_PS_STARTED_AT) -> None:
    monkeypatch.setattr(hook, "process_start_signature", lambda pid: started_at)


def _mock_main_dead(monkeypatch) -> None:
    monkeypatch.setattr(hook, "process_start_signature", lambda pid: None)


# --- transcript/run_dirの組み立てヘルパー ---


def _entry(kind: str, uuid: str, *, text: str = "", tool_calls=None, tool_inputs=None, is_meta=False) -> dict:
    content = []
    if text:
        content.append({"type": "text", "text": text})
    if tool_calls:
        for i, tool in enumerate(tool_calls):
            inp = tool_inputs[i] if tool_inputs and i < len(tool_inputs) else {}
            content.append({"type": "tool_use", "name": tool, "input": inp, "id": f"tu-{uuid}-{i}"})
    raw = {"type": kind, "uuid": uuid, "message": {"content": content}}
    if is_meta:
        raw["isMeta"] = True
    return raw


def _write_jsonl(path, entries: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def _append_jsonl(path, entries: list[dict]) -> None:
    with open(path, "a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def _setup_run(tmp_path, main_sid: str = "main-1", main_pid: int = _MAIN_PID, transcript_lines=None):
    run_dir = hook.run_dir_for(main_sid)
    run_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = tmp_path / "main_transcript.jsonl"
    _write_jsonl(transcript_path, transcript_lines or [])
    run_data = {
        "main_sid": main_sid,
        "main_pid": main_pid,
        "main_pid_started_at": _FAKE_PS_STARTED_AT,
        "main_transcript": str(transcript_path),
    }
    (run_dir / "run.json").write_text(json.dumps(run_data), encoding="utf-8")
    return run_dir, transcript_path


def _stub_sleep_and_clock(*side_effects, start: float = 1000.0):
    """main()のsleep/now引数をペアで差し替える。

    side_effectsを使い切った後もsleepが呼ばれ続けたら、時計を上限超過へ
    進めてループを終わらせる（想定外の回帰でhangしないようにするため）。
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


def _run_hook(*, cwd, last_assistant_message: str = "", sleep=None, now=None) -> tuple[int, str]:
    payload = {
        "session_id": "recorder-sess",
        "cwd": str(cwd),
        "transcript_path": "/irrelevant.jsonl",
        "last_assistant_message": last_assistant_message,
        "hook_event_name": "Stop",
    }
    fake_stdin = io.StringIO(json.dumps(payload))
    fake_stderr = io.StringIO()
    kwargs = {}
    if sleep is not None:
        kwargs["sleep"] = sleep
    if now is not None:
        kwargs["now"] = now
    with patch.object(hook.sys, "stdin", fake_stdin), patch.object(hook.sys, "stderr", fake_stderr):
        code = hook.main(**kwargs)
    return code, fake_stderr.getvalue()


def _cursor(run_dir) -> dict:
    return json.loads((run_dir / "cursor.json").read_text(encoding="utf-8"))


# --- テスト ---


class TestNoOpEntryPoints:
    def test_no_run_json_exits_zero_without_sleeping(self, tmp_path):
        stray_dir = tmp_path / "not-a-run-dir"
        stray_dir.mkdir()
        sleep, now = _stub_sleep_and_clock()
        code, stderr = _run_hook(cwd=stray_dir, sleep=sleep, now=now)
        assert code == 0
        assert stderr == ""
        assert sleep.calls == []

    def test_lock_already_held_exits_zero_without_sleeping(self, tmp_path):
        run_dir, _ = _setup_run(tmp_path)
        lock_fh = open(run_dir / "watch.lock", "a+")
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            sleep, now = _stub_sleep_and_clock()
            code, stderr = _run_hook(cwd=run_dir, sleep=sleep, now=now)
            assert code == 0
            assert sleep.calls == []
        finally:
            lock_fh.close()

    def test_unexpected_exception_exits_zero(self, tmp_path, monkeypatch):
        run_dir, _ = _setup_run(tmp_path, transcript_lines=[_entry("assistant", "u1", text="hi")])
        _mock_main_alive(monkeypatch)

        def _boom(seconds):
            raise RuntimeError("boom")

        code, stderr = _run_hook(cwd=run_dir, sleep=_boom, now=lambda: 1000.0)
        assert code == 0


class TestThresholdAndChunking:
    def test_below_threshold_waits_then_emits_on_crossing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hook, "CHAR_THRESHOLD", 30)
        run_dir, transcript = _setup_run(
            tmp_path, transcript_lines=[_entry("assistant", "u1", text="short")]
        )
        _mock_main_alive(monkeypatch)

        def _append_more():
            _append_jsonl(transcript, [_entry("assistant", "u2", text="x" * 40)])

        sleep, now = _stub_sleep_and_clock(_append_more)
        code, stderr = _run_hook(cwd=run_dir, sleep=sleep, now=now)

        assert code == 2
        assert len(sleep.calls) == 1
        assert "片0001" in stderr
        assert "DONE 0001" in stderr

        chunk = (run_dir / "chunks" / "0001.md").read_text(encoding="utf-8")
        assert "x" * 40 in chunk
        assert "short" in chunk

        cursor = _cursor(run_dir)
        assert cursor["pending"]["no"] == 1
        assert cursor["last_uuid"] is None  # DONE確認までは未確定

        # DONEを確認 → cursorが確定する
        sleep2, now2 = _stub_sleep_and_clock()
        code2, _ = _run_hook(cwd=run_dir, last_assistant_message="やりました\nDONE 0001", sleep=sleep2, now=now2)
        assert code2 == 2  # 締め切り手前no-opのexit 2（新規コンテンツが無いため）
        cursor2 = _cursor(run_dir)
        assert cursor2["pending"] is None
        assert cursor2["last_uuid"] == "u2"
        assert cursor2["unacked"] == []

    def test_activity_boundary_cuts_before_checkin(self, tmp_path, monkeypatch):
        entries = [
            _entry("assistant", "e1", text="doing thing A"),
            _entry("assistant", "e2", text="more of thing A"),
            _entry(
                "assistant", "e3",
                tool_calls=["mcp__plugin_calm_calm__check_in"],
                tool_inputs=[{"activity_id": 99}],
            ),
            _entry("assistant", "e4", text="doing thing B"),
        ]
        run_dir, transcript = _setup_run(tmp_path, transcript_lines=entries)
        _mock_main_alive(monkeypatch)

        sleep, now = _stub_sleep_and_clock()
        code, stderr = _run_hook(cwd=run_dir, sleep=sleep, now=now)

        assert code == 2
        chunk1 = (run_dir / "chunks" / "0001.md").read_text(encoding="utf-8")
        assert "thing A" in chunk1
        assert "thing B" not in chunk1
        assert "activity_id: (未設定)" in chunk1

        cursor = _cursor(run_dir)
        assert cursor["pending"]["end_uuid"] == "e2"

        # DONE確認 → e3(check_in)以降が次の片に含まれ、activity_idが反映される
        _mock_main_dead(monkeypatch)  # 残りを即座に渡させて2本目の片を確認する
        sleep2, now2 = _stub_sleep_and_clock()
        code2, _ = _run_hook(cwd=run_dir, last_assistant_message="DONE 0001", sleep=sleep2, now=now2)
        assert code2 == 2
        chunk2 = (run_dir / "chunks" / "0002.md").read_text(encoding="utf-8")
        assert "thing B" in chunk2
        assert "activity_id: 99" in chunk2
        cursor2 = _cursor(run_dir)
        assert cursor2["last_uuid"] == "e2"
        assert cursor2["pending"]["no"] == 2
        assert cursor2["pending"]["end_activity_id"] == 99


class TestDoneRetryAndUnacked:
    def test_no_done_retries_once_then_unacked_and_cursor_advances(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hook, "CHAR_THRESHOLD", 3)
        run_dir, transcript = _setup_run(
            tmp_path, transcript_lines=[_entry("assistant", "e1", text="hello world")]
        )
        _mock_main_alive(monkeypatch)

        sleep, now = _stub_sleep_and_clock()
        code, _ = _run_hook(cwd=run_dir, sleep=sleep, now=now)
        assert code == 2
        cursor = _cursor(run_dir)
        assert cursor["pending"]["retries"] == 0

        # 1回目: DONEなし → 同じ片で即座に起こし直す（ポーリングを挟まない）
        sleep2, now2 = _stub_sleep_and_clock()
        code2, stderr2 = _run_hook(cwd=run_dir, last_assistant_message="まだです", sleep=sleep2, now=now2)
        assert code2 == 2
        assert "片0001" in stderr2
        assert sleep2.calls == []
        cursor2 = _cursor(run_dir)
        assert cursor2["pending"]["retries"] == 1
        assert cursor2["unacked"] == []

        # 2回目もDONEなし → unackedへ。cursorは進み、残りが無ければ終了する
        _mock_main_dead(monkeypatch)
        sleep3, now3 = _stub_sleep_and_clock()
        code3, _ = _run_hook(cwd=run_dir, last_assistant_message="まだです2", sleep=sleep3, now=now3)
        assert code3 == 0
        cursor3 = _cursor(run_dir)
        assert cursor3["pending"] is None
        assert cursor3["unacked"] == [1]
        assert cursor3["last_uuid"] == "e1"


class TestMainDeath:
    def test_flushes_remaining_then_removes_marker_and_kills_tmux(self, tmp_path, monkeypatch, _mock_subprocess):
        main_sid = "main-death"
        run_dir, transcript = _setup_run(
            tmp_path, main_sid=main_sid, transcript_lines=[_entry("assistant", "e1", text="leftover")]
        )
        write_marker(main_sid, _MAIN_PID)
        assert marker_path(main_sid).exists()

        _mock_main_dead(monkeypatch)
        sleep, now = _stub_sleep_and_clock()
        code, _ = _run_hook(cwd=run_dir, sleep=sleep, now=now)
        assert code == 2  # 残っている内容を最後の片として渡す

        sleep2, now2 = _stub_sleep_and_clock()
        code2, _ = _run_hook(cwd=run_dir, last_assistant_message="DONE 0001", sleep=sleep2, now=now2)
        assert code2 == 0
        assert not marker_path(main_sid).exists()
        assert _mock_subprocess == [["tmux", "kill-session", "-t", f"calm-rec-{main_sid[:8]}"]]


class TestClearSessionIsolation:
    def test_clear_session_does_not_touch_cursor(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hook, "CHAR_THRESHOLD", 1)
        main_sid = "main-clear"
        run_dir, _ = _setup_run(
            tmp_path, main_sid=main_sid, transcript_lines=[_entry("assistant", "e1", text="hi")]
        )
        _mock_main_alive(monkeypatch)
        sleep, now = _stub_sleep_and_clock()
        _run_hook(cwd=run_dir, sleep=sleep, now=now)
        before = (run_dir / "cursor.json").read_text(encoding="utf-8")
        assert before

        HookState.clear_session(main_sid)

        assert (run_dir / "cursor.json").read_text(encoding="utf-8") == before


class TestOffsetValidity:
    def test_rejects_offset_that_does_not_land_right_after_a_newline(self, tmp_path):
        """`_offset_looks_valid`単体で、改行の直前判定そのものを確かめる。

        1バイトずれただけでは、ずれた先の断片がたまたまJSONとして読めて
        しまう可能性がある（この2行の内容ではそうならないが、JSON自体の
        parseability に頼ると見逃しうる）。改行の直前バイトを直接見る
        判定を、JSONの読めなさとは独立に確かめる。
        """
        transcript = tmp_path / "t.jsonl"
        e1 = _entry("assistant", "u1", text="ab")
        e2 = _entry("assistant", "u2", text="cd")
        _write_jsonl(transcript, [e1, e2])
        line1_len = len(json.dumps(e1, ensure_ascii=False).encode("utf-8"))
        valid_offset = line1_len + 1  # e1の改行の直後(e2の先頭)

        assert hook._offset_looks_valid(transcript, valid_offset) is True
        assert hook._offset_looks_valid(transcript, valid_offset - 1) is False


class TestBackfillRecovery:
    def test_recovers_position_after_earlier_line_lengthens(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hook, "CHAR_THRESHOLD", 3)
        e1 = _entry("assistant", "u1", text="first")
        run_dir, transcript = _setup_run(tmp_path, transcript_lines=[e1])
        _mock_main_alive(monkeypatch)

        # 閾値がすぐ超えるので、e1だけの片がまず切られる
        sleep, now = _stub_sleep_and_clock()
        code, _ = _run_hook(cwd=run_dir, sleep=sleep, now=now)
        assert code == 2

        # DONE確認でcursorをu1直後に確定させたあと、ポーリング1周目の副作用として
        # backfill相当(e1の行を長くする)とe2の追記を同時に行う。cursorのbyte_offset
        # はe1直後を指したままなので、2周目の読みで復旧経路を通ることになる。
        def _backfill_and_append():
            harness = ClaudeCodeHarness()
            entries = harness.read_transcript_entries(str(transcript))
            target = entries[0]
            target.raw["message"]["content"][0]["text"] = "first " + "x" * 500
            assert harness.rewrite_transcript_entry(str(transcript), target) is True
            _append_jsonl(transcript, [_entry("assistant", "u2", text="second")])

        sleep2, now2 = _stub_sleep_and_clock(_backfill_and_append)
        code2, _ = _run_hook(cwd=run_dir, last_assistant_message="DONE 0001", sleep=sleep2, now=now2)

        assert code2 == 2
        cursor = _cursor(run_dir)
        assert cursor["offset_lost"] == 0  # last_uuidから復旧できた(取りこぼしていない)
        assert cursor["last_uuid"] == "u1"  # chunk1の確定はそのまま保たれている

        chunk2 = (run_dir / "chunks" / "0002.md").read_text(encoding="utf-8")
        assert "second" in chunk2
        assert "first" not in chunk2  # e1が二重に含まれていない


class TestIncompleteTrailingLine:
    def test_not_included_until_newline_completes_it(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hook, "CHAR_THRESHOLD", 1)
        run_dir, transcript = _setup_run(
            tmp_path, transcript_lines=[_entry("assistant", "u1", text="full line")]
        )
        complete_size = transcript.stat().st_size
        with open(transcript, "a", encoding="utf-8") as f:
            f.write(json.dumps(_entry("assistant", "u2", text="incomplete"), ensure_ascii=False))
            # 改行を書かない(書きかけ)

        _mock_main_alive(monkeypatch)
        sleep, now = _stub_sleep_and_clock()
        code, _ = _run_hook(cwd=run_dir, sleep=sleep, now=now)

        assert code == 2
        chunk = (run_dir / "chunks" / "0001.md").read_text(encoding="utf-8")
        assert "full line" in chunk
        assert "incomplete" not in chunk
        cursor = _cursor(run_dir)
        assert cursor["pending"]["end_offset"] == complete_size

        # 改行を足して完成させる → DONE確認後、次の片に含まれる
        with open(transcript, "a", encoding="utf-8") as f:
            f.write("\n")
        sleep2, now2 = _stub_sleep_and_clock()
        _mock_main_dead(monkeypatch)
        code2, _ = _run_hook(cwd=run_dir, last_assistant_message="DONE 0001", sleep=sleep2, now=now2)
        assert code2 == 2
        chunk2 = (run_dir / "chunks" / "0002.md").read_text(encoding="utf-8")
        assert "incomplete" in chunk2


class TestNoOpNearDeadline:
    def test_emits_noop_and_exits_2_without_pending(self, tmp_path, monkeypatch):
        run_dir, _ = _setup_run(tmp_path, transcript_lines=[])
        _mock_main_alive(monkeypatch)
        sleep, now = _stub_sleep_and_clock()
        code, stderr = _run_hook(cwd=run_dir, sleep=sleep, now=now)
        assert code == 2
        assert "DONE -" in stderr
        assert len(sleep.calls) == 1
        # 何も新規コンテンツが無いno-opでは、cursor.jsonへの書き込みは
        # そもそも発生しない(状態が変わっていないため)。次回起動時は
        # 既定値から始まり、pendingは無い状態のままになる。
        assert not (run_dir / "cursor.json").exists()


class TestTopicCandidates:
    def test_real_relation_is_written_to_header(self, tmp_path, monkeypatch, temp_db, disable_embedding):
        monkeypatch.setenv("CALM_DB_PATH", temp_db)
        from src.services.activity_service import add_activity
        from src.services.relation_service import add_relation
        from src.services.topic_service import add_topic

        topic_id = add_topic(title="t1", description="d", tags=["domain:test"])["topic_id"]
        activity_id = add_activity(
            title="a1", description="d", tags=["domain:test"], check_in=False
        )["activity_id"]
        add_relation("activity", activity_id, [{"type": "topic", "ids": [topic_id]}])

        entries = [
            _entry(
                "assistant", "u1",
                tool_calls=["mcp__plugin_calm_calm__check_in"],
                tool_inputs=[{"activity_id": activity_id}],
            ),
            _entry("assistant", "u2", text="content after check-in"),
        ]
        run_dir, _ = _setup_run(tmp_path, transcript_lines=entries)
        _mock_main_alive(monkeypatch)
        _mock_main_dead(monkeypatch)

        sleep, now = _stub_sleep_and_clock()
        code, _ = _run_hook(cwd=run_dir, sleep=sleep, now=now)
        assert code == 2
        chunk = (run_dir / "chunks" / "0001.md").read_text(encoding="utf-8")
        assert f"activity_id: {activity_id}" in chunk
        assert f"#{topic_id} t1" in chunk

    def test_db_connection_failure_is_distinguished_from_zero_results(self, tmp_path, monkeypatch):
        # _isolate_stateが既に存在しないCALM_DB_PATHへ向けている。
        entries = [
            _entry(
                "assistant", "u1",
                tool_calls=["mcp__plugin_calm_calm__check_in"],
                tool_inputs=[{"activity_id": 5}],
            ),
            _entry("assistant", "u2", text="x"),
        ]
        run_dir, _ = _setup_run(tmp_path, transcript_lines=entries)
        _mock_main_alive(monkeypatch)
        _mock_main_dead(monkeypatch)

        sleep, now = _stub_sleep_and_clock()
        code, _ = _run_hook(cwd=run_dir, sleep=sleep, now=now)
        assert code == 2
        chunk = (run_dir / "chunks" / "0001.md").read_text(encoding="utf-8")
        assert "取得失敗" in chunk
