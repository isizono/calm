"""src/services/ow/reducer.ow_apply_state_with_conn のユニットテスト。

relay history を消化して ow_workers / ow_channels / ow_applied_msg_ids に書き込む純粋
reducer の挙動を検証する。CQS分離原則のため、activities テーブルには書き込まない
ことも確認する。
"""
import json
import os
import sqlite3
import tempfile

import pytest

from src.db import get_connection, init_database
from src.services.ow import applied_msgs as am
from src.services.ow import channels as ch
from src.services.ow import workers as wk
from src.services.ow.reducer import ow_apply_state_with_conn


NOW = "2026-06-17T10:00:00Z"
CH = "C1"


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


def _setup_channel(conn, topic_id):
    ch.upsert_channel_with_conn(
        conn,
        channel_code=CH,
        topic_id=topic_id,
        orch_handle="orch",
        now=NOW,
    )


def _make_topic(conn) -> int:
    cur = conn.execute(
        "INSERT INTO discussion_topics (title, description) VALUES (?, ?)",
        ("t", "d"),
    )
    return cur.lastrowid


def _make_activity(conn, title: str = "a") -> int:
    cur = conn.execute(
        "INSERT INTO activities (title, description, status) VALUES (?, ?, ?)",
        (title, "", "pending"),
    )
    return cur.lastrowid


def _msg(msg_id, handle, body, created_at=NOW):
    return {"msg_id": msg_id, "handle": handle, "body": body, "created_at": created_at}


def _ev_identity(handle, *, activity_id, topic_id, session_id=None, alias=None,
                  model="claude-opus-4-7", cwd="/tmp", terminated_at=None):
    data = {
        "type": "identity", "role": "worker", "handle": handle,
        "alias": alias or handle, "activity_id": activity_id,
        "topic_id": topic_id, "model": model, "cwd": cwd,
        "started_at": NOW,
    }
    if session_id is not None:
        data["session_id"] = session_id
    if terminated_at is not None:
        data["terminated_at"] = terminated_at
    return {"v": 1, "kind": "event", "from": handle, "to": "*",
            "task": "T1", "data": data}


def _ev_state(handle, state, *, cause=None, session_id=None):
    data = {"type": "state", "state": state}
    if cause:
        data["cause"] = cause
    if session_id:
        data["session_id"] = session_id
    return {"v": 1, "kind": "event", "from": handle, "to": "orch",
            "task": "T1", "data": data}


def _ev_heartbeat(handle):
    return {
        "v": 1, "kind": "event", "from": handle, "to": "*", "task": "T1",
        "data": {"type": "heartbeat", "phase": "alive"},
    }


def _cmd_assign(handle, *, activity_id, topic_id, model="claude-opus-4-7",
                 cwd="/tmp", permission_mode="auto", timeout_min=240):
    data = {
        "type": "assign", "title": "task", "activity_id": activity_id,
        "topic_id": str(topic_id), "model": model, "cwd": cwd,
        "permission_mode": permission_mode, "timeout_min": timeout_min,
    }
    return {"v": 1, "kind": "command", "from": "orch", "to": handle,
            "task": "T1", "data": data}


