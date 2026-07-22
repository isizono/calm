"""判断委譲（asks）の記録・状態遷移サービス。

AIエージェントが人間の判断を待つ問いを1件積み、人間が回答するだけで作業を
再開できるようにする受け皿。answer時点ではトリアージ（promote/dismiss）を
実行せず、次のcheck_inで配達されるまで遅延する（判定はLLMの仕事のため）。

状態遷移: open → answered → promoted/dismissed、open → withdrawn。
訂正（answered/promoted/dismissedの再答弁）は新規post（別ライフ、別行）で
行い、supersedesのようなリンクは張らない。
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from src.db import get_connection, row_to_dict
from src.services import search_service
from src.services.decision_service import add_decisions
from src.services.dedup_helpers import compute_fingerprint16, normalize_text
from src.services.embedding_service import encode_document, insert_ask_embedding_with_conn
from src.services.readable_id import strip_entity_id_inplace
from src.services.relay.entity_publish import publish_entity_event_with_conn

QUESTION_MAX_LEN = 500
CONTEXT_MAX_LEN = 8000
ANSWER_BODY_MAX_LEN = 8000

# fingerprint単位でのwithdraw直後の再post拒否ウィンドウ（誤操作保護、session条件なし）。
WITHDRAW_COOLDOWN_MINUTES = 5

VALID_STATUSES = {"open", "answered", "promoted", "dismissed", "withdrawn"}

# get_asks の limit 引数の上限。get_signals と同じ値を採用する
# （設計文書は上限を明記していないため、他の一覧系ツールより広めの値を実装判断で採用する）。
_MAX_LIMIT = 100

# stats.last_30d の集計期間。get_signals と同じ固定値。
_STATS_RECENT_DAYS = 30


def _validation_error(message: str) -> dict:
    return {"error": {"code": "VALIDATION_ERROR", "message": message}}


# ========================================
# add_ask
# ========================================


def add_ask_with_conn(
    conn: sqlite3.Connection,
    question: str,
    blocks: list[int],
    context: Optional[str] = None,
    session_id: Optional[str] = None,
) -> dict:
    """検証 + upsert + ask_blocks/ask_requestersのUNION追記をconn上で行う。

    commitは呼び出し側の責任（embedding生成の前段でコミットし、HTTP呼び出しの間
    書き込みトランザクションを開いたままにしないため。topic_service.add_topicと
    同じ二段コミットパターン）。

    Returns:
        成功時: {"id": int, "deduped": bool, "occurrence_count": int}
        失敗時: {"error": {"code": "VALIDATION_ERROR", "message": ...}}
    """
    question = (question or "").strip()
    if not question:
        return _validation_error("question must not be empty")
    if len(question) > QUESTION_MAX_LEN:
        return _validation_error(f"question must not exceed {QUESTION_MAX_LEN} characters")
    if context is not None and len(context) > CONTEXT_MAX_LEN:
        return _validation_error(f"context must not exceed {CONTEXT_MAX_LEN} characters")
    if not blocks:
        return _validation_error("blocks must not be empty")

    # duplicate activity_idはサービス層でset化して静かにdedupeする（エラーにしない）。
    block_ids = list(dict.fromkeys(blocks))
    placeholders = ",".join("?" * len(block_ids))
    rows = conn.execute(
        f"SELECT id, status FROM activities WHERE id IN ({placeholders})",
        tuple(block_ids),
    ).fetchall()
    status_by_id = {row["id"]: row["status"] for row in rows}
    missing = [bid for bid in block_ids if bid not in status_by_id]
    if missing:
        return _validation_error(f"blocks references nonexistent activity id(s): {missing}")
    if all(status == "completed" for status in status_by_id.values()):
        return _validation_error(
            "blocks must include at least one activity that is not completed"
        )

    fingerprint = compute_fingerprint16(normalize_text(question))

    recent_withdraw = conn.execute(
        """
        SELECT 1 FROM asks
        WHERE fingerprint = ? AND status = 'withdrawn'
          AND withdrawn_at >= datetime('now', ?)
        LIMIT 1
        """,
        (fingerprint, f"-{WITHDRAW_COOLDOWN_MINUTES} minutes"),
    ).fetchone()
    if recent_withdraw:
        return _validation_error(
            "this question was withdrawn within the last "
            f"{WITHDRAW_COOLDOWN_MINUTES} minutes; wait before re-posting"
        )

    cursor = conn.execute(
        """
        INSERT INTO asks (question, context, fingerprint, first_seen_session_id, last_seen_session_id)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(fingerprint) WHERE status = 'open'
        DO UPDATE SET
            occurrence_count = asks.occurrence_count + 1,
            last_seen_at = CURRENT_TIMESTAMP,
            context = excluded.context,
            last_seen_session_id = excluded.last_seen_session_id
        RETURNING id, occurrence_count
        """,
        (question, context, fingerprint, session_id, session_id),
    )
    ask_id, occurrence_count = cursor.fetchone()

    for bid in block_ids:
        conn.execute(
            "INSERT OR IGNORE INTO ask_blocks (ask_id, activity_id) VALUES (?, ?)",
            (ask_id, bid),
        )
    if session_id:
        conn.execute(
            "INSERT OR IGNORE INTO ask_requesters (ask_id, requester_session_id) VALUES (?, ?)",
            (ask_id, session_id),
        )

    publish_entity_event_with_conn(
        conn,
        entity_type="ask",
        entity_id=ask_id,
        event="created" if occurrence_count == 1 else "updated",
    )

    return {"id": ask_id, "deduped": occurrence_count > 1, "occurrence_count": occurrence_count}


def add_ask(
    question: str,
    blocks: list[int],
    context: Optional[str] = None,
    session_id: Optional[str] = None,
) -> dict:
    """MCPツール本体。add_ask_with_connで書き込みcommit後、embedding生成と
    近傍検索（similar_precedents/similar_asks）を行う。

    Returns:
        成功時: {"id", "deduped", "occurrence_count", "similar_precedents", "similar_asks"}
        失敗時: {"error": {"code": ..., "message": ...}}
    """
    conn = get_connection()
    try:
        result = add_ask_with_conn(conn, question, blocks, context=context, session_id=session_id)
        if "error" in result:
            conn.rollback()
            return result
        conn.commit()

        ask_id = result["id"]
        similar_precedents: list = []
        similar_asks: list = []
        embedding = encode_document(question.strip())
        if embedding is not None:
            insert_ask_embedding_with_conn(conn, ask_id, embedding)
            conn.commit()
            similar_precedents = search_service.find_similar_decisions(embedding=embedding, limit=3)
            similar_asks = search_service.find_similar_asks(embedding=embedding, exclude_id=ask_id, limit=3)

        result["similar_precedents"] = similar_precedents
        result["similar_asks"] = similar_asks
        return result
    except Exception as e:
        conn.rollback()
        return {"error": {"code": "DATABASE_ERROR", "message": str(e)}}
    finally:
        conn.close()


# ========================================
# get_asks
# ========================================


def _build_ask_item(conn: sqlite3.Connection, ask: dict) -> dict:
    """ask 1件にblocks/requestersを合流し、内部専用フィールドを整形する。"""
    ask.pop("fingerprint", None)
    ask_id = ask["id"]

    block_rows = conn.execute(
        """
        SELECT act.id, act.title, act.status
        FROM ask_blocks ab
        JOIN activities act ON act.id = ab.activity_id
        WHERE ab.ask_id = ?
        ORDER BY ab.added_at ASC
        """,
        (ask_id,),
    ).fetchall()
    blocks = []
    for row in block_rows:
        item = {"id": row["id"], "title": row["title"], "status": row["status"]}
        strip_entity_id_inplace(item)
        blocks.append(item)
    ask["blocks"] = blocks

    requester_rows = conn.execute(
        "SELECT requester_session_id FROM ask_requesters WHERE ask_id = ? ORDER BY added_at ASC",
        (ask_id,),
    ).fetchall()
    ask["requesters"] = [row["requester_session_id"] for row in requester_rows]

    strip_entity_id_inplace(ask, id_key="promoted_decision_id")
    strip_entity_id_inplace(ask)
    return ask


def _compute_ask_stats(conn: sqlite3.Connection) -> dict:
    by_status: dict[str, int] = {}
    for row in conn.execute("SELECT status, COUNT(*) AS c FROM asks GROUP BY status").fetchall():
        by_status[row["status"]] = row["c"]

    last_period_row = conn.execute(
        f"SELECT COUNT(*) FROM asks WHERE first_seen_at >= datetime('now', '-{_STATS_RECENT_DAYS} days')"
    ).fetchone()

    return {"by_status": by_status, f"last_{_STATS_RECENT_DAYS}d": last_period_row[0]}


def get_asks(
    status: Optional[str] = "open",
    blocking_activity_id: Optional[int] = None,
    triage_pending_only: bool = False,
    limit: int = 20,
    offset: int = 0,
    include_stats: bool = False,
) -> dict:
    """askを一覧・集計する。

    Args:
        status: フィルタ対象のstatus（"open"|"answered"|"promoted"|"dismissed"|"withdrawn"）。
            null指定で全status横断。triage_pending_only=Trueのときは無視される
        blocking_activity_id: 指定時はそのactivityをblockしているaskだけに絞る
        triage_pending_only: Trueでstatus='answered' AND triage IS NULLのみに絞る
            （statusの指定は無視される）
        limit: 取得件数上限（最大100件）
        offset: 取得開始位置
        include_stats: Trueのときstatus別クロス集計と直近30日サマリを付与

    Returns:
        {"asks": [...], "total_count": int, "stats": {...}(include_stats時)}
        失敗時: {"error": {"code": ..., "message": ...}}
        各askはidをid_rawへ退避しfingerprintを含まない。promoted_decision_idも
        他エンティティへの内部ID参照のためpromoted_decision_id_rawへ退避される。
        blocks/requestersが合流される（blocksの各要素はid_raw/title/status、
        requestersはsession_id文字列のリスト）。
    """
    if not triage_pending_only and status is not None and status not in VALID_STATUSES:
        return _validation_error(
            f"Invalid status: {status!r}. Must be one of {sorted(VALID_STATUSES)} or null"
        )

    limit = min(max(limit, 1), _MAX_LIMIT)
    offset = max(offset, 0)

    conn = get_connection()
    try:
        where_parts = []
        params: list = []
        if triage_pending_only:
            where_parts.append("a.status = 'answered' AND a.triage IS NULL")
        elif status is not None:
            where_parts.append("a.status = ?")
            params.append(status)
        if blocking_activity_id is not None:
            where_parts.append("a.id IN (SELECT ask_id FROM ask_blocks WHERE activity_id = ?)")
            params.append(blocking_activity_id)
        where_clause = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

        total_count = conn.execute(
            f"SELECT COUNT(*) FROM asks a {where_clause}", params
        ).fetchone()[0]

        rows = conn.execute(
            f"""
            SELECT a.* FROM asks a {where_clause}
            ORDER BY a.last_seen_at DESC, a.id DESC
            LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        ).fetchall()

        asks = [_build_ask_item(conn, row_to_dict(row)) for row in rows]

        result: dict = {"asks": asks, "total_count": total_count}
        if include_stats:
            result["stats"] = _compute_ask_stats(conn)
        return result
    except Exception as e:
        return {"error": {"code": "DATABASE_ERROR", "message": str(e)}}
    finally:
        conn.close()


