"""migration 0040_add_ow_tables のテスト

queue→activity管理化 Phase 1 の基盤テーブル3つを追加する migration の挙動を検証する:

- ow_channels: relayチャネルとtopic/orchの紐づけ
- ow_workers: workerランタイム状態MV（部分UNIQUE INDEX で alive 一意性を物理強制）
- ow_applied_msg_ids: reducer idempotency 用

ユーザー判断 Q10 で session_id は NULL 許容（identity未受信のready前workerをreducerで表現可能にするため）。
1 worker = 1 activity の物理強制は uq_ow_workers_one_alive_per_activity 部分UNIQUE INDEX で実現する。
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
    """全migration（0040含む）を適用済みのテスト用DBを提供する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def db_before_0040():
    """0037までのmigrationを適用したDBを提供する。0040の挙動を分離検証するために使う。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path

        parsed = parse_uri(f"sqlite:///{db_path}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        backend.init_database()
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        pre_0040 = MigrationList([m for m in all_migs if m.id < "0040"])
        with backend.lock():
            backend.apply_migrations(pre_0040)

        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


def _get_column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


def _get_indexes(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
    return {row["name"] for row in rows}


def _insert_topic(conn: sqlite3.Connection, title: str = "テストトピック") -> int:
    cur = conn.execute(
        "INSERT INTO discussion_topics (title, description) VALUES (?, ?)",
        (title, "説明"),
    )
    return cur.lastrowid


def _insert_activity(conn: sqlite3.Connection, title: str = "テストアクティビティ") -> int:
    cur = conn.execute(
        "INSERT INTO activities (title, description, status) VALUES (?, ?, ?)",
        (title, "説明", "pending"),
    )
    return cur.lastrowid


def _insert_channel(conn: sqlite3.Connection, channel_code: str, topic_id: int) -> None:
    conn.execute(
        """
        INSERT INTO ow_channels
          (channel_code, topic_id, orch_handle, last_seen_msg_id, created_at, updated_at)
        VALUES (?, ?, 'orch', 0, '2026-06-17T00:00:00Z', '2026-06-17T00:00:00Z')
        """,
        (channel_code, topic_id),
    )


def _insert_worker(
    conn: sqlite3.Connection,
    *,
    channel_code: str,
    handle: str,
    alias: str,
    activity_id: int | None,
    topic_id: int,
    task_n: int,
    workload_state: str = "spawning",
    cause: str | None = None,
    session_id: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO ow_workers
          (channel_code, handle, alias, activity_id, topic_id, task_n,
           session_id, workload_state, cause, spawned_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '2026-06-17T00:00:00Z')
        """,
        (channel_code, handle, alias, activity_id, topic_id, task_n,
         session_id, workload_state, cause),
    )
    return cur.lastrowid


class TestTablesCreated:
    """3テーブルが migration 0040 適用後に存在することを確認"""

    def test_ow_channels_exists_after_0040(self, migrated_db):
        conn = get_connection()
        try:
            assert "channel_code" in _get_column_names(conn, "ow_channels")
            assert "topic_id" in _get_column_names(conn, "ow_channels")
            assert "orch_session_id" in _get_column_names(conn, "ow_channels")
            assert "last_seen_msg_id" in _get_column_names(conn, "ow_channels")
            assert "deleted_at" in _get_column_names(conn, "ow_channels")
        finally:
            conn.close()

    def test_ow_workers_exists_after_0040(self, migrated_db):
        conn = get_connection()
        try:
            cols = _get_column_names(conn, "ow_workers")
            for col in [
                "id", "channel_code", "handle", "alias", "activity_id",
                "topic_id", "task_n", "model", "cwd", "permission_mode",
                "timeout_min", "task_material_id", "session_id",
                "workload_state", "cause", "last_state_msg_id",
                "last_heartbeat_at", "spawned_at", "ready_at", "terminated_at",
            ]:
                assert col in cols, f"ow_workers.{col} が存在しない"
        finally:
            conn.close()

    def test_ow_applied_msg_ids_exists_after_0040(self, migrated_db):
        conn = get_connection()
        try:
            cols = _get_column_names(conn, "ow_applied_msg_ids")
            for col in ["channel_code", "msg_id", "applied_at", "outcome"]:
                assert col in cols, f"ow_applied_msg_ids.{col} が存在しない"
        finally:
            conn.close()

    def test_tables_not_present_before_0040(self, db_before_0040):
        """0037適用時点では3テーブルが存在しない（前提確認）"""
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('ow_channels','ow_workers','ow_applied_msg_ids')"
            ).fetchall()
            assert rows == [], (
                f"0040適用前に owテーブルが既に存在: {[r['name'] for r in rows]}"
            )
        finally:
            conn.close()