class TestEmptyHistory:
    def test_empty_history_returns_zero_counters(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            _setup_channel(conn, tid)
            r = ow_apply_state_with_conn(
                conn, channel_code=CH, topic_id=tid, history=[], now=NOW,
            )
            assert r == {
                "applied": 0, "skipped": 0, "duplicate": 0, "last_msg_id": 0,
            }
        finally:
            conn.close()


class TestAssignCreatesWorker:
    def test_assign_inserts_worker_with_metadata(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            aid = _make_activity(conn)
            _setup_channel(conn, tid)
            history = [_msg(10, "orch",
                            _cmd_assign("w-a", activity_id=aid, topic_id=tid))]
            r = ow_apply_state_with_conn(
                conn, channel_code=CH, topic_id=tid, history=history, now=NOW,
            )
            assert r["applied"] == 1
            workers = wk.list_workers_with_conn(conn, channel_code=CH, alive_only=False)
            assert len(workers) == 1
            w = workers[0]
            assert w["handle"] == "w-a"
            assert w["activity_id"] == aid
            assert w["model"] == "claude-opus-4-7"
            assert w["cwd"] == "/tmp"
            assert w["permission_mode"] == "auto"
            assert w["timeout_min"] == 240
            assert w["task_n"] == 1
        finally:
            conn.close()


class TestIdentityUpdatesSessionId:
    def test_identity_after_assign_updates_session_id(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            aid = _make_activity(conn)
            _setup_channel(conn, tid)
            history = [
                _msg(10, "orch", _cmd_assign("w-a", activity_id=aid, topic_id=tid)),
                _msg(11, "w-a", _ev_identity("w-a", activity_id=aid,
                                              topic_id=tid, session_id="sess-1")),
            ]
            ow_apply_state_with_conn(
                conn, channel_code=CH, topic_id=tid, history=history, now=NOW,
            )
            w = wk.get_alive_worker_by_handle_with_conn(
                conn, channel_code=CH, handle="w-a"
            )
            assert w["session_id"] == "sess-1"
        finally:
            conn.close()

    def test_identity_first_creates_worker(self, db):
        """worker が assign 前に identity を出した場合でも row が作られる"""
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            aid = _make_activity(conn)
            _setup_channel(conn, tid)
            history = [
                _msg(10, "w-a", _ev_identity("w-a", activity_id=aid,
                                              topic_id=tid, session_id="sess-x")),
            ]
            ow_apply_state_with_conn(
                conn, channel_code=CH, topic_id=tid, history=history, now=NOW,
            )
            w = wk.get_alive_worker_by_handle_with_conn(
                conn, channel_code=CH, handle="w-a"
            )
            assert w is not None
            assert w["session_id"] == "sess-x"
            assert w["activity_id"] == aid
        finally:
            conn.close()


class TestStateTransitions:
    def test_full_lifecycle_progresses_state(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            aid = _make_activity(conn)
            _setup_channel(conn, tid)
            history = [
                _msg(10, "orch", _cmd_assign("w-a", activity_id=aid, topic_id=tid)),
                _msg(11, "w-a", _ev_heartbeat("w-a")),
                _msg(12, "w-a", _ev_identity("w-a", activity_id=aid,
                                              topic_id=tid, session_id="sess-1")),
                _msg(13, "w-a", _ev_state("w-a", "loading")),
                _msg(14, "w-a", _ev_state("w-a", "ready")),
                _msg(15, "w-a", _ev_state("w-a", "working")),
                _msg(16, "w-a", _ev_state("w-a", "draining")),
                _msg(17, "w-a", _ev_state("w-a", "terminated", cause="closed")),
            ]
            r = ow_apply_state_with_conn(
                conn, channel_code=CH, topic_id=tid, history=history, now=NOW,
            )
            assert r["applied"] == 8
            # alive 0件, terminated 1件
            assert wk.list_workers_with_conn(
                conn, channel_code=CH, alive_only=True
            ) == []
            all_ = wk.list_workers_with_conn(
                conn, channel_code=CH, alive_only=False
            )
            assert len(all_) == 1
            w = all_[0]
            assert w["workload_state"] == "terminated"
            assert w["cause"] == "closed"
            assert w["last_state_msg_id"] == 17
            assert w["ready_at"] == NOW
            assert w["terminated_at"] == NOW
        finally:
            conn.close()


class TestIdempotency:
    def test_re_apply_same_history_is_idempotent(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            aid = _make_activity(conn)
            _setup_channel(conn, tid)
            history = [
                _msg(10, "orch", _cmd_assign("w-a", activity_id=aid, topic_id=tid)),
                _msg(11, "w-a", _ev_state("w-a", "working")),
            ]
            r1 = ow_apply_state_with_conn(
                conn, channel_code=CH, topic_id=tid, history=history, now=NOW,
            )
            r2 = ow_apply_state_with_conn(
                conn, channel_code=CH, topic_id=tid, history=history, now=NOW,
            )
            assert r1["applied"] == 2
            assert r2["applied"] == 0
            assert r2["duplicate"] == 2
            # workerはひとつだけ
            assert len(wk.list_workers_with_conn(
                conn, channel_code=CH, alive_only=False)) == 1
        finally:
            conn.close()


class TestSkippedMessages:
    def test_unknown_kind_is_skipped(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            _setup_channel(conn, tid)
            history = [
                {"msg_id": 10, "handle": "w-a",
                 "body": {"v": 1, "kind": "noise", "data": {"type": "x"}},
                 "created_at": NOW},
            ]
            r = ow_apply_state_with_conn(
                conn, channel_code=CH, topic_id=tid, history=history, now=NOW,
            )
            assert r["skipped"] == 1
            assert am.is_msg_applied_with_conn(conn, channel_code=CH, msg_id=10)
        finally:
            conn.close()

    def test_string_body_is_skipped(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            _setup_channel(conn, tid)
            history = [{"msg_id": 10, "handle": "x", "body": "garbage",
                         "created_at": NOW}]
            r = ow_apply_state_with_conn(
                conn, channel_code=CH, topic_id=tid, history=history, now=NOW,
            )
            assert r["skipped"] == 1
        finally:
            conn.close()

    def test_invalid_state_value_is_skipped(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            _setup_channel(conn, tid)
            history = [_msg(10, "w-a", {
                "v": 1, "kind": "event", "from": "w-a", "to": "orch",
                "data": {"type": "state", "state": "bogus"},
            })]
            r = ow_apply_state_with_conn(
                conn, channel_code=CH, topic_id=tid, history=history, now=NOW,
            )
            assert r["skipped"] == 1
        finally:
            conn.close()


class TestHeartbeatUpdates:
    def test_heartbeat_updates_last_heartbeat_at(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            aid = _make_activity(conn)
            _setup_channel(conn, tid)
            history = [
                _msg(10, "orch", _cmd_assign("w-a", activity_id=aid, topic_id=tid)),
                _msg(11, "w-a", _ev_heartbeat("w-a"), created_at="2026-06-17T10:05:00Z"),
            ]
            ow_apply_state_with_conn(
                conn, channel_code=CH, topic_id=tid, history=history, now=NOW,
            )
            w = wk.get_alive_worker_by_handle_with_conn(
                conn, channel_code=CH, handle="w-a"
            )
            assert w["last_heartbeat_at"] == "2026-06-17T10:05:00Z"
        finally:
            conn.close()

    def test_heartbeat_before_identity_is_silently_skipped(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            _setup_channel(conn, tid)
            history = [_msg(10, "w-a", _ev_heartbeat("w-a"))]
            r = ow_apply_state_with_conn(
                conn, channel_code=CH, topic_id=tid, history=history, now=NOW,
            )
            # 行が無い heartbeat は applied 扱いで黙って終わる
            assert r["applied"] == 1
            assert r["skipped"] == 0
            assert wk.list_workers_with_conn(
                conn, channel_code=CH, alive_only=False) == []
        finally:
            conn.close()


class TestChannelLastSeen:
    def test_last_seen_msg_id_advances(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            aid = _make_activity(conn)
            _setup_channel(conn, tid)
            history = [
                _msg(10, "orch", _cmd_assign("w-a", activity_id=aid, topic_id=tid)),
                _msg(15, "w-a", _ev_state("w-a", "working")),
            ]
            ow_apply_state_with_conn(
                conn, channel_code=CH, topic_id=tid, history=history, now=NOW,
            )
            chan = ch.get_channel_with_conn(conn, CH)
            assert chan["last_seen_msg_id"] == 15
        finally:
            conn.close()


class TestCQSSeparation:
    """reducer は activities テーブルに書き込まないことを保証"""

    def test_activity_status_unchanged_after_reducer(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            aid = _make_activity(conn)
            _setup_channel(conn, tid)
            history = [
                _msg(10, "orch", _cmd_assign("w-a", activity_id=aid, topic_id=tid)),
                _msg(11, "w-a", _ev_state("w-a", "working")),
                _msg(12, "w-a", _ev_state("w-a", "terminated", cause="closed")),
            ]
            ow_apply_state_with_conn(
                conn, channel_code=CH, topic_id=tid, history=history, now=NOW,
            )
            row = conn.execute(
                "SELECT status, last_heartbeat_at FROM activities WHERE id = ?",
                (aid,),
            ).fetchone()
            # reducer は activity を触らない
            assert row["status"] == "pending"
            assert row["last_heartbeat_at"] is None
        finally:
            conn.close()


class TestNonWorkerIdentitySkipped:
    def test_orch_identity_does_not_create_worker(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            _setup_channel(conn, tid)
            history = [_msg(10, "orch", {
                "v": 1, "kind": "event", "from": "orch", "to": "*",
                "data": {"type": "identity", "role": "orch", "handle": "orch"},
            })]
            r = ow_apply_state_with_conn(
                conn, channel_code=CH, topic_id=tid, history=history, now=NOW,
            )
            assert r["applied"] == 1
            assert wk.list_workers_with_conn(
                conn, channel_code=CH, alive_only=False) == []
        finally:
            conn.close()
