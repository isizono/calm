"""src/services/ow/projector.ow_project_activities_with_conn のユニットテスト。

ow_workers のスナップショットを activities テーブルに反映する projector の挙動を検証。
ow:managed タグの付かない activity は変更されないこと、status downgrade が起きないこと、
outcome:cancelled / outcome:failed タグが cause に応じて追加されることを確認する。
"""
import os
import sqlite3
import tempfile

import pytest

from src.db import get_connection, init_database
from src.services.ow import channels as ch
from src.services.ow import workers as wk
from src.services.ow.projector import ow_project_activities_with_conn
from src.services.tag_service import (
    ensure_tag_ids,
    get_entity_tags,
    link_tags,
)


NOW = "2026-06-17T10:00:00Z"
LATER = "2026-06-17T11:00:00Z"
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


def _make_topic(conn) -> int:
    cur = conn.execute(
        "INSERT INTO discussion_topics (title, description) VALUES (?, ?)",
        ("t", "d"),
    )
    return cur.lastrowid


def _make_activity(
    conn, *, title="a", status="pending", with_ow_managed=True, topic_id=None
) -> int:
    cur = conn.execute(
        "INSERT INTO activities (title, description, status) VALUES (?, ?, ?)",
        (title, "", status),
    )
    aid = cur.lastrowid
    if with_ow_managed:
        tag_ids = ensure_tag_ids(conn, [("ow", "managed")])
        link_tags(conn, "activity_tags", "activity_id", aid, tag_ids)
    if topic_id is not None:
        # polymorphic relations: 'activity' < 'topic' なので source=activity, target=topic
        conn.execute(
            "INSERT INTO relations (source_type, source_id, target_type, target_id) "
            "VALUES ('activity', ?, 'topic', ?)",
            (aid, topic_id),
        )
    return aid


def _setup_channel(conn, topic_id) -> None:
    ch.upsert_channel_with_conn(
        conn, channel_code=CH, topic_id=topic_id, orch_handle="orch", now=NOW
    )


