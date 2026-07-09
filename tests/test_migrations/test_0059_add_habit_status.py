"""migration 0059_add_habit_status のテスト

0059適用後に habits テーブルへ status 列が追加され、既定値 'active' と
CHECK制約（'active'/'archived'のみ許可）が仕様通りであることを確認する。
"""
import os
import sqlite3
import tempfile

import pytest

from src.db import get_connection, init_database


@pytest.fixture
def migrated_db():
    """全migration（0059含む）を適用済みのテスト用DBを提供する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


def _get_column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    """指定テーブルのカラム名セットを返す。"""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


class TestStatusColumnAdded:
    """0059適用後にstatus列が追加されていることの確認"""

    def test_habits_has_status_column(self, migrated_db):
        """migration 0059 適用後、habits テーブルに status 列が存在する"""
        conn = get_connection()
        try:
            column_names = _get_column_names(conn, "habits")
            assert "status" in column_names
        finally:
            conn.close()

    def test_default_status_is_active(self, migrated_db):
        """status を指定せず INSERT した行は既定値 'active' になる"""
        conn = get_connection()
        try:
            cursor = conn.execute(
                "INSERT INTO habits (content) VALUES (?)", ("テスト振る舞い",)
            )
            habit_id = cursor.lastrowid
            conn.commit()
            row = conn.execute(
                "SELECT status FROM habits WHERE id = ?", (habit_id,)
            ).fetchone()
            assert row["status"] == "active"
        finally:
            conn.close()

    def test_status_check_constraint_rejects_invalid_value(self, migrated_db):
        """statusのCHECK制約が'active'/'archived'以外を拒否する"""
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO habits (content, status) VALUES (?, ?)",
                    ("不正な値", "deleted"),
                )
        finally:
            conn.rollback()
            conn.close()

    def test_status_check_constraint_accepts_archived(self, migrated_db):
        """statusに'archived'を指定してINSERTできる"""
        conn = get_connection()
        try:
            cursor = conn.execute(
                "INSERT INTO habits (content, status) VALUES (?, ?)",
                ("アーカイブ済み振る舞い", "archived"),
            )
            habit_id = cursor.lastrowid
            conn.commit()
            row = conn.execute(
                "SELECT status FROM habits WHERE id = ?", (habit_id,)
            ).fetchone()
            assert row["status"] == "archived"
        finally:
            conn.close()
