"""判断委譲（asks）の記録・状態遷移サービス。

AIエージェントが人間の判断を待つ問いを1件積み、人間が回答するだけで作業を
再開できるようにする受け皿。answer時点ではトリアージ（promote/dismiss）を
実行せず、次のcheck_inで配達されるまで遅延する（判定はLLMの仕事のため）。

状態遷移: open → answered → promoted/dismissed、open → withdrawn。
訂正（answered/promoted/dismissedの再答弁）は新規post（別ライフ、別行）で
行い、supersedesのようなリンクは張らない。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Optional

from src.db import get_connection, row_to_dict
from src.services import search_service
from src.services.decision_service import add_decisions
from src.services.dedup_helpers import compute_fingerprint16, normalize_text
from src.services.embedding_service import encode_document, insert_ask_embedding_with_conn
from src.services.readable_id import strip_entity_id_inplace
from src.services.relay.entity_publish import publish_entity_event_with_conn
from src.services.relay.runtime import notify_reconfigure_if_new
from src.services.relay.service import relay_subscribe
from src.services.tag_service import (
    get_entity_tags_batch,
    link_tags,
    resolve_tag_ids,
    resolve_tags,
    validate_and_parse_tags,
)

logger = logging.getLogger(__name__)

QUESTION_MAX_LEN = 500
CONTEXT_MAX_LEN = 8000
ANSWER_BODY_MAX_LEN = 8000
CHOICE_MAX_LEN = 100
CHOICES_MAX_COUNT = 3

# fingerprint単位でのwithdraw直後の再post拒否ウィンドウ（誤操作保護、session条件なし）。
WITHDRAW_COOLDOWN_MINUTES = 5

VALID_STATUSES = {"open", "answered", "promoted", "dismissed", "withdrawn"}
VALID_KINDS = {"ask", "meta"}

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
    tags: list[str],
    kind: str = "ask",
    context: Optional[str] = None,
    choices: Optional[list[str]] = None,
    session_id: Optional[str] = None,
) -> dict:
    """検証 + upsert + ask_blocks/ask_requestersのUNION追記をconn上で行う。

    commitは呼び出し側の責任（embedding生成の前段でコミットし、HTTP呼び出しの間
    書き込みトランザクションを開いたままにしないため。topic_service.add_topicと
    同じ二段コミットパターン）。tags/kindのフォーマット検証（必須・domain:必須・
    kind値チェック）はここで行うが、実際のタグ解決（`tag_service.resolve_tags`）と
    `ask_tags`への紐付けは呼び出し元の`add_ask`が最初のcommit後に行う
    （resolve_tagsは自前でconnを開いてcommitするため、ここでの未commitな書き込み
    トランザクション中に呼ぶと別connからのINSERTが `database is locked` になる）。

    kindはask新規作成（fingerprint一致なし）のときのみ適用される。dedup時
    （同一fingerprintのopen ask再post）は今回渡されたkindを無視し、初回投入時の
    値を保持する（判断が迷いうる点: dedupは同一問いの再出現であり、初回の分類が正で
    よいという方針を採用した）。choicesもdedup時は今回渡された値を無視し、初回投入時の
    値を保持する（kindと同じ考え方に揃える）。tagsの扱いは呼び出し元（add_ask）が
    ask_tagsの実在で判定する（本関数の責務外、add_askのdocstring参照）。

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
    if kind not in VALID_KINDS:
        return _validation_error(f"Invalid kind: {kind!r}. Must be one of {sorted(VALID_KINDS)}")

    if choices is not None:
        if not (1 <= len(choices) <= CHOICES_MAX_COUNT):
            return _validation_error(
                f"choices must contain between 1 and {CHOICES_MAX_COUNT} items"
            )
        stripped_choices = []
        for choice in choices:
            choice = (choice or "").strip()
            if not choice:
                return _validation_error("choices must not contain empty strings")
            if len(choice) > CHOICE_MAX_LEN:
                return _validation_error(
                    f"each choice must not exceed {CHOICE_MAX_LEN} characters"
                )
            stripped_choices.append(choice)
        choices = stripped_choices

    parsed_tags = validate_and_parse_tags(tags, required=True)
    if isinstance(parsed_tags, dict):
        return parsed_tags
    if not any(ns == "domain" for ns, _ in parsed_tags):
        return _validation_error("tags must include at least one 'domain:' tag")

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

    choices_json = json.dumps(choices, ensure_ascii=False) if choices is not None else None

    cursor = conn.execute(
        """
        INSERT INTO asks (question, context, fingerprint, kind, choices, first_seen_session_id, last_seen_session_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(fingerprint) WHERE status = 'open'
        DO UPDATE SET
            occurrence_count = asks.occurrence_count + 1,
            last_seen_at = CURRENT_TIMESTAMP,
            context = excluded.context,
            last_seen_session_id = excluded.last_seen_session_id
        RETURNING id, occurrence_count
        """,
        (question, context, fingerprint, kind, choices_json, session_id, session_id),
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
    tags: list[str],
    kind: str = "ask",
    context: Optional[str] = None,
    choices: Optional[list[str]] = None,
    session_id: Optional[str] = None,
) -> dict:
    """MCPツール本体。add_ask_with_connで書き込みcommit後、タグ解決・紐付け、
    embedding生成と近傍検索（similar_precedents/similar_asks）を行う。

    choices: 選択肢テンプレート（optional、最大3件、1件100字以内）。指定すると
        AskUserQuestion風の選択式UIをダッシュボード等で組み立てられる。回答
        （answer_ask）は引き続き自由文字列のまま。

    タグ解決（`tag_service.resolve_tags`）は、このask（ask_id）にまだ1件も
    タグが紐付いていない場合にのみ行う（occurrence_countではなくask_tagsの実在で
    判定する）。既にタグが付いているaskの再post（dedup）では今回渡されたtagsを
    無視し、既存の紐付けを保持する（add_ask_with_connのdocstring参照）。
    resolve_tagsは自前のconnでcommitするため、add_ask_with_connの書き込みが
    確定した後（＝この最初のcommit後）に呼ぶ。

    ask_tagsの実在で判定することで、resolve_tags失敗（DATABASE_ERROR等）により
    ask行だけが確定してタグが空のまま残ったケースでも、同じ問いを再postすれば
    （dedupで同一ask_idにヒットしてもタグ0件なので）タグ解決が再試行される
    （自己修復的リトライ）。resolve_tagsが失敗した場合、ask自体は既に作成済み
    （commit済み）のため、エラー応答に "id" を含めて呼び出し側が作成済みaskの
    存在を把握できるようにする。

    session_id指定時は、そのaskの個体専用label（ask:{id}）をrelay_subscribeする。
    relay未接続・エラー時は例外を投げず静かに無視し、ask作成自体の成否には影響しない。

    Returns:
        成功時: {"id", "deduped", "occurrence_count", "similar_precedents", "similar_asks"}
        失敗時: {"error": {"code": ..., "message": ...}}（ask作成後にタグ解決が
            失敗した場合は "id" も含む。ask自体は作成済みでタグは空のまま残る）
    """
    conn = get_connection()
    try:
        result = add_ask_with_conn(
            conn,
            question,
            blocks,
            tags,
            kind=kind,
            context=context,
            choices=choices,
            session_id=session_id,
        )
        if "error" in result:
            conn.rollback()
            return result
        conn.commit()

        ask_id = result["id"]

        if session_id:
            try:
                subscribe_result = relay_subscribe(
                    labels=[f"ask:{ask_id}"], caller_session_id=session_id
                )
                if "error" in subscribe_result:
                    logger.debug(
                        "relay_subscribe for ask_id=%s returned error, ignoring: %s",
                        ask_id, subscribe_result["error"],
                    )
                else:
                    notify_reconfigure_if_new(subscribe_result)
            except Exception:
                logger.debug(
                    "relay_subscribe raised for ask_id=%s, ignoring", ask_id, exc_info=True
                )

        has_tags = conn.execute(
            "SELECT 1 FROM ask_tags WHERE ask_id = ? LIMIT 1", (ask_id,)
        ).fetchone() is not None
        if not has_tags:
            resolved = resolve_tags(tags)
            if isinstance(resolved, dict):
                resolved["id"] = ask_id
                return resolved
            tag_ids, _merged_tags = resolved
            link_tags(conn, "ask_tags", "ask_id", ask_id, tag_ids)
            conn.commit()

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


