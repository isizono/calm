"""citation 参照テンプレ (`{{cite:X#NNN}}`) の DB 永続化を担うサービス。

本文中の `{{cite:X#NNN}}` テンプレを `citations` テーブルに保存し、
read 経路では `flavor` で展開形式を切り替える前提の参照基盤を提供する。

pure (副作用なし) なロジック — 定数表・正規表現・抽出器・型バリデータ・本文結合・
target 存在チェック — は `src.services.citations_pure` 側へ集約済み。本モジュールは
そちらを import して DB I/O を伴う高レベル API (extract_and_insert / replace_all /
upsert_citations_for_owner_with_conn / get_in_out 等) を提供する。

X は M/D/L/A/T のいずれかで、それぞれ material/decision/log/activity/topic に対応する。
"""
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from src.db import get_connection
from src.services.citations_pure import (
    OWNER_TEXT_FIELDS,
    TYPE_CODE_TO_NAME,
    TYPE_TO_TABLE,
    TYPE_TO_TITLE_EXPR,
    TYPES_WITH_RETRACT,
    VALID_TARGET_TYPES,
    _RAW_CITE_PATTERN,
    _combine_owner_text,
    _validate_owner_type,
    check_target_exists,
    convert_raw_to_cite,
    extract_citations,
)

__all__ = [
    "extract_and_insert",
    "replace_all",
    "delete_all_for_owner",
    "upsert_citations_for_owner_with_conn",
    "get_in_out",
    "record_citation_event",
    "apply_raw_to_cite_conversion",
    "VALID_EVENT_SOURCES",
    "VALID_VERIFICATION_RESULTS",
]

# citation_event_log.source の許容値 (migration 0046 の CHECK 制約と一致させる)
VALID_EVENT_SOURCES: tuple[str, ...] = (
    "write_auto_convert",
    "bulk_migration",
    "transcript_post_tool_use",
    "transcript_session_start_backfill",
    "external_doc_sanitize",
)

# citation_event_log.verification_result の許容値 (CHECK 制約と一致)
VALID_VERIFICATION_RESULTS: tuple[str, ...] = ("exists", "dangling", "skip")


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


def record_citation_event(
    conn: sqlite3.Connection,
    source: str,
    tool_name: str | None,
    target_entity_type: str | None,
    target_entity_id: int | None,
    target_field: str | None,
    before_text: str,
    after_text: str,
    verified_at: str | None = None,
    verification_result: str | None = None,
    extra: dict | None = None,
) -> int:
    """citation_event_log に 1 件 INSERT し、その row id を返す。

    呼び出し元が開いた conn にぶら下げる (autocommit 制御は呼び出し元側)。

    Args:
        source: VALID_EVENT_SOURCES のいずれか
        tool_name: write 経路の MCP tool 名等 (任意)
        target_entity_type: 'decision' / 'activity' / 'log' / 'material' / 'topic' / None
        target_entity_id: 対象エンティティ ID (任意)
        target_field: 対象 field 名 (任意、content / title 等)
        before_text: 変換前テキスト (空文字 OK、NULL 不可)
        after_text: 変換後テキスト (空文字 OK、NULL 不可)
        verified_at: target 存在チェック時刻 (UTC, "YYYY-MM-DD HH:MM:SS" 形式)
        verification_result: 'exists' / 'dangling' / 'skip' / None
        extra: 追加メタ情報 (JSON シリアライズ可能な dict、None なら extra_json は NULL)
    """
    if source not in VALID_EVENT_SOURCES:
        raise ValueError(
            f"Invalid source {source!r}; must be one of {VALID_EVENT_SOURCES}"
        )
    if (
        target_entity_type is not None
        and target_entity_type not in VALID_TARGET_TYPES
    ):
        raise ValueError(
            f"Invalid target_entity_type {target_entity_type!r}; "
            f"must be one of {VALID_TARGET_TYPES} or None"
        )
    if (
        verification_result is not None
        and verification_result not in VALID_VERIFICATION_RESULTS
    ):
        raise ValueError(
            f"Invalid verification_result {verification_result!r}; "
            f"must be one of {VALID_VERIFICATION_RESULTS} or None"
        )
    extra_json = json.dumps(extra, ensure_ascii=False) if extra is not None else None
    cur = conn.execute(
        "INSERT INTO citation_event_log ("
        "source, tool_name, target_entity_type, target_entity_id, target_field, "
        "before_text, after_text, verified_at, verification_result, extra_json"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            source,
            tool_name,
            target_entity_type,
            target_entity_id,
            target_field,
            before_text,
            after_text,
            verified_at,
            verification_result,
            extra_json,
        ),
    )
    return int(cur.lastrowid)


