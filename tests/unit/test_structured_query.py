"""構造化クエリ（entity_type / domain / date_after / date_before）フィルタのテスト

search()に追加された構造化フィルタの動作を検証する。
"""
import pytest

from src.db import get_connection
from src.services.topic_service import add_topic
from src.services.activity_service import add_activity
from tests.helpers import add_log, add_decision
from src.services.material_service import add_material
from src.services import search_service
import src.services.embedding_service as emb


DEFAULT_TAGS = ["domain:test"]


@pytest.fixture(autouse=True)
def disable_embedding(monkeypatch):
    """embeddingサービスを無効化してFTS5のみで検索する"""
    monkeypatch.setattr(emb, '_server_initialized', False)
    monkeypatch.setattr(emb, '_backfill_done', True)
    monkeypatch.setattr(emb, '_ensure_server_running', lambda: False)



# ========================================
# 1. entity_type基本フィルタ
# ========================================


def test_entity_type_filters_by_topic(temp_db):
    """entity_type="topic"を指定すると、topicタイプの結果のみ返す"""
    topic = add_topic(title="構造化クエリ対象トピック", description="テスト用トピック", tags=DEFAULT_TAGS)
    add_activity(title="構造化クエリ対象アクティビティ", description="テスト用アクティビティ", tags=DEFAULT_TAGS)
    assert "error" not in topic

    result = search_service.search(keyword="構造化クエリ対象", entity_type="topic")
    assert "error" not in result
    assert len(result["results"]) >= 1
    for item in result["results"]:
        assert item["type"] == "topic"


# ========================================
# 2. domain単独指定
# ========================================


def test_domain_filters_by_domain_tag(temp_db):
    """domain="myproject"を指定すると、domain:myprojectタグを持つ結果のみ返す"""
    add_topic(title="ドメインフィルタ対象トピック", description="プロジェクト用", tags=["domain:myproject"])
    add_topic(title="ドメインフィルタ除外トピック", description="別プロジェクト", tags=["domain:other"])

    result = search_service.search(keyword="ドメインフィルタ", domain="myproject")
    assert "error" not in result
    assert len(result["results"]) >= 1
    for item in result["results"]:
        assert "domain:myproject" in item["tags"]


# ========================================
# 3. domain+tags同時指定（ANDで両方効く）
# ========================================


def test_domain_and_tags_combined(temp_db):
    """domain+tagsを同時指定すると、両方の条件をANDで満たす結果のみ返す"""
    add_topic(
        title="複合フィルタ対象トピック",
        description="両方のタグを持つ",
        tags=["domain:myproject", "feature"],
    )
    add_topic(
        title="複合フィルタ除外トピックA",
        description="domainのみ持つ",
        tags=["domain:myproject"],
    )

    result = search_service.search(keyword="複合フィルタ", domain="myproject", tags=["feature"])
    assert "error" not in result
    assert len(result["results"]) >= 1
    for item in result["results"]:
        assert "domain:myproject" in item["tags"]
        assert "feature" in item["tags"]


# ========================================
# 4. domain+tags重複（重複除去）
# ========================================


def test_domain_tags_dedup(temp_db):
    """domain="myproject"とtags=["domain:myproject"]を同時指定しても重複除去され空結果にならない"""
    add_topic(
        title="重複除去テスト対象トピック",
        description="domain重複テスト",
        tags=["domain:myproject"],
    )

    result = search_service.search(
        keyword="重複除去テスト対象",
        domain="myproject",
        tags=["domain:myproject"],
    )
    assert "error" not in result
    assert len(result["results"]) >= 1


# ========================================
# 5. date_after単独
# ========================================


