"""migration 0046_sanitize_log_to_citation_event_log のテスト

0046 適用後に sanitize_log が DROP され、citation_event_log テーブルが期待スキーマ
(カラム / NOT NULL / DEFAULT / CHECK 制約 / インデックス / view 3 本) で作成されている
ことを確認する。0044 INSERT 経路未実装のためデータ移行は不要 (forward-only)。
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
    """全 migration (0046 含む) を適用済みのテスト用 DB を提供する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def db_before_0046():
    """0045 までの migration を適用した DB を提供する。0046 の挙動を分離検証する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path

        parsed = parse_uri(f"sqlite:///{db_path}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        backend.init_database()
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        pre_0046 = MigrationList([m for m in all_migs if m.id < "0046"])
        with backend.lock():
            backend.apply_migrations(pre_0046)

        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


def _apply_migration_0046(db_path: str) -> None:
    """db_path に対して migration 0046 のみを適用する。"""
    parsed = parse_uri(f"sqlite:///{db_path}")
    backend = _VecSQLiteBackend(parsed, default_migration_table)
    all_migs = read_migrations(str(MIGRATIONS_DIR))
    only_0046 = MigrationList(
        [m for m in all_migs if m.id.startswith("0046")]
    )
    with backend.lock():
        backend.apply_migrations(only_0046)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _view_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='view' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _get_column_info(conn: sqlite3.Connection, table: str) -> dict[str, dict]:
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


# --------------------------------------------------------------------------
# テーブル / view の作成
# --------------------------------------------------------------------------
class TestCitationEventLogTableCreated:
    """0046 適用後に citation_event_log が期待スキーマで作成され、sanitize_log は
    DROP されていることの確認"""

    def test_sanitize_log_dropped_after_0046(self, migrated_db):
        """0046 適用後に sanitize_log が存在しない"""
        conn = get_connection()
        try:
            assert not _table_exists(conn, "sanitize_log"), (
                "sanitize_log が 0046 適用後に残存している"
            )
        finally:
            conn.close()

    def test_sanitize_log_exists_before_0046(self, db_before_0046):
        """0046 適用前 (0044 適用済) は sanitize_log が存在する (前提確認)"""
        conn = get_connection()
        try:
            assert _table_exists(conn, "sanitize_log"), (
                "0046 適用前に sanitize_log が存在しない"
            )
        finally:
            conn.close()

    def test_citation_event_log_exists_after_0046(self, migrated_db):
        """citation_event_log が作成されている"""
        conn = get_connection()
        try:
            assert _table_exists(conn, "citation_event_log")
        finally:
            conn.close()

    def test_citation_event_log_does_not_exist_before_0046(self, db_before_0046):
        """0046 適用前に citation_event_log が存在しない"""
        conn = get_connection()
        try:
            assert not _table_exists(conn, "citation_event_log")
        finally:
            conn.close()

    def test_all_columns_present_with_expected_types(self, migrated_db):
        """citation_event_log の全カラムが期待型・NOT NULL・DEFAULT で存在する"""
        conn = get_connection()
        try:
            cols = _get_column_info(conn, "citation_event_log")

            assert cols["id"]["pk"] == 1
            assert cols["id"]["type"] == "INTEGER"

            assert cols["occurred_at"]["type"] == "TEXT"
            assert cols["occurred_at"]["notnull"] == 1
            assert cols["occurred_at"]["dflt_value"] is not None
            assert "datetime" in cols["occurred_at"]["dflt_value"]

            assert cols["source"]["type"] == "TEXT"
            assert cols["source"]["notnull"] == 1

            for nullable_text in ("tool_name", "target_entity_type", "target_field",
                                  "verified_at", "verification_result", "extra_json"):
                assert cols[nullable_text]["type"] == "TEXT"
                assert cols[nullable_text]["notnull"] == 0

            assert cols["target_entity_id"]["type"] == "INTEGER"
            assert cols["target_entity_id"]["notnull"] == 0

            for required_text in ("before_text", "after_text"):
                assert cols[required_text]["type"] == "TEXT"
                assert cols[required_text]["notnull"] == 1
        finally:
            conn.close()

    def test_indexes_created(self, migrated_db):
        """target / source / occurred_at インデックスが作成されている"""
        conn = get_connection()
        try:
            indexes = _get_index_names(conn, "citation_event_log")
            assert "idx_citation_event_log_target" in indexes
            assert "idx_citation_event_log_source" in indexes
            assert "idx_citation_event_log_occurred_at" in indexes
        finally:
            conn.close()

    def test_three_views_created(self, migrated_db):
        """sanitize_event_log / auto_convert_event_log / citation_event_log_by_entity
        の view 3 本が作成されている"""
        conn = get_connection()
        try:
            for view_name in (
                "sanitize_event_log",
                "auto_convert_event_log",
                "citation_event_log_by_entity",
            ):
                assert _view_exists(conn, view_name), f"view {view_name} not created"
        finally:
            conn.close()


# --------------------------------------------------------------------------
# CHECK 制約
# --------------------------------------------------------------------------
class TestCitationEventLogCheckConstraints:
    """source / target_entity_type / verification_result の CHECK 制約と
    before_text/after_text の NOT NULL 制約の動作確認"""

    @pytest.mark.parametrize(
        "source",
        [
            "write_auto_convert",
            "bulk_migration",
            "transcript_post_tool_use",
            "transcript_session_start_backfill",
            "external_doc_sanitize",
        ],
    )
    def test_insert_with_valid_source_succeeds(self, migrated_db, source):
        """source が許容 ENUM 値 5 種それぞれで INSERT 成功"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO citation_event_log (source, before_text, after_text) "
                "VALUES (?, ?, ?)",
                (source, "before", "after"),
            )
            conn.commit()
        finally:
            conn.close()

    def test_insert_with_invalid_source_fails(self, migrated_db):
        """source が未知値だと CHECK 違反"""
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO citation_event_log (source, before_text, after_text) "
                    "VALUES (?, ?, ?)",
                    ("unknown_source", "before", "after"),
                )
                conn.commit()
        finally:
            conn.close()

    @pytest.mark.parametrize(
        "target_entity_type",
        ["decision", "activity", "log", "material", "topic"],
    )
    def test_insert_with_valid_target_entity_type_succeeds(
        self, migrated_db, target_entity_type
    ):
        """target_entity_type が許容 5 種それぞれで INSERT 成功"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO citation_event_log "
                "(source, target_entity_type, before_text, after_text) "
                "VALUES (?, ?, ?, ?)",
                ("write_auto_convert", target_entity_type, "before", "after"),
            )
            conn.commit()
        finally:
            conn.close()

    def test_insert_with_null_target_entity_type_succeeds(self, migrated_db):
        """target_entity_type が NULL でも INSERT 成功 (省略許容)"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO citation_event_log (source, before_text, after_text) "
                "VALUES (?, ?, ?)",
                ("write_auto_convert", "before", "after"),
            )
            conn.commit()
        finally:
            conn.close()

    def test_insert_with_invalid_target_entity_type_fails(self, migrated_db):
        """target_entity_type が許容 5 種以外だと CHECK 違反"""
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO citation_event_log "
                    "(source, target_entity_type, before_text, after_text) "
                    "VALUES (?, ?, ?, ?)",
                    ("write_auto_convert", "habit", "before", "after"),
                )
                conn.commit()
        finally:
            conn.close()

    @pytest.mark.parametrize(
        "verification_result", ["exists", "dangling", "skip"]
    )
    def test_insert_with_valid_verification_result_succeeds(
        self, migrated_db, verification_result
    ):
        """verification_result が許容 3 種それぞれで INSERT 成功"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO citation_event_log "
                "(source, before_text, after_text, verification_result) "
                "VALUES (?, ?, ?, ?)",
                ("write_auto_convert", "before", "after", verification_result),
            )
            conn.commit()
        finally:
            conn.close()

    def test_insert_with_null_verification_result_succeeds(self, migrated_db):
        """verification_result が NULL でも INSERT 成功"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO citation_event_log (source, before_text, after_text) "
                "VALUES (?, ?, ?)",
                ("write_auto_convert", "before", "after"),
            )
            conn.commit()
        finally:
            conn.close()

    def test_insert_with_invalid_verification_result_fails(self, migrated_db):
        """verification_result が許容 3 種以外だと CHECK 違反"""
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO citation_event_log "
                    "(source, before_text, after_text, verification_result) "
                    "VALUES (?, ?, ?, ?)",
                    ("write_auto_convert", "before", "after", "unknown_result"),
                )
                conn.commit()
        finally:
            conn.close()

    def test_before_text_not_null(self, migrated_db):
        """before_text を省略すると NOT NULL 違反"""
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO citation_event_log (source, after_text) "
                    "VALUES (?, ?)",
                    ("write_auto_convert", "after"),
                )
                conn.commit()
        finally:
            conn.close()

    def test_after_text_not_null(self, migrated_db):
        """after_text を省略すると NOT NULL 違反"""
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO citation_event_log (source, before_text) "
                    "VALUES (?, ?)",
                    ("write_auto_convert", "before"),
                )
                conn.commit()
        finally:
            conn.close()

    def test_occurred_at_defaults_to_current_utc(self, migrated_db):
        """occurred_at を省略すると datetime('now') で UTC 自動 stamp"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO citation_event_log (source, before_text, after_text) "
                "VALUES (?, ?, ?)",
                ("write_auto_convert", "before", "after"),
            )
            conn.commit()
            row = conn.execute(
                "SELECT occurred_at FROM citation_event_log"
            ).fetchone()
            assert row["occurred_at"] is not None
            assert len(row["occurred_at"]) >= 19
        finally:
            conn.close()


# --------------------------------------------------------------------------
# view 3 本の動作
# --------------------------------------------------------------------------
class TestCitationEventLogViews:
    """sanitize_event_log / auto_convert_event_log / citation_event_log_by_entity
    の SELECT 動作を確認"""

    def _seed_events(self, conn: sqlite3.Connection) -> None:
        """source 5 種をそれぞれ 1 件以上 seed する。target 集約検証のため
        material#1 を 2 イベント分入れる。"""
        rows = [
            # (source, target_entity_type, target_entity_id, occurred_at)
            ("write_auto_convert", "material", 1, "2026-06-01 00:00:00"),
            ("write_auto_convert", "material", 1, "2026-06-02 00:00:00"),
            ("bulk_migration", "decision", 10, "2026-06-03 00:00:00"),
            ("transcript_post_tool_use", "activity", 5, "2026-06-04 00:00:00"),
            ("transcript_session_start_backfill", "log", 7, "2026-06-05 00:00:00"),
            ("external_doc_sanitize", None, None, "2026-06-06 00:00:00"),
        ]
        conn.executemany(
            "INSERT INTO citation_event_log "
            "(source, target_entity_type, target_entity_id, before_text, after_text, occurred_at) "
            "VALUES (?, ?, ?, 'before', 'after', ?)",
            rows,
        )
        conn.commit()

    def test_sanitize_event_log_view(self, migrated_db):
        """sanitize_event_log は transcript_*/external_doc_sanitize の 3 件のみ返す"""
        conn = get_connection()
        try:
            self._seed_events(conn)
            rows = conn.execute(
                "SELECT source FROM sanitize_event_log ORDER BY occurred_at"
            ).fetchall()
            sources = [r["source"] for r in rows]
            assert sources == [
                "transcript_post_tool_use",
                "transcript_session_start_backfill",
                "external_doc_sanitize",
            ]
        finally:
            conn.close()

    def test_auto_convert_event_log_view(self, migrated_db):
        """auto_convert_event_log は write_auto_convert + bulk_migration を返す"""
        conn = get_connection()
        try:
            self._seed_events(conn)
            rows = conn.execute(
                "SELECT source FROM auto_convert_event_log ORDER BY occurred_at"
            ).fetchall()
            sources = [r["source"] for r in rows]
            assert sources == [
                "write_auto_convert",
                "write_auto_convert",
                "bulk_migration",
            ]
        finally:
            conn.close()

    def test_citation_event_log_by_entity_view(self, migrated_db):
        """target 単位で COUNT(*) と MAX(occurred_at) を集約する。
        target が NULL のイベントは集約対象外。"""
        conn = get_connection()
        try:
            self._seed_events(conn)
            rows = conn.execute(
                "SELECT target_entity_type, target_entity_id, event_count, last_occurred_at "
                "FROM citation_event_log_by_entity "
                "ORDER BY target_entity_type, target_entity_id"
            ).fetchall()
            triples = [
                (r["target_entity_type"], r["target_entity_id"],
                 r["event_count"], r["last_occurred_at"])
                for r in rows
            ]
            assert triples == [
                ("activity", 5, 1, "2026-06-04 00:00:00"),
                ("decision", 10, 1, "2026-06-03 00:00:00"),
                ("log", 7, 1, "2026-06-05 00:00:00"),
                ("material", 1, 2, "2026-06-02 00:00:00"),
            ]
        finally:
            conn.close()


# --------------------------------------------------------------------------
# 既存データ保全
# --------------------------------------------------------------------------
class TestExistingDataPreserved:
    """0046 適用が他テーブルの既存データを壊さないことの確認"""

    def test_existing_activity_rows_preserved(self, db_before_0046):
        """0046 適用前後で activities の既存行が保持される"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO activities (title, description, status) VALUES (?, ?, ?)",
                ("既存タスク", "desc", "in_progress"),
            )
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0046(db_before_0046)

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
