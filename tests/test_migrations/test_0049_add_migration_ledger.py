"""migration 0049_add_migration_ledger のテスト

0049 適用後に migration_ledger テーブルが作成され、
migration_id を PRIMARY KEY として content_sha256 / applied_at を保持することを確認する。
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
    """全 migration（0049 含む）を適用済みのテスト用 DB を提供する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def db_before_0049():
    """0048 までの migration を適用した DB を提供する。0049 の挙動を分離検証するために使う。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path

        parsed = parse_uri(f"sqlite:///{db_path}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        backend.init_database()
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        pre_0049 = MigrationList([m for m in all_migs if m.id < "0049"])
        with backend.lock():
            backend.apply_migrations(pre_0049)

        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


def _apply_migration_0049(db_path: str) -> None:
    """db_path に対して migration 0049 のみを適用する。"""
    parsed = parse_uri(f"sqlite:///{db_path}")
    backend = _VecSQLiteBackend(parsed, default_migration_table)
    all_migs = read_migrations(str(MIGRATIONS_DIR))
    only_0049 = MigrationList([m for m in all_migs if m.id.startswith("0049")])
    with backend.lock():
        backend.apply_migrations(only_0049)


class TestMigrationLedgerTableCreated:
    def test_table_exists_after_0049(self, migrated_db):
        conn = get_connection()
        try:
            assert table_exists(conn, "migration_ledger"), (
                "migration_ledger テーブルが 0049 適用後に存在しない"
            )
        finally:
            conn.close()

    def test_table_not_exists_before_0049(self, db_before_0049):
        conn = get_connection()
        try:
            assert not table_exists(conn, "migration_ledger"), (
                "0049 適用前に migration_ledger テーブルが既に存在している"
            )
        finally:
            conn.close()

    def test_required_columns_exist(self, migrated_db):
        conn = get_connection()
        try:
            column_names = get_column_names(conn, "migration_ledger")
            for col in {"migration_id", "content_sha256", "applied_at"}:
                assert col in column_names, f"migration_ledger.{col} が 0049 適用後に存在しない"
        finally:
            conn.close()


class TestMigrationLedgerCRUD:
    """init_database()（migrated_db）はfresh DBパスで全migrationの内容ハッシュを自動記録するため、
    実在migration_idとの衝突を避けるダミーIDを使って直接のCRUD挙動を検証する。
    """

    def test_insert_and_select(self, migrated_db):
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO migration_ledger (migration_id, content_sha256) VALUES (?, ?)",
                ("9999_test_fixture_insert", "a" * 64),
            )
            conn.commit()
            row = conn.execute(
                "SELECT migration_id, content_sha256, applied_at FROM migration_ledger "
                "WHERE migration_id = ?",
                ("9999_test_fixture_insert",),
            ).fetchone()
            assert row is not None
            assert row["content_sha256"] == "a" * 64
            assert row["applied_at"] is not None, "applied_at はDEFAULT CURRENT_TIMESTAMPで自動設定されるべき"
        finally:
            conn.close()

    def test_primary_key_duplicate_rejected(self, migrated_db):
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO migration_ledger (migration_id, content_sha256) VALUES (?, ?)",
                ("9999_test_fixture_dup", "a" * 64),
            )
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO migration_ledger (migration_id, content_sha256) VALUES (?, ?)",
                    ("9999_test_fixture_dup", "b" * 64),
                )
                conn.commit()
        finally:
            conn.close()

    def test_content_sha256_not_null_enforced(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO migration_ledger (migration_id, content_sha256) VALUES (?, ?)",
                    ("9999_test_fixture_null", None),
                )
                conn.commit()
        finally:
            conn.close()

    def test_upsert_updates_hash(self, migrated_db):
        """ON CONFLICT DO UPDATE（_record_content_hashesが使う形）で内容ハッシュを更新できる"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO migration_ledger (migration_id, content_sha256) VALUES (?, ?)",
                ("9999_test_fixture_upsert", "a" * 64),
            )
            conn.commit()
            conn.execute(
                """
                INSERT INTO migration_ledger (migration_id, content_sha256, applied_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(migration_id) DO UPDATE SET
                    content_sha256 = excluded.content_sha256,
                    applied_at = excluded.applied_at
                """,
                ("9999_test_fixture_upsert", "b" * 64),
            )
            conn.commit()
            row = conn.execute(
                "SELECT content_sha256 FROM migration_ledger WHERE migration_id = ?",
                ("9999_test_fixture_upsert",),
            ).fetchone()
            assert row["content_sha256"] == "b" * 64
        finally:
            conn.close()

    def test_fresh_database_backfills_all_applied_migrations(self, migrated_db):
        """fresh DBパス（init_database）は適用した全migrationの内容ハッシュを自動記録する"""
        conn = get_connection()
        try:
            count = conn.execute("SELECT COUNT(*) FROM migration_ledger").fetchone()[0]
        finally:
            conn.close()
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        assert count == len(all_migs), (
            "fresh DBでは適用した全migrationの内容ハッシュがmigration_ledgerに記録されるべき"
        )


class TestExistingRowsUnaffected:
    def test_0049_does_not_touch_other_tables(self, db_before_0049):
        """0049 は追加専用のmigrationであり、既存テーブルの行を変更しない"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO discussion_topics (title, description) VALUES (?, ?)",
                ("既存トピック", "既存の説明"),
            )
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0049(db_before_0049)

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT title, description FROM discussion_topics WHERE title = ?",
                ("既存トピック",),
            ).fetchone()
            assert row is not None
            assert row["description"] == "既存の説明"
        finally:
            conn.close()
