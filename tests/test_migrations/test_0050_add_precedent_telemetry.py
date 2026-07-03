"""migration 0050_add_precedent_telemetry のテスト

0050 適用後に precedent_telemetry テーブルとタイムスタンプ index が作成され、
NOT NULL 制約とデフォルト timestamp が機能することを確認する。
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
    """全 migration（0050 含む）を適用済みのテスト用 DB を提供する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def db_before_0050():
    """0049 までの migration を適用した DB を提供する。0050 の挙動を分離検証するために使う。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path

        parsed = parse_uri(f"sqlite:///{db_path}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        backend.init_database()
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        pre_0050 = MigrationList([m for m in all_migs if m.id < "0050"])
        with backend.lock():
            backend.apply_migrations(pre_0050)

        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


class TestPrecedentTelemetryTableCreated:
    def test_table_exists_after_0050(self, migrated_db):
        """migration 0050 適用後、precedent_telemetry テーブルが存在する"""
        conn = get_connection()
        try:
            assert table_exists(conn, "precedent_telemetry"), (
                "precedent_telemetry テーブルが 0050 適用後に存在しない"
            )
        finally:
            conn.close()

    def test_table_not_exists_before_0050(self, db_before_0050):
        """0050 適用前は precedent_telemetry テーブルが存在しない（前提確認）"""
        conn = get_connection()
        try:
            assert not table_exists(conn, "precedent_telemetry"), (
                "0050 適用前に precedent_telemetry テーブルが既に存在している"
            )
        finally:
            conn.close()

    def test_required_columns_exist(self, migrated_db):
        """0050 適用後、precedent_telemetry テーブルに必須カラムが全部存在する"""
        conn = get_connection()
        try:
            column_names = get_column_names(conn, "precedent_telemetry")
            required = {
                "id",
                "context",
                "parameters",
                "guarantee",
                "routing_json",
                "decisions_total",
                "full_count",
                "timestamp",
            }
            for col in required:
                assert col in column_names, (
                    f"precedent_telemetry.{col} が 0050 適用後に存在しない"
                )
        finally:
            conn.close()

    def test_timestamp_index_created(self, migrated_db):
        """0050 適用後、timestamp に index が作成されている"""
        conn = get_connection()
        try:
            idx_names = index_names(conn, "idx_precedent_telemetry_%")
            assert "idx_precedent_telemetry_timestamp" in idx_names
        finally:
            conn.close()


class TestPrecedentTelemetryCRUD:
    def test_insert_and_select(self, migrated_db):
        """precedent_telemetry に INSERT して SELECT で読み取れる。timestamp が自動設定される"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO precedent_telemetry "
                "(context, parameters, guarantee, routing_json, decisions_total, full_count) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("設計文脈のテスト", '{"k": 3}', "enumerated", '{"candidates": []}', 5, 5),
            )
            conn.commit()
            row = conn.execute(
                "SELECT context, parameters, guarantee, routing_json, decisions_total, "
                "full_count, timestamp FROM precedent_telemetry WHERE context = ?",
                ("設計文脈のテスト",),
            ).fetchone()
            assert row is not None
            assert row["guarantee"] == "enumerated"
            assert row["decisions_total"] == 5
            assert row["full_count"] == 5
            assert row["timestamp"] is not None
        finally:
            conn.close()

    @pytest.mark.parametrize(
        "missing_col",
        ["context", "parameters", "guarantee", "routing_json", "decisions_total", "full_count"],
    )
    def test_not_null_columns_enforced(self, migrated_db, missing_col):
        """NOT NULL カラムを欠いた INSERT は IntegrityError になる"""
        conn = get_connection()
        try:
            values = {
                "context": "文脈",
                "parameters": "{}",
                "guarantee": "enumerated",
                "routing_json": "{}",
                "decisions_total": 0,
                "full_count": 0,
            }
            values[missing_col] = None
            cols = ", ".join(values.keys())
            placeholders = ", ".join("?" for _ in values)
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    f"INSERT INTO precedent_telemetry ({cols}) VALUES ({placeholders})",
                    tuple(values.values()),
                )
                conn.commit()
        finally:
            conn.close()
