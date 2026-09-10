"""material_service: _add_material_with_conn / _append_material_content_with_conn の
conn共有契約のユニットテスト。

add_material/update_material 経由の結合的な振る舞い（citation変換・タグリンク等）は
tests/integration/test_citations_service.py 等の既存テストで既にカバーされている。
本ファイルは、demote_tag_notes から直接呼ばれる conn 共有版の2関数が守るべき契約
（呼び出し元の conn・トランザクションを共有し、自前で接続やcommitをしないこと）を
対象にする。
"""
import pytest

from src.db import get_connection
from src.services import material_service
from src.services.material_service import (
    _add_material_with_conn,
    _append_material_content_with_conn,
)


@pytest.fixture(autouse=True)
def _auto_disable_embedding(disable_embedding):
    """このファイル内の全テストでembedding呼び出しを無効化する"""


def _count_materials(conn) -> int:
    return conn.execute("SELECT COUNT(*) AS c FROM materials").fetchone()["c"]


class TestAddMaterialWithConn:
    def test_does_not_open_its_own_connection(self, temp_db, monkeypatch):
        """呼び出し元のconnを共有し、自前でget_connectionを呼ばないことを保証する
        （N+1接続化・別トランザクション化を防ぐ契約）。"""

        def _fail_get_connection(*args, **kwargs):
            raise AssertionError("_add_material_with_conn must not open its own connection")

        monkeypatch.setattr(material_service, "get_connection", _fail_get_connection)

        conn = get_connection()
        try:
            result = _add_material_with_conn(
                conn, title="T", content="C", tags=["domain:test"], source="s",
            )
            assert "error" not in result
            conn.commit()
        finally:
            conn.close()

    def test_does_not_commit_itself(self, temp_db):
        """commitは呼び出し元の責務。呼び出し元がrollbackすればINSERTごと消える。"""
        conn = get_connection()
        try:
            result = _add_material_with_conn(
                conn, title="T", content="C", tags=["domain:test"], source="s",
            )
            assert "error" not in result
            material_id = result["material_id"]
            conn.rollback()
        finally:
            conn.close()

        conn2 = get_connection()
        try:
            row = conn2.execute(
                "SELECT * FROM materials WHERE id = ?", (material_id,)
            ).fetchone()
        finally:
            conn2.close()
        assert row is None

    def test_returns_material_id_title_content_and_tag_strings(self, temp_db):
        conn = get_connection()
        try:
            result = _add_material_with_conn(
                conn, title="タイトル", content="本文", tags=["domain:test", "hooks"], source="s",
            )
            conn.commit()
        finally:
            conn.close()

        assert "error" not in result
        assert isinstance(result["material_id"], int)
        assert result["title"] == "タイトル"
        assert result["content"] == "本文"
        assert set(result["tag_strings"]) == {"domain:test", "hooks"}
        assert result["citations_converted"] == 0

    def test_invalid_tags_returns_error_without_inserting_a_row(self, temp_db):
        conn = get_connection()
        try:
            before = _count_materials(conn)
            result = _add_material_with_conn(
                conn, title="T", content="C", tags=["ns with spaces:x"], source="s",
            )
            after = _count_materials(conn)
        finally:
            conn.rollback()
            conn.close()

        assert "error" in result
        assert after == before

    def test_related_target_is_linked_within_the_shared_transaction(self, temp_db):
        """relatedのリレーション追加が同一connのトランザクション内で行われる。"""
        from src.services.activity_service import add_activity

        activity = add_activity(
            title="関連先", description="d", tags=["domain:test"], check_in=False
        )
        activity_id = activity["activity_id"]

        conn = get_connection()
        try:
            result = _add_material_with_conn(
                conn, title="T", content="C", tags=["domain:test"], source="s",
                related=[{"type": "activity", "ids": [activity_id]}],
            )
            assert "error" not in result
            material_id = result["material_id"]
            # relations_view は双方向をUNION ALL済みなので、正規化時の
            # source/target入れ替え（type名の辞書順）を気にせず片方向で照合できる
            rel = conn.execute(
                "SELECT 1 FROM relations_view WHERE source_type='material' AND source_id=? "
                "AND target_type='activity' AND target_id=?",
                (material_id, activity_id),
            ).fetchone()
            assert rel is not None
            conn.rollback()
        finally:
            conn.close()

    def test_citations_converted_counts_raw_id_conversions_in_content(self, temp_db):
        """本文中の生ID参照が {{cite:...}} へ変換された件数を citations_converted として返す。"""
        conn = get_connection()
        try:
            target = _add_material_with_conn(
                conn, title="対象資材", content="対象本文", tags=["domain:test"], source="s",
            )
            conn.commit()
        finally:
            conn.close()
        target_id = target["material_id"]

        conn = get_connection()
        try:
            result = _add_material_with_conn(
                conn,
                title="参照元資材",
                content=f"見よ M#{target_id} 。",
                tags=["domain:test"],
                source="s",
            )
            conn.commit()
        finally:
            conn.close()

        assert result["citations_converted"] == 1
        assert result["content"] == f"見よ {{{{cite:M#{target_id}}}}} 。"


class TestAppendMaterialContentWithConn:
    def _create_material(self, conn, content="既存本文"):
        result = _add_material_with_conn(
            conn, title="追記対象", content=content, tags=["domain:test"], source="s",
        )
        assert "error" not in result
        return result["material_id"]

    def test_does_not_open_its_own_connection(self, temp_db, monkeypatch):
        conn = get_connection()
        try:
            material_id = self._create_material(conn)
            conn.commit()
        finally:
            conn.close()

        def _fail_get_connection(*args, **kwargs):
            raise AssertionError("_append_material_content_with_conn must not open its own connection")

        monkeypatch.setattr(material_service, "get_connection", _fail_get_connection)

        conn = get_connection()
        try:
            result = _append_material_content_with_conn(conn, material_id, "追記分")
            assert "error" not in result
            conn.commit()
        finally:
            conn.close()

    def test_appends_with_separator_and_does_not_commit_itself(self, temp_db):
        conn = get_connection()
        try:
            material_id = self._create_material(conn, content="既存本文")
            conn.commit()
        finally:
            conn.close()

        conn = get_connection()
        try:
            result = _append_material_content_with_conn(conn, material_id, "追記分")
            assert result["content"] == "既存本文" + material_service.CONTENT_JOIN_SEPARATOR + "追記分"
            conn.rollback()
        finally:
            conn.close()

        conn2 = get_connection()
        try:
            row = conn2.execute(
                "SELECT content FROM materials WHERE id = ?", (material_id,)
            ).fetchone()
        finally:
            conn2.close()
        # rollbackしたのでDB上の値は追記前のまま
        assert row["content"] == "既存本文"

    def test_committed_append_persists_and_updates_tag_strings(self, temp_db):
        conn = get_connection()
        try:
            material_id = self._create_material(conn, content="既存本文")
            conn.commit()
        finally:
            conn.close()

        conn = get_connection()
        try:
            result = _append_material_content_with_conn(conn, material_id, "追記分2")
            conn.commit()
        finally:
            conn.close()

        assert result["content"] == "既存本文" + material_service.CONTENT_JOIN_SEPARATOR + "追記分2"
        assert result["tag_strings"] == ["domain:test"]

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT content FROM materials WHERE id = ?", (material_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row["content"] == result["content"]
