"""FTS5 + ベクトル ハイブリッド検索サービス"""
import logging
import math
import re
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from sqlite_vec import serialize_float32

from src.db import execute_query, get_connection, row_to_dict
from src.services import embedding_service
from src.services.readable_id import apply_readable_id_inplace
from src.services.tag_service import (
    get_entity_tags,
    get_entity_tags_batch,
    get_effective_tags,
    get_effective_tags_batch_by_ids,
    parse_tag,
)

logger = logging.getLogger(__name__)

SEARCHABLE_TYPES = {'topic', 'decision', 'activity', 'log', 'material'}
VALID_TYPES = SEARCHABLE_TYPES

GET_BY_IDS_MAX = 20

TYPE_TO_TABLE = {
    'topic': 'discussion_topics',
    'decision': 'decisions',
    'activity': 'activities',
    'log': 'discussion_logs',
    'material': 'materials',
}

# snippetソースの対応表: type → (テーブル名, カラム名)
SNIPPET_SOURCE = {
    'topic': ('discussion_topics', 'description'),
    'decision': ('decisions', 'decision'),
    'activity': ('activities', 'description'),
    'log': ('discussion_logs', 'content'),
}

SNIPPET_MAX_LEN = 200

# _tag_like_search: SQLiteパラメータ上限(999)超過を防ぐためのtag_id数制限
# パラメータ数 = 2 + 7 * len(matched_tag_ids) + 1 なので、100件で最大703パラメータ
TAG_LIKE_MAX_TAG_IDS = 100

# details付与パラメータ
DETAILS_MAX_RESULTS = 10

# retracted除外フィルタ: search_indexのsource_type='decision'/'log'のみ対象
# decision/logはretracted_atカラムを持つが、topic/activity/materialは持たない
RETRACT_FILTER_SQL = """
  AND NOT EXISTS (
    SELECT 1 FROM decisions d
    WHERE d.id = si.source_id AND si.source_type = 'decision' AND d.retracted_at IS NOT NULL
  )
  AND NOT EXISTS (
    SELECT 1 FROM discussion_logs dl
    WHERE dl.id = si.source_id AND si.source_type = 'log' AND dl.retracted_at IS NOT NULL
  )
"""
DETAILS_DESCRIPTION_MAX = 500
# RRFパラメータ
RRF_K = 60
RRF_W_FTS = 1.0
RRF_W_VEC = 1.0
RRF_W_TAG = 0.5

# Adaptive RRF: FTS/ベクトルのヒット数比率に応じて重みを動的調整
ADAPTIVE_RRF_ENABLED = True
ADAPTIVE_RRF_THRESHOLDS: tuple[tuple[float, float, float], ...] = (
    # (ratio上限, w_fts, w_vec) — ratioが小さい順に評価
    (0.2, 0.5, 1.5),
    (0.5, 0.8, 1.2),
)
assert all(
    ADAPTIVE_RRF_THRESHOLDS[i][0] < ADAPTIVE_RRF_THRESHOLDS[i + 1][0]
    for i in range(len(ADAPTIVE_RRF_THRESHOLDS) - 1)
), "ADAPTIVE_RRF_THRESHOLDS must be sorted in ascending order of threshold"

from src.config import RECENCY_DECAY_FLOOR, RECENCY_DECAY_RATE

# Query Expansion パラメータ
QE_DISTANCE_THRESHOLD = 0.3   # コサイン距離。これ未満のタグを拡張候補とする
QE_MAX_EXPANSIONS = 5          # 全キーワード合計での最大拡張タグ数
QE_EXCLUDE_NAMESPACES = True   # namespace付きタグを除外するか

# nearby_tags パラメータ
NEARBY_TAGS_LIMIT = 5          # 返却するnearby_tagsの最大件数


def _escape_fts5_query(keyword: str) -> str:
    """FTS5クエリ用のエスケープ処理。ダブルクォートで囲む。"""
    # ダブルクォート内のダブルクォートは2つ重ねてエスケープ
    escaped = keyword.replace('"', '""')
    return f'"{escaped}"'


def _expand_query_with_tags(keywords: list[str]) -> list[str]:
    """キーワードをtag_vec KNN検索で拡張する。

    各キーワードでtag_vecをKNN検索し、距離がQE_DISTANCE_THRESHOLD未満の
    素タグ（QE_EXCLUDE_NAMESPACES=True時はnamespace付きを除外）を
    FTSクエリに追加する拡張キーワードリストを返す。

    拡張されたキーワードは元のキーワードの末尾に追加される。
    元のキーワードと重複するタグは除外される。

    Args:
        keywords: 元のキーワードリスト

    Returns:
        拡張後のキーワードリスト（元のキーワード + 拡張タグ名）
    """
    expanded = list(keywords)
    existing = {kw.lower() for kw in keywords}
    expansion_count = 0

    try:
        # 全キーワードの類似タグ候補を収集
        candidate_tag_ids: list[tuple[int, float]] = []
        for kw in keywords:
            similar = embedding_service.search_similar_tags(kw, k=10)
            for tag_id, distance in similar:
                if distance < QE_DISTANCE_THRESHOLD:
                    candidate_tag_ids.append((tag_id, distance))

        if not candidate_tag_ids:
            return expanded

        # 候補タグのIDを一括で取得
        unique_ids = list({tid for tid, _ in candidate_tag_ids})
        placeholders = ",".join("?" * len(unique_ids))
        rows = execute_query(
            f"SELECT id, namespace, name FROM tags WHERE id IN ({placeholders})",
            tuple(unique_ids),
        )
        tag_info_map: dict[int, tuple[str, str]] = {}
        for row in rows:
            tag_info_map[row["id"]] = (row["namespace"], row["name"])

        # 距離順でソートして拡張タグを選定
        candidate_tag_ids.sort(key=lambda x: x[1])
        for tag_id, _distance in candidate_tag_ids:
            if expansion_count >= QE_MAX_EXPANSIONS:
                break

            info = tag_info_map.get(tag_id)
            if not info:
                continue

            namespace, name = info

            # namespace付きタグを除外
            if QE_EXCLUDE_NAMESPACES and namespace:
                continue

            # 元のキーワードとの重複チェック
            if name.lower() in existing:
                continue

            expanded.append(name)
            existing.add(name.lower())
            expansion_count += 1
    except Exception:
        logger.warning("Query expansion failed, using original keywords", exc_info=True)

    return expanded


def _attach_snippets(results: list[dict]) -> None:
    """検索結果にsnippetを付与する（in-place）。

    typeごとにバッチクエリでsnippetソースを取得し、先頭SNIPPET_MAX_LEN文字を
    snippetフィールドとして付与する。
    logのtitleが空の場合はcontentの先頭50文字をフォールバック表示する。
    """
    # typeごとにグループ化
    by_type: dict[str, list[dict]] = {}
    for item in results:
        by_type.setdefault(item["type"], []).append(item)

    for type_name, items in by_type.items():
        if type_name == "material":
            # material: title優先snippet ("title: content[:残り]" 形式)
            ids = [item["id"] for item in items]
            placeholders = ",".join("?" * len(ids))
            rows = execute_query(
                f"SELECT id, title, content FROM materials WHERE id IN ({placeholders})",
                tuple(ids),
            )
            snippet_map: dict[int, str] = {}
            for r in rows:
                title = r["title"] or ""
                content = r["content"] or ""
                prefix = f"{title}: "
                remaining = max(0, SNIPPET_MAX_LEN - len(prefix))
                snippet_map[r["id"]] = prefix + content[:remaining]
            for item in items:
                item["snippet"] = snippet_map.get(item["id"], "")
            continue

        if type_name not in SNIPPET_SOURCE:
            for item in items:
                item["snippet"] = ""
            continue
        table, column = SNIPPET_SOURCE[type_name]
        ids = [item["id"] for item in items]
        placeholders = ",".join("?" * len(ids))
        rows = execute_query(
            f"SELECT id, {column} FROM {table} WHERE id IN ({placeholders})",
            tuple(ids),
        )
        snippet_map = {r["id"]: (r[column] or "")[:SNIPPET_MAX_LEN] for r in rows}
        for item in items:
            item["snippet"] = snippet_map.get(item["id"], "")

        # log: titleが空の場合にcontentの先頭50文字をフォールバック
        if type_name == "log":
            for item in items:
                if not item["title"]:
                    item["title"] = snippet_map.get(item["id"], "")[:50]


