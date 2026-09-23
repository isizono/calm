"""セッション台帳(sessions テーブル)への読み書きサービス。

`sessions` テーブルは起動器(launcher)プロセスごとに1行を持つ台帳で、行は削除
しない。unregister・TTL失効・世代交代のいずれも `ended_at`/`ended_reason` を
立てるだけで残す(migrations/0078_add_sessions.sql参照)。
"""
from __future__ import annotations

from typing import Literal, Optional

from src.db import get_connection
from src.infra.session_identity import resolve_cli_session


def register(
    session_id: str,
    *,
    id_kind: Literal["bridge", "ephemeral"],
    harness: Optional[str],
    host: Optional[str],
    mode: Literal["interactive", "headless"],
) -> None:
    """起動器プロセスをセッション台帳へ登録する(heartbeat再送も同じ経路を通る)。

    同一 cli_session_id を持つ終了していない別行があれば、新しい行を立てる前に
    ended_reason='superseded' で閉じる(世代交代)。閉じる処理とupsertは同一
    トランザクションで行う。順序を誤ると部分一意索引(idx_sessions_cli_live)
    違反になる。

    既にended済みの行はupsertで復活させない(`ON CONFLICT DO UPDATE ... WHERE
    ended_at IS NULL` によりno-op)。復活を許すと、supersededで閉じた旧世代の
    行に遅延したheartbeatが届いた際、新世代の生存行とcli_session_idが重複して
    部分一意索引違反になる。

    resolve_cli_session() は解決不能な状況を推定せずNoneを返す(fail-close)
    設計のため、一度解決できた会話識別子が後続のheartbeatで一時的に解決失敗
    することがありうる。その場合は直前に保持していた解決済みの値を維持する
    (同一launcherプロセスの会話識別子が別物に変わることは無いため)。

    session_id自身の行が既にended済みの場合(supersededで閉じられた旧世代への
    遅延heartbeat等)は、他行を閉じる処理そのものを行わない。行うと、既に
    死んでいるはずの旧世代からの遅延heartbeatが、同じcli_session_idを持つ
    現行世代の生存行を誤ってsupersededで閉じてしまう。

    この判定に使うSELECTは書き込みと同一トランザクションでBEGIN IMMEDIATEに
    より書き込みロックを先取りする。デフォルトのDEFERREDトランザクション
    (sqlite3モジュールはSELECT単体ではBEGINを発行しない)のままだと、
    SELECTと後続UPDATE/INSERTの間に別接続の書き込みが割り込みうる。割り込むと
    「session_id自身が既にended済みか」の判定が古いスナップショットのまま
    書き込みが実行され、他接続が新たに確立した現行世代の生存行を誤って
    supersededで閉じてしまう(TOCTOU)。
    """
    entry = resolve_cli_session(session_id)

    conn = get_connection(load_vec=False)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT cli_session_id, cli_pid, cwd, cli_resolve_status, ended_at "
            "FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        already_ended = existing is not None and existing["ended_at"] is not None

        if entry is not None and entry.get("cli_session_id") is not None:
            cli_session_id = entry.get("cli_session_id")
            cli_pid = entry.get("cli_pid")
            cwd = entry.get("cwd")
            cli_resolve_status = "resolved"
        elif entry is not None:
            # resolve_cli_session()はCLIセッションファイルが見つかっても、
            # 中身にsessionIdフィールドが無ければcli_session_id=Noneのまま
            # 辞書を返すことがある(read_cli_session参照)。この場合
            # 会話識別子としては使えないため、'resolved'を名乗らない。
            cli_session_id = None
            cli_pid = entry.get("cli_pid")
            cwd = entry.get("cwd")
            cli_resolve_status = "stale"
        elif existing is not None and existing["cli_session_id"] is not None:
            cli_session_id = existing["cli_session_id"]
            cli_pid = existing["cli_pid"]
            cwd = existing["cwd"]
            cli_resolve_status = existing["cli_resolve_status"]
        else:
            cli_session_id = None
            cli_pid = None
            cwd = None
            cli_resolve_status = "file_not_found"

        if cli_session_id is not None and not already_ended:
            # 世代交代: 新しい行を立てる前に、同じ会話識別子を持つ終了していない
            # 別行を閉じる。この順序を逆にすると部分一意索引違反になる。
            conn.execute(
                """
                UPDATE sessions
                SET ended_at = CURRENT_TIMESTAMP, ended_reason = 'superseded'
                WHERE cli_session_id = ? AND session_id != ? AND ended_at IS NULL
                """,
                (cli_session_id, session_id),
            )

        conn.execute(
            """
            INSERT INTO sessions (
                session_id, id_kind, harness, host, cwd,
                cli_session_id, cli_pid, cli_resolve_status, mode,
                last_heartbeat_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(session_id) DO UPDATE SET
                id_kind = excluded.id_kind,
                harness = excluded.harness,
                host = excluded.host,
                cwd = excluded.cwd,
                cli_session_id = excluded.cli_session_id,
                cli_pid = excluded.cli_pid,
                cli_resolve_status = excluded.cli_resolve_status,
                mode = excluded.mode,
                last_heartbeat_at = excluded.last_heartbeat_at
            WHERE ended_at IS NULL
            """,
            (session_id, id_kind, harness, host, cwd, cli_session_id, cli_pid, cli_resolve_status, mode),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_ended(session_id: str, reason: Literal["unregister", "ttl"]) -> None:
    """session_idの行をended状態にする。

    既にended済みの行、または存在しないsession_idはno-op(冪等)。
    """
    conn = get_connection(load_vec=False)
    try:
        conn.execute(
            """
            UPDATE sessions
            SET ended_at = CURRENT_TIMESTAMP, ended_reason = ?
            WHERE session_id = ? AND ended_at IS NULL
            """,
            (reason, session_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_checkin(session_id: Optional[str], activity_id: int) -> None:
    """check_in時にlast_checkin_activity_id/last_checkin_atを書く。

    session_idがNone、または対応する行が無い場合は何もしない。
    """
    if session_id is None:
        return
    conn = get_connection(load_vec=False)
    try:
        conn.execute(
            """
            UPDATE sessions
            SET last_checkin_activity_id = ?, last_checkin_at = CURRENT_TIMESTAMP
            WHERE session_id = ?
            """,
            (activity_id, session_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