def _utc_now_stamp() -> str:
    """SQLite の datetime('now') と整合する UTC タイムスタンプ。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def apply_raw_to_cite_conversion(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: int,
    fields_payload: dict[str, Any],
    tool_name: str | None,
) -> dict:
    """write 経路の自動変換を行う統合 helper。

    OWNER_TEXT_FIELDS (field map) を引いて entity_type ごとの対象 field を抽出し、
    各 field に対し pure 関数 convert_raw_to_cite を呼んで生 `X#NNN` を
    `{{cite:X#NNN}}` に変換する。target が DB に存在しない (dangling) リテラルは
    `[deleted X#NNN]` に確定書き換えする。変換が発生した field ごとに
    citation_event_log に 1 件 record_citation_event する。

    冪等性: 既に `{{cite:X#NNN}}` 形式のものは convert_raw_to_cite 内でスキップされ、
    入力テキストと出力テキストが一致するため event は記録されない。コードブロック内 /
    インラインバッククォート内 / `\\X#NNN` エスケープも同様にスキップされる。

    Args:
        entity_type: 'material' / 'decision' / 'log' / 'activity' / 'topic'
        entity_id: 対象エンティティ ID
        fields_payload: 対象 field の現在値 ({field_name: text})。OWNER_TEXT_FIELDS に
                        定義された field のみが変換対象、その他の key は素通し。
        tool_name: write tool 名 (event 記録の tool_name 列に入る)

    Returns:
        {
          "fields": {変換後 fields_payload 全 key},
          "event_ids": [int, ...],   # 変換が発生した field 単位の event id
          "stats":    {field_name: {sanitized_count / dangling_count / skipped_*}}
        }
    """
    if entity_type not in OWNER_TEXT_FIELDS:
        raise ValueError(
            f"Invalid entity_type {entity_type!r}; "
            f"must be one of {tuple(OWNER_TEXT_FIELDS)}"
        )
    result_fields: dict[str, Any] = dict(fields_payload)
    event_ids: list[int] = []
    stats: dict[str, dict] = {}
    for field_name in OWNER_TEXT_FIELDS[entity_type]:
        original = fields_payload.get(field_name)
        if not isinstance(original, str) or not original:
            continue

        dangling_set: set[tuple[str, int]] = set()

        def validator(target_type: str, target_id: int) -> bool:
            exists = check_target_exists(conn, target_type, target_id)
            if not exists:
                dangling_set.add((target_type, target_id))
            return exists

        converted, counters = convert_raw_to_cite(
            original, target_validator=validator
        )

        # dangling target は出力に raw `X#NNN` として残るので、確定書き換えで
        # `[deleted X#NNN]` に置換する。codeblock / escape / 既存 cite 区間内の
        # 同一リテラルは validator が呼ばれていないため dangling_set には含まれず、
        # 下記 sub の置換対象からも除外される (= raw のまま温存)。
        if dangling_set:

            def replace_dangling(m: "re.Match[str]") -> str:
                code = m.group(1)
                tid = int(m.group(2))
                target_type = TYPE_CODE_TO_NAME[code]
                if (target_type, tid) in dangling_set:
                    return f"[deleted {code}#{tid}]"
                return m.group(0)

            converted = _RAW_CITE_PATTERN.sub(replace_dangling, converted)

        result_fields[field_name] = converted

        field_stats = {
            "sanitized_count": counters["sanitized_count"],
            "dangling_count": len(dangling_set),
            "skipped_in_codeblock": counters["skipped_in_codeblock"],
            "skipped_in_existing_cite": counters["skipped_in_existing_cite"],
            "skipped_escape": counters["skipped_escape"],
        }
        stats[field_name] = field_stats

        if converted == original:
            continue
        verification_result = "dangling" if dangling_set else "exists"
        extra: dict = dict(field_stats)
        if dangling_set:
            extra["dangling_targets"] = [
                {"type": t, "id": i}
                for (t, i) in sorted(dangling_set, key=lambda p: (p[0], p[1]))
            ]
        event_id = record_citation_event(
            conn,
            source="write_auto_convert",
            tool_name=tool_name,
            target_entity_type=entity_type,
            target_entity_id=entity_id,
            target_field=field_name,
            before_text=original,
            after_text=converted,
            verified_at=_utc_now_stamp(),
            verification_result=verification_result,
            extra=extra,
        )
        event_ids.append(event_id)
    return {"fields": result_fields, "event_ids": event_ids, "stats": stats}
