"""export候補洗い出しサービス

起点エンティティ群・タグ指定からrelationグラフを辿り、5型
(topic/activity/material/decision/log)の候補一覧をexport判断に必要な付加情報
（retracted/superseded/status/本文サイズ/親topic/snippet）付きで返す。

get_mapとは独立の専用ツール（collect_export_candidates）の実装本体。get_mapは
navigation用途の既存契約（decision/logはグラフ走査の経由ノードのみでカタログに
含めない）を持つため、export候補洗い出しに必要な情報（decision/logそのものの
カタログ化、retracted/supersededフラグ等）を追加するとget_mapの契約が肥大化する。
グラフ走査自体はrelation_service._traverse_relations_with_connを共有する。
"""
import logging
import sqlite3
from collections import defaultdict

from src.db import get_connection, row_to_dict
from src.services.citations_pure import (
    OWNER_TEXT_FIELDS,
    TYPE_TO_TABLE,
    TYPE_TO_TITLE_EXPR,
    TYPES_WITH_RETRACT,
    _combine_owner_text,
    extract_citations,
)
from src.services.readable_id import strip_entity_id_inplace
from src.services.relation_service import VALID_ENTITY_TYPES, _traverse_relations_with_conn
from src.services.supersede_service import compute_supersede_info_batch
from src.services.tag_service import (
    get_entity_tags_batch,
    resolve_tag_ids,
    validate_and_parse_tags,
)

logger = logging.getLogger(__name__)

# collect_export_candidatesがカタログ本体に含める型。get_mapと違いdecision/logも含む。
ALL_CATALOG_TYPES = VALID_ENTITY_TYPES

SNIPPET_LEN = 200

# 型ごとの「主テキストフィールド」（snippet抽出元。titleは含めない）
_SNIPPET_FIELD = {
    "material": "content",
    "decision": "decision",
    "log": "content",
    "activity": "description",
    "topic": "description",
}

# 型ごとの本文サイズ計測対象フィールド（titleは含めない。decisionはdecision+reason）
_SIZE_FIELDS = {
    "material": ("content",),
    "decision": ("decision", "reason"),
    "log": ("content",),
    "activity": ("description",),
    "topic": ("description",),
}

# 型ごとのタグjunctionテーブル・エンティティ列
_JUNCTION = {
    "topic": ("topic_tags", "topic_id"),
    "activity": ("activity_tags", "activity_id"),
    "material": ("material_tags", "material_id"),
    "decision": ("decision_tags", "decision_id"),
    "log": ("log_tags", "log_id"),
}


def _validate_roots(roots: list) -> dict | None:
    """rootsのバリデーション。不正な場合はエラーdictを返す。"""
    if not isinstance(roots, list):
        return {"error": {"code": "VALIDATION_ERROR", "message": "roots must be a list"}}
    for r in roots:
        if not isinstance(r, dict) or "type" not in r or "id" not in r:
            return {
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Each root must have 'type' and 'id' fields",
                }
            }
        if r["type"] not in ALL_CATALOG_TYPES:
            return {
                "error": {
                    "code": "INVALID_ENTITY_TYPE",
                    "message": f"Invalid entity type: '{r['type']}'. Must be one of {sorted(ALL_CATALOG_TYPES)}",
                }
            }
        if not isinstance(r["id"], int) or isinstance(r["id"], bool) or r["id"] <= 0:
            return {
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": f"'id' for root type '{r['type']}' must be a positive integer",
                }
            }
    return None


def _validate_include_types(include_types: set) -> dict | None:
    for t in include_types:
        if t not in ALL_CATALOG_TYPES:
            return {
                "error": {
                    "code": "INVALID_ENTITY_TYPE",
                    "message": f"Invalid entity type in include_types: '{t}'. Must be one of {sorted(ALL_CATALOG_TYPES)}",
                }
            }
    return None


