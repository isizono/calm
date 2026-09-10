"""タグ管理ユーティリティ"""
import re
import sqlite3
import threading
from typing import Literal, Optional, Union

from src.config import TAG_NOTES_DECAY_DAYS
from src.db import execute_query, get_connection, row_to_dict
from src.services.decay_utils import is_decay_eligible

VALID_NAMESPACES = {'', 'domain', 'intent', 'glossary', 'layer'}

# tags.notesのDBトリガー天井（migrations/0066_add_tags_notes_ratchet_trigger.sql）と
# 同値。ここでの事前検証はUX目的（VALIDATION_ERRORでの案内）であり、実際の強制は
# DBトリガー側が担う。値を変更する場合は両方を揃えること。
_TAG_NOTES_RATCHET_CEILING = 4000

# resolve_tags 定数
MERGE_THRESHOLD = 0.15  # コサイン距離。これ未満なら統合
KNN_K = 10              # KNN検索の取得数（namespace後フィルタ前）。
                        # namespace別タグ数が偏る場合、フィルタ後の候補が0件になりうる。
                        # タグ総数が増加したら値の引き上げを検討すること。

# Entity table mapping (for UNION inheritance queries)
_ENTITY_TABLE = {
    "decision": "decisions",
    "log": "discussion_logs",
}


def parse_tag(tag_str: str) -> tuple[str, str]:
    """タグ文字列を (namespace, name) に分離する。

    Returns: (namespace, name)

    例:
      "domain:calm"       -> ("domain", "calm")
      "hooks"             -> ("", "hooks")
      "intent:design"     -> ("intent", "design")
    """
    if ":" in tag_str:
        namespace, name = tag_str.split(":", 1)
        return (namespace, name)
    return ("", tag_str)


def validate_and_parse_tags(
    tags: list[str],
    required: bool = False,
) -> Union[list[tuple[str, str]], dict]:
    """タグ配列をバリデーション・パースする。

    Args:
        tags: タグ文字列の配列
        required: Trueの場合、有効タグが0個のときエラーにする

    Returns:
        成功時: [(namespace, name), ...] の重複排除済みリスト
        失敗時: {"error": {"code": ..., "message": ...}}
    """
    if required and not tags:
        return {"error": {"code": "TAGS_REQUIRED", "message": "At least one tag is required"}}

    parsed = []
    seen = set()
    for tag_str in tags:
        tag_str = tag_str.strip()
        if not tag_str:
            continue
        namespace, name = parse_tag(tag_str)

        if namespace not in VALID_NAMESPACES:
            return {"error": {
                "code": "INVALID_TAG_NAMESPACE",
                "message": f"Invalid namespace '{namespace}' in tag '{tag_str}'. "
                           f"Allowed: {sorted(VALID_NAMESPACES)}"
            }}

        if not name.strip():
            return {"error": {
                "code": "INVALID_TAG_NAME",
                "message": f"Tag name must not be empty in '{tag_str}'"
            }}

        key = (namespace, name)
        if key not in seen:
            seen.add(key)
            parsed.append(key)

    if required and not parsed:
        return {"error": {"code": "TAGS_REQUIRED", "message": "At least one tag is required"}}

    return parsed


def resolve_tag_ids(conn: sqlite3.Connection, parsed_tags: list[tuple[str, str]]) -> list[int]:
    """既存タグのIDのみを返す（INSERT しない）。

    存在しないタグは結果に含まれない。
    エイリアスタグの場合はcanonical側のIDを返す。
    呼び出し元で len(result) < len(parsed_tags) をチェックすることで
    部分マッチを検出できる。
    """
    if not parsed_tags:
        return []
    placeholders = " OR ".join(
        "(namespace = ? AND name = ?)" for _ in parsed_tags
    )
    flat_params = [v for pair in parsed_tags for v in pair]
    rows = conn.execute(
        f"SELECT id, namespace, name, canonical_id FROM tags WHERE {placeholders}",
        flat_params,
    ).fetchall()
    id_map = {}
    for row in rows:
        effective_id = row["canonical_id"] if row["canonical_id"] is not None else row["id"]
        id_map[(row["namespace"], row["name"])] = effective_id
    return [id_map[(ns, name)] for ns, name in parsed_tags if (ns, name) in id_map]


def ensure_tag_ids(conn: sqlite3.Connection, parsed_tags: list[tuple[str, str]]) -> list[int]:
    """タグをINSERT OR IGNOREし、idのリストを返す。

    connを受け取り、呼び出し元のトランザクション内で動作する。
    エイリアスタグの場合はcanonical側のIDを返す。
    新規にINSERTされたタグ（未使用だったnamespace:name）はrelay publish
    （entity:tag, event:created）の対象にする。
    """
    if not parsed_tags:
        return []
    placeholders = " OR ".join(
        "(namespace = ? AND name = ?)" for _ in parsed_tags
    )
    flat_params = [v for pair in parsed_tags for v in pair]

    # INSERT前に既存タグを控えておき、INSERT OR IGNORE後との差分で新規作成分を判定する
    # （executemanyのINSERT OR IGNOREは行単位の成否を返さないため）。
    existing_before = conn.execute(
        f"SELECT namespace, name FROM tags WHERE {placeholders}", flat_params
    ).fetchall()
    existing_keys = {(row["namespace"], row["name"]) for row in existing_before}

    conn.executemany(
        "INSERT OR IGNORE INTO tags (namespace, name) VALUES (?, ?)",
        parsed_tags,
    )
    rows = conn.execute(
        f"SELECT id, namespace, name, canonical_id FROM tags WHERE {placeholders}",
        flat_params,
    ).fetchall()
    id_map = {}
    newly_created_ids = []
    for row in rows:
        key = (row["namespace"], row["name"])
        effective_id = row["canonical_id"] if row["canonical_id"] is not None else row["id"]
        id_map[key] = effective_id
        if key not in existing_keys:
            newly_created_ids.append(row["id"])

    if newly_created_ids:
        # entity_publishがtag_serviceを import するため、循環import回避のためlocal import
        from src.services.relay.entity_publish import publish_entity_event_with_conn
        for tag_id in newly_created_ids:
            publish_entity_event_with_conn(conn, entity_type="tag", entity_id=tag_id, event="created")

    return [id_map[(ns, name)] for ns, name in parsed_tags]


