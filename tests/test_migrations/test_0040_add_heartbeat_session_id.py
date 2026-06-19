"""migration 0040_add_heartbeat_session_id のテスト

0040適用後に activities テーブルへ last_heartbeat_session_id 列が追加され、
既存行のデータ・他カラムが保持されることを確認する。
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
    """全migration（0040含む）を適用済みのテスト用DBを提供する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def db_before_0040():
    """0040より前のmigrationを適用したDBを提供する。0040の挙動を分離検証するために使う。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path

        parsed = parse_uri(f"sqlite:///{db_path}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        backend.init_database()
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        pre_0040 = MigrationList([m for m in all_migs if m.id < "0040"])
        with backend.lock():
            backend.apply_migrations(pre_0040)

        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


def _apply_migration_0040(db_path: str) -> None:
    """db_pathに対してmigration 0040のみを適用する。"""
    parsed = parse_uri(f"sqlite:///{db_path}")
    backend = _VecSQLiteBackend(parsed, default_migration_table)
    all_migs = read_migrations(str(MIGRATIONS_DIR))
    only_0040 = MigrationList([m for m in all_migs if m.id.startswith("0040")])
    with backend.lock():
        backend.apply_migrations(only_0040)


def _get_column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    """指定テーブルのカラム名セットを返す。"""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


class TestSessionIdColumnAdded:
    """0040適用後に last_heartbeat_session_id 列が追加されていることの確認"""

    def test_activities_has_session_id_column_after_0040(self, migrated_db):
        """migration 0040適用後、activities に last_heartbeat_session_id 列が存在する"""
        conn = get_connection()
        try:
            column_names = _get_column_names(conn, "activities")
            assert "last_heartbeat_session_id" in column_names, (
                "activities.last_heartbeat_session_id が0040適用後に存在しない"
            )
        finally:
            conn.close()

    def test_activities_has_no_session_id_column_before_0040(self, db_before_0040):
        """0040適用前は activities に last_heartbeat_session_id 列が存在しない（前提確認）"""
        conn = get_connection()
        try:
            assert "last_heartbeat_session_id" not in _get_column_names(
                conn, "activities"
            ), "0040適用前に既に last_heartbeat_session_id 列が存在している"
        finally:
            conn.close()

    def test_other_columns_intact_after_0040(self, migrated_db):
        """0040適用後、activities の主要カラム（id/title/description/status/last_heartbeat_at）が保持される"""
        conn = get_connection()
        try:
            column_names = _get_column_names(conn, "activities")
            for col in [
                "id",
                "title",
                "description",
                "status",
                "created_at",
                "updated_at",
                "last_heartbeat_at",
            ]:
                assert col in column_names, (
                    f"activities.{col} が0040適用後に消えている"
                )
        finally:
            conn.close()


class TestExistingRowsPreserved:
    """0040適用時に既存行が破壊されないことの確認"""

    def test_existing_rows_preserved_with_null_session_id(self, db_before_0040):
        """0040適用前の activities 行は、適用後に last_heartbeat_session_id が NULL のまま既存データを保持する"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO activities (title, description, status, last_heartbeat_at) "
                "VALUES (?, ?, ?, ?)",
                ("既存タスク", "desc", "in_progress", "2026-01-01 00:00:00"),
            )
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0040(db_before_0040)

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT title, status, last_heartbeat_at, last_heartbeat_session_id "
                "FROM activities WHERE title=?",
                ("既存タスク",),
            ).fetchone()
            assert row is not None
            assert row["status"] == "in_progress"
            assert row["last_heartbeat_at"] == "2026-01-01 00:00:00"
            assert row["last_heartbeat_session_id"] is None, (
                "既存行の last_heartbeat_session_id は NULL であるべき"
            )
        finally:
            conn.close()

    def test_insert_with_session_id_after_0040(self, migrated_db):
        """0040適用後、activities に last_heartbeat_session_id を指定してINSERT/UPDATEできる"""
        conn = get_connection()
        try:
            cursor = conn.execute(
                "INSERT INTO activities (title, description, status) VALUES (?, ?, ?)",
                ("新規タスク", "desc", "pending"),
            )
            activity_id = cursor.lastrowid
            conn.execute(
                "UPDATE activities "
                "SET last_heartbeat_at = ?, last_heartbeat_session_id = ? "
                "WHERE id = ?",
                ("2026-06-19 00:00:00", "sess-xyz", activity_id),
            )
            conn.commit()

            row = conn.execute(
                "SELECT last_heartbeat_session_id FROM activities WHERE id=?",
                (activity_id,),
            ).fetchone()
            assert row is not None
            assert row["last_heartbeat_session_id"] == "sess-xyz"
        finally:
            conn.close()