class TestIndexes:
    """部分UNIQUE INDEX を含む INDEX 群の存在確認"""

    def test_unique_indexes_present(self, migrated_db):
        conn = get_connection()
        try:
            idx = _get_indexes(conn, "ow_workers")
            assert "uq_ow_workers_alive_handle" in idx
            assert "uq_ow_workers_one_alive_per_activity" in idx
            assert "uq_ow_workers_task_n" in idx
            assert "idx_ow_workers_activity" in idx
            assert "idx_ow_workers_state" in idx
            assert "idx_ow_workers_alive" in idx
        finally:
            conn.close()


class TestWorkloadStateCheck:
    """workload_state CHECK 制約"""

    def test_invalid_workload_state_rejected(self, migrated_db):
        conn = get_connection()
        try:
            tid = _insert_topic(conn)
            _insert_channel(conn, "C1", tid)
            aid = _insert_activity(conn)
            with pytest.raises(sqlite3.IntegrityError):
                _insert_worker(
                    conn, channel_code="C1", handle="w-a", alias="w-a",
                    activity_id=aid, topic_id=tid, task_n=1,
                    workload_state="bogus",
                )
        finally:
            conn.close()

    def test_valid_workload_states_accepted(self, migrated_db):
        conn = get_connection()
        try:
            tid = _insert_topic(conn)
            _insert_channel(conn, "C1", tid)
            valid_states = [
                "spawning", "loading", "ready", "working",
                "blocked", "escalated", "draining", "terminated",
            ]
            for i, state in enumerate(valid_states):
                aid = _insert_activity(conn, title=f"act-{state}")
                _insert_worker(
                    conn, channel_code="C1", handle=f"w-{i}", alias=f"w-{i}",
                    activity_id=aid, topic_id=tid, task_n=i + 1,
                    workload_state=state,
                )
        finally:
            conn.close()


class TestCauseCheck:
    """cause CHECK 制約 (NULL許容 + enum)"""

    def test_null_cause_accepted(self, migrated_db):
        conn = get_connection()
        try:
            tid = _insert_topic(conn)
            _insert_channel(conn, "C1", tid)
            aid = _insert_activity(conn)
            _insert_worker(
                conn, channel_code="C1", handle="w-a", alias="w-a",
                activity_id=aid, topic_id=tid, task_n=1, cause=None,
            )
        finally:
            conn.close()

    def test_invalid_cause_rejected(self, migrated_db):
        conn = get_connection()
        try:
            tid = _insert_topic(conn)
            _insert_channel(conn, "C1", tid)
            aid = _insert_activity(conn)
            with pytest.raises(sqlite3.IntegrityError):
                _insert_worker(
                    conn, channel_code="C1", handle="w-a", alias="w-a",
                    activity_id=aid, topic_id=tid, task_n=1,
                    workload_state="terminated", cause="bogus",
                )
        finally:
            conn.close()

    def test_valid_causes_accepted(self, migrated_db):
        conn = get_connection()
        try:
            tid = _insert_topic(conn)
            _insert_channel(conn, "C1", tid)
            for i, cause in enumerate(
                ["closed", "cancelled", "dead", "crashed", "crashed-during-drain"]
            ):
                aid = _insert_activity(conn, title=f"act-{cause}")
                _insert_worker(
                    conn, channel_code="C1", handle=f"w-{i}", alias=f"w-{i}",
                    activity_id=aid, topic_id=tid, task_n=i + 1,
                    workload_state="terminated", cause=cause,
                )
        finally:
            conn.close()