def resolve_tags(
    tags: list[str],
    force_new_tags: bool = False,
) -> tuple[list[int], list[dict]] | dict:
    """タグを解決する（あいまいマッチ付き）。

    処理フロー（タグ1つあたり）:
    1. パース: validate_and_parse_tags() を使用。namespace/name を lower().strip() で正規化
    2. 完全一致チェック: SELECT id FROM tags WHERE namespace=? AND name=?
    3. force_new_tags=True → 完全一致がなければ新規タグINSERT + embedding生成（KNN検索スキップ）
    4. KNN検索: tag_vec に対して embedding MATCH → namespace 後フィルタ
    5. 閾値判定: distance < MERGE_THRESHOLD → 既存タグIDに統合。なし → 新規作成 + embedding生成

    Args:
        tags: タグ文字列のリスト
        force_new_tags: Trueの場合、KNN検索をスキップして新規作成する
                        （ただし完全一致がある場合は既存IDを使用する）

    Returns:
        成功時: (tag_ids, merged_tags)
            - tag_ids: 解決済みタグIDリスト
            - merged_tags: [{"input": "hooks", "merged_to": "hook", "distance": 0.05}, ...]
        失敗時: {"error": {"code": ..., "message": ...}}
    """
    from src.services.embedding_service import (
        generate_and_store_tag_embedding,
        search_similar_tags,
    )

    # 1. パース + 正規化 + バリデーション
    # validate_and_parse_tags は正規化前にnamespace検証するため、
    # resolve_tags では自前で parse_tag → lower/strip正規化 → バリデーション を行う
    normalized = []
    seen = set()
    for tag_str in tags:
        tag_str = tag_str.strip()
        if not tag_str:
            continue
        ns, name = parse_tag(tag_str)
        # 正規化: lower().strip()
        ns = ns.lower().strip()
        name = name.lower().strip()

        if ns not in VALID_NAMESPACES:
            return {"error": {
                "code": "INVALID_TAG_NAMESPACE",
                "message": f"Invalid namespace '{ns}' in tag '{tag_str}'. "
                           f"Allowed: {sorted(VALID_NAMESPACES)}"
            }}
        if not name:
            return {"error": {
                "code": "INVALID_TAG_NAME",
                "message": f"Tag name must not be empty in '{tag_str}'"
            }}

        key = (ns, name)
        if key not in seen:
            seen.add(key)
            normalized.append(key)

    if not normalized:
        return ([], [])

    conn = get_connection()
    try:
        resolved_ids: list[int] = []
        merged_tags: list[dict] = []
        seen_ids: set[int] = set()

        for ns, name in normalized:
            # 入力タグの表示用文字列
            input_tag_str = f"{ns}:{name}" if ns else name

            # 2. 完全一致チェック（canonical解決付き）
            row = conn.execute(
                "SELECT id, canonical_id FROM tags WHERE namespace = ? AND name = ?",
                (ns, name),
            ).fetchone()

            if row:
                tag_id = row["canonical_id"] if row["canonical_id"] is not None else row["id"]
                if tag_id not in seen_ids:
                    resolved_ids.append(tag_id)
                    seen_ids.add(tag_id)
                continue

            # 3. force_new_tags=True → KNN検索スキップ、新規作成
            # NOTE: ループ内で中間commit()している。generate_and_store_tag_embedding()が
            # 別コネクションを開くため、未コミットの行は参照できない制約による。
            # このため複数タグ処理の途中でエラーが発生した場合、前半のINSERTは
            # rollbackされない（アトミック性を犠牲にしている）。
            if force_new_tags:
                conn.execute(
                    "INSERT OR IGNORE INTO tags (namespace, name) VALUES (?, ?)",
                    (ns, name),
                )
                new_row = conn.execute(
                    "SELECT id FROM tags WHERE namespace = ? AND name = ?",
                    (ns, name),
                ).fetchone()
                tag_id = new_row["id"]
                if tag_id not in seen_ids:
                    resolved_ids.append(tag_id)
                    seen_ids.add(tag_id)
                conn.commit()
                # embedding生成（失敗してもエラーにしない）
                generate_and_store_tag_embedding(tag_id, name)
                continue

            # 4. KNN検索
            similar = search_similar_tags(name, k=KNN_K)

            # namespace後フィルタ + 閾値判定
            best_match = None
            for candidate_id, distance in similar:
                if distance >= MERGE_THRESHOLD:
                    continue
                # candidateのnamespaceを確認
                candidate_row = conn.execute(
                    "SELECT namespace, name FROM tags WHERE id = ?",
                    (candidate_id,),
                ).fetchone()
                if candidate_row and candidate_row["namespace"] == ns:
                    best_match = (candidate_id, candidate_row["name"], distance)
                    break  # distance順なので最初のマッチがベスト

            if best_match:
                # 5a. 統合
                match_id, match_name, distance = best_match
                if match_id not in seen_ids:
                    resolved_ids.append(match_id)
                    seen_ids.add(match_id)
                merged_to_str = f"{ns}:{match_name}" if ns else match_name
                merged_tags.append({
                    "input": input_tag_str,
                    "merged_to": merged_to_str,
                    "distance": round(distance, 4),
                })
            else:
                # 5b. 新規作成 + embedding生成
                conn.execute(
                    "INSERT OR IGNORE INTO tags (namespace, name) VALUES (?, ?)",
                    (ns, name),
                )
                new_row = conn.execute(
                    "SELECT id FROM tags WHERE namespace = ? AND name = ?",
                    (ns, name),
                ).fetchone()
                tag_id = new_row["id"]
                if tag_id not in seen_ids:
                    resolved_ids.append(tag_id)
                    seen_ids.add(tag_id)
                conn.commit()
                # embedding生成（失敗してもエラーにしない）
                generate_and_store_tag_embedding(tag_id, name)

        conn.commit()
        return (resolved_ids, merged_tags)

    except Exception as e:
        conn.rollback()
        return {
            "error": {
                "code": "DATABASE_ERROR",
                "message": str(e),
            }
        }
    finally:
        conn.close()


def link_tags(
    conn: sqlite3.Connection,
    junction_table: str,
    entity_column: str,
    entity_id: int,
    tag_ids: list[int],
) -> None:
    """中間テーブルにタグを紐付ける。"""
    if not tag_ids:
        return
    conn.executemany(
        f"INSERT OR IGNORE INTO {junction_table} ({entity_column}, tag_id) VALUES (?, ?)",
        [(entity_id, tid) for tid in tag_ids],
    )


def format_tags(tag_rows) -> list[str]:
    """DB行をタグ文字列のリストに変換する。

    namespace付き: "namespace:name"、素タグ: "name"
    アルファベット順ソート。
    """
    tags = []
    for row in tag_rows:
        ns = row["namespace"]
        name = row["name"]
        if ns:
            tags.append(f"{ns}:{name}")
        else:
            tags.append(name)
    return sorted(tags)


def get_entity_tags(
    conn: sqlite3.Connection,
    junction_table: str,
    entity_column: str,
    entity_id: int,
) -> list[str]:
    """エンティティに紐づくタグ文字列リストを取得する。"""
    rows = conn.execute(
        f"""
        SELECT t.namespace, t.name
        FROM tags t
        JOIN {junction_table} jt ON t.id = jt.tag_id
        WHERE jt.{entity_column} = ?
        """,
        (entity_id,),
    ).fetchall()
    return format_tags(rows)


def get_entity_tags_batch(
    conn: sqlite3.Connection,
    junction_table: str,
    entity_column: str,
    entity_ids: list[int],
) -> dict[int, list[str]]:
    """複数エンティティに紐づくタグ文字列リストを一括取得する。

    Returns: {entity_id: ["tag1", "tag2", ...], ...}
    """
    if not entity_ids:
        return {}
    placeholders = ",".join("?" * len(entity_ids))
    rows = conn.execute(
        f"""
        SELECT jt.{entity_column} AS entity_id, t.namespace, t.name
        FROM tags t
        JOIN {junction_table} jt ON t.id = jt.tag_id
        WHERE jt.{entity_column} IN ({placeholders})
        """,
        tuple(entity_ids),
    ).fetchall()

    groups: dict[int, list] = {}
    for row in rows:
        eid = row["entity_id"]
        if eid not in groups:
            groups[eid] = []
        groups[eid].append(row)

    return {eid: format_tags(tag_rows) for eid, tag_rows in groups.items()}


def get_effective_tags_batch(
    conn: sqlite3.Connection,
    entity_type: str,
    parent_topic_id: int,
) -> dict[int, list[str]]:
    """topic_id配下の全entity(decision/log)の有効タグを一括取得する。

    Returns: {entity_id: ["tag1", "tag2", ...], ...}
    """
    entity_table = _ENTITY_TABLE[entity_type]
    junction_table = f"{entity_type}_tags"
    id_column = f"{entity_type}_id"

    # decision/log の親 topic は relations.belongs_to 経由で解決
    rows = conn.execute(
        f"""
        SELECT e.id AS entity_id, t.namespace, t.name
        FROM {entity_table} e
        JOIN relations r ON r.source_type = ? AND r.source_id = e.id
                        AND r.target_type = 'topic' AND r.relation_type = 'belongs_to'
        JOIN topic_tags tt ON tt.topic_id = r.target_id
        JOIN tags t ON t.id = tt.tag_id
        WHERE r.target_id = ?

        UNION

        SELECT et.{id_column} AS entity_id, t.namespace, t.name
        FROM {junction_table} et
        JOIN tags t ON t.id = et.tag_id
        WHERE et.{id_column} IN (
            SELECT r2.source_id FROM relations r2
            WHERE r2.source_type = ? AND r2.target_type = 'topic'
              AND r2.relation_type = 'belongs_to' AND r2.target_id = ?
        )
        """,
        (entity_type, parent_topic_id, entity_type, parent_topic_id),
    ).fetchall()

    # entity_idごとにグルーピング
    groups: dict[int, list] = {}
    for row in rows:
        eid = row["entity_id"]
        if eid not in groups:
            groups[eid] = []
        groups[eid].append(row)

    # format_tagsで文字列配列に変換
    return {eid: format_tags(tag_rows) for eid, tag_rows in groups.items()}


