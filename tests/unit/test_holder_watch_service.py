"""担い手欄の見張り（停止の知らせ）と、担い手の交代時の警告のテスト。"""
import json
import os
import time

import pytest

from src.db import get_connection
from src.infra.cli_session import sessions_dir
from src.services.activity_service import add_activity, update_activity
from src.services.holder_watch_service import (
    PROJECTS_DIR_ENV,
    check_once,
    holder_session_id,
)

OLD_SID = "11111111-1111-1111-1111-111111111111"
NEW_SID = "22222222-2222-2222-2222-222222222222"
OTHER_SID = "33333333-3333-3333-3333-333333333333"


def _desc(sid: str | None, job: str | None = None) -> str:
    holder = f"担い手: holder-x（sessionId {sid}）／2026-10-09 03:10" if sid else "担い手: 空席／2026-10-09"
    cron = f"仕込み: 起こし直し（CronCreate、job {job}）\n" if job else ""
    return f"## 状態\n{holder}\n{cron}常駐: 相談役 / sessionId {OTHER_SID}\n"


def _write_session(sid: str, pid: int, status: str = "idle") -> None:
    d = sessions_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{pid}.json").write_text(json.dumps(
        {"pid": pid, "sessionId": sid, "name": "holder-x", "status": status, "startedAt": 1}
    ))


def _write_transcript(tmp_path, sid: str, age_sec: float, subagent: bool = False) -> None:
    name = f"{sid}/subagents/agent-x.jsonl" if subagent else f"{sid}.jsonl"
    p = tmp_path / "projects" / "-proj" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}\n")
    t = time.time() - age_sec
    os.utime(p, (t, t))


# OSのpid上限（macOS 99998、Linux既定4194304）を超える値は生きたプロセスを指さない
DEAD_PID = 99_999_999


@pytest.fixture
def projects(tmp_path, monkeypatch):
    monkeypatch.setenv(PROJECTS_DIR_ENV, str(tmp_path / "projects"))
    return tmp_path


def _orch(desc: str, tags=("domain:calm", "intent:discuss", "orch")) -> int:
    return add_activity(title="ダミーorch", description=desc, tags=list(tags), check_in=False)["activity_id"]


def _rows(sql: str, args=()) -> list:
    conn = get_connection()
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def test_holder_session_id_reads_only_the_holder_line():
    assert holder_session_id(_desc(OLD_SID)) == OLD_SID
    assert holder_session_id(_desc(None)) is None
    assert holder_session_id("担い手欄なし\n常駐 sessionId " + OTHER_SID) is None


class TestHandoffWarnings:
    def test_busy_old_holder_and_no_new_job_both_warn_but_write_goes_through(self, temp_db):
        aid = _orch(_desc(OLD_SID, job="81f254da"))
        _write_session(OLD_SID, os.getpid(), status="busy")

        result = update_activity(aid, description=_desc(NEW_SID, job="81f254da"))

        warnings = result["holder_warnings"]
        assert len(warnings) == 2
        assert OLD_SID in warnings[0] and "busy" in warnings[0]
        assert "job id" in warnings[1]
        stored = _rows("SELECT description FROM activities WHERE id = ?", (aid,))[0][0]
        assert NEW_SID in stored

    def test_idle_old_holder_and_new_job_id_give_no_warning(self, temp_db):
        aid = _orch(_desc(OLD_SID, job="81f254da"))
        _write_session(OLD_SID, os.getpid(), status="idle")

        result = update_activity(aid, description=_desc(NEW_SID, job="a1b2c3d4"))

        assert "holder_warnings" not in result

    def test_idle_old_holder_with_running_subagent_warns(self, temp_db, projects):
        aid = _orch(_desc(OLD_SID))
        _write_session(OLD_SID, os.getpid(), status="idle")
        _write_transcript(projects, OLD_SID, age_sec=30, subagent=True)

        result = update_activity(aid, description=_desc(NEW_SID, job="a1b2c3d4"))

        assert len(result["holder_warnings"]) == 1
        assert "裏のSA" in result["holder_warnings"][0]

    def test_dead_old_holder_is_not_busy(self, temp_db):
        aid = _orch(_desc(OLD_SID))
        _write_session(OLD_SID, DEAD_PID, status="busy")

        result = update_activity(aid, description=_desc(NEW_SID, job="a1b2c3d4"))

        assert "holder_warnings" not in result

    def test_vacating_or_keeping_the_holder_does_not_warn(self, temp_db):
        aid = _orch(_desc(OLD_SID))
        _write_session(OLD_SID, os.getpid(), status="busy")

        assert "holder_warnings" not in update_activity(aid, description=_desc(OLD_SID) + "追記")
        assert "holder_warnings" not in update_activity(aid, description=_desc(None))


