"""migration 0073_add_asks_notify_wanted のテスト

0073適用後にasks.notify_wantedカラムが期待通り存在し、既定1（通知希望あり）・
CHECK制約（0/1のみ）・既存データの遡及既定値であることを、ask_serviceを
経由せず生SQLで検証する。
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
from test_migrations.conftest import get_column_names


@pytest.fixture
def migrated_db():
    """全migration（0073含む）を適用済みのテスト用DBを提供する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def db_before_0073():
    """0072までのmigrationを適用したDBを提供する。0073の挙動を分離検証するために使う。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path

        parsed = parse_uri(f"sqlite:///{db_path}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        backend.init_database()
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        pre_0073 = MigrationList([m for m in all_migs if m.id < "0073"])
        with backend.lock():
            backend.apply_migrations(pre_0073)

        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


def _insert_ask(conn: sqlite3.Connection, **overrides) -> int:
    fields = {
        "question": "should we do X?",
        "fingerprint": "deadbeefdeadbeef",
    }
    fields.update(overrides)
    columns = ", ".join(fields.keys())
    placeholders = ", ".join("?" for _ in fields)
    cur = conn.execute(
        f"INSERT INTO asks ({columns}) VALUES ({placeholders})",
        tuple(fields.values()),
    )
    return cur.lastrowid


class TestNotifyWantedColumn:
    def test_notify_wanted_does_not_exist_before_0073(self, db_before_0073):
        conn = get_connection()
        try:
            columns = get_column_names(conn, "asks")
        finally:
            conn.close()
        assert "notify_wanted" not in columns

    def test_notify_wanted_exists_after_0073(self, migrated_db):
        conn = get_connection()
        try:
            columns = get_column_names(conn, "asks")
        finally:
            conn.close()
        assert "notify_wanted" in columns

    def test_notify_wanted_defaults_to_1(self, migrated_db):
        conn = get_connection()
        try:
            ask_id = _insert_ask(conn)
            conn.commit()
            row = conn.execute(
                "SELECT notify_wanted FROM asks WHERE id = ?", (ask_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row["notify_wanted"] == 1

    def test_notify_wanted_explicit_0_is_stored(self, migrated_db):
        conn = get_connection()
        try:
            ask_id = _insert_ask(conn, notify_wanted=0)
            conn.commit()
            row = conn.execute(
                "SELECT notify_wanted FROM asks WHERE id = ?", (ask_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row["notify_wanted"] == 0

    def test_notify_wanted_rejects_out_of_range_value(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_ask(conn, notify_wanted=2)
        finally:
            conn.rollback()
            conn.close()

    def test_existing_rows_backfilled_to_1(self, db_before_0073):
        """0072までのDBに事前投入したaskが、0073適用後にnotify_wanted=1で
        埋まっていることを確認する（既定値ALTER TABLEによる遡及適用）。"""
        conn = get_connection()
        try:
            ask_id = _insert_ask(conn, fingerprint="pre0073ask0000000")
            conn.commit()
        finally:
            conn.close()

        parsed = parse_uri(f"sqlite:///{os.environ['DISCUSSION_DB_PATH']}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        remaining = MigrationList([m for m in all_migs if m.id >= "0073"])
        with backend.lock():
            backend.apply_migrations(remaining)

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT notify_wanted FROM asks WHERE id = ?", (ask_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row["notify_wanted"] == 1
