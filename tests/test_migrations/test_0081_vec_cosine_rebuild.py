"""migration 0081_vec_cosine_rebuild のテスト

vec_index / tag_vec を distance_metric=cosine で再構築するmigrationについて、
(1) 再構築前後で行数・rowid集合が不変であること
(2) 再構築後もKNNクエリ(MATCH + k)が成功すること(shadow tables残骸によるサイレント
    破壊が起きていないことの確認)
(3) 再構築後のKNN序列がノルム非依存になる(cosineへの移行が実際に効いていること)
を確認する。
"""
import sqlite3

import pytest
from sqlite_vec import serialize_float32
from yoyo import default_migration_table, read_migrations
from yoyo.connections import parse_uri
from yoyo.migrations import MigrationList

from src.db import MIGRATIONS_DIR, _VecSQLiteBackend, get_connection
from src.services.tag_service import _injected_tags
from test_migrations.conftest import db_before_migration

EMBEDDING_DIM = 384


@pytest.fixture
def db_before_0081():
    """0080 までの migration を適用した DB を提供する。0081 の挙動を分離検証するために使う。"""
    with db_before_migration("0081") as db_path:
        _injected_tags.clear()
        yield db_path


def _apply_migration_0081(db_path: str) -> None:
    """db_path に対して migration 0081 のみを適用する。"""
    parsed = parse_uri(f"sqlite:///{db_path}")
    backend = _VecSQLiteBackend(parsed, default_migration_table)
    all_migs = read_migrations(str(MIGRATIONS_DIR))
    only_0081 = MigrationList([m for m in all_migs if m.id.startswith("0081")])
    with backend.lock():
        backend.apply_migrations(only_0081)


def _vec(*values: float) -> bytes:
    """先頭に values、残りを0で埋めたEMBEDDING_DIM次元のembeddingをBLOB化する。"""
    v = list(values) + [0.0] * (EMBEDDING_DIM - len(values))
    return serialize_float32(v)


class TestRowsPreserved:
    """再構築前後で行数・rowid集合が不変であることの確認"""

    def test_vec_index_rowids_and_count_unchanged(self, db_before_0081):
        conn = get_connection()
        try:
            for rowid in (1, 2, 3):
                conn.execute(
                    "INSERT INTO vec_index(rowid, embedding) VALUES (?, ?)",
                    (rowid, _vec(float(rowid))),
                )
            conn.commit()
            before = {
                row["rowid"] for row in conn.execute("SELECT rowid FROM vec_index").fetchall()
            }
        finally:
            conn.close()

        _apply_migration_0081(db_before_0081)

        conn = get_connection()
        try:
            after = {
                row["rowid"] for row in conn.execute("SELECT rowid FROM vec_index").fetchall()
            }
            assert after == before, "vec_index再構築後にrowid集合が変化した"
        finally:
            conn.close()

    def test_tag_vec_rowids_and_count_unchanged(self, db_before_0081):
        conn = get_connection()
        try:
            for rowid in (10, 20):
                conn.execute(
                    "INSERT INTO tag_vec(rowid, embedding) VALUES (?, ?)",
                    (rowid, _vec(float(rowid))),
                )
            conn.commit()
            before = {
                row["rowid"] for row in conn.execute("SELECT rowid FROM tag_vec").fetchall()
            }
        finally:
            conn.close()

        _apply_migration_0081(db_before_0081)

        conn = get_connection()
        try:
            after = {
                row["rowid"] for row in conn.execute("SELECT rowid FROM tag_vec").fetchall()
            }
            assert after == before, "tag_vec再構築後にrowid集合が変化した"
        finally:
            conn.close()

    def test_tag_vec_empty_table_survives_rebuild(self, db_before_0081):
        """実DBのtag_vecは0行であり、空テーブルの退避・再構築が空振りで成立することを確認する。"""
        _apply_migration_0081(db_before_0081)

        conn = get_connection()
        try:
            count = conn.execute("SELECT COUNT(*) AS c FROM tag_vec").fetchone()["c"]
            assert count == 0
        finally:
            conn.close()


