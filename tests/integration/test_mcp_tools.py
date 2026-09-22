"""MCPツールのテスト（subjects廃止後）"""
import pytest
from src.services.topic_service import add_topic
from src.services.activity_service import add_activity
from src.services.discussion_log_service import add_logs
from src.services.decision_service import add_decisions
from src.services.timeline_service import get_timeline



def test_add_topic_with_tags(temp_db):
    """タグ付きでトピックの追加が成功する"""
    result = add_topic(
        title="test-topic",
        description="テストトピック",
        tags=["domain:test"],
    )

    assert "error" not in result
    assert result["topic_id"] > 0


def test_add_topic_tags_required(temp_db):
    """タグなしでトピック追加するとTAGS_REQUIREDエラーが返る"""
    result = add_topic(
        title="test-topic",
        description="テストトピック",
        tags=[],
    )

    assert "error" in result
    assert result["error"]["code"] == "TAGS_REQUIRED"


def test_add_activity_with_tags(temp_db):
    """タグ付きでアクティビティの追加が成功する"""
    result = add_activity(
        title="test-activity",
        description="テストアクティビティ",
        tags=["domain:test"],
        check_in=False,
    )

    assert "error" not in result
    assert result["activity_id"] > 0


def test_add_activity_tags_required(temp_db):
    """タグなしでアクティビティ追加するとTAGS_REQUIREDエラーが返る"""
    result = add_activity(
        title="test-activity",
        description="テストアクティビティ",
        tags=[],
    )

    assert "error" in result
    assert result["error"]["code"] == "TAGS_REQUIRED"


def test_get_timeline_with_topic(temp_db):
    """get_timelineがtopic_id指定でlogs・decisionsを時系列混合で返す"""
    topic = add_topic(title="timeline-test", description="テスト", tags=["domain:test"])
    tid = topic["topic_id"]

    add_logs([{"topic_id": tid, "content": "ログ内容", "title": "テストログ"}])
    add_decisions([{"topic_id": tid, "decision": "テスト決定", "reason": "理由"}])

    result = get_timeline(topic_id=tid)

    assert "error" not in result
    assert result["total"] == 2
    assert len(result["items"]) == 2
    types = {item["type"] for item in result["items"]}
    assert types == {"log", "decision"}


def test_get_timeline_validation_error(temp_db):
    """get_timelineがtopic_id・activity_id両方なしでバリデーションエラーを返す"""
    result = get_timeline()
    assert "error" in result
    assert result["error"]["code"] == "VALIDATION_ERROR"