class TestCheckOnce:
    def test_holder_seen_alive_then_dead_fires_one_ask_and_one_signal_only_once(
            self, temp_db, projects):
        aid = _orch(_desc(OLD_SID))
        seen = set()
        _write_session(OLD_SID, os.getpid())
        _write_transcript(projects, OLD_SID, age_sec=30)
        assert check_once(dead_sec=600, stale_sec=3600, seen_alive=seen) == []

        (sessions_dir() / f"{os.getpid()}.json").unlink()
        _write_session(OLD_SID, DEAD_PID)
        _write_transcript(projects, OLD_SID, age_sec=700)
        fired = check_once(dead_sec=600, stale_sec=3600, seen_alive=seen)

        assert [(f["activity_id"], f["session_id"]) for f in fired] == [(aid, OLD_SID)]
        assert "プロセスが無い" in fired[0]["reason"]
        asks = _rows("SELECT a.id, a.status, a.question FROM asks a "
                     "JOIN ask_blocks b ON b.ask_id = a.id WHERE b.activity_id = ?", (aid,))
        assert len(asks) == 1 and asks[0]["status"] == "open"
        assert "ダミーorch" in asks[0]["question"] and "holder-x" in asks[0]["question"]
        signals = _rows("SELECT kind, summary FROM signal_events WHERE kind = 'custom:holder-down'")
        assert len(signals) == 1 and OLD_SID in signals[0]["summary"]

        # 人がaskを片付けた後でも、同じ担い手について2回目は立てない
        conn = get_connection()
        conn.execute("UPDATE asks SET status = 'withdrawn', withdrawn_at = CURRENT_TIMESTAMP")
        conn.execute("UPDATE signal_events SET status = 'dismissed'")
        conn.commit()
        conn.close()
        assert check_once(dead_sec=600, stale_sec=3600, seen_alive=seen) == []

    def test_holder_already_dead_when_first_seen_is_not_reported(self, temp_db, projects):
        _orch(_desc(OLD_SID))
        _write_session(OLD_SID, DEAD_PID)
        _write_transcript(projects, OLD_SID, age_sec=700)

        assert check_once(dead_sec=600, stale_sec=3600, seen_alive=set()) == []

    def test_recently_dead_holder_waits_for_the_grace(self, temp_db, projects):
        aid = _orch(_desc(OLD_SID))
        _write_session(OLD_SID, DEAD_PID)
        _write_transcript(projects, OLD_SID, age_sec=60)

        assert check_once(dead_sec=600, stale_sec=3600, seen_alive={(aid, OLD_SID)}) == []

    def test_live_holder_with_stale_transcript_fires_even_when_idle(self, temp_db, projects):
        aid = _orch(_desc(OLD_SID))
        _write_session(OLD_SID, os.getpid(), status="idle")
        _write_transcript(projects, OLD_SID, age_sec=4000)

        fired = check_once(dead_sec=600, stale_sec=3600, seen_alive=set())

        assert [f["activity_id"] for f in fired] == [aid]
        assert "更新されていない" in fired[0]["reason"]

    def test_running_subagent_keeps_an_idle_holder_from_looking_stuck(self, temp_db, projects):
        _orch(_desc(OLD_SID))
        _write_session(OLD_SID, os.getpid(), status="idle")
        _write_transcript(projects, OLD_SID, age_sec=4000)
        _write_transcript(projects, OLD_SID, age_sec=60, subagent=True)

        assert check_once(dead_sec=600, stale_sec=3600, seen_alive=set()) == []

    def test_live_fresh_holder_vacant_and_non_orch_are_left_alone(self, temp_db, projects):
        _orch(_desc(OLD_SID))
        _write_session(OLD_SID, os.getpid())
        _write_transcript(projects, OLD_SID, age_sec=30)
        _orch(_desc(None))
        _orch(_desc(NEW_SID), tags=("domain:calm", "intent:discuss", "orchestration"))
        _write_session(NEW_SID, DEAD_PID)

        assert check_once(dead_sec=600, stale_sec=3600, seen_alive=set()) == []
