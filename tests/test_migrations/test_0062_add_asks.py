"""migration 0062_add_asks のテスト

0062 適用後に asks / ask_blocks / ask_requesters / ask_vec が期待通り存在し、
CHECK制約・部分UNIQUEインデックス・外部キーが機能することを、
ask_service を経由せず生SQLで検証する。
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
    """全migration（0062含む）を適用済みのテスト用DBを提供する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def db_before_0062():
    """0061までのmigrationを適用したDBを提供する。0062の挙動を分離検証するために使う。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path

        parsed = parse_uri(f"sqlite:///{db_path}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        backend.init_database()
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        pre_0062 = MigrationList([m for m in all_migs if m.id < "0062"])
        with backend.lock():
            backend.apply_migrations(pre_0062)

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


def _insert_activity(conn: sqlite3.Connection, title: str = "a1") -> int:
    cur = conn.execute(
        "INSERT INTO activities (title, description) VALUES (?, ?)",
        (title, "desc"),
    )
    return cur.lastrowid


def _insert_decision(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "INSERT INTO decisions (decision, reason) VALUES (?, ?)",
        ("d", "r"),
    )
    return cur.lastrowid


class TestTablesCreated:
    def test_asks_does_not_exist_before_0062(self, db_before_0062):
        conn = get_connection()
        try:
            assert not table_exists(conn, "asks")
            assert not table_exists(conn, "ask_blocks")
            assert not table_exists(conn, "ask_requesters")
        finally:
            conn.close()

    def test_asks_and_junctions_exist_after_0062(self, migrated_db):
        conn = get_connection()
        try:
            assert table_exists(conn, "asks")
            assert table_exists(conn, "ask_blocks")
            assert table_exists(conn, "ask_requesters")
            assert table_exists(conn, "ask_vec")
        finally:
            conn.close()

    def test_expected_columns(self, migrated_db):
        conn = get_connection()
        try:
            columns = get_column_names(conn, "asks")
        finally:
            conn.close()
        expected = {
            "id", "question", "context", "fingerprint", "status",
            "answer_body", "answered_at", "answered_session_id",
            "triage", "triaged_at", "triaged_session_id", "triage_reason",
            "promoted_decision_id",
            "withdrawn_at", "withdrawn_session_id", "withdraw_reason",
            "occurrence_count", "first_seen_at", "last_seen_at",
            "first_seen_session_id", "last_seen_session_id",
        }
        assert expected <= columns

    def test_expected_indexes(self, migrated_db):
        conn = get_connection()
        try:
            names = index_names(conn, "idx_ask%")
        finally:
            conn.close()
        assert {
            "idx_asks_fingerprint_open",
            "idx_asks_status_last_seen",
            "idx_asks_triage_pending",
            "idx_ask_blocks_activity",
        } <= names


class TestDefaults:
    def test_status_defaults_to_open(self, migrated_db):
        conn = get_connection()
        try:
            ask_id = _insert_ask(conn)
            conn.commit()
            row = conn.execute(
                "SELECT status, occurrence_count FROM asks WHERE id = ?", (ask_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row["status"] == "open"
        assert row["occurrence_count"] == 1


class TestCheckConstraints:
    def test_invalid_status_rejected(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_ask(conn, status="not_a_status")
        finally:
            conn.rollback()
            conn.close()

    def test_answered_without_answer_body_rejected(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_ask(conn, status="answered")
        finally:
            conn.rollback()
            conn.close()

    def test_answered_with_body_and_timestamp_accepted(self, migrated_db):
        conn = get_connection()
        try:
            ask_id = _insert_ask(
                conn,
                status="answered",
                answer_body="yes",
                answered_at="2026-01-01 00:00:00",
            )
            conn.commit()
            row = conn.execute("SELECT status FROM asks WHERE id = ?", (ask_id,)).fetchone()
        finally:
            conn.close()
        assert row["status"] == "answered"

    def test_triage_without_answered_at_rejected(self, migrated_db):
        """statusは'open'のままtriageだけ設定するケース（statusのCHECKは満たすが、
        triage設定にはanswered_atが必須というCHECKには違反する)。"""
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_ask(conn, triage="promote")
        finally:
            conn.rollback()
            conn.close()

    def test_promoted_status_without_decision_id_rejected(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_ask(
                    conn,
                    status="promoted",
                    answer_body="yes",
                    answered_at="2026-01-01 00:00:00",
                    triage="promote",
                    triaged_at="2026-01-01 00:00:00",
                )
        finally:
            conn.rollback()
            conn.close()

    def test_promoted_decision_id_while_open_rejected(self, migrated_db):
        conn = get_connection()
        try:
            decision_id = _insert_decision(conn)
            with pytest.raises(sqlite3.IntegrityError):
                _insert_ask(conn, promoted_decision_id=decision_id)
        finally:
            conn.rollback()
            conn.close()

    def test_promoted_decision_id_references_nonexistent_decision_rejected(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_ask(
                    conn,
                    status="promoted",
                    answer_body="yes",
                    answered_at="2026-01-01 00:00:00",
                    triage="promote",
                    triaged_at="2026-01-01 00:00:00",
                    promoted_decision_id=999999,
                )
        finally:
            conn.rollback()
            conn.close()

    def test_withdrawn_without_timestamp_rejected(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_ask(conn, status="withdrawn")
        finally:
            conn.rollback()
            conn.close()

    def test_withdrawn_timestamp_while_open_rejected(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_ask(conn, withdrawn_at="2026-01-01 00:00:00")
        finally:
            conn.rollback()
            conn.close()


class TestPartialUniqueIndex:
    def test_duplicate_fingerprint_rejected_while_status_open(self, migrated_db):
        conn = get_connection()
        try:
            _insert_ask(conn, fingerprint="dup000000000000")
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                _insert_ask(conn, fingerprint="dup000000000000")
        finally:
            conn.rollback()
            conn.close()

    def test_duplicate_fingerprint_allowed_once_not_open(self, migrated_db):
        conn = get_connection()
        try:
            first_id = _insert_ask(conn, fingerprint="dup111111111111")
            conn.execute(
                """
                UPDATE asks SET status = 'withdrawn', withdrawn_at = '2026-01-01 00:00:00'
                WHERE id = ?
                """,
                (first_id,),
            )
            second_id = _insert_ask(conn, fingerprint="dup111111111111")
            conn.commit()
        finally:
            conn.close()
        assert first_id != second_id


class TestForeignKeys:
    def test_ask_blocks_cascade_deletes_on_activity_delete(self, migrated_db):
        conn = get_connection()
        try:
            ask_id = _insert_ask(conn)
            activity_id = _insert_activity(conn)
            conn.execute(
                "INSERT INTO ask_blocks (ask_id, activity_id) VALUES (?, ?)",
                (ask_id, activity_id),
            )
            conn.commit()

            conn.execute("DELETE FROM activities WHERE id = ?", (activity_id,))
            conn.commit()

            remaining = conn.execute(
                "SELECT COUNT(*) FROM ask_blocks WHERE ask_id = ?", (ask_id,)
            ).fetchone()[0]
        finally:
            conn.close()
        assert remaining == 0

    def test_ask_blocks_references_nonexistent_ask_rejected(self, migrated_db):
        conn = get_connection()
        try:
            activity_id = _insert_activity(conn)
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO ask_blocks (ask_id, activity_id) VALUES (?, ?)",
                    (999999, activity_id),
                )
        finally:
            conn.rollback()
            conn.close()
