"""migration 0045_add_activities_orch_managed のテスト

0045適用後に activities テーブルへ orch_managed 列が追加され、
既存の素タグ "orch-managed" を持つ activity の orch_managed が 1 でバックフィルされることを確認する。
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
    """全migration（0045含む）を適用済みのテスト用DBを提供する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def db_before_0045():
    """0044までのmigrationを適用したDBを提供する。0045の挙動を分離検証するために使う。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path

        parsed = parse_uri(f"sqlite:///{db_path}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        backend.init_database()
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        pre_0045 = MigrationList([m for m in all_migs if m.id < "0045"])
        with backend.lock():
            backend.apply_migrations(pre_0045)

        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


def _apply_migration_0045(db_path: str) -> None:
    """db_pathに対してmigration 0045のみを適用する。"""
    parsed = parse_uri(f"sqlite:///{db_path}")
    backend = _VecSQLiteBackend(parsed, default_migration_table)
    all_migs = read_migrations(str(MIGRATIONS_DIR))
    only_0045 = MigrationList(
        [m for m in all_migs if m.id.startswith("0045")]
    )
    with backend.lock():
        backend.apply_migrations(only_0045)


def _get_column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    """指定テーブルのカラム名セットを返す。"""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


def _insert_activity(conn: sqlite3.Connection, title: str) -> int:
    """activitiesに1行INSERTしてidを返す。"""
    cur = conn.execute(
        "INSERT INTO activities (title, description, status) VALUES (?, ?, ?)",
        (title, "desc", "pending"),
    )
    return cur.lastrowid


def _attach_orch_managed_tag(conn: sqlite3.Connection, activity_id: int) -> None:
    """素タグ 'orch-managed' を activity_id にリンクする。"""
    cur = conn.execute(
        "INSERT OR IGNORE INTO tags (namespace, name) VALUES (?, ?)",
        ("", "orch-managed"),
    )
    tag_id = conn.execute(
        "SELECT id FROM tags WHERE namespace = ? AND name = ?",
        ("", "orch-managed"),
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO activity_tags (activity_id, tag_id) VALUES (?, ?)",
        (activity_id, tag_id),
    )


class TestOrchManagedColumnAdded:
    """0045適用後に orch_managed 列が追加されていることの確認"""

    def test_activities_has_orch_managed_column_after_0045(self, migrated_db):
        """migration 0045 適用後、activities テーブルに orch_managed 列が存在する"""
        conn = get_connection()
        try:
            column_names = _get_column_names(conn, "activities")
            assert "orch_managed" in column_names, (
                "activities.orch_managed が 0045 適用後に存在しない"
            )
        finally:
            conn.close()

    def test_activities_has_no_orch_managed_column_before_0045(self, db_before_0045):
        """0044 適用時点では activities に orch_managed 列が存在しない（前提確認）"""
        conn = get_connection()
        try:
            assert "orch_managed" not in _get_column_names(conn, "activities"), (
                "0045 適用前の activities に orch_managed 列が既に存在している"
            )
        finally:
            conn.close()

    def test_default_false_for_new_rows(self, migrated_db):
        """0045 適用後、orch_managed を指定せず INSERT した行は 0 になる"""
        conn = get_connection()
        try:
            cur = conn.execute(
                "INSERT INTO activities (title, description, status) "
                "VALUES (?, ?, ?)",
                ("通常 activity", "desc", "pending"),
            )
            conn.commit()
            row = conn.execute(
                "SELECT orch_managed FROM activities WHERE id = ?",
                (cur.lastrowid,),
            ).fetchone()
            assert row["orch_managed"] == 0
        finally:
            conn.close()

    def test_insert_with_orch_managed_true(self, migrated_db):
        """0045 適用後、orch_managed=1 を明示 INSERT できる"""
        conn = get_connection()
        try:
            cur = conn.execute(
                "INSERT INTO activities (title, description, status, orch_managed) "
                "VALUES (?, ?, ?, ?)",
                ("orch activity", "desc", "pending", 1),
            )
            conn.commit()
            row = conn.execute(
                "SELECT orch_managed FROM activities WHERE id = ?",
                (cur.lastrowid,),
            ).fetchone()
            assert row["orch_managed"] == 1
        finally:
            conn.close()


class TestBackfillFromTag:
    """0045 適用時の既存 orch-managed タグからのバックフィル確認"""

    def test_existing_tagged_activity_is_marked_true(self, db_before_0045):
        """0044 時点で orch-managed タグを持つ activity は、0045 適用後 orch_managed=1 になる"""
        conn = get_connection()
        try:
            tagged_id = _insert_activity(conn, "orch-managed 付与済 activity")
            _attach_orch_managed_tag(conn, tagged_id)
            untagged_id = _insert_activity(conn, "通常 activity")
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0045(db_before_0045)

        conn = get_connection()
        try:
            row_tagged = conn.execute(
                "SELECT orch_managed FROM activities WHERE id = ?",
                (tagged_id,),
            ).fetchone()
            row_untagged = conn.execute(
                "SELECT orch_managed FROM activities WHERE id = ?",
                (untagged_id,),
            ).fetchone()
            assert row_tagged["orch_managed"] == 1, (
                "orch-managed タグ付き activity が 0045 後に orch_managed=1 になっていない"
            )
            assert row_untagged["orch_managed"] == 0, (
                "タグなし activity が誤って orch_managed=1 に設定されている"
            )
        finally:
            conn.close()

    def test_namespaced_orch_managed_tag_is_not_picked_up(self, db_before_0045):
        """namespace 付きの :orch-managed タグ（例: foo:orch-managed）は対象外（素タグのみ）"""
        conn = get_connection()
        try:
            aid = _insert_activity(conn, "namespaced tag activity")
            cur = conn.execute(
                "INSERT OR IGNORE INTO tags (namespace, name) VALUES (?, ?)",
                ("foo", "orch-managed"),
            )
            tag_id = conn.execute(
                "SELECT id FROM tags WHERE namespace = ? AND name = ?",
                ("foo", "orch-managed"),
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO activity_tags (activity_id, tag_id) VALUES (?, ?)",
                (aid, tag_id),
            )
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0045(db_before_0045)

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT orch_managed FROM activities WHERE id = ?",
                (aid,),
            ).fetchone()
            assert row["orch_managed"] == 0, (
                "namespace 付きタグが誤ってバックフィルの対象になっている"
            )
        finally:
            conn.close()
