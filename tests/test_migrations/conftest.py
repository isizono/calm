"""test_migrations 共通のスキーマ検査ユーティリティ。

migration テストで頻出する「カラム名一覧」「テーブル存在確認」「インデックス名一覧」を
sqlite_master / PRAGMA から取得する純粋ヘルパーを提供する。
"""
import contextlib
import os
import sqlite3
import tempfile

from yoyo import default_migration_table, read_migrations
from yoyo.connections import parse_uri
from yoyo.migrations import MigrationList

from src.db import MIGRATIONS_DIR, _VecSQLiteBackend


@contextlib.contextmanager
def db_before_migration(migration_id: str):
    """migration_id未満のmigrationのみを適用したテスト用DBを提供する。

    各test_00XX.pyのdb_before_00XX()フィクスチャが、対象migration IDを渡して呼び出す。
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path

        parsed = parse_uri(f"sqlite:///{db_path}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        backend.init_database()
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        pre = MigrationList([m for m in all_migs if m.id < migration_id])
        with backend.lock():
            backend.apply_migrations(pre)

        try:
            yield db_path
        finally:
            os.environ.pop("DISCUSSION_DB_PATH", None)


def get_column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    """指定テーブルのカラム名セットを返す。"""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """指定テーブルが sqlite_master に存在するか確認する。"""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def index_names(conn: sqlite3.Connection, name_pattern: str) -> set[str]:
    """sqlite_master から name LIKE pattern のインデックス名セットを返す。"""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE ?",
        (name_pattern,),
    ).fetchall()
    return {row["name"] for row in rows}
