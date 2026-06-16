"""ow_applied_msg_ids テーブルの CRUD ヘルパー。

reducer の idempotency を保証する用途。msg_id 単位で「適用済み (applied)」または
「解釈不能で skip (skipped)」を記録する。
"""
import sqlite3


def mark_msg_applied_with_conn(
    conn: sqlite3.Connection,
    *,
    channel_code: str,
    msg_id: int,
    applied_at: str,
    outcome: str = "applied",
) -> None:
    """msg_id を applied/skipped として記録する。既に記録済みなら何もしない (PK重複は無視)。"""
    if outcome not in ("applied", "skipped"):
        raise ValueError(f"invalid outcome: {outcome}")
    conn.execute(
        """
        INSERT OR IGNORE INTO ow_applied_msg_ids
          (channel_code, msg_id, applied_at, outcome)
        VALUES (?, ?, ?, ?)
        """,
        (channel_code, msg_id, applied_at, outcome),
    )


def is_msg_applied_with_conn(
    conn: sqlite3.Connection, *, channel_code: str, msg_id: int
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM ow_applied_msg_ids WHERE channel_code = ? AND msg_id = ?",
        (channel_code, msg_id),
    ).fetchone()
    return row is not None


def get_applied_msg_id_set_with_conn(
    conn: sqlite3.Connection, *, channel_code: str
) -> set[int]:
    """指定channelで適用済みの msg_id 全集合。reducer の冪等性チェックに使う。"""
    rows = conn.execute(
        "SELECT msg_id FROM ow_applied_msg_ids WHERE channel_code = ?",
        (channel_code,),
    ).fetchall()
    return {r["msg_id"] for r in rows}


def get_max_applied_msg_id_with_conn(
    conn: sqlite3.Connection, *, channel_code: str
) -> int:
    """指定channelで適用済みの最大 msg_id。未適用なら 0。"""
    row = conn.execute(
        "SELECT COALESCE(MAX(msg_id), 0) AS m "
        "FROM ow_applied_msg_ids WHERE channel_code = ?",
        (channel_code,),
    ).fetchone()
    return row["m"] or 0
