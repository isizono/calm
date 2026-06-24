"""migration 0048_session_identity のテスト

0048 適用後に session_identity テーブルが作成され、
decisions / discussion_logs / discussion_topics / activities / materials に
caller_session_id 列が追加されることを確認する。
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
    """全 migration（0048 含む）を適用済みのテスト用 DB を提供する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def db_before_0048():
    """0047 までの migration を適用した DB を提供する。0048 の挙動を分離検証するために使う。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path

        parsed = parse_uri(f"sqlite:///{db_path}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        backend.init_database()
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        pre_0048 = MigrationList([m for m in all_migs if m.id < "0048"])
        with backend.lock():
            backend.apply_migrations(pre_0048)

        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


def _apply_migration_0048(db_path: str) -> None:
    """db_path に対して migration 0048 のみを適用する。"""
    parsed = parse_uri(f"sqlite:///{db_path}")
    backend = _VecSQLiteBackend(parsed, default_migration_table)
    all_migs = read_migrations(str(MIGRATIONS_DIR))
    only_0048 = MigrationList([m for m in all_migs if m.id.startswith("0048")])
    with backend.lock():
        backend.apply_migrations(only_0048)


def _get_column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    """指定テーブルのカラム名セットを返す。"""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """指定テーブルが sqlite_master に存在するか確認する。"""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _index_names(conn: sqlite3.Connection, name_pattern: str) -> set[str]:
    """sqlite_master から name LIKE pattern のインデックス名セットを返す。"""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE ?",
        (name_pattern,),
    ).fetchall()
    return {row["name"] for row in rows}


class TestSessionIdentityTableCreated:
    """0048 適用後に session_identity テーブルが作成されることの確認"""

    def test_session_identity_table_exists_after_0048(self, migrated_db):
        """migration 0048 適用後、session_identity テーブルが存在する"""
        conn = get_connection()
        try:
            assert _table_exists(conn, "session_identity"), (
                "session_identity テーブルが 0048 適用後に存在しない"
            )
        finally:
            conn.close()

    def test_session_identity_table_not_exists_before_0048(self, db_before_0048):
        """0048 適用前は session_identity テーブルが存在しない（前提確認）"""
        conn = get_connection()
        try:
            assert not _table_exists(conn, "session_identity"), (
                "0048 適用前に session_identity テーブルが既に存在している"
            )
        finally:
            conn.close()

    def test_session_identity_required_columns_exist(self, migrated_db):
        """0048 適用後、session_identity テーブルに必須カラムが全部存在する"""
        conn = get_connection()
        try:
            column_names = _get_column_names(conn, "session_identity")
            required = {
                "session_id",
                "role",
                "handle",
                "topic_id",
                "parent_session_id",
                "spawned_at",
                "last_heartbeat",
                "ended_at",
            }
            for col in required:
                assert col in column_names, (
                    f"session_identity.{col} が 0048 適用後に存在しない"
                )
        finally:
            conn.close()

    def test_session_identity_indexes_created(self, migrated_db):
        """0048 適用後、session_identity の 3 つのインデックスが作成されている"""
        conn = get_connection()
        try:
            idx_names = _index_names(conn, "idx_session_identity_%")
            expected = {
                "idx_session_identity_role",
                "idx_session_identity_handle",
                "idx_session_identity_ended",
            }
            for idx in expected:
                assert idx in idx_names, (
                    f"インデックス {idx} が 0048 適用後に存在しない"
                )
        finally:
            conn.close()


class TestCallerSessionIdColumnsAdded:
    """0048 適用後に caller_session_id カラムが 5 テーブルに追加されることの確認"""

    TABLES = [
        "decisions",
        "discussion_logs",
        "discussion_topics",
        "activities",
        "materials",
    ]

    def test_caller_session_id_exists_in_all_tables_after_0048(self, migrated_db):
        """0048 適用後、5 テーブルすべてに caller_session_id カラムが存在する"""
        conn = get_connection()
        try:
            for table in self.TABLES:
                col_names = _get_column_names(conn, table)
                assert "caller_session_id" in col_names, (
                    f"{table}.caller_session_id が 0048 適用後に存在しない"
                )
        finally:
            conn.close()

    def test_caller_session_id_not_exists_before_0048(self, db_before_0048):
        """0048 適用前は 5 テーブルいずれにも caller_session_id カラムが存在しない"""
        conn = get_connection()
        try:
            for table in self.TABLES:
                col_names = _get_column_names(conn, table)
                assert "caller_session_id" not in col_names, (
                    f"0048 適用前に {table}.caller_session_id が既に存在している"
                )
        finally:
            conn.close()

    def test_caller_session_id_is_nullable(self, migrated_db):
        """0048 適用後、caller_session_id を指定せずに INSERT できる（NULL 許容確認）"""
        conn = get_connection()
        try:
            # activities は caller_session_id なしで INSERT できる
            cur = conn.execute(
                "INSERT INTO activities (title, description, status) VALUES (?, ?, ?)",
                ("テスト activity", "desc", "pending"),
            )
            conn.commit()
            row = conn.execute(
                "SELECT caller_session_id FROM activities WHERE id = ?",
                (cur.lastrowid,),
            ).fetchone()
            assert row is not None
            assert row["caller_session_id"] is None, (
                "caller_session_id 未指定の場合は NULL であるべき"
            )
        finally:
            conn.close()


class TestExistingRowsPreserved:
    """0048 適用前に挿入した行が、適用後も破壊されないことの確認"""

    def test_existing_activities_preserved(self, db_before_0048):
        """0048 適用前の activities 行は、適用後に caller_session_id が NULL のまま他カラムを保持する"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO activities (title, description, status) VALUES (?, ?, ?)",
                ("既存 activity", "既存の説明", "in_progress"),
            )
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0048(db_before_0048)

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT title, description, status, caller_session_id "
                "FROM activities WHERE title = ?",
                ("既存 activity",),
            ).fetchone()
            assert row is not None
            assert row["title"] == "既存 activity"
            assert row["description"] == "既存の説明"
            assert row["status"] == "in_progress"
            assert row["caller_session_id"] is None, (
                "既存行の caller_session_id は NULL であるべき"
            )
        finally:
            conn.close()

    def test_existing_decisions_preserved(self, db_before_0048):
        """0048 適用前の decisions 行は、適用後に caller_session_id が NULL のまま他カラムを保持する"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO decisions (decision, reason) VALUES (?, ?)",
                ("既存の決定", "既存の理由"),
            )
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0048(db_before_0048)

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT decision, reason, caller_session_id FROM decisions WHERE decision = ?",
                ("既存の決定",),
            ).fetchone()
            assert row is not None
            assert row["decision"] == "既存の決定"
            assert row["reason"] == "既存の理由"
            assert row["caller_session_id"] is None, (
                "既存行の decisions.caller_session_id は NULL であるべき"
            )
        finally:
            conn.close()

    def test_existing_discussion_logs_preserved(self, db_before_0048):
        """0048 適用前の discussion_logs 行は、適用後に caller_session_id が NULL のまま他カラムを保持する"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO discussion_logs (title, content) VALUES (?, ?)",
                ("既存ログタイトル", "既存のログ内容"),
            )
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0048(db_before_0048)

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT title, content, caller_session_id "
                "FROM discussion_logs WHERE title = ?",
                ("既存ログタイトル",),
            ).fetchone()
            assert row is not None
            assert row["title"] == "既存ログタイトル"
            assert row["content"] == "既存のログ内容"
            assert row["caller_session_id"] is None, (
                "既存行の discussion_logs.caller_session_id は NULL であるべき"
            )
        finally:
            conn.close()

    def test_existing_discussion_topics_preserved(self, db_before_0048):
        """0048 適用前の discussion_topics 行は、適用後に caller_session_id が NULL のまま他カラムを保持する"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO discussion_topics (title, description) VALUES (?, ?)",
                ("既存トピック", "既存の説明"),
            )
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0048(db_before_0048)

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT title, description, caller_session_id "
                "FROM discussion_topics WHERE title = ?",
                ("既存トピック",),
            ).fetchone()
            assert row is not None
            assert row["title"] == "既存トピック"
            assert row["description"] == "既存の説明"
            assert row["caller_session_id"] is None, (
                "既存行の discussion_topics.caller_session_id は NULL であるべき"
            )
        finally:
            conn.close()

    def test_existing_materials_preserved(self, db_before_0048):
        """0048 適用前の materials 行は、適用後に caller_session_id が NULL のまま他カラムを保持する"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO materials (title, content) VALUES (?, ?)",
                ("既存マテリアル", "既存の内容"),
            )
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0048(db_before_0048)

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT title, content, caller_session_id "
                "FROM materials WHERE title = ?",
                ("既存マテリアル",),
            ).fetchone()
            assert row is not None
            assert row["title"] == "既存マテリアル"
            assert row["content"] == "既存の内容"
            assert row["caller_session_id"] is None, (
                "既存行の materials.caller_session_id は NULL であるべき"
            )
        finally:
            conn.close()


