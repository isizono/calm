"""SearchPipeline Phase A: SearchContext / build_common_where のテスト。

Phase A スコープ:
- SearchContext frozen dataclass の構築可能性と frozen 性
- build_common_where が entity_type / date_after / date_before を組み立てる出力
- search() が SearchContext 経由で既存挙動を保つ等価性
"""
import dataclasses
import os
import tempfile

import pytest

import src.services.embedding_service as emb
from src.db import init_database
from src.services import search_service
from src.services.activity_service import add_activity
from src.services.search_service import SearchContext, build_common_where
from src.services.topic_service import add_topic
from tests.helpers import add_decision, add_log as add_log_entry


DEFAULT_TAGS = ["domain:test"]


@pytest.fixture(autouse=True)
def disable_embedding(monkeypatch):
    """Phase A の等価性テストでも embedding サービスは無効化する。"""
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


def _make_ctx(**overrides) -> SearchContext:
    """テスト用のデフォルト SearchContext を生成する。"""
    defaults = dict(
        keywords=("hello",),
        fts_keywords=("hello",),
        original_keyword_count=None,
        tag_ids=None,
        entity_type=None,
        limit=10,
        offset=0,
        fetch_limit=50,
        keyword_mode="and",
        include_details=False,
        date_after=None,
        date_before=None,
        domain=None,
    )
    defaults.update(overrides)
    return SearchContext(**defaults)


# ========================================
# SearchContext 構築
# ========================================


def test_search_context_construct_minimal():
    """必須フィールドを与えれば SearchContext が構築できる。"""
    ctx = _make_ctx()
    assert ctx.keywords == ("hello",)
    assert ctx.fts_keywords == ("hello",)
    assert ctx.original_keyword_count is None
    assert ctx.tag_ids is None
    assert ctx.entity_type is None
    assert ctx.limit == 10
    assert ctx.offset == 0
    assert ctx.fetch_limit == 50
    assert ctx.keyword_mode == "and"
    assert ctx.include_details is False
    assert ctx.date_after is None
    assert ctx.date_before is None
    assert ctx.domain is None


def test_search_context_construct_full():
    """全フィールドを指定して構築できる。"""
    ctx = _make_ctx(
        keywords=("alpha", "beta"),
        fts_keywords=("alpha", "beta", "expanded"),
        original_keyword_count=2,
        tag_ids=(1, 2, 3),
        entity_type="decision",
        limit=20,
        offset=5,
        fetch_limit=100,
        keyword_mode="or",
        include_details=True,
        date_after="2026-01-01",
        date_before="2026-06-30 23:59:59",
        domain="cc-memory",
    )
    assert ctx.keywords == ("alpha", "beta")
    assert ctx.fts_keywords == ("alpha", "beta", "expanded")
    assert ctx.original_keyword_count == 2
    assert ctx.tag_ids == (1, 2, 3)
    assert ctx.entity_type == "decision"
    assert ctx.keyword_mode == "or"
    assert ctx.include_details is True
    assert ctx.date_after == "2026-01-01"
    assert ctx.date_before == "2026-06-30 23:59:59"
    assert ctx.domain == "cc-memory"


def test_search_context_is_frozen():
    """SearchContext は frozen のため属性の上書きは FrozenInstanceError を投げる。"""
    ctx = _make_ctx()
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.limit = 999  # type: ignore[misc]


def test_search_context_replace_creates_new_instance():
    """dataclasses.replace で新規インスタンスが生成される。"""
    ctx = _make_ctx()
    ctx2 = dataclasses.replace(ctx, limit=20, offset=5)
    assert ctx.limit == 10 and ctx.offset == 0
    assert ctx2.limit == 20 and ctx2.offset == 5
    assert ctx is not ctx2


# ========================================
# build_common_where
# ========================================


def test_build_common_where_default_no_filters():
    """date 指定なし・entity_type=None でも entity_type プレースホルダ句は出る。"""
    ctx = _make_ctx()
    sql, params = build_common_where(ctx)
    # entity_type=None でも "AND (? IS NULL OR si.source_type = ?)" の形は出力する
    # （? に NULL がバインドされて常時 True 扱いになる）
    assert "AND (? IS NULL OR si.source_type = ?)" in sql
    assert params == [None, None]


def test_build_common_where_with_entity_type():
    ctx = _make_ctx(entity_type="decision")
    sql, params = build_common_where(ctx)
    assert "AND (? IS NULL OR si.source_type = ?)" in sql
    assert params == ["decision", "decision"]


def test_build_common_where_with_date_after_only():
    ctx = _make_ctx(entity_type="decision", date_after="2026-01-01")
    sql, params = build_common_where(ctx)
    assert "AND (? IS NULL OR si.source_type = ?)" in sql
    assert "AND si.created_at >= ?" in sql
    assert "AND si.created_at <= ?" not in sql
    assert params == ["decision", "decision", "2026-01-01"]


