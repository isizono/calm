"""migration 0091_add_destination_telemetry のテスト

適用前はテーブルが無く、適用後は必須カラムの NOT NULL とデフォルト timestamp が機能する。
"""
import sqlite3

import pytest

from src.db import get_connection
from test_migrations.conftest import db_before_migration, table_exists


def test_table_absent_before_migration():
    with db_before_migration("0091"):
        conn = get_connection(load_vec=False)
        try:
            assert not table_exists(conn, "destination_telemetry")
        finally:
            conn.close()


def test_not_null_columns_and_default_timestamp(temp_db):
    conn = get_connection(load_vec=False)
    try:
        conn.execute(
            "INSERT INTO destination_telemetry (trigger_tool, path, candidate_count, reason) "
            "VALUES ('check_in', 'nearby', 0, 'no_candidates')"
        )
        assert conn.execute("SELECT timestamp FROM destination_telemetry").fetchone()[0] is not None
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO destination_telemetry (trigger_tool, path, candidate_count) VALUES ('a', 'b', 0)")
    finally:
        conn.rollback()
        conn.close()
