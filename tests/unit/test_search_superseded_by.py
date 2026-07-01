"""search 結果の decision に superseded_by が付与されることを検証する"""
import hashlib
import os
import tempfile

import numpy as np
import pytest

import src.services.embedding_service as emb
from src.db import init_database
from src.services import search_service
from src.services.relation_service import add_relation
from src.services.tag_service import _injected_tags
from src.services.topic_service import add_topic
from tests.helpers import add_decision


EMBEDDING_DIM = 384
DEFAULT_TAGS = ["domain:test"]


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
def mock_embedding_model(monkeypatch):
    def mock_encode_batch(texts, prefix):
        embeddings = []
        for text in texts:
            prefix_str = "検索文書: " if prefix == "document" else "検索クエリ: "
            seed = int(hashlib.sha256((prefix_str + text).encode()).hexdigest(), 16) % (2**32)
            np.random.seed(seed)
            embeddings.append(np.random.rand(EMBEDDING_DIM).astype(np.float32).tolist())
        return embeddings

    monkeypatch.setattr(emb, "_encode_batch", mock_encode_batch)
    monkeypatch.setattr(emb, "_server_initialized", True)
    monkeypatch.setattr(emb, "_backfill_done", True)
    yield


def _find_decision(results: list[dict], id_raw: int) -> dict:
    for item in results:
        if item["type"] == "decision" and item.get("id_raw") == id_raw:
            return item
    raise AssertionError(f"decision id_raw={id_raw} not found in results")


def test_search_decision_superseded_by_is_none_when_not_superseded(temp_db, mock_embedding_model):
    """未 supersede の decision は superseded_by=None"""
    topic = add_topic(title="スーパーシード検索用トピック", description="Desc", tags=DEFAULT_TAGS)
    d = add_decision(decision="スーパーシード検索対象決定", reason="理由", topic_id=topic["topic_id"])

    result = search_service.search(keyword="スーパーシード検索対象決定", entity_type="decision")

    assert "error" not in result
    item = _find_decision(result["results"], d["decision_id"])
    assert "superseded_by" in item
    assert item["superseded_by"] is None


def test_search_decision_superseded_by_returns_source_id(temp_db, mock_embedding_model):
    """supersede されている decision は最新 superseder id を返す"""
    topic = add_topic(title="スーパーシード検索用トピック", description="Desc", tags=DEFAULT_TAGS)
    d_old = add_decision(
        decision="スーパーシード検索対象古い決定",
        reason="古い理由",
        topic_id=topic["topic_id"],
    )
    d_new = add_decision(decision="新しい決定", reason="新しい理由", topic_id=topic["topic_id"])
    add_relation(
        "decision", d_new["decision_id"],
        [{"type": "decision", "ids": [d_old["decision_id"]]}],
        relation_type="supersedes",
    )

    result = search_service.search(
        keyword="スーパーシード検索対象古い決定", entity_type="decision"
    )

    assert "error" not in result
    item = _find_decision(result["results"], d_old["decision_id"])
    assert item["superseded_by"] == d_new["decision_id"]


def test_search_non_decision_results_have_no_superseded_by_field(temp_db, mock_embedding_model):
    """topic など decision 以外の結果には superseded_by は付かない"""
    add_topic(
        title="トピック側のみのスーパーシードテスト用",
        description="Desc",
        tags=DEFAULT_TAGS,
    )

    result = search_service.search(keyword="トピック側のみのスーパーシードテスト用")

    assert "error" not in result
    for item in result["results"]:
        if item["type"] != "decision":
            assert "superseded_by" not in item
