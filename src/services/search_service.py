"""FTS5 + ベクトル ハイブリッド検索サービス"""
import dataclasses
import json
import logging
import math
import re
import sqlite3
import textwrap
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Optional

from sqlite_vec import serialize_float32

from src.db import execute_query, get_connection, get_db_path, row_to_dict
from src.services import embedding_service, precedent_pure
from src.services.readable_id import apply_readable_id_inplace
from src.services.supersede_service import get_superseded_by_batch
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

# RRF / recency boost 後に offset+limit で切り詰めるための拡大係数。
# search() が retriever に渡す fetch_limit = (offset + limit) * FETCH_LIMIT_MULTIPLIER
FETCH_LIMIT_MULTIPLIER = 5


@dataclass(frozen=True)
class SearchContext:
    """search() のステージ間で受け渡す検索パラメータコンテキスト。

    フィールドは Validate / Normalize / Expand 完了後の確定値を保持する。
    frozen のため後段ステージで書き換える場合は dataclasses.replace を使う。

    fields:
        keywords: 正規化済み元キーワード（QE 拡張を含まない）
        fts_keywords: FTS5 用キーワード（QE 拡張済みの場合がある）
        original_keyword_count: QE 拡張時の元キーワード件数。未拡張時は None
        tag_ids: タグフィルタ用に解決済みの tag_id（未指定時は None）
        entity_type: 検索対象 entity_type フィルタ（未指定時は None）
        limit: 最終 limit（1..50）
        offset: ページネーション offset
        fetch_limit: 各 retriever に渡す多めの取得件数
        keyword_mode: "and" / "or"
        include_details: details 添付フラグ
        date_after: 日付フィルタ下限（補完なし）
        date_before: 日付フィルタ上限（日付のみ指定時は 23:59:59 補完済）
        domain: telemetry 用に保持する元 domain 引数
    """
    keywords: tuple[str, ...]
    fts_keywords: tuple[str, ...]
    original_keyword_count: Optional[int]
    tag_ids: Optional[tuple[int, ...]]
    entity_type: Optional[str]
    limit: int
    offset: int
    fetch_limit: int
    keyword_mode: Literal["and", "or"]
    include_details: bool
    date_after: Optional[str]
    date_before: Optional[str]
    domain: Optional[str]


def build_common_where(
    ctx: SearchContext,
    *,
    si_alias: str = "si",
) -> tuple[str, list]:
    """3 retriever で共通する WHERE 句（entity_type / date 範囲）を組み立てる。

    Args:
        ctx: SearchContext
        si_alias: 参照するテーブルエイリアス。空文字列の場合はカラム prefix なし
            （例: _vector_search の "FROM search_index ..." のような無エイリアス参照用）

    Returns:
        (sql_fragment, params)
        sql_fragment は "AND ..." で始まる WHERE 連結用フラグメント（複数 AND 句を
        改行で連結）。呼び出し元で SQL テンプレート内のインデントに合わせて
        textwrap.indent で字下げする想定。
        params は ? の順序に沿ったパラメータリスト。

    retract フィルタは search_index / FTS5 / vec_index からの物理削除モデルへ移行済の
    ため含めない。
    """
    prefix = f"{si_alias}." if si_alias else ""
    parts: list[str] = [f"AND (? IS NULL OR {prefix}source_type = ?)"]
    params: list = [ctx.entity_type, ctx.entity_type]

    if ctx.date_after:
        parts.append(f"AND {prefix}created_at >= ?")
        params.append(ctx.date_after)

    if ctx.date_before:
        parts.append(f"AND {prefix}created_at <= ?")
        params.append(ctx.date_before)

    return "\n".join(parts), params


