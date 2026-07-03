"""migration 0050_cleanup_vec_orphans のテスト

0050 適用後に、対応する search_index 行を持たない vec_index の孤児行が削除され、
対応行を持つ vec_index 行は 1 行も消えないことを確認する。
"""
import os
import tempfile

import pytest
from sqlite_vec import serialize_float32
from yoyo import default_migration_table, read_migrations
from yoyo.connections import parse_uri
from yoyo.migrations import MigrationList

from src.db import MIGRATIONS_DIR, _VecSQLiteBackend, get_connection, init_database
from src.services.tag_service import _injected_tags

EMBEDDING_DIM = 384


@pytest.fixture
def migrated_db():
    """全 migration（0050 含む）を適用済みのテスト用 DB を提供する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def db_before_0050():
    """0049 までの migration を適用した DB を提供する。0050 の挙動を分離検証するために使う。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path

        parsed = parse_uri(f"sqlite:///{db_path}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        backend.init_database()
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        pre_0050 = MigrationList([m for m in all_migs if m.id < "0050"])
        with backend.lock():
            backend.apply_migrations(pre_0050)

        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


def _apply_migration_0050(db_path: str) -> None:
    """db_path に対して migration 0050 のみを適用する。"""
    parsed = parse_uri(f"sqlite:///{db_path}")
    backend = _VecSQLiteBackend(parsed, default_migration_table)
    all_migs = read_migrations(str(MIGRATIONS_DIR))
    only_0050 = MigrationList([m for m in all_migs if m.id.startswith("0050")])
    with backend.lock():
        backend.apply_migrations(only_0050)


def _make_embedding(seed: float) -> bytes:
    return serialize_float32([seed] * EMBEDDING_DIM)


class TestOrphanRowsRemoved:
    """search_index に対応行の無い vec_index 行が 0050 適用で削除されることの確認"""

    def test_orphan_vec_rows_deleted_after_0050(self, db_before_0050):
        """search_index に無い rowid を持つ vec_index 行は 0050 適用後に消える"""
        conn = get_connection()
        try:
            # 実エンティティ（search_indexに行がある）
            real_cur = conn.execute(
                "INSERT INTO decisions (decision, reason) VALUES (?, ?)",
                ("実在決定", "理由"),
            )
            conn.commit()
            real_search_id = conn.execute(
                "SELECT id FROM search_index WHERE source_type='decision' AND source_id=?",
                (real_cur.lastrowid,),
            ).fetchone()["id"]

            conn.execute(
                "INSERT INTO vec_index(rowid, embedding) VALUES (?, ?)",
                (real_search_id, _make_embedding(0.1)),
            )
            # 孤児: search_indexに対応行が無いrowid
            orphan_rowid = real_search_id + 10000
            conn.execute(
                "INSERT INTO vec_index(rowid, embedding) VALUES (?, ?)",
                (orphan_rowid, _make_embedding(0.2)),
            )
            conn.commit()

            # 前提確認: 適用前は両方存在する
            rowids_before = {
                row["rowid"]
                for row in conn.execute("SELECT rowid FROM vec_index").fetchall()
            }
            assert real_search_id in rowids_before
            assert orphan_rowid in rowids_before
        finally:
            conn.close()

        _apply_migration_0050(db_before_0050)

        conn = get_connection()
        try:
            rowids_after = {
                row["rowid"]
                for row in conn.execute("SELECT rowid FROM vec_index").fetchall()
            }
            assert orphan_rowid not in rowids_after, (
                "search_index に対応行の無い孤児 vec_index 行が 0050 適用後も残っている"
            )
            assert real_search_id in rowids_after, (
                "search_index に対応行のある vec_index 行が誤って削除された"
            )
        finally:
            conn.close()

    def test_multiple_orphans_all_removed(self, db_before_0050):
        """複数の孤児行がすべて削除される"""
        conn = get_connection()
        try:
            orphan_rowids = [900001, 900002, 900003]
            for i, rowid in enumerate(orphan_rowids):
                conn.execute(
                    "INSERT INTO vec_index(rowid, embedding) VALUES (?, ?)",
                    (rowid, _make_embedding(0.1 * (i + 1))),
                )
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0050(db_before_0050)

        conn = get_connection()
        try:
            remaining = {
                row["rowid"]
                for row in conn.execute("SELECT rowid FROM vec_index").fetchall()
            }
            for rowid in orphan_rowids:
                assert rowid not in remaining
        finally:
            conn.close()


class TestNonOrphanRowsPreserved:
    """search_index に対応行を持つ vec_index 行が 1 行も消えないことの確認"""

    def test_all_valid_vec_rows_survive_migration(self, db_before_0050):
        """search_index の全行に対応する vec_index 行を作った状態で 0050 を適用しても、
        1 行も消えない"""
        conn = get_connection()
        try:
            search_ids = []
            for i in range(5):
                cur = conn.execute(
                    "INSERT INTO discussion_logs (title, content) VALUES (?, ?)",
                    (f"ログ{i}", f"内容{i}"),
                )
                conn.commit()
                sid = conn.execute(
                    "SELECT id FROM search_index WHERE source_type='log' AND source_id=?",
                    (cur.lastrowid,),
                ).fetchone()["id"]
                search_ids.append(sid)

            for i, sid in enumerate(search_ids):
                conn.execute(
                    "INSERT INTO vec_index(rowid, embedding) VALUES (?, ?)",
                    (sid, _make_embedding(0.01 * (i + 1))),
                )
            conn.commit()

            before_count = conn.execute(
                "SELECT COUNT(*) AS c FROM vec_index"
            ).fetchone()["c"]
        finally:
            conn.close()

        _apply_migration_0050(db_before_0050)

        conn = get_connection()
        try:
            after_count = conn.execute(
                "SELECT COUNT(*) AS c FROM vec_index"
            ).fetchone()["c"]
            assert after_count == before_count, (
                "search_index に対応行を持つ vec_index 行が 0050 適用で減ってしまった"
            )
            remaining = {
                row["rowid"]
                for row in conn.execute("SELECT rowid FROM vec_index").fetchall()
            }
            assert remaining == set(search_ids)
        finally:
            conn.close()

    def test_no_orphans_no_op(self, migrated_db):
        """孤児が無い状態でも 0050 適用済みDBの初期化は正常に完走する（no-op確認）"""
        conn = get_connection()
        try:
            count = conn.execute("SELECT COUNT(*) AS c FROM vec_index").fetchone()["c"]
            assert count == 0
        finally:
            conn.close()