def _attach_details(results: list[dict]) -> None:
    """検索結果にdetailsを付与する（in-place）。

    typeごとにバッチクエリで詳細情報を取得し、detailsフィールドとして付与する。
    - topic: description(500文字制限) + recent_decisions最大3件
    - activity: description(500文字制限) + status
    - decision: decision + reason全文
    - log: content先頭500文字
    - material: detailsは付与しない（snippetのまま）
    """
    if not results:
        return

    # typeごとにグループ化
    by_type: dict[str, list[dict]] = {}
    for item in results:
        by_type.setdefault(item["type"], []).append(item)

    for type_name, items in by_type.items():
        ids = [item["id"] for item in items]
        placeholders = ",".join("?" * len(ids))

        if type_name == "topic":
            # description取得
            rows = execute_query(
                f"SELECT id, description FROM discussion_topics WHERE id IN ({placeholders})",
                tuple(ids),
            )
            desc_map = {r["id"]: (r["description"] or "")[:DETAILS_DESCRIPTION_MAX] for r in rows}

            # recent_decisions取得（各topicの最新3件）
            # topic_idごとにまとめてクエリし、ROW_NUMBERで上位3件に絞る
            decision_rows = execute_query(
                f"""
                SELECT topic_id, decision, reason,
                       ROW_NUMBER() OVER (PARTITION BY topic_id ORDER BY id DESC) AS rn
                FROM decisions
                WHERE topic_id IN ({placeholders})
                """,
                tuple(ids),
            )
            decisions_map: dict[int, list[dict]] = {}
            for r in decision_rows:
                if r["rn"] <= 3:
                    decisions_map.setdefault(r["topic_id"], []).append({
                        "decision": r["decision"],
                        "reason": r["reason"],
                    })

            for item in items:
                item["details"] = {
                    "description": desc_map.get(item["id"], ""),
                    "recent_decisions": decisions_map.get(item["id"], []),
                }

        elif type_name == "activity":
            rows = execute_query(
                f"SELECT id, description, status FROM activities WHERE id IN ({placeholders})",
                tuple(ids),
            )
            detail_map = {
                r["id"]: {
                    "description": (r["description"] or "")[:DETAILS_DESCRIPTION_MAX],
                    "status": r["status"],
                }
                for r in rows
            }
            for item in items:
                item["details"] = detail_map.get(item["id"], {"description": "", "status": ""})

        elif type_name == "decision":
            rows = execute_query(
                f"SELECT id, decision, reason FROM decisions WHERE id IN ({placeholders})",
                tuple(ids),
            )
            detail_map = {
                r["id"]: {
                    "decision": r["decision"],
                    "reason": r["reason"],
                }
                for r in rows
            }
            for item in items:
                item["details"] = detail_map.get(item["id"], {"decision": "", "reason": ""})

        elif type_name == "log":
            rows = execute_query(
                f"SELECT id, content FROM discussion_logs WHERE id IN ({placeholders})",
                tuple(ids),
            )
            detail_map = {
                r["id"]: {
                    "content": (r["content"] or "")[:DETAILS_DESCRIPTION_MAX],
                }
                for r in rows
            }
            for item in items:
                item["details"] = detail_map.get(item["id"], {"content": ""})

        # material: detailsは付与しない（snippetのまま）


def _attach_tags(results: list[dict]) -> None:
    """検索結果にtagsを付与する（in-place）。

    typeごとに適切な方法でタグを取得する:
    - topic/activity: get_entity_tags_batch でバッチ取得
    - decision/log: get_effective_tags_batch_by_ids でバッチ取得（UNION継承）
    """
    if not results:
        return

    by_type: dict[str, list[dict]] = {}
    for item in results:
        by_type.setdefault(item["type"], []).append(item)

    conn = get_connection()
    try:
        for type_name, items in by_type.items():
            if type_name == "topic":
                ids = [item["id"] for item in items]
                tag_map = get_entity_tags_batch(conn, "topic_tags", "topic_id", ids)
                for item in items:
                    item["tags"] = tag_map.get(item["id"], [])
            elif type_name == "activity":
                ids = [item["id"] for item in items]
                tag_map = get_entity_tags_batch(conn, "activity_tags", "activity_id", ids)
                for item in items:
                    item["tags"] = tag_map.get(item["id"], [])
            elif type_name in ("decision", "log"):
                ids = [item["id"] for item in items]
                tags_map = get_effective_tags_batch_by_ids(conn, type_name, ids)
                for item in items:
                    item["tags"] = tags_map.get(item["id"], [])
            elif type_name == "material":
                # material: material_tagsから直接取得
                ids = [item["id"] for item in items]
                tag_map = get_entity_tags_batch(conn, "material_tags", "material_id", ids)
                for item in items:
                    item["tags"] = tag_map.get(item["id"], [])
            else:
                for item in items:
                    item["tags"] = []
    finally:
        conn.close()


# 共起テーブル定義: (テーブル名, エンティティIDカラム名)
_CO_OCCURRENCE_TABLES = [
    ("topic_tags", "topic_id"),
    ("decision_tags", "decision_id"),
    ("log_tags", "log_id"),
    ("activity_tags", "activity_id"),
    ("material_tags", "material_id"),
]


def _compute_nearby_tags(
    results: list[dict],
    query_tag_ids: list[int] | None,
    offset: int,
) -> list[dict]:
    """検索結果のタグ共起から関連タグを計算する。

    5タグテーブルのself-joinで共起関係を集計し、結果に含まれないタグを
    co_count降順で返す。namespace付きタグ(domain:/intent:)は除外。

    Args:
        results: _attach_tags済みの検索結果
        query_tag_ids: 検索フィルタに使用されたtag_id（除外用）
        offset: ページネーションオフセット

    Returns:
        [{"tag": "tag_name", "co_count": N}, ...]
    """
    if offset > 0 or not results:
        return []

    # resultsからタグ文字列を収集
    all_tag_strings: set[str] = set()
    for item in results:
        all_tag_strings.update(item.get("tags", []))

    if not all_tag_strings:
        return []

    conn = get_connection()
    try:
        # タグ文字列→tag_id解決（エイリアスも考慮）
        result_tag_ids = set(_resolve_tag_ids_readonly(conn, list(all_tag_strings)))

        if not result_tag_ids:
            return []

        # 除外セット: 結果タグ + クエリフィルタタグ
        exclude_ids = set(result_tag_ids)
        if query_tag_ids:
            exclude_ids.update(query_tag_ids)

        result_ids_list = list(result_tag_ids)
        exclude_ids_list = list(exclude_ids)

        ph_in = ",".join("?" * len(result_ids_list))
        ph_not = ",".join("?" * len(exclude_ids_list))

        # 各テーブルでself-join → テーブル単位でGROUP BY → UNION ALL
        unions = []
        params: list = []
        for table, id_col in _CO_OCCURRENCE_TABLES:
            unions.append(f"""
                SELECT t2.tag_id, COUNT(DISTINCT t1.{id_col}) AS co_count
                FROM {table} t1
                JOIN {table} t2 ON t1.{id_col} = t2.{id_col}
                WHERE t1.tag_id IN ({ph_in})
                  AND t2.tag_id NOT IN ({ph_not})
                GROUP BY t2.tag_id
            """)
            params.extend(result_ids_list)
            params.extend(exclude_ids_list)

        union_sql = " UNION ALL ".join(unions)
        query = f"""
            SELECT t.name, SUM(sub.co_count) AS total_co_count
            FROM ({union_sql}) sub
            JOIN tags t ON t.id = sub.tag_id
            WHERE t.namespace = ''
            GROUP BY sub.tag_id, t.name
            ORDER BY total_co_count DESC
            LIMIT ?
        """
        params.append(NEARBY_TAGS_LIMIT)

        rows = conn.execute(query, tuple(params)).fetchall()
        return [{"tag": row["name"], "co_count": row["total_co_count"]} for row in rows]
    finally:
        conn.close()


