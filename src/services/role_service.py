"""capability gating の role 解決を集約するモジュール。

lookup_role が単一判定点。env OW_ROLE と session_identity DB lookup を統合し、
将来 env 経路廃止時もこの関数だけ書き換えれば済むようにする。
"""
import os
import sqlite3
from typing import Literal, Optional

Role = Literal["orch", "dispatcher", "worker", "user"]

_ROLE_ENV = "OW_ROLE"
_VALID_ROLES = ("orch", "dispatcher", "worker", "user")


def lookup_role(conn: sqlite3.Connection, session_id: Optional[str]) -> Optional[Role]:
    """session_id から role を解決する。

    優先順:
    1. session_identity.role (auto-db-register 後、active row のみ)
    2. env OW_ROLE (env 経路、worker session の現行互換)
    3. None (役割未判定 = grace period)
    """
    if session_id:
        row = conn.execute(
            "SELECT role FROM session_identity WHERE session_id = ? AND ended_at IS NULL",
            (session_id,),
        ).fetchone()
        if row and row[0] in _VALID_ROLES:
            return row[0]  # type: ignore[return-value]

    env_role = os.environ.get(_ROLE_ENV)
    if env_role in _VALID_ROLES:
        return env_role  # type: ignore[return-value]

    return None


def register_session(
    conn: sqlite3.Connection,
    session_id: str,
    role: Role,
    handle: Optional[str] = None,
    topic_id: Optional[int] = None,
    parent_session_id: Optional[str] = None,
) -> None:
    """session_identity に INSERT ON CONFLICT で idempotent に register する。

    既存 session_id があれば role / handle / last_heartbeat を更新する。
    """
    conn.execute(
        """
        INSERT INTO session_identity (session_id, role, handle, topic_id, parent_session_id)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
          role = excluded.role,
          handle = excluded.handle,
          last_heartbeat = CURRENT_TIMESTAMP
        """,
        (session_id, role, handle, topic_id, parent_session_id),
    )


def unregister_session(conn: sqlite3.Connection, session_id: str) -> None:
    """session_identity の ended_at をセットし、セッションを終了扱いにする。"""
    conn.execute(
        "UPDATE session_identity SET ended_at = CURRENT_TIMESTAMP WHERE session_id = ?",
        (session_id,),
    )


def update_heartbeat(conn: sqlite3.Connection, session_id: str) -> None:
    """session_identity の last_heartbeat を現在時刻に更新する。"""
    conn.execute(
        "UPDATE session_identity SET last_heartbeat = CURRENT_TIMESTAMP WHERE session_id = ?",
        (session_id,),
    )


def get_caller_session_id() -> Optional[str]:
    """MCP context から caller の session_id を取得する。

    MCP サーバーのツール実行コンテキスト外では None を返す。
    """
    try:
        from fastmcp.server.dependencies import get_context
        ctx = get_context()
        return ctx.session_id
    except Exception:
        return None
