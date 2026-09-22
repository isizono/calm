"""タグエイリアス（canonical）機能のユニットテスト

- マイグレーション: canonical_idカラムの存在確認
- ensure_tag_ids / resolve_tag_ids / _resolve_tag_ids_readonly のcanonical解決
- update_tag: canonical設定/解除/上書き/紐付け付け替え/バリデーション
- E2Eフロー: タグ付きtopic作成 → エイリアス設定 → 検索でcanonical側にヒット
"""
import pytest
from src.db import get_connection
from src.services.tag_service import (
    ensure_tag_ids,
    resolve_tag_ids,
    update_tag,
)
from src.services.search_service import _resolve_tag_ids_readonly
from src.services.topic_service import add_topic
from src.services.activity_service import add_activity
from tests.helpers import add_log, add_decision
import src.services.embedding_service as emb


@pytest.fixture(autouse=True)
def disable_embedding(monkeypatch):
    """embeddingサービスを無効化"""
    monkeypatch.setattr(emb, '_server_initialized', False)
    monkeypatch.setattr(emb, '_backfill_done', True)
    monkeypatch.setattr(emb, '_ensure_server_running', lambda: False)



def _create_tag(conn, namespace, name):
    """ヘルパー: タグを作成してIDを返す"""
    conn.execute(
        "INSERT OR IGNORE INTO tags (namespace, name) VALUES (?, ?)",
        (namespace, name),
    )
    row = conn.execute(
        "SELECT id FROM tags WHERE namespace = ? AND name = ?",
        (namespace, name),
    ).fetchone()
    return row["id"]


def _set_canonical(conn, alias_id, canonical_id):
    """ヘルパー: canonical_idを直接設定"""
    conn.execute(
        "UPDATE tags SET canonical_id = ? WHERE id = ?",
        (canonical_id, alias_id),
    )


# ========================================
# マイグレーションテスト
# ========================================


class TestMigration:
    """canonical_idカラムの存在確認"""

    def test_canonical_id_column_exists(self, temp_db):
        """canonical_idカラムが追加されていること"""
        conn = get_connection()
        try:
            cursor = conn.execute("PRAGMA table_info(tags)")
            columns = {row["name"] for row in cursor.fetchall()}
            assert "canonical_id" in columns
        finally:
            conn.close()


# ========================================
# ensure_tag_ids テスト
# ========================================


class TestEnsureTagIdsCanonical:
    """ensure_tag_idsのcanonical解決テスト"""

    def test_alias_resolves_to_canonical(self, temp_db):
        """エイリアスタグ名でensureするとcanonical側IDが返る"""
        conn = get_connection()
        try:
            canonical_id = _create_tag(conn, "", "BE")
            alias_id = _create_tag(conn, "", "prm")
            _set_canonical(conn, alias_id, canonical_id)
            conn.commit()

            result = ensure_tag_ids(conn, [("", "prm")])
            assert result == [canonical_id]
        finally:
            conn.close()

    def test_non_alias_returns_own_id(self, temp_db):
        """正規タグ名でensureすると自身のIDが返る"""
        conn = get_connection()
        try:
            tag_id = _create_tag(conn, "", "BE")
            conn.commit()

            result = ensure_tag_ids(conn, [("", "BE")])
            assert result == [tag_id]
        finally:
            conn.close()


# ========================================
# resolve_tag_ids テスト
# ========================================


class TestResolveTagIdsCanonical:
    """resolve_tag_idsのcanonical解決テスト"""

    def test_alias_resolves_to_canonical(self, temp_db):
        """エイリアスタグ名で解決するとcanonical側IDが返る"""
        conn = get_connection()
        try:
            canonical_id = _create_tag(conn, "domain", "BE")
            alias_id = _create_tag(conn, "", "prm")
            _set_canonical(conn, alias_id, canonical_id)
            conn.commit()

            result = resolve_tag_ids(conn, [("", "prm")])
            assert result == [canonical_id]
        finally:
            conn.close()


# ========================================
# _resolve_tag_ids_readonly テスト
# ========================================


class TestResolveTagIdsReadonlyCanonical:
    """_resolve_tag_ids_readonlyのcanonical解決テスト"""

    def test_alias_resolves_to_canonical(self, temp_db):
        """エイリアスタグ名で検索するとcanonical側IDが返る"""
        conn = get_connection()
        try:
            canonical_id = _create_tag(conn, "domain", "BE")
            alias_id = _create_tag(conn, "", "prm")
            _set_canonical(conn, alias_id, canonical_id)
            conn.commit()

            result = _resolve_tag_ids_readonly(conn, ["prm"])
            assert result == [canonical_id]
        finally:
            conn.close()


# ========================================
# update_tag — canonical 設定/解除/上書き テスト
# ========================================