def _resolve_tag_ids_readonly(conn, tag_strings: list[str]) -> list[int]:
    """タグ文字列からtag_idを取得（SELECT ONLY、新規作成しない）。

    存在しないタグが含まれる場合、そのタグは無視される。
    全タグが存在しない場合は空リストを返す。
    エイリアスタグの場合はcanonical側のIDを返す。
    """
    tag_ids = []
    for tag_str in tag_strings:
        ns, name = parse_tag(tag_str)
        row = conn.execute(
            "SELECT id, canonical_id FROM tags WHERE namespace = ? AND name = ?",
            (ns, name)
        ).fetchone()
        if row:
            effective_id = row["canonical_id"] if row["canonical_id"] is not None else row["id"]
            tag_ids.append(effective_id)
    return tag_ids


def _build_tag_filter_cte(tag_ids: list[int]) -> tuple[str, list]:
    """タグフィルタ用のCTE SQLとパラメータを構築する。

    Returns:
        (cte_sql, params) のタプル。cte_sqlは "WITH tag_filtered AS (...)" の形式。
    """
    n_tags = len(tag_ids)
    placeholders = ",".join("?" * n_tags)

    cte_sql = f"""
    WITH tag_filtered AS (
        -- topic (直接タグ)
        SELECT 'topic' AS source_type, topic_id AS source_id FROM (
            SELECT tt.topic_id, tt.tag_id
            FROM topic_tags tt
            WHERE tt.tag_id IN ({placeholders})
        ) GROUP BY topic_id HAVING COUNT(DISTINCT tag_id) = ?

        UNION ALL
        -- activity (直接タグ)
        SELECT 'activity', activity_id FROM (
            SELECT at.activity_id, at.tag_id
            FROM activity_tags at
            WHERE at.tag_id IN ({placeholders})
        ) GROUP BY activity_id HAVING COUNT(DISTINCT tag_id) = ?

        UNION ALL
        -- decision (UNION継承)
        SELECT 'decision', decision_id FROM (
            SELECT d.id AS decision_id, tt.tag_id
            FROM decisions d JOIN topic_tags tt ON tt.topic_id = d.topic_id
            WHERE tt.tag_id IN ({placeholders})
            UNION
            SELECT dt.decision_id, dt.tag_id
            FROM decision_tags dt WHERE dt.tag_id IN ({placeholders})
        ) GROUP BY decision_id HAVING COUNT(DISTINCT tag_id) = ?

        UNION ALL
        -- log (UNION継承)
        SELECT 'log', log_id FROM (
            SELECT dl.id AS log_id, tt.tag_id
            FROM discussion_logs dl JOIN topic_tags tt ON tt.topic_id = dl.topic_id
            WHERE tt.tag_id IN ({placeholders})
            UNION
            SELECT lt.log_id, lt.tag_id
            FROM log_tags lt WHERE lt.tag_id IN ({placeholders})
        ) GROUP BY log_id HAVING COUNT(DISTINCT tag_id) = ?

        UNION ALL
        -- material (直接タグ)
        SELECT 'material', material_id FROM (
            SELECT mt.material_id, mt.tag_id
            FROM material_tags mt
            WHERE mt.tag_id IN ({placeholders})
        ) GROUP BY material_id HAVING COUNT(DISTINCT tag_id) = ?
    )
    """

    # パラメータ: 各セクションに tag_ids + n_tags を渡す
    params: list = []
    # topic
    params.extend(tag_ids)
    params.append(n_tags)
    # activity
    params.extend(tag_ids)
    params.append(n_tags)
    # decision (2つのIN句)
    params.extend(tag_ids)
    params.extend(tag_ids)
    params.append(n_tags)
    # log (2つのIN句)
    params.extend(tag_ids)
    params.extend(tag_ids)
    params.append(n_tags)
    # material (1つのIN句)
    params.extend(tag_ids)
    params.append(n_tags)

    return cte_sql, params


def _fts_search(
    keywords: list[str],
    tag_ids: Optional[list[int]],
    entity_type: Optional[str],
    limit: int,
    keyword_mode: str = "and",
    original_keyword_count: Optional[int] = None,
    date_after: Optional[str] = None,
    date_before: Optional[str] = None,
    include_retracted: bool = False,
) -> list[dict]:
    """FTS5検索。結果はBM25ランク順のリスト。

    Args:
        keywords: 検索キーワードリスト（QE拡張分を含む場合がある）
        tag_ids: タグフィルタ用のtag_idリスト
        entity_type: 検索対象の絞り込み
        entity_type: 検索対象の絞り込み
        limit: 取得件数上限
        keyword_mode: キーワード結合モード（"and" / "or"）
        original_keyword_count: 元のキーワード数。指定時、先頭N個をAND結合し
            残りをOR追加する（Query Expansion用）。未指定時は従来通り。
        date_after: 日付フィルタ（以降）
        date_before: 日付フィルタ（以前）
        include_retracted: Trueのとき取り消し済みdecision/logも含める
    """
    # OR時: 3文字以上のキーワードだけでFTS5クエリを組む（2文字はフィルタ除外）
    if keyword_mode == "or":
        fts_keywords = [kw for kw in keywords if len(kw) >= 3]
        if not fts_keywords:
            return []
        escaped_parts = [_escape_fts5_query(kw) for kw in fts_keywords]
        escaped_keyword = " OR ".join(escaped_parts)
    else:
        if original_keyword_count is not None and original_keyword_count < len(keywords):
            # QE拡張あり: 元キーワードをAND結合し、拡張タグをOR追加
            original_parts = [_escape_fts5_query(kw) for kw in keywords[:original_keyword_count]]
            expanded_parts = [_escape_fts5_query(kw) for kw in keywords[original_keyword_count:]]
            original_query = " AND ".join(original_parts)
            # (元kw1 AND 元kw2) OR 拡張1 OR 拡張2
            all_parts = [f"({original_query})"] + expanded_parts
            escaped_keyword = " OR ".join(all_parts)
        else:
            escaped_parts = [_escape_fts5_query(kw) for kw in keywords]
            escaped_keyword = " AND ".join(escaped_parts)

    # 日付フィルタの動的WHERE句構築
    date_clauses = []
    date_params: list = []
    if date_after:
        date_clauses.append("AND si.created_at >= ?")
        date_params.append(date_after)
    if date_before:
        date_clauses.append("AND si.created_at <= ?")
        date_params.append(date_before)
    date_sql = "\n          ".join(date_clauses)
    retract_sql = "" if include_retracted else RETRACT_FILTER_SQL

    if tag_ids:
        cte_sql, cte_params = _build_tag_filter_cte(tag_ids)
        query = f"""
        {cte_sql}
        SELECT
          si.source_type AS type,
          si.source_id AS id,
          si.title
        FROM search_index_fts
        JOIN search_index si ON si.id = search_index_fts.rowid
        JOIN tag_filtered tf ON tf.source_type = si.source_type AND tf.source_id = si.source_id
        WHERE search_index_fts MATCH ?
          AND (? IS NULL OR si.source_type = ?)
          {date_sql}
          {retract_sql}
        ORDER BY bm25(search_index_fts, 5.0, 1.0)
        LIMIT ?
        """
        params = (*cte_params, escaped_keyword, entity_type, entity_type, *date_params, limit)
    else:
        query = f"""
        SELECT
          si.source_type AS type,
          si.source_id AS id,
          si.title
        FROM search_index_fts
        JOIN search_index si ON si.id = search_index_fts.rowid
        WHERE search_index_fts MATCH ?
          AND (? IS NULL OR si.source_type = ?)
          {date_sql}
          {retract_sql}
        ORDER BY bm25(search_index_fts, 5.0, 1.0)
        LIMIT ?
        """
        params = (escaped_keyword, entity_type, entity_type, *date_params, limit)

    rows = execute_query(query, params)
    results = []
    for row in rows:
        r = row_to_dict(row)
        results.append({
            "type": r["type"],
            "id": r["id"],
            "title": r["title"],
        })
    return results


