"""migration 0078_drop_activities_orch_managed のテスト

0078適用後に activities テーブルから orch_managed 列が削除され、
既存行の他カラムの値（status/heartbeat/closed_*/タグ紐付け含む）が保持される
ことを確認する。SQLite 3.35+ で ALTER TABLE ... DROP COLUMN が使用可能なことを
前提とする。
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
from test_migrations.conftest import db_before_migration, get_column_names


@pytest.fixture
def migrated_db():
    """全migration（0078含む）を適用済みのテスト用DBを提供する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def db_before_0078():
    """0077までのmigrationを適用したDBを提供する。0078の挙動を分離検証するために使う。"""
    with db_before_migration("0078") as db_path:
        _injected_tags.clear()
        yield db_path


def _apply_migration_0078(db_path: str) -> None:
    """db_pathに対してmigration 0078のみを適用する。"""
    parsed = parse_uri(f"sqlite:///{db_path}")
    backend = _VecSQLiteBackend(parsed, default_migration_table)
    all_migs = read_migrations(str(MIGRATIONS_DIR))
    only_0078 = MigrationList([m for m in all_migs if m.id.startswith("0078")])
    with backend.lock():
        backend.apply_migrations(only_0078)


class TestOrchManagedColumnDropped:
    """0078適用後に orch_managed 列が削除されていることの確認"""

    def test_activities_has_no_orch_managed_column_after_0078(self, migrated_db):
        """migration 0078 適用後、activities テーブルに orch_managed 列が存在しない"""
        conn = get_connection()
        try:
            assert "orch_managed" not in get_column_names(conn, "activities"), (
                "activities.orch_managed が 0078 適用後も残っている"
            )
        finally:
            conn.close()

    def test_activities_has_orch_managed_column_before_0078(self, db_before_0078):
        """0077 適用時点では activities に orch_managed 列が存在する（前提確認）"""
        conn = get_connection()
        try:
            assert "orch_managed" in get_column_names(conn, "activities"), (
                "0078 適用前の activities に orch_managed 列が存在しない"
            )
        finally:
            conn.close()

    def test_orch_managed_column_removed_after_applying_0078(self, db_before_0078):
        """0077までのDBに0078を適用すると、orch_managed列が削除される"""
        _apply_migration_0078(db_before_0078)

        conn = get_connection()
        try:
            assert "orch_managed" not in get_column_names(conn, "activities"), (
                "0078適用後もactivities.orch_managedが残っている"
            )
        finally:
            conn.close()


class TestOtherColumnsUnaffected:
    """0078でDROPされるべきでないカラムへの影響がないことの確認"""

    def test_activities_other_columns_intact(self, migrated_db):
        conn = get_connection()
        try:
            column_names = get_column_names(conn, "activities")
            for col in [
                "id", "title", "description", "status",
                "created_at", "updated_at",
                "last_heartbeat_at", "last_heartbeat_session_id",
                "closed_at", "closed_by", "closed_reason",
            ]:
                assert col in column_names, f"activities.{col} が0078適用後に消えている"
        finally:
            conn.close()

    def test_insert_activity_without_orch_managed(self, migrated_db):
        """0078適用後、activitiesにorch_managed列なしでINSERTできる"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO activities (title, description, status) VALUES (?, ?, ?)",
                ("テストactivity", "説明", "pending"),
            )
            conn.commit()
            row = conn.execute(
                "SELECT title FROM activities WHERE title='テストactivity'"
            ).fetchone()
            assert row is not None
        finally:
            conn.close()


class TestDataIntegrity:
    """0078適用後のデータ保持確認"""

    def test_existing_row_full_state_preserved_after_applying_0078(self, db_before_0078):
        """0077までのDBに、orch_managed=1で他カラムも埋めた既存行を作った状態で
        0078を適用しても、orch_managed以外のカラム値とタグ紐付けは保持される"""
        conn = get_connection()
        try:
            cur = conn.execute(
                "INSERT INTO activities "
                "(title, description, status, orch_managed, last_heartbeat_at, "
                " last_heartbeat_session_id, closed_at, closed_by, closed_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "既存orch管理activity", "既存の説明", "completed", 1,
                    "2026-01-01 00:00:00", "sess-001",
                    "2026-01-02 00:00:00", "user", "手動完了",
                ),
            )
            activity_id = cur.lastrowid
            tag_cur = conn.execute(
                "INSERT INTO tags (namespace, name) VALUES (?, ?)",
                ("domain", "migration-0078-test"),
            )
            tag_id = tag_cur.lastrowid
            conn.execute(
                "INSERT INTO activity_tags (activity_id, tag_id) VALUES (?, ?)",
                (activity_id, tag_id),
            )
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0078(db_before_0078)

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT title, description, status, last_heartbeat_at, "
                "last_heartbeat_session_id, closed_at, closed_by, closed_reason "
                "FROM activities WHERE id = ?",
                (activity_id,),
            ).fetchone()
            assert row is not None
            assert row["title"] == "既存orch管理activity"
            assert row["description"] == "既存の説明"
            assert row["status"] == "completed"
            assert row["last_heartbeat_at"] == "2026-01-01 00:00:00"
            assert row["last_heartbeat_session_id"] == "sess-001"
            assert row["closed_at"] == "2026-01-02 00:00:00"
            assert row["closed_by"] == "user"
            assert row["closed_reason"] == "手動完了"

            tag_row = conn.execute(
                "SELECT t.namespace, t.name FROM activity_tags at "
                "JOIN tags t ON t.id = at.tag_id WHERE at.activity_id = ?",
                (activity_id,),
            ).fetchone()
            assert tag_row is not None
            assert tag_row["namespace"] == "domain"
            assert tag_row["name"] == "migration-0078-test"
        finally:
            conn.close()
