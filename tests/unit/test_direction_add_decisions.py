"""add_decisions の layer:direction 対応（title必須バリデーション + 既存active方向性decision提示）のテスト"""
import os
import tempfile

import pytest

from src.db import get_connection, init_database
from src.services.decision_service import add_decisions
from src.services.direction_service import DIRECTION_NAME, DIRECTION_NAMESPACE
from src.services.tag_service import _injected_tags
from src.services.topic_service import add_topic

DIRECTION_TAG = f"{DIRECTION_NAMESPACE}:{DIRECTION_NAME}"
DOMAIN_TAG = "domain:direction-add-test"


@pytest.fixture
def temp_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def topic_id(temp_db):
    t = add_topic(title="direction add テスト", description="Desc", tags=[DOMAIN_TAG])
    return t["topic_id"]


class TestTitleRequiredForDirection:
    def test_missing_title_errors(self, topic_id):
        result = add_decisions([
            {"topic_id": topic_id, "decision": "方向性なし", "reason": "r", "tags": [DIRECTION_TAG]},
        ])
        assert "error" not in result
        assert result["created"] == []
        assert len(result["errors"]) == 1
        assert result["errors"][0]["error"]["code"] == "ITEM_ERROR"

    def test_empty_title_errors(self, topic_id):
        """空白のみのtitleはNULLに正規化されるため、layer:directionではエラーになる"""
        result = add_decisions([
            {"topic_id": topic_id, "decision": "方向性なし", "reason": "r", "title": "   ",
             "tags": [DIRECTION_TAG]},
        ])
        assert result["created"] == []
        assert len(result["errors"]) == 1

    def test_with_title_succeeds(self, topic_id):
        result = add_decisions([
            {"topic_id": topic_id, "decision": "方向性あり", "reason": "r", "title": "要点",
             "tags": [DIRECTION_TAG]},
        ])
        assert "error" not in result
        assert not result["errors"]
        assert len(result["created"]) == 1

    def test_non_direction_item_title_still_optional(self, topic_id):
        """layer:directionが無い通常itemはtitle省略可能（既存挙動を壊さない）"""
        result = add_decisions([
            {"topic_id": topic_id, "decision": "通常決定", "reason": "r"},
        ])
        assert "error" not in result
        assert not result["errors"]
        assert len(result["created"]) == 1

    def test_partial_success_in_batch(self, topic_id):
        """バッチ内の1件がdirection title欠落エラーでも、他の正常itemはSAVEPOINTで独立して成功する"""
        result = add_decisions([
            {"topic_id": topic_id, "decision": "方向性なし", "reason": "r", "tags": [DIRECTION_TAG]},
            {"topic_id": topic_id, "decision": "通常決定", "reason": "r"},
        ])
        assert len(result["created"]) == 1
        assert len(result["errors"]) == 1
        assert result["errors"][0]["index"] == 0
        assert result["created"][0]["decision_id"] is not None


class TestExistingDirectionDecisionsResponse:
    def test_non_direction_item_has_no_direction_fields(self, topic_id):
        result = add_decisions([
            {"topic_id": topic_id, "decision": "通常決定", "reason": "r"},
        ])
        created = result["created"][0]
        assert "existing_direction_decisions" not in created
        assert "direction_note" not in created

    def test_first_direction_item_has_empty_existing_list(self, topic_id):
        result = add_decisions([
            {"topic_id": topic_id, "decision": "最初の方向性", "reason": "r", "title": "第1",
             "tags": [DIRECTION_TAG]},
        ])
        created = result["created"][0]
        assert created["existing_direction_decisions"] == []
        assert "direction_note" in created

    def test_second_direction_item_lists_first_excluding_self(self, topic_id):
        first = add_decisions([
            {"topic_id": topic_id, "decision": "最初の方向性", "reason": "r", "title": "第1",
             "tags": [DIRECTION_TAG]},
        ])
        first_id = first["created"][0]["decision_id"]

        second = add_decisions([
            {"topic_id": topic_id, "decision": "2番目の方向性", "reason": "r", "title": "第2",
             "tags": [DIRECTION_TAG]},
        ])
        created = second["created"][0]
        existing_ids = [d["id"] for d in created["existing_direction_decisions"]]
        assert existing_ids == [first_id]
        assert created["decision_id"] not in existing_ids
        assert "1件" in created["direction_note"]

    def test_existing_direction_decisions_scoped_to_domain(self, topic_id):
        """別domainの方向性decisionはexisting_direction_decisionsに現れない"""
        other_topic = add_topic(
            title="別domain方向性", description="d", tags=["domain:direction-add-other"],
        )
        add_decisions([
            {"topic_id": other_topic["topic_id"], "decision": "別domainの方向性", "reason": "r",
             "title": "別domain", "tags": [DIRECTION_TAG]},
        ])

        result = add_decisions([
            {"topic_id": topic_id, "decision": "対象domainの方向性", "reason": "r", "title": "対象",
             "tags": [DIRECTION_TAG]},
        ])
        assert result["created"][0]["existing_direction_decisions"] == []

    def test_direction_item_still_has_related_decisions(self, topic_id):
        """既存機構のrelated_decisionsキーもdirection itemに引き続き付く"""
        result = add_decisions([
            {"topic_id": topic_id, "decision": "方向性", "reason": "r", "title": "第1",
             "tags": [DIRECTION_TAG]},
        ])
        assert "related_decisions" in result["created"][0]

    def test_internal_flag_not_leaked_to_response(self, topic_id):
        """内部フラグ_is_direction_itemがレスポンスに漏れない"""
        result = add_decisions([
            {"topic_id": topic_id, "decision": "通常決定", "reason": "r"},
        ])
        assert "_is_direction_item" not in result["created"][0]

        direction_result = add_decisions([
            {"topic_id": topic_id, "decision": "方向性", "reason": "r", "title": "第1",
             "tags": [DIRECTION_TAG]},
        ])
        assert "_is_direction_item" not in direction_result["created"][0]