def _vector_search(
    keywords: list[str],
    tag_ids: Optional[list[int]],
    entity_type: Optional[str],
    limit: int,
    keyword_mode: str = "and",
    date_after: Optional[str] = None,
    date_before: Optional[str] = None,
    include_retracted: bool = False,
) -> Optional[list[dict]]:
    """ベクトル検索。ベクトル検索無効時はNoneを返す。"""
    try:
        # retractフィルタ（search_indexのsiエイリアスを使う版）
        # _vector_searchではsearch_indexにsiエイリアスがないクエリがあるため、
        # search_index直接参照版を使う
        retract_sql_direct = "" if include_retracted else """
  AND NOT EXISTS (
    SELECT 1 FROM decisions d
    WHERE d.id = search_index.source_id AND search_index.source_type = 'decision' AND d.retracted_at IS NOT NULL
  )
  AND NOT EXISTS (
    SELECT 1 FROM discussion_logs dl
    WHERE dl.id = search_index.source_id AND search_index.source_type = 'log' AND dl.retracted_at IS NOT NULL
  )
"""

        # 日付フィルタの動的WHERE句構築
        date_clauses = []
        date_params_list: list = []
        if date_after:
            date_clauses.append("AND created_at >= ?")
            date_params_list.append(date_after)
        if date_before:
            date_clauses.append("AND created_at <= ?")
            date_params_list.append(date_before)
        date_sql = "\n                      ".join(date_clauses)

        if keyword_mode == "or" and len(keywords) > 1:
            # OR時: 各キーワードで個別にベクトル検索し、結果をマージ
            merged: dict[tuple, dict] = {}  # key: (type, id)
            for kw in keywords:
                query_embedding = embedding_service.encode_query(kw)
                if query_embedding is None:
                    continue

                blob = serialize_float32(query_embedding)
                vec_rows = execute_query(
                    "SELECT rowid, distance FROM vec_index WHERE embedding MATCH ? AND k = ?",
                    (blob, limit),
                )
                if not vec_rows:
                    continue

                vec_data = {}
                for row in vec_rows:
                    r = row_to_dict(row)
                    vec_data[r["rowid"]] = r["distance"]

                rowids = list(vec_data.keys())
                rowid_placeholders = ",".join("?" * len(rowids))

                if tag_ids:
                    cte_sql, cte_params = _build_tag_filter_cte(tag_ids)
                    query = f"""
                    {cte_sql}
                    SELECT id, source_type, source_id, title
                    FROM search_index
                    WHERE id IN ({rowid_placeholders})
                      AND (? IS NULL OR source_type = ?)
                      AND EXISTS (
                        SELECT 1 FROM tag_filtered tf
                        WHERE tf.source_type = search_index.source_type
                          AND tf.source_id = search_index.source_id
                      )
                      {date_sql}
                      {retract_sql_direct}
                    """
                    params = (*cte_params, *rowids, entity_type, entity_type, *date_params_list)
                else:
                    query = f"""
                    SELECT id, source_type, source_id, title
                    FROM search_index
                    WHERE id IN ({rowid_placeholders})
                      AND (? IS NULL OR source_type = ?)
                      {date_sql}
                      {retract_sql_direct}
                    """
                    params = (*rowids, entity_type, entity_type, *date_params_list)

                filter_rows = execute_query(query, params)
                for row in filter_rows:
                    r = row_to_dict(row)
                    key = (r["source_type"], r["source_id"])
                    distance = vec_data[r["id"]]
                    if key not in merged or distance < merged[key]["distance"]:
                        merged[key] = {
                            "type": r["source_type"],
                            "id": r["source_id"],
                            "title": r["title"],
                            "distance": distance,
                        }

            if not merged:
                return None
            results = list(merged.values())
            results.sort(key=lambda x: x["distance"])
            return results
        else:
            # AND時: 従来通り（スペース結合して1 embedding）
            combined_keyword = " ".join(keywords)
            query_embedding = embedding_service.encode_query(combined_keyword)
            if query_embedding is None:
                return None

            blob = serialize_float32(query_embedding)

            # vec_indexからKNN取得（タグフィルタ不可なので多めに取得）
            # limitはsearch()側でlimit*5に拡大済み
            vec_rows = execute_query(
                "SELECT rowid, distance FROM vec_index WHERE embedding MATCH ? AND k = ?",
                (blob, limit),
            )

            if not vec_rows:
                return []

            vec_data = {}
            for row in vec_rows:
                r = row_to_dict(row)
                vec_data[r["rowid"]] = r["distance"]

            rowids = list(vec_data.keys())
            rowid_placeholders = ",".join("?" * len(rowids))

            if tag_ids:
                cte_sql, cte_params = _build_tag_filter_cte(tag_ids)
                query = f"""
                {cte_sql}
                SELECT id, source_type, source_id, title
                FROM search_index
                WHERE id IN ({rowid_placeholders})
                  AND (? IS NULL OR source_type = ?)
                  AND EXISTS (
                    SELECT 1 FROM tag_filtered tf
                    WHERE tf.source_type = search_index.source_type
                      AND tf.source_id = search_index.source_id
                  )
                  {date_sql}
                  {retract_sql_direct}
                """
                params = (*cte_params, *rowids, entity_type, entity_type, *date_params_list)
            else:
                query = f"""
                SELECT id, source_type, source_id, title
                FROM search_index
                WHERE id IN ({rowid_placeholders})
                  AND (? IS NULL OR source_type = ?)
                  {date_sql}
                  {retract_sql_direct}
                """
                params = (*rowids, entity_type, entity_type, *date_params_list)

            filter_rows = execute_query(query, params)

            results = []
            for row in filter_rows:
                r = row_to_dict(row)
                results.append({
                    "type": r["source_type"],
                    "id": r["source_id"],
                    "title": r["title"],
                    "distance": vec_data[r["id"]],
                })

            # distance順でソート（小さいほど類似度が高い）
            results.sort(key=lambda x: x["distance"])
            return results[:limit]

    except (ValueError, RuntimeError, OSError):
        logger.warning("Vector search failed, falling back to FTS-only", exc_info=True)
        return None


