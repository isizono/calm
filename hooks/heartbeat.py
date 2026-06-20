"""ハートビート更新 — stop_hookから呼ばれるDB書き込みの隔離モジュール"""
from src.db import get_connection


def update_heartbeat(activity_id: int, session_id: str | None = None) -> None:
    """last_heartbeat_at と last_heartbeat_session_id を現在のセッションで更新する。

    session_id を同梱することで、session_start_hook が「自セッション自身の
    heartbeat を別セッション扱いする」誤表示を回避できる。session_id が None
    の場合（テストフィクスチャ等）は NULL のまま書き込み、既存挙動を保つ。
    """
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE activities "
            "SET last_heartbeat_at = datetime('now'), "
            "    last_heartbeat_session_id = ? "
            "WHERE id = ?",
            (session_id, activity_id),
        )
        conn.commit()
    finally:
        conn.close()
