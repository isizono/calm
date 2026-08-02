"""migration 0068_add_asks_kind_and_tags のテスト

0068 適用後に asks.kind カラムと ask_tags 中間テーブルが期待通り存在し、
CHECK制約・デフォルト値・外部キーが機能することを、ask_service を経由せず
生SQLで検証する。
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
    """全migration（0068含む）を適用済みのテスト用DBを提供する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def db_before_0068():
    """0067までのmigrationを適用したDBを提供する。0068の挙動を分離検証するために使う。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path

        parsed = parse_uri(f"sqlite:///{db_path}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        backend.init_database()
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        pre_0068 = MigrationList([m for m in all_migs if m.id < "0068"])
        with backend.lock():
            backend.apply_migrations(pre_0068)

        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


def _insert_ask(conn: sqlite3.Connection, **overrides) -> int:
    fields = {
        "question": "should we do X?",
        "fingerprint": "deadbeefdeadbeef",
    }
    fields.update(overrides)
    columns = ", ".join(fields.keys())
    placeholders = ", ".join("?" for _ in fields)
    cur = conn.execute(
        f"INSERT INTO asks ({columns}) VALUES ({placeholders})",
        tuple(fields.values()),
    )
    return cur.lastrowid


def _insert_tag(conn: sqlite3.Connection, namespace: str = "domain", name: str = "test") -> int:
    cur = conn.execute(
        "INSERT INTO tags (namespace, name) VALUES (?, ?)",
        (namespace, name),
    )
    return cur.lastrowid


class TestKindColumn:
    def test_kind_does_not_exist_before_0068(self, db_before_0068):
        conn = get_connection()
        try:
            columns = get_column_names(conn, "asks")
        finally:
            conn.close()
        assert "kind" not in columns

    def test_kind_defaults_to_ask(self, migrated_db):
        conn = get_connection()
        try:
            ask_id = _insert_ask(conn)
            conn.commit()
            row = conn.execute("SELECT kind FROM asks WHERE id = ?", (ask_id,)).fetchone()
        finally:
            conn.close()
        assert row["kind"] == "ask"

    def test_kind_meta_accepted(self, migrated_db):
        conn = get_connection()
        try:
            ask_id = _insert_ask(conn, fingerprint="meta000000000000", kind="meta")
            conn.commit()
            row = conn.execute("SELECT kind FROM asks WHERE id = ?", (ask_id,)).fetchone()
        finally:
            conn.close()
        assert row["kind"] == "meta"

    def test_invalid_kind_rejected(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_ask(conn, kind="not_a_kind")
        finally:
            conn.rollback()
            conn.close()

    def test_existing_rows_backfilled_to_default_kind(self, db_before_0068):
        """0067までのDBに事前投入したaskが、0068適用後もkind='ask'で埋まることを確認する
        （遡及的なタグ付与・kind設定は行わない設計だが、デフォルト値での自動充足は起きる）。"""
        conn = get_connection()
        try:
            ask_id = _insert_ask(conn, fingerprint="pre0068ask000000")
            conn.commit()
        finally:
            conn.close()

        # 残りのmigration（0068含む）を適用
        parsed = parse_uri(f"sqlite:///{os.environ['DISCUSSION_DB_PATH']}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        remaining = MigrationList([m for m in all_migs if m.id >= "0068"])
        with backend.lock():
            backend.apply_migrations(remaining)

        conn = get_connection()
        try:
            row = conn.execute("SELECT kind FROM asks WHERE id = ?", (ask_id,)).fetchone()
        finally:
            conn.close()
        assert row["kind"] == "ask"


class TestAskTagsTable:
    def test_ask_tags_does_not_exist_before_0068(self, db_before_0068):
        conn = get_connection()
        try:
            assert not table_exists(conn, "ask_tags")
        finally:
            conn.close()

    def test_ask_tags_exists_after_0068(self, migrated_db):
        conn = get_connection()
        try:
            assert table_exists(conn, "ask_tags")
        finally:
            conn.close()

    def test_expected_columns(self, migrated_db):
        conn = get_connection()
        try:
            columns = get_column_names(conn, "ask_tags")
        finally:
            conn.close()
        assert {"ask_id", "tag_id"} <= columns

    def test_expected_index(self, migrated_db):
        conn = get_connection()
        try:
            names = index_names(conn, "idx_ask_tags%")
        finally:
            conn.close()
        assert "idx_ask_tags_tag" in names

    def test_link_and_lookup(self, migrated_db):
        conn = get_connection()
        try:
            ask_id = _insert_ask(conn)
            tag_id = _insert_tag(conn)
            conn.execute(
                "INSERT INTO ask_tags (ask_id, tag_id) VALUES (?, ?)",
                (ask_id, tag_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT tag_id FROM ask_tags WHERE ask_id = ?", (ask_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row["tag_id"] == tag_id

    def test_duplicate_link_rejected(self, migrated_db):
        conn = get_connection()
        try:
            ask_id = _insert_ask(conn)
            tag_id = _insert_tag(conn)
            conn.execute(
                "INSERT INTO ask_tags (ask_id, tag_id) VALUES (?, ?)",
                (ask_id, tag_id),
            )
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO ask_tags (ask_id, tag_id) VALUES (?, ?)",
                    (ask_id, tag_id),
                )
        finally:
            conn.rollback()
            conn.close()

    def test_cascade_deletes_on_ask_delete(self, migrated_db):
        conn = get_connection()
        try:
            ask_id = _insert_ask(conn)
            tag_id = _insert_tag(conn)
            conn.execute(
                "INSERT INTO ask_tags (ask_id, tag_id) VALUES (?, ?)",
                (ask_id, tag_id),
            )
            conn.commit()

            conn.execute("DELETE FROM asks WHERE id = ?", (ask_id,))
            conn.commit()

            remaining = conn.execute(
                "SELECT COUNT(*) FROM ask_tags WHERE ask_id = ?", (ask_id,)
            ).fetchone()[0]
        finally:
            conn.close()
        assert remaining == 0

    def test_cascade_deletes_on_tag_delete(self, migrated_db):
        conn = get_connection()
        try:
            ask_id = _insert_ask(conn)
            tag_id = _insert_tag(conn)
            conn.execute(
                "INSERT INTO ask_tags (ask_id, tag_id) VALUES (?, ?)",
                (ask_id, tag_id),
            )
            conn.commit()

            conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
            conn.commit()

            remaining = conn.execute(
                "SELECT COUNT(*) FROM ask_tags WHERE tag_id = ?", (tag_id,)
            ).fetchone()[0]
        finally:
            conn.close()
        assert remaining == 0

    def test_references_nonexistent_ask_rejected(self, migrated_db):
        conn = get_connection()
        try:
            tag_id = _insert_tag(conn)
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO ask_tags (ask_id, tag_id) VALUES (?, ?)",
                    (999999, tag_id),
                )
        finally:
            conn.rollback()
            conn.close()

    def test_references_nonexistent_tag_rejected(self, migrated_db):
        conn = get_connection()
        try:
            ask_id = _insert_ask(conn)
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO ask_tags (ask_id, tag_id) VALUES (?, ?)",
                    (ask_id, 999999),
                )
        finally:
            conn.rollback()
            conn.close()