def find_similar_topics(
    text: str,
    exclude_id: int,
    limit: int = 3,
    embedding: list[float] | None = None,
) -> list[dict]:
    """テキストに類似するトピックをベクトル検索で取得する（自身を除外）。

    add_topic のレスポンスに含めるサジェスト用。
    embedding サーバー未起動時は空リストを返す。

    Args:
        text: 検索テキスト（title + description）
        exclude_id: 除外するトピックID（新規作成された自身）
        limit: 最大取得件数
        embedding: 事前生成済みのembeddingベクトル（指定時はencode_queryをスキップ）

    Returns:
        類似トピックのリスト [{id, title, distance}, ...]
    """
    try:
        query_embedding = embedding if embedding is not None else embedding_service.encode_query(text)
        if query_embedding is None:
            return []

        blob = serialize_float32(query_embedding)
        # 自身除外 + type フィルタ分を考慮して多めに取得
        vec_rows = execute_query(
            "SELECT rowid, distance FROM vec_index WHERE embedding MATCH ? AND k = ?",
            (blob, limit * 5),
        )
        if not vec_rows:
            return []

        vec_data = {}
        for row in vec_rows:
            r = row_to_dict(row)
            vec_data[r["rowid"]] = r["distance"]

        rowids = list(vec_data.keys())
        rowid_placeholders = ",".join("?" * len(rowids))

        filter_rows = execute_query(
            f"""
            SELECT id, source_type, source_id, title
            FROM search_index
            WHERE id IN ({rowid_placeholders})
              AND source_type = 'topic'
              AND source_id != ?
            """,
            (*rowids, exclude_id),
        )

        results = []
        for row in filter_rows:
            r = row_to_dict(row)
            results.append({
                "id": r["source_id"],
                "title": r["title"],
                "distance": round(vec_data[r["id"]], 4),
            })

        results.sort(key=lambda x: x["distance"])
        return results[:limit]

    except (ValueError, RuntimeError, OSError, sqlite3.Error):
        logger.warning("find_similar_topics failed", exc_info=True)
        return []


def find_similar_decisions(
    exclude_id: int,
    topic_id: int,
    text: str | None = None,
    embedding: list[float] | None = None,
    limit: int = 3,
) -> list[dict]:
    """同じtopic内でテキスト/embeddingに類似するdecisionをベクトル検索で取得する。

    add_decisions のレスポンスに含める「関連decision」サジェスト用。
    既存decisionのembedding（decision+reason+tagsで生成済み・vec_index格納済み）を
    KNNし、search_index経由でsource_type='decision'に絞り、decisionsテーブルへJOINして
    同一topic_id・retracted_at IS NULL・自身除外に限定する。
    矛盾・重複への気づきを促す導線であり、embeddingサーバー未起動時は空リストを返す。

    Args:
        exclude_id: 除外するdecision ID（新規作成された自身）
        topic_id: 関連decisionを絞り込む対象topic ID
        text: 検索テキスト（embedding未指定時のみ使う）
        embedding: 事前生成済みのembeddingベクトル（指定時はencode_queryをスキップ）
        limit: 最大取得件数

    Returns:
        類似decisionのリスト [{id, title, distance}, ...]
        title は title優先・decision本文fallback（COALESCE(title, decision)）。
        distance は小さいほど類似度が高い。
    """
    try:
        query_embedding = embedding if embedding is not None else (
            embedding_service.encode_query(text) if text else None
        )
        if query_embedding is None:
            return []

        blob = serialize_float32(query_embedding)
        # グローバルKNNで多めに取得してから decision型 + 同一topic + 自身除外 + retract除外 に
        # post-filterする。find_similar_topicsより絞り込みが厳しい（種別＋topic）ため候補数を
        # limit*20に増やす。本機能は矛盾・重複への気づき導線であり厳密なrecallは要求しない
        # （DB規模が非常に大きくなり同一topicのdecisionが上位に来ない場合は取りこぼし得るが、
        #  サジェストが減るだけで誤動作はしない）。将来はtopic絞り込み後KNNへの作り替え余地あり。
        vec_rows = execute_query(
            "SELECT rowid, distance FROM vec_index WHERE embedding MATCH ? AND k = ?",
            (blob, limit * 20),
        )
        if not vec_rows:
            return []

        vec_data = {}
        for row in vec_rows:
            r = row_to_dict(row)
            vec_data[r["rowid"]] = r["distance"]

        rowids = list(vec_data.keys())
        rowid_placeholders = ",".join("?" * len(rowids))

        filter_rows = execute_query(
            f"""
            SELECT si.id, si.source_id, COALESCE(d.title, d.decision) AS title
            FROM search_index si
            JOIN decisions d ON d.id = si.source_id
            WHERE si.id IN ({rowid_placeholders})
              AND si.source_type = 'decision'
              AND si.source_id != ?
              AND d.topic_id = ?
              AND d.retracted_at IS NULL
            """,
            (*rowids, exclude_id, topic_id),
        )

        results = []
        for row in filter_rows:
            r = row_to_dict(row)
            results.append({
                "id": r["source_id"],
                "title": r["title"],
                "distance": round(vec_data[r["id"]], 4),
            })

        results.sort(key=lambda x: x["distance"])
        return results[:limit]

    except (ValueError, RuntimeError, OSError, sqlite3.Error):
        logger.warning("find_similar_decisions failed", exc_info=True)
        return []