def test_build_common_where_with_date_before_only():
    ctx = _make_ctx(date_before="2026-06-30 23:59:59")
    sql, params = build_common_where(ctx)
    assert "AND si.created_at <= ?" in sql
    assert "AND si.created_at >= ?" not in sql
    # entity_type=None でも entity_type 句は出る + date_before
    assert params == [None, None, "2026-06-30 23:59:59"]


def test_build_common_where_with_both_dates():
    ctx = _make_ctx(
        entity_type="log",
        date_after="2026-01-01",
        date_before="2026-06-30 23:59:59",
    )
    sql, params = build_common_where(ctx)
    assert "AND (? IS NULL OR si.source_type = ?)" in sql
    assert "AND si.created_at >= ?" in sql
    assert "AND si.created_at <= ?" in sql
    assert params == ["log", "log", "2026-01-01", "2026-06-30 23:59:59"]


def test_build_common_where_alias_empty_prefix():
    """si_alias='' でカラム参照に prefix を付けない（_vector_search 用）。"""
    ctx = _make_ctx(entity_type="decision", date_after="2026-01-01")
    sql, params = build_common_where(ctx, si_alias="")
    assert "AND (? IS NULL OR source_type = ?)" in sql
    assert "AND created_at >= ?" in sql
    assert "si.source_type" not in sql
    assert "si.created_at" not in sql
    assert params == ["decision", "decision", "2026-01-01"]


def test_build_common_where_custom_alias():
    """si_alias を任意の文字列に切り替えられる。"""
    ctx = _make_ctx(entity_type="topic")
    sql, params = build_common_where(ctx, si_alias="search_index")
    assert "AND (? IS NULL OR search_index.source_type = ?)" in sql
    assert params == ["topic", "topic"]


# ========================================
# Phase A 等価性: search() が SearchContext 経由でも既存挙動を保つ
# ========================================


def test_phase_a_search_preserves_response_keys(temp_db):
    """search() の返却辞書は results / total_count / search_methods_used /
    degraded / nearby_tags の5キーを保つ。"""
    add_topic(
        title="SearchPipelineテストトピック",
        description="Phase A 等価性確認用",
        tags=DEFAULT_TAGS,
    )
    result = search_service.search(keyword="SearchPipeline")
    assert "error" not in result
    assert set(result.keys()) == {
        "results",
        "total_count",
        "search_methods_used",
        "degraded",
        "nearby_tags",
    }
    assert isinstance(result["results"], list)
    assert isinstance(result["total_count"], int)
    assert isinstance(result["search_methods_used"], list)
    assert isinstance(result["degraded"], bool)
    assert isinstance(result["nearby_tags"], list)


def test_phase_a_search_entity_type_filter(temp_db):
    """entity_type フィルタは Phase A 後も SearchContext 経由で機能する。"""
    add_topic(
        title="topicEntityFilterCheck",
        description="topic 側",
        tags=DEFAULT_TAGS,
    )
    add_activity(
        title="activityEntityFilterCheck",
        description="activity 側",
        tags=DEFAULT_TAGS,
    )
    result = search_service.search(
        keyword="EntityFilterCheck", entity_type="topic"
    )
    assert "error" not in result
    types_seen = {r["type"] for r in result["results"]}
    assert types_seen <= {"topic"}
    assert any(r["type"] == "topic" for r in result["results"])


def test_phase_a_search_date_filter(temp_db):
    """date_after / date_before フィルタは SearchContext 経由でも反映される。"""
    add_topic(
        title="DateFilterPhaseATopic",
        description="日付フィルタテスト",
        tags=DEFAULT_TAGS,
    )
    # 未来の date_after で 0 件
    result = search_service.search(
        keyword="DateFilterPhaseATopic", date_after="2099-01-01"
    )
    assert "error" not in result
    assert result["total_count"] == 0
    assert result["results"] == []

    # 過去の date_after で >= 1 件
    result2 = search_service.search(
        keyword="DateFilterPhaseATopic", date_after="2020-01-01"
    )
    assert "error" not in result2
    assert result2["total_count"] >= 1


def test_phase_a_search_keyword_mode_or(temp_db):
    """keyword_mode='or' は SearchContext 経由でも OR ヒットを返す。"""
    add_topic(title="alphaOnlyTopic", description="alpha", tags=DEFAULT_TAGS)
    add_topic(title="betaOnlyTopic", description="beta", tags=DEFAULT_TAGS)

    result = search_service.search(
        keyword=["alphaOnlyTopic", "betaOnlyTopic"], keyword_mode="or"
    )
    assert "error" not in result
    titles = {r["title"] for r in result["results"]}
    assert "alphaOnlyTopic" in titles
    assert "betaOnlyTopic" in titles