def _fetch_rows_for_type_with_conn(conn: sqlite3.Connection, etype: str, ids: list[int]) -> list[sqlite3.Row]:
    """型別テーブルから*全カラム+resolved_title（TYPE_TO_TITLE_EXPR適用済み）を取得する。"""
    if not ids:
        return []
    table = TYPE_TO_TABLE[etype]
    title_expr = TYPE_TO_TITLE_EXPR[etype]
    placeholders = ",".join("?" * len(ids))
    return conn.execute(
        f"SELECT *, {title_expr} AS resolved_title FROM {table} WHERE id IN ({placeholders})",
        ids,
    ).fetchall()


def _build_candidate(etype: str, row_d: dict, depth: int) -> dict:
    """1行分のcandidate dictを組み立てる（type固有の付加情報を含む）。"""
    snippet_field = _SNIPPET_FIELD[etype]
    size_fields = _SIZE_FIELDS[etype]
    candidate = {
        "type": etype,
        "id": row_d["id"],
        "title": row_d.get("resolved_title") or "",
        "snippet": (row_d.get(snippet_field) or "")[:SNIPPET_LEN],
        "tags": [],
        "depth": depth,
        "size_chars": sum(len(row_d.get(f) or "") for f in size_fields),
        "parent_topic_title": None,
    }
    if etype in TYPES_WITH_RETRACT:
        candidate["retracted"] = row_d.get("retracted_at") is not None
    if etype == "activity":
        candidate["status"] = row_d["status"]
    return candidate


def _collect_tag_seed_keys_with_conn(conn: sqlite3.Connection, tag_ids: list[int]) -> set[tuple[str, int]]:
    """指定タグIDを持つ全エンティティの(type, id)集合を返す（5型junction横断・深度0固定）。"""
    if not tag_ids:
        return set()
    placeholders = ",".join("?" * len(tag_ids))
    keys: set[tuple[str, int]] = set()
    for etype, (junction, col) in _JUNCTION.items():
        rows = conn.execute(
            f"SELECT DISTINCT {col} AS entity_id FROM {junction} WHERE tag_id IN ({placeholders})",
            tag_ids,
        ).fetchall()
        keys.update((etype, row["entity_id"]) for row in rows)
    return keys


def _fetch_parent_topic_titles_with_conn(
    conn: sqlite3.Connection, ids_by_type: dict[str, list[int]]
) -> dict[tuple[str, int], str]:
    """belongs_to経由の親topicタイトルを一括取得する。

    複数の親topicが存在する場合（relationsのPRIMARY KEYは複数belongs_to行を許容する）は
    target_id昇順の先頭を採用する。
    """
    result: dict[tuple[str, int], str] = {}
    for etype, ids in ids_by_type.items():
        if not ids:
            continue
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"""
            SELECT r.source_id AS entity_id, t.title AS topic_title
            FROM relations r
            JOIN discussion_topics t ON t.id = r.target_id
            WHERE r.relation_type = 'belongs_to'
              AND r.source_type = ?
              AND r.target_type = 'topic'
              AND r.source_id IN ({placeholders})
            ORDER BY r.source_id, r.target_id
            """,
            (etype, *ids),
        ).fetchall()
        for row in rows:
            key = (etype, row["entity_id"])
            if key not in result:
                result[key] = row["topic_title"]
    return result


def _fetch_titles_with_conn(
    conn: sqlite3.Connection, ids_by_type: dict[str, set[int]]
) -> dict[tuple[str, int], str]:
    """closure_warnings用: 候補集合外のエンティティのタイトルを一括取得する。"""
    titles: dict[tuple[str, int], str] = {}
    for etype, ids in ids_by_type.items():
        ids = [i for i in ids if i is not None]
        if not ids:
            continue
        table = TYPE_TO_TABLE[etype]
        title_expr = TYPE_TO_TITLE_EXPR[etype]
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT id, {title_expr} AS title FROM {table} WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        for row in rows:
            titles[(etype, row["id"])] = row["title"]
    return titles


