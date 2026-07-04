"""migration 0052_add_tag_junction_indexes のテスト

0052 適用後に topic_tags / activity_tags / decision_tags / log_tags への
tag_id 逆引き index と search_index(created_at) の index が作成され、
既存データ・タグフィルタ結果が変わらないことを確認する。
"""
import os
import tempfile

import pytest
from yoyo import default_migration_table, read_migrations
from yoyo.connections import parse_uri
from yoyo.migrations import MigrationList

from src.db import MIGRATIONS_DIR, _VecSQLiteBackend, get_connection, init_database
from src.services.tag_service import _injected_tags, ensure_tag_ids, link_tags
from test_migrations.conftest import index_names


@pytest.fixture
def migrated_db():
    """全 migration（0052 含む）を適用済みのテスト用 DB を提供する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def db_before_0052():
    """0048 までの migration を適用した DB を提供する。0052 の挙動を分離検証するために使う。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path

        parsed = parse_uri(f"sqlite:///{db_path}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        backend.init_database()
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        pre_0052 = MigrationList([m for m in all_migs if m.id < "0052"])
        with backend.lock():
            backend.apply_migrations(pre_0052)

        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


def _apply_migration_0052(db_path: str) -> None:
    """db_path に対して migration 0052 のみを適用する。"""
    parsed = parse_uri(f"sqlite:///{db_path}")
    backend = _VecSQLiteBackend(parsed, default_migration_table)
    all_migs = read_migrations(str(MIGRATIONS_DIR))
    only_0052 = MigrationList([m for m in all_migs if m.id.startswith("0052")])
    with backend.lock():
        backend.apply_migrations(only_0052)


class TestIndexesCreated:
    """0052 適用後に 5 本の index が作成されることの確認"""

    EXPECTED_INDEXES = {
        "idx_topic_tags_tag",
        "idx_activity_tags_tag",
        "idx_decision_tags_tag",
        "idx_log_tags_tag",
        "idx_search_index_created_at",
    }

    def test_indexes_exist_after_0052(self, migrated_db):
        """0052 適用後、5 本の index すべてが sqlite_master に存在する"""
        conn = get_connection()
        try:
            existing = set()
            for pattern in ["idx_topic_tags_tag", "idx_activity_tags_tag",
                             "idx_decision_tags_tag", "idx_log_tags_tag",
                             "idx_search_index_created_at"]:
                existing |= index_names(conn, pattern)
            for idx in self.EXPECTED_INDEXES:
                assert idx in existing, f"インデックス {idx} が 0052 適用後に存在しない"
        finally:
            conn.close()

    def test_indexes_not_exist_before_0052(self, db_before_0052):
        """0052 適用前は 5 本の index がいずれも存在しない（前提確認）"""
        conn = get_connection()
        try:
            existing = set()
            for pattern in ["idx_topic_tags_tag", "idx_activity_tags_tag",
                             "idx_decision_tags_tag", "idx_log_tags_tag",
                             "idx_search_index_created_at"]:
                existing |= index_names(conn, pattern)
            assert existing.isdisjoint(self.EXPECTED_INDEXES), (
                "0052 適用前に対象インデックスが既に存在している"
            )
        finally:
            conn.close()

    def test_indexes_target_tag_id_column(self, migrated_db):
        """junction 4 本の index が tag_id カラムを対象にしている"""
        conn = get_connection()
        try:
            for table, idx in [
                ("topic_tags", "idx_topic_tags_tag"),
                ("activity_tags", "idx_activity_tags_tag"),
                ("decision_tags", "idx_decision_tags_tag"),
                ("log_tags", "idx_log_tags_tag"),
            ]:
                rows = conn.execute(f"PRAGMA index_info({idx})").fetchall()
                assert len(rows) == 1, f"{idx} は単一カラムindexであるべき"
                assert rows[0]["name"] == "tag_id", (
                    f"{idx} は tag_id を対象にすべき（table={table}）"
                )
        finally:
            conn.close()

    def test_search_index_created_at_index_targets_created_at(self, migrated_db):
        """idx_search_index_created_at が created_at カラムを対象にしている"""
        conn = get_connection()
        try:
            rows = conn.execute(
                "PRAGMA index_info(idx_search_index_created_at)"
            ).fetchall()
            assert len(rows) == 1
            assert rows[0]["name"] == "created_at"
        finally:
            conn.close()


class TestExistingDataPreserved:
    """0052 適用前に投入したタグ紐付けデータが、適用後も破壊されないことの確認"""

    def test_existing_tag_links_preserved(self, db_before_0052):
        """0052 適用前の topic_tags / activity_tags / decision_tags / log_tags 行は
        適用後も残る"""
        conn = get_connection()
        try:
            topic_cur = conn.execute(
                "INSERT INTO discussion_topics (title, description) VALUES (?, ?)",
                ("既存トピック", "説明"),
            )
            topic_id = topic_cur.lastrowid
            activity_cur = conn.execute(
                "INSERT INTO activities (title, description, status) VALUES (?, ?, ?)",
                ("既存activity", "説明", "in_progress"),
            )
            activity_id = activity_cur.lastrowid
            decision_cur = conn.execute(
                "INSERT INTO decisions (decision, reason) VALUES (?, ?)",
                ("既存決定", "既存理由"),
            )
            decision_id = decision_cur.lastrowid
            log_cur = conn.execute(
                "INSERT INTO discussion_logs (title, content) VALUES (?, ?)",
                ("既存ログ", "内容"),
            )
            log_id = log_cur.lastrowid
            conn.commit()

            tag_ids = ensure_tag_ids(conn, [("domain", "hygiene-test")])
            link_tags(conn, "topic_tags", "topic_id", topic_id, tag_ids)
            link_tags(conn, "activity_tags", "activity_id", activity_id, tag_ids)
            link_tags(conn, "decision_tags", "decision_id", decision_id, tag_ids)
            link_tags(conn, "log_tags", "log_id", log_id, tag_ids)
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0052(db_before_0052)

        conn = get_connection()
        try:
            tag_id = tag_ids[0]
            assert conn.execute(
                "SELECT 1 FROM topic_tags WHERE topic_id = ? AND tag_id = ?",
                (topic_id, tag_id),
            ).fetchone() is not None
            assert conn.execute(
                "SELECT 1 FROM activity_tags WHERE activity_id = ? AND tag_id = ?",
                (activity_id, tag_id),
            ).fetchone() is not None
            assert conn.execute(
                "SELECT 1 FROM decision_tags WHERE decision_id = ? AND tag_id = ?",
                (decision_id, tag_id),
            ).fetchone() is not None
            assert conn.execute(
                "SELECT 1 FROM log_tags WHERE log_id = ? AND tag_id = ?",
                (log_id, tag_id),
            ).fetchone() is not None
        finally:
            conn.close()


class TestTagFilterResultsUnchanged:
    """index 追加が検索結果の集合に影響しないことの確認（性能のみの変更である裏取り）"""

    def test_tag_filter_query_result_unchanged_by_index(self, migrated_db):
        """tag_id で絞る典型クエリが、期待通りの行集合を返す
        （index はクエリプランを変えるだけで結果集合は変わらない）"""
        conn = get_connection()
        try:
            d1 = conn.execute(
                "INSERT INTO decisions (decision, reason) VALUES (?, ?)",
                ("対象決定", "理由1"),
            ).lastrowid
            d2 = conn.execute(
                "INSERT INTO decisions (decision, reason) VALUES (?, ?)",
                ("対象外決定", "理由2"),
            ).lastrowid
            conn.commit()

            tag_ids = ensure_tag_ids(conn, [("domain", "filter-target")])
            link_tags(conn, "decision_tags", "decision_id", d1, tag_ids)
            conn.commit()

            rows = conn.execute(
                "SELECT decision_id FROM decision_tags WHERE tag_id = ?",
                (tag_ids[0],),
            ).fetchall()
            matched_ids = {row["decision_id"] for row in rows}
            assert matched_ids == {d1}
            assert d2 not in matched_ids
        finally:
            conn.close()
