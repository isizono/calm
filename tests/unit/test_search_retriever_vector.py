"""vector_retrieve retriever 単体テスト。

embedding サーバー失敗時の None 返却と、共有 conn の使用を確認する。
実際のベクトル検索結果は test_hybrid_search 側の統合テストで担保しているため、
ここでは retriever のシグネチャ・null フォールバック・例外ハンドリングに焦点を当てる。
"""
import hashlib
import os
import tempfile

import numpy as np
import pytest

import src.services.embedding_service as emb
from src.db import get_connection, init_database
from src.services import search_service
from src.services.search_service import vector_retrieve
from src.services.topic_service import add_topic
from tests.helpers import make_search_context as _make_ctx

EMBEDDING_DIM = 384
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


@pytest.fixture
def disable_embedding(monkeypatch):
    monkeypatch.setattr(emb, "_server_initialized", False)
    monkeypatch.setattr(emb, "_backfill_done", True)
    monkeypatch.setattr(emb, "_ensure_server_running", lambda: False)


def test_vector_retrieve_returns_none_when_embedding_disabled(temp_db, disable_embedding):
    """encode_query が None を返すと (= 埋め込みサーバー未稼働) vector_retrieve は None。"""
    add_topic(title="alpha topic", description="hello", tags=DEFAULT_TAGS)

    conn = get_connection()
    try:
        ctx = _make_ctx(keywords=("alpha",), fts_keywords=("alpha",))
        result = vector_retrieve(ctx, conn)
    finally:
        conn.close()

    assert result is None


def test_vector_retrieve_uses_shared_conn(temp_db, mock_embedding_model, monkeypatch):
    """vector_retrieve は共有 conn を使い、自前で get_connection() を呼ばない。"""
    add_topic(title="alpha topic", description="hello world", tags=DEFAULT_TAGS)

    call_count = {"n": 0}
    real_get_connection = search_service.get_connection

    def tracking_get_connection():
        call_count["n"] += 1
        return real_get_connection()

    monkeypatch.setattr(search_service, "get_connection", tracking_get_connection)

    conn = real_get_connection()
    try:
        ctx = _make_ctx(keywords=("alpha",), fts_keywords=("alpha",))
        vector_retrieve(ctx, conn)
    finally:
        conn.close()

    assert call_count["n"] == 0


def test_vector_retrieve_returns_list_when_embedding_available(temp_db, mock_embedding_model):
    """埋め込みサーバー稼働時は AND モードで list を返す（None ではない）。"""
    add_topic(title="alpha topic", description="hello world", tags=DEFAULT_TAGS)

    conn = get_connection()
    try:
        ctx = _make_ctx(keywords=("alpha",), fts_keywords=("alpha",))
        result = vector_retrieve(ctx, conn)
    finally:
        conn.close()

    assert isinstance(result, list)


def test_vector_retrieve_or_mode_merges_per_keyword(temp_db, mock_embedding_model):
    """OR モードでは各キーワードを個別に埋め込み → 結果をマージして返す。"""
    add_topic(title="alpha topic", description="hello", tags=DEFAULT_TAGS)
    add_topic(title="beta topic", description="world", tags=DEFAULT_TAGS)

    conn = get_connection()
    try:
        ctx = _make_ctx(
            keywords=("alpha", "beta"),
            fts_keywords=("alpha", "beta"),
            keyword_mode="or",
        )
        result = vector_retrieve(ctx, conn)
    finally:
        conn.close()

    # OR モードで両 keyword に対応する KNN を回すので None ではないはず
    assert result is not None
    assert isinstance(result, list)