class TestSessionIdentityCRUD:
    """session_identity テーブルの CRUD 動作確認"""

    def test_insert_and_select_session_identity(self, migrated_db):
        """session_identity に INSERT して SELECT で読み取れる"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO session_identity (session_id, role) VALUES (?, ?)",
                ("sess-001", "worker"),
            )
            conn.commit()
            row = conn.execute(
                "SELECT session_id, role, handle, topic_id, parent_session_id, "
                "spawned_at, last_heartbeat, ended_at "
                "FROM session_identity WHERE session_id = ?",
                ("sess-001",),
            ).fetchone()
            assert row is not None
            assert row["session_id"] == "sess-001"
            assert row["role"] == "worker"
            assert row["handle"] is None
            assert row["topic_id"] is None
            assert row["parent_session_id"] is None
            assert row["ended_at"] is None
            assert row["spawned_at"] is not None
            assert row["last_heartbeat"] is not None
        finally:
            conn.close()

    def test_insert_full_session_identity(self, migrated_db):
        """session_identity に全カラムを指定して INSERT できる"""
        conn = get_connection()
        try:
            # topic を先に作成
            cur = conn.execute(
                "INSERT INTO discussion_topics (title, description) VALUES (?, ?)",
                ("テストトピック", "テスト"),
            )
            topic_id = cur.lastrowid
            conn.commit()

            conn.execute(
                "INSERT INTO session_identity "
                "(session_id, role, handle, topic_id, parent_session_id) "
                "VALUES (?, ?, ?, ?, ?)",
                ("sess-002", "orch", "my-orch", topic_id, "sess-parent-001"),
            )
            conn.commit()
            row = conn.execute(
                "SELECT role, handle, topic_id, parent_session_id "
                "FROM session_identity WHERE session_id = ?",
                ("sess-002",),
            ).fetchone()
            assert row is not None
            assert row["role"] == "orch"
            assert row["handle"] == "my-orch"
            assert row["topic_id"] == topic_id
            assert row["parent_session_id"] == "sess-parent-001"
        finally:
            conn.close()

    def test_duplicate_handle_allowed(self, migrated_db):
        """handle に重複値を持つ複数行を INSERT できる（UNIQUE 制約がないことの確認）"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO session_identity (session_id, role, handle) VALUES (?, ?, ?)",
                ("sess-003", "worker", "shared-handle"),
            )
            conn.execute(
                "INSERT INTO session_identity (session_id, role, handle) VALUES (?, ?, ?)",
                ("sess-004", "worker", "shared-handle"),
            )
            conn.commit()
            rows = conn.execute(
                "SELECT session_id FROM session_identity WHERE handle = ?",
                ("shared-handle",),
            ).fetchall()
            assert len(rows) == 2, (
                "同名 handle を持つ複数行が存在できるべき（UNIQUE 制約がないことの確認）"
            )
        finally:
            conn.close()

    def test_parent_session_id_relaxed_fk(self, migrated_db):
        """parent_session_id に存在しない session_id を入れられる（relax FK の確認）"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO session_identity (session_id, role, parent_session_id) "
                "VALUES (?, ?, ?)",
                ("sess-005", "worker", "nonexistent-parent"),
            )
            conn.commit()
            row = conn.execute(
                "SELECT parent_session_id FROM session_identity WHERE session_id = ?",
                ("sess-005",),
            ).fetchone()
            assert row is not None
            assert row["parent_session_id"] == "nonexistent-parent", (
                "存在しない parent_session_id を持つ行を INSERT できるべき"
            )
        finally:
            conn.close()

    def test_topic_id_fk_enforced(self, migrated_db):
        """topic_id に存在しない discussion_topics.id を入れると IntegrityError になる
        （parent_session_id の relax FK との対照: REFERENCES が機能していることの確認）"""
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO session_identity (session_id, role, topic_id) "
                    "VALUES (?, ?, ?)",
                    ("sess-006", "worker", 999999),
                )
                conn.commit()
        finally:
            conn.close()

    def test_role_not_null_enforced(self, migrated_db):
        """role に NULL を指定して INSERT すると IntegrityError になる（NOT NULL 制約の確認）"""
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO session_identity (session_id, role) VALUES (?, ?)",
                    ("sess-007", None),
                )
                conn.commit()
        finally:
            conn.close()

    def test_session_id_primary_key_duplicate_rejected(self, migrated_db):
        """同じ session_id を持つ行を 2 回 INSERT すると IntegrityError になる（PRIMARY KEY の確認）"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO session_identity (session_id, role) VALUES (?, ?)",
                ("sess-008", "worker"),
            )
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO session_identity (session_id, role) VALUES (?, ?)",
                    ("sess-008", "orch"),
                )
                conn.commit()
        finally:
            conn.close()
