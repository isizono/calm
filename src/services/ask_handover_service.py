"""askを「止めている作業を今担っている者」へ差し出すための読み取りクエリ。

check_inの応答枠、UserPromptSubmit hookの知らせ、update_activity(completed)の
応答が共有する。いずれもread-onlyで、回答本文（answer_body）は返さない。

「隣の作業」は、対象activityとgoalの親子関係（goal_conditionsがactivityを束縛する
関係）にある作業と、activity_dependenciesでつながる作業（向きは問わない）。
「待ち」の判定はaskのstatusで行う（ask_blocksはtriage後も残る）。
"""
from __future__ import annotations

import sqlite3

from src.services.readable_id import strip_entity_id_inplace

RECENT_SETTLED_DAYS = 7
QUESTION_PREVIEW_CHARS = 80
SETTLED_DETAIL_CHARS = 80


def _preview(text: str | None, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def get_neighbor_activity_ids(conn: sqlite3.Connection, activity_id: int) -> list[int]:
    """goalの親子とdepends_onでつながる作業のid一覧（自身は含まない、id昇順）。"""
    rows = conn.execute(
        """
        SELECT gc.bound_id AS id
          FROM goal_activities ga
          JOIN goal_conditions gc ON gc.goal_id = ga.goal_id
         WHERE ga.activity_id = :a AND gc.bound_type = 'activity'
        UNION
        SELECT ga.activity_id
          FROM goal_conditions gc
          JOIN goal_activities ga ON ga.goal_id = gc.goal_id
         WHERE gc.bound_type = 'activity' AND gc.bound_id = :a
        UNION
        SELECT dependency_id FROM activity_dependencies WHERE dependent_id = :a
        UNION
        SELECT dependent_id FROM activity_dependencies WHERE dependency_id = :a
        """,
        {"a": activity_id},
    ).fetchall()
    return sorted({r["id"] for r in rows if r["id"] != activity_id})


def _in_clause(ids: list[int]) -> str:
    return ",".join("?" * len(ids))


def get_pending_asks_blocking(conn: sqlite3.Connection, activity_id: int) -> list[dict]:
    """activityを止めている未決着のask（openまたはanswered未triage）。

    要素: {"id_raw", "question", "status"}。回答本文は含まない。
    """
    rows = conn.execute(
        """
        SELECT a.id, a.question, a.status
          FROM asks a JOIN ask_blocks ab ON ab.ask_id = a.id
         WHERE ab.activity_id = ?
           AND (a.status = 'open' OR (a.status = 'answered' AND a.triage IS NULL))
         ORDER BY a.id
        """,
        (activity_id,),
    ).fetchall()
    items = []
    for r in rows:
        item = {"id": r["id"], "question": _preview(r["question"], QUESTION_PREVIEW_CHARS), "status": r["status"]}
        strip_entity_id_inplace(item)
        items.append(item)
    return items


def get_neighbor_pending_asks(
    conn: sqlite3.Connection, activity_id: int, limit: int
) -> tuple[list[dict], int, list[int]]:
    """隣の作業を止めている未決・回答済み（未triage）のask。

    activity自身も止めているaskは含めない（自分の枠に出るため）。
    要素: {"id_raw", "question", "status", "activity": 作業の題}。
    Returns: (limit件までの要素, 超過件数, 超過したaskが止めている作業のid一覧)
    """
    neighbors = get_neighbor_activity_ids(conn, activity_id)
    if not neighbors:
        return [], 0, []
    rows = conn.execute(
        f"""
        SELECT a.id, a.question, a.status, MIN(act.id) AS activity_id, -- 単一のMINと並べた裸のカラムは最小行の値になる（SQLite）
              
               act.title AS title
          FROM asks a
          JOIN ask_blocks ab ON ab.ask_id = a.id
          JOIN activities act ON act.id = ab.activity_id
         WHERE ab.activity_id IN ({_in_clause(neighbors)})
           AND (a.status = 'open' OR (a.status = 'answered' AND a.triage IS NULL))
           AND a.id NOT IN (SELECT ask_id FROM ask_blocks WHERE activity_id = ?)
         GROUP BY a.id
         ORDER BY a.last_seen_at DESC, a.id DESC
        """,
        (*neighbors, activity_id),
    ).fetchall()
    items = []
    for r in rows[:limit]:
        item = {
            "id": r["id"],
            "question": _preview(r["question"], QUESTION_PREVIEW_CHARS),
            "status": r["status"],
            "activity": r["title"],
        }
        strip_entity_id_inplace(item)
        items.append(item)
    return items, max(len(rows) - limit, 0), list(dict.fromkeys(r["activity_id"] for r in rows[limit:]))


def get_recent_settled_asks(
    conn: sqlite3.Connection, activity_id: int, limit: int, days: int = RECENT_SETTLED_DAYS
) -> tuple[list[dict], int, list[int]]:
    """activityと隣の作業を止めていたaskのうち、triageから`days`日以内のもの。

    要素: {"id_raw", "question", "activity": 作業の題, "outcome": "promoted"|"dismissed",
    "detail": promoteなら昇格先decisionの見出し・dismissなら却下理由}。
    Returns: (limit件までの要素, 超過件数, 超過したaskが止めている作業のid一覧)
    """
    scope = [activity_id, *get_neighbor_activity_ids(conn, activity_id)]
    rows = conn.execute(
        f"""
        SELECT a.id, a.question, a.status, a.triage_reason,
               d.title AS decision_title, d.decision AS decision_text,
               MIN(act.id) AS activity_id, -- 単一のMINと並べた裸のカラムは最小行の値になる（SQLite）
              
               act.title AS title
          FROM asks a
          JOIN ask_blocks ab ON ab.ask_id = a.id
          JOIN activities act ON act.id = ab.activity_id
          LEFT JOIN decisions d ON d.id = a.promoted_decision_id
         WHERE ab.activity_id IN ({_in_clause(scope)})
           AND a.status IN ('promoted', 'dismissed')
           AND a.triaged_at >= datetime('now', ?)
         GROUP BY a.id
         ORDER BY a.triaged_at DESC, a.id DESC
        """,
        (*scope, f"-{days} days"),
    ).fetchall()
    items = []
    for r in rows[:limit]:
        if r["status"] == "promoted":
            detail = r["decision_title"] or r["decision_text"]
        else:
            detail = r["triage_reason"]
        item = {
            "id": r["id"],
            "question": _preview(r["question"], QUESTION_PREVIEW_CHARS),
            "activity": r["title"],
            "outcome": r["status"],
            "detail": _preview(detail, SETTLED_DETAIL_CHARS),
        }
        strip_entity_id_inplace(item)
        items.append(item)
    return items, max(len(rows) - limit, 0), list(dict.fromkeys(r["activity_id"] for r in rows[limit:]))


def get_asks_answered_since(
    conn: sqlite3.Connection, activity_id: int, since: str
) -> list[dict]:
    """activityと隣の作業を止めていたaskのうち、`since`（UTCの`YYYY-MM-DD HH:MM:SS`）以降に
    回答されたもの（秒精度のため、同一秒の回答を取りこぼさないよう境界を含める）（回答後にtriage済みのものも含む）。回答本文は含まない。

    要素: {"id_raw", "question", "status", "activity": 作業の題}。回答の新しい順。
    """
    scope = [activity_id, *get_neighbor_activity_ids(conn, activity_id)]
    rows = conn.execute(
        f"""
        SELECT a.id, a.question, a.status, MIN(act.title) AS title
          FROM asks a
          JOIN ask_blocks ab ON ab.ask_id = a.id
          JOIN activities act ON act.id = ab.activity_id
         WHERE ab.activity_id IN ({_in_clause(scope)})
           AND a.status IN ('answered', 'promoted', 'dismissed')
           AND a.answered_at >= ?
         GROUP BY a.id
         ORDER BY a.answered_at DESC, a.id DESC
        """,
        (*scope, since),
    ).fetchall()
    items = []
    for r in rows:
        item = {
            "id": r["id"],
            "question": _preview(r["question"], QUESTION_PREVIEW_CHARS),
            "status": r["status"],
            "activity": r["title"],
        }
        strip_entity_id_inplace(item)
        items.append(item)
    return items


def move_pending_asks_with_conn(
    conn: sqlite3.Connection,
    from_activity_id: int,
    to_activity_id: int,
    ask_ids: list[int] | None = None,
) -> dict:
    """from_activityを止めている未決着ask（openまたはanswered未triage）を
    to_activityへ付け替える（from側のblockは外す）。commitは呼び出し側。

    ask_idsを渡すとそのaskだけを動かす。from_activityを止めている未決着askでない
    idが含まれていれば何も動かさずエラーにする。省略時は未決着askを全件動かす。
    to_activityが存在しない・完了済み・from_activityと同じ場合もエラー。

    Returns:
        成功時: {"moved": [{"id_raw", "question", "status"}, ...]}（動かすaskが無ければ空リスト）
        失敗時: {"error": {"code": "VALIDATION_ERROR", "message": ...}}
    """
    def _err(message: str) -> dict:
        return {"error": {"code": "VALIDATION_ERROR", "message": message}}

    if to_activity_id == from_activity_id:
        return _err("move_asks_to must differ from the activity being updated")
    target = conn.execute("SELECT status FROM activities WHERE id = ?", (to_activity_id,)).fetchone()
    if target is None:
        return _err(f"move_asks_to references nonexistent activity id: {to_activity_id}")
    if target["status"] == "completed":
        return _err("move_asks_to must be an activity that is not completed")

    pending = get_pending_asks_blocking(conn, from_activity_id)
    if ask_ids is not None:
        pending_ids = {a["id_raw"] for a in pending}
        invalid = [i for i in ask_ids if i not in pending_ids]
        if invalid:
            return _err(
                f"move_ask_ids contains ask(s) not pending on this activity: {invalid}"
            )
        wanted = set(ask_ids)
        pending = [a for a in pending if a["id_raw"] in wanted]

    for a in pending:
        conn.execute(
            "INSERT OR IGNORE INTO ask_blocks (ask_id, activity_id) VALUES (?, ?)",
            (a["id_raw"], to_activity_id),
        )
        conn.execute(
            "DELETE FROM ask_blocks WHERE ask_id = ? AND activity_id = ?",
            (a["id_raw"], from_activity_id),
        )
    return {"moved": pending}
