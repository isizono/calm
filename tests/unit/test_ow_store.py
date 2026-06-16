"""src/services/ow/ store CRUD (channels / workers / applied_msgs) のユニットテスト。

Phase 1 で新規追加した ow_channels / ow_workers / ow_applied_msg_ids 3テーブルに対する
CRUDヘルパーが、conn共有版とラッパー版の両方で期待通りに動くことを検証する。
"""
import os
import sqlite3
import tempfile

import pytest

from src.db import get_connection, init_database
from src.services.ow import applied_msgs as am
from src.services.ow import channels as ch
from src.services.ow import workers as wk


NOW = "2026-06-17T00:00:00Z"
LATER = "2026-06-17T01:00:00Z"


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


def _make_topic(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "INSERT INTO discussion_topics (title, description) VALUES (?, ?)",
        ("t", "d"),
    )
    return cur.lastrowid


def _make_activity(conn: sqlite3.Connection, title: str = "a") -> int:
    cur = conn.execute(
        "INSERT INTO activities (title, description, status) VALUES (?, ?, ?)",
        (title, "", "pending"),
    )
    return cur.lastrowid


class TestChannelsCRUD:
    def test_upsert_inserts_new_row(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            ch.upsert_channel_with_conn(
                conn,
                channel_code="C1",
                topic_id=tid,
                orch_handle="orch",
                orch_cwd="/tmp",
                now=NOW,
            )
            conn.commit()
            row = ch.get_channel_with_conn(conn, "C1")
            assert row is not None
            assert row["topic_id"] == tid
            assert row["orch_handle"] == "orch"
            assert row["orch_cwd"] == "/tmp"
            assert row["last_seen_msg_id"] == 0
            assert row["created_at"] == NOW
        finally:
            conn.close()

    def test_upsert_updates_existing_row_preserves_immutables(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            ch.upsert_channel_with_conn(
                conn,
                channel_code="C1",
                topic_id=tid,
                orch_handle="orch",
                orch_cwd="/tmp",
                now=NOW,
            )
            ch.update_channel_last_seen_with_conn(
                conn, channel_code="C1", last_seen_msg_id=42, now=NOW
            )
            # 2回目: orch_session_id だけ更新
            ch.upsert_channel_with_conn(
                conn,
                channel_code="C1",
                topic_id=tid,
                orch_handle="orch",
                orch_session_id="sess-123",
                now=LATER,
            )
            conn.commit()
            row = ch.get_channel_with_conn(conn, "C1")
            assert row["orch_session_id"] == "sess-123"
            assert row["orch_cwd"] == "/tmp"  # 保持
            assert row["last_seen_msg_id"] == 42  # 保持
            assert row["created_at"] == NOW  # 保持
            assert row["updated_at"] == LATER
        finally:
            conn.close()

    def test_update_last_seen_is_monotonic(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            ch.upsert_channel_with_conn(
                conn, channel_code="C1", topic_id=tid, orch_handle="orch", now=NOW
            )
            ch.update_channel_last_seen_with_conn(
                conn, channel_code="C1", last_seen_msg_id=10, now=NOW
            )
            ch.update_channel_last_seen_with_conn(
                conn, channel_code="C1", last_seen_msg_id=5, now=LATER
            )
            assert ch.get_channel_with_conn(conn, "C1")["last_seen_msg_id"] == 10
        finally:
            conn.close()

    def test_list_channels_filters_deleted_by_default(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            ch.upsert_channel_with_conn(
                conn, channel_code="C1", topic_id=tid, orch_handle="orch", now=NOW
            )
            ch.upsert_channel_with_conn(
                conn, channel_code="C2", topic_id=tid, orch_handle="orch", now=NOW
            )
            ch.soft_delete_channel_with_conn(
                conn, channel_code="C2", deleted_at=LATER
            )
            assert len(ch.list_channels_with_conn(conn, topic_id=tid)) == 1
            assert (
                len(
                    ch.list_channels_with_conn(
                        conn, topic_id=tid, include_deleted=True
                    )
                )
                == 2
            )
        finally:
            conn.close()


class TestWorkersCRUD:
    def test_allocate_task_n_starts_at_one(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            ch.upsert_channel_with_conn(
                conn, channel_code="C1", topic_id=tid, orch_handle="orch", now=NOW
            )
            assert wk.allocate_task_n_with_conn(conn, "C1") == 1
        finally:
            conn.close()

    def test_allocate_task_n_increments(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            ch.upsert_channel_with_conn(
                conn, channel_code="C1", topic_id=tid, orch_handle="orch", now=NOW
            )
            aid = _make_activity(conn)
            wk.insert_worker_with_conn(
                conn,
                channel_code="C1", handle="w-a", alias="w-a",
                activity_id=aid, topic_id=tid,
                task_n=wk.allocate_task_n_with_conn(conn, "C1"),
                spawned_at=NOW,
            )
            assert wk.allocate_task_n_with_conn(conn, "C1") == 2
        finally:
            conn.close()

    def test_insert_and_fetch_worker(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            ch.upsert_channel_with_conn(
                conn, channel_code="C1", topic_id=tid, orch_handle="orch", now=NOW
            )
            aid = _make_activity(conn)
            wid = wk.insert_worker_with_conn(
                conn,
                channel_code="C1", handle="w-a", alias="w-a",
                activity_id=aid, topic_id=tid, task_n=1,
                model="claude-opus-4-7", cwd="/tmp",
                spawned_at=NOW,
            )
            assert wid > 0
            row = wk.get_worker_by_id_with_conn(conn, wid)
            assert row["handle"] == "w-a"
            assert row["workload_state"] == "spawning"
            assert row["session_id"] is None  # Q10: NULL許容
            assert wk.get_alive_worker_by_handle_with_conn(
                conn, channel_code="C1", handle="w-a"
            ) is not None
        finally:
            conn.close()

    def test_update_worker_state_keeps_other_fields(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            ch.upsert_channel_with_conn(
                conn, channel_code="C1", topic_id=tid, orch_handle="orch", now=NOW
            )
            aid = _make_activity(conn)
            wid = wk.insert_worker_with_conn(
                conn,
                channel_code="C1", handle="w-a", alias="w-a",
                activity_id=aid, topic_id=tid, task_n=1,
                model="claude-opus-4-7",
                spawned_at=NOW,
            )
            wk.update_worker_state_with_conn(
                conn,
                worker_id=wid,
                workload_state="ready",
                last_state_msg_id=10,
                last_heartbeat_at=NOW,
                ready_at=NOW,
            )
            row = wk.get_worker_by_id_with_conn(conn, wid)
            assert row["workload_state"] == "ready"
            assert row["last_state_msg_id"] == 10
            assert row["ready_at"] == NOW
            assert row["model"] == "claude-opus-4-7"  # 保持
        finally:
            conn.close()

    def test_update_worker_identity_sets_session_id(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            ch.upsert_channel_with_conn(
                conn, channel_code="C1", topic_id=tid, orch_handle="orch", now=NOW
            )
            aid = _make_activity(conn)
            wid = wk.insert_worker_with_conn(
                conn,
                channel_code="C1", handle="w-a", alias="w-a",
                activity_id=aid, topic_id=tid, task_n=1, spawned_at=NOW,
            )
            wk.update_worker_identity_with_conn(
                conn, worker_id=wid, session_id="sess-1", model="claude-opus-4-7"
            )
            row = wk.get_worker_by_id_with_conn(conn, wid)
            assert row["session_id"] == "sess-1"
            assert row["model"] == "claude-opus-4-7"
        finally:
            conn.close()

    def test_list_workers_alive_only_excludes_terminated(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            ch.upsert_channel_with_conn(
                conn, channel_code="C1", topic_id=tid, orch_handle="orch", now=NOW
            )
            a1 = _make_activity(conn, "a1")
            a2 = _make_activity(conn, "a2")
            wid1 = wk.insert_worker_with_conn(
                conn,
                channel_code="C1", handle="w-a", alias="w-a",
                activity_id=a1, topic_id=tid, task_n=1, spawned_at=NOW,
            )
            wk.update_worker_state_with_conn(
                conn, worker_id=wid1, workload_state="terminated",
                cause="closed", terminated_at=LATER,
            )
            wk.insert_worker_with_conn(
                conn,
                channel_code="C1", handle="w-b", alias="w-b",
                activity_id=a2, topic_id=tid, task_n=2,
                workload_state="working", spawned_at=NOW,
            )
            alive = wk.list_workers_with_conn(
                conn, channel_code="C1", alive_only=True
            )
            assert len(alive) == 1
            assert alive[0]["handle"] == "w-b"
            all_ = wk.list_workers_with_conn(
                conn, channel_code="C1", alive_only=False
            )
            assert len(all_) == 2
        finally:
            conn.close()


class TestAppliedMsgIds:
    def test_mark_and_check_applied(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            ch.upsert_channel_with_conn(
                conn, channel_code="C1", topic_id=tid, orch_handle="orch", now=NOW
            )
            am.mark_msg_applied_with_conn(
                conn, channel_code="C1", msg_id=10, applied_at=NOW
            )
            assert am.is_msg_applied_with_conn(conn, channel_code="C1", msg_id=10)
            assert not am.is_msg_applied_with_conn(conn, channel_code="C1", msg_id=11)
        finally:
            conn.close()

    def test_mark_msg_applied_is_idempotent(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            ch.upsert_channel_with_conn(
                conn, channel_code="C1", topic_id=tid, orch_handle="orch", now=NOW
            )
            am.mark_msg_applied_with_conn(
                conn, channel_code="C1", msg_id=10, applied_at=NOW
            )
            am.mark_msg_applied_with_conn(
                conn, channel_code="C1", msg_id=10, applied_at=LATER
            )  # 二度目はINSERT OR IGNORE で何もしない
            # 1行のみ
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM ow_applied_msg_ids WHERE channel_code = 'C1'"
            ).fetchone()
            assert row["c"] == 1
        finally:
            conn.close()

    def test_invalid_outcome_raises(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            ch.upsert_channel_with_conn(
                conn, channel_code="C1", topic_id=tid, orch_handle="orch", now=NOW
            )
            with pytest.raises(ValueError):
                am.mark_msg_applied_with_conn(
                    conn, channel_code="C1", msg_id=10,
                    applied_at=NOW, outcome="rejected",
                )
        finally:
            conn.close()

    def test_get_max_applied_msg_id(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            ch.upsert_channel_with_conn(
                conn, channel_code="C1", topic_id=tid, orch_handle="orch", now=NOW
            )
            assert am.get_max_applied_msg_id_with_conn(conn, channel_code="C1") == 0
            am.mark_msg_applied_with_conn(
                conn, channel_code="C1", msg_id=5, applied_at=NOW
            )
            am.mark_msg_applied_with_conn(
                conn, channel_code="C1", msg_id=12, applied_at=NOW
            )
            am.mark_msg_applied_with_conn(
                conn, channel_code="C1", msg_id=8, applied_at=NOW, outcome="skipped"
            )
            assert am.get_max_applied_msg_id_with_conn(conn, channel_code="C1") == 12
        finally:
            conn.close()

    def test_get_applied_msg_id_set(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            ch.upsert_channel_with_conn(
                conn, channel_code="C1", topic_id=tid, orch_handle="orch", now=NOW
            )
            for mid in [1, 3, 5]:
                am.mark_msg_applied_with_conn(
                    conn, channel_code="C1", msg_id=mid, applied_at=NOW
                )
            assert am.get_applied_msg_id_set_with_conn(conn, channel_code="C1") == {1, 3, 5}
        finally:
            conn.close()
