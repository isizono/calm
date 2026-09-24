"""フィードバック機構のMCPツール3本（引く/書く/ノートを足す）の実装。

エントリの変更（update/delete、削除済み名前へのcreateによる復活）は必ず
get_feedback_entriesで取得した最新read_mark（MAX(feedback_notes.id)）を
要求する。これにより「変更前に必ずノートが読まれる」を仕組みで保証する。
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Optional

from src.db import get_connection
from src.services.feedback_rules import (
    PENDING_STUMBLES_SQL,
    ConditionError,
    maintenance_hint,
    parse_condition,
    validate_condition,
)

_NAME_RE = re.compile(r"[a-z0-9-]+")
_VALID_STRENGTHS = ("notify", "block")
_VALID_TIMINGS = ("utterance", "tool_fail", "pre_tool")
_VALID_KINDS = ("stumble", "note")

BODY_MAX_LEN = 100
REF_MAX_LEN = 500
NOTE_BODY_MAX_LEN = 500

_FIX_HINTS = {
    "VALIDATION_ERROR": "引数を見直す",
    "NOT_FOUND": "nameを確認する(未作成、または削除済みの可能性がある)",
    "CONFLICT": "get_feedback_entriesで最新のread_markを取り直してから再実行する",
    "DUPLICATE": "既存エントリを直すときはaction='update'を使う",
    "DATABASE_ERROR": "エラーメッセージを確認して引数を見直す",
}


def _reject(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message, "fix": _FIX_HINTS.get(code, "引数を見直す")}}


class _Rejected(Exception):
    """トランザクション内の拒否をrollbackまで一気に伝えるための内部シグナル。"""

    def __init__(self, result: dict):
        self.result = result


# ---------------------------------------------------------------------------
# 共通ヘルパー
# ---------------------------------------------------------------------------


def _fetch_notes_with_read_mark(conn: sqlite3.Connection, entry_id: int) -> tuple[list[dict], int]:
    """ノート一覧とread_mark(末尾行のid、無ければ0)を1クエリで取得する。

    ノートはidの昇順で並ぶので、末尾行のidが常にMAX(id)と一致する
    (feedback_notesは追記専用でidは単調増加のため)。
    """
    rows = conn.execute(
        "SELECT id, kind, body, created_at FROM feedback_notes WHERE entry_id = ? ORDER BY id",
        (entry_id,),
    ).fetchall()
    notes = [{"kind": r["kind"], "body": r["body"], "created_at": r["created_at"]} for r in rows]
    read_mark = rows[-1]["id"] if rows else 0
    return notes, read_mark


def _read_mark(conn: sqlite3.Connection, entry_id: int) -> int:
    row = conn.execute(
        "SELECT MAX(id) AS m FROM feedback_notes WHERE entry_id = ?", (entry_id,)
    ).fetchone()
    return row["m"] or 0


def _row_to_entry(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    notes, read_mark = _fetch_notes_with_read_mark(conn, row["id"])
    return {
        "id": row["id"],
        "name": row["name"],
        "body": row["body"],
        "ref": row["ref"],
        "strength": row["strength"],
        "timing": row["timing"],
        "condition": json.loads(row["condition_json"]),
        "delivered_count": row["delivered_count"],
        "overridden_count": row["overridden_count"],
        "deleted_at": row["deleted_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "notes": notes,
        "read_mark": read_mark,
    }


# ---------------------------------------------------------------------------
# 引く
# ---------------------------------------------------------------------------


def get_feedback_entries(
    name: Optional[str] = None,
    query: Optional[str] = None,
    include_deleted: bool = False,
) -> dict:
    conn = get_connection()
    try:
        sql = "SELECT * FROM feedback_entries WHERE 1=1"
        params: list = []
        if name is not None:
            sql += " AND name = ?"
            params.append(name)
        if query is not None:
            sql += " AND (body LIKE ? OR ref LIKE ?)"
            like = f"%{query}%"
            params.extend([like, like])
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        sql += " ORDER BY name"
        rows = conn.execute(sql, params).fetchall()
        return {"ok": True, "entries": [_row_to_entry(conn, row) for row in rows]}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 書く（create/update/delete）
# ---------------------------------------------------------------------------


def _require_body(body) -> str:
    if not isinstance(body, str) or not body.strip():
        raise _Rejected(_reject("VALIDATION_ERROR", "body は非空文字列"))
    if len(body) > BODY_MAX_LEN:
        raise _Rejected(_reject("VALIDATION_ERROR", f"body は{BODY_MAX_LEN}字以内"))
    return body


def _check_ref(ref) -> Optional[str]:
    if ref is not None and (not isinstance(ref, str) or len(ref) > REF_MAX_LEN):
        raise _Rejected(_reject("VALIDATION_ERROR", f"ref は{REF_MAX_LEN}字以内の文字列"))
    return ref


def _validate_content(strength, timing, condition) -> dict:
    if strength not in _VALID_STRENGTHS:
        raise _Rejected(_reject("VALIDATION_ERROR", f"strength は {_VALID_STRENGTHS} のいずれか"))
    if timing not in _VALID_TIMINGS:
        raise _Rejected(_reject("VALIDATION_ERROR", f"timing は {_VALID_TIMINGS} のいずれか"))
    if (strength == "block") != (timing == "pre_tool"):
        raise _Rejected(_reject(
            "VALIDATION_ERROR", "strength='block' と timing='pre_tool' は常に対応させる"
        ))
    try:
        parsed = parse_condition(condition if condition is not None else {})
        return validate_condition(parsed, strength=strength, timing=timing)
    except ConditionError as e:
        raise _Rejected(_reject("VALIDATION_ERROR", str(e))) from e


def _check_read_mark(conn: sqlite3.Connection, entry_id: int, read_mark) -> None:
    if not isinstance(read_mark, int) or isinstance(read_mark, bool):
        raise _Rejected(_reject("VALIDATION_ERROR", "read_mark(整数)が必要"))
    current = _read_mark(conn, entry_id)
    if read_mark != current:
        raise _Rejected(_reject(
            "CONFLICT",
            f"read_markが古い(渡された値={read_mark}、現在={current})。"
            "get_feedback_entriesで読み直してから再実行すること。",
        ))


def _apply_delete(conn: sqlite3.Connection, existing, read_mark) -> int:
    if existing is None or existing["deleted_at"] is not None:
        raise _Rejected(_reject("NOT_FOUND", "対象のエントリが見つからない(未作成または削除済み)"))
    _check_read_mark(conn, existing["id"], read_mark)
    conn.execute(
        "UPDATE feedback_entries SET deleted_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (existing["id"],),
    )
    return existing["id"]


def _apply_update(conn: sqlite3.Connection, existing, body, ref, strength, timing, condition, read_mark) -> int:
    if existing is None or existing["deleted_at"] is not None:
        raise _Rejected(_reject("NOT_FOUND", "対象のエントリが見つからない(未作成または削除済み)"))
    _check_read_mark(conn, existing["id"], read_mark)
    body = _require_body(body)
    ref = _check_ref(ref)
    normalized = _validate_content(strength, timing, condition)
    conn.execute(
        """UPDATE feedback_entries
           SET body = ?, ref = ?, strength = ?, timing = ?, condition_json = ?, updated_at = CURRENT_TIMESTAMP
           WHERE id = ?""",
        (body, ref, strength, timing, json.dumps(normalized, ensure_ascii=False), existing["id"]),
    )
    return existing["id"]


def _apply_create(conn: sqlite3.Connection, name, existing, body, ref, strength, timing, condition, read_mark) -> int:
    if existing is not None and existing["deleted_at"] is None:
        raise _Rejected(_reject("DUPLICATE", f"name '{name}' は既に使われている"))

    if existing is not None:
        # 削除済みエントリの名前を再利用する復活。既存エントリへの変更そのものなので
        # read_markを要求する(骨格「変更前に必ずノートが読まれる」を仕組みで保証する)。
        # read_mark検証を内容検証より先に行う(_apply_updateと同じ順序に揃える。
        # read_markが古くbodyも不正、という入力でCONFLICT/VALIDATION_ERRORの
        # どちらが返るかが変更経路によって食い違わないようにするため)。
        _check_read_mark(conn, existing["id"], read_mark)

    body = _require_body(body)
    ref = _check_ref(ref)
    normalized = _validate_content(strength, timing, condition)
    condition_json = json.dumps(normalized, ensure_ascii=False)

    if existing is not None:
        conn.execute(
            """UPDATE feedback_entries
               SET body = ?, ref = ?, strength = ?, timing = ?, condition_json = ?,
                   deleted_at = NULL, updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (body, ref, strength, timing, condition_json, existing["id"]),
        )
        return existing["id"]

    cur = conn.execute(
        """INSERT INTO feedback_entries (name, body, ref, strength, timing, condition_json)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (name, body, ref, strength, timing, condition_json),
    )
    return cur.lastrowid


def write_feedback_entry(
    name: str,
    action: str,
    body: Optional[str] = None,
    ref: Optional[str] = None,
    strength: Optional[str] = None,
    timing: Optional[str] = None,
    condition=None,
    read_mark: Optional[int] = None,
) -> dict:
    if action not in ("create", "update", "delete"):
        return _reject("VALIDATION_ERROR", f"action は create/update/delete のいずれか: {action}")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        return _reject("VALIDATION_ERROR", "name は英小文字・数字・ハイフンのみの非空文字列")

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT * FROM feedback_entries WHERE name = ?", (name,)).fetchone()

        if action == "delete":
            entry_id = _apply_delete(conn, existing, read_mark)
        elif action == "update":
            entry_id = _apply_update(conn, existing, body, ref, strength, timing, condition, read_mark)
        else:
            entry_id = _apply_create(conn, name, existing, body, ref, strength, timing, condition, read_mark)

        row = conn.execute("SELECT * FROM feedback_entries WHERE id = ?", (entry_id,)).fetchone()
        entry = _row_to_entry(conn, row)
        conn.commit()
        return {"ok": True, "entry": entry}
    except _Rejected as e:
        conn.rollback()
        return e.result
    except sqlite3.Error as e:
        conn.rollback()
        return _reject("DATABASE_ERROR", str(e))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# ノートを足す
# ---------------------------------------------------------------------------


def add_feedback_note(name: str, kind: str, body: str) -> dict:
    if kind not in _VALID_KINDS:
        return _reject("VALIDATION_ERROR", f"kind は {_VALID_KINDS} のいずれか")
    if not isinstance(body, str) or not body.strip():
        return _reject("VALIDATION_ERROR", "body は非空文字列")
    if len(body) > NOTE_BODY_MAX_LEN:
        return _reject("VALIDATION_ERROR", f"body は{NOTE_BODY_MAX_LEN}字以内")

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT id FROM feedback_entries WHERE name = ?", (name,)).fetchone()
        if row is None:
            conn.rollback()
            return _reject("NOT_FOUND", f"name '{name}' のエントリが存在しない")
        entry_id = row["id"]
        cur = conn.execute(
            "INSERT INTO feedback_notes (entry_id, kind, body) VALUES (?, ?, ?)",
            (entry_id, kind, body),
        )
        note_row = conn.execute(
            "SELECT id, kind, body, created_at FROM feedback_notes WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        pending_row = conn.execute(
            f"SELECT {PENDING_STUMBLES_SQL} AS p FROM feedback_entries e WHERE e.id = ?",
            (entry_id,),
        ).fetchone()
        conn.commit()
        result = {
            "ok": True,
            "note": {
                "kind": note_row["kind"],
                "body": note_row["body"],
                "created_at": note_row["created_at"],
            },
            "read_mark": note_row["id"],
        }
        hint = maintenance_hint(0, pending_row["p"])
        if hint:
            result["hint"] = hint
        return result
    except sqlite3.Error as e:
        conn.rollback()
        return _reject("DATABASE_ERROR", str(e))
    finally:
        conn.close()