def _tag_like_search(
    keywords: list[str],
    tag_ids: Optional[list[int]],
    entity_type: Optional[str],
    limit: int,
    keyword_mode: str = "and",
    date_after: Optional[str] = None,
    date_before: Optional[str] = None,
    include_retracted: bool = False,
) -> list[dict]:
    """タグ名のLIKE検索。キーワードにマッチするタグを持つエンティティを返す。

    entity_tagsの各中間テーブルからタグ名LIKE検索し、
    search_index経由で結果を返す。

    ANDモードでは「全キーワードを名前に含む単一タグ」を探す。
    FTS/ベクトルのAND（複数語を含む文書）とは異なる意味論であり、
    マッチするのは "domain:api-design" のような複合タグ名に限られる。
    """
    # LIKEワイルドカード文字をエスケープ
    def _escape_like(s: str) -> str:
        return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    # 全キーワードのLIKEパターンを作成
    like_patterns = [f"%{_escape_like(kw)}%" for kw in keywords]

    # タグテーブルからキーワードにマッチするtag_idsを取得
    # name単体 or namespace:name の結合文字列に対してLIKE検索する
    tag_full_expr = "CASE WHEN namespace != '' THEN namespace || ':' || name ELSE name END"
    single_cond = f"(name LIKE ? ESCAPE '\\' OR {tag_full_expr} LIKE ? ESCAPE '\\')"
    if keyword_mode == "or":
        # OR: いずれかのキーワードにマッチするタグ
        conditions = " OR ".join([single_cond] * len(like_patterns))
        params: list = []
        for p in like_patterns:
            params.extend([p, p])
    else:
        # AND: すべてのキーワードにマッチするタグ（1つのタグ名が全キーワードを含む）
        conditions = " AND ".join([single_cond] * len(like_patterns))
        params = []
        for p in like_patterns:
            params.extend([p, p])

    matching_tags = execute_query(
        f"SELECT id FROM tags WHERE {conditions}",
        tuple(params),
    )
    if not matching_tags:
        return []

    matched_tag_ids = [r["id"] for r in matching_tags]

    # SQLiteパラメータ上限(999)超過を防止
    matched_tag_ids = matched_tag_ids[:TAG_LIKE_MAX_TAG_IDS]

    # tag_idsフィルタ: 指定がある場合は交差を取る
    if tag_ids:
        matched_tag_ids = [tid for tid in matched_tag_ids if tid in tag_ids]
        if not matched_tag_ids:
            return []

    # マッチしたタグを持つエンティティをsearch_index経由で取得
    tag_placeholders = ",".join("?" * len(matched_tag_ids))

    # 日付フィルタの動的WHERE句構築
    date_clauses = []
    date_params_tl: list = []
    if date_after:
        date_clauses.append("AND si.created_at >= ?")
        date_params_tl.append(date_after)
    if date_before:
        date_clauses.append("AND si.created_at <= ?")
        date_params_tl.append(date_before)
    date_sql = "\n      ".join(date_clauses)
    retract_sql = "" if include_retracted else RETRACT_FILTER_SQL

    # 各中間テーブルからエンティティを収集（UNION ALL）
    query = f"""
    SELECT DISTINCT si.source_type AS type, si.source_id AS id, si.title
    FROM search_index si
    WHERE
      (? IS NULL OR si.source_type = ?)
      {date_sql}
      {retract_sql}
      AND (
        EXISTS (
            SELECT 1 FROM topic_tags tt
            WHERE tt.topic_id = si.source_id AND si.source_type = 'topic'
              AND tt.tag_id IN ({tag_placeholders})
        )
        OR EXISTS (
            SELECT 1 FROM activity_tags at
            WHERE at.activity_id = si.source_id AND si.source_type = 'activity'
              AND at.tag_id IN ({tag_placeholders})
        )
        OR EXISTS (
            SELECT 1 FROM decision_tags dt
            WHERE dt.decision_id = si.source_id AND si.source_type = 'decision'
              AND dt.tag_id IN ({tag_placeholders})
        )
        OR EXISTS (
            SELECT 1 FROM log_tags lt
            WHERE lt.log_id = si.source_id AND si.source_type = 'log'
              AND lt.tag_id IN ({tag_placeholders})
        )
        OR EXISTS (
            SELECT 1 FROM material_tags mt
            WHERE mt.material_id = si.source_id AND si.source_type = 'material'
              AND mt.tag_id IN ({tag_placeholders})
        )
        OR EXISTS (
            SELECT 1 FROM decisions d
            JOIN topic_tags tt2 ON tt2.topic_id = d.topic_id
            WHERE d.id = si.source_id AND si.source_type = 'decision'
              AND tt2.tag_id IN ({tag_placeholders})
        )
        OR EXISTS (
            SELECT 1 FROM discussion_logs dl
            JOIN topic_tags tt3 ON tt3.topic_id = dl.topic_id
            WHERE dl.id = si.source_id AND si.source_type = 'log'
              AND tt3.tag_id IN ({tag_placeholders})
        )
      )
    ORDER BY si.id DESC
    LIMIT ?
    """
    # パラメータ: entity_type × 2 + date_params + matched_tag_ids × 7 + limit
    query_params = [entity_type, entity_type]
    query_params.extend(date_params_tl)
    for _ in range(7):
        query_params.extend(matched_tag_ids)
    query_params.append(limit)

    rows = execute_query(query, tuple(query_params))
    results = []
    for row in rows:
        r = row_to_dict(row)
        results.append({
            "type": r["type"],
            "id": r["id"],
            "title": r["title"],
        })
    return results



def _apply_recency_boost(results: list[dict], now: datetime | None = None) -> None:
    """RRFスコアにrecency boost（指数減衰）を適用する（in-place）。

    recency_factor = max(exp(-age_days * RECENCY_DECAY_RATE), RECENCY_DECAY_FLOOR)
    をスコアに乗算し、スコア降順で再ソートする。
    """
    if not results:
        return

    if now is None:
        now = datetime.now(timezone.utc)

    # typeごとにcreated_atをバッチ取得
    by_type: dict[str, list[dict]] = {}
    for item in results:
        by_type.setdefault(item["type"], []).append(item)

    for type_name, items in by_type.items():
        table = TYPE_TO_TABLE.get(type_name)
        if not table:
            continue
        ids = [item["id"] for item in items]
        placeholders = ",".join("?" * len(ids))
        rows = execute_query(
            f"SELECT id, created_at FROM {table} WHERE id IN ({placeholders})",
            tuple(ids),
        )
        created_map = {r["id"]: r["created_at"] for r in rows}
        for item in items:
            created_str = created_map.get(item["id"])
            if created_str:
                created = datetime.fromisoformat(created_str).replace(tzinfo=timezone.utc)
                age_days = max(0, (now - created).days)
                recency_factor = max(math.exp(-age_days * RECENCY_DECAY_RATE), RECENCY_DECAY_FLOOR)
                item["score"] *= recency_factor

    # スコア降順で再ソート
    results.sort(key=lambda x: x["score"], reverse=True)


def _compute_adaptive_weights(fts_count: int, vec_count: int) -> tuple[float, float]:
    """FTS/ベクトルのヒット数比率に応じてRRF重みを動的に算出する。

    ADAPTIVE_RRF_ENABLED=Falseまたはvec_count=0のときはデフォルト重みを返す。
    fts_count=0かつvec_count>0の場合はratio=0.0となり最もベクトル寄りの重みが適用される。
    タグLIKEの重み(RRF_W_TAG)は適応対象外。

    Returns:
        (w_fts, w_vec) のタプル
    """
    if not ADAPTIVE_RRF_ENABLED or vec_count == 0:
        return RRF_W_FTS, RRF_W_VEC
    ratio = fts_count / vec_count
    for threshold, w_fts, w_vec in ADAPTIVE_RRF_THRESHOLDS:
        if ratio < threshold:
            return w_fts, w_vec
    return RRF_W_FTS, RRF_W_VEC


def _rrf_merge(
    fts_results: list[dict],
    vec_results: list[dict],
    limit: int,
    tag_results: Optional[list[dict]] = None,
) -> list[dict]:
    """RRF（Reciprocal Rank Fusion）でFTS5・ベクトル・タグLIKE結果を統合する。"""
    scores: dict[tuple, dict] = {}  # key: (type, id)

    # Adaptive RRF: ヒット数比率に応じてFTS/ベクトルの重みを動的調整
    w_fts, w_vec = _compute_adaptive_weights(len(fts_results), len(vec_results))

    # FTS5結果にRRFスコアを付与（1始まりランク）
    for rank, item in enumerate(fts_results, start=1):
        key = (item["type"], item["id"])
        scores[key] = {
            "type": item["type"],
            "id": item["id"],
            "title": item["title"],
            "score": w_fts / (RRF_K + rank),
        }

    # ベクトル結果のRRFスコアを加算（1始まりランク）
    for rank, item in enumerate(vec_results, start=1):
        key = (item["type"], item["id"])
        vec_score = w_vec / (RRF_K + rank)
        if key in scores:
            scores[key]["score"] += vec_score
        else:
            scores[key] = {
                "type": item["type"],
                "id": item["id"],
                "title": item["title"],
                "score": vec_score,
            }

    # タグLIKE結果のRRFスコアを加算（1始まりランク）
    if tag_results:
        for rank, item in enumerate(tag_results, start=1):
            key = (item["type"], item["id"])
            tag_score = RRF_W_TAG / (RRF_K + rank)
            if key in scores:
                scores[key]["score"] += tag_score
            else:
                scores[key] = {
                    "type": item["type"],
                    "id": item["id"],
                    "title": item["title"],
                    "score": tag_score,
                }

    # 理論最大値で正規化（全ソース1位の場合のスコア）
    max_score = (w_fts + w_vec) / (RRF_K + 1)
    if tag_results:
        max_score += RRF_W_TAG / (RRF_K + 1)
    if max_score > 0:
        for item in scores.values():
            item["score"] = round(item["score"] / max_score, 4)

    # RRFスコア降順でソートし、上位limit件を返す
    merged = sorted(scores.values(), key=lambda x: x["score"], reverse=True)
    return merged[:limit]


