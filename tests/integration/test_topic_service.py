"""topic_serviceの統合テスト"""
import os
import tempfile

import pytest

from src.db import init_database, get_connection
from src.services.activity_service import add_activity
from src.services.relation_service import add_relation
from src.services.topic_service import add_topic, get_activity_topics_batch


@pytest.fixture
def temp_db():
    """テスト用の一時的なデータベースを作成する"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


class TestGetActivityTopicsBatch:
    """get_activity_topics_batch のテスト (D#2465 relations_view バッチ取得)"""

    def test_empty_input_returns_empty(self, temp_db):
        """activity_ids が空ならクエリ無しで空辞書を返す"""
        conn = get_connection()
        try:
            assert get_activity_topics_batch(conn, []) == {}
        finally:
            conn.close()

    def test_returns_related_topics(self, temp_db):
        """activity → topic の relate を relations_view 経由で拾う"""
        topic = add_topic(title="トピックA", description="d", tags=["domain:test"])
        result = add_activity(
            title="[作業] A",
            description="d",
            tags=["domain:test"],
            check_in=False,
        )
        activity_id = result["activity_id"]
        topic_id = topic["topic_id"]
        add_relation("activity", activity_id, [{"type": "topic", "ids": [topic_id]}])

        conn = get_connection()
        try:
            batch = get_activity_topics_batch(conn, [activity_id])
        finally:
            conn.close()

        assert activity_id in batch
        assert len(batch[activity_id]) == 1
        assert batch[activity_id][0]["id"] == topic_id
        assert batch[activity_id][0]["title"] == "トピックA"

    def test_topicless_activity_absent_from_result(self, temp_db):
        """関連topicが無いactivityは結果dictに現れない（キー自体を持たない）"""
        result = add_activity(
            title="[作業] 孤立",
            description="d",
            tags=["domain:test"],
            check_in=False,
        )
        activity_id = result["activity_id"]

        conn = get_connection()
        try:
            batch = get_activity_topics_batch(conn, [activity_id])
        finally:
            conn.close()

        assert batch == {}

    def test_topics_sorted_by_id_ascending(self, temp_db):
        """1つのactivityに複数topicが紐づくとき、topic_id昇順で返る（決定的）"""
        t1 = add_topic(title="先発トピック", description="d", tags=["domain:test"])["topic_id"]
        t2 = add_topic(title="後発トピック", description="d", tags=["domain:test"])["topic_id"]
        result = add_activity(
            title="[作業] 複数topic",
            description="d",
            tags=["domain:test"],
            check_in=False,
        )
        activity_id = result["activity_id"]
        # 逆順で挿入しても返り順は topic_id 昇順
        add_relation("activity", activity_id, [{"type": "topic", "ids": [t2]}])
        add_relation("activity", activity_id, [{"type": "topic", "ids": [t1]}])

        conn = get_connection()
        try:
            batch = get_activity_topics_batch(conn, [activity_id])
        finally:
            conn.close()

        topic_ids = [t["id"] for t in batch[activity_id]]
        assert topic_ids == sorted([t1, t2])

    def test_batch_multiple_activities(self, temp_db):
        """複数activity_idsを一括で問い合わせ、各activityに対応する関連を返す"""
        topic = add_topic(title="共通トピック", description="d", tags=["domain:test"])["topic_id"]
        a1 = add_activity(
            title="[作業] 1",
            description="d",
            tags=["domain:test"],
            check_in=False,
        )["activity_id"]
        a2 = add_activity(
            title="[作業] 2",
            description="d",
            tags=["domain:test"],
            check_in=False,
        )["activity_id"]
        a3 = add_activity(
            title="[作業] 3",
            description="d",
            tags=["domain:test"],
            check_in=False,
        )["activity_id"]
        add_relation("activity", a1, [{"type": "topic", "ids": [topic]}])
        add_relation("activity", a2, [{"type": "topic", "ids": [topic]}])
        # a3 は関連無し

        conn = get_connection()
        try:
            batch = get_activity_topics_batch(conn, [a1, a2, a3])
        finally:
            conn.close()

        assert a1 in batch
        assert a2 in batch
        assert a3 not in batch
        assert batch[a1][0]["id"] == topic
        assert batch[a2][0]["id"] == topic