def _exec_select(conn: sqlite3.Connection, query: str, params=()) -> list[sqlite3.Row]:
    """共有 conn 上で SELECT を実行する内部ヘルパ。

    旧 ``execute_query`` (自前で conn を開閉する版) と同じ ``sqlite3.Error`` ラップ規約
    (``"クエリ実行エラー: {e}"``) を維持するため、retriever 群はこのヘルパを使う。
    """
    try:
        return conn.execute(query, params).fetchall()
    except sqlite3.Error as e:
        raise sqlite3.Error(f"クエリ実行エラー: {e}") from e


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
            # topicごとにまとめてクエリし、ROW_NUMBERで上位3件に絞る
            decision_rows = execute_query(
                f"""
                SELECT r.target_id AS topic_id, d.decision, d.reason,
                       ROW_NUMBER() OVER (PARTITION BY r.target_id ORDER BY d.id DESC) AS rn
                FROM decisions d
                JOIN relations r ON r.source_type='decision' AND r.source_id=d.id
                                AND r.target_type='topic' AND r.relation_type='belongs_to'
                WHERE r.target_id IN ({placeholders})
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
            FROM decisions d
            JOIN relations r ON r.source_type='decision' AND r.source_id=d.id
                            AND r.target_type='topic' AND r.relation_type='belongs_to'
            JOIN topic_tags tt ON tt.topic_id = r.target_id
            WHERE tt.tag_id IN ({placeholders})
            UNION
            SELECT dt.decision_id, dt.tag_id
            FROM decision_tags dt WHERE dt.tag_id IN ({placeholders})
        ) GROUP BY decision_id HAVING COUNT(DISTINCT tag_id) = ?

        UNION ALL
        -- log (UNION継承)
        SELECT 'log', log_id FROM (
            SELECT dl.id AS log_id, tt.tag_id
            FROM discussion_logs dl
            JOIN relations r ON r.source_type='log' AND r.source_id=dl.id
                            AND r.target_type='topic' AND r.relation_type='belongs_to'
            JOIN topic_tags tt ON tt.topic_id = r.target_id
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


def fts_retrieve(ctx: SearchContext, conn: sqlite3.Connection) -> list[dict]:
    """FTS5 retriever。bm25 ランク順の結果リストを返す。

    retract 時に search_index / search_index_fts / vec_index から物理削除されるため、
    取り消し済みエンティティは検索対象に現れない。

    Args:
        ctx: SearchContext。ctx.fts_keywords / ctx.keyword_mode /
            ctx.original_keyword_count / ctx.tag_ids / ctx.entity_type /
            ctx.fetch_limit / ctx.date_after / ctx.date_before を参照する。
        conn: 呼出元 (orchestrator) が開いた共有 SQLite コネクション。
    """
    keywords = list(ctx.fts_keywords)

    # OR時: 3文字以上のキーワードだけでFTS5クエリを組む（2文字はフィルタ除外）
    if ctx.keyword_mode == "or":
        fts_keywords = [kw for kw in keywords if len(kw) >= 3]
        if not fts_keywords:
            return []
        escaped_parts = [_escape_fts5_query(kw) for kw in fts_keywords]
        escaped_keyword = " OR ".join(escaped_parts)
    else:
        original_kw_count = ctx.original_keyword_count
        if original_kw_count is not None and original_kw_count < len(keywords):
            # QE拡張あり: 元キーワードをAND結合し、拡張タグをOR追加
            original_parts = [_escape_fts5_query(kw) for kw in keywords[:original_kw_count]]
            expanded_parts = [_escape_fts5_query(kw) for kw in keywords[original_kw_count:]]
            original_query = " AND ".join(original_parts)
            # (元kw1 AND 元kw2) OR 拡張1 OR 拡張2
            all_parts = [f"({original_query})"] + expanded_parts
            escaped_keyword = " OR ".join(all_parts)
        else:
            escaped_parts = [_escape_fts5_query(kw) for kw in keywords]
            escaped_keyword = " AND ".join(escaped_parts)

    common_where, common_params = build_common_where(ctx, si_alias="si")
    common_where_indented = textwrap.indent(common_where, " " * 10).lstrip()
    tag_ids = list(ctx.tag_ids) if ctx.tag_ids else None

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
          {common_where_indented}
        ORDER BY bm25(search_index_fts, 5.0, 1.0)
        LIMIT ?
        """
        params = (*cte_params, escaped_keyword, *common_params, ctx.fetch_limit)
    else:
        query = f"""
        SELECT
          si.source_type AS type,
          si.source_id AS id,
          si.title
        FROM search_index_fts
        JOIN search_index si ON si.id = search_index_fts.rowid
        WHERE search_index_fts MATCH ?
          {common_where_indented}
        ORDER BY bm25(search_index_fts, 5.0, 1.0)
        LIMIT ?
        """
        params = (escaped_keyword, *common_params, ctx.fetch_limit)

    rows = _exec_select(conn, query, params)
    results = []
    for row in rows:
        r = row_to_dict(row)
        results.append({
            "type": r["type"],
            "id": r["id"],
            "title": r["title"],
        })
    return results


def vector_retrieve(ctx: SearchContext, conn: sqlite3.Connection) -> Optional[list[dict]]:
    """ベクトル retriever。ベクトル検索が無効/失敗時は None を返す。

    retract 時に vec_index から物理削除されるため、取り消し済みエンティティは
    KNN の候補スロットを食わない（KNN 実効 recall 改善）。

    Args:
        ctx: SearchContext。ベクトル検索は元の ctx.keywords を使用し、
            QE 拡張済みの ctx.fts_keywords は参照しない。
        conn: 呼出元 (orchestrator) が開いた共有 SQLite コネクション。
    """
    keywords = list(ctx.keywords)
    fetch_limit = ctx.fetch_limit
    tag_ids = list(ctx.tag_ids) if ctx.tag_ids else None

    try:
        common_where, common_params = build_common_where(ctx, si_alias="")
        common_where_or = textwrap.indent(common_where, " " * 22).lstrip()
        common_where_and = textwrap.indent(common_where, " " * 18).lstrip()

        if ctx.keyword_mode == "or" and len(keywords) > 1:
            # OR時: 各キーワードで個別にベクトル検索し、結果をマージ
            merged: dict[tuple, dict] = {}  # key: (type, id)
            for kw in keywords:
                query_embedding = embedding_service.encode_query(kw)
                if query_embedding is None:
                    continue

                blob = serialize_float32(query_embedding)
                vec_rows = _exec_select(
                    conn,
                    "SELECT rowid, distance FROM vec_index WHERE embedding MATCH ? AND k = ?",
                    (blob, fetch_limit),
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
                      AND EXISTS (
                        SELECT 1 FROM tag_filtered tf
                        WHERE tf.source_type = search_index.source_type
                          AND tf.source_id = search_index.source_id
                      )
                      {common_where_or}
                    """
                    params = (*cte_params, *rowids, *common_params)
                else:
                    query = f"""
                    SELECT id, source_type, source_id, title
                    FROM search_index
                    WHERE id IN ({rowid_placeholders})
                      {common_where_or}
                    """
                    params = (*rowids, *common_params)

                filter_rows = _exec_select(conn, query, params)
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
            # fetch_limitはsearch()側で (offset+limit)*FETCH_LIMIT_MULTIPLIER に拡大済み
            vec_rows = _exec_select(
                conn,
                "SELECT rowid, distance FROM vec_index WHERE embedding MATCH ? AND k = ?",
                (blob, fetch_limit),
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
                  AND EXISTS (
                    SELECT 1 FROM tag_filtered tf
                    WHERE tf.source_type = search_index.source_type
                      AND tf.source_id = search_index.source_id
                  )
                  {common_where_and}
                """
                params = (*cte_params, *rowids, *common_params)
            else:
                query = f"""
                SELECT id, source_type, source_id, title
                FROM search_index
                WHERE id IN ({rowid_placeholders})
                  {common_where_and}
                """
                params = (*rowids, *common_params)

            filter_rows = _exec_select(conn, query, params)

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
            return results[:fetch_limit]

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
            JOIN relations r ON r.source_type='decision' AND r.source_id=d.id
                            AND r.target_type='topic' AND r.relation_type='belongs_to'
            WHERE si.id IN ({rowid_placeholders})
              AND si.source_type = 'decision'
              AND si.source_id != ?
              AND r.target_id = ?
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


def tag_like_retrieve(ctx: SearchContext, conn: sqlite3.Connection) -> list[dict]:
    """タグ名 LIKE retriever。キーワードにマッチするタグを持つエンティティを返す。

    entity_tags の各中間テーブルからタグ名 LIKE 検索し、search_index 経由で結果を返す。

    AND モードでは「全キーワードを名前に含む単一タグ」を探す。
    FTS/ベクトルの AND（複数語を含む文書）とは異なる意味論であり、
    マッチするのは "domain:api-design" のような複合タグ名に限られる。

    Args:
        ctx: SearchContext。タグ LIKE 検索は元の ctx.keywords を使用し、
            QE 拡張済みの ctx.fts_keywords は参照しない。
        conn: 呼出元 (orchestrator) が開いた共有 SQLite コネクション。
    """
    keywords = list(ctx.keywords)
    tag_ids = list(ctx.tag_ids) if ctx.tag_ids else None

    # LIKEワイルドカード文字をエスケープ
    def _escape_like(s: str) -> str:
        return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    # 全キーワードのLIKEパターンを作成
    like_patterns = [f"%{_escape_like(kw)}%" for kw in keywords]

    # タグテーブルからキーワードにマッチするtag_idsを取得
    # name単体 or namespace:name の結合文字列に対してLIKE検索する
    tag_full_expr = "CASE WHEN namespace != '' THEN namespace || ':' || name ELSE name END"
    single_cond = f"(name LIKE ? ESCAPE '\\' OR {tag_full_expr} LIKE ? ESCAPE '\\')"
    if ctx.keyword_mode == "or":
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

    matching_tags = _exec_select(
        conn,
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

    common_where, common_params = build_common_where(ctx, si_alias="si")
    common_where_indented = textwrap.indent(common_where, " " * 6).lstrip()

    # 各中間テーブルからエンティティを収集（UNION ALL）
    # WHERE 1=1 を起点に common_where（"AND ..." で始まる）を連結する。
    query = f"""
    SELECT DISTINCT si.source_type AS type, si.source_id AS id, si.title
    FROM search_index si
    WHERE 1=1
      {common_where_indented}
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
            JOIN relations r2 ON r2.source_type='decision' AND r2.source_id=d.id
                             AND r2.target_type='topic' AND r2.relation_type='belongs_to'
            JOIN topic_tags tt2 ON tt2.topic_id = r2.target_id
            WHERE d.id = si.source_id AND si.source_type = 'decision'
              AND tt2.tag_id IN ({tag_placeholders})
        )
        OR EXISTS (
            SELECT 1 FROM discussion_logs dl
            JOIN relations r3 ON r3.source_type='log' AND r3.source_id=dl.id
                             AND r3.target_type='topic' AND r3.relation_type='belongs_to'
            JOIN topic_tags tt3 ON tt3.topic_id = r3.target_id
            WHERE dl.id = si.source_id AND si.source_type = 'log'
              AND tt3.tag_id IN ({tag_placeholders})
        )
      )
    ORDER BY si.id DESC
    LIMIT ?
    """
    # パラメータ: common_params + matched_tag_ids × 7 + fetch_limit
    query_params: list = list(common_params)
    for _ in range(7):
        query_params.extend(matched_tag_ids)
    query_params.append(ctx.fetch_limit)

    rows = _exec_select(conn, query, tuple(query_params))
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
    を score_breakdown.rrf_normalized に乗算して final_score を確定する。
    各結果に以下を付与する:
    - score_breakdown.recency_factor: 適用された減衰係数（created_at取得不可時は1.0）
    - final_score: rrf_normalized * recency_factor
    - score: final_score と同値（旧 API 互換のため残置）

    確定後、final_score 降順で再ソートする。
    """
    if not results:
        return

    if now is None:
        now = datetime.now(timezone.utc)

    # 初期値: score_breakdown が無い場合は score を rrf_normalized 相当として扱い、
    # recency_factor=1.0 で初期化。score_breakdown はあるが個別キーが欠ける場合も
    # 各キーを setdefault で補完する（_rrf_merge 経由なら既に全キー揃っている）。
    for item in results:
        bd = item.setdefault("score_breakdown", {})
        bd.setdefault("fts", 0.0)
        bd.setdefault("vec", 0.0)
        bd.setdefault("tag", 0.0)
        bd.setdefault("rrf_normalized", item.get("score", 0.0))
        bd.setdefault("recency_factor", 1.0)

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
                item["score_breakdown"]["recency_factor"] = recency_factor

    # final_score = rrf_normalized * recency_factor を確定
    for item in results:
        bd = item["score_breakdown"]
        final_score = bd["rrf_normalized"] * bd["recency_factor"]
        item["final_score"] = final_score
        item["score"] = final_score

    # final_score 降順で再ソート
    results.sort(key=lambda x: x["final_score"], reverse=True)


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
    """RRF（Reciprocal Rank Fusion）でFTS5・ベクトル・タグLIKE結果を統合する。

    各結果に以下のスコア内訳フィールドを付与する:
    - score_breakdown.fts: FTS5由来のRRF寄与（Adaptive RRF重み w_fts 適用後、理論最大値による正規化前）
    - score_breakdown.vec: ベクトル由来のRRF寄与（Adaptive RRF重み w_vec 適用後、理論最大値による正規化前）
    - score_breakdown.tag: タグLIKE由来のRRF寄与（RRF_W_TAG 適用後、理論最大値による正規化前）
    - score_breakdown.rrf_normalized: 3寄与の合計を理論最大値で割った正規化済みスコア（0〜1）

    重要: fts/vec の重みは `_compute_adaptive_weights` がヒット数比率で動的に決めるため、
    同一ランクでも検索ごとに値の絶対値が異なりうる。クロス検索での絶対比較には向かない。

    recency_factor / final_score は後段の `_apply_recency_boost` で付与される。
    既存 score フィールドは互換のため最終的な final_score と同値で残置する。

    前提: 各ソース (fts_results / vec_results / tag_results) 内に同一 (type, id) の重複は無い
    ことを呼出元が保証する。重複があると `+=` により寄与が二重に加算され rrf_normalized が
    1.0 を超える可能性がある。現状の `_fts_search` / `_vector_search` / `_tag_like_search` は
    重複を返さない実装になっている。
    """
    scores: dict[tuple, dict] = {}  # key: (type, id)

    # Adaptive RRF: ヒット数比率に応じてFTS/ベクトルの重みを動的調整
    w_fts, w_vec = _compute_adaptive_weights(len(fts_results), len(vec_results))

    def _ensure_entry(item: dict) -> dict:
        key = (item["type"], item["id"])
        if key not in scores:
            scores[key] = {
                "type": item["type"],
                "id": item["id"],
                "title": item["title"],
                "score_breakdown": {"fts": 0.0, "vec": 0.0, "tag": 0.0},
            }
        return scores[key]

    # FTS5結果にRRFスコアを付与（1始まりランク）
    for rank, item in enumerate(fts_results, start=1):
        entry = _ensure_entry(item)
        entry["score_breakdown"]["fts"] += w_fts / (RRF_K + rank)

    # ベクトル結果のRRFスコアを加算（1始まりランク）
    for rank, item in enumerate(vec_results, start=1):
        entry = _ensure_entry(item)
        entry["score_breakdown"]["vec"] += w_vec / (RRF_K + rank)

    # タグLIKE結果のRRFスコアを加算（1始まりランク）
    if tag_results:
        for rank, item in enumerate(tag_results, start=1):
            entry = _ensure_entry(item)
            entry["score_breakdown"]["tag"] += RRF_W_TAG / (RRF_K + rank)

    # 理論最大値で正規化（全ソース1位の場合のスコア）
    max_score = (w_fts + w_vec) / (RRF_K + 1)
    if tag_results:
        max_score += RRF_W_TAG / (RRF_K + 1)
    for entry in scores.values():
        bd = entry["score_breakdown"]
        # 注: rrf_normalized は丸め前の生 raw_sum から算出する。
        # 公開フィールド fts/vec/tag は表示用に round(.,6) するため、
        # それらを足して max_score で割っても rrf_normalized と微小（≤1e-6 程度）にずれる。
        raw_sum = bd["fts"] + bd["vec"] + bd["tag"]
        rrf_normalized = round(raw_sum / max_score, 4) if max_score > 0 else 0.0
        bd["fts"] = round(bd["fts"], 6)
        bd["vec"] = round(bd["vec"], 6)
        bd["tag"] = round(bd["tag"], 6)
        bd["rrf_normalized"] = rrf_normalized
        # recency boost 前のスコアを score にセット (旧 API 互換)。
        # _apply_recency_boost で recency_factor 乗算後に final_score を確定する。
        entry["score"] = rrf_normalized

    # RRFスコア降順でソートし、上位limit件を返す
    merged = sorted(scores.values(), key=lambda x: x["score"], reverse=True)
    return merged[:limit]


class _SearchEarlyReturn(Exception):
    """search パイプラインのステージから早期 return を要求するための内部 sentinel。

    バリデーションエラーやタグ未登録などで途中ステージが「ここで終了して
    特定のレスポンスを返したい」と判断したとき、orchestrator まで例外で抜けて
    レスポンス dict を組み立てる。
    """

    def __init__(self, response: dict):
        self.response = response
        super().__init__()


def _validate(
    keyword: str | list[str],
    keyword_mode: str,
    entity_type: Optional[str],
    domain: Optional[str],
    date_after: Optional[str],
    date_before: Optional[str],
) -> tuple[list[str], Optional[str], Optional[str], Optional[str], Optional[str]]:
    """search 引数のバリデーションと表層的な正規化 (空文字 → None, strip) を行う。

    Returns:
        (keywords, entity_type, domain, date_after, date_before) — 検証後の値。

    Raises:
        _SearchEarlyReturn: バリデーションエラー時、対応する error dict を載せて投げる。
    """
    if keyword_mode not in ("and", "or"):
        raise _SearchEarlyReturn({
            "error": {
                "code": "INVALID_KEYWORD_MODE",
                "message": f"Invalid keyword_mode: {keyword_mode}. Must be 'and' or 'or'",
            }
        })

    if isinstance(keyword, str):
        keywords = [keyword.strip()]
    else:
        keywords = [k.strip() for k in keyword]

    if not keywords:
        raise _SearchEarlyReturn({
            "error": {
                "code": "KEYWORD_TOO_SHORT",
                "message": "keyword must be at least 2 characters",
            }
        })

    for kw in keywords:
        if len(kw) < 2:
            raise _SearchEarlyReturn({
                "error": {
                    "code": "KEYWORD_TOO_SHORT",
                    "message": "keyword must be at least 2 characters",
                }
            })

    if entity_type == "":
        entity_type = None
    if domain == "":
        domain = None
    if date_after == "":
        date_after = None
    if date_before == "":
        date_before = None

    if entity_type is not None and entity_type not in SEARCHABLE_TYPES:
        raise _SearchEarlyReturn({
            "error": {
                "code": "INVALID_ENTITY_TYPE",
                "message": f"Invalid entity_type: {entity_type}. Must be one of {sorted(SEARCHABLE_TYPES)}",
            }
        })

    date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2}:\d{2})?$")
    for param_name, param_value in [("date_after", date_after), ("date_before", date_before)]:
        if param_value is None:
            continue
        if not date_pattern.match(param_value):
            raise _SearchEarlyReturn({
                "error": {
                    "code": "INVALID_PARAMETER",
                    "message": f"{param_name} must be ISO date format (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS), got '{param_value}'",
                }
            })
        try:
            fmt = "%Y-%m-%d %H:%M:%S" if len(param_value) > 10 else "%Y-%m-%d"
            datetime.strptime(param_value, fmt)
        except ValueError:
            raise _SearchEarlyReturn({
                "error": {
                    "code": "INVALID_PARAMETER",
                    "message": f"{param_name} contains invalid date value: '{param_value}'",
                }
            })

    return keywords, entity_type, domain, date_after, date_before


def _normalize(
    keywords: list[str],
    tags: Optional[list[str]],
    entity_type: Optional[str],
    domain: Optional[str],
    date_after: Optional[str],
    date_before: Optional[str],
    limit: int,
    offset: int,
    keyword_mode: str,
    include_details: bool,
    conn: sqlite3.Connection,
) -> tuple[SearchContext, Optional[list[int]], Optional[list[str]]]:
    """SearchContext を組み立てるステージ。

    - domain → tags マージ (元 list があれば破壊的更新、None なら新規生成)
    - date_before に時刻が無ければ " 23:59:59" 補完
    - limit / offset の範囲補正
    - fetch_limit = (offset + limit) * FETCH_LIMIT_MULTIPLIER を計算
    - tag_ids を DB 解決 (一部が見つからなければ早期 return = 空結果)

    Returns:
        (ctx, query_tag_ids, effective_tags) — effective_tags は domain 補完後の tags
        (telemetry parameters 用に保持)。

    Raises:
        _SearchEarlyReturn: 指定タグの一部が DB に存在しないとき、空結果 dict を載せて投げる。
    """
    if domain:
        domain_tag = f"domain:{domain}"
        if tags is None:
            tags = [domain_tag]
        elif domain_tag not in tags:
            tags.append(domain_tag)

    if date_before is not None and len(date_before) == 10:
        date_before = date_before + " 23:59:59"

    limit = max(1, min(limit, 50))
    offset = max(0, offset)

    tag_ids: Optional[list[int]] = None
    if tags:
        tag_ids = _resolve_tag_ids_readonly(conn, tags)
        # 指定タグの一部でも DB に存在しない場合、AND フィルタは必ず空結果
        if len(tag_ids) < len(tags):
            raise _SearchEarlyReturn({
                "results": [],
                "total_count": 0,
                "search_methods_used": [],
            })

    fetch_limit = (offset + limit) * FETCH_LIMIT_MULTIPLIER

    ctx = SearchContext(
        keywords=tuple(keywords),
        fts_keywords=tuple(keywords),
        original_keyword_count=None,
        tag_ids=tuple(tag_ids) if tag_ids is not None else None,
        entity_type=entity_type,
        limit=limit,
        offset=offset,
        fetch_limit=fetch_limit,
        keyword_mode=keyword_mode,
        include_details=include_details,
        date_after=date_after,
        date_before=date_before,
        domain=domain,
    )
    return ctx, tag_ids, tags


def _expand(ctx: SearchContext) -> SearchContext:
    """Query Expansion: tag_vec KNN で fts_keywords を構築した新 ctx を返す。

    元キーワードは ctx.keywords に保持されたまま。ベクトル検索・タグ LIKE 検索は
    元キーワードを使い、FTS のみが拡張済み fts_keywords を使う。
    """
    fts_keywords = _expand_query_with_tags(list(ctx.keywords))
    original_kw_count: Optional[int] = None
    if len(fts_keywords) > len(ctx.keywords):
        logger.info(
            "Query expanded: %s -> %s",
            ctx.keywords,
            fts_keywords[len(ctx.keywords):],
        )
        original_kw_count = len(ctx.keywords)

    return dataclasses.replace(
        ctx,
        fts_keywords=tuple(fts_keywords),
        original_keyword_count=original_kw_count,
    )


def _retrieve(ctx: SearchContext, conn: sqlite3.Connection) -> dict:
    """3 retriever (fts / vector / tag_like) を逐次呼び出して結果を集約する。

    Returns:
        {"fts": list[dict], "vec": list[dict] | None, "tag": list[dict],
         "methods_used": list[str]}

    Raises:
        _SearchEarlyReturn: 2 文字以下キーワード + ベクトル無効 + tag_like 空、で
            ハイブリッド検索が成立しないとき KEYWORD_TOO_SHORT を投げる。
    """
    keywords = list(ctx.keywords)
    fts_keywords = list(ctx.fts_keywords)
    min_len = min(len(kw) for kw in keywords)
    methods_used: list[str] = []

    fts_results: list[dict] = []
    if ctx.keyword_mode == "or":
        # OR時: 3文字以上のキーワードが1つでもあればFTSを使う
        if any(len(kw) >= 3 for kw in fts_keywords):
            fts_results = fts_retrieve(ctx, conn)
            methods_used.append("fts5")
    else:
        # AND時: 全キーワードが3文字以上のときのみ FTS を使う
        # (QE 拡張分は OR 結合で追加されるので、元キーワードの文字数で判定する)
        if min_len >= 3:
            fts_results = fts_retrieve(ctx, conn)
            methods_used.append("fts5")

    vec_results = vector_retrieve(ctx, conn)
    if vec_results is not None:
        methods_used.append("vector")

    tag_like_results = tag_like_retrieve(ctx, conn)
    if tag_like_results:
        methods_used.append("tag_like")

    fts_available = (
        any(len(kw) >= 3 for kw in keywords) if ctx.keyword_mode == "or"
        else min_len >= 3
    )
    if not fts_available and vec_results is None and not tag_like_results:
        raise _SearchEarlyReturn({
            "error": {
                "code": "KEYWORD_TOO_SHORT",
                "message": "keyword must be at least 3 characters when vector search is unavailable",
            }
        })

    return {
        "fts": fts_results,
        "vec": vec_results,
        "tag": tag_like_results,
        "methods_used": methods_used,
    }


def _merge(ctx: SearchContext, retrieval: dict) -> list[dict]:
    """RRF 統合ステージ。各 result に score_breakdown.{fts,vec,tag,rrf_normalized} を付与する。"""
    effective_vec = retrieval["vec"] if retrieval["vec"] is not None else []
    return _rrf_merge(
        retrieval["fts"],
        effective_vec,
        ctx.fetch_limit,
        tag_results=retrieval["tag"],
    )


def _rerank(ctx: SearchContext, merged: list[dict]) -> list[dict]:
    """recency boost ステージ。score_breakdown.recency_factor / final_score を確定し、final_score 降順に再ソートする。

    ``ctx`` は他ステージとシグネチャを揃えるために受け取るが、現状の recency 減衰は
    created_at と RECENCY_DECAY_RATE / RECENCY_DECAY_FLOOR のみで決まるため、本関数内では
    ``ctx`` を参照しない。ctx 依存のブースト (domain 別重み付け等) を後段で追加する余地を
    残しておく。
    """
    _apply_recency_boost(merged)
    return merged


def _slice(ctx: SearchContext, results: list[dict]) -> tuple[list[dict], int]:
    """offset + limit で切り出す。total_count は切り出し前の件数。"""
    total_count = len(results)
    sliced = results[ctx.offset:ctx.offset + ctx.limit]
    return sliced, total_count


def _attach_superseded_by(results: list[dict]) -> None:
    """decision タイプの結果に superseded_by を付与する (in-place)。

    supersede されている decision の最新 superseder id を「早期警告」として乗せる軽量
    マーカー。詳細な chain は get_decisions 側で取り直す前提のため、supersede されて
    いなければ None、複数 superseder があれば最新1件のみ返す。
    """
    decision_ids = [item["id"] for item in results if item["type"] == "decision"]
    if not decision_ids:
        return
    conn = get_connection()
    try:
        superseded_by_map = get_superseded_by_batch(conn, decision_ids)
    finally:
        conn.close()
    for item in results:
        if item["type"] == "decision":
            item["superseded_by"] = superseded_by_map.get(item["id"])


def _decorate(
    ctx: SearchContext,
    sliced: list[dict],
    query_tag_ids: Optional[list[int]],
) -> tuple[list[dict], list[dict]]:
    """検索結果に snippet / tags / details / superseded_by / readable_id を付与し、nearby_tags を計算する。

    ``sliced`` は in-place で書き換わるが、データフローを明示するため戻り値にも含める。
    呼出元 (orchestrator) は ``decorated, nearby_tags = _decorate(...)`` のパターンで
    両方を受け取る。

    Returns:
        (decorated, nearby_tags)
        decorated: 引数 sliced と同一の list (in-place 装飾済)。
        nearby_tags: [{"tag": "...", "co_count": N}, ...]。offset>0 の場合は空リスト。
    """
    _attach_snippets(sliced)
    _attach_tags(sliced)
    _attach_superseded_by(sliced)
    if ctx.include_details:
        _attach_details(sliced[:DETAILS_MAX_RESULTS])

    nearby_tags = _compute_nearby_tags(sliced, query_tag_ids, ctx.offset)

    # readable_id 化: 各結果の id を削除し、整数 id を id_raw に退避する
    # type フィールドは適用前に確定済みなのでそのまま参照する
    for item in sliced:
        apply_readable_id_inplace(item, item["type"])

    return sliced, nearby_tags


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
        limit: 取得件数上限（デフォルト10件、最大50件）
        offset: スキップ件数（デフォルト0）。ページネーション用
        keyword_mode: キーワード結合モード（"and" または "or"。デフォルト "and"）
        include_details: Trueのとき上位DETAILS_MAX_RESULTS件にdetailsを自動添付する（デフォルトFalse）
        domain: ドメインフィルタ。内部でtags=["domain:{domain}"]にマージされる
        date_after: 日付フィルタ（以降）。YYYY-MM-DD or YYYY-MM-DD HH:MM:SS形式
        date_before: 日付フィルタ（以前）。YYYY-MM-DD or YYYY-MM-DD HH:MM:SS形式

    Returns:
        検索結果一覧（type, id, title, score, final_score, score_breakdown, snippet, tags）。
        final_score は 0〜1 に正規化された関連度スコア（RRF理論最大値基準 × recency減衰）。
        score_breakdown は以下のサブフィールドを持つ:
          - fts / vec / tag: 各ソースのRRF生寄与（正規化前、recency適用前）
          - rrf_normalized: 3寄与の合計を理論最大値で割った正規化済み値（0〜1、recency適用前）
          - recency_factor: created_atに基づく指数減衰係数（0〜1）
        既存 score フィールドは final_score と同値で互換のため残置されている。
        snippetは各typeの対応するソースカラムの先頭200文字（materialはtitle優先表示）。
        tagsはエンティティに紐づくタグ文字列のリスト。
        include_details=Trueの場合、上位DETAILS_MAX_RESULTS件にdetailsが追加される。
    """
    try:
        keywords, entity_type, domain, date_after, date_before = _validate(
            keyword, keyword_mode, entity_type, domain, date_after, date_before,
        )

        conn = get_connection()
        try:
            ctx, query_tag_ids, effective_tags = _normalize(
                keywords, tags, entity_type, domain, date_after, date_before,
                limit, offset, keyword_mode, include_details, conn,
            )
            ctx = _expand(ctx)
            retrieval = _retrieve(ctx, conn)
            merged = _merge(ctx, retrieval)
            merged = _rerank(ctx, merged)
            sliced, total_count = _slice(ctx, merged)
            sliced, nearby_tags = _decorate(ctx, sliced, query_tag_ids)
        finally:
            conn.close()

        _record_search_telemetry_async(
            query=keyword,
            parameters={
                "tags": effective_tags,
                "entity_type": entity_type,
                "limit": ctx.limit,
                "offset": ctx.offset,
                "keyword_mode": keyword_mode,
                "include_details": include_details,
                "domain": domain,
                "date_after": date_after,
                "date_before": date_before,
            },
            result_count=total_count,
        )

        return {
            "results": sliced,
            "total_count": total_count,
            "search_methods_used": retrieval["methods_used"],
            "nearby_tags": nearby_tags,
        }

    except _SearchEarlyReturn as early:
        return early.response
    except Exception as e:
        return {
            "error": {
                "code": "DATABASE_ERROR",
                "message": str(e),
            }
        }


def _telemetry_get_connection() -> sqlite3.Connection:
    """telemetry 書込専用の軽量コネクション。

    `search_telemetry` への INSERT は sqlite-vec 拡張を必要としないため、
    `db.get_connection()` の `enable_load_extension(True)` → 拡張ロード →
    `enable_load_extension(False)` のオーバーヘッドや拡張ロード失敗時の
    warning ログを避ける目的で、最小構成（WAL + busy_timeout）のみ設定する。
    daemon thread から呼ばれることを想定。
    """
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _record_search_telemetry_async(
    query: str | list[str],
    parameters: dict,
    result_count: int,
) -> threading.Thread | None:
    """search 呼出の telemetry を別スレッドで非同期書込する。

    search() のレスポンスタイムに影響しないよう daemon thread で走らせる。
    書込中の例外は logger.warning に出して握りつぶし、search 本体を壊さない。
    Thread 生成や start 自体が失敗（e.g. ``RuntimeError: can't start new thread``）
    した場合も、呼出元 search() の外側 try で DATABASE_ERROR 化されないよう
    ここで握って warning + None 返却にする。

    書込は `_telemetry_get_connection()` 経由で sqlite-vec 拡張を
    ロードしない軽量コネクションを使う。

    Returns:
        起動した daemon Thread。起動に失敗した場合は None。
    """
    def _write() -> None:
        try:
            query_json = json.dumps(query, ensure_ascii=False)
            parameters_json = json.dumps(parameters, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            logger.warning("search_telemetry serialize failed: %s", e)
            return

        try:
            conn = _telemetry_get_connection()
            try:
                conn.execute(
                    "INSERT INTO search_telemetry (query, parameters, result_count) "
                    "VALUES (?, ?, ?)",
                    (query_json, parameters_json, int(result_count)),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.warning("search_telemetry write failed: %s", e)

    try:
        thread = threading.Thread(target=_write, daemon=True)
        thread.start()
    except Exception as e:
        logger.warning("search_telemetry thread start failed: %s", e)
        return None
    return thread


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
        precedent_pure.attach_precedent(result, data.get("reason"))
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
            "content": data["content"],
            "source": data["source"],
            "tags": tags,
            "created_at": data["created_at"],
            "hint": "contentの先頭1-2文は内容の説明・要約にしてください（check-in時にsnippetとして表示されます）",
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

        data = row_to_dict(row)
        # decision/log の親 topic は relations.belongs_to 経由で解決し data に詰める
        # (DB カラムから物理削除済みのため、_format_row が data["topic_id"] を参照できるよう補完)
        if type in ('decision', 'log'):
            r = conn.execute(
                "SELECT target_id FROM relations WHERE source_type=? AND source_id=? "
                "AND target_type='topic' AND relation_type='belongs_to' LIMIT 1",
                (type, id),
            ).fetchone()
            data["topic_id"] = r["target_id"] if r else None

        return {"type": type, "data": _format_row(type, data, tags)}

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