def search(
    keyword: str | list[str],
    tags: Optional[list[str]] = None,
    entity_type: Optional[str] = None,
    limit: int = 10,
    offset: int = 0,
    keyword_mode: str = "and",
    include_details: bool = False,
    domain: Optional[str] = None,
    date_after: Optional[str] = None,
    date_before: Optional[str] = None,
    include_retracted: bool = False,
) -> dict:
    """
    キーワードで横断検索する。

    FTS5 trigramとベクトル検索のハイブリッド。RRFスコアで統合・ランキング。
    2文字以上のキーワードを指定する。
    配列で複数キーワードを渡すとAND検索（すべてを含む結果のみ返す）。
    keyword_mode="or"でOR検索（いずれかを含む結果を返す）。
    3文字以上: FTS5 + ベクトル検索のハイブリッド。
    2文字: ベクトル検索のみ（ベクトル検索無効時はエラー）。
    tagsでフィルタリング可能（AND結合）。未指定で全件検索。
    詳細情報が必要な場合は get_by_id(type, id) で取得する。

    Args:
        keyword: 検索キーワード（2文字以上）。配列で複数指定時はAND検索
        tags: タグフィルタ（AND条件。未指定=全件検索）
        entity_type: 検索対象の絞り込み（'topic', 'decision', 'activity', 'log', 'material'。未指定で全種類）
        entity_type: 検索対象の絞り込み（'topic', 'decision', 'activity', 'log', 'material'。未指定で全種類）
        limit: 取得件数上限（デフォルト10件、最大50件）
        offset: スキップ件数（デフォルト0）。ページネーション用
        keyword_mode: キーワード結合モード（"and" または "or"。デフォルト "and"）
        include_details: Trueのとき上位DETAILS_MAX_RESULTS件にdetailsを自動添付する（デフォルトFalse）
        domain: ドメインフィルタ。内部でtags=["domain:{domain}"]にマージされる
        date_after: 日付フィルタ（以降）。YYYY-MM-DD or YYYY-MM-DD HH:MM:SS形式
        date_before: 日付フィルタ（以前）。YYYY-MM-DD or YYYY-MM-DD HH:MM:SS形式

    Returns:
        検索結果一覧（type, id, title, score, snippet, tags）
        scoreは0〜1に正規化された関連度スコア（RRF理論最大値基準）。
        snippetは各typeの対応するソースカラムの先頭200文字（materialはtitle優先表示）。
        tagsはエンティティに紐づくタグ文字列のリスト。
        include_details=Trueの場合、上位DETAILS_MAX_RESULTS件にdetailsが追加される。
    """
    # keyword_modeバリデーション
    if keyword_mode not in ("and", "or"):
        return {
            "error": {
                "code": "INVALID_KEYWORD_MODE",
                "message": f"Invalid keyword_mode: {keyword_mode}. Must be 'and' or 'or'"
            }
        }

    # 正規化: str → list[str]
    if isinstance(keyword, str):
        keywords = [keyword.strip()]
    else:
        keywords = [k.strip() for k in keyword]

    # 空配列チェック
    if not keywords:
        return {
            "error": {
                "code": "KEYWORD_TOO_SHORT",
                "message": "keyword must be at least 2 characters"
            }
        }

    # バリデーション: 各要素2文字以上
    for kw in keywords:
        if len(kw) < 2:
            return {
                "error": {
                    "code": "KEYWORD_TOO_SHORT",
                    "message": "keyword must be at least 2 characters"
                }
            }

    # 空文字→None正規化（entity_type, domain, date_after, date_before）
    if entity_type is not None and entity_type == "":
        entity_type = None
    if domain is not None and domain == "":
        domain = None
    if date_after is not None and date_after == "":
        date_after = None
    if date_before is not None and date_before == "":
        date_before = None

    if entity_type is not None and entity_type not in SEARCHABLE_TYPES:
        return {
            "error": {
                "code": "INVALID_ENTITY_TYPE",
                "message": f"Invalid entity_type: {entity_type}. Must be one of {sorted(SEARCHABLE_TYPES)}"
            }
        }

    # 日付バリデーション（形式チェック + 値の妥当性チェック）
    date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2}:\d{2})?$")
    for param_name, param_value in [("date_after", date_after), ("date_before", date_before)]:
        if param_value is not None:
            if not date_pattern.match(param_value):
                return {
                    "error": {
                        "code": "INVALID_PARAMETER",
                        "message": f"{param_name} must be ISO date format (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS), got '{param_value}'",
                    }
                }
            try:
                fmt = "%Y-%m-%d %H:%M:%S" if len(param_value) > 10 else "%Y-%m-%d"
                datetime.strptime(param_value, fmt)
            except ValueError:
                return {
                    "error": {
                        "code": "INVALID_PARAMETER",
                        "message": f"{param_name} contains invalid date value: '{param_value}'",
                    }
                }

    # date_before: 日付のみ指定時に" 23:59:59"を付与して当日を含む
    if date_before is not None and len(date_before) == 10:
        date_before = date_before + " 23:59:59"

    # domain → tagsマージ（重複除去）
    if domain:
        domain_tag = f"domain:{domain}"
        if tags is None:
            tags = [domain_tag]
        elif domain_tag not in tags:
            tags.append(domain_tag)

    limit = max(1, min(limit, 50))
    offset = max(0, offset)

    try:
        # タグフィルタの解決
        tag_ids = None
        if tags:
            conn = get_connection()
            try:
                tag_ids = _resolve_tag_ids_readonly(conn, tags)
                # 指定タグの一部でもDBに存在しない場合、ANDフィルタは必ず空結果
                if len(tag_ids) < len(tags):
                    return {"results": [], "total_count": 0, "search_methods_used": []}
            finally:
                conn.close()

        # RRFで両ソースをマージした後にoffset+limitで切るため、各ソースから多めに取得する
        fetch_limit = (offset + limit) * 5

        # 使用された検索手法を追跡
        methods_used: list[str] = []

        # Query Expansion: tag_vec KNN検索でFTSクエリを拡張
        # ベクトル検索には元のキーワードをそのまま渡す
        fts_keywords = _expand_query_with_tags(keywords)
        if len(fts_keywords) > len(keywords):
            logger.info(
                "Query expanded: %s -> %s",
                keywords,
                fts_keywords[len(keywords):],
            )

        # FTS5検索判定
        min_len = min(len(kw) for kw in keywords)
        fts_results = []
        # QE拡張がある場合、元のキーワード数を記録
        original_kw_count = len(keywords) if len(fts_keywords) > len(keywords) else None

        if keyword_mode == "or":
            # OR時: 3文字以上のキーワードが1つでもあればFTSを使う
            if any(len(kw) >= 3 for kw in fts_keywords):
                fts_results = _fts_search(fts_keywords, tag_ids, entity_type, fetch_limit, keyword_mode, None, date_after, date_before, include_retracted=include_retracted)
                methods_used.append("fts5")
        else:
            # AND時（現行通り）: 全キーワードが3文字以上の場合のみ
            # QE拡張分はOR結合で追加されるため、元のキーワードの文字数チェックを使用
            if min_len >= 3:
                fts_results = _fts_search(fts_keywords, tag_ids, entity_type, fetch_limit, keyword_mode, original_kw_count, date_after, date_before, include_retracted=include_retracted)
                methods_used.append("fts5")

        # ベクトル検索（元のキーワードのまま、拡張なし）
        vec_results = _vector_search(keywords, tag_ids, entity_type, fetch_limit, keyword_mode, date_after, date_before, include_retracted=include_retracted)
        if vec_results is not None:
            methods_used.append("vector")

        # タグLIKE検索（キーワード長の制限なし）
        tag_like_results = _tag_like_search(keywords, tag_ids, entity_type, fetch_limit, keyword_mode, date_after, date_before, include_retracted=include_retracted)
        if tag_like_results:
            methods_used.append("tag_like")

        # 2文字キーワード + ベクトル検索無効 → エラー
        # OR時: 3文字以上が1つでもあればFTSで検索できるのでエラーにしない
        # タグLIKE検索結果がある場合もエラーにしない
        fts_available = (
            any(len(kw) >= 3 for kw in keywords) if keyword_mode == "or"
            else min_len >= 3
        )
        if not fts_available and vec_results is None and not tag_like_results:
            return {
                "error": {
                    "code": "KEYWORD_TOO_SHORT",
                    "message": "keyword must be at least 3 characters when vector search is unavailable"
                }
            }

        # RRF統合（recency boost前なのでfetch_limitで多めに保持）
        effective_vec = vec_results if vec_results is not None else []
        results = _rrf_merge(fts_results, effective_vec, fetch_limit, tag_results=tag_like_results)

        _apply_recency_boost(results)

        # recency boost後にoffset+limitで切り詰め
        total_count = len(results)
        results = results[offset:offset + limit]

        _attach_snippets(results)
        _attach_tags(results)

        if include_details:
            details_targets = results[:DETAILS_MAX_RESULTS]
            _attach_details(details_targets)

        nearby_tags = _compute_nearby_tags(results, tag_ids, offset)

        # α化: 各結果の id を「title (#NNN)」形式に置換し、元の id を id_raw に退避
        # type フィールドは α化前に確定済みなのでそのまま参照する
        for item in results:
            apply_readable_id_inplace(item, item["type"])

        return {
            "results": results,
            "total_count": total_count,
            "search_methods_used": methods_used,
            "nearby_tags": nearby_tags,
        }

    except Exception as e:
        return {
            "error": {
                "code": "DATABASE_ERROR",
                "message": str(e),
            }
        }