def get_effective_tags_batch_by_ids(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_ids: list[int],
) -> dict[int, list[str]]:
    """複数entity(decision/log)の有効タグ（topic_tags UNION entity_tags）を一括取得する。

    get_effective_tagsのバッチ版。entity_idのリストを受け取り、
    各entityの有効タグをまとめて返す。

    Returns: {entity_id: ["tag1", "tag2", ...], ...}
    """
    if not entity_ids:
        return {}
    entity_table = _ENTITY_TABLE[entity_type]
    junction_table = f"{entity_type}_tags"
    id_column = f"{entity_type}_id"

    placeholders = ",".join("?" * len(entity_ids))
    # 継承元 topic は relations.belongs_to 経由で解決
    rows = conn.execute(
        f"""
        SELECT e.id AS entity_id, t.namespace, t.name
        FROM {entity_table} e
        JOIN relations r ON r.source_type = ? AND r.source_id = e.id
                        AND r.target_type = 'topic' AND r.relation_type = 'belongs_to'
        JOIN topic_tags tt ON tt.topic_id = r.target_id
        JOIN tags t ON t.id = tt.tag_id
        WHERE e.id IN ({placeholders})

        UNION

        SELECT et.{id_column} AS entity_id, t.namespace, t.name
        FROM {junction_table} et
        JOIN tags t ON t.id = et.tag_id
        WHERE et.{id_column} IN ({placeholders})
        """,
        (entity_type, *entity_ids, *entity_ids),
    ).fetchall()

    # entity_idごとにグルーピング
    groups: dict[int, list] = {}
    for row in rows:
        eid = row["entity_id"]
        if eid not in groups:
            groups[eid] = []
        groups[eid].append(row)

    return {eid: format_tags(tag_rows) for eid, tag_rows in groups.items()}


def get_effective_tags(conn: sqlite3.Connection, entity_type: str, entity_id: int) -> list[str]:
    """entity(decision/log)の有効タグ（topic_tags UNION entity_tags）を取得する。"""
    entity_table = _ENTITY_TABLE[entity_type]
    junction_table = f"{entity_type}_tags"
    id_column = f"{entity_type}_id"

    # 継承元 topic は relations.belongs_to 経由で解決
    rows = conn.execute(
        f"""
        SELECT DISTINCT t.namespace, t.name
        FROM tags t
        WHERE t.id IN (
            SELECT tt.tag_id
            FROM topic_tags tt
            JOIN relations r ON r.target_type = 'topic' AND r.target_id = tt.topic_id
                            AND r.source_type = ? AND r.relation_type = 'belongs_to'
            WHERE r.source_id = ?

            UNION

            SELECT et.tag_id
            FROM {junction_table} et
            WHERE et.{id_column} = ?
        )
        """,
        (entity_type, entity_id, entity_id),
    ).fetchall()
    return format_tags(rows)


def get_archived_tags_for_strings(conn: sqlite3.Connection, tag_strings: list[str]) -> list[dict]:
    """タグ文字列集合のうちarchivedなものだけを抽出する。

    呼び出し元がエンティティ横断で集めたタグ文字列（例: 応答内の全アイテムの
    tagsを1つにまとめたもの）を渡すと、1クエリでそのうちarchivedなものだけを返す。

    Args:
        conn: DB接続
        tag_strings: タグ文字列のリスト（例: ["domain:calm", "domain:orch-legacy"]）

    Returns:
        [{"tag": "domain:orch-legacy", "archived_reason": "..."}, ...]
        （tag昇順ソート。非archivedタグ・存在しないタグは結果に含まれない）
    """
    if not tag_strings:
        return []
    parsed = list({parse_tag(t) for t in tag_strings})
    placeholders = " OR ".join(["(namespace = ? AND name = ?)"] * len(parsed))
    params = [v for pair in parsed for v in pair]
    rows = conn.execute(
        f"SELECT namespace, name, archived_reason FROM tags WHERE ({placeholders}) AND archived_at IS NOT NULL",
        params,
    ).fetchall()
    results = [
        {
            "tag": f"{r['namespace']}:{r['name']}" if r["namespace"] else r["name"],
            "archived_reason": r["archived_reason"],
        }
        for r in rows
    ]
    results.sort(key=lambda x: x["tag"])
    return results


# search_tags RRFパラメータ
_SEARCH_TAGS_RRF_K = 60
_SEARCH_TAGS_W_LIKE = 1.0
_SEARCH_TAGS_W_VEC = 1.0


