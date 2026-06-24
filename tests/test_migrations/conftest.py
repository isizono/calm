"""test_migrations 共通のスキーマ検査ユーティリティ。

migration テストで頻出する「カラム名一覧」「テーブル存在確認」「インデックス名一覧」を
sqlite_master / PRAGMA から取得する純粋ヘルパーを提供する。
"""
import sqlite3


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