def _format_row(type_name: str, data: dict, tags: list[str]) -> dict:
    """typeに応じたレスポンス整形"""
    if type_name == 'topic':
        result = {
            "id": data["id"],
            "title": data["title"],
            "description": data["description"],
            "tags": tags,
            "created_at": data["created_at"],
        }
        apply_readable_id_inplace(result, "topic")
        return result
    elif type_name == 'decision':
        display_title = data.get("title") or (data["decision"] or "")[:50]
        result = {
            "id": data["id"],
            "topic_id": data["topic_id"],
            "title": display_title,
            "decision": data["decision"],
            "reason": data["reason"],
            "tags": tags,
            "created_at": data["created_at"],
        }
        if data.get("retracted_at"):
            result["retracted_at"] = data["retracted_at"]
        apply_readable_id_inplace(result, "decision")
        return result
    elif type_name == 'activity':
        result = {
            "id": data["id"],
            "title": data["title"],
            "description": data["description"],
            "status": data["status"],
            "tags": tags,
            "created_at": data["created_at"],
            "updated_at": data["updated_at"],
        }
        apply_readable_id_inplace(result, "activity")
        return result
    elif type_name == 'log':
        title = data["title"]
        if not title:
            title = data["content"][:50]
        result = {
            "id": data["id"],
            "topic_id": data["topic_id"],
            "title": title,
            "content": data["content"],
            "tags": tags,
            "created_at": data["created_at"],
        }
        if data.get("retracted_at"):
            result["retracted_at"] = data["retracted_at"]
        apply_readable_id_inplace(result, "log")
        return result
    elif type_name == 'material':
        result = {
            "material_id": data["id"],
            "title": data["title"],
            "tags": tags,
            "created_at": data["created_at"],
        }
        apply_readable_id_inplace(
            result, "material", id_key="material_id"
        )
        return result
    return data


def get_by_id(type: str, id: int, conn=None) -> dict:
    """
    search結果の詳細情報を取得する。

    searchツールで得られたtype + idの組み合わせを指定して、
    元データの完全な情報を取得する。

    Args:
        type: データ種別（'topic', 'decision', 'activity', 'log', 'material'）
        id: データのID
        conn: 既存のDB接続（省略時は内部で新規作成・クローズ）

    Returns:
        指定した種別に応じた詳細情報
    """
    if type not in VALID_TYPES:
        return {
            "error": {
                "code": "INVALID_TYPE",
                "message": f"Invalid type: {type}. Must be one of {sorted(VALID_TYPES)}"
            }
        }

    table = TYPE_TO_TABLE[type]

    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (id,)).fetchone()
        if not row:
            return {
                "error": {
                    "code": "NOT_FOUND",
                    "message": f"{type} with id {id} not found"
                }
            }

        # タグ取得: topic/activityはget_entity_tags、decision/logはget_effective_tags、materialはactivity_tags継承
        if type == 'topic':
            tags = get_entity_tags(conn, "topic_tags", "topic_id", id)
        elif type == 'activity':
            tags = get_entity_tags(conn, "activity_tags", "activity_id", id)
        elif type == 'decision':
            tags = get_effective_tags(conn, "decision", id)
        elif type == 'log':
            tags = get_effective_tags(conn, "log", id)
        elif type == 'material':
            # material: material_tagsから直接取得
            tags = get_entity_tags(conn, "material_tags", "material_id", id)
        else:
            tags = []

        return {"type": type, "data": _format_row(type, row_to_dict(row), tags)}

    except Exception as e:
        return {
            "error": {
                "code": "DATABASE_ERROR",
                "message": str(e),
            }
        }
    finally:
        if own_conn:
            conn.close()


def get_by_ids(items: list[dict]) -> dict:
    """
    複数のtype+idペアをバッチ取得する。

    Args:
        items: [{type: str, id: int}, ...] のリスト（最大20件）

    Returns:
        {"results": [get_by_idの結果, ...]}
    """
    if not items:
        return {"results": []}

    if len(items) > GET_BY_IDS_MAX:
        return {
            "error": {
                "code": "TOO_MANY_ITEMS",
                "message": f"Maximum {GET_BY_IDS_MAX} items allowed, got {len(items)}"
            }
        }

    conn = get_connection()
    try:
        results = []
        for item in items:
            item_type = item.get("type")
            item_id = item.get("id")
            if item_type is None or item_id is None:
                results.append({
                    "error": {
                        "code": "VALIDATION_ERROR",
                        "message": "Each item must have 'type' and 'id' fields"
                    }
                })
                continue
            result = get_by_id(item_type, item_id, conn=conn)
            results.append(result)

        return {"results": results}
    finally:
        conn.close()