# ========================================
# answer_ask
# ========================================


def answer_ask_with_conn(
    conn: sqlite3.Connection,
    ask_id: int,
    answer_body: str,
    session_id: Optional[str] = None,
) -> dict:
    """状態確認とUPDATEを1段クエリに畳んでopen→answeredに遷移する（TOCTOU回避）。

    トリアージ（promote/dismiss）はここでは実行しない。次のcheck_inで配達
    されるまで遅延する（判定はLLMの仕事のため）。

    Returns:
        成功時: {"id", "status": "answered", "triage_pending": True, "blocked_activities", "next_step"}
        失敗時: {"error": {"code": "VALIDATION_ERROR", "message": ...}}
    """
    answer_body = (answer_body or "").strip()
    if not answer_body:
        return _validation_error("answer_body must not be empty")
    if len(answer_body) > ANSWER_BODY_MAX_LEN:
        return _validation_error(f"answer_body must not exceed {ANSWER_BODY_MAX_LEN} characters")

    conn.execute("SAVEPOINT answer_ask")
    try:
        cursor = conn.execute(
            """
            UPDATE asks
            SET status = 'answered', answer_body = ?, answered_at = CURRENT_TIMESTAMP,
                answered_session_id = ?, last_seen_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'open'
            """,
            (answer_body, session_id, ask_id),
        )
        if cursor.rowcount == 0:
            conn.execute("RELEASE SAVEPOINT answer_ask")
            return _validation_error(f"ask id={ask_id} is not in 'open' status")

        blocked_rows = conn.execute(
            "SELECT activity_id FROM ask_blocks WHERE ask_id = ?", (ask_id,)
        ).fetchall()
        blocked_activities = [row["activity_id"] for row in blocked_rows]

        publish_entity_event_with_conn(conn, entity_type="ask", entity_id=ask_id, event="updated")
        conn.execute("RELEASE SAVEPOINT answer_ask")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT answer_ask")
        conn.execute("RELEASE SAVEPOINT answer_ask")
        raise

    return {
        "id": ask_id,
        "status": "answered",
        "triage_pending": True,
        "blocked_activities": blocked_activities,
        "next_step": "triage_askでpromote/dismissへ振り分けてください。",
    }


