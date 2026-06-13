"""migration 0036_add_materials_updated_at のテスト

0036適用後に materials テーブルへ updated_at列が追加され、
既存行の updated_at が created_at でバックフィルされることを確認する。
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


@pytest.fixture
def migrated_db():
    """全migration（0036含む）を適用済みのテスト用DBを提供する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def db_before_0036():
    """0035までのmigrationを適用したDBを提供する。0036の挙動を分離検証するために使う。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path

        parsed = parse_uri(f"sqlite:///{db_path}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        backend.init_database()
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        pre_0036 = MigrationList([m for m in all_migs if m.id < "0036"])
        with backend.lock():
            backend.apply_migrations(pre_0036)

        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


def _apply_migration_0036(db_path: str) -> None:
    """db_pathに対してmigration 0036のみを適用する。"""
    parsed = parse_uri(f"sqlite:///{db_path}")
    backend = _VecSQLiteBackend(parsed, default_migration_table)
    all_migs = read_migrations(str(MIGRATIONS_DIR))
    only_0036 = MigrationList([m for m in all_migs if m.id.startswith("0036")])
    with backend.lock():
        backend.apply_migrations(only_0036)


def _get_column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    """指定テーブルのカラム名セットを返す。"""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


class TestUpdatedAtColumnAdded:
    """0036適用後にupdated_at列が追加されていることの確認"""

    def test_materials_has_updated_at_column_after_0036(self, migrated_db):
        """migration 0036適用後、materialsテーブルにupdated_at列が存在する"""
        conn = get_connection()
        try:
            column_names = _get_column_names(conn, "materials")
            assert "updated_at" in column_names, (
                "materials.updated_at が0036適用後に存在しない"
            )
        finally:
            conn.close()

    def test_materials_has_no_updated_at_column_before_0036(self, db_before_0036):
        """0035適用時点では materials に updated_at列が存在しない（前提確認）"""
        conn = get_connection()
        try:
            assert "updated_at" not in _get_column_names(conn, "materials"), (
                "0036適用前のmaterialsにupdated_at列が既に存在している"
            )
        finally:
            conn.close()

    def test_other_columns_intact_after_0036(self, migrated_db):
        """0036適用後、materialsのid/title/content/source/created_atカラムが保持される"""
        conn = get_connection()
        try:
            column_names = _get_column_names(conn, "materials")
            for col in ["id", "title", "content", "source", "created_at"]:
                assert col in column_names, (
                    f"materials.{col} が0036適用後に消えている"
                )
        finally:
            conn.close()


class TestBackfill:
    """0036適用時の updated_at バックフィル確認"""

    def test_existing_rows_backfilled_with_created_at(self, db_before_0036):
        """0035時点のmaterials行は、0036適用後にupdated_atがcreated_atと同値になる"""
        # 0035時点でmaterialsにrowを1件INSERTする
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO materials (title, content, source, created_at) "
                "VALUES (?, ?, ?, ?)",
                ("既存資材", "内容", "test", "2024-01-01 00:00:00"),
            )
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0036(db_before_0036)

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT created_at, updated_at FROM materials WHERE title='既存資材'"
            ).fetchone()
            assert row is not None, "バックフィル対象のmaterial行が見つからない"
            assert row["updated_at"] == row["created_at"], (
                f"updated_at({row['updated_at']}) が created_at({row['created_at']}) と一致しない"
            )
            assert row["updated_at"] == "2024-01-01 00:00:00", (
                "updated_atがcreated_atの値でバックフィルされていない"
            )
        finally:
            conn.close()

    def test_insert_material_with_updated_at_after_0036(self, migrated_db):
        """0036適用後、materialsにupdated_atを指定してINSERTできる"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO materials (title, content, source, updated_at) "
                "VALUES (?, ?, ?, ?)",
                ("新規資材", "内容", "test", "2025-06-13 12:00:00"),
            )
            conn.commit()

            row = conn.execute(
                "SELECT updated_at FROM materials WHERE title='新規資材'"
            ).fetchone()
            assert row is not None
            assert row["updated_at"] == "2025-06-13 12:00:00"
        finally:
            conn.close()