def test_date_after_filters_recent(temp_db):
    """date_afterを指定すると、指定日以降に作成されたエンティティのみ返す"""
    topic = add_topic(title="日付フィルタ古いトピック", description="古いデータ", tags=DEFAULT_TAGS)
    assert "error" not in topic
    topic_id = topic["topic_id"]

    # created_atを過去に書き換え
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE discussion_topics SET created_at = '2020-01-01 00:00:00' WHERE id = ?",
            (topic_id,),
        )
        conn.execute(
            "UPDATE search_index SET created_at = '2020-01-01 00:00:00' WHERE source_type = 'topic' AND source_id = ?",
            (topic_id,),
        )
        conn.commit()
    finally:
        conn.close()

    add_topic(title="日付フィルタ新しいトピック", description="新しいデータ", tags=DEFAULT_TAGS)

    result = search_service.search(keyword="日付フィルタ", date_after="2025-01-01")
    assert "error" not in result
    assert len(result["results"]) >= 1
    for item in result["results"]:
        assert item["title"] != "日付フィルタ古いトピック"


# ========================================
# 6. date_before単独
# ========================================


def test_date_before_filters_old(temp_db):
    """date_beforeを指定すると、指定日以前に作成されたエンティティのみ返す"""
    topic = add_topic(title="日付前フィルタ古いトピック", description="古いデータ", tags=DEFAULT_TAGS)
    assert "error" not in topic
    topic_id = topic["topic_id"]

    # created_atを過去に書き換え
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE discussion_topics SET created_at = '2020-06-15 12:00:00' WHERE id = ?",
            (topic_id,),
        )
        conn.execute(
            "UPDATE search_index SET created_at = '2020-06-15 12:00:00' WHERE source_type = 'topic' AND source_id = ?",
            (topic_id,),
        )
        conn.commit()
    finally:
        conn.close()

    add_topic(title="日付前フィルタ新しいトピック", description="新しいデータ", tags=DEFAULT_TAGS)

    result = search_service.search(keyword="日付前フィルタ", date_before="2021-01-01")
    assert "error" not in result
    assert len(result["results"]) >= 1
    for item in result["results"]:
        assert item["title"] != "日付前フィルタ新しいトピック"


# ========================================
# 7. date_after+date_before範囲指定
# ========================================


def test_date_range_filter(temp_db):
    """date_after+date_beforeの範囲指定で、範囲内のエンティティのみ返す"""
    topic_old = add_topic(title="範囲外古いトピック検索用", description="範囲外古い", tags=DEFAULT_TAGS)
    topic_mid = add_topic(title="範囲内トピック検索用", description="範囲内", tags=DEFAULT_TAGS)
    topic_new = add_topic(title="範囲外新しいトピック検索用", description="範囲外新しい", tags=DEFAULT_TAGS)

    old_id = topic_old["topic_id"]
    mid_id = topic_mid["topic_id"]
    new_id = topic_new["topic_id"]

    conn = get_connection()
    try:
        conn.execute(
            "UPDATE discussion_topics SET created_at = '2020-01-01 00:00:00' WHERE id = ?",
            (old_id,),
        )
        conn.execute(
            "UPDATE search_index SET created_at = '2020-01-01 00:00:00' WHERE source_type = 'topic' AND source_id = ?",
            (old_id,),
        )
        conn.execute(
            "UPDATE discussion_topics SET created_at = '2023-06-15 12:00:00' WHERE id = ?",
            (mid_id,),
        )
        conn.execute(
            "UPDATE search_index SET created_at = '2023-06-15 12:00:00' WHERE source_type = 'topic' AND source_id = ?",
            (mid_id,),
        )
        conn.execute(
            "UPDATE discussion_topics SET created_at = '2030-01-01 00:00:00' WHERE id = ?",
            (new_id,),
        )
        conn.execute(
            "UPDATE search_index SET created_at = '2030-01-01 00:00:00' WHERE source_type = 'topic' AND source_id = ?",
            (new_id,),
        )
        conn.commit()
    finally:
        conn.close()

    result = search_service.search(
        keyword="トピック検索用",
        date_after="2023-01-01",
        date_before="2024-12-31",
    )
    assert "error" not in result
    assert len(result["results"]) >= 1
    titles = [item["title"] for item in result["results"]]
    assert "範囲内トピック検索用" in titles
    assert "範囲外古いトピック検索用" not in titles
    assert "範囲外新しいトピック検索用" not in titles


