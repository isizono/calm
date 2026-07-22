"""migration 0062_add_tags_last_injected_at のテスト

0062適用後に tags テーブルへ last_injected_at 列が追加され、既定値がNULLであること、
既存タグデータが書き換わらないことを確認する。
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
    """全migration（0062含む）を適用済みのテスト用DBを提供する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def db_before_0062():
    """0061までのmigrationを適用したDBを提供する。0062の挙動を分離検証するために使う。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path

        parsed = parse_uri(f"sqlite:///{db_path}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        backend.init_database()
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        pre_0062 = MigrationList([m for m in all_migs if m.id < "0062"])
        with backend.lock():
            backend.apply_migrations(pre_0062)

        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


def _apply_migration_0062(db_path: str) -> None:
    """db_pathに対してmigration 0062のみを適用する。"""
    parsed = parse_uri(f"sqlite:///{db_path}")
    backend = _VecSQLiteBackend(parsed, default_migration_table)
    all_migs = read_migrations(str(MIGRATIONS_DIR))
    only_0062 = MigrationList([m for m in all_migs if m.id.startswith("0062")])
    with backend.lock():
        backend.apply_migrations(only_0062)


def _get_column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    """指定テーブルのカラム名セットを返す。"""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


def _insert_tag(conn: sqlite3.Connection, namespace: str, name: str) -> int:
    """tagsに1行INSERTしてidを返す（last_injected_atは既定値のまま）。"""
    cur = conn.execute(
        "INSERT INTO tags (namespace, name) VALUES (?, ?)", (namespace, name)
    )
    return cur.lastrowid


class TestColumnAdded:
    """0062適用後にlast_injected_at列が追加されていることの確認"""

    def test_tags_has_new_column_after_0062(self, migrated_db):
        """migration 0062 適用後、tags テーブルに last_injected_at が存在する"""
        conn = get_connection()
        try:
            column_names = _get_column_names(conn, "tags")
            assert "last_injected_at" in column_names, (
                "tags.last_injected_at が 0062 適用後に存在しない"
            )
        finally:
            conn.close()

    def test_tags_has_no_new_column_before_0062(self, db_before_0062):
        """0061 適用時点では last_injected_at が存在しない（前提確認）"""
        conn = get_connection()
        try:
            column_names = _get_column_names(conn, "tags")
            assert "last_injected_at" not in column_names, (
                "0062 適用前の tags に last_injected_at 列が既に存在している"
            )
        finally:
            conn.close()

    def test_default_value_for_new_rows(self, migrated_db):
        """0062 適用後、last_injected_atを指定せず INSERT した行はNULL"""
        conn = get_connection()
        try:
            tag_id = _insert_tag(conn, "domain", "new-tag-for-0062-test")
            conn.commit()
            row = conn.execute(
                "SELECT last_injected_at FROM tags WHERE id = ?", (tag_id,)
            ).fetchone()
            assert row["last_injected_at"] is None
        finally:
            conn.close()


class TestNoDataMutation:
    """0062がスキーマ変更のみで、既存タグのlast_injected_atを書き換えないことの確認"""

    def test_existing_tags_remain_null_after_0062(self, db_before_0062):
        """0061時点で存在するタグは、idにかかわらず0062適用後も全てlast_injected_at=NULLのまま"""
        conn = get_connection()
        try:
            ids = [
                _insert_tag(conn, "domain", f"pre-existing-tag-{i}")
                for i in range(1, 21)
            ]
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0062(db_before_0062)

        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT id, last_injected_at FROM tags WHERE id IN ({})".format(
                    ",".join("?" * len(ids))
                ),
                ids,
            ).fetchall()
            assert len(rows) == len(ids)
            for row in rows:
                assert row["last_injected_at"] is None, (
                    f"tag id={row['id']} が 0062 適用だけで last_injected_at を "
                    "書き換えられている（データ移行を含まない前提に反する）"
                )
        finally:
            conn.close()
