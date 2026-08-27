"""migration 0070_add_instance_meta のテスト

0070適用後にinstance_metaテーブルが期待通り存在し、単一行制約（id=1固定CHECK）が
機能することを、instance_serviceを経由せず生SQLで検証する。
"""
import os
import sqlite3
import tempfile

import pytest
from yoyo import default_migration_table, read_migrations
from yoyo.connections import parse_uri
from yoyo.migrations import MigrationList

from src.db import MIGRATIONS_DIR, _VecSQLiteBackend, get_connection, init_database
from src.services.tag_service import _injected_tags
from test_migrations.conftest import get_column_names, table_exists


@pytest.fixture
def migrated_db():
    """全migration（0070含む）を適用済みのテスト用DBを提供する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def db_before_0070():
    """0069までのmigrationを適用したDBを提供する。0070の挙動を分離検証するために使う。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path

        parsed = parse_uri(f"sqlite:///{db_path}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        backend.init_database()
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        pre_0070 = MigrationList([m for m in all_migs if m.id < "0070"])
        with backend.lock():
            backend.apply_migrations(pre_0070)

        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


class TestInstanceMetaTable:
    def test_table_does_not_exist_before_0070(self, db_before_0070):
        conn = get_connection()
        try:
            exists = table_exists(conn, "instance_meta")
        finally:
            conn.close()
        assert exists is False

    def test_table_exists_after_0070(self, migrated_db):
        conn = get_connection()
        try:
            exists = table_exists(conn, "instance_meta")
            columns = get_column_names(conn, "instance_meta")
        finally:
            conn.close()
        assert exists is True
        assert columns == {"id", "instance_id", "created_at"}

    def test_insert_with_id_1_succeeds(self, migrated_db):
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO instance_meta (id, instance_id) VALUES (1, 'team-a')"
            )
            conn.commit()
            row = conn.execute(
                "SELECT instance_id, created_at FROM instance_meta WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()
        assert row["instance_id"] == "team-a"
        assert row["created_at"] is not None

    def test_insert_with_id_other_than_1_rejected(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO instance_meta (id, instance_id) VALUES (2, 'team-b')"
                )
        finally:
            conn.rollback()
            conn.close()

    def test_second_row_insert_rejected_by_primary_key(self, migrated_db):
        """id=1固定のPRIMARY KEY制約により、2行目のINSERT（id省略）は主キー重複で拒否される。"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO instance_meta (id, instance_id) VALUES (1, 'team-a')"
            )
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO instance_meta (id, instance_id) VALUES (1, 'team-b')"
                )
        finally:
            conn.rollback()
            conn.close()

    def test_instance_id_not_null_enforced(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO instance_meta (id) VALUES (1)")
        finally:
            conn.rollback()
            conn.close()
