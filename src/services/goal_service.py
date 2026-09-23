"""goal機構（goals / goal_conditions / goal_activities）のサービス。

set_goal・update_goal・judge_goal・get_goalの4ツールを実装する。書き込み系
3ツールは、読んでから書くまでを他の書き込みと直列化するため、どのDMLよりも
前にBEGIN IMMEDIATEを発行する。1回の呼び出しは1トランザクションで、1件でも
エラーなら何も書かない（エラーはconn.rollback()で戻す）。

goal ブロック（ラベル・次の一手・束縛先の状態）は保存せず、読み出すたびに
導出する（保存するのは条件の状態と goal の判定記録だけ）。束縛先の状態は、
goal の条件が何件あっても束縛先の型（activity/decision/ask、最大3種）ごとに
1本の問い合わせでまとめて読み、条件の数に比例して問い合わせが増えないように
してある（`_fetch_bound_states`）。

goal_activities は activity_id を単独主キーにした WITHOUT ROWID の表で、
1つの activity が属する goal は高々1つである。goal の書き込みツールは
どれもこの表と goals・goal_conditions・activities・signal_events だけを
書き、update_activity は呼ばない（自前の接続で完了をコミットするため、
判定・差し戻しと同じトランザクションにできない）。
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Optional

from src.config import GOAL_RECHECK_HOURS
from src.db import get_connection
from src.services import ask_service
from src.services.readable_id import strip_entity_id_inplace
from src.services.signal_service import record_signal

HANDLE_MAX_LEN = 40
_HANDLE_RE = re.compile(r"^[a-z0-9-]+$")

VALID_ACTORS = {"claude", "human", "external"}
VALID_STATES = {"open", "satisfied", "waived"}
VALID_BOUND_TYPES = {"activity", "decision", "ask"}
VALID_VERDICTS = {"achieved", "failed"}
VALID_JUDGED_BY = {"session", "human"}

_BOUND_TABLE = {
    "activity": "activities",
    "decision": "decisions",
    "ask": "asks",
}


def _validation_error(message: str) -> dict:
    return {"error": {"code": "VALIDATION_ERROR", "message": message}}


def _not_found(message: str) -> dict:
    return {"error": {"code": "NOT_FOUND", "message": message}}


def _database_error(message: str) -> dict:
    return {"error": {"code": "DATABASE_ERROR", "message": message}}


def _is_non_empty_str(value: object) -> bool:
    return isinstance(value, str) and value.strip() != ""


# ========================================
# 束縛先の実在・状態
# ========================================


def _bound_exists(conn: sqlite3.Connection, bound_type: str, bound_id: int) -> bool:
    table = _BOUND_TABLE[bound_type]
    row = conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (bound_id,)).fetchone()
    return row is not None


def _is_open_question_decision(row: sqlite3.Row) -> bool:
    """decisionの本文かtitleが「[議論中]」で始まるかを判定する。

    未決横断の一覧・完了確認の読み出しでもこの判定を再利用する（別実装を作らない）。
    """
    title = row["title"] or ""
    decision_text = row["decision"] or ""
    return title.startswith("[議論中]") or decision_text.startswith("[議論中]")


def _decision_is_done(conn: sqlite3.Connection, decision_id: int) -> bool:
    """束縛したdecisionが「済」かどうかを返す。

    通常のdecisionは「retract されておらず、生きた置き換えが無い」ときに済。
    [議論中] decisionは逆に「retract されておらず、生きた置き換えがある」ときに済。
    行が無い（削除された）decisionは済でないとして扱う（satisfied側の崩れの
    判定にそのまま合流させる）。
    """
    row = conn.execute(
        "SELECT retracted_at, title, decision FROM decisions WHERE id = ?",
        (decision_id,),
    ).fetchone()
    if row is None:
        return False
    has_living_replacement = (
        conn.execute(
            """
            SELECT 1 FROM decision_supersedes s
            JOIN decisions n ON n.id = s.source_id
            WHERE s.target_id = ? AND s.kind = 'replaces' AND n.retracted_at IS NULL
            LIMIT 1
            """,
            (decision_id,),
        ).fetchone()
        is not None
    )
    if _is_open_question_decision(row):
        return row["retracted_at"] is None and has_living_replacement
    return row["retracted_at"] is None and not has_living_replacement


# ========================================
# 条件の形の検証
# ========================================


def _validate_condition_form(condition: object) -> dict:
    """set_goal(new)・update_goal(add) が受け取る条件1件の形を検証し、正規化する。

    Returns:
        {"ok": True, "value": {statement, actor, state, note, bound_type, bound_id}}
        または {"ok": False, "error": <VALIDATION_ERROR dict>}
    """
    if not isinstance(condition, dict):
        return {"ok": False, "error": _validation_error("condition must be an object")}

    statement = condition.get("statement")
    if not _is_non_empty_str(statement):
        return {"ok": False, "error": _validation_error("condition.statement must be a non-empty string")}

    actor = condition.get("actor")
    if actor not in VALID_ACTORS:
        return {"ok": False, "error": _validation_error(f"condition.actor must be one of {sorted(VALID_ACTORS)}")}

    state = condition.get("state", "open")
    if state not in VALID_STATES:
        return {"ok": False, "error": _validation_error(f"condition.state must be one of {sorted(VALID_STATES)}")}

    note = condition.get("note")
    if note is not None and not isinstance(note, str):
        return {"ok": False, "error": _validation_error("condition.note must be a string")}
    if state == "waived" and not _is_non_empty_str(note):
        return {"ok": False, "error": _validation_error("condition.note is required when state='waived'")}

    bound = condition.get("bound")
    bound_type = None
    bound_id = None
    if bound is not None:
        if not isinstance(bound, dict):
            return {"ok": False, "error": _validation_error("condition.bound must be an object or null")}
        bound_type = bound.get("type")
        bound_id = bound.get("id")
        if bound_type not in VALID_BOUND_TYPES:
            return {"ok": False, "error": _validation_error(f"condition.bound.type must be one of {sorted(VALID_BOUND_TYPES)}")}
        if not isinstance(bound_id, int) or isinstance(bound_id, bool):
            return {"ok": False, "error": _validation_error("condition.bound.id must be an integer")}

    return {
        "ok": True,
        "value": {
            "statement": statement.strip(),
            "actor": actor,
            "state": state,
            "note": note,
            "bound_type": bound_type,
            "bound_id": bound_id,
        },
    }


def _insert_condition(conn: sqlite3.Connection, goal_id: int, value: dict) -> int:
    last_satisfied_at = "CURRENT_TIMESTAMP" if value["state"] == "satisfied" else None
    cursor = conn.execute(
        f"""
        INSERT INTO goal_conditions
            (goal_id, statement, actor, state, note, bound_type, bound_id, last_satisfied_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, {last_satisfied_at or 'NULL'})
        """,
        (
            goal_id,
            value["statement"],
            value["actor"],
            value["state"],
            value["note"],
            value["bound_type"],
            value["bound_id"],
        ),
    )
    return cursor.lastrowid


# ========================================
# set_goal
# ========================================


def _validate_handle(handle: object) -> Optional[dict]:
    if not isinstance(handle, str) or not _HANDLE_RE.match(handle):
        return _validation_error("handle must contain only lowercase letters, digits, and hyphens")
    if len(handle) > HANDLE_MAX_LEN:
        return _validation_error(f"handle must be {HANDLE_MAX_LEN} characters or fewer")
    return None


def _validate_goal_arg(goal: object) -> Optional[dict]:
    """set_goalのgoal引数（4形のいずれか）の形を検証する。中身の詳細検証は各分岐で行う。"""
    if goal is None:
        return None
    if not isinstance(goal, dict):
        return _validation_error("goal must be an object or null")
    keys = {k for k in ("new", "goal_id", "waiver") if k in goal}
    if len(keys) != 1:
        return _validation_error("goal must have exactly one of: new, goal_id, waiver")
    return None


def set_goal_with_conn(conn: sqlite3.Connection, activity_id: int, goal: Optional[dict], replace: bool = False) -> dict:
    err = _validate_goal_arg(goal)
    if err:
        return err

    form = None
    if goal is not None:
        form = "new" if "new" in goal else ("goal_id" if "goal_id" in goal else "waiver")

    if form == "new":
        new_spec = goal["new"]
        if not isinstance(new_spec, dict):
            return _validation_error("goal.new must be an object")
        handle_err = _validate_handle(new_spec.get("handle"))
        if handle_err:
            return handle_err
        statement = new_spec.get("statement")
        if not _is_non_empty_str(statement):
            return _validation_error("goal.new.statement must be a non-empty string")
        conditions = new_spec.get("conditions")
        if not isinstance(conditions, list) or len(conditions) == 0:
            return _validation_error("goal.new.conditions must be a non-empty list")
        normalized_conditions = []
        for cond in conditions:
            result = _validate_condition_form(cond)
            if not result["ok"]:
                return result["error"]
            normalized_conditions.append(result["value"])
        handle = new_spec["handle"]
        statement = statement.strip()
    elif form == "goal_id":
        target_goal_id = goal["goal_id"]
        if not isinstance(target_goal_id, int) or isinstance(target_goal_id, bool):
            return _validation_error("goal.goal_id must be an integer")
    elif form == "waiver":
        waiver_reason = goal["waiver"]
        if not _is_non_empty_str(waiver_reason):
            return _validation_error("goal.waiver must be a non-empty string")
        waiver_reason = waiver_reason.strip()

    activity_row = conn.execute("SELECT id FROM activities WHERE id = ?", (activity_id,)).fetchone()
    if activity_row is None:
        return _not_found(f"activity {activity_id} not found")

    target_goal_row = None
    if form == "goal_id":
        target_goal_row = conn.execute("SELECT * FROM goals WHERE id = ?", (target_goal_id,)).fetchone()
        if target_goal_row is None:
            return _not_found(f"goal {target_goal_id} not found")

    current_row = conn.execute(
        "SELECT * FROM goal_activities WHERE activity_id = ?", (activity_id,)
    ).fetchone()
    current_goal_row = None
    if current_row is not None and current_row["goal_id"] is not None:
        current_goal_row = conn.execute(
            "SELECT * FROM goals WHERE id = ?", (current_row["goal_id"],)
        ).fetchone()

    # 3. 同じ内容なら何もせずに成功を返す
    is_same = False
    if form is None:
        is_same = current_row is None
    elif form == "goal_id":
        is_same = current_row is not None and current_row["goal_id"] == target_goal_id
    elif form == "waiver":
        is_same = (
            current_row is not None
            and current_row["goal_id"] is None
            and current_row["waiver_reason"] == waiver_reason
        )
    elif form == "new":
        is_same = (
            current_goal_row is not None and current_goal_row["handle"] == handle
        )

    if is_same:
        result_goal_id = current_row["goal_id"] if current_row is not None else None
        result: dict = {"activity_id": activity_id, "no_op": True}
        if result_goal_id is not None:
            result["goal_id"] = result_goal_id
            strip_entity_id_inplace(result, "goal_id")
        strip_entity_id_inplace(result, "activity_id")
        return result

    # 4. 既に行があり、内容が違い、replace=false なら ACTIVITY_GOAL_EXISTS
    if current_row is not None and not replace:
        current: dict
        if current_row["goal_id"] is not None:
            current = {
                "goal_id": current_row["goal_id"],
                "handle": current_goal_row["handle"] if current_goal_row else None,
                "statement": current_goal_row["statement"] if current_goal_row else None,
            }
            strip_entity_id_inplace(current, "goal_id")
        else:
            current = {"waiver": current_row["waiver_reason"]}
        return {"info": "ACTIVITY_GOAL_EXISTS", "current": current}

    # 5. 付け先の goal か外す側の goal が判定済みなら GOAL_CLOSED
    if form == "goal_id" and target_goal_row["closed"] == 1:
        result = {"goal_id": target_goal_row["id"]}
        strip_entity_id_inplace(result, "goal_id")
        return {"info": "GOAL_CLOSED", "goal": result}
    if current_goal_row is not None and current_goal_row["closed"] == 1:
        result = {"goal_id": current_goal_row["id"]}
        strip_entity_id_inplace(result, "goal_id")
        return {"info": "GOAL_CLOSED", "goal": result}

    # 6. 未判定 goal の最後の activity を外す結果になるなら GOAL_WOULD_ORPHAN
    if current_row is not None and current_row["goal_id"] is not None:
        linked_count = conn.execute(
            "SELECT COUNT(*) AS c FROM goal_activities WHERE goal_id = ?",
            (current_row["goal_id"],),
        ).fetchone()["c"]
        if linked_count <= 1:
            return {
                "error": {
                    "code": "GOAL_WOULD_ORPHAN",
                    "message": "removing this activity would leave the goal with no linked activity",
                }
            }

    # 7. new: handle の重複、束縛先の実在
    new_goal_id = None
    if form == "new":
        taken = conn.execute("SELECT 1 FROM goals WHERE handle = ?", (handle,)).fetchone()
        if taken is not None:
            return {"error": {"code": "HANDLE_TAKEN", "message": f"handle {handle!r} is already used"}}
        for cond in normalized_conditions:
            if cond["bound_type"] is not None and not _bound_exists(conn, cond["bound_type"], cond["bound_id"]):
                return _not_found(f"{cond['bound_type']} {cond['bound_id']} not found")

        cursor = conn.execute(
            "INSERT INTO goals (handle, statement) VALUES (?, ?)", (handle, statement)
        )
        new_goal_id = cursor.lastrowid
        for cond in normalized_conditions:
            _insert_condition(conn, new_goal_id, cond)

    # 8. goal_activities への書き込み
    write_goal_id = new_goal_id if form == "new" else (target_goal_id if form == "goal_id" else None)
    write_waiver = waiver_reason if form == "waiver" else None

    if form is None:
        conn.execute("DELETE FROM goal_activities WHERE activity_id = ?", (activity_id,))
    elif current_row is None:
        conn.execute(
            "INSERT INTO goal_activities (activity_id, goal_id, waiver_reason) VALUES (?, ?, ?)",
            (activity_id, write_goal_id, write_waiver),
        )
    else:
        conn.execute(
            """
            UPDATE goal_activities
            SET goal_id = ?, waiver_reason = ?, added_at = CURRENT_TIMESTAMP
            WHERE activity_id = ?
            """,
            (write_goal_id, write_waiver, activity_id),
        )

    result = {"activity_id": activity_id}
    if write_goal_id is not None:
        result["goal_id"] = write_goal_id
        strip_entity_id_inplace(result, "goal_id")
    strip_entity_id_inplace(result, "activity_id")
    return result


def set_goal(activity_id: int, goal: Optional[dict], replace: bool = False) -> dict:
    """activityのgoal上の立場を決める（新規作成/既存への紐づけ/不要印/未定義への解除）。

    成功時は、このactivityを指定した読み出しとしてgoalブロック（規則1〜4を
    含めて評価）を添える。GOAL_CLOSEDは、対象のgoal自身のブロックを添える
    （このactivityの現在の立場ではない）。ACTIVITY_GOAL_EXISTSにはgoalブロックを
    添えない（既存の行をcurrentに同梱するだけ）。
    """
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = set_goal_with_conn(conn, activity_id, goal, replace)
        if "error" in result:
            conn.rollback()
            return result
        conn.commit()
        if result.get("info") == "GOAL_CLOSED":
            closed_goal_id = result["goal"]["goal_id_raw"]
            result["goal"] = build_goal_block_by_goal_id(conn, closed_goal_id)
        elif result.get("info") != "ACTIVITY_GOAL_EXISTS":
            result["goal"] = build_goal_block_for_activity(conn, activity_id)
        return result
    except sqlite3.Error as e:
        conn.rollback()
        return _database_error(str(e))
    finally:
        conn.close()


# ========================================
# update_goal
# ========================================


def _apply_change_add(conn: sqlite3.Connection, goal_id: int, change: dict) -> Optional[dict]:
    result = _validate_condition_form(change)
    if not result["ok"]:
        return result["error"]
    value = result["value"]
    if value["bound_type"] is not None and not _bound_exists(conn, value["bound_type"], value["bound_id"]):
        return _not_found(f"{value['bound_type']} {value['bound_id']} not found")
    _insert_condition(conn, goal_id, value)
    return None


def _get_condition_for_goal(conn: sqlite3.Connection, goal_id: int, condition_id: object) -> dict:
    """条件idの検証結果を返す。

    Returns: {"ok": True, "row": <Row>} | {"ok": False, "error": <error dict>}
    """
    if not isinstance(condition_id, int) or isinstance(condition_id, bool):
        return {"ok": False, "error": _validation_error("changes[].id must be an integer")}
    row = conn.execute("SELECT * FROM goal_conditions WHERE id = ?", (condition_id,)).fetchone()
    if row is None:
        return {"ok": False, "error": _not_found(f"condition {condition_id} not found")}
    if row["goal_id"] != goal_id:
        return {"ok": False, "error": _validation_error(f"condition {condition_id} does not belong to goal {goal_id}")}
    return {"ok": True, "row": row}


def _apply_change_set(conn: sqlite3.Connection, goal_id: int, change: dict) -> Optional[dict]:
    lookup = _get_condition_for_goal(conn, goal_id, change.get("id"))
    if not lookup["ok"]:
        return lookup["error"]
    row = lookup["row"]

    state = change.get("state")
    if state not in VALID_STATES:
        return _validation_error(f"changes[].state must be one of {sorted(VALID_STATES)}")
    note = change.get("note")
    if note is not None and not isinstance(note, str):
        return _validation_error("changes[].note must be a string")
    if state == "waived" and not _is_non_empty_str(note):
        return _validation_error("changes[].note is required when state='waived'")

    if state == "satisfied" and row["state"] != "satisfied":
        conn.execute(
            """
            UPDATE goal_conditions
            SET state = ?, note = ?, last_satisfied_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (state, note, row["id"]),
        )
    else:
        conn.execute(
            """
            UPDATE goal_conditions
            SET state = ?, note = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (state, note, row["id"]),
        )
    return None


def _apply_change_edit(conn: sqlite3.Connection, goal_id: int, change: dict) -> Optional[dict]:
    lookup = _get_condition_for_goal(conn, goal_id, change.get("id"))
    if not lookup["ok"]:
        return lookup["error"]
    row = lookup["row"]

    set_parts = []
    values: list = []

    if "actor" in change:
        actor = change["actor"]
        if actor not in VALID_ACTORS:
            return _validation_error(f"changes[].actor must be one of {sorted(VALID_ACTORS)}")
        set_parts.append("actor = ?")
        values.append(actor)

    if "bound" in change:
        bound = change["bound"]
        if bound is None:
            set_parts.append("bound_type = NULL, bound_id = NULL")
        else:
            if not isinstance(bound, dict):
                return _validation_error("changes[].bound must be an object or null")
            bound_type = bound.get("type")
            bound_id = bound.get("id")
            if bound_type not in VALID_BOUND_TYPES:
                return _validation_error(f"changes[].bound.type must be one of {sorted(VALID_BOUND_TYPES)}")
            if not isinstance(bound_id, int) or isinstance(bound_id, bool):
                return _validation_error("changes[].bound.id must be an integer")
            if not _bound_exists(conn, bound_type, bound_id):
                return _not_found(f"{bound_type} {bound_id} not found")
            set_parts.append("bound_type = ?, bound_id = ?")
            values.extend([bound_type, bound_id])

    set_parts.append("updated_at = CURRENT_TIMESTAMP")
    values.append(row["id"])
    conn.execute(f"UPDATE goal_conditions SET {', '.join(set_parts)} WHERE id = ?", tuple(values))
    return None


_CHANGE_APPLIERS = {"add": _apply_change_add, "set": _apply_change_set, "edit": _apply_change_edit}


def _validate_changes_structure(changes: list) -> Optional[dict]:
    if not isinstance(changes, list):
        return _validation_error("changes must be a list")
    seen: set[tuple[int, str]] = set()
    for change in changes:
        if not isinstance(change, dict):
            return _validation_error("each change must be an object")
        op = change.get("op")
        if op not in _CHANGE_APPLIERS:
            return _validation_error(f"changes[].op must be one of {sorted(_CHANGE_APPLIERS)}")
        if op in ("set", "edit"):
            condition_id = change.get("id")
            if isinstance(condition_id, int) and not isinstance(condition_id, bool):
                key = (condition_id, op)
                if key in seen:
                    return _validation_error(
                        f"duplicate op {op!r} for the same condition {condition_id} in one call"
                    )
                seen.add(key)
    return None


def update_goal_with_conn(
    conn: sqlite3.Connection,
    goal_id: int,
    changes: Optional[list[dict]] = None,
    statement: Optional[str] = None,
    reopen_reason: Optional[str] = None,
    session_id: Optional[str] = None,
) -> dict:
    changes = changes or []

    goal_row = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
    if goal_row is None:
        return _not_found(f"goal {goal_id} not found")

    if reopen_reason is not None and not _is_non_empty_str(reopen_reason):
        return _validation_error("reopen_reason must be a non-empty string")
    if statement is not None and not _is_non_empty_str(statement):
        return _validation_error("statement must be a non-empty string")

    if reopen_reason is not None:
        if goal_row["closed"] == 0:
            result = {"goal_id": goal_id}
            strip_entity_id_inplace(result, "goal_id")
            return {"info": "GOAL_ALREADY_OPEN", "goal": result}
    else:
        if goal_row["closed"] == 1 and (len(changes) > 0 or statement is not None):
            result = {"goal_id": goal_id}
            strip_entity_id_inplace(result, "goal_id")
            return {"info": "GOAL_CLOSED", "goal": result}

    structure_err = _validate_changes_structure(changes)
    if structure_err:
        return structure_err

    reopened_info = None
    if reopen_reason is not None:
        reopened_info = {
            "verdict": goal_row["verdict"],
            "judged_by": goal_row["judged_by"],
            "judged_at": goal_row["judged_at"],
            "judge_note": goal_row["judge_note"],
        }
        conn.execute("UPDATE goals SET closed = 0 WHERE id = ? AND closed = 1", (goal_id,))
        linked_activity_rows = conn.execute(
            "SELECT activity_id FROM goal_activities WHERE goal_id = ?", (goal_id,)
        ).fetchall()
        conn.execute(
            """
            UPDATE activities
            SET status = 'pending', updated_at = CURRENT_TIMESTAMP
            WHERE id IN (SELECT activity_id FROM goal_activities WHERE goal_id = ?)
              AND status = 'completed' AND closed_by = 'goal_judge'
            """,
            (goal_id,),
        )
        summary = f"goal判定の差し戻し: {goal_row['handle']}（{goal_row['judged_at']} の判定）"
        record_signal(
            "goal_rollback",
            summary,
            source="tool:update_goal",
            detail=reopen_reason,
            context=reopened_info,
            refs=[{"type": "activity", "id": r["activity_id"]} for r in linked_activity_rows],
            session_id=session_id,
            conn=conn,
        )

    if statement is not None:
        conn.execute("UPDATE goals SET statement = ? WHERE id = ?", (statement.strip(), goal_id))

    applied = 0
    for change in changes:
        applier = _CHANGE_APPLIERS[change["op"]]
        err = applier(conn, goal_id, change)
        if err:
            return err
        applied += 1

    result: dict = {"goal_id": goal_id, "applied": applied}
    if reopened_info is not None:
        result["reopened"] = reopened_info
    strip_entity_id_inplace(result, "goal_id")
    return result


def update_goal(
    goal_id: int,
    changes: Optional[list[dict]] = None,
    statement: Optional[str] = None,
    reopen_reason: Optional[str] = None,
    session_id: Optional[str] = None,
) -> dict:
    """条件の追加・状態の書き込み・担い手と束縛の変更、goalの一文の修正、差し戻しを行う。

    成功時・GOAL_CLOSED・GOAL_ALREADY_OPENのいずれも、このgoal自身のブロック
    （規則5〜14から評価。activity単位の規則1〜4は評価しない）を添える。
    """
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = update_goal_with_conn(conn, goal_id, changes, statement, reopen_reason, session_id)
        if "error" in result:
            conn.rollback()
            return result
        conn.commit()
        result["goal"] = build_goal_block_by_goal_id(conn, goal_id)
        return result
    except sqlite3.Error as e:
        conn.rollback()
        return _database_error(str(e))
    finally:
        conn.close()


# ========================================
# judge_goal
# ========================================


def judge_goal_with_conn(
    conn: sqlite3.Connection,
    goal_id: int,
    verdict: str,
    note: Optional[str] = None,
    judged_by: str = "session",
) -> dict:
    if verdict not in VALID_VERDICTS:
        return _validation_error(f"verdict must be one of {sorted(VALID_VERDICTS)}")
    if judged_by not in VALID_JUDGED_BY:
        return _validation_error(f"judged_by must be one of {sorted(VALID_JUDGED_BY)}")
    if verdict == "failed" and not _is_non_empty_str(note):
        return _validation_error("note is required when verdict='failed'")

    goal_row = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
    if goal_row is None:
        return _not_found(f"goal {goal_id} not found")

    if goal_row["closed"] == 1:
        result = {"goal_id": goal_id}
        strip_entity_id_inplace(result, "goal_id")
        return {"info": "GOAL_ALREADY_CLOSED", "goal": result}

    if verdict == "achieved":
        conditions = conn.execute(
            "SELECT * FROM goal_conditions WHERE goal_id = ?", (goal_id,)
        ).fetchall()
        open_conditions = [c for c in conditions if c["state"] == "open"]
        if open_conditions:
            return {
                "error": {
                    "code": "GOAL_NOT_READY",
                    "message": "goal has open conditions",
                    "open_conditions": [
                        {"id_raw": c["id"], "statement": c["statement"]} for c in open_conditions
                    ],
                }
            }
        satisfied_conditions = [c for c in conditions if c["state"] == "satisfied"]
        if not satisfied_conditions:
            return {
                "error": {
                    "code": "GOAL_NOTHING_SATISFIED",
                    "message": "goal has no satisfied conditions",
                }
            }
        decision_specs = [
            ("decision", c["bound_id"]) for c in satisfied_conditions if c["bound_type"] == "decision"
        ]
        decision_states = _fetch_bound_states(conn, decision_specs) if decision_specs else {}
        broken = [
            c
            for c in satisfied_conditions
            if c["bound_type"] == "decision"
            and decision_states[("decision", c["bound_id"])]["state"] != "done"
        ]
        if broken:
            return {
                "error": {
                    "code": "GOAL_BINDING_BROKEN",
                    "message": "a satisfied condition's decision binding is no longer done",
                    "broken_conditions": [
                        {"id_raw": c["id"], "statement": c["statement"]} for c in broken
                    ],
                }
            }

    cursor = conn.execute(
        """
        UPDATE goals
        SET closed = 1, verdict = ?, judged_by = ?, judged_at = CURRENT_TIMESTAMP, judge_note = ?
        WHERE id = ? AND closed = 0
        """,
        (verdict, judged_by, note, goal_id),
    )
    if cursor.rowcount == 0:
        result = {"goal_id": goal_id}
        strip_entity_id_inplace(result, "goal_id")
        return {"info": "GOAL_ALREADY_CLOSED", "goal": result}

    to_close = conn.execute(
        """
        SELECT a.id AS id, a.title AS title
        FROM activities a
        JOIN goal_activities ga ON ga.activity_id = a.id
        WHERE ga.goal_id = ? AND a.status <> 'completed'
        """,
        (goal_id,),
    ).fetchall()
    conn.execute(
        """
        UPDATE activities
        SET status = 'completed', closed_by = 'goal_judge', closed_reason = ?,
            closed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
        WHERE id IN (SELECT activity_id FROM goal_activities WHERE goal_id = ?)
          AND status <> 'completed'
        """,
        (note, goal_id),
    )

    closed_activities = []
    for row in to_close:
        item = {"id": row["id"], "title": row["title"]}
        strip_entity_id_inplace(item)
        closed_activities.append(item)

    result = {"goal_id": goal_id, "verdict": verdict, "closed_activities": closed_activities}
    strip_entity_id_inplace(result, "goal_id")
    return result


def judge_goal(
    goal_id: int,
    verdict: str,
    note: Optional[str] = None,
    judged_by: str = "session",
) -> dict:
    """goalの終了を明示的に判定して閉じる。紐づく未完了のactivityも同時に閉じる。

    成功時・GOAL_ALREADY_CLOSEDのいずれも、このgoal自身のブロック
    （label=closed。規則5〜14から評価）を添える。エラー
    （GOAL_NOT_READY・GOAL_NOTHING_SATISFIED・GOAL_BINDING_BROKEN等）には
    goalブロックを添えない（同梱する条件一覧で十分なため）。
    """
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = judge_goal_with_conn(conn, goal_id, verdict, note, judged_by)
        if "error" in result:
            conn.rollback()
            return result
        conn.commit()
        result["goal"] = build_goal_block_by_goal_id(conn, goal_id)
        return result
    except sqlite3.Error as e:
        conn.rollback()
        return _database_error(str(e))
    finally:
        conn.close()


# ========================================
# 束縛先の状態（読み出し側、goalごとに型別1本の問い合わせ）
# ========================================


def _fetch_bound_states(conn: sqlite3.Connection, bound_specs: list[tuple[str, int]]) -> dict:
    """複数の束縛先の現在状態を、束縛の型（最大3種）ごとに1本の問い合わせで読む。

    goalの条件が何件あっても、型の数だけ問い合わせを発行する（条件の数に比例しない）。
    decisionは、生きた置き換え（retractされていないdecisionからkind='replaces'の辺で
    直接指されていること）も同じ1本の問い合わせでまとめて読む。

    Returns:
        {(bound_type, bound_id): {"state": "done"|"pending"|"gone",
                                   "title": str | None,
                                   "reason": "retracted"|"replaced"|"withdrawn"|"missing"|None,
                                   "successor_title": str | None}}
        reasonはstate="gone"のときだけ意味を持つ。
    """
    result: dict = {}
    by_type: dict[str, set[int]] = {}
    for bound_type, bound_id in bound_specs:
        by_type.setdefault(bound_type, set()).add(bound_id)

    if "activity" in by_type:
        ids = sorted(by_type["activity"])
        placeholders = ",".join("?" * len(ids))
        found = {
            r["id"]: r
            for r in conn.execute(
                f"SELECT id, title, status FROM activities WHERE id IN ({placeholders})", ids
            ).fetchall()
        }
        for i in ids:
            row = found.get(i)
            if row is None:
                result[("activity", i)] = {"state": "gone", "title": None, "reason": "missing", "successor_title": None}
            else:
                state = "done" if row["status"] == "completed" else "pending"
                result[("activity", i)] = {"state": state, "title": row["title"], "reason": None, "successor_title": None}

    if "ask" in by_type:
        ids = sorted(by_type["ask"])
        placeholders = ",".join("?" * len(ids))
        found = {
            r["id"]: r
            for r in conn.execute(
                f"SELECT id, question, status FROM asks WHERE id IN ({placeholders})", ids
            ).fetchall()
        }
        for i in ids:
            row = found.get(i)
            if row is None:
                result[("ask", i)] = {"state": "gone", "title": None, "reason": "missing", "successor_title": None}
            elif row["status"] == "open":
                result[("ask", i)] = {"state": "pending", "title": row["question"], "reason": None, "successor_title": None}
            elif row["status"] == "withdrawn":
                result[("ask", i)] = {"state": "gone", "title": row["question"], "reason": "withdrawn", "successor_title": None}
            else:
                result[("ask", i)] = {"state": "done", "title": row["question"], "reason": None, "successor_title": None}

    if "decision" in by_type:
        ids = sorted(by_type["decision"])
        placeholders = ",".join("?" * len(ids))
        found = {
            r["id"]: r
            for r in conn.execute(
                f"SELECT id, title, decision, retracted_at FROM decisions WHERE id IN ({placeholders})", ids
            ).fetchall()
        }
        replacement_rows = conn.execute(
            f"""
            SELECT s.target_id AS target_id, n.id AS source_id,
                   n.title AS source_title, n.decision AS source_decision
            FROM decision_supersedes s
            JOIN decisions n ON n.id = s.source_id
            WHERE s.target_id IN ({placeholders}) AND s.kind = 'replaces' AND n.retracted_at IS NULL
            """,
            ids,
        ).fetchall()
        living_replacement: dict[int, dict] = {}
        for r in replacement_rows:
            current = living_replacement.get(r["target_id"])
            if current is None or r["source_id"] > current["source_id"]:
                living_replacement[r["target_id"]] = {
                    "source_id": r["source_id"],
                    "title": r["source_title"] or r["source_decision"],
                }

        for i in ids:
            row = found.get(i)
            if row is None:
                result[("decision", i)] = {"state": "gone", "title": None, "reason": "missing", "successor_title": None}
                continue
            title = row["title"] or row["decision"]
            repl = living_replacement.get(i)
            retracted = row["retracted_at"] is not None
            if _is_open_question_decision(row):
                if retracted:
                    state, reason = "gone", "retracted"
                elif repl is not None:
                    state, reason = "done", None
                else:
                    state, reason = "pending", None
            else:
                if retracted:
                    state, reason = "gone", "retracted"
                elif repl is not None:
                    # 通常decisionはpending状態を持たない。生きた置き換えは崩れとして出る
                    state, reason = "gone", "replaced"
                else:
                    state, reason = "done", None
            result[("decision", i)] = {
                "state": state,
                "title": title,
                "reason": reason,
                "successor_title": repl["title"] if repl is not None else None,
            }

    return result


def _condition_flags(cond_row, bound_state: Optional[dict]) -> list[str]:
    """条件1行の4フラグ（reopened/broken/bound_done/recheck）を導く。

    崩れ（broken）は、充足済みの条件がdecisionを束縛している場合だけ検査する
    （activity/askの束縛は、read-for-checkinが子activityをin_progressへ戻す
    副作用と地続きになり、正しく達成したgoalが閉じられなくなるため対象外とする）。
    open側の崩れ（束縛先が消えた）は型を問わない。
    """
    flags = []
    if cond_row["state"] == "open" and cond_row["last_satisfied_at"] is not None:
        flags.append("reopened")

    broken = False
    if cond_row["state"] == "satisfied" and cond_row["bound_type"] == "decision":
        broken = bound_state is None or bound_state["state"] != "done"
    elif cond_row["state"] == "open" and cond_row["bound_type"] is not None:
        broken = bound_state is not None and bound_state["state"] == "gone"
    if broken:
        flags.append("broken")

    if cond_row["state"] == "open" and bound_state is not None and bound_state["state"] == "done":
        flags.append("bound_done")

    if cond_row["state"] == "open" and cond_row["is_recheck"]:
        flags.append("recheck")

    return flags


def _load_conditions(conn: sqlite3.Connection, goal_id: int):
    """goalの全条件をid順で読む。recheck判定用にis_recheckをSQLで一緒に計算する。"""
    return conn.execute(
        """
        SELECT *,
               CASE WHEN state = 'open' AND actor IN ('human', 'external')
                        AND updated_at < datetime('now', '-' || ? || ' hours')
                    THEN 1 ELSE 0 END AS is_recheck
        FROM goal_conditions
        WHERE goal_id = ?
        ORDER BY id
        """,
        (GOAL_RECHECK_HOURS, goal_id),
    ).fetchall()


def _enrich_conditions(condition_rows, bound_states: dict) -> list[dict]:
    """条件行に束縛先の状態とフラグを合流させ、扱いやすい辞書のリストにする。"""
    enriched = []
    for c in condition_rows:
        bound_state = bound_states.get((c["bound_type"], c["bound_id"])) if c["bound_type"] else None
        enriched.append(
            {
                "id": c["id"],
                "statement": c["statement"],
                "actor": c["actor"],
                "state": c["state"],
                "note": c["note"],
                "last_satisfied_at": c["last_satisfied_at"],
                "updated_at": c["updated_at"],
                "bound_type": c["bound_type"],
                "bound_id": c["bound_id"],
                "bound_state": bound_state,
                "flags": _condition_flags(c, bound_state),
            }
        )
    return enriched


def _bound_states_for_conditions(conn: sqlite3.Connection, condition_rows) -> dict:
    bound_specs = [(c["bound_type"], c["bound_id"]) for c in condition_rows if c["bound_type"] is not None]
    if not bound_specs:
        return {}
    return _fetch_bound_states(conn, bound_specs)


# ========================================
# 判定待ちの未決（open_questions）
# ========================================


def _open_questions_for_activities(conn: sqlite3.Connection, activity_ids: list[int]) -> list[dict]:
    """判定待ちのとき、閉じることになるactivityに紐づく未決を読む。

    未決は、それらのactivityをブロックしているstatus='open'のaskと、
    relationsのrelated（source_type='activity'の側がそれらのactivity、
    target_type='decision'）で結ばれたdecisionのうち、文頭が「[議論中]」で
    まだ結論から置き換えられていない（pending）ものである。
    """
    if not activity_ids:
        return []
    placeholders = ",".join("?" * len(activity_ids))
    items: list[dict] = []

    ask_rows = conn.execute(
        f"""
        SELECT DISTINCT a.id, a.question
        FROM asks a
        JOIN ask_blocks ab ON ab.ask_id = a.id
        WHERE ab.activity_id IN ({placeholders}) AND a.status = 'open'
        ORDER BY a.id
        """,
        activity_ids,
    ).fetchall()
    for r in ask_rows:
        items.append({"type": "ask", "id": r["id"], "title": r["question"]})

    decision_rows = conn.execute(
        f"""
        SELECT DISTINCT d.id, d.title, d.decision, d.retracted_at
        FROM decisions d
        JOIN relations r ON r.source_type = 'activity' AND r.target_type = 'decision'
                         AND r.target_id = d.id AND r.relation_type = 'related'
        WHERE r.source_id IN ({placeholders})
        ORDER BY d.id
        """,
        activity_ids,
    ).fetchall()
    for r in decision_rows:
        if not _is_open_question_decision(r):
            continue
        if r["retracted_at"] is not None:
            continue
        if _decision_is_done(conn, r["id"]):
            continue
        items.append({"type": "decision", "id": r["id"], "title": r["title"] or r["decision"]})

    for item in items:
        strip_entity_id_inplace(item)
    return items


# ========================================
# goal ブロックの組み立て（次の一手の選択規則1〜14）
# ========================================

_REMAINING_MAX = 3
_OTHER_ACTIVITIES_MAX = 3
_OPEN_QUESTIONS_MAX = 3
_GOAL_BLOCK_BUDGET_CHARS = 800

_UNDEFINED_NEXT = {
    "rule": 4,
    "what": (
        "終了条件が未定義。真偽の付く終了条件が言われている・推せるならset_goalで書く"
        "（追認は不要）。候補が複数で1つに定まらない、またはこの活動には終わりが"
        "あるはずだが何なのか推せないときはユーザーに聞き、定まれば書く。"
        "終わりの無い活動なら不要印を付ける。聞いても定まらない、または"
        "そもそも判断材料が無いときは何もしない"
    ),
    "actor": "claude",
}


def _activity_scope_next(pending_asks: Optional[dict]) -> Optional[dict]:
    """規則1（回答待ちのask）・規則2（振り分け待ちのask）。

    activityを指定した読み出しでだけ評価する。completedのactivityでは
    pending_asksが空になるので（`ask_service.get_pending_asks_with_conn`が
    completedを除外する）、自然に一致しない。
    """
    if pending_asks is None:
        return None
    answer = pending_asks.get("awaiting_answer") or []
    if answer:
        return {"rule": 1, "what": f"人間の回答待ち: {answer[0]['question']}", "actor": "human"}
    triage = pending_asks.get("awaiting_triage") or []
    if triage:
        return {"rule": 2, "what": f"triage_askで振り分ける: {triage[0]['question']}", "actor": "claude"}
    return None


def _rule6_what(cond: dict) -> str:
    if cond["state"] == "satisfied":
        bound_state = cond["bound_state"] or {}
        reason = bound_state.get("reason")
        if reason == "replaced":
            succ = bound_state.get("successor_title") or "?"
            phrase = f"後継に置き換えられた（後継『{succ}』）"
        elif reason == "retracted":
            phrase = "撤回された"
        else:
            phrase = "崩れた"
        return (
            f"「{cond['statement']}」の束縛先が{phrase}。後継でも満たすなら束縛を張り替え、"
            "満たさなければopenに戻す"
        )
    return f"「{cond['statement']}」の束縛先が消えた。束縛を外すかwaivedにする"


def _rule_context(goal_row, conditions: list[dict], activity_scoped: bool) -> dict:
    """規則5〜14の各一致条件が参照する集合を、条件リストから1回だけ切り出す。"""
    open_conds = [c for c in conditions if c["state"] == "open"]
    return {
        "goal_row": goal_row,
        "activity_scoped": activity_scoped,
        "open": open_conds,
        "broken": [c for c in conditions if "broken" in c["flags"]],
        "reopened_claude": [c for c in conditions if c["actor"] == "claude" and "reopened" in c["flags"]],
        "satisfied": [c for c in conditions if c["state"] == "satisfied"],
        "bound_done": [c for c in conditions if "bound_done" in c["flags"]],
        "claude_open": [c for c in open_conds if c["actor"] == "claude"],
        "claude_total": [c for c in conditions if c["actor"] == "claude"],
        "recheck": [c for c in open_conds if "recheck" in c["flags"]],
    }


def _rule5_judged(ctx: dict) -> dict:
    goal_row = ctx["goal_row"]
    what = f"判定済み（{goal_row['verdict']}・{goal_row['judged_at']}）。"
    if ctx["activity_scoped"]:
        what += "このactivityがcompletedでなく残作業が無ければ、update_activity(status=completed)で閉じ直す。"
    what += "判定が誤りならupdate_goal(reopen_reason=...)で差し戻す。別の終わりを目指すなら新しいactivityを起票する"
    return {"rule": 5, "what": what, "actor": "claude"}


def _rule6_broken(ctx: dict) -> dict:
    chosen = sorted(
        ctx["broken"], key=lambda c: (c["last_satisfied_at"] is None, c["last_satisfied_at"] or "", c["id"])
    )[0]
    return {"rule": 6, "what": _rule6_what(chosen), "actor": "claude", "condition_id_raw": chosen["id"]}


def _rule7_reopened(ctx: dict) -> dict:
    chosen = sorted(ctx["reopened_claude"], key=lambda c: (c["last_satisfied_at"] or "", c["id"]))[0]
    return {"rule": 7, "what": chosen["statement"], "actor": "claude", "condition_id_raw": chosen["id"]}


def _rule8_achieved_ready(ctx: dict) -> dict:
    what = (
        f"全条件が終端し、充足は{len(ctx['satisfied'])}件。judge_goal(achieved)で閉じる。"
        "judge_noteに何をもって達成としたかを1文で書いてよい。閉じるactivityに未決"
        "（open_questions）が残っていれば、先に畳むかユーザーに1ターン聞く"
    )
    return {"rule": 8, "what": what, "actor": "claude"}


def _rule9_nothing_satisfied(ctx: dict) -> dict:
    what = (
        "1件も充足せずに全条件が終端した。judge_goal(failed, 理由)で閉じるか、条件を足す。"
        "閉じるactivityに未決（open_questions）が残っていれば、先に畳むかユーザーに1ターン聞く"
    )
    return {"rule": 9, "what": what, "actor": "claude"}


def _rule10_bound_done(ctx: dict) -> dict:
    chosen = sorted(ctx["bound_done"], key=lambda c: c["id"])[0]
    return {
        "rule": 10,
        "what": f"「{chosen['statement']}」の束縛先は済んでいる。確かめてsatisfiedに書く",
        "actor": "claude",
        "condition_id_raw": chosen["id"],
    }


def _rule11_claude_turn(ctx: dict) -> dict:
    chosen = sorted(ctx["claude_open"], key=lambda c: c["id"])[0]
    what = chosen["statement"]
    bound_state = chosen["bound_state"]
    if chosen["bound_type"] == "activity" and bound_state and bound_state.get("title"):
        what = f"activity『{bound_state['title']}』を進める"
    return {"rule": 11, "what": what, "actor": "claude", "condition_id_raw": chosen["id"]}


def _rule12_no_stop_line(ctx: dict) -> dict:
    return {
        "rule": 12,
        "what": "停止線が書かれていない。Claudeが自力で到達できる条件を1本書く",
        "actor": "claude",
    }


def _rule13_recheck(ctx: dict) -> dict:
    chosen = sorted(ctx["recheck"], key=lambda c: c["id"])[0]
    what = (
        f"「{chosen['statement']}」は最後の確認から時間が経っている。確かめ、"
        "済んでいればsatisfiedに書いて事実をmaterialかlogに残す。未決着なら確認済み"
        "（state=open）を記録して待つ"
    )
    return {"rule": 13, "what": what, "actor": "claude", "condition_id_raw": chosen["id"]}


def _rule14_waiting(ctx: dict) -> dict:
    chosen = sorted(ctx["open"], key=lambda c: c["id"])[0]
    return {"rule": 14, "what": f"待ち: {chosen['statement']}", "actor": chosen["actor"], "condition_id_raw": chosen["id"]}


# 規則5〜14。上から順に評価し、最初に一致した1件だけを返す。各行は
# (規則番号, 一致条件, 返すものの組み立て) の3つ組で、規則の追加・並べ替えは
# ここに1行足すか動かすだけでよい。
_GOAL_SCOPE_RULES: list[tuple[int, "callable", "callable"]] = [
    (5, lambda ctx: ctx["goal_row"]["closed"] == 1, _rule5_judged),
    (6, lambda ctx: bool(ctx["broken"]), _rule6_broken),
    (7, lambda ctx: bool(ctx["reopened_claude"]), _rule7_reopened),
    (8, lambda ctx: not ctx["open"] and bool(ctx["satisfied"]), _rule8_achieved_ready),
    (9, lambda ctx: not ctx["open"] and not ctx["satisfied"], _rule9_nothing_satisfied),
    (10, lambda ctx: bool(ctx["bound_done"]), _rule10_bound_done),
    (11, lambda ctx: bool(ctx["claude_open"]), _rule11_claude_turn),
    (12, lambda ctx: not ctx["claude_total"], _rule12_no_stop_line),
    (13, lambda ctx: bool(ctx["recheck"]), _rule13_recheck),
    (14, lambda ctx: True, _rule14_waiting),
]


def _select_next_goal_scope(goal_row, conditions: list[dict], *, activity_scoped: bool) -> dict:
    """規則5〜14（図2）。goal_id/handleで指した読み出しはこの入口から評価し、
    activity_idで指した読み出しは規則1〜4が一致しなかったときにこの入口へ進む。
    """
    ctx = _rule_context(goal_row, conditions, activity_scoped)
    for _rule_number, predicate, build in _GOAL_SCOPE_RULES:
        if predicate(ctx):
            return build(ctx)
    raise AssertionError("規則14が常にTrueを返すため、ここには到達しない")


def _bound_display(bound_type: str, bound_state: Optional[dict]) -> Optional[str]:
    """goalブロック表示用の束縛先1行 `"<型>『<タイトル>』: <済/未/崩れの理由>"` を作る。"""
    if bound_state is None:
        return None
    title = bound_state.get("title") or "?"
    state = bound_state["state"]
    if state == "done":
        return f"{bound_type}『{title}』: 済"
    if state == "pending":
        return f"{bound_type}『{title}』: 未"
    reason = bound_state.get("reason")
    if reason == "replaced":
        succ = bound_state.get("successor_title") or "?"
        return f"{bound_type}『{title}』: 置き換え済み（後継『{succ}』）"
    if reason == "retracted":
        return f"{bound_type}『{title}』: 撤回済み"
    if reason == "withdrawn":
        return f"{bound_type}『{title}』: 取り下げ済み"
    return f"{bound_type}『{title}』: 崩れ（束縛先が消えた）"


def _condition_entry(cond: dict, *, terminal: bool = False) -> dict:
    """remaining用は{id, statement, actor, state, flags, bound}、terminal用は
    {id, statement, state, note, bound}を返す（actor/flagsはremaining専用）。
    """
    entry: dict = {"id": cond["id"], "statement": cond["statement"]}
    if not terminal:
        entry["actor"] = cond["actor"]
    entry["state"] = cond["state"]
    if not terminal and cond["flags"]:
        entry["flags"] = cond["flags"]
    if terminal and cond["note"] is not None:
        entry["note"] = cond["note"]
    if cond["bound_type"] is not None:
        bound_disp = _bound_display(cond["bound_type"], cond["bound_state"])
        if bound_disp:
            entry["bound"] = bound_disp
    strip_entity_id_inplace(entry)
    return entry


def _activity_ref(row) -> dict:
    d = {"id": row["id"], "title": row["title"], "status": row["status"]}
    strip_entity_id_inplace(d)
    return d


def _approx_chars(block: dict) -> int:
    return len(json.dumps(block, ensure_ascii=False))


def _fold_to_budget(block: dict) -> None:
    """goalブロックが目安の800字を超えそうなとき、other_activities→remaining→
    terminalの順に、リストを件数の表示へ畳む（in-place）。statementと条件文は
    ここでは一切削らない（畳むのはリストの掲載件数だけ）。
    """
    if _approx_chars(block) <= _GOAL_BLOCK_BUDGET_CHARS:
        return
    for key in ("other_activities", "remaining", "terminal"):
        value = block.get(key)
        if isinstance(value, list):
            block[key] = f"{len(value)}件"
            if key == "remaining":
                block.pop("others", None)
            if _approx_chars(block) <= _GOAL_BLOCK_BUDGET_CHARS:
                return


def _goal_core_fields(goal_row, conditions: list[dict], next_info: dict) -> tuple[dict, str]:
    """goalブロック・get_goalの両方が共有する核（label・progress・claude内訳・
    last_verdict）を組み立てる。
    """
    open_conds = [c for c in conditions if c["state"] == "open"]
    broken_conds = [c for c in conditions if "broken" in c["flags"]]
    terminal_conds = [c for c in conditions if c["state"] in ("satisfied", "waived")]
    claude_conds = [c for c in conditions if c["actor"] == "claude"]
    claude_terminal = [c for c in claude_conds if c["state"] in ("satisfied", "waived")]
    label = "closed" if goal_row["closed"] == 1 else ("judge_ready" if not open_conds and not broken_conds else "active")

    block: dict = {
        "goal_id": goal_row["id"],
        "handle": goal_row["handle"],
        "statement": goal_row["statement"],
        "label": label,
        "progress": f"{len(terminal_conds)}/{len(conditions)}",
        "claude": f"claude条件 {len(claude_conds)}件中{len(claude_terminal)}件終端",
        "next": next_info,
    }
    strip_entity_id_inplace(block, "goal_id")
    if goal_row["judged_at"] is not None:
        block["last_verdict"] = {
            "verdict": goal_row["verdict"],
            "judged_by": goal_row["judged_by"],
            "judged_at": goal_row["judged_at"],
            "judge_note": goal_row["judge_note"],
        }
    return block, label


def _linked_activity_rows(conn: sqlite3.Connection, goal_id: int):
    return conn.execute(
        """
        SELECT a.id, a.title, a.status
        FROM activities a
        JOIN goal_activities ga ON ga.activity_id = a.id
        WHERE ga.goal_id = ?
        ORDER BY a.id
        """,
        (goal_id,),
    ).fetchall()


def _assemble_goal_block(
    conn: sqlite3.Connection,
    goal_row,
    conditions: list[dict],
    next_info: dict,
    linked_rows,
    *,
    exclude_activity_id: Optional[int] = None,
) -> dict:
    """check_in・set_goal・update_goal・judge_goal・goal_hintが共有するgoalブロックを
    組み立てる（remaining/terminal/other_activities/open_questionsの畳み込みを含む）。
    """
    block, label = _goal_core_fields(goal_row, conditions, next_info)

    remaining_pool = [c for c in conditions if c["state"] == "open" or "broken" in c["flags"]]
    next_cid = next_info.get("condition_id_raw")
    remaining_sorted = sorted(
        remaining_pool,
        key=lambda c: (0 if c["id"] == next_cid else 1, 0 if "broken" in c["flags"] else 1, c["id"]),
    )
    if remaining_sorted:
        shown = remaining_sorted[:_REMAINING_MAX]
        block["remaining"] = [_condition_entry(c) for c in shown]
        overflow = len(remaining_sorted) - len(shown)
        if overflow > 0:
            block["others"] = f"他 {overflow} 件"

    if len(linked_rows) > 1:
        others_rows = [r for r in linked_rows if r["id"] != exclude_activity_id]
        if others_rows:
            block["other_activities"] = [_activity_ref(r) for r in others_rows[:_OTHER_ACTIVITIES_MAX]]

    if label == "judge_ready":
        block["terminal"] = [_condition_entry(c, terminal=True) for c in conditions]
        closing_ids = [r["id"] for r in linked_rows if r["status"] != "completed"]
        open_qs = _open_questions_for_activities(conn, closing_ids)
        if open_qs:
            shown_oq = open_qs[:_OPEN_QUESTIONS_MAX]
            block["open_questions"] = shown_oq
            more = len(open_qs) - len(shown_oq)
            if more > 0:
                block["open_questions_more"] = more

    _fold_to_budget(block)
    return block


def build_goal_block_for_activity(conn: sqlite3.Connection, activity_id: int) -> dict:
    """activity_idを指定した読み出し（check_in・set_goalの応答・get_goal(activity_id)・
    update_activityのgoal_hint）向けのgoalブロックを組み立てる。規則1〜4を含めて評価する。
    """
    pending_asks = ask_service.get_pending_asks_with_conn(conn, activity_id)
    scope_next = _activity_scope_next(pending_asks)
    link_row = conn.execute("SELECT * FROM goal_activities WHERE activity_id = ?", (activity_id,)).fetchone()

    if link_row is None:
        return {"label": "undefined", "next": scope_next or _UNDEFINED_NEXT}
    if link_row["goal_id"] is None:
        result: dict = {"label": "not_needed", "reason": link_row["waiver_reason"]}
        if scope_next is not None:
            result["next"] = scope_next
        return result

    goal_id = link_row["goal_id"]
    goal_row = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
    condition_rows = _load_conditions(conn, goal_id)
    bound_states = _bound_states_for_conditions(conn, condition_rows)
    enriched = _enrich_conditions(condition_rows, bound_states)
    next_info = scope_next or _select_next_goal_scope(goal_row, enriched, activity_scoped=True)
    linked_rows = _linked_activity_rows(conn, goal_id)
    return _assemble_goal_block(conn, goal_row, enriched, next_info, linked_rows, exclude_activity_id=activity_id)


def build_goal_block_by_goal_id(conn: sqlite3.Connection, goal_id: int) -> Optional[dict]:
    """goal_idで指した読み出し（update_goal・judge_goalの応答）向けのgoalブロックを
    組み立てる。規則5〜14だけを評価する（activity単位の規則1〜4は評価しない）。
    """
    goal_row = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
    if goal_row is None:
        return None
    condition_rows = _load_conditions(conn, goal_id)
    bound_states = _bound_states_for_conditions(conn, condition_rows)
    enriched = _enrich_conditions(condition_rows, bound_states)
    next_info = _select_next_goal_scope(goal_row, enriched, activity_scoped=False)
    linked_rows = _linked_activity_rows(conn, goal_id)
    return _assemble_goal_block(conn, goal_row, enriched, next_info, linked_rows)


def build_goal_hint(conn: sqlite3.Connection, activity_id: int) -> Optional[dict]:
    """update_activityでcompletedにした直後、紐づくgoalが未判定なら添えるgoal_hintを
    組み立てる。completedの書き込みをコミットした後の接続で呼ぶ想定である。

    紐づくgoalが無い、または既に判定済みならNoneを返す（goal_hintを付けない）。
    """
    link_row = conn.execute("SELECT * FROM goal_activities WHERE activity_id = ?", (activity_id,)).fetchone()
    if link_row is None or link_row["goal_id"] is None:
        return None
    goal_id = link_row["goal_id"]
    goal_row = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
    if goal_row is None or goal_row["closed"] == 1:
        return None

    condition_rows = _load_conditions(conn, goal_id)
    bound_states = _bound_states_for_conditions(conn, condition_rows)
    enriched = _enrich_conditions(condition_rows, bound_states)
    next_info = _select_next_goal_scope(goal_row, enriched, activity_scoped=False)

    open_conds = [c for c in enriched if c["state"] == "open"]
    broken_conds = [c for c in enriched if "broken" in c["flags"]]
    label = "judge_ready" if not open_conds and not broken_conds else "active"

    linked_rows = _linked_activity_rows(conn, goal_id)
    open_activities_left = sum(1 for r in linked_rows if r["status"] != "completed")

    hint: dict = {
        "goal_id": goal_id,
        "handle": goal_row["handle"],
        "label": label,
        "next": next_info,
        "open_activities_left": open_activities_left,
    }
    strip_entity_id_inplace(hint, "goal_id")

    if label == "judge_ready":
        closing_ids = [r["id"] for r in linked_rows if r["status"] != "completed"]
        open_qs = _open_questions_for_activities(conn, closing_ids)
        if open_qs:
            hint["open_questions"] = open_qs[:_OPEN_QUESTIONS_MAX]

    if open_activities_left == 0:
        hint["warning"] = "goalが未判定のまま、紐づくactivityがすべて閉じた。条件を確かめてjudge_goalを呼ぶ"

    return hint


# ========================================
# get_goal
# ========================================


def _full_condition_entry(c: dict) -> dict:
    entry: dict = {
        "id": c["id"],
        "statement": c["statement"],
        "actor": c["actor"],
        "state": c["state"],
        "flags": c["flags"],
        "note": c["note"],
        "last_satisfied_at": c["last_satisfied_at"],
        "updated_at": c["updated_at"],
        "bound": None,
    }
    if c["bound_type"] is not None:
        bs = c["bound_state"] or {}
        bound: dict = {"type": c["bound_type"], "id": c["bound_id"], "title": bs.get("title"), "state": bs.get("state")}
        if bs.get("successor_title") is not None:
            bound["successor_title"] = bs["successor_title"]
        strip_entity_id_inplace(bound)
        entry["bound"] = bound
    strip_entity_id_inplace(entry)
    return entry


def _linked_activities_payload(conn: sqlite3.Connection, goal_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT a.id, a.title, a.status, a.closed_by
        FROM activities a
        JOIN goal_activities ga ON ga.activity_id = a.id
        WHERE ga.goal_id = ?
        ORDER BY a.id
        """,
        (goal_id,),
    ).fetchall()
    result = []
    for r in rows:
        d = {"id": r["id"], "title": r["title"], "status": r["status"], "closed_by": r["closed_by"]}
        strip_entity_id_inplace(d)
        result.append(d)
    return result


