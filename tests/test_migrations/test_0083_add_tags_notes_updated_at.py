"""migration 0083_add_tags_notes_updated_at のテスト

0083適用後に tags テーブルへ notes_updated_at 列が追加され、既定値がNULLであること、
既存タグデータが書き換わらないことを確認する。
"""
import sqlite3

import pytest
from yoyo import default_migration_table, read_migrations
from yoyo.connections import parse_uri
from yoyo.migrations import MigrationList

from src.db import MIGRATIONS_DIR, _VecSQLiteBackend, get_connection
from test_migrations.conftest import db_before_migration, get_column_names


@pytest.fixture
def migrated_db(temp_db):
    """全migration（0083含む）を適用済みのテスト用DBを提供する。"""
    yield temp_db


@pytest.fixture
def db_before_0083():
    """0081までのmigrationを適用したDBを提供する。0083の挙動を分離検証するために使う。"""
    with db_before_migration("0083") as db_path:
        yield db_path


def _apply_migration_0083(db_path: str) -> None:
    """db_pathに対してmigration 0083のみを適用する。"""
    parsed = parse_uri(f"sqlite:///{db_path}")
    backend = _VecSQLiteBackend(parsed, default_migration_table)
    all_migs = read_migrations(str(MIGRATIONS_DIR))
    only_0083 = MigrationList([m for m in all_migs if m.id.startswith("0083")])
    with backend.lock():
        backend.apply_migrations(only_0083)


def _insert_tag(conn: sqlite3.Connection, namespace: str, name: str) -> int:
    """tagsに1行INSERTしてidを返す（notes_updated_atは既定値のまま）。"""
    cur = conn.execute(
        "INSERT INTO tags (namespace, name) VALUES (?, ?)", (namespace, name)
    )
    return cur.lastrowid


class TestColumnAdded:
    """0083適用後にnotes_updated_at列が追加されていることの確認"""

    def test_tags_has_new_column_after_0083(self, migrated_db):
        """migration 0083 適用後、tags テーブルに notes_updated_at が存在する"""
        conn = get_connection()
        try:
            column_names = get_column_names(conn, "tags")
            assert "notes_updated_at" in column_names, (
                "tags.notes_updated_at が 0083 適用後に存在しない"
            )
        finally:
            conn.close()

    def test_tags_has_no_new_column_before_0083(self, db_before_0083):
        """0081 適用時点では notes_updated_at が存在しない（前提確認）"""
        conn = get_connection()
        try:
            column_names = get_column_names(conn, "tags")
            assert "notes_updated_at" not in column_names, (
                "0083 適用前の tags に notes_updated_at 列が既に存在している"
            )
        finally:
            conn.close()

    def test_default_value_for_new_rows(self, migrated_db):
        """0083 適用後、notes_updated_atを指定せず INSERT した行はNULL"""
        conn = get_connection()
        try:
            tag_id = _insert_tag(conn, "domain", "new-tag-for-0083-test")
            conn.commit()
            row = conn.execute(
                "SELECT notes_updated_at FROM tags WHERE id = ?", (tag_id,)
            ).fetchone()
            assert row["notes_updated_at"] is None
        finally:
            conn.close()


class TestNoDataMutation:
    """0083がスキーマ変更のみで、既存タグのnotes_updated_atを書き換えないことの確認"""

    def test_existing_tags_remain_null_after_0083(self, db_before_0083):
        """0081時点で存在するタグは、idにかかわらず0083適用後も全てnotes_updated_at=NULLのまま"""
        conn = get_connection()
        try:
            ids = [
                _insert_tag(conn, "domain", f"pre-existing-tag-{i}")
                for i in range(1, 21)
            ]
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0083(db_before_0083)

        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT id, notes_updated_at FROM tags WHERE id IN ({})".format(
                    ",".join("?" * len(ids))
                ),
                ids,
            ).fetchall()
            assert len(rows) == len(ids)
            for row in rows:
                assert row["notes_updated_at"] is None, (
                    f"tag id={row['id']} が 0083 適用だけで notes_updated_at を "
                    "書き換えられている（データ移行を含まない前提に反する）"
                )
        finally:
            conn.close()