def search_tags(
    query: str,
    namespace: Optional[str] = None,
    include_notes: bool = False,
    limit: int = 20,
) -> dict:
    """タグをキーワード検索する（LIKE + ベクトル KNN のハイブリッド）。

    チャネル1: タグ名LIKE部分一致（usage_count降順）
    チャネル2: tag_vec KNN検索（embedding_service.search_similar_tags）
    統合: シンプルRRF（2チャネル）

    Args:
        query: 検索キーワード（タグ名部分一致 + ベクトル検索）
        namespace: namespaceフィルタ（"domain", "intent", ""、未指定で全タグ）
        include_notes: Trueのときnotesを返す（デフォルトFalse）。notesを持つ結果の
            last_injected_atも更新する（tag notes decay述語の明示参照による復帰経路。
            get_habits(habit_id=...)がhabits側で持つ参照スタンプ更新と同じ役割）
        limit: 取得件数上限（デフォルト20）

    Returns:
        検索結果（tags配列、各要素にscore付き）
    """
    from src.services.embedding_service import search_similar_tags

    if not query or not query.strip():
        return {"error": {"code": "INVALID_QUERY", "message": "query must not be empty"}}

    query = query.strip()
    limit = max(1, min(limit, 100))

    try:
        conn = get_connection()
        try:
            # --- チャネル1: LIKE部分一致 ---
            like_pattern = f"%{query}%"
            if namespace is not None:
                like_rows = conn.execute(
                    """
                    SELECT t.id, t.namespace, t.name, t.notes, t.description, t.canonical_id,
                      t.archived_at, t.archived_reason,
                      ct.namespace AS canonical_namespace, ct.name AS canonical_name,
                      (SELECT COUNT(*) FROM topic_tags WHERE tag_id = t.id) +
                      (SELECT COUNT(*) FROM activity_tags WHERE tag_id = t.id) +
                      (SELECT COUNT(*) FROM decision_tags WHERE tag_id = t.id) +
                      (SELECT COUNT(*) FROM log_tags WHERE tag_id = t.id) +
                      (SELECT COUNT(*) FROM material_tags WHERE tag_id = t.id) AS usage_count
                    FROM tags t
                    LEFT JOIN tags AS ct ON t.canonical_id = ct.id
                    WHERE t.name LIKE ? AND t.namespace = ?
                    ORDER BY usage_count DESC, t.name ASC
                    LIMIT ?
                    """,
                    (like_pattern, namespace, limit * 5),
                ).fetchall()
            else:
                like_rows = conn.execute(
                    """
                    SELECT t.id, t.namespace, t.name, t.notes, t.description, t.canonical_id,
                      t.archived_at, t.archived_reason,
                      ct.namespace AS canonical_namespace, ct.name AS canonical_name,
                      (SELECT COUNT(*) FROM topic_tags WHERE tag_id = t.id) +
                      (SELECT COUNT(*) FROM activity_tags WHERE tag_id = t.id) +
                      (SELECT COUNT(*) FROM decision_tags WHERE tag_id = t.id) +
                      (SELECT COUNT(*) FROM log_tags WHERE tag_id = t.id) +
                      (SELECT COUNT(*) FROM material_tags WHERE tag_id = t.id) AS usage_count
                    FROM tags t
                    LEFT JOIN tags AS ct ON t.canonical_id = ct.id
                    WHERE t.name LIKE ?
                    ORDER BY usage_count DESC, t.name ASC
                    LIMIT ?
                    """,
                    (like_pattern, limit * 5),
                ).fetchall()

            # LIKE結果をdict化（id -> row_dict + rank）
            like_tag_data: dict[int, dict] = {}
            like_ranks: dict[int, int] = {}
            for rank, row in enumerate(like_rows, start=1):
                r = row_to_dict(row)
                tag_id = r["id"]
                like_tag_data[tag_id] = r
                like_ranks[tag_id] = rank

            # --- チャネル2: ベクトルKNN検索 ---
            vec_results = search_similar_tags(query, k=limit * 3)
            # namespace後フィルタ
            vec_ranks: dict[int, int] = {}
            rank_counter = 1
            for tag_id, _distance in vec_results:
                if namespace is not None:
                    # namespaceフィルタが必要な場合、DBで確認
                    if tag_id in like_tag_data:
                        # LIKE結果にある = namespaceフィルタ済み
                        vec_ranks[tag_id] = rank_counter
                        rank_counter += 1
                    else:
                        # LIKE結果にない = DBで確認
                        ns_row = conn.execute(
                            "SELECT namespace FROM tags WHERE id = ?",
                            (tag_id,),
                        ).fetchone()
                        if ns_row and ns_row["namespace"] == namespace:
                            vec_ranks[tag_id] = rank_counter
                            rank_counter += 1
                else:
                    vec_ranks[tag_id] = rank_counter
                    rank_counter += 1

            # --- RRF統合 ---
            all_tag_ids = set(like_ranks.keys()) | set(vec_ranks.keys())
            scored: list[tuple[int, float]] = []
            for tag_id in all_tag_ids:
                score = 0.0
                if tag_id in like_ranks:
                    score += _SEARCH_TAGS_W_LIKE / (_SEARCH_TAGS_RRF_K + like_ranks[tag_id])
                if tag_id in vec_ranks:
                    score += _SEARCH_TAGS_W_VEC / (_SEARCH_TAGS_RRF_K + vec_ranks[tag_id])
                scored.append((tag_id, score))

            # スコア降順ソート → limit適用
            scored.sort(key=lambda x: x[1], reverse=True)
            scored = scored[:limit]

            # --- 結果構築 ---
            # LIKE結果にないタグのデータをDBから取得
            missing_ids = [tid for tid, _ in scored if tid not in like_tag_data]
            if missing_ids:
                placeholders = ",".join("?" * len(missing_ids))
                missing_rows = conn.execute(
                    f"""
                    SELECT t.id, t.namespace, t.name, t.notes, t.description, t.canonical_id,
                      t.archived_at, t.archived_reason,
                      ct.namespace AS canonical_namespace, ct.name AS canonical_name,
                      (SELECT COUNT(*) FROM topic_tags WHERE tag_id = t.id) +
                      (SELECT COUNT(*) FROM activity_tags WHERE tag_id = t.id) +
                      (SELECT COUNT(*) FROM decision_tags WHERE tag_id = t.id) +
                      (SELECT COUNT(*) FROM log_tags WHERE tag_id = t.id) +
                      (SELECT COUNT(*) FROM material_tags WHERE tag_id = t.id) AS usage_count
                    FROM tags t
                    LEFT JOIN tags AS ct ON t.canonical_id = ct.id
                    WHERE t.id IN ({placeholders})
                    """,
                    tuple(missing_ids),
                ).fetchall()
                for row in missing_rows:
                    r = row_to_dict(row)
                    like_tag_data[r["id"]] = r

            tags = []
            for tag_id, score in scored:
                r = like_tag_data.get(tag_id)
                if r is None:
                    continue
                ns = r["namespace"]
                name = r["name"]
                tag_str = f"{ns}:{name}" if ns else name

                # canonical文字列の構築
                canonical = None
                if r["canonical_id"] is not None:
                    c_ns = r["canonical_namespace"]
                    c_name = r["canonical_name"]
                    canonical = f"{c_ns}:{c_name}" if c_ns else c_name

                entry: dict = {
                    "tag": tag_str,
                    "id": r["id"],
                    "namespace": ns,
                    "name": name,
                    "usage_count": r["usage_count"],
                    "score": round(score, 4),
                    "canonical": canonical,
                    "description": r["description"],
                    "archived": r["archived_at"] is not None,
                    "archived_reason": r["archived_reason"],
                }
                if include_notes:
                    entry["notes"] = r["notes"]
                tags.append(entry)

            if include_notes:
                # notesを実際に返したタグのlast_injected_atを更新する。tag notesには
                # get_habits(habit_id=...)に相当する明示参照の復帰経路が他に無いため、
                # これが無いと一度decay判定されたタグは自動注入から永久にpointer化された
                # ままになる（is_decay_eligibleはlast_injected_atが更新されない限り
                # 恒久的にTrueを返し続けるため）。
                referenced_ids = [
                    tag_id for tag_id, _ in scored
                    if like_tag_data.get(tag_id, {}).get("notes")
                ]
                if referenced_ids:
                    ph = ",".join("?" * len(referenced_ids))
                    conn.execute(
                        f"UPDATE tags SET last_injected_at = CURRENT_TIMESTAMP WHERE id IN ({ph})",
                        referenced_ids,
                    )
                    conn.commit()

            return {"tags": tags}

        finally:
            conn.close()

    except Exception as e:
        return {
            "error": {
                "code": "DATABASE_ERROR",
                "message": str(e),
            }
        }


JUNCTION_TABLES = [
    ("topic_tags", "topic_id"),
    ("activity_tags", "activity_id"),
    ("decision_tags", "decision_id"),
    ("log_tags", "log_id"),
    ("material_tags", "material_id"),
]


