"""citation 参照テンプレ (`{{cite:X#NNN}}`) の抽出・保存・引き当てを行うサービス。

本文中の `{{cite:X#NNN}}` テンプレを `citations` テーブルに保存し、
read 経路では `flavor` で展開形式を切り替える前提の参照基盤を提供する。

X は M/D/L/A/T のいずれかで、それぞれ material/decision/log/activity/topic に対応する。
"""
import logging
import re
import sqlite3
from typing import Literal

from src.db import get_connection

logger = logging.getLogger(__name__)

VALID_OWNER_TYPES = ("material", "decision", "log", "activity", "topic")
VALID_TARGET_TYPES = VALID_OWNER_TYPES

TYPE_CODE_TO_NAME: dict[str, str] = {
    "M": "material",
    "D": "decision",
    "L": "log",
    "A": "activity",
    "T": "topic",
}
TYPE_NAME_TO_CODE: dict[str, str] = {v: k for k, v in TYPE_CODE_TO_NAME.items()}

TYPE_TO_TABLE: dict[str, str] = {
    "material": "materials",
    "decision": "decisions",
    "log": "discussion_logs",
    "activity": "activities",
    "topic": "discussion_topics",
}

# 各 entity 種別の表示タイトル取得式 (SELECT 内で使用)。
# decision は title が NULL のとき decision 本文へ fall back する。
TYPE_TO_TITLE_EXPR: dict[str, str] = {
    "material": "title",
    "decision": "COALESCE(NULLIF(TRIM(title), ''), substr(decision, 1, 80))",
    "log": "COALESCE(NULLIF(TRIM(title), ''), substr(content, 1, 30))",
    "activity": "title",
    "topic": "title",
}

# retract カラムを持つ entity 種別
TYPES_WITH_RETRACT = {"decision", "log", "material"}

# owner 種別ごとに、本文中の citation 抽出対象となるテキストフィールド
# (DB カラム名そのまま、結合順は occurrence の決定要因)
OWNER_TEXT_FIELDS: dict[str, tuple[str, ...]] = {
    "material": ("title", "content"),
    "decision": ("decision", "reason"),
    "log": ("content",),
    "activity": ("title", "description"),
    "topic": ("title", "description"),
}

_CITE_PATTERN = re.compile(r"\{\{cite:([MDLAT])#(\d+)\}\}")
_CITE_LIKE_PATTERN = re.compile(r"\{\{cite:[^}]*\}\}")


def extract_citations(content: str) -> list[tuple[str, int]]:
    """本文から citation 参照を出現順に抽出する。

    コードブロック (フェンス ``` / ~~~ と インラインバッククォート) 内の
    テンプレはスキップする。`\\{{cite:...}}` のエスケープもスキップする。
    不正形式 (`{{cite:Z#1}}`, `{{cite:foo}}` 等) は警告ログを出して無視する。

    Returns:
        [(target_type, target_id), ...] の出現順リスト。occurrence は 1 始まりで連番。
    """
    results: list[tuple[str, int]] = []
    in_fence = False
    for raw_line in content.split("\n"):
        stripped = raw_line.lstrip()
        # フェンス境界 (```/~~~ で始まる行) でトグル
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        results.extend(_scan_line(raw_line))
    return results


def _scan_line(line: str) -> list[tuple[str, int]]:
    """1 行内の citation を走査。インラインバッククォート / エスケープをスキップ。"""
    out: list[tuple[str, int]] = []
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == "`":
            # 対応する閉じバッククォートまでスキップ
            close = line.find("`", i + 1)
            if close == -1:
                # 未閉じインラインコード: 行末まで保守的にスキップ
                break
            i = close + 1
            continue
        if ch == "\\" and line[i + 1 : i + 3] == "{{":
            # エスケープ `\{{cite:...}}` 全体をスキップ
            end = line.find("}}", i + 1)
            if end == -1:
                i += 1
                continue
            i = end + 2
            continue
        m = _CITE_PATTERN.match(line, i)
        if m:
            code = m.group(1)
            target_id_str = m.group(2)
            target_type = TYPE_CODE_TO_NAME.get(code)
            if target_type is None:
                logger.warning("citation parser: unknown type code %r", code)
                i = m.end()
                continue
            try:
                target_id = int(target_id_str)
            except ValueError:
                logger.warning("citation parser: invalid id %r", target_id_str)
                i = m.end()
                continue
            out.append((target_type, target_id))
            i = m.end()
            continue
        # 不正形式テンプレ (`{{cite:foo}}` 等) は警告
        like = _CITE_LIKE_PATTERN.match(line, i)
        if like:
            logger.warning("citation parser: malformed template skipped: %r", like.group(0))
            i = like.end()
            continue
        i += 1
    return out


def _validate_owner_type(owner_type: str) -> None:
    if owner_type not in VALID_OWNER_TYPES:
        raise ValueError(
            f"Invalid owner_type {owner_type!r}; must be one of {VALID_OWNER_TYPES}"
        )


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


def _combine_owner_text(owner_type: str, fields: dict) -> str:
    """owner の本文を occurrence 計算用に決定的順序で結合する。"""
    cols = OWNER_TEXT_FIELDS[owner_type]
    return "\n".join(fields.get(c) or "" for c in cols)


def upsert_citations_for_owner_with_conn(
    conn: sqlite3.Connection, owner_type: str, owner_id: int, **fields
) -> int:
    """add/update 共通: 既存 citations を全削除 → 本文結合 → 再投入。

    本文無変更でも呼ぶことで occurrence の一貫性が保たれる (M#373 §3.4.4)。
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