def _build_closure_warnings_with_conn(
    conn: sqlite3.Connection,
    candidates: list[dict],
    candidate_set: set[tuple[str, int]],
    raw_text_by_key: dict[tuple[str, int], str],
) -> list[dict]:
    """supersede先・引用(cite)先が選択範囲外のケースを検知する。"""
    title_by_key = {(c["type"], c["id"]): c["title"] for c in candidates}

    decision_ids = [c["id"] for c in candidates if c["type"] == "decision"]
    supersede_pairs: list[tuple[int, int]] = []
    if decision_ids:
        placeholders = ",".join("?" * len(decision_ids))
        rows = conn.execute(
            f"""
            SELECT source_id, target_id FROM decision_supersedes
            WHERE kind = 'replaces' AND source_id IN ({placeholders})
            """,
            decision_ids,
        ).fetchall()
        for row in rows:
            if ("decision", row["target_id"]) not in candidate_set:
                supersede_pairs.append((row["source_id"], row["target_id"]))

    cite_pairs: list[tuple[tuple[str, int], str, int]] = []
    for key, text in raw_text_by_key.items():
        for target_type, target_id in extract_citations(text):
            if target_type not in ALL_CATALOG_TYPES:
                continue
            if (target_type, target_id) not in candidate_set:
                cite_pairs.append((key, target_type, target_id))

    missing_targets: dict[str, set[int]] = defaultdict(set)
    for _, target_id in supersede_pairs:
        missing_targets["decision"].add(target_id)
    for _, target_type, target_id in cite_pairs:
        missing_targets[target_type].add(target_id)
    resolved_titles = _fetch_titles_with_conn(conn, missing_targets)

    warnings: list[dict] = []
    for source_id, target_id in supersede_pairs:
        warnings.append(
            {
                "kind": "supersede_target_outside",
                "from_title": title_by_key.get(("decision", source_id), f"decision#{source_id}"),
                "target_title": resolved_titles.get(("decision", target_id), f"decision#{target_id}"),
                "target": {"type": "decision", "id_raw": target_id},
            }
        )
    for (ftype, fid), target_type, target_id in cite_pairs:
        warnings.append(
            {
                "kind": "cite_target_outside",
                "from_title": title_by_key.get((ftype, fid), f"{ftype}#{fid}"),
                "target_title": resolved_titles.get((target_type, target_id), f"{target_type}#{target_id}"),
                "target": {"type": target_type, "id_raw": target_id},
            }
        )
    return warnings


def _compute_co_tags_with_conn(
    conn: sqlite3.Connection, tag_root_ids: list[int], seed_keys: set[tuple[str, int]]
) -> list[dict]:
    """tag_rootsのシード集合上でdomain:タグの共起を集計する（既存analyze_tagsはmaterial_tagsを
    含まないため流用せず、5型junction横断の専用軽量集計として書く）。"""
    if not tag_root_ids or not seed_keys:
        return []

    ids_by_type: dict[str, list[int]] = defaultdict(list)
    for etype, eid in seed_keys:
        ids_by_type[etype].append(eid)

    tag_root_placeholders = ",".join("?" * len(tag_root_ids))
    co_counts: dict[int, int] = defaultdict(int)
    for etype, ids in ids_by_type.items():
        junction, col = _JUNCTION[etype]
        id_placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"""
            SELECT tag_id, COUNT(DISTINCT {col}) AS cnt
            FROM {junction}
            WHERE {col} IN ({id_placeholders}) AND tag_id NOT IN ({tag_root_placeholders})
            GROUP BY tag_id
            """,
            (*ids, *tag_root_ids),
        ).fetchall()
        for row in rows:
            co_counts[row["tag_id"]] += row["cnt"]

    if not co_counts:
        return []

    total_seed = len(seed_keys)
    tag_ids = list(co_counts.keys())
    placeholders = ",".join("?" * len(tag_ids))
    tag_rows = conn.execute(
        f"SELECT id, namespace, name FROM tags WHERE id IN ({placeholders}) AND namespace = 'domain'",
        tag_ids,
    ).fetchall()

    result = [
        {
            "tag": f"{row['namespace']}:{row['name']}",
            "overlap": co_counts[row["id"]],
            "share": round(co_counts[row["id"]] / total_seed, 4),
        }
        for row in tag_rows
    ]
    result.sort(key=lambda x: (-x["overlap"], x["tag"]))
    return result


