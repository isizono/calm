"""tag_like_retrieve retriever 単体テスト。

キーワードを含むタグ名のエンティティが返ること、共有 conn が使われることを
直接シグネチャ経由で検証する。
"""
import os
import tempfile

import pytest

import src.services.embedding_service as emb
from src.db import get_connection, init_database
from src.services import search_service
from src.services.search_service import tag_like_retrieve
from src.services.topic_service import add_topic
from tests.helpers import make_search_context as _make_ctx


@pytest.fixture(autouse=True)
def disable_embedding(monkeypatch):
    monkeypatch.setattr(emb, "_server_initialized", False)
    monkeypatch.setattr(emb, "_backfill_done", True)
    monkeypatch.setattr(emb, "_ensure_server_running", lambda: False)


@pytest.fixture
def temp_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


def test_tag_like_retrieve_uses_shared_conn(temp_db, monkeypatch):
    """tag_like_retrieve は共有 conn を使い、自前で get_connection() を呼ばない。"""
    add_topic(title="x", description="x", tags=["domain:alpha-test"])

    call_count = {"n": 0}
    real_get_connection = search_service.get_connection

    def tracking_get_connection():
        call_count["n"] += 1
        return real_get_connection()

    monkeypatch.setattr(search_service, "get_connection", tracking_get_connection)

    conn = real_get_connection()
    try:
        ctx = _make_ctx(keywords=("alpha",), fts_keywords=("alpha",))
        tag_like_retrieve(ctx, conn)
    finally:
        conn.close()

    assert call_count["n"] == 0


def test_tag_like_retrieve_finds_entity_by_tag_substring(temp_db):
    """キーワードがタグ名 (namespace:name) の一部として含まれるエンティティを返す。"""
    add_topic(title="topicA", description="x", tags=["domain:alpha-design"])
    add_topic(title="topicB", description="x", tags=["domain:other"])

    conn = get_connection()
    try:
        ctx = _make_ctx(keywords=("alpha",), fts_keywords=("alpha",))
        results = tag_like_retrieve(ctx, conn)
    finally:
        conn.close()

    titles = {r["title"] for r in results}
    assert "topicA" in titles
    assert "topicB" not in titles


def test_tag_like_retrieve_returns_empty_for_no_matching_tags(temp_db):
    """キーワードに該当するタグが 1 つも無ければ空リストを返す。"""
    add_topic(title="topicA", description="x", tags=["domain:other"])

    conn = get_connection()
    try:
        ctx = _make_ctx(keywords=("zzz",), fts_keywords=("zzz",))
        results = tag_like_retrieve(ctx, conn)
    finally:
        conn.close()

    assert results == []


def test_tag_like_retrieve_and_mode_requires_single_tag_containing_all(temp_db):
    """AND モードでは 1 つのタグ名が全キーワードを含む必要がある。"""
    # "alpha-beta" は alpha と beta 両方を含むので AND でヒット
    add_topic(title="topicAB", description="x", tags=["alpha-beta"])
    # "alpha" タグと "beta" タグが別々だと AND ではヒットしない
    add_topic(title="topicSep", description="x", tags=["alpha", "beta"])

    conn = get_connection()
    try:
        ctx = _make_ctx(keywords=("alpha", "beta"), fts_keywords=("alpha", "beta"), keyword_mode="and")
        results = tag_like_retrieve(ctx, conn)
    finally:
        conn.close()

    titles = {r["title"] for r in results}
    assert "topicAB" in titles
    assert "topicSep" not in titles


def test_tag_like_retrieve_or_mode_matches_any_keyword(temp_db):
    """OR モードでは「いずれかのキーワードを含むタグ」を持つエンティティが返る。"""
    add_topic(title="topicA", description="x", tags=["alpha-tag"])
    add_topic(title="topicB", description="x", tags=["beta-tag"])
    add_topic(title="topicC", description="x", tags=["gamma-tag"])

    conn = get_connection()
    try:
        ctx = _make_ctx(keywords=("alpha", "beta"), fts_keywords=("alpha", "beta"), keyword_mode="or")
        results = tag_like_retrieve(ctx, conn)
    finally:
        conn.close()

    titles = {r["title"] for r in results}
    assert "topicA" in titles
    assert "topicB" in titles
    assert "topicC" not in titles