def get_goal(
    goal_id: Optional[int] = None,
    activity_id: Optional[int] = None,
    handle: Optional[str] = None,
) -> dict:
    """1つのgoalの全条件（充足済みを含む）とid、紐づくactivityを読む（読み取り専用）。

    goal_id・activity_id・handleのちょうど1つを指定する。activity_idを指定して
    goalが無い場合は、未定義ならlabel=undefinedとnext、不要ならlabel=not_needed
    とreason（規則1・2が一致したときはnextも）を返す。
    """
    specified = [v for v in (goal_id, activity_id, handle) if v is not None]
    if len(specified) != 1:
        return _validation_error("exactly one of goal_id, activity_id, handle must be given")
    if goal_id is not None and (not isinstance(goal_id, int) or isinstance(goal_id, bool)):
        return _validation_error("goal_id must be an integer")
    if activity_id is not None and (not isinstance(activity_id, int) or isinstance(activity_id, bool)):
        return _validation_error("activity_id must be an integer")
    if handle is not None and not isinstance(handle, str):
        return _validation_error("handle must be a string")

    conn = get_connection()
    try:
        activity_scope_id = None
        resolved_goal_id = goal_id
        pending_asks = None

        if activity_id is not None:
            if conn.execute("SELECT 1 FROM activities WHERE id = ?", (activity_id,)).fetchone() is None:
                return _not_found(f"activity {activity_id} not found")
            link_row = conn.execute(
                "SELECT * FROM goal_activities WHERE activity_id = ?", (activity_id,)
            ).fetchone()
            pending_asks = ask_service.get_pending_asks_with_conn(conn, activity_id)
            scope_next = _activity_scope_next(pending_asks)
            if link_row is None:
                return {"label": "undefined", "next": scope_next or _UNDEFINED_NEXT}
            if link_row["goal_id"] is None:
                result: dict = {"label": "not_needed", "reason": link_row["waiver_reason"]}
                if scope_next is not None:
                    result["next"] = scope_next
                return result
            resolved_goal_id = link_row["goal_id"]
            activity_scope_id = activity_id
        elif handle is not None:
            row = conn.execute("SELECT id FROM goals WHERE handle = ?", (handle,)).fetchone()
            if row is None:
                return _not_found(f"goal with handle {handle!r} not found")
            resolved_goal_id = row["id"]
        else:
            if conn.execute("SELECT 1 FROM goals WHERE id = ?", (goal_id,)).fetchone() is None:
                return _not_found(f"goal {goal_id} not found")

        goal_row = conn.execute("SELECT * FROM goals WHERE id = ?", (resolved_goal_id,)).fetchone()
        if goal_row is None:
            return _not_found(f"goal {resolved_goal_id} not found")

        condition_rows = _load_conditions(conn, resolved_goal_id)
        bound_states = _bound_states_for_conditions(conn, condition_rows)
        enriched = _enrich_conditions(condition_rows, bound_states)

        scope_next = _activity_scope_next(pending_asks) if pending_asks is not None else None
        next_info = scope_next or _select_next_goal_scope(
            goal_row, enriched, activity_scoped=(activity_scope_id is not None)
        )

        block, _label = _goal_core_fields(goal_row, enriched, next_info)
        block["conditions"] = [_full_condition_entry(c) for c in enriched]
        block["activities"] = _linked_activities_payload(conn, resolved_goal_id)
        return block
    except sqlite3.Error as e:
        return _database_error(str(e))
    finally:
        conn.close()