def update_tag(
    tag: str,
    notes: str | None = None,
    canonical: str | None = None,
    rename: str | None = None,
    description: str | None = None,
    archived: bool | None = None,
    archived_reason: str | None = None,
) -> dict:
    """既存タグの notes（教訓・運用ルール）、canonical（エイリアス先）、name（リネーム）、
    description（短い説明文）、またはarchived（退役状態）を更新する。

    notes記述規約: notesに全文で置いてよいのは行動を変える取扱注意のみ。仕様・状態・
    手順・歴史記録は正典（コード/docs/decision/activity等）に置き、notesには1行の
    ポインタだけを残す（種別と正典の対応表は demote_tag_notes のdocstring参照）。
    文字数上限を超えている場合は、notesを直接書き換える前に demote_tag_notes で
    該当セクションを資材へ退避してから縮めること。

    Args:
        tag: タグ文字列（例: "domain:calm", "hooks"）
        notes: 教訓・運用ルールのテキスト（全文置換）
        canonical: エイリアス先タグ文字列。設定するとtagがcanonicalのエイリアスになる。
                   ""（空文字）でエイリアス解除。上書き可能だが、旧canonical先に
                   付け替え済みの紐付けは戻らない。
        rename: 新しいタグ名。namespace変更も可能（例: "hooks" → "domain:hooks"）。
                新名が既存タグと衝突する場合はエラー。
        description: タグの短い説明文（最大100文字）。空文字はNULLに正規化される。
        archived: Trueで退役、Falseで解除。既に同状態のときは変更せず updated: False
                  を返す（冪等）。解除時は archived_reason も自動的にNULLへ戻る。
        archived_reason: 退役理由の短いテキスト（最大100文字）。archived=True と
                         同時指定のときのみ有効。既に archived 状態のタグへ
                         archived=True を再適用した場合、このパラメータで理由を
                         書き換えることはできない（一度 archived=False で解除して
                         から再設定する）。

    Returns:
        成功時: {"tag": str, "notes": str, "updated": True} (notes更新時)
                {"tag": str, "canonical": str | None, "updated": True} (canonical更新時)
                {"tag": str, "renamed_to": str, "updated": True} (rename時)
                {"tag": str, "description": str | None, "updated": True} (description更新時)
                {"tag": str, "archived": bool, "archived_at": str | None,
                 "archived_reason": str | None, "updated": bool} (archived更新時)
        失敗時: {"error": {"code": ..., "message": ...}}
    """
    # entity_publishがtag_serviceをimportするため、循環import回避のためlocal import
    # （モジュールtopでのimportはこのモジュールがロードされる時点で循環になる）
    from src.services.relay.entity_publish import publish_entity_event_with_conn

    # archived_reason は archived=True と同時指定のときのみ有効（archived=False・未指定への
    # 単独付随は不可）
    if archived_reason is not None and archived is not True:
        return {
            "error": {
                "code": "ORPHAN_ARCHIVED_REASON",
                "message": "archived_reason requires archived=True in the same call.",
            }
        }

    # バリデーション: 相互排他（notes, canonical, rename, description, archived は1つだけ指定可能）
    specified = [p for p in (notes, canonical, rename, description, archived) if p is not None]
    if len(specified) > 1:
        return {
            "error": {
                "code": "CONFLICTING_PARAMS",
                "message": "Only one of 'notes', 'canonical', 'rename', 'description', or 'archived' can be specified. Use separate calls.",
            }
        }

    # 少なくとも1つは指定必須
    if not specified:
        return {
            "error": {
                "code": "MISSING_PARAMS",
                "message": "At least one of 'notes', 'canonical', 'rename', 'description', or 'archived' must be specified.",
            }
        }

    parsed = validate_and_parse_tags([tag])
    if isinstance(parsed, dict):
        return parsed
    namespace, name = parsed[0]

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, notes, canonical_id, archived_at, archived_reason FROM tags WHERE namespace = ? AND name = ?",
            (namespace, name),
        ).fetchone()

        if not row:
            tag_display = f"{namespace}:{name}" if namespace else name
            return {
                "error": {
                    "code": "NOT_FOUND",
                    "message": f"Tag '{tag_display}' not found",
                }
            }

        tag_id = row["id"]
        tag_str = f"{namespace}:{name}" if namespace else name

        # --- rename ---
        if rename is not None:
            parsed_new = validate_and_parse_tags([rename])
            if isinstance(parsed_new, dict):
                return parsed_new
            if not parsed_new:
                return {
                    "error": {
                        "code": "INVALID_TAG_NAME",
                        "message": "rename cannot be empty.",
                    }
                }
            new_namespace, new_name = parsed_new[0]

            # 同一名へのリネームは無意味
            if new_namespace == namespace and new_name == name:
                return {
                    "error": {
                        "code": "SAME_NAME",
                        "message": f"New name is the same as current name: '{tag_str}'",
                    }
                }

            # 新名が既存タグと衝突するかチェック
            existing = conn.execute(
                "SELECT id FROM tags WHERE namespace = ? AND name = ?",
                (new_namespace, new_name),
            ).fetchone()
            if existing:
                new_display = f"{new_namespace}:{new_name}" if new_namespace else new_name
                return {
                    "error": {
                        "code": "ALREADY_EXISTS",
                        "message": f"Tag '{new_display}' already exists.",
                    }
                }

            conn.execute(
                "UPDATE tags SET namespace = ?, name = ? WHERE id = ?",
                (new_namespace, new_name, tag_id),
            )
            publish_entity_event_with_conn(conn, entity_type="tag", entity_id=tag_id, event="updated")
            conn.commit()
            new_tag_str = f"{new_namespace}:{new_name}" if new_namespace else new_name
            return {"tag": tag_str, "renamed_to": new_tag_str, "updated": True}

        # --- description 更新 ---
        if description is not None:
            # 空文字→NULL正規化
            if description == "":
                description = None
            conn.execute(
                "UPDATE tags SET description = ? WHERE id = ?",
                (description, tag_id),
            )
            publish_entity_event_with_conn(conn, entity_type="tag", entity_id=tag_id, event="updated")
            conn.commit()
            return {"tag": tag_str, "description": description, "updated": True}

        # --- notes 更新 ---
        if notes is not None:
            existing_notes = row["notes"] or ""
            # ラチェット則: 縮む更新は天井超過中でも常に許可するため、超過チェックは
            # 「新しい長さが天井を超え、かつ既存より増加している」場合のみに限る
            # （DBトリガー0066と同じ条件）
            if (
                len(notes) > _TAG_NOTES_RATCHET_CEILING
                and len(notes) > len(existing_notes)
            ):
                return {
                    "error": {
                        "code": "VALIDATION_ERROR",
                        "message": (
                            f"notes must be at most {_TAG_NOTES_RATCHET_CEILING} characters "
                            f"when increasing in length (current: {len(existing_notes)}, "
                            f"attempted: {len(notes)})."
                        ),
                    }
                }
            conn.execute(
                "UPDATE tags SET notes = ? WHERE id = ?",
                (notes, tag_id),
            )
            publish_entity_event_with_conn(conn, entity_type="tag", entity_id=tag_id, event="updated")
            conn.commit()
            return {"tag": tag_str, "notes": notes, "updated": True}

        # --- archived 更新 ---
        if archived is not None:
            if archived:
                if row["archived_at"] is not None:
                    # 冪等: 既にarchived。archived_reasonの後追い書き換えはしない
                    # （解除→再設定運用。5. Edge cases参照）
                    return {
                        "tag": tag_str,
                        "archived": True,
                        "archived_at": row["archived_at"],
                        "archived_reason": row["archived_reason"],
                        "updated": False,
                    }
                # 自分がcanonical先として他タグから参照されている場合、参照元が
                # 気づかないままarchivedになるのを避けるため拒否する
                dependent = conn.execute(
                    "SELECT id FROM tags WHERE canonical_id = ? LIMIT 1",
                    (tag_id,),
                ).fetchone()
                if dependent:
                    return {
                        "error": {
                            "code": "ARCHIVED_CANONICAL_INVALID",
                            "message": f"Tag '{tag_str}' is the canonical target of other aliases. "
                                       "Remove those aliases first.",
                        }
                    }
                conn.execute(
                    "UPDATE tags SET archived_at = CURRENT_TIMESTAMP, archived_reason = ? WHERE id = ?",
                    (archived_reason, tag_id),
                )
                publish_entity_event_with_conn(conn, entity_type="tag", entity_id=tag_id, event="updated")
                conn.commit()
                new_row = conn.execute(
                    "SELECT archived_at FROM tags WHERE id = ?", (tag_id,)
                ).fetchone()
                return {
                    "tag": tag_str,
                    "archived": True,
                    "archived_at": new_row["archived_at"],
                    "archived_reason": archived_reason,
                    "updated": True,
                }
            else:
                if row["archived_at"] is None:
                    return {"tag": tag_str, "archived": False, "updated": False}
                conn.execute(
                    "UPDATE tags SET archived_at = NULL, archived_reason = NULL WHERE id = ?",
                    (tag_id,),
                )
                publish_entity_event_with_conn(conn, entity_type="tag", entity_id=tag_id, event="updated")
                conn.commit()
                return {"tag": tag_str, "archived": False, "updated": True}

        # --- canonical 更新 ---
        # canonical="" → エイリアス解除
        if canonical == "":
            conn.execute(
                "UPDATE tags SET canonical_id = NULL WHERE id = ?",
                (tag_id,),
            )
            publish_entity_event_with_conn(conn, entity_type="tag", entity_id=tag_id, event="updated")
            conn.commit()
            return {"tag": tag_str, "canonical": None, "updated": True}

        # エイリアスタグにnotes有りの場合 → エラー（空文字もnotesなしとして扱う）
        if row["notes"]:
            return {
                "error": {
                    "code": "HAS_NOTES",
                    "message": f"Tag '{tag_str}' has notes. Remove notes before setting as alias.",
                }
            }

        # archived中のタグは新規にエイリアスにできない
        if row["archived_at"] is not None:
            return {
                "error": {
                    "code": "ARCHIVED_CANONICAL_INVALID",
                    "message": f"Tag '{tag_str}' is archived. Cannot set canonical while archived.",
                }
            }

        # canonical先タグを解決
        parsed_canonical = validate_and_parse_tags([canonical])
        if isinstance(parsed_canonical, dict):
            return parsed_canonical
        c_namespace, c_name = parsed_canonical[0]

        c_row = conn.execute(
            "SELECT id, canonical_id, archived_at FROM tags WHERE namespace = ? AND name = ?",
            (c_namespace, c_name),
        ).fetchone()

        if not c_row:
            c_display = f"{c_namespace}:{c_name}" if c_namespace else c_name
            return {
                "error": {
                    "code": "NOT_FOUND",
                    "message": f"Canonical tag '{c_display}' not found",
                }
            }

        if c_row["archived_at"] is not None:
            c_display = f"{c_namespace}:{c_name}" if c_namespace else c_name
            return {
                "error": {
                    "code": "ARCHIVED_CANONICAL_INVALID",
                    "message": f"Canonical target '{c_display}' is archived. Cannot alias to an archived tag.",
                }
            }

        canonical_id = c_row["id"]

        # 自分自身へのエイリアスは無意味なので禁止
        if canonical_id == tag_id:
            return {
                "error": {
                    "code": "CHAIN_NOT_ALLOWED",
                    "message": "Cannot set a tag as alias of itself.",
                }
            }

        # canonical先が既にエイリアス → 連鎖禁止
        if c_row["canonical_id"] is not None:
            return {
                "error": {
                    "code": "CHAIN_NOT_ALLOWED",
                    "message": "Canonical target is already an alias. Chains are not allowed.",
                }
            }

        # 自分が他タグのcanonical先になっている場合 → 連鎖禁止
        dependent = conn.execute(
            "SELECT id FROM tags WHERE canonical_id = ? LIMIT 1",
            (tag_id,),
        ).fetchone()
        if dependent:
            return {
                "error": {
                    "code": "CHAIN_NOT_ALLOWED",
                    "message": f"Tag '{tag_str}' is the canonical target of other aliases. "
                               "Remove those aliases first.",
                }
            }

        # canonical_id を設定
        conn.execute(
            "UPDATE tags SET canonical_id = ? WHERE id = ?",
            (canonical_id, tag_id),
        )

        # 影響を受けるエンティティを収集（embedding再生成用）
        _entity_col_to_type = {
            "topic_id": "topic",
            "activity_id": "activity",
            "decision_id": "decision",
            "log_id": "log",
            "material_id": "material",
        }
        affected_entities: list[tuple[str, int]] = []
        for table, entity_col in JUNCTION_TABLES:
            rows = conn.execute(
                f"SELECT {entity_col} FROM {table} WHERE tag_id = ?",
                (tag_id,),
            ).fetchall()
            etype = _entity_col_to_type.get(entity_col)
            if etype:
                for r in rows:
                    affected_entities.append((etype, r[entity_col]))

        # 紐付け付け替え: 中間テーブル4つ
        for table, entity_col in JUNCTION_TABLES:
            # 1. 重複する行を削除（canonical側IDが既に存在する場合）
            conn.execute(
                f"""
                DELETE FROM {table} WHERE {entity_col} IN (
                    SELECT a.{entity_col} FROM {table} a
                    INNER JOIN {table} b ON a.{entity_col} = b.{entity_col}
                    WHERE a.tag_id = ? AND b.tag_id = ?
                ) AND tag_id = ?
                """,
                (tag_id, canonical_id, tag_id),
            )
            # 2. 残りを付け替え
            conn.execute(
                f"UPDATE {table} SET tag_id = ? WHERE tag_id = ?",
                (canonical_id, tag_id),
            )

        publish_entity_event_with_conn(conn, entity_type="tag", entity_id=tag_id, event="updated")
        conn.commit()

        # タグ変更に伴うembedding再生成（コミット後に同期的に実行）
        from src.services.embedding_service import regenerate_embedding
        for etype, eid in affected_entities:
            regenerate_embedding(etype, eid)

        c_tag_str = f"{c_namespace}:{c_name}" if c_namespace else c_name
        return {"tag": tag_str, "canonical": c_tag_str, "updated": True}

    except Exception as e:
        conn.rollback()
        return {
            "error": {
                "code": "DATABASE_ERROR",
                "message": str(e),
            }
        }
    finally:
        conn.close()


