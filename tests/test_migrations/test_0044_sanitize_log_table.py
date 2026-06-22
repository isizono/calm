"""migration 0044_sanitize_log_table のテスト

0044 適用後に sanitize_log テーブルが作成され、スキーマ定義 (カラム / CHECK 制約 /
DEFAULT / インデックス) が期待通りに振る舞うことを確認する。
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
    """全 migration（0044 含む）を適用済みのテスト用 DB を提供する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def db_before_0044():
    """0044 より前の migration を適用した DB を提供する。0044 の挙動を分離検証するために使う。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path

        parsed = parse_uri(f"sqlite:///{db_path}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        backend.init_database()
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        pre_0044 = MigrationList([m for m in all_migs if m.id < "0044"])
        with backend.lock():
            backend.apply_migrations(pre_0044)

        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


def _apply_migration_0044(db_path: str) -> None:
    """db_path に対して migration 0044 のみを適用する。"""
    parsed = parse_uri(f"sqlite:///{db_path}")
    backend = _VecSQLiteBackend(parsed, default_migration_table)
    all_migs = read_migrations(str(MIGRATIONS_DIR))
    only_0044 = MigrationList([m for m in all_migs if m.id.startswith("0044")])
    with backend.lock():
        backend.apply_migrations(only_0044)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _get_column_info(conn: sqlite3.Connection, table: str) -> dict[str, dict]:
    """指定テーブルのカラム情報 (name -> {type, notnull, dflt_value, pk}) を返す。"""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {
        row["name"]: {
            "type": row["type"],
            "notnull": row["notnull"],
            "dflt_value": row["dflt_value"],
            "pk": row["pk"],
        }
        for row in rows
    }


def _get_index_names(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
    return {row["name"] for row in rows}


class TestSanitizeLogTableCreated:
    """0044 適用後に sanitize_log テーブルが期待スキーマで作成されていることの確認"""

    def test_table_exists_after_0044(self, migrated_db):
        """migration 0044 適用後、sanitize_log テーブルが存在する"""
        conn = get_connection()
        try:
            assert _table_exists(conn, "sanitize_log"), (
                "sanitize_log テーブルが 0044 適用後に存在しない"
            )
        finally:
            conn.close()

    def test_table_does_not_exist_before_0044(self, db_before_0044):
        """0044 適用前は sanitize_log テーブルが存在しない（前提確認）"""
        conn = get_connection()
        try:
            assert not _table_exists(conn, "sanitize_log"), (
                "0044 適用前に既に sanitize_log テーブルが存在している"
            )
        finally:
            conn.close()

    def test_all_columns_present_with_expected_types(self, migrated_db):
        """sanitize_log の全カラムが期待型・NOT NULL・DEFAULT で存在する"""
        conn = get_connection()
        try:
            cols = _get_column_info(conn, "sanitize_log")

            assert cols["id"]["pk"] == 1
            assert cols["id"]["type"] == "INTEGER"

            assert "session_id" in cols
            assert cols["session_id"]["type"] == "TEXT"
            assert cols["session_id"]["notnull"] == 0

            assert "transcript_path" in cols
            assert cols["transcript_path"]["type"] == "TEXT"
            assert cols["transcript_path"]["notnull"] == 0

            assert cols["hook_kind"]["type"] == "TEXT"
            assert cols["hook_kind"]["notnull"] == 1

            for counter in ("occurrence_count", "sanitized_count", "failed_count"):
                assert cols[counter]["type"] == "INTEGER"
                assert cols[counter]["notnull"] == 1
                assert cols[counter]["dflt_value"] == "0", (
                    f"{counter} の DEFAULT が 0 ではない: {cols[counter]['dflt_value']}"
                )

            assert cols["failure_reason"]["type"] == "TEXT"
            assert cols["failure_reason"]["notnull"] == 0

            assert cols["recorded_at"]["type"] == "TEXT"
            assert cols["recorded_at"]["notnull"] == 1
            assert cols["recorded_at"]["dflt_value"] is not None
            assert "datetime" in cols["recorded_at"]["dflt_value"]
        finally:
            conn.close()

    def test_indexes_created(self, migrated_db):
        """session_id / recorded_at インデックスが作成されている"""
        conn = get_connection()
        try:
            indexes = _get_index_names(conn, "sanitize_log")
            assert "idx_sanitize_log_session" in indexes
            assert "idx_sanitize_log_recorded_at" in indexes
        finally:
            conn.close()


class TestSanitizeLogConstraints:
    """CHECK 制約と DEFAULT 値の動作確認"""

    def test_insert_with_post_tool_use_hook_kind(self, migrated_db):
        """hook_kind='post_tool_use' で INSERT 成功"""
        conn = get_connection()
        try:
            cursor = conn.execute(
                "INSERT INTO sanitize_log (session_id, hook_kind, occurrence_count, "
                "sanitized_count, failed_count) VALUES (?, ?, ?, ?, ?)",
                ("sess-001", "post_tool_use", 3, 3, 0),
            )
            conn.commit()
            row_id = cursor.lastrowid

            row = conn.execute(
                "SELECT session_id, hook_kind, occurrence_count, sanitized_count, "
                "failed_count, failure_reason, recorded_at FROM sanitize_log WHERE id=?",
                (row_id,),
            ).fetchone()
            assert row["session_id"] == "sess-001"
            assert row["hook_kind"] == "post_tool_use"
            assert row["occurrence_count"] == 3
            assert row["sanitized_count"] == 3
            assert row["failed_count"] == 0
            assert row["failure_reason"] is None
            assert row["recorded_at"] is not None
        finally:
            conn.close()

    def test_insert_with_session_start_backfill_hook_kind(self, migrated_db):
        """hook_kind='session_start_backfill' で INSERT 成功"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO sanitize_log (transcript_path, hook_kind, occurrence_count, "
                "sanitized_count, failed_count, failure_reason) VALUES (?, ?, ?, ?, ?, ?)",
                ("/tmp/transcript.jsonl", "session_start_backfill", 5, 4, 1, "regex compile failed"),
            )
            conn.commit()

            row = conn.execute(
                "SELECT hook_kind, transcript_path, failed_count, failure_reason "
                "FROM sanitize_log WHERE hook_kind='session_start_backfill'"
            ).fetchone()
            assert row["hook_kind"] == "session_start_backfill"
            assert row["transcript_path"] == "/tmp/transcript.jsonl"
            assert row["failed_count"] == 1
            assert row["failure_reason"] == "regex compile failed"
        finally:
            conn.close()

    def test_insert_with_invalid_hook_kind_fails(self, migrated_db):
        """hook_kind に 'post_tool_use' / 'session_start_backfill' 以外を指定すると CHECK 制約違反"""
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO sanitize_log (session_id, hook_kind) VALUES (?, ?)",
                    ("sess-001", "unknown_hook"),
                )
                conn.commit()
        finally:
            conn.close()

    def test_counter_defaults_to_zero(self, migrated_db):
        """occurrence_count / sanitized_count / failed_count を省略すると 0 になる"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO sanitize_log (session_id, hook_kind) VALUES (?, ?)",
                ("sess-001", "post_tool_use"),
            )
            conn.commit()

            row = conn.execute(
                "SELECT occurrence_count, sanitized_count, failed_count "
                "FROM sanitize_log WHERE hook_kind='post_tool_use'"
            ).fetchone()
            assert row["occurrence_count"] == 0
            assert row["sanitized_count"] == 0
            assert row["failed_count"] == 0
        finally:
            conn.close()

    def test_recorded_at_defaults_to_current_utc(self, migrated_db):
        """recorded_at を省略すると datetime('now') で UTC 自動 stamp される"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO sanitize_log (session_id, hook_kind) VALUES (?, ?)",
                ("sess-001", "post_tool_use"),
            )
            conn.commit()

            row = conn.execute(
                "SELECT recorded_at FROM sanitize_log WHERE hook_kind='post_tool_use'"
            ).fetchone()
            assert row["recorded_at"] is not None
            assert len(row["recorded_at"]) >= 19
        finally:
            conn.close()

    def test_recorded_at_rejects_explicit_null(self, migrated_db):
        """recorded_at に明示的に NULL を指定すると NOT NULL 違反"""
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO sanitize_log (session_id, hook_kind, recorded_at) "
                    "VALUES (?, ?, ?)",
                    ("sess-001", "post_tool_use", None),
                )
                conn.commit()
        finally:
            conn.close()

    def test_hook_kind_not_null(self, migrated_db):
        """hook_kind を省略すると NOT NULL 違反"""
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO sanitize_log (session_id) VALUES (?)",
                    ("sess-001",),
                )
                conn.commit()
        finally:
            conn.close()

    def test_counter_integrity_check_rejects_oversum(self, migrated_db):
        """sanitized_count + failed_count が occurrence_count を超えると CHECK 違反"""
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO sanitize_log (session_id, hook_kind, occurrence_count, "
                    "sanitized_count, failed_count) VALUES (?, ?, ?, ?, ?)",
                    ("sess-001", "post_tool_use", 5, 4, 2),
                )
                conn.commit()
        finally:
            conn.close()

    def test_counter_integrity_check_allows_equal(self, migrated_db):
        """sanitized_count + failed_count == occurrence_count は許容"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO sanitize_log (session_id, hook_kind, occurrence_count, "
                "sanitized_count, failed_count) VALUES (?, ?, ?, ?, ?)",
                ("sess-001", "post_tool_use", 5, 3, 2),
            )
            conn.commit()
        finally:
            conn.close()

    def test_session_or_transcript_required(self, migrated_db):
        """session_id と transcript_path が両方 NULL の行は CHECK 違反"""
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO sanitize_log (hook_kind) VALUES (?)",
                    ("post_tool_use",),
                )
                conn.commit()
        finally:
            conn.close()


class TestSanitizeLogQueriesByIndex:
    """インデックス経由のクエリが期待通り行を取得できることの確認"""

    def test_query_by_session_id(self, migrated_db):
        """idx_sanitize_log_session 経由で特定 session の行を取得できる"""
        conn = get_connection()
        try:
            for session_id, occurrence in [("s1", 1), ("s1", 2), ("s2", 3)]:
                conn.execute(
                    "INSERT INTO sanitize_log (session_id, hook_kind, occurrence_count) "
                    "VALUES (?, ?, ?)",
                    (session_id, "post_tool_use", occurrence),
                )
            conn.commit()

            rows = conn.execute(
                "SELECT occurrence_count FROM sanitize_log WHERE session_id=? ORDER BY id",
                ("s1",),
            ).fetchall()
            assert [r["occurrence_count"] for r in rows] == [1, 2]
        finally:
            conn.close()

    def test_query_by_recorded_at_range(self, migrated_db):
        """idx_sanitize_log_recorded_at 経由で時系列フィルタが動く"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO sanitize_log (session_id, hook_kind, occurrence_count, recorded_at) "
                "VALUES (?, ?, ?, ?)",
                ("sess-old", "post_tool_use", 1, "2026-01-01 00:00:00"),
            )
            conn.execute(
                "INSERT INTO sanitize_log (session_id, hook_kind, occurrence_count, recorded_at) "
                "VALUES (?, ?, ?, ?)",
                ("sess-new", "post_tool_use", 2, "2026-06-01 00:00:00"),
            )
            conn.commit()

            row = conn.execute(
                "SELECT occurrence_count FROM sanitize_log "
                "WHERE recorded_at >= ? AND recorded_at < ? ",
                ("2026-06-01 00:00:00", "2027-01-01 00:00:00"),
            ).fetchone()
            assert row is not None
            assert row["occurrence_count"] == 2
        finally:
            conn.close()


class TestExistingDataPreserved:
    """0044 適用が他テーブルの既存データを破壊しないことの確認"""

    def test_existing_activity_rows_preserved(self, db_before_0044):
        """0044 適用前後で activities の既存行が保持される"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO activities (title, description, status) VALUES (?, ?, ?)",
                ("既存タスク", "desc", "in_progress"),
            )
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0044(db_before_0044)

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT title, status FROM activities WHERE title=?",
                ("既存タスク",),
            ).fetchone()
            assert row is not None
            assert row["status"] == "in_progress"
        finally:
            conn.close()
