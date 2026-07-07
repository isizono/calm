"""migration 0057_drop_capability_gating のテスト

0057適用後に session_identity テーブルが存在せず、decisions / discussion_logs /
discussion_topics / activities / materials の caller_session_id 列も存在しないことを
確認する。SQLite 3.35+ で ALTER TABLE ... DROP COLUMN が使用可能なことを前提とする。
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

TABLES = [
    "decisions",
    "discussion_logs",
    "discussion_topics",
    "activities",
    "materials",
]


@pytest.fixture
def migrated_db():
    """全migration（0057含む）を適用済みのテスト用DBを提供する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def db_before_0057():
    """0056までのmigrationを適用したDBを提供する。0057の挙動を分離検証するために使う。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path

        parsed = parse_uri(f"sqlite:///{db_path}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        backend.init_database()
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        pre_0057 = MigrationList([m for m in all_migs if m.id < "0057"])
        with backend.lock():
            backend.apply_migrations(pre_0057)

        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


def _apply_migration_0057(db_path: str) -> None:
    """db_pathに対してmigration 0057のみを適用する。"""
    parsed = parse_uri(f"sqlite:///{db_path}")
    backend = _VecSQLiteBackend(parsed, default_migration_table)
    all_migs = read_migrations(str(MIGRATIONS_DIR))
    only_0057 = MigrationList([m for m in all_migs if m.id.startswith("0057")])
    with backend.lock():
        backend.apply_migrations(only_0057)


def _get_column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    """指定テーブルのカラム名セットを返す。"""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


class TestSessionIdentityTableDropped:
    """0057適用後にsession_identityテーブルが削除されていることの確認"""

    def test_session_identity_table_not_exists_after_0057(self, migrated_db):
        """migration 0057適用後、session_identityテーブルが存在しない"""
        conn = get_connection()
        try:
            assert not _table_exists(conn, "session_identity"), (
                "session_identity テーブルが0057適用後も残っている"
            )
        finally:
            conn.close()

    def test_session_identity_table_present_before_0057(self, db_before_0057):
        """0056適用時点ではsession_identityテーブルが存在する（前提確認）"""
        conn = get_connection()
        try:
            assert _table_exists(conn, "session_identity"), (
                "0057適用前のDBにsession_identityテーブルがない"
            )
        finally:
            conn.close()

    def test_session_identity_table_removed_after_applying_0057(self, db_before_0057):
        """0056までのDBに0057を適用すると、session_identityテーブルが削除される"""
        _apply_migration_0057(db_before_0057)

        conn = get_connection()
        try:
            assert not _table_exists(conn, "session_identity"), (
                "0057適用後もsession_identityテーブルが残っている"
            )
        finally:
            conn.close()


class TestCallerSessionIdColumnsDropped:
    """0057適用後にcaller_session_id列が5テーブルすべてから削除されていることの確認"""

    @pytest.mark.parametrize("table", TABLES)
    def test_caller_session_id_column_absent_after_0057(self, migrated_db, table):
        """migration 0057適用後、対象テーブルにcaller_session_id列が存在しない"""
        conn = get_connection()
        try:
            column_names = _get_column_names(conn, table)
            assert "caller_session_id" not in column_names, (
                f"{table}.caller_session_id が0057適用後も残っている"
            )
        finally:
            conn.close()

    @pytest.mark.parametrize("table", TABLES)
    def test_caller_session_id_column_present_before_0057(self, db_before_0057, table):
        """0056適用時点では対象テーブルにcaller_session_id列が存在する（前提確認）"""
        conn = get_connection()
        try:
            assert "caller_session_id" in _get_column_names(conn, table), (
                f"0057適用前の{table}にcaller_session_id列がない"
            )
        finally:
            conn.close()

    def test_caller_session_id_columns_removed_after_applying_0057(self, db_before_0057):
        """0056までのDBに0057を適用すると、5テーブルのcaller_session_id列がすべて削除される"""
        _apply_migration_0057(db_before_0057)

        conn = get_connection()
        try:
            for table in TABLES:
                assert "caller_session_id" not in _get_column_names(conn, table), (
                    f"0057適用後も{table}.caller_session_id が残っている"
                )
        finally:
            conn.close()


class TestOtherColumnsUnaffected:
    """0057でDROPされるべきでないカラムへの影響がないことの確認"""

    def test_decisions_other_columns_intact(self, migrated_db):
        conn = get_connection()
        try:
            column_names = _get_column_names(conn, "decisions")
            for col in ["id", "decision", "reason", "title", "created_at", "retracted_at"]:
                assert col in column_names, f"decisions.{col} が0057適用後に消えている"
        finally:
            conn.close()

    def test_discussion_logs_other_columns_intact(self, migrated_db):
        conn = get_connection()
        try:
            column_names = _get_column_names(conn, "discussion_logs")
            for col in ["id", "title", "content", "created_at", "retracted_at"]:
                assert col in column_names, f"discussion_logs.{col} が0057適用後に消えている"
        finally:
            conn.close()

    def test_discussion_topics_other_columns_intact(self, migrated_db):
        conn = get_connection()
        try:
            column_names = _get_column_names(conn, "discussion_topics")
            for col in ["id", "title", "description", "created_at"]:
                assert col in column_names, f"discussion_topics.{col} が0057適用後に消えている"
        finally:
            conn.close()

    def test_activities_other_columns_intact(self, migrated_db):
        conn = get_connection()
        try:
            column_names = _get_column_names(conn, "activities")
            for col in ["id", "title", "description", "status", "orch_managed"]:
                assert col in column_names, f"activities.{col} が0057適用後に消えている"
        finally:
            conn.close()

    def test_materials_other_columns_intact(self, migrated_db):
        conn = get_connection()
        try:
            column_names = _get_column_names(conn, "materials")
            for col in ["id", "title", "content", "source", "updated_at", "retracted_at"]:
                assert col in column_names, f"materials.{col} が0057適用後に消えている"
        finally:
            conn.close()


class TestDataIntegrity:
    """0057適用後のデータ操作確認"""

    def test_insert_topic_without_caller_session_id(self, migrated_db):
        """0057適用後、discussion_topicsにcaller_session_id列なしでINSERTできる"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO discussion_topics (title, description) VALUES (?, ?)",
                ("テストトピック", "説明"),
            )
            conn.commit()
            row = conn.execute(
                "SELECT title FROM discussion_topics WHERE title='テストトピック'"
            ).fetchone()
            assert row is not None
        finally:
            conn.close()

    def test_insert_decision_without_caller_session_id(self, migrated_db):
        """0057適用後、decisionsにcaller_session_id列なしでINSERTできる"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO decisions (decision, reason) VALUES (?, ?)",
                ("テスト決定", "テスト根拠"),
            )
            conn.commit()
            row = conn.execute(
                "SELECT decision FROM decisions WHERE decision='テスト決定'"
            ).fetchone()
            assert row is not None
        finally:
            conn.close()

    def test_insert_activity_without_caller_session_id(self, migrated_db):
        """0057適用後、activitiesにcaller_session_id列なしでINSERTできる"""
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

    def test_insert_material_without_caller_session_id(self, migrated_db):
        """0057適用後、materialsにcaller_session_id列なしでINSERTできる"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO materials (title, content) VALUES (?, ?)",
                ("テスト資材", "内容"),
            )
            conn.commit()
            row = conn.execute(
                "SELECT title FROM materials WHERE title='テスト資材'"
            ).fetchone()
            assert row is not None
        finally:
            conn.close()

    def test_existing_rows_preserved_after_applying_0057(self, db_before_0057):
        """0056までのDBに既存行を作った状態で0057を適用しても、他カラムの値は保持される"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO discussion_topics (title, description) VALUES (?, ?)",
                ("既存トピック", "既存の説明"),
            )
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0057(db_before_0057)

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT title, description FROM discussion_topics WHERE title=?",
                ("既存トピック",),
            ).fetchone()
            assert row is not None
            assert row["title"] == "既存トピック"
            assert row["description"] == "既存の説明"
        finally:
            conn.close()
