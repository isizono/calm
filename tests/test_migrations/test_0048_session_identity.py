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
from test_migrations.conftest import get_column_names, index_names, table_exists


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


class TestSessionIdentityTableCreated:
    """0048 適用後に session_identity テーブルが作成されることの確認"""

    def test_session_identity_table_exists_after_0048(self, migrated_db):
        """migration 0048 適用後、session_identity テーブルが存在する"""
        conn = get_connection()
        try:
            assert table_exists(conn, "session_identity"), (
                "session_identity テーブルが 0048 適用後に存在しない"
            )
        finally:
            conn.close()

    def test_session_identity_table_not_exists_before_0048(self, db_before_0048):
        """0048 適用前は session_identity テーブルが存在しない（前提確認）"""
        conn = get_connection()
        try:
            assert not table_exists(conn, "session_identity"), (
                "0048 適用前に session_identity テーブルが既に存在している"
            )
        finally:
            conn.close()

    def test_session_identity_required_columns_exist(self, migrated_db):
        """0048 適用後、session_identity テーブルに必須カラムが全部存在する"""
        conn = get_connection()
        try:
            column_names = get_column_names(conn, "session_identity")
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
            idx_names = index_names(conn, "idx_session_identity_%")
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
                col_names = get_column_names(conn, table)
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
                col_names = get_column_names(conn, table)
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

    @pytest.mark.parametrize(
        "table,insert_sql,params,where_col,where_val,expected",
        [
            (
                "activities",
                "INSERT INTO activities (title, description, status) VALUES (?, ?, ?)",
                ("既存 activity", "既存の説明", "in_progress"),
                "title",
                "既存 activity",
                {"title": "既存 activity", "description": "既存の説明", "status": "in_progress"},
            ),
            (
                "decisions",
                "INSERT INTO decisions (decision, reason) VALUES (?, ?)",
                ("既存の決定", "既存の理由"),
                "decision",
                "既存の決定",
                {"decision": "既存の決定", "reason": "既存の理由"},
            ),
            (
                "discussion_logs",
                "INSERT INTO discussion_logs (title, content) VALUES (?, ?)",
                ("既存ログタイトル", "既存のログ内容"),
                "title",
                "既存ログタイトル",
                {"title": "既存ログタイトル", "content": "既存のログ内容"},
            ),
            (
                "discussion_topics",
                "INSERT INTO discussion_topics (title, description) VALUES (?, ?)",
                ("既存トピック", "既存の説明"),
                "title",
                "既存トピック",
                {"title": "既存トピック", "description": "既存の説明"},
            ),
            (
                "materials",
                "INSERT INTO materials (title, content) VALUES (?, ?)",
                ("既存マテリアル", "既存の内容"),
                "title",
                "既存マテリアル",
                {"title": "既存マテリアル", "content": "既存の内容"},
            ),
        ],
    )
    def test_existing_rows_preserved(
        self, db_before_0048, table, insert_sql, params, where_col, where_val, expected
    ):
        """0048 適用前の各テーブルの行は、適用後に caller_session_id が NULL のまま他カラムを保持する"""
        conn = get_connection()
        try:
            conn.execute(insert_sql, params)
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0048(db_before_0048)

        select_cols = ", ".join([*expected.keys(), "caller_session_id"])
        conn = get_connection()
        try:
            row = conn.execute(
                f"SELECT {select_cols} FROM {table} WHERE {where_col} = ?",
                (where_val,),
            ).fetchone()
            assert row is not None
            for col, value in expected.items():
                assert row[col] == value, (
                    f"既存行の {table}.{col} が保持されていない"
                )
            assert row["caller_session_id"] is None, (
                f"既存行の {table}.caller_session_id は NULL であるべき"
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
            # PRAGMA foreign_keys = ON が get_connection() で設定されることを前提とする
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO session_identity (session_id, role, topic_id) "
                    "VALUES (?, ?, ?)",
                    ("sess-006", "worker", 999999),
                )
                conn.commit()
        finally:
            conn.close()

    def test_topic_id_set_null_on_topic_delete(self, migrated_db):
        """参照先 discussion_topics 行を削除すると session_identity.topic_id が NULL になる
        （ON DELETE SET NULL の確認）"""
        conn = get_connection()
        try:
            # PRAGMA foreign_keys = ON が get_connection() で設定されることを前提とする
            cur = conn.execute(
                "INSERT INTO discussion_topics (title, description) VALUES (?, ?)",
                ("削除対象トピック", "テスト"),
            )
            topic_id = cur.lastrowid
            conn.execute(
                "INSERT INTO session_identity (session_id, role, topic_id) VALUES (?, ?, ?)",
                ("sess-del-001", "worker", topic_id),
            )
            conn.commit()

            conn.execute("DELETE FROM discussion_topics WHERE id = ?", (topic_id,))
            conn.commit()

            row = conn.execute(
                "SELECT topic_id FROM session_identity WHERE session_id = ?",
                ("sess-del-001",),
            ).fetchone()
            assert row is not None, (
                "topic 削除後も session_identity 行自体は残るべき（ON DELETE SET NULL）"
            )
            assert row["topic_id"] is None, (
                "参照先 topic 削除後は topic_id が NULL になるべき"
            )
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
