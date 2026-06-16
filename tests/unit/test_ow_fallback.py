"""read-fallback シムのユニットテスト（Phase 1 限定）。

ow_workers にデータがあれば ow_workers をそのまま返し、空なら旧 queue-t<topic>.md
パース結果を擬似 ow_workers 形式で返すことを検証する。
"""
import os
import tempfile
from pathlib import Path

import pytest

from src.db import get_connection, init_database
from src.services.ow import channels as ch
from src.services.ow.fallback import read_workers_with_fallback_with_conn


NOW = "2026-06-17T10:00:00Z"


@pytest.fixture
def db_and_queue_dir(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        queue_dir = os.path.join(tmpdir, "queue")
        os.makedirs(queue_dir, exist_ok=True)
        monkeypatch.setenv("DISCUSSION_DB_PATH", db_path)
        monkeypatch.setenv("OW_QUEUE_DIR", queue_dir)
        # ow_service は OW_QUEUE_DIR をimport時にmodule変数へキャッシュするため、
        # env変更だけでは _get_queue_dir() に反映されない。明示的に書き換える。
        import src.services.ow_service as ows
        monkeypatch.setattr(ows, "OW_QUEUE_DIR", queue_dir)
        init_database()
        yield db_path, queue_dir


def _make_topic(conn) -> int:
    cur = conn.execute(
        "INSERT INTO discussion_topics (title, description) VALUES (?, ?)",
        ("t", "d"),
    )
    return cur.lastrowid


def _make_activity(conn) -> int:
    cur = conn.execute(
        "INSERT INTO activities (title, description, status) VALUES (?, ?, ?)",
        ("a", "", "pending"),
    )
    return cur.lastrowid


class TestPreferOwWorkers:
    def test_returns_ow_workers_when_present(self, db_and_queue_dir):
        _, _ = db_and_queue_dir
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            aid = _make_activity(conn)
            ch.upsert_channel_with_conn(
                conn, channel_code="C1", topic_id=tid,
                orch_handle="orch", now=NOW,
            )
            conn.execute(
                """
                INSERT INTO ow_workers
                  (channel_code, handle, alias, activity_id, topic_id, task_n,
                   workload_state, spawned_at)
                VALUES ('C1', 'w-a', 'w-a', ?, ?, 1, 'working', ?)
                """,
                (aid, tid, NOW),
            )
            conn.commit()
            result = read_workers_with_fallback_with_conn(
                conn, channel_code="C1", topic_id=tid,
            )
            assert result["source"] == "ow_workers"
            assert len(result["workers"]) == 1
            assert result["workers"][0]["handle"] == "w-a"
        finally:
            conn.close()


class TestLegacyQueueFallback:
    def test_falls_back_to_queue_md_when_ow_workers_empty(self, db_and_queue_dir):
        _, queue_dir = db_and_queue_dir
        # 旧 queue-t1.md を投入
        queue_content = """---
topic_id: 1
orch_activity_id: 100
channel_code: C1
orch_cwd: /tmp
last_seen_msg_id: 0
---

## T1 | sample task | working
- worker: w-x / term_ref: iterm2-tab-1 / session: abc
"""
        Path(queue_dir, "queue-t1.md").write_text(queue_content)
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            ch.upsert_channel_with_conn(
                conn, channel_code="C1", topic_id=tid,
                orch_handle="orch", now=NOW,
            )
            conn.commit()
            result = read_workers_with_fallback_with_conn(
                conn, channel_code="C1", topic_id=1,
            )
            assert result["source"] == "legacy_queue"
            assert len(result["workers"]) == 1
            w = result["workers"][0]
            assert w["handle"] == "w-x"
            assert w["alias"] == "w-x"
            assert w["task_n"] == 1
            assert w["workload_state"] == "working"
            assert w["term_ref"] == "iterm2-tab-1"
            assert w["title"] == "sample task"
            assert w["_source"] == "legacy_queue"
        finally:
            conn.close()

    def test_returns_empty_when_no_ow_workers_and_no_queue_file(
        self, db_and_queue_dir
    ):
        conn = get_connection()
        try:
            tid = _make_topic(conn)
            ch.upsert_channel_with_conn(
                conn, channel_code="C1", topic_id=tid,
                orch_handle="orch", now=NOW,
            )
            conn.commit()
            result = read_workers_with_fallback_with_conn(
                conn, channel_code="C1", topic_id=tid,
            )
            assert result["source"] == "legacy_queue"
            assert result["workers"] == []
        finally:
            conn.close()
