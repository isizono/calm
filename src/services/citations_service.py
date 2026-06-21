"""citation 参照テンプレ (`{{cite:X#NNN}}`) の DB 永続化を担うサービス。

本文中の `{{cite:X#NNN}}` テンプレを `citations` テーブルに保存し、
read 経路では `flavor` で展開形式を切り替える前提の参照基盤を提供する。

pure (副作用なし) なロジック — 定数表・正規表現・抽出器・型バリデータ・本文結合・
target 存在チェック — は `src.services.citations_pure` 側へ集約済み。本モジュールは
そちらを import して DB I/O を伴う高レベル API (extract_and_insert / replace_all /
upsert_citations_for_owner_with_conn / get_in_out 等) を提供する。

X は M/D/L/A/T のいずれかで、それぞれ material/decision/log/activity/topic に対応する。
"""
import sqlite3

from src.db import get_connection
from src.services.citations_pure import (
    OWNER_TEXT_FIELDS,
    TYPE_TO_TABLE,
    TYPE_TO_TITLE_EXPR,
    TYPES_WITH_RETRACT,
    _combine_owner_text,
    _validate_owner_type,
    extract_citations,
)

__all__ = [
    "extract_and_insert",
    "replace_all",
    "delete_all_for_owner",
    "upsert_citations_for_owner_with_conn",
    "get_in_out",
]


def extract_and_insert(owner_type: str, owner_id: int, content: str) -> int:
    """owner 本文をパースして citations へ INSERT する公開関数。

    既存 citations は削除しない (新規 INSERT のみ)。update 経路では replace_all を使うこと。

    Returns:
        INSERT 件数
    """
    _validate_owner_type(owner_type)
    with get_connection() as conn:
        with conn:
            return _extract_and_insert_with_conn(conn, owner_type, owner_id, content)


def _extract_and_insert_with_conn(
    conn: sqlite3.Connection, owner_type: str, owner_id: int, content: str
) -> int:
    citations = extract_citations(content or "")
    if not citations:
        return 0
    rows = [
        (owner_type, owner_id, target_type, target_id, occurrence)
        for occurrence, (target_type, target_id) in enumerate(citations, start=1)
    ]
    conn.executemany(
        "INSERT INTO citations (owner_type, owner_id, target_type, target_id, occurrence) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def replace_all(owner_type: str, owner_id: int, content: str) -> int:
    """既存 citations 全削除 → 新本文をパース → 再投入 (update 経路用)。"""
    _validate_owner_type(owner_type)
    with get_connection() as conn:
        with conn:
            return _replace_all_with_conn(conn, owner_type, owner_id, content)


def _replace_all_with_conn(
    conn: sqlite3.Connection, owner_type: str, owner_id: int, content: str
) -> int:
    conn.execute(
        "DELETE FROM citations WHERE owner_type = ? AND owner_id = ?",
        (owner_type, owner_id),
    )
    return _extract_and_insert_with_conn(conn, owner_type, owner_id, content)


def delete_all_for_owner(owner_type: str, owner_id: int) -> int:
    """owner 自身が削除されたとき呼ぶ手動 cascade (DB トリガーで自動削除されるが
    トリガー経由しない経路の互換用)。"""
    _validate_owner_type(owner_type)
    with get_connection() as conn:
        with conn:
            return _delete_all_for_owner_with_conn(conn, owner_type, owner_id)


def _delete_all_for_owner_with_conn(
    conn: sqlite3.Connection, owner_type: str, owner_id: int
) -> int:
    cur = conn.execute(
        "DELETE FROM citations WHERE owner_type = ? AND owner_id = ?",
        (owner_type, owner_id),
    )
    return cur.rowcount


def upsert_citations_for_owner_with_conn(
    conn: sqlite3.Connection, owner_type: str, owner_id: int, **fields
) -> int:
    """add/update 共通: 既存 citations を全削除 → 本文結合 → 再投入。

    本文無変更でも呼ぶことで occurrence の一貫性が保たれる。
    fields は OWNER_TEXT_FIELDS で定義された名前の部分集合を渡す
    (未指定キーは空文字扱い)。
    """
    _validate_owner_type(owner_type)
    if owner_type not in OWNER_TEXT_FIELDS:
        return 0
    text = _combine_owner_text(owner_type, fields)
    return _replace_all_with_conn(conn, owner_type, owner_id, text)


def _resolve_targets(
    conn: sqlite3.Connection, pairs: list[tuple[str, int]]
) -> dict[tuple[str, int], dict]:
    """(type, id) の集合に対し、現在の DB から title / deleted / retracted を取得する。

    Returns:
        {(type, id): {"title": str|None, "deleted": bool, "retracted": bool}}
    """
    if not pairs:
        return {}
    by_type: dict[str, set[int]] = {}
    for t, i in pairs:
        by_type.setdefault(t, set()).add(i)
    out: dict[tuple[str, int], dict] = {}
    for t, ids in by_type.items():
        table = TYPE_TO_TABLE[t]
        title_expr = TYPE_TO_TITLE_EXPR[t]
        placeholders = ",".join(["?"] * len(ids))
        has_retract = t in TYPES_WITH_RETRACT
        retract_expr = "retracted_at" if has_retract else "NULL"
        query = (
            f"SELECT id, {title_expr} AS title, {retract_expr} AS retracted_at "
            f"FROM {table} WHERE id IN ({placeholders})"
        )
        found_ids: set[int] = set()
        for row in conn.execute(query, tuple(ids)):
            found_ids.add(row["id"])
            out[(t, row["id"])] = {
                "title": row["title"],
                "deleted": False,
                "retracted": bool(row["retracted_at"]) if has_retract else False,
            }
        # 未発見 = 物理削除済
        for missing in ids - found_ids:
            out[(t, missing)] = {"title": None, "deleted": True, "retracted": False}
    return out


def get_in_out(owner_type: str, owner_id: int) -> dict:
    """owner の citations_in / citations_out を DISTINCT で取得する。"""
    _validate_owner_type(owner_type)
    with get_connection() as conn:
        return _get_in_out_with_conn(conn, owner_type, owner_id)


def _get_in_out_with_conn(
    conn: sqlite3.Connection, owner_type: str, owner_id: int
) -> dict:
    out_pairs: list[tuple[str, int]] = []
    for row in conn.execute(
        "SELECT DISTINCT target_type, target_id FROM citations "
        "WHERE owner_type = ? AND owner_id = ? "
        "ORDER BY target_type, target_id",
        (owner_type, owner_id),
    ):
        out_pairs.append((row["target_type"], row["target_id"]))
    in_pairs: list[tuple[str, int]] = []
    for row in conn.execute(
        "SELECT DISTINCT owner_type, owner_id FROM citations "
        "WHERE target_type = ? AND target_id = ? "
        "ORDER BY owner_type, owner_id",
        (owner_type, owner_id),
    ):
        in_pairs.append((row["owner_type"], row["owner_id"]))
    resolved_out = _resolve_targets(conn, out_pairs)
    resolved_in = _resolve_targets(conn, in_pairs)
    return {
        "out": [_format_in_out_entry(t, i, resolved_out[(t, i)]) for t, i in out_pairs],
        "in": [_format_in_out_entry(t, i, resolved_in[(t, i)]) for t, i in in_pairs],
    }


def _format_in_out_entry(entity_type: str, entity_id: int, meta: dict) -> dict:
    entry: dict = {
        "type": entity_type,
        "id": entity_id,
        "title": meta["title"],
    }
    if meta["deleted"]:
        entry["deleted"] = True
    if meta["retracted"]:
        entry["retracted"] = True
    return entry
