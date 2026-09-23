"""サービス層の単体テスト（エラーハンドリング、特殊文字など）"""
import sqlite3
import pytest
from src.services.topic_service import add_topic
from tests.helpers import add_decision


DEFAULT_TAGS = ["domain:test"]



# ========================================
# エラーハンドリングのテスト
# ========================================


def test_add_topic_with_empty_tags_returns_error(temp_db):
    """空のタグ配列でTAGS_REQUIREDエラーが返る"""
    result = add_topic(title="Invalid Topic", description="Test", tags=[])
    assert "error" in result
    assert result["error"]["code"] == "TAGS_REQUIRED"


def test_add_topic_with_invalid_namespace_returns_error(temp_db):
    """不正なnamespaceでINVALID_TAG_NAMESPACEエラーが返る"""
    result = add_topic(title="Invalid NS Topic", description="Test", tags=["bad:tag"])
    assert "error" in result
    assert result["error"]["code"] == "INVALID_TAG_NAMESPACE"


# ========================================
# 特殊文字の検索テスト（FTS5 trigram）
# ========================================


# ========================================
# パラメータバリデーションのテスト
# ========================================
