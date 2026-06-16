"""ow_workers テーブルの CRUD ヘルパー。

ow_workers は worker run instance を表す MV テーブル。同一activityに対して再spawnすると
terminated行が履歴として残り、alive期間中のみ部分 UNIQUE INDEX で 1 worker = 1 activity
を物理強制する。
"""
import sqlite3

from src.db import get_connection, row_to_dict

ALIVE_WORKLOAD_STATES = (
    "spawning", "loading", "ready", "working", "blocked", "escalated", "draining",
)


def allocate_task_n_with_conn(conn: sqlite3.Connection, channel_code: str) -> int:
    """channel単位の task_n 連番を採番する。

    部分UNIQUE INDEX uq_ow_workers_task_n が衝突検知の保険。並行spawn時はリトライ前提。
    """
    row = conn.execute(
        "SELECT COALESCE(MAX(task_n), 0) AS m FROM ow_workers WHERE channel_code = ?",
        (channel_code,),
    ).fetchone()
    return (row["m"] or 0) + 1


def insert_worker_with_conn(
    conn: sqlite3.Connection,
    *,
    channel_code: str,
    handle: str,
    alias: str,
    activity_id: int | None,
    topic_id: int,
    task_n: int,
    spawned_at: str,
    model: str | None = None,
    cwd: str | None = None,
    permission_mode: str | None = None,
    timeout_min: int | None = None,
    task_material_id: int | None = None,
    session_id: str | None = None,
    workload_state: str = "spawning",
) -> int:
    """ow_workers に新規 worker 行を INSERT し id を返す。"""
    cur = conn.execute(
        """
        INSERT INTO ow_workers
          (channel_code, handle, alias, activity_id, topic_id, task_n,
           model, cwd, permission_mode, timeout_min, task_material_id,
           session_id, workload_state, spawned_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (channel_code, handle, alias, activity_id, topic_id, task_n,
         model, cwd, permission_mode, timeout_min, task_material_id,
         session_id, workload_state, spawned_at),
    )
    return cur.lastrowid


def get_alive_worker_by_handle_with_conn(
    conn: sqlite3.Connection,
    *,
    channel_code: str,
    handle: str,
) -> dict | None:
    """alive (非terminated) な worker を handle で1件取得する。

    uq_ow_workers_alive_handle により alive 期間中は (channel, handle) が一意なので
    返却は0件 or 1件。
    """
    row = conn.execute(
        """
        SELECT * FROM ow_workers
        WHERE channel_code = ? AND handle = ? AND workload_state != 'terminated'
        """,
        (channel_code, handle),
    ).fetchone()
    return row_to_dict(row) if row else None


def get_worker_by_id_with_conn(
    conn: sqlite3.Connection, worker_id: int
) -> dict | None:
    row = conn.execute(
        "SELECT * FROM ow_workers WHERE id = ?", (worker_id,)
    ).fetchone()
    return row_to_dict(row) if row else None


def list_workers_with_conn(
    conn: sqlite3.Connection,
    *,
    channel_code: str | None = None,
    topic_id: int | None = None,
    activity_id: int | None = None,
    alive_only: bool = True,
) -> list[dict]:
    clauses: list[str] = []
    params: list = []
    if channel_code is not None:
        clauses.append("channel_code = ?")
        params.append(channel_code)
    if topic_id is not None:
        clauses.append("topic_id = ?")
        params.append(topic_id)
    if activity_id is not None:
        clauses.append("activity_id = ?")
        params.append(activity_id)
    if alive_only:
        clauses.append("workload_state != 'terminated'")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM ow_workers {where} ORDER BY task_n", params
    ).fetchall()
    return [row_to_dict(r) for r in rows]


def update_worker_state_with_conn(
    conn: sqlite3.Connection,
    *,
    worker_id: int,
    workload_state: str,
    cause: str | None = None,
    last_state_msg_id: int | None = None,
    last_heartbeat_at: str | None = None,
    ready_at: str | None = None,
    terminated_at: str | None = None,
    session_id: str | None = None,
) -> None:
    """worker の state とタイムスタンプを更新する。

    各引数が None の項目は既存値を保持する（COALESCE）。workload_state は常に上書き。
    """
    conn.execute(
        """
        UPDATE ow_workers
        SET workload_state = ?,
            cause = COALESCE(?, cause),
            last_state_msg_id = COALESCE(?, last_state_msg_id),
            last_heartbeat_at = COALESCE(?, last_heartbeat_at),
            ready_at = COALESCE(?, ready_at),
            terminated_at = COALESCE(?, terminated_at),
            session_id = COALESCE(?, session_id)
        WHERE id = ?
        """,
        (workload_state, cause, last_state_msg_id, last_heartbeat_at,
         ready_at, terminated_at, session_id, worker_id),
    )


def update_worker_heartbeat_with_conn(
    conn: sqlite3.Connection,
    *,
    worker_id: int,
    last_heartbeat_at: str,
) -> None:
    """heartbeat のみ更新（state遷移を伴わない）。"""
    conn.execute(
        "UPDATE ow_workers SET last_heartbeat_at = ? WHERE id = ?",
        (last_heartbeat_at, worker_id),
    )


def update_worker_identity_with_conn(
    conn: sqlite3.Connection,
    *,
    worker_id: int,
    session_id: str | None = None,
    model: str | None = None,
    cwd: str | None = None,
) -> None:
    """identity event 受信時の身元情報を更新する（COALESCE で部分更新）。"""
    conn.execute(
        """
        UPDATE ow_workers
        SET session_id = COALESCE(?, session_id),
            model = COALESCE(?, model),
            cwd = COALESCE(?, cwd)
        WHERE id = ?
        """,
        (session_id, model, cwd, worker_id),
    )


def get_alive_workers(channel_code: str) -> list[dict]:
    conn = get_connection()
    try:
        return list_workers_with_conn(
            conn, channel_code=channel_code, alive_only=True
        )
    finally:
        conn.close()
