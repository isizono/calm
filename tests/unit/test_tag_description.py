"""update_tag description機能のユニットテスト"""
import pytest
from src.db import get_connection
from src.services.tag_service import update_tag
from src.services.topic_service import add_topic
import src.services.embedding_service as emb


@pytest.fixture(autouse=True)
def disable_embedding(monkeypatch):
    """embeddingサービスを無効化"""
    monkeypatch.setattr(emb, '_server_initialized', False)
    monkeypatch.setattr(emb, '_backfill_done', True)
    monkeypatch.setattr(emb, '_ensure_server_running', lambda: False)



def test_update_tag_description_set(temp_db):
    """descriptionを設定できる"""
    add_topic(title="T", description="D", tags=["domain:test"])

    result = update_tag("domain:test", description="テスト用タグ")
    assert "error" not in result
    assert result["updated"] is True
    assert result["tag"] == "domain:test"
    assert result["description"] == "テスト用タグ"

    # DB確認
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT description FROM tags WHERE namespace = 'domain' AND name = 'test'"
        ).fetchone()
        assert row["description"] == "テスト用タグ"
    finally:
        conn.close()


def test_update_tag_description_update(temp_db):
    """descriptionを更新できる"""
    add_topic(title="T", description="D", tags=["domain:test"])
    update_tag("domain:test", description="初期値")

    result = update_tag("domain:test", description="更新値")
    assert "error" not in result
    assert result["description"] == "更新値"


def test_update_tag_description_null(temp_db):
    """description=""でNULLに正規化される"""
    add_topic(title="T", description="D", tags=["domain:test"])
    update_tag("domain:test", description="初期値")

    result = update_tag("domain:test", description="")
    assert "error" not in result
    assert result["description"] is None

    # DB確認
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT description FROM tags WHERE namespace = 'domain' AND name = 'test'"
        ).fetchone()
        assert row["description"] is None
    finally:
        conn.close()


def test_update_tag_description_empty_string_normalized(temp_db):
    """空文字descriptionがNULLに正規化される"""
    add_topic(title="T", description="D", tags=["domain:test"])

    result = update_tag("domain:test", description="")
    assert "error" not in result
    assert result["updated"] is True
    assert result["description"] is None


def test_update_tag_description_too_long(temp_db):
    """101文字でCHECK制約エラー"""
    add_topic(title="T", description="D", tags=["domain:test"])

    long_desc = "a" * 101
    result = update_tag("domain:test", description=long_desc)
    assert "error" in result
    assert result["error"]["code"] == "DATABASE_ERROR"


def test_update_tag_description_mutual_exclusion(temp_db):
    """notes/canonical/renameとdescriptionの同時指定でエラー"""
    add_topic(title="T", description="D", tags=["domain:test"])

    # description + notes
    result = update_tag("domain:test", description="desc", notes="notes")
    assert "error" in result
    assert result["error"]["code"] == "CONFLICTING_PARAMS"

    # description + canonical
    result = update_tag("domain:test", description="desc", canonical="domain:other")
    assert "error" in result
    assert result["error"]["code"] == "CONFLICTING_PARAMS"

    # description + rename
    result = update_tag("domain:test", description="desc", rename="domain:renamed")
    assert "error" in result
    assert result["error"]["code"] == "CONFLICTING_PARAMS"


def test_update_tag_description_100_chars_ok(temp_db):
    """100文字ちょうどはOK"""
    add_topic(title="T", description="D", tags=["domain:test"])

    desc_100 = "a" * 100
    result = update_tag("domain:test", description=desc_100)
    assert "error" not in result
    assert result["description"] == desc_100
