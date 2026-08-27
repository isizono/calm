"""migration 0072_repair_search_index_fts_orphans のテスト

0072適用前に存在する search_index_fts の孤立rowid・rowid衝突による内容混在が、
0072適用後に解消され、search_indexの現在の内容のみを正しく反映することを確認する。
"""
import os
import tempfile

import pytest
from yoyo import default_migration_table, read_migrations
from yoyo.connections import parse_uri
from yoyo.migrations import MigrationList

from src.db import MIGRATIONS_DIR, _VecSQLiteBackend, get_connection
from src.services.tag_service import _injected_tags


@pytest.fixture
def db_before_0072():
    """0071までのmigrationを適用したDBを提供する。0072の挙動を分離検証するために使う。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path

        parsed = parse_uri(f"sqlite:///{db_path}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        backend.init_database()
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        pre_0072 = MigrationList([m for m in all_migs if m.id < "0072"])
        with backend.lock():
            backend.apply_migrations(pre_0072)

        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


def _apply_migration_0072(db_path: str) -> None:
    """db_path に対して migration 0072 のみを適用する。"""
    parsed = parse_uri(f"sqlite:///{db_path}")
    backend = _VecSQLiteBackend(parsed, default_migration_table)
    all_migs = read_migrations(str(MIGRATIONS_DIR))
    only_0072 = MigrationList([m for m in all_migs if m.id.startswith("0072")])
    with backend.lock():
        backend.apply_migrations(only_0072)


def _orphan_fts_rowids(conn) -> set[int]:
    rows = conn.execute(
        "SELECT search_index_fts.rowid FROM search_index_fts "
        "LEFT JOIN search_index ON search_index.id = search_index_fts.rowid "
        "WHERE search_index.id IS NULL"
    ).fetchall()
    return {row["rowid"] for row in rows}


class TestOrphanRowsRemoved:
    """search_indexに対応行の無いsearch_index_fts行が0072適用で消えることの確認"""

    def test_orphan_fts_row_removed_after_0072(self, db_before_0072):
        """0071時点のバグ(retract undoの再登録なし)を模擬して作った孤立FTS行が消える"""
        conn = get_connection()
        try:
            cur = conn.execute(
                "INSERT INTO materials (title, content) VALUES (?, ?)",
                ("実在資材", "本文survivor_marker_def"),
            )
            conn.commit()
            real_si_id = conn.execute(
                "SELECT id FROM search_index WHERE source_type='material' AND source_id=?",
                (cur.lastrowid,),
            ).fetchone()["id"]

            # 孤立行: search_indexに対応行の無いrowid(旧バグでUPDATEトリガーの
            # サブクエリがNULLを返しFTS5が自動採番した状態を模擬)
            orphan_rowid = real_si_id + 100
            conn.execute(
                "INSERT INTO search_index_fts (rowid, title, body) VALUES (?, ?, ?)",
                (orphan_rowid, "孤立タイトル", "孤立本文"),
            )
            conn.commit()

            assert orphan_rowid in _orphan_fts_rowids(conn)
        finally:
            conn.close()

        _apply_migration_0072(db_before_0072)

        conn = get_connection()
        try:
            assert _orphan_fts_rowids(conn) == set()
            # 実在行はそのまま検索できる
            hit = conn.execute(
                "SELECT rowid FROM search_index_fts WHERE rowid = ? AND search_index_fts MATCH 'survivor_marker_def'",
                (real_si_id,),
            ).fetchone()
            assert hit is not None
        finally:
            conn.close()


class TestRowidCollisionContentRepaired:
    """rowid衝突で別エンティティの内容が混在していた行が0072適用で正しい内容のみになることの確認"""

    def test_stale_content_at_colliding_rowid_is_overwritten(self, db_before_0072):
        """同一rowidに旧エンティティ(取り消し済み)の孤立トークンと新エンティティの
        正規トークンが両方積まれてしまった状態(バグのcollisionシナリオ。contentless
        FTS5は同一rowidへの複数回INSERTでトークンを追加accumulateするだけで
        上書きしないため、削除漏れの孤立行の上に正規insertが乗ると内容が混在する)を
        直接構築し、0072適用後はそのrowidの内容が現在のsearch_index行(新エンティティ)
        のものだけになることを確認する。"""
        conn = get_connection()
        try:
            cur = conn.execute(
                "INSERT INTO materials (title, content) VALUES (?, ?)",
                ("新エンティティ", "新しい本文 fresh_marker_abc"),
            )
            conn.commit()
            new_si_id = conn.execute(
                "SELECT id FROM search_index WHERE source_type='material' AND source_id=?",
                (cur.lastrowid,),
            ).fetchone()["id"]

            # 同じrowidに、取り消し済みだった旧エンティティの孤立トークンを追加で積む
            # (旧UPDATEトリガーがsearch_index行の無い状態でNULL rowid採番INSERTした結果、
            # 後から正規採番された新エンティティのsearch_index.idと衝突した状態を模擬)
            conn.execute(
                "INSERT INTO search_index_fts (rowid, title, body) VALUES (?, ?, ?)",
                (new_si_id, "旧エンティティ", "取り消し済みの本文 stale_marker_xyz"),
            )
            conn.commit()

            # 修復前: そのrowidでstale_marker_xyzもヒットしてしまう(混在)
            stale_hit = conn.execute(
                "SELECT rowid FROM search_index_fts WHERE rowid = ? AND search_index_fts MATCH 'stale_marker_xyz'",
                (new_si_id,),
            ).fetchone()
            assert stale_hit is not None
        finally:
            conn.close()

        _apply_migration_0072(db_before_0072)

        conn = get_connection()
        try:
            # 修復後: そのrowidはsearch_index(新エンティティ)の内容のみを反映し、
            # 旧エンティティの本文ではもうヒットしない
            stale_hit_after = conn.execute(
                "SELECT rowid FROM search_index_fts WHERE rowid = ? AND search_index_fts MATCH 'stale_marker_xyz'",
                (new_si_id,),
            ).fetchone()
            assert stale_hit_after is None

            fresh_hit_after = conn.execute(
                "SELECT rowid FROM search_index_fts WHERE rowid = ? AND search_index_fts MATCH 'fresh_marker_abc'",
                (new_si_id,),
            ).fetchone()
            assert fresh_hit_after is not None
        finally:
            conn.close()


class TestNonOrphanRowsPreserved:
    """search_indexに対応行を持つsearch_index_fts行が0072適用で1件も消えないことの確認"""

    def test_all_valid_entities_still_searchable_after_0072(self, db_before_0072):
        conn = get_connection()
        try:
            markers = []
            for i in range(4):
                marker = f"entity_marker_{i}"
                conn.execute(
                    "INSERT INTO decisions (decision, reason) VALUES (?, ?)",
                    (f"決定{i}", f"理由{i} {marker}"),
                )
                markers.append(marker)
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0072(db_before_0072)

        conn = get_connection()
        try:
            for marker in markers:
                hit = conn.execute(
                    "SELECT rowid FROM search_index_fts WHERE search_index_fts MATCH ?",
                    (marker,),
                ).fetchall()
                assert len(hit) == 1, f"{marker} は0072適用後もちょうど1件ヒットするはず"
        finally:
            conn.close()
