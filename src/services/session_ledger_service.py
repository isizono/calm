"""セッション台帳(sessions テーブル)への読み書きサービス。

`sessions` テーブルは起動器(launcher)プロセスごとに1行を持つ台帳で、行は削除
しない。unregister・TTL失効・世代交代のいずれも `ended_at`/`ended_reason` を
立てるだけで残す(migrations/0078_add_sessions.sql参照)。
"""
from __future__ import annotations

from typing import Literal

from src.db import get_connection
from src.infra.session_identity import resolve_cli_session


def register(
    session_id: str,
    *,
    id_kind: Literal["bridge", "ephemeral"],
    harness: str | None,
    host: str | None,
    mode: Literal["interactive", "headless"],
) -> None:
    """起動器プロセスをセッション台帳へ登録する(heartbeat再送も同じ経路を通る)。

    同一harness・同一cli_session_idを持つ終了していない別行があれば、新しい
    行を立てる前にended_reason='superseded'で閉じる(世代交代)。閉じる処理と
    upsertは同一トランザクションで行う。順序を誤ると部分一意索引
    (idx_sessions_cli_live、(harness, cli_session_id)の複合)違反になる。
    一意性をharnessでも区切るのは、会話識別子の番号体系がharnessごとに
    異なり、異なるharness間で同じcli_session_id値が偶然一致しても別の会話
    として扱う必要があるため。

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
            "SELECT cli_session_id, cli_pid, cwd, cli_resolve_status, ended_at, ended_reason "
            "FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        # stale_on_startupで閉じた行は、同じsession_idのheartbeatが届いた時点で
        # 生きていたと分かるため復活させる(下のON CONFLICT参照)。
        already_ended = (
            existing is not None
            and existing["ended_at"] is not None
            and existing["ended_reason"] != "stale_on_startup"
        )

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
            cli_resolve_status = "not_found"

        if cli_session_id is not None and not already_ended:
            # 世代交代: 新しい行を立てる前に、同じharness・同じ会話識別子を
            # 持つ終了していない別行を閉じる。この順序を逆にすると部分一意
            # 索引違反になる。harnessも条件に含めるのは、索引が
            # (harness, cli_session_id)の複合になったため(harness IS ?は
            # NULL同士を一致させるSQLの標準的な比較方法)。
            conn.execute(
                """
                UPDATE sessions
                SET ended_at = CURRENT_TIMESTAMP, ended_reason = 'superseded'
                WHERE cli_session_id = ? AND harness IS ? AND session_id != ? AND ended_at IS NULL
                """,
                (cli_session_id, harness, session_id),
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
                last_heartbeat_at = excluded.last_heartbeat_at,
                ended_at = NULL,
                ended_reason = NULL
            WHERE ended_at IS NULL OR ended_reason = 'stale_on_startup'
            """,
            (session_id, id_kind, harness, host, cwd, cli_session_id, cli_pid, cli_resolve_status, mode),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_ended(session_id: str, reason: Literal["unregister", "ttl", "stale_on_startup"]) -> None:
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


def close_stale_sessions(liveness_timeout_sec: float) -> int:
    """前のサーバープロセスの時代から`ended_at`が空のまま残っている行を閉じる。

    サーバー起動直後はこのプロセスのSessionManagerが持つin-memoryのliveness
    reaperがまだ何も監視していない（register()されたheartbeatの記録がゼロ）
    ため、以前のサーバープロセスが生きていた間にliveness TTLを超えて
    heartbeatが途絶していた行（＝旧サーバーのreaperが処理しきれないうちに
    サーバー自体が終了したもの）は、新サーバーが自発的に拾わない限り
    `ended_at IS NULL`のまま永久に残る。liveness reaperが本来使う判定基準
    （`last_heartbeat_at`がTTLを超えて更新されていない）をサーバー起動時点に
    1回だけ適用し、同じ経路で閉じる。

    `liveness_timeout_sec<=0`（liveness reaper自体が無効化されている設定）の
    場合は何もしない（「stale」の定義自体が存在しないため）。

    まだ生きていて、たまたまheartbeatがTTLを超えて途絶した直後の行も対象に
    なりうる。この場合も、以後そのlauncherからheartbeat（register()）が届いた
    時点で行は復活する（ended_at/ended_reasonがNULLに戻る）。

    Returns:
        閉じた行数。
    """
    if liveness_timeout_sec <= 0:
        return 0

    conn = get_connection(load_vec=False)
    try:
        cursor = conn.execute(
            """
            UPDATE sessions
            SET ended_at = CURRENT_TIMESTAMP, ended_reason = 'stale_on_startup'
            WHERE ended_at IS NULL
              AND (last_heartbeat_at IS NULL OR last_heartbeat_at < datetime('now', ?))
            """,
            (f"-{liveness_timeout_sec} seconds",),
        )
        conn.commit()
        return cursor.rowcount
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_checkin(session_id: str | None, activity_id: int) -> None:
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
