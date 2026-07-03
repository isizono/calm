"""get_decisions / get_by_ids / search における precedent 付与のテスト。

定型節（却下案:/適用条件:/適用外:/検証:）を持つ decision の reason から
precedent コンパクト形（`src.services.precedent_pure.summarize_precedent`）が
読み出し面に付与されることを検証する。節が無い decision にはキーが付かないこと、
search() の discovery 面には付与されないことも合わせて検証する。
"""
import os
import tempfile

import pytest

from src.db import init_database
from src.services import search_service
from src.services.decision_service import get_decisions
from src.services.topic_service import add_topic
import src.services.embedding_service as emb
from tests.helpers import add_decision

DEFAULT_TAGS = ["domain:test"]

PRECEDENT_REASON = (
    "自由記述の理由。\n"
    "\n"
    "却下案:\n"
    "- 案A: 理由A\n"
    "\n"
    "適用外:\n"
    "- 除外領域\n"
    "\n"
    "検証: 実機確認 / 2026-07-04\n"
)

PLAIN_REASON = "普通の理由本文。節は無い。"


@pytest.fixture(autouse=True)
def disable_embedding(monkeypatch):
    """precedent 読み出しのテストでは embedding サービスを無効化する。"""
    monkeypatch.setattr(emb, '_server_initialized', False)
    monkeypatch.setattr(emb, '_backfill_done', True)
    monkeypatch.setattr(emb, '_ensure_server_running', lambda: False)


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


@pytest.fixture
def topic(temp_db):
    return add_topic(
        title="判例テストトピック", description="precedent読み出しテスト用", tags=DEFAULT_TAGS
    )


class TestGetDecisionsPrecedent:
    def test_decision_with_sections_gets_precedent(self, topic):
        add_decision("採用する", PRECEDENT_REASON, topic_id=topic["topic_id"], tags=DEFAULT_TAGS)
        result = get_decisions("topic", topic["topic_id"])
        assert "error" not in result
        assert len(result["decisions"]) == 1
        item = result["decisions"][0]
        assert item["precedent"] == {
            "rejected_alternatives": 1,
            "scope": True,
            "verification_anchors": ["実機確認 / 2026-07-04"],
        }

    def test_decision_without_sections_has_no_precedent_key(self, topic):
        add_decision("採用する", PLAIN_REASON, topic_id=topic["topic_id"], tags=DEFAULT_TAGS)
        result = get_decisions("topic", topic["topic_id"])
        item = result["decisions"][0]
        assert "precedent" not in item

    def test_existing_fields_unchanged(self, topic):
        add_decision("採用する", PRECEDENT_REASON, topic_id=topic["topic_id"], tags=DEFAULT_TAGS)
        result = get_decisions("topic", topic["topic_id"])
        item = result["decisions"][0]
        assert item["is_superseded"] is False
        assert item["is_retracted"] is False
        assert item["supersede_chain"] == [item["id_raw"]]
        assert item["tags"] == DEFAULT_TAGS

    def test_activity_entity_type_also_gets_precedent(self, topic):
        from src.services.activity_service import add_activity
        from src.services.relation_service import add_relation

        activity = add_activity(
            title="判例テストアクティビティ", description="precedent読み出しテスト用",
            tags=DEFAULT_TAGS, check_in=False,
        )
        add_relation(
            "activity", activity["activity_id"],
            [{"type": "topic", "ids": [topic["topic_id"]]}],
        )
        add_decision("採用する", PRECEDENT_REASON, topic_id=topic["topic_id"], tags=DEFAULT_TAGS)
        result = get_decisions("activity", activity["activity_id"])
        assert "error" not in result
        assert len(result["decisions"]) == 1
        assert result["decisions"][0]["precedent"]["rejected_alternatives"] == 1


class TestGetByIdsPrecedent:
    def test_decision_with_sections_gets_precedent(self, topic):
        created = add_decision(
            "採用する", PRECEDENT_REASON, topic_id=topic["topic_id"], tags=DEFAULT_TAGS
        )
        result = search_service.get_by_ids(
            [{"type": "decision", "id": created["decision_id"]}]
        )
        assert "error" not in result
        data = result["results"][0]["data"]
        assert data["precedent"] == {
            "rejected_alternatives": 1,
            "scope": True,
            "verification_anchors": ["実機確認 / 2026-07-04"],
        }

    def test_decision_without_sections_has_no_precedent_key(self, topic):
        created = add_decision(
            "採用する", PLAIN_REASON, topic_id=topic["topic_id"], tags=DEFAULT_TAGS
        )
        result = search_service.get_by_ids(
            [{"type": "decision", "id": created["decision_id"]}]
        )
        data = result["results"][0]["data"]
        assert "precedent" not in data

    def test_non_decision_types_unaffected(self, topic):
        result = search_service.get_by_ids(
            [{"type": "topic", "id": topic["topic_id"]}]
        )
        assert "error" not in result
        data = result["results"][0]["data"]
        assert "precedent" not in data


class TestSearchDoesNotIncludePrecedent:
    def test_search_result_has_no_precedent_key(self, topic):
        add_decision(
            "採用テスト決定事項キーワード", PRECEDENT_REASON,
            topic_id=topic["topic_id"], tags=DEFAULT_TAGS,
        )
        result = search_service.search(
            keyword="採用テスト決定事項キーワード", entity_type="decision"
        )
        assert "error" not in result
        assert len(result["results"]) >= 1
        for r in result["results"]:
            assert "precedent" not in r

    def test_search_include_details_has_no_precedent_key(self, topic):
        add_decision(
            "採用テスト決定事項詳細キーワード", PRECEDENT_REASON,
            topic_id=topic["topic_id"], tags=DEFAULT_TAGS,
        )
        result = search_service.search(
            keyword="採用テスト決定事項詳細キーワード",
            entity_type="decision",
            include_details=True,
        )
        assert "error" not in result
        assert len(result["results"]) >= 1
        for r in result["results"]:
            assert "precedent" not in r
            if "details" in r:
                assert "precedent" not in r["details"]