class TestSessionIdNullable:
    """session_id NULL 許容（Q10 判断）"""

    def test_null_session_id_accepted(self, migrated_db):
        conn = get_connection()
        try:
            tid = _insert_topic(conn)
            _insert_channel(conn, "C1", tid)
            aid = _insert_activity(conn)
            _insert_worker(
                conn, channel_code="C1", handle="w-a", alias="w-a",
                activity_id=aid, topic_id=tid, task_n=1, session_id=None,
            )
            row = conn.execute(
                "SELECT session_id FROM ow_workers WHERE handle = 'w-a'"
            ).fetchone()
            assert row["session_id"] is None
        finally:
            conn.close()


class TestAliveHandleUniqueness:
    """uq_ow_workers_alive_handle: alive期間中 (channel, handle) 一意"""

    def test_two_alive_same_handle_rejected(self, migrated_db):
        conn = get_connection()
        try:
            tid = _insert_topic(conn)
            _insert_channel(conn, "C1", tid)
            aid1 = _insert_activity(conn, title="a1")
            aid2 = _insert_activity(conn, title="a2")
            _insert_worker(
                conn, channel_code="C1", handle="w-a", alias="w-a-1",
                activity_id=aid1, topic_id=tid, task_n=1, workload_state="working",
            )
            with pytest.raises(sqlite3.IntegrityError):
                _insert_worker(
                    conn, channel_code="C1", handle="w-a", alias="w-a-2",
                    activity_id=aid2, topic_id=tid, task_n=2,
                    workload_state="working",
                )
        finally:
            conn.close()

    def test_terminated_handle_can_be_reused(self, migrated_db):
        conn = get_connection()
        try:
            tid = _insert_topic(conn)
            _insert_channel(conn, "C1", tid)
            aid1 = _insert_activity(conn, title="a1")
            aid2 = _insert_activity(conn, title="a2")
            _insert_worker(
                conn, channel_code="C1", handle="w-a", alias="w-a-1",
                activity_id=aid1, topic_id=tid, task_n=1,
                workload_state="terminated", cause="closed",
            )
            # 同handleを再利用できる (terminatedはalive判定外)
            _insert_worker(
                conn, channel_code="C1", handle="w-a", alias="w-a-2",
                activity_id=aid2, topic_id=tid, task_n=2,
                workload_state="working",
            )
        finally:
            conn.close()


class TestOneAliveWorkerPerActivity:
    """uq_ow_workers_one_alive_per_activity: alive期間中 activity_id 一意"""

    def test_two_alive_same_activity_rejected(self, migrated_db):
        conn = get_connection()
        try:
            tid = _insert_topic(conn)
            _insert_channel(conn, "C1", tid)
            aid = _insert_activity(conn)
            _insert_worker(
                conn, channel_code="C1", handle="w-a", alias="w-a",
                activity_id=aid, topic_id=tid, task_n=1, workload_state="working",
            )
            with pytest.raises(sqlite3.IntegrityError):
                _insert_worker(
                    conn, channel_code="C1", handle="w-b", alias="w-b",
                    activity_id=aid, topic_id=tid, task_n=2,
                    workload_state="working",
                )
        finally:
            conn.close()

    def test_alive_after_terminated_same_activity(self, migrated_db):
        """同activityのworkerがterminated後、別workerをaliveで再立てられる（履歴あり再spawn）"""
        conn = get_connection()
        try:
            tid = _insert_topic(conn)
            _insert_channel(conn, "C1", tid)
            aid = _insert_activity(conn)
            _insert_worker(
                conn, channel_code="C1", handle="w-a", alias="w-a-1",
                activity_id=aid, topic_id=tid, task_n=1,
                workload_state="terminated", cause="closed",
            )
            _insert_worker(
                conn, channel_code="C1", handle="w-b", alias="w-b",
                activity_id=aid, topic_id=tid, task_n=2,
                workload_state="working",
            )
        finally:
            conn.close()

    def test_null_activity_id_not_constrained_by_unique(self, migrated_db):
        """activity_id IS NULL のworkerは UNIQUE 制約対象外（複数同居可）"""
        conn = get_connection()
        try:
            tid = _insert_topic(conn)
            _insert_channel(conn, "C1", tid)
            _insert_worker(
                conn, channel_code="C1", handle="w-a", alias="w-a",
                activity_id=None, topic_id=tid, task_n=1, workload_state="working",
            )
            _insert_worker(
                conn, channel_code="C1", handle="w-b", alias="w-b",
                activity_id=None, topic_id=tid, task_n=2, workload_state="working",
            )
        finally:
            conn.close()