class TestKnnSucceedsAfterRebuild:
    """再構築後もKNNクエリが成功すること(shadow tables残骸によるサイレント破壊が無いこと)"""

    def test_vec_index_knn_succeeds(self, db_before_0081):
        _apply_migration_0081(db_before_0081)

        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO vec_index(rowid, embedding) VALUES (?, ?)",
                (1, _vec(1.0)),
            )
            conn.commit()
            rows = conn.execute(
                "SELECT rowid, distance FROM vec_index WHERE embedding MATCH ? AND k = ?",
                (_vec(1.0), 5),
            ).fetchall()
            assert len(rows) == 1
            assert rows[0]["rowid"] == 1
        finally:
            conn.close()

    def test_tag_vec_knn_succeeds(self, db_before_0081):
        _apply_migration_0081(db_before_0081)

        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO tag_vec(rowid, embedding) VALUES (?, ?)",
                (7, _vec(1.0)),
            )
            conn.commit()
            rows = conn.execute(
                "SELECT rowid, distance FROM tag_vec WHERE embedding MATCH ? AND k = ?",
                (_vec(1.0), 5),
            ).fetchall()
            assert len(rows) == 1
            assert rows[0]["rowid"] == 7
        finally:
            conn.close()


class TestCosineOrderingAfterRebuild:
    """再構築後のKNN序列がノルム非依存になること(cosine化が実際に効いていること)"""

    def test_vec_index_ranks_direction_over_norm(self, db_before_0081):
        """クエリと方向が一致する大ノルムのベクトルが、方向がずれた小ノルムの
        ベクトルより上位に来る(移行前のL2では逆順になることを先に確認したうえで、
        移行後にcosineへ反転することを確認する)。

        query=[1,0,...] に対し、aligned=[100,0,...](方向一致・大ノルム)と
        off_axis=[0.1,0.1,0,...](方向ズレ・小ノルム)を比較する。
        L2ではoff_axis(距離≈0.906)がaligned(距離≈99)より近いと判定されるが、
        cosineではaligned(距離=0、完全一致)がoff_axis(距離≈0.293)より近くなる。
        """
        query = _vec(1.0)
        aligned_large_norm = _vec(100.0)
        off_axis_small_norm = _vec(0.1, 0.1)

        def _query_order() -> list[sqlite3.Row]:
            conn = get_connection()
            try:
                conn.execute("DELETE FROM vec_index")
                conn.execute(
                    "INSERT INTO vec_index(rowid, embedding) VALUES (?, ?)", (1, aligned_large_norm)
                )
                conn.execute(
                    "INSERT INTO vec_index(rowid, embedding) VALUES (?, ?)", (2, off_axis_small_norm)
                )
                conn.commit()
                return conn.execute(
                    "SELECT rowid, distance FROM vec_index WHERE embedding MATCH ? AND k = ?",
                    (query, 2),
                ).fetchall()
            finally:
                conn.close()

        # 適用前(L2): 方向がずれていてもノルムが小さいoff_axis(rowid=2)が近いと判定される逆順
        before_rows = _query_order()
        before_by_rowid = {row["rowid"]: row["distance"] for row in before_rows}
        assert before_by_rowid[2] < before_by_rowid[1], (
            "前提が崩れている: L2のままなら方向ズレでもノルムの小さいoff_axisが近くなるはず"
        )

        _apply_migration_0081(db_before_0081)

        # 適用後(cosine): 方向一致・大ノルムのaligned(rowid=1)が近い正順に反転する
        after_rows = _query_order()
        after_by_rowid = {row["rowid"]: row["distance"] for row in after_rows}
        assert after_by_rowid[1] < after_by_rowid[2], (
            "cosine化後は方向一致・大ノルムのrowid=1がoff_axisより近い(小さいdistance)はず"
        )
        # cosine距離は方向完全一致なら0
        assert after_by_rowid[1] == pytest.approx(0.0, abs=1e-4)
        # ORDER BY distance相当で上位(rowid=1)から返る
        assert after_rows[0]["rowid"] == 1