def answer_ask(ask_id: int, answer_body: str, session_id: Optional[str] = None) -> dict:
    conn = get_connection()
    try:
        result = answer_ask_with_conn(conn, ask_id, answer_body, session_id=session_id)
        if "error" in result:
            conn.rollback()
        else:
            conn.commit()
        return result
    except Exception as e:
        conn.rollback()
        return {"error": {"code": "DATABASE_ERROR", "message": str(e)}}
    finally:
        conn.close()


# ========================================
# triage_ask
# ========================================


def triage_ask_with_conn(
    conn: sqlite3.Connection,
    ask_id: int,
    action: str,
    decision: Optional[str] = None,
    reason: Optional[str] = None,
    title: Optional[str] = None,
    tags: Optional[list[str]] = None,
    dismiss_reason: Optional[str] = None,
    session_id: Optional[str] = None,
) -> dict:
    """answered状態のaskをpromote（decision化）またはdismissへトリアージする。

    promote失敗時（decision_service例外・並行更新によるTOCTOU検知）はSAVEPOINTで
    ask側のstatus変更もロールバックし、'answered'のまま残す。ただしdecision自体は
    decision_serviceが自前のconnで既にcommit済みのため、promote処理の途中で
    ask側の更新が失敗した場合、作成済みのdecisionはask未紐付けのまま残る
    （decision_serviceがconn共有版を提供していないための制約。極めて稀な
    競合時のみ発生し、孤立decision自体は無効なデータではない）。

    Returns:
        成功時(promote): {"id", "status": "promoted", "promoted_decision_id"}
        成功時(dismiss): {"id", "status": "dismissed"}
        失敗時: {"error": {"code": ..., "message": ...}}
    """
    if action not in ("promote", "dismiss"):
        return _validation_error(f"Invalid action: {action!r}. Must be 'promote' or 'dismiss'")

    pre_row = conn.execute("SELECT status, triage FROM asks WHERE id = ?", (ask_id,)).fetchone()
    if pre_row is None or pre_row["status"] != "answered" or pre_row["triage"] is not None:
        return _validation_error(
            f"ask id={ask_id} is not awaiting triage "
            "(must be status='answered' with triage not yet set)"
        )

    conn.execute("SAVEPOINT triage_ask")
    try:
        if action == "promote":
            if not decision or not decision.strip():
                raise ValueError("decision is required for action='promote'")
            if not reason or not reason.strip():
                raise ValueError("reason is required for action='promote'")

            decision_result = add_decisions(
                items=[{"decision": decision, "reason": reason, "title": title, "tags": tags}]
            )
            if decision_result.get("errors"):
                raise ValueError(
                    f"decision creation failed: {decision_result['errors'][0]['error']['message']}"
                )
            promoted_decision_id = decision_result["created"][0]["decision_id"]

            cursor = conn.execute(
                """
                UPDATE asks
                SET status = 'promoted', triage = 'promote', triaged_at = CURRENT_TIMESTAMP,
                    triaged_session_id = ?, promoted_decision_id = ?
                WHERE id = ? AND status = 'answered' AND triage IS NULL
                """,
                (session_id, promoted_decision_id, ask_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"ask id={ask_id} is no longer awaiting triage")

            conn.execute("DELETE FROM ask_blocks WHERE ask_id = ?", (ask_id,))
            publish_entity_event_with_conn(conn, entity_type="ask", entity_id=ask_id, event="updated")
            conn.execute("RELEASE SAVEPOINT triage_ask")
            return {"id": ask_id, "status": "promoted", "promoted_decision_id": promoted_decision_id}

        if not dismiss_reason or not dismiss_reason.strip():
            raise ValueError("dismiss_reason is required for action='dismiss'")

        cursor = conn.execute(
            """
            UPDATE asks
            SET status = 'dismissed', triage = 'dismiss', triaged_at = CURRENT_TIMESTAMP,
                triaged_session_id = ?, triage_reason = ?
            WHERE id = ? AND status = 'answered' AND triage IS NULL
            """,
            (session_id, dismiss_reason, ask_id),
        )
        if cursor.rowcount == 0:
            raise ValueError(f"ask id={ask_id} is no longer awaiting triage")

        conn.execute("DELETE FROM ask_blocks WHERE ask_id = ?", (ask_id,))
        publish_entity_event_with_conn(conn, entity_type="ask", entity_id=ask_id, event="updated")
        conn.execute("RELEASE SAVEPOINT triage_ask")
        return {"id": ask_id, "status": "dismissed"}

    except ValueError as e:
        conn.execute("ROLLBACK TO SAVEPOINT triage_ask")
        conn.execute("RELEASE SAVEPOINT triage_ask")
        return _validation_error(str(e))
    except Exception as e:
        conn.execute("ROLLBACK TO SAVEPOINT triage_ask")
        conn.execute("RELEASE SAVEPOINT triage_ask")
        return {"error": {"code": "DATABASE_ERROR", "message": str(e)}}


def triage_ask(
    ask_id: int,
    action: str,
    decision: Optional[str] = None,
    reason: Optional[str] = None,
    title: Optional[str] = None,
    tags: Optional[list[str]] = None,
    dismiss_reason: Optional[str] = None,
    session_id: Optional[str] = None,
) -> dict:
    conn = get_connection()
    try:
        result = triage_ask_with_conn(
            conn,
            ask_id,
            action,
            decision=decision,
            reason=reason,
            title=title,
            tags=tags,
            dismiss_reason=dismiss_reason,
            session_id=session_id,
        )
        if "error" in result:
            conn.rollback()
        else:
            conn.commit()
        return result
    except Exception as e:
        conn.rollback()
        return {"error": {"code": "DATABASE_ERROR", "message": str(e)}}
    finally:
        conn.close()


# ========================================
# withdraw_ask
# ========================================


def withdraw_ask_with_conn(
    conn: sqlite3.Connection,
    ask_id: int,
    reason: str,
    session_id: Optional[str] = None,
) -> dict:
    """openのaskを取り下げる（状態確認とUPDATEを1段クエリに畳む、TOCTOU回避）。

    ask_blocksは削除するが、ask_requestersは参照ログとして残す（削除しない）。

    Returns:
        成功時: {"id", "status": "withdrawn"}
        失敗時: {"error": {"code": "VALIDATION_ERROR", "message": ...}}
    """
    reason = (reason or "").strip()
    if not reason:
        return _validation_error("reason must not be empty")

    conn.execute("SAVEPOINT withdraw_ask")
    try:
        cursor = conn.execute(
            """
            UPDATE asks
            SET status = 'withdrawn', withdrawn_at = CURRENT_TIMESTAMP,
                withdrawn_session_id = ?, withdraw_reason = ?
            WHERE id = ? AND status = 'open'
            """,
            (session_id, reason, ask_id),
        )
        if cursor.rowcount == 0:
            conn.execute("RELEASE SAVEPOINT withdraw_ask")
            return _validation_error(f"ask id={ask_id} is not in 'open' status")

        conn.execute("DELETE FROM ask_blocks WHERE ask_id = ?", (ask_id,))
        publish_entity_event_with_conn(conn, entity_type="ask", entity_id=ask_id, event="updated")
        conn.execute("RELEASE SAVEPOINT withdraw_ask")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT withdraw_ask")
        conn.execute("RELEASE SAVEPOINT withdraw_ask")
        raise

    return {"id": ask_id, "status": "withdrawn"}


def withdraw_ask(ask_id: int, reason: str, session_id: Optional[str] = None) -> dict:
    conn = get_connection()
    try:
        result = withdraw_ask_with_conn(conn, ask_id, reason, session_id=session_id)
        if "error" in result:
            conn.rollback()
        else:
            conn.commit()
        return result
    except Exception as e:
        conn.rollback()
        return {"error": {"code": "DATABASE_ERROR", "message": str(e)}}
    finally:
        conn.close()


# ========================================
# check_inから呼ばれる配達クエリ
# ========================================


def get_pending_asks_with_conn(conn: sqlite3.Connection, activity_id: int) -> dict:
    """指定activityをblockしているaskを、フェーズ別に返す（check_inから呼ばれる）。

    activities.statusがcompleted以外（pending/in_progress/snoozed/shelved）の
    ときのみ配達する。

    Returns:
        {"awaiting_answer": [...], "awaiting_triage": [...]}
        awaiting_answer要素: {"id_raw", "question", "last_seen_at"}
        awaiting_triage要素: {"id_raw", "question", "answer_body", "last_seen_at"}
    """
    rows = conn.execute(
        """
        SELECT a.id, a.question, a.status, a.answer_body, a.triage, a.last_seen_at
        FROM asks a
        JOIN ask_blocks ab ON ab.ask_id = a.id
        JOIN activities act ON act.id = ab.activity_id
        WHERE ab.activity_id = ?
          AND a.status IN ('open', 'answered')
          AND act.status != 'completed'
        ORDER BY a.last_seen_at DESC
        """,
        (activity_id,),
    ).fetchall()

    awaiting_answer = []
    awaiting_triage = []
    for row in rows:
        r = row_to_dict(row)
        if r["status"] == "open":
            item = {"id": r["id"], "question": r["question"], "last_seen_at": r["last_seen_at"]}
            strip_entity_id_inplace(item)
            awaiting_answer.append(item)
        elif r["status"] == "answered" and r["triage"] is None:
            item = {
                "id": r["id"],
                "question": r["question"],
                "answer_body": r["answer_body"],
                "last_seen_at": r["last_seen_at"],
            }
            strip_entity_id_inplace(item)
            awaiting_triage.append(item)

    return {"awaiting_answer": awaiting_answer, "awaiting_triage": awaiting_triage}