class TestTaskNUniqueness:
    """uq_ow_workers_task_n: (channel_code, task_n) は全期間で一意（terminated含む）"""

    def test_duplicate_task_n_rejected_even_terminated(self, migrated_db):
        conn = get_connection()
        try:
            tid = _insert_topic(conn)
            _insert_channel(conn, "C1", tid)
            aid1 = _insert_activity(conn, title="a1")
            aid2 = _insert_activity(conn, title="a2")
            _insert_worker(
                conn, channel_code="C1", handle="w-a", alias="w-a",
                activity_id=aid1, topic_id=tid, task_n=1,
                workload_state="terminated", cause="closed",
            )
            with pytest.raises(sqlite3.IntegrityError):
                _insert_worker(
                    conn, channel_code="C1", handle="w-b", alias="w-b",
                    activity_id=aid2, topic_id=tid, task_n=1,
                    workload_state="working",
                )
        finally:
            conn.close()

    def test_same_task_n_on_different_channels(self, migrated_db):
        conn = get_connection()
        try:
            tid = _insert_topic(conn)
            _insert_channel(conn, "C1", tid)
            _insert_channel(conn, "C2", tid)
            aid1 = _insert_activity(conn, title="a1")
            aid2 = _insert_activity(conn, title="a2")
            _insert_worker(
                conn, channel_code="C1", handle="w-a", alias="w-a",
                activity_id=aid1, topic_id=tid, task_n=1, workload_state="working",
            )
            _insert_worker(
                conn, channel_code="C2", handle="w-b", alias="w-b",
                activity_id=aid2, topic_id=tid, task_n=1, workload_state="working",
            )
        finally:
            conn.close()


class TestAppliedMsgIdsOutcome:
    """ow_applied_msg_ids.outcome CHECK 制約 ('applied','skipped')"""

    def test_valid_outcomes_accepted(self, migrated_db):
        conn = get_connection()
        try:
            tid = _insert_topic(conn)
            _insert_channel(conn, "C1", tid)
            for outcome, mid in [("applied", 1), ("skipped", 2)]:
                conn.execute(
                    "INSERT INTO ow_applied_msg_ids "
                    "(channel_code, msg_id, applied_at, outcome) "
                    "VALUES (?, ?, '2026-06-17T00:00:00Z', ?)",
                    ("C1", mid, outcome),
                )
        finally:
            conn.close()

    def test_invalid_outcome_rejected(self, migrated_db):
        conn = get_connection()
        try:
            tid = _insert_topic(conn)
            _insert_channel(conn, "C1", tid)
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO ow_applied_msg_ids "
                    "(channel_code, msg_id, applied_at, outcome) "
                    "VALUES (?, ?, '2026-06-17T00:00:00Z', ?)",
                    ("C1", 1, "rejected"),
                )
        finally:
            conn.close()

    def test_composite_pk_prevents_duplicate(self, migrated_db):
        conn = get_connection()
        try:
            tid = _insert_topic(conn)
            _insert_channel(conn, "C1", tid)
            conn.execute(
                "INSERT INTO ow_applied_msg_ids "
                "(channel_code, msg_id, applied_at, outcome) "
                "VALUES ('C1', 1, '2026-06-17T00:00:00Z', 'applied')"
            )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO ow_applied_msg_ids "
                    "(channel_code, msg_id, applied_at, outcome) "
                    "VALUES ('C1', 1, '2026-06-17T00:00:00Z', 'applied')"
                )
        finally:
            conn.close()


class TestForeignKeysSetNull:
    """activity_id / task_material_id の ON DELETE SET NULL"""

    def test_activity_delete_sets_worker_activity_id_null(self, migrated_db):
        conn = get_connection()
        try:
            tid = _insert_topic(conn)
            _insert_channel(conn, "C1", tid)
            aid = _insert_activity(conn)
            wid = _insert_worker(
                conn, channel_code="C1", handle="w-a", alias="w-a",
                activity_id=aid, topic_id=tid, task_n=1, workload_state="working",
            )
            conn.execute("DELETE FROM activities WHERE id = ?", (aid,))
            row = conn.execute(
                "SELECT activity_id FROM ow_workers WHERE id = ?", (wid,)
            ).fetchone()
            assert row["activity_id"] is None
        finally:
            conn.close()
