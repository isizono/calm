"""migration 0058_add_habit_trigger_mode のテスト

0058適用後に habits テーブルへ description / trigger_mode / importance_score /
last_recalled_at の4列が追加され、既定値が仕様通りであることを確認する。

0058はスキーマ変更のみで、既存habitの trigger_mode を書き換えるデータ移行を
含まない（habit idはinstall先ごとの採番historyに依存するため、特定idへの
リテラルUPDATEは他環境で無関係な行を書き換えるリスクがある）。本テストは
「0058適用後も既存habitは全てtrigger_mode='always'のまま」を確認することで
この no-op-on-data 特性を保証する。
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
    """全migration（0058含む）を適用済みのテスト用DBを提供する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def db_before_0058():
    """0057までのmigrationを適用したDBを提供する。0058の挙動を分離検証するために使う。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path

        parsed = parse_uri(f"sqlite:///{db_path}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        backend.init_database()
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        pre_0058 = MigrationList([m for m in all_migs if m.id < "0058"])
        with backend.lock():
            backend.apply_migrations(pre_0058)

        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


def _apply_migration_0058(db_path: str) -> None:
    """db_pathに対してmigration 0058のみを適用する。"""
    parsed = parse_uri(f"sqlite:///{db_path}")
    backend = _VecSQLiteBackend(parsed, default_migration_table)
    all_migs = read_migrations(str(MIGRATIONS_DIR))
    only_0058 = MigrationList([m for m in all_migs if m.id.startswith("0058")])
    with backend.lock():
        backend.apply_migrations(only_0058)


def _get_column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    """指定テーブルのカラム名セットを返す。"""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


def _insert_habit(conn: sqlite3.Connection, content: str) -> int:
    """habitsに1行INSERTしてidを返す（0057時点のスキーマ、trigger_mode等は未存在）。"""
    cur = conn.execute("INSERT INTO habits (content) VALUES (?)", (content,))
    return cur.lastrowid


class TestColumnsAdded:
    """0058適用後に新規4列が追加されていることの確認"""

    def test_habits_has_new_columns_after_0058(self, migrated_db):
        """migration 0058 適用後、habits テーブルに4列すべてが存在する"""
        conn = get_connection()
        try:
            column_names = _get_column_names(conn, "habits")
            for col in ("description", "trigger_mode", "importance_score", "last_recalled_at"):
                assert col in column_names, f"habits.{col} が 0058 適用後に存在しない"
        finally:
            conn.close()

    def test_habits_has_no_new_columns_before_0058(self, db_before_0058):
        """0057 適用時点では新規4列が存在しない（前提確認）"""
        conn = get_connection()
        try:
            column_names = _get_column_names(conn, "habits")
            for col in ("description", "trigger_mode", "importance_score", "last_recalled_at"):
                assert col not in column_names, (
                    f"0058 適用前の habits に {col} 列が既に存在している"
                )
        finally:
            conn.close()

    def test_default_values_for_new_rows(self, migrated_db):
        """0058 適用後、新規4列を指定せず INSERT した行は既定値になる"""
        conn = get_connection()
        try:
            habit_id = _insert_habit(conn, "テスト振る舞い")
            conn.commit()
            row = conn.execute(
                "SELECT description, trigger_mode, importance_score, last_recalled_at "
                "FROM habits WHERE id = ?",
                (habit_id,),
            ).fetchone()
            assert row["description"] == ""
            assert row["trigger_mode"] == "always"
            assert row["importance_score"] == 1.0
            assert row["last_recalled_at"] is None
        finally:
            conn.close()

    def test_trigger_mode_check_constraint_rejects_invalid_value(self, migrated_db):
        """trigger_modeのCHECK制約が'always'/'intelligently'以外を拒否する"""
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO habits (content, trigger_mode) VALUES (?, ?)",
                    ("不正な値", "invalid_mode"),
                )
        finally:
            conn.rollback()
            conn.close()


class TestNoDataMutation:
    """0058がスキーマ変更のみで、既存habitのtrigger_modeを書き換えないことの確認"""

    def test_existing_habits_remain_always_after_0058(self, db_before_0058):
        """0057時点で存在するhabitは、idにかかわらず0058適用後も全てtrigger_mode='always'のまま"""
        conn = get_connection()
        try:
            # migration 0058 が過去に対象としていた id 群を含め、複数habitを作成する
            ids = [_insert_habit(conn, f"habit-{i}") for i in range(1, 21)]
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0058(db_before_0058)

        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT id, trigger_mode FROM habits WHERE id IN ({})".format(
                    ",".join("?" * len(ids))
                ),
                ids,
            ).fetchall()
            assert len(rows) == len(ids)
            for row in rows:
                assert row["trigger_mode"] == "always", (
                    f"habit id={row['id']} が 0058 適用だけで trigger_mode を "
                    "書き換えられている（データ移行を含まない前提に反する）"
                )
        finally:
            conn.close()
