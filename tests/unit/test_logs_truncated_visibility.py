"""get_logs の total_count / truncated フィールドのテスト

get_decisions で先行実装済みの total_count / truncated を対称的に get_logs へも
追加した拡張の検証。ページネーション意味論（topic側は id ASC、activity側は id DESC）
も get_decisions と同一のため、test_decisions_truncated_visibility.py と対になる
構成にしている。
"""
import os
import tempfile
import pytest

from src.db import init_database
from src.services.activity_service import add_activity
from src.services.topic_service import add_topic
from src.services.relation_service import add_relation
from src.services.discussion_log_service import get_logs
from src.services.retract_service import retract
from tests.helpers import add_log
from src.services.tag_service import _injected_tags


DEFAULT_TAGS = ["domain:test"]


@pytest.fixture
def temp_db():
    """テスト用の一時的なデータベースを作成する"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def topic(temp_db):
    """テスト用トピックを作成する"""
    return add_topic(title="テストトピック", description="テスト用", tags=DEFAULT_TAGS)


class TestGetLogsTotalCountTruncatedTopic:
    """get_logs(entity_type="topic") の total_count / truncated"""

    def test_under_limit_not_truncated(self, topic):
        tid = topic["topic_id"]
        for i in range(3):
            add_log(topic_id=tid, content=f"ログ{i}")

        result = get_logs("topic", tid)

        assert "error" not in result
        assert len(result["logs"]) == 3
        assert result["total_count"] == 3
        assert result["truncated"] is False

    def test_over_limit_is_truncated(self, topic):
        tid = topic["topic_id"]
        for i in range(40):
            add_log(topic_id=tid, content=f"ログ{i}")

        result = get_logs("topic", tid)

        assert "error" not in result
        assert len(result["logs"]) == 30
        assert result["total_count"] == 40
        assert result["truncated"] is True

    def test_retracted_excluded_from_total_count_by_default(self, topic):
        tid = topic["topic_id"]
        kept = add_log(topic_id=tid, content="残るログ")
        removed = add_log(topic_id=tid, content="取り消されるログ")
        retract("log", [removed["log_id"]])

        result = get_logs("topic", tid)

        assert "error" not in result
        assert len(result["logs"]) == 1
        assert result["total_count"] == 1
        assert result["truncated"] is False

    def test_retracted_included_in_total_count_when_requested(self, topic):
        tid = topic["topic_id"]
        kept = add_log(topic_id=tid, content="残るログ")
        removed = add_log(topic_id=tid, content="取り消されるログ")
        retract("log", [removed["log_id"]])

        result = get_logs("topic", tid, include_retracted=True)

        assert "error" not in result
        assert len(result["logs"]) == 2
        assert result["total_count"] == 2
        assert result["truncated"] is False

    def test_pagination_start_id_and_limit_still_work(self, topic):
        tid = topic["topic_id"]
        logs = [add_log(topic_id=tid, content=f"ログ{i}") for i in range(5)]

        result1 = get_logs("topic", tid, limit=3)
        assert len(result1["logs"]) == 3
        assert result1["total_count"] == 5
        assert result1["truncated"] is True

        result2 = get_logs("topic", tid, start_id=logs[3]["log_id"], limit=3)
        assert len(result2["logs"]) == 2
        assert result2["logs"][0]["id_raw"] == logs[3]["log_id"]
        assert result2["total_count"] == 5
        assert result2["truncated"] is False

    def test_truncated_true_when_more_after_start_id_page(self, topic):
        tid = topic["topic_id"]
        logs = [add_log(topic_id=tid, content=f"ログ{i}") for i in range(5)]
        # logs[1] 以降は4件。limit=2 → 2件返し、後続2件が残る
        result = get_logs("topic", tid, start_id=logs[1]["log_id"], limit=2)
        assert len(result["logs"]) == 2
        assert result["total_count"] == 5
        assert result["truncated"] is True


class TestGetLogsTotalCountTruncatedActivity:
    """get_logs(entity_type="activity") の total_count / truncated"""

    def _activity_for_topic(self, topic_id: int) -> int:
        act = add_activity(
            title="[作業] タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False
        )
        add_relation("activity", act["activity_id"], [{"type": "topic", "ids": [topic_id]}])
        return act["activity_id"]

    def test_under_limit_not_truncated(self, topic):
        tid = topic["topic_id"]
        for i in range(3):
            add_log(topic_id=tid, content=f"ログ{i}")
        activity_id = self._activity_for_topic(tid)

        result = get_logs("activity", activity_id)

        assert "error" not in result
        assert len(result["logs"]) == 3
        assert result["total_count"] == 3
        assert result["truncated"] is False

    def test_over_limit_is_truncated(self, topic):
        tid = topic["topic_id"]
        for i in range(40):
            add_log(topic_id=tid, content=f"ログ{i}")
        activity_id = self._activity_for_topic(tid)

        result = get_logs("activity", activity_id)

        assert "error" not in result
        assert len(result["logs"]) == 30
        assert result["total_count"] == 40
        assert result["truncated"] is True

    def test_no_related_topics_returns_zero_total_count(self, temp_db):
        act = add_activity(
            title="[作業] タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False
        )

        result = get_logs("activity", act["activity_id"])

        assert "error" not in result
        assert result["logs"] == []
        assert result["total_count"] == 0
        assert result["truncated"] is False

    def test_multiple_topics_total_count_deduplicated(self, temp_db):
        """複数topicにbelongs_toするlogが重複カウントされない"""
        t1 = add_topic(title="トピック1", description="Desc", tags=DEFAULT_TAGS)
        t2 = add_topic(title="トピック2", description="Desc", tags=DEFAULT_TAGS)
        add_log(topic_id=t1["topic_id"], content="T1ログ")
        add_log(topic_id=t2["topic_id"], content="T2ログ")
        act = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation(
            "activity",
            act["activity_id"],
            [{"type": "topic", "ids": [t1["topic_id"], t2["topic_id"]]}],
        )

        result = get_logs("activity", act["activity_id"])

        assert "error" not in result
        assert len(result["logs"]) == 2
        assert result["total_count"] == 2
        assert result["truncated"] is False

    def test_retracted_excluded_from_total_count_by_default(self, topic):
        tid = topic["topic_id"]
        removed = add_log(topic_id=tid, content="取り消されるログ")
        add_log(topic_id=tid, content="残るログ")
        retract("log", [removed["log_id"]])
        activity_id = self._activity_for_topic(tid)

        result = get_logs("activity", activity_id)

        assert "error" not in result
        assert len(result["logs"]) == 1
        assert result["total_count"] == 1
        assert result["truncated"] is False

    def test_truncated_false_on_last_page_with_start_id(self, topic):
        """activity（id DESC）で start_id 指定の最終ページ、後続が無ければ truncated は False"""
        tid = topic["topic_id"]
        logs = [add_log(topic_id=tid, content=f"ログ{i}") for i in range(5)]
        activity_id = self._activity_for_topic(tid)
        # DESC 順: [4],[3],[2],[1],[0]。start_id=logs[2].id で id<=2 → [2],[1],[0] の3件
        result = get_logs("activity", activity_id, start_id=logs[2]["log_id"], limit=3)
        assert len(result["logs"]) == 3
        assert result["total_count"] == 5
        assert result["truncated"] is False

    def test_truncated_true_when_more_after_start_id_page(self, topic):
        """activity（id DESC）で start_id 指定、後続が残れば truncated は True"""
        tid = topic["topic_id"]
        logs = [add_log(topic_id=tid, content=f"ログ{i}") for i in range(5)]
        activity_id = self._activity_for_topic(tid)
        result = get_logs("activity", activity_id, start_id=logs[3]["log_id"], limit=2)
        assert len(result["logs"]) == 2
        assert result["total_count"] == 5
        assert result["truncated"] is True
