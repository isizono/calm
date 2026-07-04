"""migration 0049_add_signal_events のテスト

0049 適用後に signal_events テーブルとその索引・CHECK 制約が期待通り
存在することを、Python 層の signal_service を経由せず生 SQL で検証する。
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
from test_migrations.conftest import get_column_names, index_names, table_exists


@pytest.fixture
def migrated_db():
    """全migration（0049含む）を適用済みのテスト用DBを提供する。"""
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
    """0048までのmigrationを適用したDBを提供する。0049の挙動を分離検証するために使う。"""
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


def _insert_signal(conn: sqlite3.Connection, **overrides) -> int:
    fields = {
        "kind": "machine_error",
        "source": "tool:foo",
        "summary": "boom",
        "fingerprint": "deadbeefdeadbeef",
    }
    fields.update(overrides)
    columns = ", ".join(fields.keys())
    placeholders = ", ".join("?" for _ in fields)
    cur = conn.execute(
        f"INSERT INTO signal_events ({columns}) VALUES ({placeholders})",
        tuple(fields.values()),
    )
    return cur.lastrowid


class TestTableCreated:
    def test_signal_events_does_not_exist_before_0049(self, db_before_0049):
        conn = get_connection()
        try:
            assert not table_exists(conn, "signal_events")
        finally:
            conn.close()

    def test_signal_events_exists_after_0049(self, migrated_db):
        conn = get_connection()
        try:
            assert table_exists(conn, "signal_events")
        finally:
            conn.close()

    def test_expected_columns(self, migrated_db):
        conn = get_connection()
        try:
            columns = get_column_names(conn, "signal_events")
        finally:
            conn.close()
        expected = {
            "id", "kind", "source", "summary", "detail", "refs", "context",
            "fingerprint", "occurrence_count", "first_seen_at", "last_seen_at",
            "session_id", "status", "promoted_type", "promoted_id",
        }
        assert expected <= columns

    def test_expected_indexes(self, migrated_db):
        conn = get_connection()
        try:
            names = index_names(conn, "idx_signal_%")
        finally:
            conn.close()
        assert {
            "idx_signal_fingerprint_new",
            "idx_signal_status",
            "idx_signal_kind",
        } <= names


class TestDefaults:
    def test_status_defaults_to_new(self, migrated_db):
        conn = get_connection()
        try:
            signal_id = _insert_signal(conn)
            conn.commit()
            row = conn.execute(
                "SELECT status, occurrence_count FROM signal_events WHERE id = ?",
                (signal_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row["status"] == "new"
        assert row["occurrence_count"] == 1


class TestCheckConstraints:
    def test_invalid_status_rejected(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_signal(conn, status="not_a_status")
        finally:
            conn.rollback()
            conn.close()

    def test_promoted_type_without_id_rejected(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_signal(conn, promoted_type="topic")
        finally:
            conn.rollback()
            conn.close()

    def test_promoted_id_without_type_rejected(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_signal(conn, promoted_id=1)
        finally:
            conn.rollback()
            conn.close()

    def test_promoted_type_and_id_together_accepted(self, migrated_db):
        conn = get_connection()
        try:
            signal_id = _insert_signal(conn, promoted_type="topic", promoted_id=1)
            conn.commit()
            row = conn.execute(
                "SELECT promoted_type, promoted_id FROM signal_events WHERE id = ?",
                (signal_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row["promoted_type"] == "topic"
        assert row["promoted_id"] == 1


class TestPartialUniqueIndex:
    def test_duplicate_fingerprint_rejected_while_status_new(self, migrated_db):
        conn = get_connection()
        try:
            _insert_signal(conn, fingerprint="dup000000000000")
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                _insert_signal(conn, fingerprint="dup000000000000")
        finally:
            conn.rollback()
            conn.close()

    def test_duplicate_fingerprint_allowed_once_not_new(self, migrated_db):
        conn = get_connection()
        try:
            first_id = _insert_signal(conn, fingerprint="dup111111111111")
            conn.execute(
                "UPDATE signal_events SET status = 'dismissed' WHERE id = ?",
                (first_id,),
            )
            second_id = _insert_signal(conn, fingerprint="dup111111111111")
            conn.commit()
        finally:
            conn.close()
        assert first_id != second_id
