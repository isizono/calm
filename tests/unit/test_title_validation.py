"""title 長さ validation の単体テスト + 各 service への結合テスト。

helper の境界値テストと、6 つの add/update 系サービスが 40 字超で
VALIDATION_ERROR を返すことを確認する。
"""
import os
import tempfile

import pytest

from src.db import init_database
from src.services.activity_service import add_activity, update_activity
from src.services.decision_service import add_decisions
from src.services.material_service import add_material, update_material
from src.services.title_validation import TITLE_MAX_LEN, validate_title
from src.services.topic_service import add_topic


DEFAULT_TAGS = ["domain:test"]


@pytest.fixture
def temp_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


class TestValidateTitleHelper:
    def test_none_skipped(self):
        assert validate_title(None) is None

    def test_exactly_max_passes(self):
        assert validate_title("a" * TITLE_MAX_LEN) is None

    def test_over_max_rejected(self):
        err = validate_title("a" * (TITLE_MAX_LEN + 1))
        assert err is not None
        assert err["error"]["code"] == "VALIDATION_ERROR"
        assert str(TITLE_MAX_LEN) in err["error"]["message"]


class TestAddTopicTitleLength:
    def test_long_title_rejected(self, temp_db):
        result = add_topic(
            title="a" * (TITLE_MAX_LEN + 1),
            description="desc",
            tags=DEFAULT_TAGS,
        )
        assert result.get("error", {}).get("code") == "VALIDATION_ERROR"

    def test_max_title_passes(self, temp_db):
        result = add_topic(
            title="a" * TITLE_MAX_LEN,
            description="desc",
            tags=DEFAULT_TAGS,
        )
        assert "error" not in result


class TestAddActivityTitleLength:
    def test_long_title_rejected(self, temp_db):
        result = add_activity(
            title="a" * (TITLE_MAX_LEN + 1),
            description="desc",
            tags=DEFAULT_TAGS,
        )
        assert result.get("error", {}).get("code") == "VALIDATION_ERROR"


class TestUpdateActivityTitleLength:
    def test_long_title_rejected(self, temp_db):
        topic = add_topic(title="t", description="d", tags=DEFAULT_TAGS)
        activity = add_activity(
            title="orig",
            description="d",
            tags=DEFAULT_TAGS,
            related=[{"type": "topic", "ids": [topic["topic_id"]]}],
        )
        result = update_activity(
            activity_id=activity["activity_id"],
            title="a" * (TITLE_MAX_LEN + 1),
        )
        assert result.get("error", {}).get("code") == "VALIDATION_ERROR"

    def test_none_title_skipped(self, temp_db):
        topic = add_topic(title="t", description="d", tags=DEFAULT_TAGS)
        activity = add_activity(
            title="orig",
            description="d",
            tags=DEFAULT_TAGS,
            related=[{"type": "topic", "ids": [topic["topic_id"]]}],
        )
        result = update_activity(
            activity_id=activity["activity_id"],
            status="in_progress",
        )
        assert "error" not in result


class TestAddDecisionsTitleLength:
    def test_long_title_rejected(self, temp_db):
        topic = add_topic(title="t", description="d", tags=DEFAULT_TAGS)
        result = add_decisions(
            items=[
                {
                    "topic_id": topic["topic_id"],
                    "decision": "x",
                    "reason": "y",
                    "title": "a" * (TITLE_MAX_LEN + 1),
                }
            ]
        )
        assert result["errors"]
        # items ループ内の error は全 ITEM_ERROR に分類される既存設計
        assert result["errors"][0]["error"]["code"] == "ITEM_ERROR"
        assert "exceeds maximum" in result["errors"][0]["error"]["message"]

    def test_none_title_passes(self, temp_db):
        topic = add_topic(title="t", description="d", tags=DEFAULT_TAGS)
        result = add_decisions(
            items=[
                {
                    "topic_id": topic["topic_id"],
                    "decision": "x",
                    "reason": "y",
                }
            ]
        )
        assert not result["errors"]


class TestAddMaterialTitleLength:
    def test_long_title_rejected(self, temp_db):
        result = add_material(
            title="a" * (TITLE_MAX_LEN + 1),
            content="content",
            tags=DEFAULT_TAGS,
            source="test",
        )
        assert result.get("error", {}).get("code") == "VALIDATION_ERROR"


class TestUpdateMaterialTitleLength:
    def test_long_title_rejected(self, temp_db):
        material = add_material(
            title="orig",
            content="c",
            tags=DEFAULT_TAGS,
            source="test",
        )
        result = update_material(
            material_id=material["material_id"],
            title="a" * (TITLE_MAX_LEN + 1),
        )
        assert result.get("error", {}).get("code") == "VALIDATION_ERROR"
