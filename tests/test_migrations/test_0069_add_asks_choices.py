"""migration 0069_add_asks_choices のテスト

0069 適用後に asks.choices カラムが期待通り存在し、nullable・既存データ非破壊
であることを、ask_service を経由せず生SQLで検証する。
"""
import json
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
    """全migration（0069含む）を適用済みのテスト用DBを提供する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def db_before_0069():
    """0068までのmigrationを適用したDBを提供する。0069の挙動を分離検証するために使う。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path

        parsed = parse_uri(f"sqlite:///{db_path}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        backend.init_database()
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        pre_0069 = MigrationList([m for m in all_migs if m.id < "0069"])
        with backend.lock():
            backend.apply_migrations(pre_0069)

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


class TestChoicesColumn:
    def test_choices_does_not_exist_before_0069(self, db_before_0069):
        conn = get_connection()
        try:
            columns = get_column_names(conn, "asks")
        finally:
            conn.close()
        assert "choices" not in columns

    def test_choices_exists_after_0069(self, migrated_db):
        conn = get_connection()
        try:
            columns = get_column_names(conn, "asks")
        finally:
            conn.close()
        assert "choices" in columns

    def test_choices_defaults_to_null(self, migrated_db):
        conn = get_connection()
        try:
            ask_id = _insert_ask(conn)
            conn.commit()
            row = conn.execute("SELECT choices FROM asks WHERE id = ?", (ask_id,)).fetchone()
        finally:
            conn.close()
        assert row["choices"] is None

    def test_choices_stores_json_array(self, migrated_db):
        conn = get_connection()
        try:
            payload = json.dumps(["A案", "B案", "C案"], ensure_ascii=False)
            ask_id = _insert_ask(conn, fingerprint="choices0000000001", choices=payload)
            conn.commit()
            row = conn.execute("SELECT choices FROM asks WHERE id = ?", (ask_id,)).fetchone()
        finally:
            conn.close()
        assert json.loads(row["choices"]) == ["A案", "B案", "C案"]

    def test_existing_rows_backfilled_to_null(self, db_before_0069):
        """0068までのDBに事前投入したaskが、0069適用後もchoices=NULLのままであることを確認する
        （既存データへの遡及適用は行わない設計）。"""
        conn = get_connection()
        try:
            ask_id = _insert_ask(conn, fingerprint="pre0069ask0000000")
            conn.commit()
        finally:
            conn.close()

        parsed = parse_uri(f"sqlite:///{os.environ['DISCUSSION_DB_PATH']}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        remaining = MigrationList([m for m in all_migs if m.id >= "0069"])
        with backend.lock():
            backend.apply_migrations(remaining)

        conn = get_connection()
        try:
            row = conn.execute("SELECT choices FROM asks WHERE id = ?", (ask_id,)).fetchone()
        finally:
            conn.close()
        assert row["choices"] is None