def get_available_intents() -> list[dict]:
    """intent:タグ一覧をdescription付きで返す（canonical除外、アルファベット順）"""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT name, description FROM tags
            WHERE namespace = 'intent' AND canonical_id IS NULL
            ORDER BY name ASC
            """,
        ).fetchall()
        return [
            {"tag": f"intent:{r['name']}", "description": r["description"]}
            for r in rows
        ]
    finally:
        conn.close()


# ========================================
# 遭遇時注入（Tag Notes Injection）
# ========================================

# セッション別の注入済みタグ追跡（ctx.session_idキー）
# セッション終了はこのモジュールに通知されないため、上限超過時に挿入順の
# 最古セッションから追い出す（放置するとセッション数ぶん永久に成長する）。
# 追い出された長寿セッションは同じタグの notes を再度受け取るだけで実害はない。
# 追い出しは check-then-act（len 判定 → del）でGILのアトミック性に頼れないため、
# ツール並行実行下の同時到達を _injected_tags_lock で直列化する。
_injected_tags: dict[str, set[str]] = {}
_injected_tags_lock = threading.Lock()
_INJECTED_TAGS_MAX_SESSIONS = 256


def collect_tag_notes_for_injection(
    conn: sqlite3.Connection,
    tag_strings: list[str],
    session_id: str | None = None,
    always_inject_namespaces: list[str] | None = None,
    mark: bool = True,
) -> list[dict] | None:
    """未注入タグの notes を収集し、注入済みとしてマークする。

    Args:
        conn: DB接続
        tag_strings: タグ文字列リスト（例: ["domain:calm", "intent:design"]）
        session_id: MCPセッションID。セッション別に注入済みを管理する
        always_inject_namespaces: 常時注入するnamespaceのリスト（例: ["intent"]）。
            このnamespaceに属するタグは _injected_tags チェックをスキップし、
            毎回 notes を返す。_injected_tags には登録しない。
        mark: True（デフォルト）の場合、_injected_tags のチェックと更新を行う。
            False の場合、_injected_tags を参照も更新もしない（読み取り経路用）。
            last_injected_at の更新（decay述語のトラッキング）も mark と連動する。

    Returns:
        notes があるタグの一覧。なければ None
        [{"tag": "domain:calm", "notes": "..."}, ...]
        notes が空文字列（全セクションを退避し尽くした後の tags.notes 等）のタグは
        対象外にする（NULL 判定だけでは拾えないため）。
        タグ作成からTAG_NOTES_DECAY_DAYSを超え、かつ全文配信実績（last_injected_at）も
        同日数以内に更新されていないタグは、notesの全文の代わりに1行ポインタ文言へ縮退する
        （レンダー時decay。search_tags等の返却対象からは除外しない）。
        always_inject_namespaces対象のタグは常時全文注入という既存契約が優先されるため、
        decay判定の対象から除外される（ポインタ文言に縮退しない）。
    """
    session_key = session_id or "__default__"
    always_ns = set(always_inject_namespaces) if always_inject_namespaces else set()

    # always_inject対象とそれ以外を分離（パース結果も保持）
    always_parsed = []
    normal_tags = []
    normal_parsed = []
    for t in tag_strings:
        ns, name = parse_tag(t)
        if ns in always_ns:
            always_parsed.append((ns, name))
        else:
            normal_tags.append(t)
            normal_parsed.append((ns, name))

    if mark:
        with _injected_tags_lock:
            if session_key not in _injected_tags:
                while len(_injected_tags) >= _INJECTED_TAGS_MAX_SESSIONS:
                    del _injected_tags[next(iter(_injected_tags))]
            session_set = _injected_tags.setdefault(session_key, set())
            new_normal = [
                (t, p) for t, p in zip(normal_tags, normal_parsed)
                if t not in session_set
            ]
            session_set.update(t for t, _ in new_normal)
    else:
        # mark=False: 全タグをクエリ対象にし、_injected_tags は更新しない
        new_normal = list(zip(normal_tags, normal_parsed))

    # クエリ対象: new_normal + always（always_tagsは毎回クエリ）
    parsed = [p for _, p in new_normal] + always_parsed
    if not parsed:
        return None
    placeholders = " OR ".join(["(namespace = ? AND name = ?)"] * len(parsed))
    params = [v for pair in parsed for v in pair]
    rows = conn.execute(
        f"SELECT id, namespace, name, notes, created_at, last_injected_at FROM tags "
        f"WHERE ({placeholders}) AND notes IS NOT NULL AND LENGTH(notes) > 0 AND archived_at IS NULL",
        params
    ).fetchall()

    if not rows:
        return None

    results = []
    fresh_ids = []
    for row in rows:
        tag_str = f"{row['namespace']}:{row['name']}" if row["namespace"] else row["name"]
        # always_inject_namespaces対象は常時全文注入契約が優先されるため、decay判定自体を
        # スキップする。
        if row["namespace"] not in always_ns and is_decay_eligible(
            row["created_at"], row["last_injected_at"], TAG_NOTES_DECAY_DAYS
        ):
            results.append({"tag": tag_str, "notes": _decay_pointer_text(tag_str)})
            continue
        results.append({"tag": tag_str, "notes": row["notes"]})
        fresh_ids.append(row["id"])

    if mark and fresh_ids:
        # 中間commit: resolve_tags（同ファイル内、force_new_tags/新規作成分岐）と同じ理由
        # （呼び出し元の共有connに対する後続処理への影響回避）で、ここで先にcommitする。
        # mark=Falseの読み取り専用経路ではlast_injected_atも更新しない
        # （mark引数が副作用全般を制御する既存契約に合わせる）。
        placeholders_ids = ",".join("?" * len(fresh_ids))
        conn.execute(
            f"UPDATE tags SET last_injected_at = CURRENT_TIMESTAMP WHERE id IN ({placeholders_ids})",
            fresh_ids,
        )
        conn.commit()

    return results if results else None


def _decay_pointer_text(tag_str: str) -> str:
    """decay対象タグのnotesを全文の代わりに縮退させる1行ポインタ文言を返す。"""
    return (
        f"{tag_str} のnotesは長期間参照されていないため全文表示を省略した。"
        "内容が必要な場合は search_tags(include_notes=True) で確認する。"
    )


def _set_tag_notes_by_id_with_conn(conn: sqlite3.Connection, tag_id: int, notes: str) -> None:
    """tag_id指定でnotesを全文置換する。commitは呼び出し元が行う。"""
    conn.execute("UPDATE tags SET notes = ? WHERE id = ?", (notes, tag_id))


def _append_tag_notes_with_conn(conn, tag_str: str, content: str) -> int:
    """タグのnotesにcontentを追記しtag_idを返す。タグ不在時はValueError。"""
    if not content or not content.strip():
        raise ValueError("content must not be empty")
    parsed = validate_and_parse_tags([tag_str])
    if isinstance(parsed, dict):
        raise ValueError(parsed["error"]["message"])
    namespace, name = parsed[0]
    row = conn.execute(
        "SELECT id, notes FROM tags WHERE namespace = ? AND name = ?",
        (namespace, name),
    ).fetchone()
    if not row:
        raise ValueError(f"Tag '{tag_str}' not found")
    tag_id = row["id"]
    existing = row["notes"]
    new_notes = f"{existing}\n\n{content}" if existing else content
    conn.execute("UPDATE tags SET notes = ? WHERE id = ?", (new_notes, tag_id))
    return tag_id


# ========================================
# demote_tag_notes
# ========================================

# 末尾trailerの1行判定: 空行、または「#」で始まり空白を含まない行
# (例: #audited-2026-09-04, #recompose-delta-skipped-until:2026-09-10)。
_TRAILER_LINE_RE = re.compile(r'^#\S+$')

_SECTION_HEADING_PREFIX = "## "

# 退避索引を集約する予約セクションの見出し。notes末尾（trailerの直前）に1つだけ置く。
_DEMOTE_INDEX_HEADING = "## 退避済み（全文は資材へ）"

DEMOTE_ARCHIVE_TAG = "tag-notes-archive"

_ARCHIVE_TITLE_PREFIX = "tag notes退避: "


def _normalize_section_key(text: str) -> str:
    """見出しテキストの照合キーを作る。前後空白と先頭 "## " の有無を無視して比較する。"""
    text = text.strip()
    if text.startswith(_SECTION_HEADING_PREFIX):
        text = text[len(_SECTION_HEADING_PREFIX):]
    return text.strip()


def _split_tag_notes_layers(notes: str) -> dict:
    """tag notesを preamble / sections / trailer の3層に分解する（純粋関数）。

    - trailer: 末尾から連続する「空行」または「ハッシュタグのみの行
      (例: #audited-2026-09-04, #recompose-delta-skipped-until:2026-09-10)」。
      該当しない行に当たった時点で走査を止める（本文途中の同種の行は対象にしない）。
    - preamble: 最初の "## " 見出し行より前の本文（見出しが1つも無ければ全体）。
    - sections: "## " 見出し行を境界に分割したブロック列。各要素は見出し行を含む
      逐語テキスト（次のブロック直前まで、改行込み）。

    Returns:
        {"preamble": str, "sections": [{"heading": str, "block": str}, ...], "trailer": str}
        preamble + 各sectionのblockを元の順序で連結 + trailer は、notesと1バイトも
        違わない文字列に戻る（区切り文字は各blockの中に含まれており、この関数は
        文字列の分割のみを行い、内容の変更・正規化は一切しない）。
    """
    lines = notes.splitlines(keepends=True)

    trailer_start = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].rstrip("\n")
        if stripped.strip() == "" or _TRAILER_LINE_RE.match(stripped.strip()):
            trailer_start = i
        else:
            break
    trailer = "".join(lines[trailer_start:])
    body_lines = lines[:trailer_start]

    heading_indices = [i for i, l in enumerate(body_lines) if l.startswith(_SECTION_HEADING_PREFIX)]
    if not heading_indices:
        preamble = "".join(body_lines)
        sections: list[dict] = []
    else:
        preamble = "".join(body_lines[:heading_indices[0]])
        sections = []
        for idx, start in enumerate(heading_indices):
            end = heading_indices[idx + 1] if idx + 1 < len(heading_indices) else len(body_lines)
            block = "".join(body_lines[start:end])
            heading_text = body_lines[start].rstrip("\n")
            sections.append({"heading": heading_text, "block": block})

    return {"preamble": preamble, "sections": sections, "trailer": trailer}


def _build_pointer_line(heading: str, material_id: int) -> str:
    """索引セクションの1行分のポインタ文言を作る。"""
    return f"- {_normalize_section_key(heading)} → get_material(material_id={material_id})"


def _build_archive_title(tag_str: str, max_len: int) -> str:
    """退避先資材のtitleを組み立てる。max_len超過時はタグ名部分を切り詰める。"""
    prefix = _ARCHIVE_TITLE_PREFIX
    available = max_len - len(prefix)
    if available <= 0:
        return prefix[:max_len]
    if len(tag_str) <= available:
        return f"{prefix}{tag_str}"
    return f"{prefix}{tag_str[:available]}"


def demote_tag_notes(
    tag: str,
    sections: list[str],
    mode: Literal["pointer", "drop"] = "pointer",
    archive_material_id: Optional[int] = None,
    archive_tags: Optional[list[str]] = None,
    reason: Optional[str] = None,
) -> dict:
    """tag notesの指定セクションを資材へ逐語退避し、notesを縮小する。

    ## tag notes 記述規約(正典)

    tag notesに全文で置いてよいのは「そのタグに触れる者が最初に知るべき、行動を
    変える取扱注意」だけである。種別ごとの扱いは以下の通り。

    | 種別 | notesに置く量 | 置き場所(正典) |
    |---|---|---|
    | 教訓・落とし穴(そのタグ固有・現役) | 全文 | notes自身 |
    | 仕様スナップショット | 1行ポインタ | コード / docs / decision |
    | 状態・進行ジャーナル(「YYYY-MM-DD時点で〜中」等) | 0行。書くこと自体を禁止 | activity / topic / log |
    | 運用手順 | 1行ポインタ | docs配下、または資材 |
    | 歴史記録 | 1行ポインタ、または0行 | 資材 |
    | 環境知識 | 全文(rules / auto-memoryと重複させない) | notes自身 |

    既に書かれてしまった分は本ツールで資材へ逐語退避してから縮める。縮小と退避は
    必ず同時に行うこと(引き先が無い状態で縮めるとその場で情報が消える)。本ツールは
    退避書き込みとnotes縮小を1トランザクションにまとめており、notesの書き込みが
    文字数上限(4000字)で拒否された場合は退避書き込み側も含めて全体がロールバック
    され、退避先資材は作られずに残る。

    この規約は tag notes に触れる全てのツール呼び出しへ配る正典であり、update_tag
    等の他ツールのdocstringには要約と本docstringへの参照のみを置く。

    ## 引数(詳細は docs/spec/mcp-tools.md 参照)

    tag: 対象タグ。
    sections: 退避する見出しテキストの配列("## "の有無は問わず正規化して照合)。
        存在しない見出しはSECTION_NOT_FOUND、重複見出しはAMBIGUOUS_SECTIONで拒否。
        前文(最初の"## "行より前)は退避対象にできない。
    mode: "pointer"(既定)=退避後にnotes末尾へ1行ポインタを残す(索引は1セクションに
        集約・重複排除)。"drop"=ポインタも残さない。
    archive_material_id: 既存の退避先資材へ追記(省略時は新規作成)。retract済み・
        存在しないIDはVALIDATION_ERROR。
    archive_tags: 退避先資材のタグ(省略時 [tag, "tag-notes-archive"])。
    reason: 退避理由の1行(退避先資材の冒頭に入る)。

    ## 返り値

    成功時: {tag, material_id, material_title, material_created, demoted_sections,
    pointers_added, notes_length: {before, after, ceiling, over_budget},
    citations_converted}。

    notes_length.over_budget が True の間は、縮む更新以外のあらゆる追記が拒否され
    続ける(ラチェット則)。整理の終了条件はdemote回数でなくover_budgetがFalseに
    なったかで判定すること。

    citations_converted は退避先資材の本文中で生ID参照が {{cite:...}} へ変換された
    件数。notesに残した側はバイト同一を保証するが、退避先はこの変換分だけ表記が
    変わりうる。

    失敗時: {"error": {"code": str, "message": str}}
    (NOT_FOUND / SECTION_NOT_FOUND / AMBIGUOUS_SECTION / VALIDATION_ERROR /
     CONSTRAINT_VIOLATION / DATABASE_ERROR)
    """
    # NOTE: 上記docstringは main.py の demote_tag_notes ツールdocstringと
    # 同一に保つこと(二層とも同じ内容が必要)。
    # entity_publishがtag_serviceをimportするため、循環import回避のためlocal import
    from src.services.material_service import (
        _add_material_with_conn,
        _append_material_content_with_conn,
    )
    from src.services.embedding_service import build_embedding_text, generate_and_store_embedding
    from src.services.relay.entity_publish import publish_entity_event_with_conn
    from src.services.title_validation import TITLE_MAX_LEN

    if not sections:
        return {"error": {"code": "VALIDATION_ERROR", "message": "sections must not be empty"}}

    if mode not in ("pointer", "drop"):
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": f"mode must be 'pointer' or 'drop', got {mode!r}",
            }
        }

    parsed = validate_and_parse_tags([tag])
    if isinstance(parsed, dict):
        return parsed
    namespace, name = parsed[0]
    tag_str = f"{namespace}:{name}" if namespace else name

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, notes FROM tags WHERE namespace = ? AND name = ?",
            (namespace, name),
        ).fetchone()
        if not row:
            return {"error": {"code": "NOT_FOUND", "message": f"Tag '{tag_str}' not found"}}
        tag_id = row["id"]
        existing_notes = row["notes"] or ""

        layers = _split_tag_notes_layers(existing_notes)
        preamble = layers["preamble"]
        note_sections = layers["sections"]
        trailer = layers["trailer"]

        # sections引数の見出し照合(存在確認・曖昧判定)
        heading_key_map: dict[str, list[int]] = {}
        for idx, sec in enumerate(note_sections):
            key = _normalize_section_key(sec["heading"])
            heading_key_map.setdefault(key, []).append(idx)

        matched_indices: list[int] = []
        for raw in sections:
            key = _normalize_section_key(raw)
            occurrences = heading_key_map.get(key, [])
            if not occurrences:
                available = [s["heading"] for s in note_sections]
                return {
                    "error": {
                        "code": "SECTION_NOT_FOUND",
                        "message": (
                            f"Section '{raw}' not found in tag '{tag_str}'. "
                            f"Available sections: {available}"
                        ),
                    }
                }
            if len(occurrences) > 1:
                return {
                    "error": {
                        "code": "AMBIGUOUS_SECTION",
                        "message": (
                            f"Section heading '{raw}' matches {len(occurrences)} sections "
                            f"in tag '{tag_str}'. Cannot determine which to demote."
                        ),
                    }
                }
            matched_indices.append(occurrences[0])

        # 重複指定(同じ見出しを複数回指定)は1回にまとめ、元のnotes順で処理する
        matched_set = set(matched_indices)
        unique_matched_indices = sorted(matched_set)

        if archive_material_id is not None:
            m_row = conn.execute(
                "SELECT retracted_at FROM materials WHERE id = ?", (archive_material_id,)
            ).fetchone()
            if not m_row or m_row["retracted_at"] is not None:
                return {
                    "error": {
                        "code": "VALIDATION_ERROR",
                        "message": f"archive_material_id {archive_material_id} not found or retracted",
                    }
                }

        demoted_headings = [note_sections[i]["heading"] for i in unique_matched_indices]
        demoted_blocks = [note_sections[i]["block"] for i in unique_matched_indices]
        remaining_sections = [s for i, s in enumerate(note_sections) if i not in matched_set]

        is_new = archive_material_id is None
        effective_archive_tags = archive_tags if archive_tags else [tag_str, DEMOTE_ARCHIVE_TAG]
        payload = "".join(demoted_blocks)

        if is_new:
            summary = f"{tag_str} のtag notesから退避した記録。"
            if reason:
                stripped_reason = reason.strip()
                summary += stripped_reason
                if stripped_reason and not stripped_reason.endswith(("。", ".", "!", "?", "！", "？")):
                    summary += "。"
            archive_content = f"{summary}\n\n{payload}"
            archive_title = _build_archive_title(tag_str, TITLE_MAX_LEN)
            write_result = _add_material_with_conn(
                conn,
                title=archive_title,
                content=archive_content,
                tags=effective_archive_tags,
                source="demote_tag_notesによるtag notes退避",
            )
        else:
            archive_content = f"{reason.strip()}\n\n{payload}" if reason else payload
            write_result = _append_material_content_with_conn(conn, archive_material_id, archive_content)

        if "error" in write_result:
            conn.rollback()
            return write_result

        material_id = write_result["material_id"]

        # 索引セクションの統合(mode="pointer"のときのみ)。既存の索引セクションが
        # remaining_sections内にあれば取り出して統合し、末尾(trailerの直前)へ集約する。
        pointers_added = 0
        index_block = ""
        if mode == "pointer":
            pointer_lines_new = [_build_pointer_line(h, material_id) for h in demoted_headings]
            kept_sections = []
            existing_index_lines: list[str] = []
            for sec in remaining_sections:
                if _normalize_section_key(sec["heading"]) == _normalize_section_key(_DEMOTE_INDEX_HEADING):
                    body_lines = sec["block"].splitlines()
                    existing_index_lines = [l for l in body_lines[1:] if l.strip()]
                else:
                    kept_sections.append(sec)
            all_lines = list(existing_index_lines)
            for line in pointer_lines_new:
                if line not in all_lines:
                    all_lines.append(line)
                    pointers_added += 1
            remaining_sections = kept_sections
            index_block = _DEMOTE_INDEX_HEADING + "\n" + "\n".join(all_lines) + "\n"

        body_so_far = preamble + "".join(s["block"] for s in remaining_sections)
        if index_block:
            if not body_so_far or body_so_far.endswith("\n\n"):
                separator = ""
            elif body_so_far.endswith("\n"):
                separator = "\n"
            else:
                separator = "\n\n"
            new_notes = body_so_far + separator + index_block + trailer
        else:
            new_notes = body_so_far + trailer

        if new_notes.strip() == "":
            new_notes = ""

        # 増加中の書き込みが天井(4000字)を超える場合、DBトリガーがsqlite3.IntegrityErrorを
        # 送出する(縮む更新は天井超過中でも常に通るため、ここに来るのは索引セクション追加分の
        # オーバーヘッドが除去分を上回り正味で増加したケースに限られる)。update_tagの
        # 事前チェックと同じ理由で、生SQLiteメッセージを露出させずVALIDATION_ERRORへ変換する。
        # before/afterの実測値を返すのは、整理ループを回すエージェントが次に何セクションを
        # 追加で退避すべきか機械的に判断できるようにするため。
        try:
            _set_tag_notes_by_id_with_conn(conn, tag_id, new_notes)
        except sqlite3.IntegrityError:
            conn.rollback()
            return {
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": (
                        f"demote後のnotesが{_TAG_NOTES_RATCHET_CEILING}字を超えたまま "
                        f"増加するため書き込みを拒否した (before: {len(existing_notes)}字, "
                        f"after: {len(new_notes)}字)。退避するセクションを追加するか、"
                        f"mode=\"drop\"で索引ポインタを省略することを検討すること。"
                    ),
                }
            }

        publish_entity_event_with_conn(conn, entity_type="tag", entity_id=tag_id, event="updated")

        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        return {"error": {"code": "CONSTRAINT_VIOLATION", "message": str(e)}}
    except Exception as e:
        conn.rollback()
        return {"error": {"code": "DATABASE_ERROR", "message": str(e)}}
    finally:
        conn.close()

    tag_text = " ".join(write_result["tag_strings"]) if write_result["tag_strings"] else ""
    generate_and_store_embedding(
        "material", material_id,
        build_embedding_text(write_result["title"], write_result["content"], tag_text),
    )

    return {
        "tag": tag_str,
        "material_id": material_id,
        "material_title": write_result["title"],
        "material_created": is_new,
        "demoted_sections": [_normalize_section_key(h) for h in demoted_headings],
        "pointers_added": pointers_added,
        "notes_length": {
            "before": len(existing_notes),
            "after": len(new_notes),
            "ceiling": _TAG_NOTES_RATCHET_CEILING,
            "over_budget": len(new_notes) > _TAG_NOTES_RATCHET_CEILING,
        },
        "citations_converted": write_result.get("citations_converted", 0),
    }
