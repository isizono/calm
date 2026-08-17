"""nearby_tags（タグ共起サジェスト）のテスト

_compute_nearby_tags単体テスト + search統合テスト。
"""
import hashlib
import os
import tempfile

import numpy as np
import pytest

from src.db import init_database, get_connection
from src.services.search_service import (
    _compute_nearby_tags,
    NEARBY_TAGS_LIMIT,
)
from src.services import search_service
from src.services.topic_service import add_topic
from src.services.activity_service import add_activity
from tests.helpers import add_log, add_decision
from src.services.material_service import add_material
import src.services.embedding_service as emb


EMBEDDING_DIM = 384


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
def mock_embedding_model(monkeypatch):
    """embedding_serverへのHTTPリクエストをモック化"""

    def mock_encode_batch(texts, prefix):
        embeddings = []
        for text in texts:
            prefix_str = "検索文書: " if prefix == "document" else "検索クエリ: "
            seed = int(hashlib.sha256((prefix_str + text).encode()).hexdigest(), 16) % (2**32)
            np.random.seed(seed)
            embeddings.append(np.random.rand(EMBEDDING_DIM).astype(np.float32).tolist())
        return embeddings

    monkeypatch.setattr(emb, '_encode_batch', mock_encode_batch)
    monkeypatch.setattr(emb, '_server_initialized', True)
    monkeypatch.setattr(emb, '_backfill_done', True)


@pytest.fixture
def disable_embedding(monkeypatch):
    """embeddingサービスを無効化"""
    monkeypatch.setattr(emb, '_server_initialized', False)
    monkeypatch.setattr(emb, '_backfill_done', True)
    monkeypatch.setattr(emb, '_ensure_server_running', lambda: False)


# ========================================
# _compute_nearby_tags 単体テスト
# ========================================


def test_compute_nearby_tags_basic(temp_db):
    """共起タグが正しく返される基本ケース"""
    # topic1: tags=[alpha, beta, gamma]
    # topic2: tags=[alpha, delta]
    # 検索結果にalphaが含まれる → beta, gamma, deltaが共起候補
    add_topic(title="T1", description="test", tags=["alpha", "beta", "gamma"])
    add_topic(title="T2", description="test", tags=["alpha", "delta"])

    results = [
        {"type": "topic", "id": 1, "tags": ["alpha"]},
    ]
    nearby = _compute_nearby_tags(results, None, 0)

    tag_names = [n["tag"] for n in nearby]
    assert "beta" in tag_names
    assert "gamma" in tag_names
    assert "delta" in tag_names
    assert "alpha" not in tag_names  # 入力タグは除外


def test_compute_nearby_tags_excludes_namespace(temp_db):
    """domain:/intent:タグはnearby_tagsから除外される"""
    add_topic(title="T1", description="test", tags=["alpha", "domain:calm", "intent:design"])

    results = [
        {"type": "topic", "id": 1, "tags": ["alpha"]},
    ]
    nearby = _compute_nearby_tags(results, None, 0)

    tag_names = [n["tag"] for n in nearby]
    assert "domain:calm" not in tag_names
    assert "intent:design" not in tag_names


def test_compute_nearby_tags_excludes_query_tags(temp_db):
    """検索フィルタに使用されたタグはnearby_tagsから除外される"""
    add_topic(title="T1", description="test", tags=["alpha", "beta", "gamma"])

    # betaのtag_idを取得
    conn = get_connection()
    row = conn.execute("SELECT id FROM tags WHERE namespace = '' AND name = 'beta'").fetchone()
    beta_tag_id = row["id"]
    conn.close()

    results = [
        {"type": "topic", "id": 1, "tags": ["alpha"]},
    ]
    nearby = _compute_nearby_tags(results, [beta_tag_id], 0)

    tag_names = [n["tag"] for n in nearby]
    assert "beta" not in tag_names  # クエリタグは除外
    assert "gamma" in tag_names


def test_compute_nearby_tags_empty_results(temp_db):
    """結果0件のときは空配列"""
    nearby = _compute_nearby_tags([], None, 0)
    assert nearby == []


def test_compute_nearby_tags_offset_skip(temp_db):
    """offset>0のときは空配列"""
    add_topic(title="T1", description="test", tags=["alpha", "beta"])

    results = [
        {"type": "topic", "id": 1, "tags": ["alpha"]},
    ]
    nearby = _compute_nearby_tags(results, None, 1)
    assert nearby == []


def test_compute_nearby_tags_all_namespace_tags(temp_db):
    """結果のタグが全てnamespace付きの場合、共起候補もnamespace付きなら空配列"""
    add_topic(title="T1", description="test", tags=["domain:calm", "intent:design"])

    results = [
        {"type": "topic", "id": 1, "tags": ["domain:calm"]},
    ]
    nearby = _compute_nearby_tags(results, None, 0)

    # intent:designは除外されるので空
    tag_names = [n["tag"] for n in nearby]
    assert "intent:design" not in tag_names


