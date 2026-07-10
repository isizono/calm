"""migration 0059_add_tag_archived のテスト

0059適用後に tags テーブルへ archived_at / archived_reason の2列と、
archived_at 用の部分インデックス idx_tags_archived_at が追加され、
archived_reason の長さ制約（100文字以内）が effectiveであることを確認する。

0059はスキーマ変更のみで、既存タグの archived_at / archived_reason を書き換える
データ移行を含まない（既存タグは全てNULLのまま非archived扱いになる）。
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
    """全migration（0059含む）を適用済みのテスト用DBを提供する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def db_before_0059():
    """0058までのmigrationを適用したDBを提供する。0059の挙動を分離検証するために使う。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path

        parsed = parse_uri(f"sqlite:///{db_path}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        backend.init_database()
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        pre_0059 = MigrationList([m for m in all_migs if m.id < "0059"])
        with backend.lock():
            backend.apply_migrations(pre_0059)

        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


def _apply_migration_0059(db_path: str) -> None:
    """db_pathに対してmigration 0059のみを適用する。"""
    parsed = parse_uri(f"sqlite:///{db_path}")
    backend = _VecSQLiteBackend(parsed, default_migration_table)
    all_migs = read_migrations(str(MIGRATIONS_DIR))
    only_0059 = MigrationList([m for m in all_migs if m.id.startswith("0059")])
    with backend.lock():
        backend.apply_migrations(only_0059)


def _get_column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    """指定テーブルのカラム名セットを返す。"""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


def _get_index_names(conn: sqlite3.Connection, table: str) -> set[str]:
    """指定テーブルのインデックス名セットを返す。"""
    rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
    return {row["name"] for row in rows}


def _insert_tag(conn: sqlite3.Connection, namespace: str, name: str) -> int:
    """tagsに1行INSERTしてidを返す（archived_at/archived_reasonは既定値のまま）。"""
    cur = conn.execute(
        "INSERT INTO tags (namespace, name) VALUES (?, ?)", (namespace, name)
    )
    return cur.lastrowid


class TestColumnsAndIndexAdded:
    """0059適用後にarchived_at/archived_reason列とpartial indexが追加されていることの確認"""

    def test_tags_has_new_columns_after_0059(self, migrated_db):
        """migration 0059 適用後、tags テーブルに archived_at / archived_reason が存在する"""
        conn = get_connection()
        try:
            column_names = _get_column_names(conn, "tags")
            for col in ("archived_at", "archived_reason"):
                assert col in column_names, f"tags.{col} が 0059 適用後に存在しない"
        finally:
            conn.close()

    def test_tags_has_no_new_columns_before_0059(self, db_before_0059):
        """0058 適用時点では archived_at / archived_reason が存在しない（前提確認）"""
        conn = get_connection()
        try:
            column_names = _get_column_names(conn, "tags")
            for col in ("archived_at", "archived_reason"):
                assert col not in column_names, (
                    f"0059 適用前の tags に {col} 列が既に存在している"
                )
        finally:
            conn.close()

    def test_partial_index_exists_after_0059(self, migrated_db):
        """migration 0059 適用後、idx_tags_archived_at インデックスが存在する"""
        conn = get_connection()
        try:
            assert "idx_tags_archived_at" in _get_index_names(conn, "tags")
        finally:
            conn.close()

    def test_partial_index_absent_before_0059(self, db_before_0059):
        """0058 適用時点では idx_tags_archived_at インデックスが存在しない（前提確認）"""
        conn = get_connection()
        try:
            assert "idx_tags_archived_at" not in _get_index_names(conn, "tags")
        finally:
            conn.close()

    def test_default_values_for_new_rows(self, migrated_db):
        """0059 適用後、archived_at/archived_reasonを指定せず INSERT した行は両方NULL"""
        conn = get_connection()
        try:
            tag_id = _insert_tag(conn, "domain", "new-tag-for-0059-test")
            conn.commit()
            row = conn.execute(
                "SELECT archived_at, archived_reason FROM tags WHERE id = ?",
                (tag_id,),
            ).fetchone()
            assert row["archived_at"] is None
            assert row["archived_reason"] is None
        finally:
            conn.close()

    def test_archived_reason_check_constraint_rejects_too_long_value(self, migrated_db):
        """archived_reasonのCHECK制約が100文字超過を拒否する"""
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO tags (namespace, name, archived_reason) VALUES (?, ?, ?)",
                    ("domain", "too-long-archived-reason-tag", "a" * 101),
                )
        finally:
            conn.rollback()
            conn.close()

    def test_archived_reason_check_constraint_allows_100_chars(self, migrated_db):
        """archived_reasonがちょうど100文字はCHECK制約を通過する"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO tags (namespace, name, archived_reason) VALUES (?, ?, ?)",
                ("domain", "exactly-100-chars-archived-reason-tag", "a" * 100),
            )
            conn.commit()
            row = conn.execute(
                "SELECT archived_reason FROM tags WHERE namespace = 'domain' "
                "AND name = 'exactly-100-chars-archived-reason-tag'"
            ).fetchone()
            assert row["archived_reason"] == "a" * 100
        finally:
            conn.close()


class TestNoDataMutation:
    """0059がスキーマ変更のみで、既存タグのarchived_at/archived_reasonを書き換えないことの確認"""

    def test_existing_tags_remain_non_archived_after_0059(self, db_before_0059):
        """0058時点で存在するタグは、idにかかわらず0059適用後も全てarchived_at=NULLのまま"""
        conn = get_connection()
        try:
            ids = [
                _insert_tag(conn, "domain", f"pre-existing-tag-{i}")
                for i in range(1, 21)
            ]
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0059(db_before_0059)

        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT id, archived_at, archived_reason FROM tags WHERE id IN ({})".format(
                    ",".join("?" * len(ids))
                ),
                ids,
            ).fetchall()
            assert len(rows) == len(ids)
            for row in rows:
                assert row["archived_at"] is None, (
                    f"tag id={row['id']} が 0059 適用だけで archived_at を "
                    "書き換えられている（データ移行を含まない前提に反する）"
                )
                assert row["archived_reason"] is None
        finally:
            conn.close()