def _insert_worker(
    conn,
    *,
    handle,
    activity_id,
    topic_id,
    task_n,
    workload_state="working",
    cause=None,
    last_heartbeat_at=None,
    terminated_at=None,
):
    cur = conn.execute(
        """
        INSERT INTO ow_workers
          (channel_code, handle, alias, activity_id, topic_id, task_n,
           workload_state, cause, last_heartbeat_at, terminated_at, spawned_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (CH, handle, handle, activity_id, topic_id, task_n,
         workload_state, cause, last_heartbeat_at, terminated_at, NOW),
    )
    return cur.lastrowid


class TestStatusTransitions:
    def test_working_worker_moves_pending_to_in_progress(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            _setup_channel(conn, tid)
            aid = _make_activity(conn, topic_id=tid)
            _insert_worker(
                conn, handle="w-a", activity_id=aid, topic_id=tid,
                task_n=1, workload_state="working",
            )
            ow_project_activities_with_conn(conn, topic_id=tid)
            row = conn.execute(
                "SELECT status FROM activities WHERE id = ?", (aid,)
            ).fetchone()
            assert row["status"] == "in_progress"
        finally:
            conn.close()

    def test_terminated_closed_moves_in_progress_to_completed(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            _setup_channel(conn, tid)
            aid = _make_activity(conn, status="in_progress", topic_id=tid)
            _insert_worker(
                conn, handle="w-a", activity_id=aid, topic_id=tid,
                task_n=1, workload_state="terminated", cause="closed",
                terminated_at=LATER,
            )
            ow_project_activities_with_conn(conn, topic_id=tid)
            row = conn.execute(
                "SELECT status FROM activities WHERE id = ?", (aid,)
            ).fetchone()
            assert row["status"] == "completed"
        finally:
            conn.close()

    def test_completed_not_downgraded_when_new_worker_appears(self, db):
        """completed状態のactivityにworkingなworkerが現れても in_progress に戻さない"""
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            _setup_channel(conn, tid)
            aid = _make_activity(conn, status="completed", topic_id=tid)
            _insert_worker(
                conn, handle="w-a", activity_id=aid, topic_id=tid,
                task_n=1, workload_state="working",
            )
            ow_project_activities_with_conn(conn, topic_id=tid)
            row = conn.execute(
                "SELECT status FROM activities WHERE id = ?", (aid,)
            ).fetchone()
            assert row["status"] == "completed"
        finally:
            conn.close()

    def test_only_loading_worker_keeps_pending(self, db):
        """loading/ready の worker しかなければ activity は pending のまま"""
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            _setup_channel(conn, tid)
            aid = _make_activity(conn, topic_id=tid)
            _insert_worker(
                conn, handle="w-a", activity_id=aid, topic_id=tid,
                task_n=1, workload_state="loading",
            )
            ow_project_activities_with_conn(conn, topic_id=tid)
            row = conn.execute(
                "SELECT status FROM activities WHERE id = ?", (aid,)
            ).fetchone()
            assert row["status"] == "pending"
        finally:
            conn.close()


class TestOutcomeTags:
    def test_cancelled_cause_adds_outcome_cancelled_tag(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            _setup_channel(conn, tid)
            aid = _make_activity(conn, status="in_progress", topic_id=tid)
            _insert_worker(
                conn, handle="w-a", activity_id=aid, topic_id=tid,
                task_n=1, workload_state="terminated", cause="cancelled",
                terminated_at=LATER,
            )
            ow_project_activities_with_conn(conn, topic_id=tid)
            tags = get_entity_tags(conn, "activity_tags", "activity_id", aid)
            assert "outcome:cancelled" in tags
        finally:
            conn.close()

    def test_crashed_cause_adds_outcome_failed_tag(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            _setup_channel(conn, tid)
            aid = _make_activity(conn, status="in_progress", topic_id=tid)
            _insert_worker(
                conn, handle="w-a", activity_id=aid, topic_id=tid,
                task_n=1, workload_state="terminated", cause="crashed",
                terminated_at=LATER,
            )
            ow_project_activities_with_conn(conn, topic_id=tid)
            tags = get_entity_tags(conn, "activity_tags", "activity_id", aid)
            assert "outcome:failed" in tags
        finally:
            conn.close()

    def test_closed_cause_adds_no_outcome_tag(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            _setup_channel(conn, tid)
            aid = _make_activity(conn, status="in_progress", topic_id=tid)
            _insert_worker(
                conn, handle="w-a", activity_id=aid, topic_id=tid,
                task_n=1, workload_state="terminated", cause="closed",
                terminated_at=LATER,
            )
            ow_project_activities_with_conn(conn, topic_id=tid)
            tags = get_entity_tags(conn, "activity_tags", "activity_id", aid)
            assert not any(t.startswith("outcome:") for t in tags)
        finally:
            conn.close()

    def test_outcome_tag_added_only_once(self, db):
        """二度projectしてもoutcome:cancelledタグは1回だけ追加される（重複しない）"""
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            _setup_channel(conn, tid)
            aid = _make_activity(conn, status="in_progress", topic_id=tid)
            _insert_worker(
                conn, handle="w-a", activity_id=aid, topic_id=tid,
                task_n=1, workload_state="terminated", cause="cancelled",
                terminated_at=LATER,
            )
            ow_project_activities_with_conn(conn, topic_id=tid)
            ow_project_activities_with_conn(conn, topic_id=tid)
            tags = get_entity_tags(conn, "activity_tags", "activity_id", aid)
            assert tags.count("outcome:cancelled") == 1
        finally:
            conn.close()


class TestSkipsNonManagedActivity:
    def test_activity_without_ow_managed_tag_not_touched(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            _setup_channel(conn, tid)
            aid = _make_activity(conn, with_ow_managed=False, topic_id=tid)
            _insert_worker(
                conn, handle="w-a", activity_id=aid, topic_id=tid,
                task_n=1, workload_state="working",
            )
            ow_project_activities_with_conn(conn, topic_id=tid)
            row = conn.execute(
                "SELECT status FROM activities WHERE id = ?", (aid,)
            ).fetchone()
            # ow:managed が付いていないので projector は触らない
            assert row["status"] == "pending"
        finally:
            conn.close()


class TestHeartbeatSync:
    def test_latest_alive_heartbeat_is_propagated(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            _setup_channel(conn, tid)
            aid = _make_activity(conn, topic_id=tid)
            _insert_worker(
                conn, handle="w-a", activity_id=aid, topic_id=tid,
                task_n=1, workload_state="working",
                last_heartbeat_at="2026-06-17T10:30:00Z",
            )
            ow_project_activities_with_conn(conn, topic_id=tid)
            row = conn.execute(
                "SELECT last_heartbeat_at FROM activities WHERE id = ?", (aid,)
            ).fetchone()
            assert row["last_heartbeat_at"] == "2026-06-17T10:30:00Z"
        finally:
            conn.close()


class TestTopicScope:
    def test_topic_scoped_projection_skips_other_topics(self, db):
        conn = get_connection()
        try:
            t1 = _make_topic(conn)
            t2 = _make_topic(conn)
            _setup_channel(conn, t1)
            ch.upsert_channel_with_conn(
                conn, channel_code="C2", topic_id=t2,
                orch_handle="orch", now=NOW,
            )
            a1 = _make_activity(conn, topic_id=t1)
            a2 = _make_activity(conn, topic_id=t2)
            _insert_worker(
                conn, handle="w-a", activity_id=a1, topic_id=t1,
                task_n=1, workload_state="working",
            )
            conn.execute(
                """
                INSERT INTO ow_workers
                  (channel_code, handle, alias, activity_id, topic_id, task_n,
                   workload_state, spawned_at)
                VALUES ('C2', 'w-b', 'w-b', ?, ?, 1, 'working', ?)
                """,
                (a2, t2, NOW),
            )
            ow_project_activities_with_conn(conn, topic_id=t1)
            # t1 の activity だけ更新される
            assert conn.execute(
                "SELECT status FROM activities WHERE id = ?", (a1,)
            ).fetchone()["status"] == "in_progress"
            assert conn.execute(
                "SELECT status FROM activities WHERE id = ?", (a2,)
            ).fetchone()["status"] == "pending"
        finally:
            conn.close()