def _build_ask_item(conn: sqlite3.Connection, ask: dict, tags: list[str]) -> dict:
    """ask 1件にblocks/requesters/tagsを合流し、内部専用フィールドを整形する。

    tags はタグ文字列のリストのみを合流する（タグnotesは返さない。決定済み仕様）。
    """
    ask.pop("fingerprint", None)
    ask_id = ask["id"]

    if ask.get("choices") is not None:
        ask["choices"] = json.loads(ask["choices"])

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

    ask["tags"] = tags

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
    tags: Optional[list[str]] = None,
    kind: Optional[str] = None,
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
        tags: タグ配列（optional。指定時はAND条件でフィルタ、未指定時は全件。
            空配列を明示指定した場合はadd_ask等と同じくTAGS_REQUIREDエラーになる）
        kind: フィルタ対象のkind（"ask"|"meta"）。Noneでフィルタなし
        limit: 取得件数上限（最大100件）
        offset: 取得開始位置
        include_stats: Trueのときstatus別クロス集計と直近30日サマリを付与

    Returns:
        {"asks": [...], "total_count": int, "stats": {...}(include_stats時)}
        失敗時: {"error": {"code": ..., "message": ...}}
        各askはidをid_rawへ退避しfingerprintを含まない。promoted_decision_idも
        他エンティティへの内部ID参照のためpromoted_decision_id_rawへ退避される。
        blocks/requesters/tags（タグ文字列のリスト。notesは含まない）が合流される
        （blocksの各要素はid_raw/title/status、requestersはsession_id文字列のリスト）。
    """
    if not triage_pending_only and status is not None and status not in VALID_STATUSES:
        return _validation_error(
            f"Invalid status: {status!r}. Must be one of {sorted(VALID_STATUSES)} or null"
        )
    if kind is not None and kind not in VALID_KINDS:
        return _validation_error(f"Invalid kind: {kind!r}. Must be one of {sorted(VALID_KINDS)} or null")

    parsed_tags = None
    if tags is not None:
        parsed_tags = validate_and_parse_tags(tags, required=True)
        if isinstance(parsed_tags, dict):
            return parsed_tags

    limit = min(max(limit, 1), _MAX_LIMIT)
    offset = max(offset, 0)

    conn = get_connection()
    try:
        # タグフィルタでask_idsを絞り込む（tags指定時のみ、AND条件）
        if parsed_tags is not None:
            tag_ids = resolve_tag_ids(conn, parsed_tags)
            if not tag_ids or len(tag_ids) < len(parsed_tags):
                empty_result: dict = {"asks": [], "total_count": 0}
                if include_stats:
                    empty_result["stats"] = _compute_ask_stats(conn)
                return empty_result
            tag_placeholders = ",".join("?" * len(tag_ids))
            ask_ids_rows = conn.execute(
                f"""
                SELECT ask_id FROM ask_tags
                WHERE tag_id IN ({tag_placeholders})
                GROUP BY ask_id
                HAVING COUNT(DISTINCT tag_id) = ?
                """,
                (*tag_ids, len(tag_ids)),
            ).fetchall()
            matched_ask_ids = [row["ask_id"] for row in ask_ids_rows]
            if not matched_ask_ids:
                empty_result = {"asks": [], "total_count": 0}
                if include_stats:
                    empty_result["stats"] = _compute_ask_stats(conn)
                return empty_result
        else:
            matched_ask_ids = None

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
        if kind is not None:
            where_parts.append("a.kind = ?")
            params.append(kind)
        if matched_ask_ids is not None:
            id_placeholders = ",".join("?" * len(matched_ask_ids))
            where_parts.append(f"a.id IN ({id_placeholders})")
            params.extend(matched_ask_ids)
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

        fetched_ids = [row["id"] for row in rows]
        tags_map = get_entity_tags_batch(conn, "ask_tags", "ask_id", fetched_ids)
        asks = [
            _build_ask_item(conn, row_to_dict(row), tags_map.get(row["id"], []))
            for row in rows
        ]

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
            decision = (decision or "").strip()
            reason = (reason or "").strip()
            if not decision:
                raise ValueError("decision is required for action='promote'")
            if not reason:
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

        dismiss_reason = (dismiss_reason or "").strip()
        if not dismiss_reason:
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