class TestUpdateTagCanonical:
    """update_tagのcanonical設定テスト"""

    def test_set_canonical(self, temp_db):
        """エイリアスが正しく設定されること"""
        add_topic(title="T", description="D", tags=["domain:BE", "prm"])

        result = update_tag("prm", canonical="domain:BE")
        assert "error" not in result
        assert result["tag"] == "prm"
        assert result["canonical"] == "domain:BE"
        assert result["updated"] is True

        # DBで確認
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT canonical_id FROM tags WHERE namespace = '' AND name = 'prm'"
            ).fetchone()
            be_row = conn.execute(
                "SELECT id FROM tags WHERE namespace = 'domain' AND name = 'BE'"
            ).fetchone()
            assert row["canonical_id"] == be_row["id"]
        finally:
            conn.close()

    def test_unset_canonical(self, temp_db):
        """canonical=""でエイリアス解除されること"""
        add_topic(title="T", description="D", tags=["domain:BE", "prm"])
        update_tag("prm", canonical="domain:BE")

        result = update_tag("prm", canonical="")
        assert "error" not in result
        assert result["canonical"] is None

        # DBで確認
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT canonical_id FROM tags WHERE namespace = '' AND name = 'prm'"
            ).fetchone()
            assert row["canonical_id"] is None
        finally:
            conn.close()

    def test_overwrite_canonical(self, temp_db):
        """別のcanonicalに変更できること"""
        add_topic(title="T", description="D", tags=["domain:BE", "domain:FE", "prm"])
        update_tag("prm", canonical="domain:BE")

        result = update_tag("prm", canonical="domain:FE")
        assert "error" not in result
        assert result["canonical"] == "domain:FE"


# ========================================
# update_tag — 紐付け付け替え テスト
# ========================================


class TestUpdateTagRelinking:
    """update_tagの紐付け付け替えテスト"""

    def test_relink_junction_tables(self, temp_db):
        """中間テーブル4つの紐付けがcanonical側に移ること"""
        topic = add_topic(title="T", description="D", tags=["prm"])
        activity = add_activity(title="A", description="D", tags=["prm"], check_in=False)
        decision = add_decision(
            topic_id=topic["topic_id"], decision="Dec", reason="R", tags=["prm"]
        )
        log = add_log(
            topic_id=topic["topic_id"], title="L", content="C", tags=["prm"]
        )

        # canonical側タグを作成
        conn = get_connection()
        try:
            canonical_id = _create_tag(conn, "domain", "BE")
            conn.commit()
        finally:
            conn.close()

        # エイリアス設定
        result = update_tag("prm", canonical="domain:BE")
        assert "error" not in result

        # 各中間テーブルを確認
        conn = get_connection()
        try:
            # topic_tags
            row = conn.execute(
                "SELECT tag_id FROM topic_tags WHERE topic_id = ?",
                (topic["topic_id"],),
            ).fetchall()
            tag_ids = [r["tag_id"] for r in row]
            assert canonical_id in tag_ids

            # activity_tags
            row = conn.execute(
                "SELECT tag_id FROM activity_tags WHERE activity_id = ?",
                (activity["activity_id"],),
            ).fetchall()
            tag_ids = [r["tag_id"] for r in row]
            assert canonical_id in tag_ids

            # decision_tags
            row = conn.execute(
                "SELECT tag_id FROM decision_tags WHERE decision_id = ?",
                (decision["decision_id"],),
            ).fetchall()
            tag_ids = [r["tag_id"] for r in row]
            assert canonical_id in tag_ids

            # log_tags
            row = conn.execute(
                "SELECT tag_id FROM log_tags WHERE log_id = ?",
                (log["log_id"],),
            ).fetchall()
            tag_ids = [r["tag_id"] for r in row]
            assert canonical_id in tag_ids
        finally:
            conn.close()

    def test_relink_with_duplicate(self, temp_db):
        """canonical側に既存紐付けがある場合、重複が除去されること"""
        # topicにprmとdomain:BEの両方を付ける
        topic = add_topic(title="T", description="D", tags=["prm", "domain:BE"])

        # エイリアス設定（prmをdomain:BEに付け替え）
        result = update_tag("prm", canonical="domain:BE")
        assert "error" not in result

        # topic_tagsにはdomain:BEが1つだけ（重複なし）
        conn = get_connection()
        try:
            prm_row = conn.execute(
                "SELECT id FROM tags WHERE namespace = '' AND name = 'prm'"
            ).fetchone()
            be_row = conn.execute(
                "SELECT id FROM tags WHERE namespace = 'domain' AND name = 'BE'"
            ).fetchone()

            rows = conn.execute(
                "SELECT tag_id FROM topic_tags WHERE topic_id = ?",
                (topic["topic_id"],),
            ).fetchall()
            tag_ids = [r["tag_id"] for r in rows]

            # prm側の紐付けは消えている
            assert prm_row["id"] not in tag_ids
            # domain:BE側の紐付けが存在（1つだけ）
            assert tag_ids.count(be_row["id"]) == 1
        finally:
            conn.close()


# ========================================
# update_tag — バリデーション テスト
# ========================================


