"""migration 0050_add_topic_vec のテスト

0050 適用後に topic_vec 仮想テーブルが作成され、rowid をキーにした
ベクトルの INSERT / KNN 検索ができることを確認する。
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
from test_migrations.conftest import table_exists

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
    """0048 までの migration を適用した DB を提供する。0050 の挙動を分離検証するために使う。"""
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


class TestTopicVecTableCreated:
    def test_topic_vec_table_exists_after_0050(self, migrated_db):
        """migration 0050 適用後、topic_vec テーブルが存在する"""
        conn = get_connection()
        try:
            assert table_exists(conn, "topic_vec"), (
                "topic_vec テーブルが 0050 適用後に存在しない"
            )
        finally:
            conn.close()

    def test_topic_vec_table_not_exists_before_0050(self, db_before_0050):
        """0050 適用前は topic_vec テーブルが存在しない（前提確認）"""
        conn = get_connection()
        try:
            assert not table_exists(conn, "topic_vec"), (
                "0050 適用前に topic_vec テーブルが既に存在している"
            )
        finally:
            conn.close()


class TestTopicVecCRUD:
    def test_insert_and_knn_search(self, migrated_db):
        """topic_vec に rowid = topic_id でINSERTし、KNN検索で引ける"""
        conn = get_connection()
        try:
            cur = conn.execute(
                "INSERT INTO discussion_topics (title, description) VALUES (?, ?)",
                ("KNNテストトピック", "テスト"),
            )
            topic_id = cur.lastrowid
            conn.commit()

            embedding = [0.1] * EMBEDDING_DIM
            blob = serialize_float32(embedding)
            conn.execute(
                "INSERT INTO topic_vec(rowid, embedding) VALUES (?, ?)",
                (topic_id, blob),
            )
            conn.commit()

            row = conn.execute(
                "SELECT rowid, distance FROM topic_vec WHERE embedding MATCH ? AND k = ?",
                (blob, 1),
            ).fetchone()
            assert row is not None
            assert row["rowid"] == topic_id
            assert row["distance"] == pytest.approx(0.0, abs=1e-6)
        finally:
            conn.close()

    def test_only_topic_entities_in_knn_population(self, migrated_db):
        """topic_vec の母集団が topic のみである（他 entity を大量投入しても脱落しない）

        vec_index（全 entity 共用）はグローバル KNN 後に post-filter するため
        corpus 増大で topic が候補から脱落しうる。topic_vec は型専用テーブルの
        ため、他エンティティが何件あっても topic の KNN 結果には混入しない
        （母集団自体が topic だけであることを確認する）。
        """
        conn = get_connection()
        try:
            topic_ids = []
            for i in range(3):
                cur = conn.execute(
                    "INSERT INTO discussion_topics (title, description) VALUES (?, ?)",
                    (f"母集団テストトピック{i}", "テスト"),
                )
                topic_id = cur.lastrowid
                topic_ids.append(topic_id)
                embedding = [float(i)] * EMBEDDING_DIM
                conn.execute(
                    "INSERT INTO topic_vec(rowid, embedding) VALUES (?, ?)",
                    (topic_id, serialize_float32(embedding)),
                )

            # 大量の decision embedding を vec_index 側にのみ投入する
            # （topic_vec には影響しないはず）
            for i in range(20):
                cur = conn.execute(
                    "INSERT INTO decisions (decision, reason) VALUES (?, ?)",
                    (f"母集団攪乱decision{i}", "テスト"),
                )
                dec_id = cur.lastrowid
                si = conn.execute(
                    "SELECT id FROM search_index WHERE source_type = 'decision' AND source_id = ?",
                    (dec_id,),
                ).fetchone()
                assert si is not None
                conn.execute(
                    "INSERT INTO vec_index(rowid, embedding) VALUES (?, ?)",
                    (si["id"], serialize_float32([1.0] * EMBEDDING_DIM)),
                )
            conn.commit()

            rows = conn.execute(
                "SELECT rowid FROM topic_vec WHERE embedding MATCH ? AND k = ?",
                (serialize_float32([0.0] * EMBEDDING_DIM), 10),
            ).fetchall()
            result_ids = {r["rowid"] for r in rows}
            assert result_ids.issubset(set(topic_ids)), (
                "topic_vec の KNN 結果に topic 以外の rowid が混入している"
            )
            # 投入した 3 topic 全てが候補に含まれる（脱落していない）
            assert result_ids == set(topic_ids)
        finally:
            conn.close()

    def test_delete_removes_row(self, migrated_db):
        """topic_vec から DELETE すると行が消える（孤児防止の delete 経路確認）"""
        conn = get_connection()
        try:
            cur = conn.execute(
                "INSERT INTO discussion_topics (title, description) VALUES (?, ?)",
                ("削除テストトピック", "テスト"),
            )
            topic_id = cur.lastrowid
            conn.execute(
                "INSERT INTO topic_vec(rowid, embedding) VALUES (?, ?)",
                (topic_id, serialize_float32([0.2] * EMBEDDING_DIM)),
            )
            conn.commit()

            conn.execute("DELETE FROM topic_vec WHERE rowid = ?", (topic_id,))
            conn.commit()

            row = conn.execute(
                "SELECT rowid FROM topic_vec WHERE rowid = ?", (topic_id,)
            ).fetchone()
            assert row is None
        finally:
            conn.close()
