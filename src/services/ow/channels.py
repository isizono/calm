"""ow_channels テーブルの CRUD ヘルパー。

ow_channels はrelay側channelとtopic/orchの紐づけを保持する。relay.db との cross-DB FK
は張れないため channel_code は TEXT として保持し、reducer が必要に応じて整合を遅延検出する。
"""
import sqlite3

from src.db import get_connection, row_to_dict


def upsert_channel_with_conn(
    conn: sqlite3.Connection,
    *,
    channel_code: str,
    topic_id: int,
    orch_handle: str,
    orch_activity_id: int | None = None,
    orch_cwd: str | None = None,
    orch_session_id: str | None = None,
    now: str,
) -> None:
    """ow_channels に対し INSERT or UPDATE する。

    既存行があれば orch_* / updated_at のみ更新し、last_seen_msg_id / deleted_at / topic_id
    / orch_handle / created_at は保持する。
    """
    existing = conn.execute(
        "SELECT 1 FROM ow_channels WHERE channel_code = ?", (channel_code,)
    ).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO ow_channels
              (channel_code, topic_id, orch_handle, orch_activity_id,
               orch_cwd, orch_session_id, last_seen_msg_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (channel_code, topic_id, orch_handle, orch_activity_id,
             orch_cwd, orch_session_id, now, now),
        )
        return
    conn.execute(
        """
        UPDATE ow_channels
        SET orch_activity_id = COALESCE(?, orch_activity_id),
            orch_cwd = COALESCE(?, orch_cwd),
            orch_session_id = COALESCE(?, orch_session_id),
            updated_at = ?
        WHERE channel_code = ?
        """,
        (orch_activity_id, orch_cwd, orch_session_id, now, channel_code),
    )


def get_channel_with_conn(
    conn: sqlite3.Connection, channel_code: str
) -> dict | None:
    """channel_code に該当する ow_channels 1件を dict で返す。無ければ None。"""
    row = conn.execute(
        "SELECT * FROM ow_channels WHERE channel_code = ?", (channel_code,)
    ).fetchone()
    return row_to_dict(row) if row else None


def list_channels_with_conn(
    conn: sqlite3.Connection,
    *,
    topic_id: int | None = None,
    include_deleted: bool = False,
) -> list[dict]:
    """ow_channels を一覧する。topic_id 指定時はそのtopic配下のみ。"""
    clauses: list[str] = []
    params: list = []
    if topic_id is not None:
        clauses.append("topic_id = ?")
        params.append(topic_id)
    if not include_deleted:
        clauses.append("deleted_at IS NULL")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM ow_channels {where} ORDER BY channel_code", params
    ).fetchall()
    return [row_to_dict(r) for r in rows]


def update_channel_last_seen_with_conn(
    conn: sqlite3.Connection,
    *,
    channel_code: str,
    last_seen_msg_id: int,
    now: str,
) -> None:
    """last_seen_msg_id を単調増加で更新する。逆行更新は無視。"""
    conn.execute(
        """
        UPDATE ow_channels
        SET last_seen_msg_id = MAX(last_seen_msg_id, ?),
            updated_at = ?
        WHERE channel_code = ?
        """,
        (last_seen_msg_id, now, channel_code),
    )


def soft_delete_channel_with_conn(
    conn: sqlite3.Connection,
    *,
    channel_code: str,
    deleted_at: str,
) -> None:
    """relay側channelの消失を反映するソフトデリート（行は残す）。"""
    conn.execute(
        "UPDATE ow_channels SET deleted_at = ?, updated_at = ? WHERE channel_code = ?",
        (deleted_at, deleted_at, channel_code),
    )


def upsert_channel(
    *,
    channel_code: str,
    topic_id: int,
    orch_handle: str,
    orch_activity_id: int | None = None,
    orch_cwd: str | None = None,
    orch_session_id: str | None = None,
    now: str,
) -> None:
    """upsert_channel_with_conn の単独conn版ラッパー。"""
    conn = get_connection()
    try:
        upsert_channel_with_conn(
            conn,
            channel_code=channel_code,
            topic_id=topic_id,
            orch_handle=orch_handle,
            orch_activity_id=orch_activity_id,
            orch_cwd=orch_cwd,
            orch_session_id=orch_session_id,
            now=now,
        )
        conn.commit()
    finally:
        conn.close()


def get_channel(channel_code: str) -> dict | None:
    conn = get_connection()
    try:
        return get_channel_with_conn(conn, channel_code)
    finally:
        conn.close()