class TestUpdateTagValidation:
    """update_tagのバリデーションテスト"""

    def test_conflicting_params(self, temp_db):
        """notesとcanonical同時指定でエラー"""
        add_topic(title="T", description="D", tags=["domain:test"])

        result = update_tag("domain:test", notes="note", canonical="domain:other")
        assert "error" in result
        assert result["error"]["code"] == "CONFLICTING_PARAMS"

    def test_chain_not_allowed_forward(self, temp_db):
        """canonical先がエイリアスの場合エラー"""
        add_topic(title="T", description="D", tags=["domain:a", "domain:b", "domain:c"])
        # domain:b → domain:a のエイリアスにする
        update_tag("domain:b", canonical="domain:a")

        # domain:c → domain:b (エイリアス)にしようとする → エラー
        result = update_tag("domain:c", canonical="domain:b")
        assert "error" in result
        assert result["error"]["code"] == "CHAIN_NOT_ALLOWED"

    def test_chain_not_allowed_backward(self, temp_db):
        """自分が他タグのcanonical先の場合エラー"""
        add_topic(title="T", description="D", tags=["domain:a", "domain:b", "domain:c"])
        # domain:b → domain:a のエイリアスにする
        update_tag("domain:b", canonical="domain:a")

        # domain:a を domain:c のエイリアスにしようとする → エラー（domain:bが依存）
        result = update_tag("domain:a", canonical="domain:c")
        assert "error" in result
        assert result["error"]["code"] == "CHAIN_NOT_ALLOWED"

    def test_has_notes_error(self, temp_db):
        """notes付きタグをエイリアスにしようとしてエラー"""
        add_topic(title="T", description="D", tags=["domain:BE", "prm"])
        update_tag("prm", notes="重要な教訓")

        result = update_tag("prm", canonical="domain:BE")
        assert "error" in result
        assert result["error"]["code"] == "HAS_NOTES"

    def test_not_found_canonical_target(self, temp_db):
        """canonical先タグが存在しない場合エラー"""
        add_topic(title="T", description="D", tags=["prm"])

        result = update_tag("prm", canonical="domain:nonexistent")
        assert "error" in result
        assert result["error"]["code"] == "NOT_FOUND"

    def test_not_found_source_tag(self, temp_db):
        """ソースタグが存在しない場合エラー"""
        result = update_tag("nonexistent", canonical="domain:BE")
        assert "error" in result
        assert result["error"]["code"] == "NOT_FOUND"


# ========================================
# E2E フロー テスト
# ========================================


class TestE2EFlow:
    """エンドツーエンドフローテスト"""

    def test_alias_search_resolves_via_canonical(self, temp_db):
        """タグ付きtopic作成 → エイリアス設定 → エイリアス名で検索するとcanonical側で解決"""
        # 1. topicをdomain:BEタグで作成
        topic = add_topic(title="Backend Topic", description="BE work", tags=["domain:BE"])

        # 2. prmタグを作成してdomain:BEのエイリアスにする
        conn = get_connection()
        try:
            _create_tag(conn, "", "prm")
            conn.commit()
        finally:
            conn.close()
        update_tag("prm", canonical="domain:BE")

        # 3. prmでタグ解決するとdomain:BEのIDが返る
        conn = get_connection()
        try:
            # _resolve_tag_ids_readonlyで確認
            canonical_ids = _resolve_tag_ids_readonly(conn, ["prm"])
            be_ids = _resolve_tag_ids_readonly(conn, ["domain:BE"])
            assert canonical_ids == be_ids

            # resolve_tag_idsでも確認
            from src.services.tag_service import parse_tag
            canonical_ids2 = resolve_tag_ids(conn, [parse_tag("prm")])
            be_ids2 = resolve_tag_ids(conn, [parse_tag("domain:BE")])
            assert canonical_ids2 == be_ids2
        finally:
            conn.close()

    def test_new_record_with_alias_tag_links_to_canonical(self, temp_db):
        """エイリアス設定後にエイリアスタグ名で記録するとcanonical側IDで紐付く"""
        # 1. タグ作成とエイリアス設定
        add_topic(title="Setup", description="D", tags=["domain:BE", "prm"])
        update_tag("prm", canonical="domain:BE")

        # 2. prmタグ名でtopic作成
        topic = add_topic(title="New Topic", description="Created with alias", tags=["prm"])

        # 3. topic_tagsにはdomain:BE側のIDで紐付いているか確認
        conn = get_connection()
        try:
            be_row = conn.execute(
                "SELECT id FROM tags WHERE namespace = 'domain' AND name = 'BE'"
            ).fetchone()
            canonical_id = be_row["id"]

            rows = conn.execute(
                "SELECT tag_id FROM topic_tags WHERE topic_id = ?",
                (topic["topic_id"],),
            ).fetchall()
            tag_ids = [r["tag_id"] for r in rows]
            assert canonical_id in tag_ids
        finally:
            conn.close()