def test_compute_nearby_tags_co_count_order(temp_db):
    """co_count降順でソートされる"""
    # beta: topic1, topic2, topic3 に共起 → co_count=3
    # gamma: topic1 にのみ共起 → co_count=1
    add_topic(title="T1", description="test", tags=["alpha", "beta", "gamma"])
    add_topic(title="T2", description="test", tags=["alpha", "beta"])
    add_topic(title="T3", description="test", tags=["alpha", "beta"])

    results = [
        {"type": "topic", "id": 1, "tags": ["alpha"]},
        {"type": "topic", "id": 2, "tags": ["alpha"]},
        {"type": "topic", "id": 3, "tags": ["alpha"]},
    ]
    nearby = _compute_nearby_tags(results, None, 0)

    assert len(nearby) >= 2
    # betaがgammaより上位
    beta_idx = next(i for i, n in enumerate(nearby) if n["tag"] == "beta")
    gamma_idx = next(i for i, n in enumerate(nearby) if n["tag"] == "gamma")
    assert beta_idx < gamma_idx
    assert nearby[beta_idx]["co_count"] > nearby[gamma_idx]["co_count"]


def test_compute_nearby_tags_cross_table(temp_db):
    """複数テーブルにまたがる共起がSUMされる"""
    # alphaとbetaの共起: topic_tags + activity_tags
    topic = add_topic(title="T1", description="test", tags=["alpha", "beta"])
    add_activity(title="A1", description="test", tags=["alpha", "beta"], check_in=False)

    results = [
        {"type": "topic", "id": topic["topic_id"], "tags": ["alpha"]},
    ]
    nearby = _compute_nearby_tags(results, None, 0)

    beta_entry = next((n for n in nearby if n["tag"] == "beta"), None)
    assert beta_entry is not None
    assert beta_entry["co_count"] >= 2  # topic_tags + activity_tags


def test_compute_nearby_tags_limit(temp_db):
    """NEARBY_TAGS_LIMITを超えない"""
    # 大量のタグを作成
    tags = ["alpha"] + [f"tag{i}" for i in range(NEARBY_TAGS_LIMIT + 5)]
    add_topic(title="T1", description="test", tags=tags)

    results = [
        {"type": "topic", "id": 1, "tags": ["alpha"]},
    ]
    nearby = _compute_nearby_tags(results, None, 0)

    assert len(nearby) <= NEARBY_TAGS_LIMIT


# ========================================
# search統合テスト
# ========================================


def test_search_returns_nearby_tags(temp_db, mock_embedding_model):
    """searchレスポンスにnearby_tagsフィールドが含まれる"""
    add_topic(title="nearby検索テスト対象", description="テスト用", tags=["search-test", "extra-tag"])
    add_topic(title="nearby検索テスト関連", description="テスト用", tags=["search-test", "related-tag"])

    result = search_service.search(keyword="nearby検索テスト")

    assert "error" not in result
    assert "nearby_tags" in result
    assert isinstance(result["nearby_tags"], list)


def test_search_nearby_tags_structure(temp_db, mock_embedding_model):
    """nearby_tagsの各要素がtag/co_countを持つ"""
    # 検索結果のタグと共起するが結果には含まれないタグを作る
    add_topic(title="構造テスト対象", description="構造テスト用", tags=["struct-tag"])
    # struct-tagと共起するタグを別トピックに作成（検索にはヒットしない）
    add_topic(title="ZZZZZZ", description="ZZZZZZ", tags=["struct-tag", "nearby-candidate"])

    result = search_service.search(keyword="構造テスト対象")

    assert "error" not in result
    for entry in result["nearby_tags"]:
        assert "tag" in entry
        assert "co_count" in entry
        assert isinstance(entry["tag"], str)
        assert isinstance(entry["co_count"], int)
        assert entry["co_count"] > 0


def test_search_nearby_tags_with_offset(temp_db, mock_embedding_model):
    """offset>0のときnearby_tagsは空配列"""
    add_topic(title="offsetテスト用トピック", description="テスト", tags=["offset-test", "some-tag"])

    result = search_service.search(keyword="offsetテスト用", offset=1)

    assert "error" not in result
    assert result["nearby_tags"] == []


def test_search_nearby_tags_with_tag_filter(temp_db, mock_embedding_model):
    """tagsフィルタで絞った場合もnearby_tagsが返る（フィルタタグは除外）"""
    add_topic(
        title="フィルタnearbyテスト",
        description="テスト",
        tags=["domain:test", "filter-nearby", "extra-nearby"],
    )

    result = search_service.search(keyword="フィルタnearbyテスト", tags=["domain:test"])

    assert "error" not in result
    assert "nearby_tags" in result
    nearby_tag_names = [n["tag"] for n in result["nearby_tags"]]
    assert "domain:test" not in nearby_tag_names  # フィルタタグは除外


def test_search_nearby_tags_empty_results(temp_db, mock_embedding_model):
    """検索結果0件のときnearby_tagsは空配列"""
    result = search_service.search(keyword="存在しないキーワードXYZ")

    assert "error" not in result
    assert result["nearby_tags"] == []