def collect_export_candidates(
    roots: list[dict] | None = None,
    max_depth: int = 2,
    include_types: list[str] | None = None,
    tag_roots: list[str] | None = None,
    include_snippets: bool = True,
    limit: int | None = None,
    offset: int = 0,
) -> dict:
    """起点エンティティ群・タグ指定からexport候補を洗い出す。

    起点(roots)からrelationを辿って到達可能な5型(topic/activity/material/decision/log)の
    エンティティを収集し、export判断に必要な付加情報（retracted/superseded/status/
    本文サイズ/親topic/snippet）付きで返す。tag_rootsを指定すると、指定タグを持つ全
    エンティティを深度0固定でシード集合に合流させる（グラフ拡張はしない）。get_mapとは
    独立の専用ツールであり、decision/logもカタログ本体に含む点がget_mapと異なる。

    Args:
        roots: 起点。[{"type": ..., "id": ...}, ...]（複数起点可）。tag_rootsのみで
            シードする場合は空リスト/省略可（roots/tag_rootsの少なくとも一方が必要）
        max_depth: rootsからの走査深度上限（デフォルト2、上限10）。tag_rootsのシードには
            適用されない（常に深度0固定）
        include_types: 返却する型のフィルタ（デフォルト5型全部）。走査・closure_warnings
            判定には影響しない（表示フィルタのみ）
        tag_roots: 指定タグ文字列（例: ["domain:cc-memory"]）を持つ全エンティティを
            シード集合に合流させる
        include_snippets: Falseにすると各candidateからsnippetキーを省く
            （ドメイン規模での応答サイズ対策）
        limit: 返却candidates件数の上限（デフォルトNone=無制限）
        offset: 返却開始位置（デフォルト0）

    Returns:
        成功時: {
            "candidates": [{type, id_raw, title, snippet, tags, depth, size_chars,
                             parent_topic_title, retracted?, superseded?, status?}, ...],
            "closure_warnings": [{kind, from_title, target_title, target: {type, id_raw}}, ...],
            "total_count": int,  # include_types適用後・limit適用前の件数
            "truncated": bool,   # limitにより後続candidatesが切り捨てられていればTrue
            "co_tags": [{tag, overlap, share}, ...],  # tag_roots指定時のみキーが付く
        }
        失敗時: {"error": {"code": ..., "message": ...}}
    """
    roots = roots or []
    tag_roots = tag_roots or []

    if not roots and not tag_roots:
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "roots or tag_roots must be provided",
            }
        }

    err = _validate_roots(roots)
    if err:
        return err

    if max_depth < 0:
        return {"error": {"code": "INVALID_PARAMETER", "message": "max_depth must be >= 0"}}
    if max_depth > 10:
        return {"error": {"code": "INVALID_PARAMETER", "message": "max_depth must be <= 10"}}

    include_types_set = set(include_types) if include_types is not None else set(ALL_CATALOG_TYPES)
    err = _validate_include_types(include_types_set)
    if err:
        return err

    if limit is not None and limit < 1:
        return {"error": {"code": "INVALID_PARAMETER", "message": "limit must be >= 1"}}
    if offset < 0:
        return {"error": {"code": "INVALID_PARAMETER", "message": "offset must be >= 0"}}

    conn = get_connection(load_vec=False)
    try:
        depth_by_key: dict[tuple[str, int], int] = {}

        if roots:
            root_tuples = [(r["type"], r["id"]) for r in roots]
            traversal_rows = _traverse_relations_with_conn(
                conn, root_tuples, max_depth, catalog_types=set(ALL_CATALOG_TYPES), min_depth=0
            )
            for row in traversal_rows:
                depth_by_key[(row["entity_type"], row["entity_id"])] = row["depth"]

        tag_root_ids: list[int] = []
        seed_keys: set[tuple[str, int]] = set()
        if tag_roots:
            parsed = validate_and_parse_tags(tag_roots)
            if isinstance(parsed, dict):
                return parsed
            tag_root_ids = resolve_tag_ids(conn, parsed)
            seed_keys = _collect_tag_seed_keys_with_conn(conn, tag_root_ids)
            for key in seed_keys:
                depth_by_key[key] = 0

        if not depth_by_key:
            result = {
                "candidates": [],
                "closure_warnings": [],
                "total_count": 0,
                "truncated": False,
            }
            if tag_roots:
                result["co_tags"] = []
            return result

        ids_by_type: dict[str, list[int]] = defaultdict(list)
        for etype, eid in depth_by_key:
            ids_by_type[etype].append(eid)

        candidates: list[dict] = []
        raw_text_by_key: dict[tuple[str, int], str] = {}

        for etype, ids in ids_by_type.items():
            rows = _fetch_rows_for_type_with_conn(conn, etype, ids)
            for row in rows:
                row_d = row_to_dict(row)
                eid = row_d["id"]
                depth = depth_by_key[(etype, eid)]
                candidates.append(_build_candidate(etype, row_d, depth))
                raw_text_by_key[(etype, eid)] = _combine_owner_text(etype, row_d)

        decision_ids = [c["id"] for c in candidates if c["type"] == "decision"]
        if decision_ids:
            supersede_map = compute_supersede_info_batch(conn, decision_ids)
            for c in candidates:
                if c["type"] == "decision":
                    c["superseded"] = supersede_map.get(c["id"], {}).get("is_superseded", False)

        tags_by_key: dict[tuple[str, int], list[str]] = {}
        for etype, ids in ids_by_type.items():
            junction, col = _JUNCTION[etype]
            batch = get_entity_tags_batch(conn, junction, col, ids)
            for eid, tags in batch.items():
                tags_by_key[(etype, eid)] = tags
        for c in candidates:
            c["tags"] = tags_by_key.get((c["type"], c["id"]), [])

        child_ids_by_type = {t: ids for t, ids in ids_by_type.items() if t != "topic"}
        parent_titles = _fetch_parent_topic_titles_with_conn(conn, child_ids_by_type)
        for c in candidates:
            if c["type"] != "topic":
                c["parent_topic_title"] = parent_titles.get((c["type"], c["id"]))

        candidate_set = {(c["type"], c["id"]) for c in candidates}
        closure_warnings = _build_closure_warnings_with_conn(conn, candidates, candidate_set, raw_text_by_key)

        co_tags = None
        if tag_roots:
            co_tags = _compute_co_tags_with_conn(conn, tag_root_ids, seed_keys)

        filtered = [c for c in candidates if c["type"] in include_types_set]
        filtered.sort(key=lambda c: (c["depth"], c["type"], c["id"]))
        total_count = len(filtered)

        if limit is not None:
            page = filtered[offset : offset + limit]
            truncated = offset + limit < total_count
        else:
            page = filtered[offset:] if offset else filtered
            truncated = False

        for c in page:
            if not include_snippets:
                c.pop("snippet", None)
            strip_entity_id_inplace(c)

        result = {
            "candidates": page,
            "closure_warnings": closure_warnings,
            "total_count": total_count,
            "truncated": truncated,
        }
        if tag_roots:
            result["co_tags"] = co_tags
        return result
    except Exception as e:
        logger.error(f"collect_export_candidates failed: {e}")
        return {"error": {"code": "DATABASE_ERROR", "message": str(e)}}
    finally:
        conn.close()
