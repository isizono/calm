"""migration 0087_drop_leftover_fts5_check_tables のテスト

起動時FTS5可否チェックの後始末漏れで本体DBに残ってしまった_fts5_checkと、
その影のテーブル5つ（_fts5_check_config/_content/_data/_docsize/_idx）が、
0087適用で削除されることを確認する。
"""
import sqlite3

import pytest
from yoyo import default_migration_table, read_migrations
from yoyo.connections import parse_uri
from yoyo.migrations import MigrationList

from src.db import MIGRATIONS_DIR, _VecSQLiteBackend, get_connection
from src.services.tag_service import _injected_tags
from test_migrations.conftest import db_before_migration, table_exists

_SHADOW_TABLES = (
    "_fts5_check_config",
    "_fts5_check_content",
    "_fts5_check_data",
    "_fts5_check_docsize",
    "_fts5_check_idx",
)
_ALL_TABLES = ("_fts5_check",) + _SHADOW_TABLES


@pytest.fixture
def db_before_0087():
    """0081までのmigrationを適用したDBを提供する。0087の挙動を分離検証するために使う。"""
    with db_before_migration("0087") as db_path:
        _injected_tags.clear()
        yield db_path


def _apply_migration_0087(db_path: str) -> None:
    """db_path に対して migration 0087 のみを適用する。"""
    parsed = parse_uri(f"sqlite:///{db_path}")
    backend = _VecSQLiteBackend(parsed, default_migration_table)
    all_migs = read_migrations(str(MIGRATIONS_DIR))
    only_0087 = MigrationList([m for m in all_migs if m.id.startswith("0087")])
    with backend.lock():
        backend.apply_migrations(only_0087)


def _create_leftover_fts5_check(conn: sqlite3.Connection) -> None:
    """本番DBで観測された「チェック用FTS5テーブルが後始末されず残った」状態を再現する。"""
    conn.execute("CREATE VIRTUAL TABLE _fts5_check USING fts5(x)")
    conn.execute("INSERT INTO _fts5_check(x) VALUES ('leftover')")


class TestLeftoverTablesDropped:
    def test_leftover_tables_exist_before_0087(self, db_before_0087):
        conn = get_connection()
        try:
            _create_leftover_fts5_check(conn)
            conn.commit()
            for table in _ALL_TABLES:
                assert table_exists(conn, table), table
        finally:
            conn.close()

    def test_leftover_tables_are_dropped_after_0087(self, db_before_0087):
        conn = get_connection()
        try:
            _create_leftover_fts5_check(conn)
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0087(db_before_0087)

        conn = get_connection()
        try:
            for table in _ALL_TABLES:
                assert not table_exists(conn, table), table
        finally:
            conn.close()

    def test_0087_is_a_noop_when_no_leftover_tables_exist(self, db_before_0087):
        """leftoverテーブルが存在しない環境（通常のデプロイ先）でも0087適用は失敗しない。"""
        conn = get_connection()
        try:
            for table in _ALL_TABLES:
                assert not table_exists(conn, table), table
        finally:
            conn.close()

        _apply_migration_0087(db_before_0087)

        conn = get_connection()
        try:
            for table in _ALL_TABLES:
                assert not table_exists(conn, table), table
        finally:
            conn.close()
