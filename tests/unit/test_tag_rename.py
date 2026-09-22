"""タグリネーム機能のユニットテスト

- update_tag(rename=...): 基本リネーム、namespace変更、衝突エラー、同名エラー
- リネーム後の紐付け維持確認
- バリデーション: 他パラメータとの排他
"""
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



class TestRenameBasic:
    """rename基本動作テスト"""

    def test_rename_bare_tag(self, temp_db):
        """素タグのリネーム"""
        add_topic(title="T", description="D", tags=["hooks"])

        result = update_tag("hooks", rename="hook-system")
        assert result["updated"] is True
        assert result["tag"] == "hooks"
        assert result["renamed_to"] == "hook-system"

    def test_rename_namespaced_tag(self, temp_db):
        """namespace付きタグのリネーム"""
        add_topic(title="T", description="D", tags=["domain:test"])

        result = update_tag("domain:test", rename="domain:testing")
        assert result["updated"] is True
        assert result["renamed_to"] == "domain:testing"

    def test_rename_change_namespace(self, temp_db):
        """素タグからnamespace付きへの変更"""
        add_topic(title="T", description="D", tags=["hooks"])

        result = update_tag("hooks", rename="domain:hooks")
        assert result["updated"] is True
        assert result["tag"] == "hooks"
        assert result["renamed_to"] == "domain:hooks"

    def test_rename_remove_namespace(self, temp_db):
        """namespace付きから素タグへの変更"""
        add_topic(title="T", description="D", tags=["domain:hooks"])

        result = update_tag("domain:hooks", rename="hooks")
        assert result["updated"] is True
        assert result["renamed_to"] == "hooks"


class TestRenamePreservesLinks:
    """リネーム後の紐付け維持テスト"""

    def test_topic_link_preserved(self, temp_db):
        """リネーム後もtopicとの紐付けが維持される"""
        result = add_topic(title="T", description="D", tags=["old-name"])
        topic_id = result["topic_id"]

        # リネーム
        update_tag("old-name", rename="new-name")

        # 紐付けが維持されていることを確認
        conn = get_connection()
        try:
            rows = conn.execute(
                """
                SELECT t.namespace, t.name FROM tags t
                JOIN topic_tags tt ON t.id = tt.tag_id
                WHERE tt.topic_id = ?
                """,
                (topic_id,),
            ).fetchall()
            tag_names = [r["name"] for r in rows]
            assert "new-name" in tag_names
            assert "old-name" not in tag_names
        finally:
            conn.close()


class TestRenameValidation:
    """renameのバリデーションテスト"""

    def test_rename_empty_string_error(self, temp_db):
        """空文字でのリネームでエラー"""
        add_topic(title="T", description="D", tags=["hooks"])

        result = update_tag("hooks", rename="")
        assert "error" in result
        assert result["error"]["code"] == "INVALID_TAG_NAME"

    def test_rename_alias_tag(self, temp_db):
        """エイリアスタグ（canonical_id設定済み）のリネーム"""
        add_topic(title="T", description="D", tags=["old-alias", "canonical-target"])
        update_tag("old-alias", canonical="canonical-target")

        result = update_tag("old-alias", rename="new-alias")
        assert result["updated"] is True
        assert result["renamed_to"] == "new-alias"

    def test_rename_tag_with_notes(self, temp_db):
        """notes付きタグのリネーム（notes維持）"""
        add_topic(title="T", description="D", tags=["hooks"])
        update_tag("hooks", notes="重要な教訓")

        result = update_tag("hooks", rename="hook-system")
        assert result["updated"] is True
        assert result["renamed_to"] == "hook-system"

        # notesが維持されていることを確認
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT notes FROM tags WHERE namespace = '' AND name = 'hook-system'",
            ).fetchone()
            assert row["notes"] == "重要な教訓"
        finally:
            conn.close()

    def test_rename_same_name_error(self, temp_db):
        """同一名へのリネームでエラー"""
        add_topic(title="T", description="D", tags=["hooks"])

        result = update_tag("hooks", rename="hooks")
        assert "error" in result
        assert result["error"]["code"] == "SAME_NAME"

    def test_rename_collision_error(self, temp_db):
        """既存タグとの衝突でエラー"""
        add_topic(title="T", description="D", tags=["old-tag", "existing-tag"])

        result = update_tag("old-tag", rename="existing-tag")
        assert "error" in result
        assert result["error"]["code"] == "ALREADY_EXISTS"

    def test_rename_not_found_error(self, temp_db):
        """存在しないタグのリネームでエラー"""
        result = update_tag("nonexistent", rename="new-name")
        assert "error" in result
        assert result["error"]["code"] == "NOT_FOUND"

    def test_rename_invalid_namespace_error(self, temp_db):
        """無効なnamespaceへのリネームでエラー"""
        add_topic(title="T", description="D", tags=["hooks"])

        result = update_tag("hooks", rename="invalid:hooks")
        assert "error" in result

    def test_conflicting_rename_and_notes(self, temp_db):
        """renameとnotesの同時指定でエラー"""
        add_topic(title="T", description="D", tags=["hooks"])

        result = update_tag("hooks", rename="hook-system", notes="some note")
        assert "error" in result
        assert result["error"]["code"] == "CONFLICTING_PARAMS"

    def test_conflicting_rename_and_canonical(self, temp_db):
        """renameとcanonicalの同時指定でエラー"""
        add_topic(title="T", description="D", tags=["hooks", "domain:test"])

        result = update_tag("hooks", rename="hook-system", canonical="domain:test")
        assert "error" in result
        assert result["error"]["code"] == "CONFLICTING_PARAMS"

    def test_missing_all_params(self, temp_db):
        """全パラメータ未指定でエラー"""
        result = update_tag("hooks")
        assert "error" in result
        assert result["error"]["code"] == "MISSING_PARAMS"
