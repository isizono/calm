"""fts_retrieve retriever 単体テスト。

SearchContext と共有 conn を直接渡したときに FTS5 ベースのランキング結果が
正しく返ることを確認する。
"""
import os
import tempfile

import pytest

import src.services.embedding_service as emb
from src.db import get_connection, init_database
from src.services import search_service
from src.services.activity_service import add_activity
from src.services.search_service import fts_retrieve
from src.services.topic_service import add_topic
from tests.helpers import add_decision, make_search_context as _make_ctx

DEFAULT_TAGS = ["domain:test"]


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


def test_fts_retrieve_uses_shared_conn(temp_db, monkeypatch):
    """fts_retrieve は共有 conn を使い、自前で get_connection() を呼ばない。"""
    add_topic(title="alpha topic", description="hello world", tags=DEFAULT_TAGS)

    # search_service モジュールの get_connection が呼ばれないことを検証
    call_count = {"n": 0}
    real_get_connection = search_service.get_connection

    def tracking_get_connection():
        call_count["n"] += 1
        return real_get_connection()

    monkeypatch.setattr(search_service, "get_connection", tracking_get_connection)

    conn = real_get_connection()
    try:
        ctx = _make_ctx(keywords=("alpha",), fts_keywords=("alpha",))
        results = fts_retrieve(ctx, conn)
    finally:
        conn.close()

    assert call_count["n"] == 0
    assert any(r["type"] == "topic" and r["title"] == "alpha topic" for r in results)


def test_fts_retrieve_returns_basic_shape(temp_db):
    """fts_retrieve は type / id / title の dict のリストを返す。"""
    add_topic(title="alpha topic", description="hello world", tags=DEFAULT_TAGS)

    conn = get_connection()
    try:
        ctx = _make_ctx(keywords=("alpha",), fts_keywords=("alpha",))
        results = fts_retrieve(ctx, conn)
    finally:
        conn.close()

    assert isinstance(results, list)
    assert all(set(r.keys()) >= {"type", "id", "title"} for r in results)


def test_fts_retrieve_or_mode_filters_short_keywords(temp_db):
    """OR モードでは 3 文字未満のキーワードは FTS クエリに含まれない。"""
    add_topic(title="hello hi", description="content", tags=DEFAULT_TAGS)

    conn = get_connection()
    try:
        # "hi" は 2 文字なので OR モードでは除外され、"hello" のみで検索される
        ctx = _make_ctx(
            keywords=("hello", "hi"),
            fts_keywords=("hello", "hi"),
            keyword_mode="or",
        )
        results = fts_retrieve(ctx, conn)
    finally:
        conn.close()

    assert any(r["title"] == "hello hi" for r in results)


def test_fts_retrieve_returns_empty_when_or_mode_has_only_short_keywords(temp_db):
    """OR モードで 3 文字以上のキーワードが 1 件もないと空リストを返す。"""
    add_topic(title="hello world", description="content", tags=DEFAULT_TAGS)

    conn = get_connection()
    try:
        ctx = _make_ctx(
            keywords=("hi", "ok"),
            fts_keywords=("hi", "ok"),
            keyword_mode="or",
        )
        results = fts_retrieve(ctx, conn)
    finally:
        conn.close()

    assert results == []


def test_fts_retrieve_entity_type_filter(temp_db):
    """entity_type=decision でフィルタすると decision のみが返る。"""
    topic_id = add_topic(title="alpha topic", description="x", tags=DEFAULT_TAGS)["topic_id"]
    add_activity(title="alpha activity", description="x", tags=DEFAULT_TAGS)
    add_decision(decision="alpha decision body", reason="r", topic_id=topic_id, tags=DEFAULT_TAGS)

    conn = get_connection()
    try:
        ctx = _make_ctx(keywords=("alpha",), fts_keywords=("alpha",), entity_type="decision")
        results = fts_retrieve(ctx, conn)
    finally:
        conn.close()

    assert results
    assert all(r["type"] == "decision" for r in results)


def test_fts_retrieve_respects_fetch_limit(temp_db):
    """fetch_limit で結果件数が切り詰められる。"""
    for i in range(5):
        add_topic(title=f"alpha topic {i}", description=f"hello {i}", tags=DEFAULT_TAGS)

    conn = get_connection()
    try:
        ctx = _make_ctx(keywords=("alpha",), fts_keywords=("alpha",), fetch_limit=2)
        results = fts_retrieve(ctx, conn)
    finally:
        conn.close()

    assert len(results) <= 2


def test_fts_retrieve_qe_expansion_or_combines_with_and(temp_db):
    """QE 拡張があるとき AND の元キーワード群と OR の拡張タグが結合される。"""
    add_topic(title="alpha doc", description="content beta", tags=DEFAULT_TAGS)
    add_topic(title="expanded-only", description="zeta", tags=DEFAULT_TAGS)

    conn = get_connection()
    try:
        # fts_keywords = (alpha, beta, zeta) で original=2 → (alpha AND beta) OR zeta
        ctx = _make_ctx(
            keywords=("alpha", "beta"),
            fts_keywords=("alpha", "beta", "zeta"),
            original_keyword_count=2,
        )
        results = fts_retrieve(ctx, conn)
    finally:
        conn.close()

    titles = {r["title"] for r in results}
    assert "alpha doc" in titles
    assert "expanded-only" in titles
