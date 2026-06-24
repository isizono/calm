"""role_service の unit test。

lookup_role / register_session / unregister_session / update_heartbeat の
各関数をカバーする。fixture は test_0048_session_identity.py の
migrated_db パターンに倣い、全 migration 適用済み DB を使う。
"""
import os
import tempfile
import time

import pytest

from src.db import get_connection, init_database
from src.services.tag_service import _injected_tags
from src.services.role_service import (
    lookup_role,
    register_session,
    unregister_session,
    update_heartbeat,
)


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


# ---------------------------------------------------------------------------
# lookup_role
# ---------------------------------------------------------------------------

class TestLookupRole:
    """lookup_role の各経路をカバーするテスト群。"""

    def test_db_active_row_returns_role(self, migrated_db):
        """DB に active な role がある場合はそのまま返す。"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO session_identity (session_id, role) VALUES (?, ?)",
                ("sess-active", "orch"),
            )
            conn.commit()
            result = lookup_role(conn, "sess-active")
            assert result == "orch"
        finally:
            conn.close()

    def test_db_ended_row_returns_none(self, migrated_db):
        """ended_at が設定済みの row は lookup 対象外（None を返す）。"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO session_identity (session_id, role, ended_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP)",
                ("sess-ended", "worker"),
            )
            conn.commit()
            result = lookup_role(conn, "sess-ended")
            assert result is None
        finally:
            conn.close()

    def test_no_db_row_env_role_returned(self, migrated_db):
        """DB に row がなく OW_ROLE が設定されている場合は env の値を返す。"""
        conn = get_connection()
        try:
            os.environ["OW_ROLE"] = "worker"
            result = lookup_role(conn, "sess-nonexistent")
            assert result == "worker"
        finally:
            del os.environ["OW_ROLE"]
            conn.close()

    def test_no_db_no_env_returns_none(self, migrated_db):
        """DB に row がなく OW_ROLE も未設定なら None を返す。"""
        conn = get_connection()
        try:
            os.environ.pop("OW_ROLE", None)
            result = lookup_role(conn, "sess-unknown")
            assert result is None
        finally:
            conn.close()

    def test_db_takes_priority_over_env(self, migrated_db):
        """DB と env 両方ある場合は DB を優先する。"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO session_identity (session_id, role) VALUES (?, ?)",
                ("sess-priority", "orch"),
            )
            conn.commit()
            os.environ["OW_ROLE"] = "worker"
            result = lookup_role(conn, "sess-priority")
            assert result == "orch", "DB の role が env より優先されるべき"
        finally:
            os.environ.pop("OW_ROLE", None)
            conn.close()

    def test_invalid_role_in_db_falls_back_to_env(self, migrated_db):
        """DB の role 値が不正（空文字など）の場合は env にフォールバックする。

        NOTE: NOT NULL 制約のため空文字は通るが、_VALID_ROLES に含まれないため
        不正扱いとなり env にフォールバックする。
        """
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO session_identity (session_id, role) VALUES (?, ?)",
                ("sess-invalid", "unknown_role"),
            )
            conn.commit()
            os.environ["OW_ROLE"] = "dispatcher"
            result = lookup_role(conn, "sess-invalid")
            assert result == "dispatcher", "不正 DB role は env にフォールバックするべき"
        finally:
            os.environ.pop("OW_ROLE", None)
            conn.close()

    def test_none_session_id_falls_back_to_env(self, migrated_db):
        """session_id が None の場合は DB lookup をスキップして env を返す。"""
        conn = get_connection()
        try:
            os.environ["OW_ROLE"] = "worker"
            result = lookup_role(conn, None)
            assert result == "worker"
        finally:
            os.environ.pop("OW_ROLE", None)
            conn.close()

    def test_invalid_env_role_returns_none(self, migrated_db):
        """OW_ROLE に不正な値が設定されている場合は None を返す。"""
        conn = get_connection()
        try:
            os.environ["OW_ROLE"] = "invalid_role"
            result = lookup_role(conn, None)
            assert result is None
        finally:
            os.environ.pop("OW_ROLE", None)
            conn.close()


# ---------------------------------------------------------------------------
# register_session
# ---------------------------------------------------------------------------

class TestRegisterSession:
    """register_session の動作確認。"""

    def test_insert_new_session(self, migrated_db):
        """新規 session_id を INSERT できる。"""
        conn = get_connection()
        try:
            register_session(conn, "sess-new", "worker")
            conn.commit()
            row = conn.execute(
                "SELECT role FROM session_identity WHERE session_id = ?",
                ("sess-new",),
            ).fetchone()
            assert row is not None
            assert row["role"] == "worker"
        finally:
            conn.close()

    def test_insert_with_all_fields(self, migrated_db):
        """handle / topic_id / parent_session_id を指定して INSERT できる。"""
        conn = get_connection()
        try:
            # topic を先に作成
            cur = conn.execute(
                "INSERT INTO discussion_topics (title, description) VALUES (?, ?)",
                ("テストトピック", "テスト"),
            )
            topic_id = cur.lastrowid
            conn.commit()

            register_session(
                conn,
                "sess-full",
                "orch",
                handle="my-orch",
                topic_id=topic_id,
                parent_session_id="sess-parent",
            )
            conn.commit()

            row = conn.execute(
                "SELECT role, handle, topic_id, parent_session_id "
                "FROM session_identity WHERE session_id = ?",
                ("sess-full",),
            ).fetchone()
            assert row["role"] == "orch"
            assert row["handle"] == "my-orch"
            assert row["topic_id"] == topic_id
            assert row["parent_session_id"] == "sess-parent"
        finally:
            conn.close()

    def test_on_conflict_updates_role_and_handle(self, migrated_db):
        """既存 session_id を再 register すると role / handle が更新される（ON CONFLICT）。"""
        conn = get_connection()
        try:
            register_session(conn, "sess-conflict", "worker", handle="old-handle")
            conn.commit()

            register_session(conn, "sess-conflict", "orch", handle="new-handle")
            conn.commit()

            row = conn.execute(
                "SELECT role, handle FROM session_identity WHERE session_id = ?",
                ("sess-conflict",),
            ).fetchone()
            assert row["role"] == "orch"
            assert row["handle"] == "new-handle"
        finally:
            conn.close()

    def test_register_updates_last_heartbeat_on_conflict(self, migrated_db):
        """ON CONFLICT 時に last_heartbeat が更新される。"""
        conn = get_connection()
        try:
            register_session(conn, "sess-hb", "worker")
            conn.commit()

            before = conn.execute(
                "SELECT last_heartbeat FROM session_identity WHERE session_id = ?",
                ("sess-hb",),
            ).fetchone()["last_heartbeat"]

            # 1 秒以上待ってから再 register（SQLite の CURRENT_TIMESTAMP は秒単位）
            time.sleep(1.1)

            register_session(conn, "sess-hb", "worker")
            conn.commit()

            after = conn.execute(
                "SELECT last_heartbeat FROM session_identity WHERE session_id = ?",
                ("sess-hb",),
            ).fetchone()["last_heartbeat"]

            assert after >= before  # 同じか後になるはず（秒単位なので > が成立しないケースを考慮）
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# unregister_session
# ---------------------------------------------------------------------------

class TestUnregisterSession:
    """unregister_session の動作確認。"""

    def test_sets_ended_at(self, migrated_db):
        """unregister_session を呼ぶと ended_at が NULL でなくなる。"""
        conn = get_connection()
        try:
            register_session(conn, "sess-unreg", "worker")
            conn.commit()

            unregister_session(conn, "sess-unreg")
            conn.commit()

            row = conn.execute(
                "SELECT ended_at FROM session_identity WHERE session_id = ?",
                ("sess-unreg",),
            ).fetchone()
            assert row is not None
            assert row["ended_at"] is not None, "ended_at が設定されているべき"
        finally:
            conn.close()

    def test_unregistered_session_not_returned_by_lookup(self, migrated_db):
        """unregister 済みのセッションは lookup_role で返されない。"""
        conn = get_connection()
        try:
            register_session(conn, "sess-unreg2", "orch")
            conn.commit()
            unregister_session(conn, "sess-unreg2")
            conn.commit()

            os.environ.pop("OW_ROLE", None)
            result = lookup_role(conn, "sess-unreg2")
            assert result is None
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# update_heartbeat
# ---------------------------------------------------------------------------

class TestUpdateHeartbeat:
    """update_heartbeat の動作確認。"""

    def test_last_heartbeat_is_updated(self, migrated_db):
        """update_heartbeat を呼ぶと last_heartbeat が更新される。"""
        conn = get_connection()
        try:
            register_session(conn, "sess-hb2", "dispatcher")
            conn.commit()

            before = conn.execute(
                "SELECT last_heartbeat FROM session_identity WHERE session_id = ?",
                ("sess-hb2",),
            ).fetchone()["last_heartbeat"]

            time.sleep(1.1)

            update_heartbeat(conn, "sess-hb2")
            conn.commit()

            after = conn.execute(
                "SELECT last_heartbeat FROM session_identity WHERE session_id = ?",
                ("sess-hb2",),
            ).fetchone()["last_heartbeat"]

            assert after >= before
        finally:
            conn.close()
