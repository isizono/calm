"""タグ管理ユーティリティ"""
import sqlite3
import threading
from typing import Optional, Union

from src.db import execute_query, get_connection, row_to_dict

VALID_NAMESPACES = {'', 'domain', 'intent', 'glossary', 'layer'}

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
      "domain:cc-memory"  -> ("domain", "cc-memory")
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
        include_notes: Trueのときnotesを返す（デフォルトFalse）
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
                }
                if include_notes:
                    entry["notes"] = r["notes"]
                tags.append(entry)

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
) -> dict:
    """既存タグの notes（教訓・運用ルール）、canonical（エイリアス先）、name（リネーム）、
    またはdescription（短い説明文）を更新する。

    Args:
        tag: タグ文字列（例: "domain:cc-memory", "hooks"）
        notes: 教訓・運用ルールのテキスト（全文置換）
        canonical: エイリアス先タグ文字列。設定するとtagがcanonicalのエイリアスになる。
                   ""（空文字）でエイリアス解除。上書き可能だが、旧canonical先に
                   付け替え済みの紐付けは戻らない。
        rename: 新しいタグ名。namespace変更も可能（例: "hooks" → "domain:hooks"）。
                新名が既存タグと衝突する場合はエラー。
        description: タグの短い説明文（最大100文字）。空文字はNULLに正規化される。

    Returns:
        成功時: {"tag": str, "notes": str, "updated": True} (notes更新時)
                {"tag": str, "canonical": str | None, "updated": True} (canonical更新時)
                {"tag": str, "renamed_to": str, "updated": True} (rename時)
                {"tag": str, "description": str | None, "updated": True} (description更新時)
        失敗時: {"error": {"code": ..., "message": ...}}
    """
    # entity_publishがtag_serviceをimportするため、循環import回避のためlocal import
    # （モジュールtopでのimportはこのモジュールがロードされる時点で循環になる）
    from src.services.relay.entity_publish import publish_entity_event_with_conn

    # バリデーション: 相互排他（notes, canonical, rename, description は1つだけ指定可能）
    specified = [p for p in (notes, canonical, rename, description) if p is not None]
    if len(specified) > 1:
        return {
            "error": {
                "code": "CONFLICTING_PARAMS",
                "message": "Only one of 'notes', 'canonical', 'rename', or 'description' can be specified. Use separate calls.",
            }
        }

    # 少なくとも1つは指定必須
    if not specified:
        return {
            "error": {
                "code": "MISSING_PARAMS",
                "message": "At least one of 'notes', 'canonical', 'rename', or 'description' must be specified.",
            }
        }

    parsed = validate_and_parse_tags([tag])
    if isinstance(parsed, dict):
        return parsed
    namespace, name = parsed[0]

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, notes, canonical_id FROM tags WHERE namespace = ? AND name = ?",
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
            conn.execute(
                "UPDATE tags SET notes = ? WHERE id = ?",
                (notes, tag_id),
            )
            publish_entity_event_with_conn(conn, entity_type="tag", entity_id=tag_id, event="updated")
            conn.commit()
            return {"tag": tag_str, "notes": notes, "updated": True}

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

        # canonical先タグを解決
        parsed_canonical = validate_and_parse_tags([canonical])
        if isinstance(parsed_canonical, dict):
            return parsed_canonical
        c_namespace, c_name = parsed_canonical[0]

        c_row = conn.execute(
            "SELECT id, canonical_id FROM tags WHERE namespace = ? AND name = ?",
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
        tag_strings: タグ文字列リスト（例: ["domain:cc-memory", "intent:design"]）
        session_id: MCPセッションID。セッション別に注入済みを管理する
        always_inject_namespaces: 常時注入するnamespaceのリスト（例: ["intent"]）。
            このnamespaceに属するタグは _injected_tags チェックをスキップし、
            毎回 notes を返す。_injected_tags には登録しない。
        mark: True（デフォルト）の場合、_injected_tags のチェックと更新を行う。
            False の場合、_injected_tags を参照も更新もしない（読み取り経路用）。

    Returns:
        notes があるタグの一覧。なければ None
        [{"tag": "domain:cc-memory", "notes": "..."}, ...]
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
        f"SELECT namespace, name, notes FROM tags WHERE ({placeholders}) AND notes IS NOT NULL",
        params
    ).fetchall()

    if not rows:
        return None

    results = []
    for row in rows:
        tag_str = f"{row['namespace']}:{row['name']}" if row["namespace"] else row["name"]
        results.append({"tag": tag_str, "notes": row["notes"]})

    return results if results else None


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
