"""get_decisions の total_count / truncated フィールドのテスト

LIMIT 30 による黙示的な切り捨てを応答フィールドで可視化する拡張の検証。
"""
import os
import tempfile
import pytest

from src.db import init_database
from src.services.activity_service import add_activity
from src.services.topic_service import add_topic
from src.services.relation_service import add_relation
from src.services.decision_service import get_decisions
from src.services.retract_service import retract
from tests.helpers import add_decision
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


class TestGetDecisionsTotalCountTruncatedTopic:
    """get_decisions(entity_type="topic") の total_count / truncated"""

    def test_under_limit_not_truncated(self, topic):
        """decisionが30件以下ならtotal_countは実件数と一致し、truncatedはFalse"""
        tid = topic["topic_id"]
        for i in range(3):
            add_decision(decision=f"決定{i}", reason=f"理由{i}", topic_id=tid)

        result = get_decisions("topic", tid)

        assert "error" not in result
        assert len(result["decisions"]) == 3
        assert result["total_count"] == 3
        assert result["truncated"] is False

    def test_over_limit_is_truncated(self, topic):
        """decisionが30件を超えると応答はlimit件までだがtotal_countは実件数、truncatedはTrue"""
        tid = topic["topic_id"]
        for i in range(40):
            add_decision(decision=f"決定{i}", reason=f"理由{i}", topic_id=tid)

        result = get_decisions("topic", tid)

        assert "error" not in result
        assert len(result["decisions"]) == 30
        assert result["total_count"] == 40
        assert result["truncated"] is True

    def test_retracted_excluded_from_total_count_by_default(self, topic):
        """デフォルト（include_retracted=False）ではtotal_countもretract済みを除外する"""
        tid = topic["topic_id"]
        kept = add_decision(decision="残る決定", reason="理由", topic_id=tid)
        removed = add_decision(decision="取り消される決定", reason="理由", topic_id=tid)
        retract("decision", [removed["decision_id"]])

        result = get_decisions("topic", tid)

        assert "error" not in result
        assert len(result["decisions"]) == 1
        assert result["total_count"] == 1
        assert result["truncated"] is False

    def test_retracted_included_in_total_count_when_requested(self, topic):
        """include_retracted=Trueのときtotal_countもretract済みを含めて数える"""
        tid = topic["topic_id"]
        kept = add_decision(decision="残る決定", reason="理由", topic_id=tid)
        removed = add_decision(decision="取り消される決定", reason="理由", topic_id=tid)
        retract("decision", [removed["decision_id"]])

        result = get_decisions("topic", tid, include_retracted=True)

        assert "error" not in result
        assert len(result["decisions"]) == 2
        assert result["total_count"] == 2
        assert result["truncated"] is False

    def test_nonexistent_topic_returns_zero_total_count(self, temp_db):
        """存在しないtopic_idの場合、total_count=0, truncated=Falseが返る"""
        result = get_decisions("topic", 999999)

        assert "error" not in result
        assert result["decisions"] == []
        assert result["total_count"] == 0
        assert result["truncated"] is False

    def test_existing_fields_unaffected_by_extension(self, topic):
        """既存フィールド（topic_id/topic_name/decisions本文）は拡張後も従来通り返る"""
        tid = topic["topic_id"]
        add_decision(decision="決定1", reason="理由1", topic_id=tid)

        result = get_decisions("topic", tid)

        assert result["topic_id"] == tid
        assert result["topic_name"] == "テストトピック"
        assert len(result["decisions"]) == 1
        assert result["decisions"][0]["decision"] == "決定1"

    def test_pagination_start_id_and_limit_still_work(self, topic):
        """start_id/limitの既存ページネーション挙動は拡張後も変わらない"""
        tid = topic["topic_id"]
        decisions = [
            add_decision(decision=f"決定{i}", reason=f"理由{i}", topic_id=tid)
            for i in range(5)
        ]

        result1 = get_decisions("topic", tid, limit=3)
        assert len(result1["decisions"]) == 3
        assert result1["total_count"] == 5
        assert result1["truncated"] is True

        result2 = get_decisions(
            "topic", tid, start_id=decisions[3]["decision_id"], limit=3
        )
        assert len(result2["decisions"]) == 2
        assert result2["decisions"][0]["id_raw"] == decisions[3]["decision_id"]


class TestGetDecisionsTotalCountTruncatedActivity:
    """get_decisions(entity_type="activity") の total_count / truncated"""

    def _activity_for_topic(self, topic_id: int) -> int:
        act = add_activity(
            title="[作業] タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False
        )
        add_relation("activity", act["activity_id"], [{"type": "topic", "ids": [topic_id]}])
        return act["activity_id"]

    def test_under_limit_not_truncated(self, topic):
        tid = topic["topic_id"]
        for i in range(3):
            add_decision(decision=f"決定{i}", reason=f"理由{i}", topic_id=tid)
        activity_id = self._activity_for_topic(tid)

        result = get_decisions("activity", activity_id)

        assert "error" not in result
        assert len(result["decisions"]) == 3
        assert result["total_count"] == 3
        assert result["truncated"] is False

    def test_over_limit_is_truncated(self, topic):
        tid = topic["topic_id"]
        for i in range(40):
            add_decision(decision=f"決定{i}", reason=f"理由{i}", topic_id=tid)
        activity_id = self._activity_for_topic(tid)

        result = get_decisions("activity", activity_id)

        assert "error" not in result
        assert len(result["decisions"]) == 30
        assert result["total_count"] == 40
        assert result["truncated"] is True

    def test_no_related_topics_returns_zero_total_count(self, temp_db):
        act = add_activity(
            title="[作業] タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False
        )

        result = get_decisions("activity", act["activity_id"])

        assert "error" not in result
        assert result["decisions"] == []
        assert result["total_count"] == 0
        assert result["truncated"] is False

    def test_multiple_topics_total_count_deduplicated(self, temp_db):
        """複数topicにbelongs_toするdecisionが重複カウントされない"""
        t1 = add_topic(title="トピック1", description="Desc", tags=DEFAULT_TAGS)
        t2 = add_topic(title="トピック2", description="Desc", tags=DEFAULT_TAGS)
        add_decision(decision="T1決定", reason="理由", topic_id=t1["topic_id"])
        add_decision(decision="T2決定", reason="理由", topic_id=t2["topic_id"])
        act = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation(
            "activity",
            act["activity_id"],
            [{"type": "topic", "ids": [t1["topic_id"], t2["topic_id"]]}],
        )

        result = get_decisions("activity", act["activity_id"])

        assert "error" not in result
        assert len(result["decisions"]) == 2
        assert result["total_count"] == 2
        assert result["truncated"] is False

    def test_retracted_excluded_from_total_count_by_default(self, topic):
        tid = topic["topic_id"]
        removed = add_decision(decision="取り消される決定", reason="理由", topic_id=tid)
        add_decision(decision="残る決定", reason="理由", topic_id=tid)
        retract("decision", [removed["decision_id"]])
        activity_id = self._activity_for_topic(tid)

        result = get_decisions("activity", activity_id)

        assert "error" not in result
        assert len(result["decisions"]) == 1
        assert result["total_count"] == 1
        assert result["truncated"] is False