# ========================================
# 8. date_before当日含み
# ========================================


def test_date_before_includes_same_day(temp_db):
    """date_beforeに日付のみ指定した場合、当日のデータが含まれる（" 23:59:59"が自動付与される）"""
    topic = add_topic(title="当日含みテスト対象トピック", description="当日テスト", tags=DEFAULT_TAGS)
    assert "error" not in topic
    topic_id = topic["topic_id"]

    # 2023-03-01 15:00:00に設定
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE discussion_topics SET created_at = '2023-03-01 15:00:00' WHERE id = ?",
            (topic_id,),
        )
        conn.execute(
            "UPDATE search_index SET created_at = '2023-03-01 15:00:00' WHERE source_type = 'topic' AND source_id = ?",
            (topic_id,),
        )
        conn.commit()
    finally:
        conn.close()

    # date_before="2023-03-01"（日付のみ）→ "2023-03-01 23:59:59"に変換され当日含む
    result = search_service.search(keyword="当日含みテスト対象", date_before="2023-03-01")
    assert "error" not in result
    assert len(result["results"]) >= 1
    assert result["results"][0]["title"] == "当日含みテスト対象トピック"


# ========================================
# 9. 不正日付フォーマット
# ========================================


def test_invalid_date_format_returns_error(temp_db):
    """不正な日付フォーマットを指定するとINVALID_PARAMETERエラーを返す"""
    result = search_service.search(keyword="テスト検索", date_after="2023/01/01")
    assert "error" in result
    assert result["error"]["code"] == "INVALID_PARAMETER"
    assert "date_after" in result["error"]["message"]

    result2 = search_service.search(keyword="テスト検索", date_before="invalid-date")
    assert "error" in result2
    assert result2["error"]["code"] == "INVALID_PARAMETER"
    assert "date_before" in result2["error"]["message"]


# ========================================
# 10. domain空文字
# ========================================


def test_empty_domain_treated_as_none(temp_db):
    """domain=""を指定した場合、None扱いとなりフィルタされずエラーにもならない"""
    add_topic(title="空ドメインテスト対象トピック", description="空ドメインテスト", tags=DEFAULT_TAGS)

    result = search_service.search(keyword="空ドメインテスト対象", domain="")
    assert "error" not in result
    assert len(result["results"]) >= 1


# ========================================
# 11. 不正な日付値（形式は正しいが存在しない日付）
# ========================================


def test_invalid_date_value_returns_error(temp_db):
    """形式は正しいが存在しない日付（13月や32日）を指定するとINVALID_PARAMETERエラーを返す"""
    result = search_service.search(keyword="テスト検索", date_after="2023-13-45")
    assert "error" in result
    assert result["error"]["code"] == "INVALID_PARAMETER"

    result2 = search_service.search(keyword="テスト検索", date_before="2023-01-01 25:99:99")
    assert "error" in result2
    assert result2["error"]["code"] == "INVALID_PARAMETER"


# ========================================
# 12. date_after/date_before空文字
# ========================================


def test_empty_date_after_treated_as_none(temp_db):
    """date_after=""を指定した場合、None扱いとなりフィルタされずエラーにもならない"""
    add_topic(title="空日付テスト対象トピック", description="空日付テスト", tags=DEFAULT_TAGS)

    result = search_service.search(keyword="空日付テスト対象", date_after="")
    assert "error" not in result
    assert len(result["results"]) >= 1


def test_empty_date_before_treated_as_none(temp_db):
    """date_before=""を指定した場合、None扱いとなりフィルタされずエラーにもならない"""
    add_topic(title="空日付前テスト対象トピック", description="空日付前テスト", tags=DEFAULT_TAGS)

    result = search_service.search(keyword="空日付前テスト対象", date_before="")
    assert "error" not in result
    assert len(result["results"]) >= 1
