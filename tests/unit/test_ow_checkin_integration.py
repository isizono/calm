"""check_in と ow ダッシュボードの統合テスト（M#288 §3.8）

ow:managed タグ付き activity に check_in すると、ダッシュボード render の
該当行が summary に追加されることを検証する。非 ow activity への check_in は
従来通りの summary のみを返す。
"""
import os
import tempfile

import pytest

from src.db import get_connection, init_database
from src.services.checkin_service import check_in
from src.services.ow import channels as ch
from src.services.tag_service import ensure_tag_ids, link_tags


NOW = "2026-06-17T10:00:00Z"


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        os.environ["OW_VIEWS_DIR"] = os.path.join(tmpdir, "views")
        init_database()
        yield db_path
        for k in ("DISCUSSION_DB_PATH", "OW_VIEWS_DIR"):
            if k in os.environ:
                del os.environ[k]


def _make_topic(conn, title="t") -> int:
    cur = conn.execute(
        "INSERT INTO discussion_topics (title, description) VALUES (?, ?)",
        (title, "d"),
    )
    return cur.lastrowid


def _make_activity_with_topic(
    conn, *, title="A", topic_id, with_ow_managed=True, intent="implement"
) -> int:
    cur = conn.execute(
        "INSERT INTO activities (title, description, status) VALUES (?, ?, ?)",
        (title, "", "pending"),
    )
    aid = cur.lastrowid
    if with_ow_managed:
        link_tags(conn, "activity_tags", "activity_id", aid,
                  ensure_tag_ids(conn, [("ow", "managed")]))
    link_tags(conn, "activity_tags", "activity_id", aid,
              ensure_tag_ids(conn, [("intent", intent)]))
    # activity ↔ topic 紐付け（polymorphic）: 'activity' < 'topic'
    conn.execute(
        "INSERT INTO relations (source_type, source_id, target_type, target_id) "
        "VALUES ('activity', ?, 'topic', ?)",
        (aid, topic_id),
    )
    return aid


def _insert_worker(
    conn, *, channel_code, alias, activity_id, topic_id, task_n,
    workload_state="working", last_heartbeat_at=None,
):
    conn.execute(
        """
        INSERT INTO ow_workers
          (channel_code, handle, alias, activity_id, topic_id, task_n,
           workload_state, last_heartbeat_at, spawned_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (channel_code, alias, alias, activity_id, topic_id, task_n,
         workload_state, last_heartbeat_at, NOW),
    )


class TestOwSummaryAppend:
    def test_ow_managed_activity_summary_contains_dashboard_line(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn, "T67 topic")
            ch.upsert_channel_with_conn(
                conn, channel_code="C1", topic_id=tid,
                orch_handle="orch", now=NOW,
            )
            aid = _make_activity_with_topic(
                conn, title="ow managed activity", topic_id=tid,
            )
            _insert_worker(
                conn, channel_code="C1", alias="w-z",
                activity_id=aid, topic_id=tid, task_n=1,
                workload_state="working",
                last_heartbeat_at="2026-06-17T09:59:58Z",
            )
            conn.commit()
        finally:
            conn.close()
        result = check_in(aid)
        assert "summary" in result
        # ow行は "  ow: ●[空白] [aid] ..." の形で末尾に追加される
        assert "\n  ow: " in result["summary"]
        assert f"[{aid}]" in result["summary"]
        # base summary（タイトル + intent）も残っている
        assert "check-in:" in result["summary"]
        assert "intent: implement" in result["summary"]

    def test_non_ow_activity_summary_unchanged(self, db):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            aid = _make_activity_with_topic(
                conn, topic_id=tid, with_ow_managed=False,
            )
            conn.commit()
        finally:
            conn.close()
        result = check_in(aid)
        assert "summary" in result
        assert "\n  ow: " not in result["summary"]

    def test_ow_managed_activity_without_topic_no_ow_line(self, db):
        """ow:managed でも topic 紐付けが無い場合は ow 行追加しない"""
        conn = get_connection()
        try:
            cur = conn.execute(
                "INSERT INTO activities (title, description, status) VALUES (?, ?, ?)",
                ("orphan", "", "pending"),
            )
            aid = cur.lastrowid
            link_tags(conn, "activity_tags", "activity_id", aid,
                      ensure_tag_ids(conn, [("ow", "managed")]))
            link_tags(conn, "activity_tags", "activity_id", aid,
                      ensure_tag_ids(conn, [("intent", "implement")]))
            conn.commit()
        finally:
            conn.close()
        result = check_in(aid)
        assert "\n  ow: " not in result["summary"]
