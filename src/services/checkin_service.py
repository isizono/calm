"""checkin_tier_serviceが使う共有クエリヘルパーと、check_in応答からscopeを
読み取るcheckin_scopeを提供する。

collect_and_assemble（checkin_tier_service）とdelta_middlewareの両方が
このモジュールの関数を参照する。
"""
import sqlite3

from src.services.readable_id import strip_entity_id_inplace
from src.services.supersede_service import compute_destabilization_info_batch
from src.services.topic_service import count_decisions_per_topic, count_materials_per_topic

# 1次 decisions の展開上限
DECISIONS_FULL_LIMIT = 15


_PINNED_CHILD_SINGULAR = {
    "decisions": "decision", "logs": "log", "materials": "material",
    "topics": "topic", "activities": "activity",
}


def _pinned_item_pointer(response: dict, child_key: str, item_id: int | None) -> list[dict]:
    if child_key == "materials":
        return [{"tool": "get_material", "args": {"material_id": item_id}}]
    singular = _PINNED_CHILD_SINGULAR.get(child_key, child_key)
    return [{"tool": "get_by_ids", "args": {"items": [{"type": singular, "id": item_id}]}}]


def checkin_scope(result: dict) -> tuple[int, list[int]] | None:
    """check_in応答からscope（activity_id, topic_ids）を読み取る。

    差分通知middlewareはこの関数だけを呼び、check_in応答の形を直接読まない。
    形を変えるPRとスコープの読み方を変えるPRを必ず同じにするための唯一の窓口。
    activityのid_rawが取れない場合（error応答等）はNoneを返す。
    """
    if not isinstance(result, dict):
        return None
    anchor = result.get("anchor")
    if not isinstance(anchor, dict):
        return None
    activity = anchor.get("activity")
    if not isinstance(activity, dict):
        return None
    activity_id = activity.get("id_raw")
    if activity_id is None:
        return None
    context = result.get("context")
    topics = context.get("topics") if isinstance(context, dict) else None
    topic_ids = [t["id_raw"] for t in topics or [] if isinstance(t, dict) and "id_raw" in t]
    return activity_id, topic_ids


def _get_direct_relations(conn: sqlite3.Connection, entity_type: str, entity_id: int) -> dict[str, list[int]]:
    """relations_viewから直接関連エンティティのIDをtype別に取得する。

    Returns:
        {"topic": [id, ...], "activity": [id, ...]}
    """
    rows = conn.execute(
        "SELECT target_type, target_id FROM relations_view WHERE source_type = ? AND source_id = ?",
        (entity_type, entity_id),
    ).fetchall()

    result: dict[str, list[int]] = {"topic": [], "activity": []}
    for row in rows:
        target_type = row["target_type"]
        if target_type in result:
            result[target_type].append(row["target_id"])
    return result


def _get_topics_info(conn: sqlite3.Connection, topic_ids: list[int]) -> list[dict]:
    """複数トピックの基本情報を取得する。

    各topicにdecisions_count（retracted除外）とmaterials_count（直接リレーションのみ）を付与する。
    カウントがゼロのtopicでもフィールドは0として返す（フィールド欠落させない）。
    """
    if not topic_ids:
        return []
    placeholders = ",".join("?" * len(topic_ids))
    rows = conn.execute(
        f"SELECT id, title FROM discussion_topics WHERE id IN ({placeholders})",
        tuple(topic_ids),
    ).fetchall()
    dec_counts = count_decisions_per_topic(conn, topic_ids)
    mat_counts = count_materials_per_topic(conn, topic_ids)
    result = []
    for row in rows:
        item = {
            "id": row["id"],
            "title": row["title"],
            "decisions_count": dec_counts.get(row["id"], 0),
            "materials_count": mat_counts.get(row["id"], 0),
        }
        strip_entity_id_inplace(item)
        result.append(item)
    return result


def _get_activities_overview(conn: sqlite3.Connection, activity_ids: list[int]) -> list[dict]:
    """複数アクティビティの概要を取得する（1次展開用）。"""
    if not activity_ids:
        return []
    placeholders = ",".join("?" * len(activity_ids))
    rows = conn.execute(
        f"SELECT id, title, status FROM activities WHERE id IN ({placeholders})",
        tuple(activity_ids),
    ).fetchall()
    result = []
    for row in rows:
        item = {"id": row["id"], "title": row["title"], "status": row["status"]}
        strip_entity_id_inplace(item)
        result.append(item)
    return result


def _get_decisions_from_topics(conn: sqlite3.Connection, topic_ids: list[int]) -> list[dict]:
    """複数トピックのdecisionsを横断取得し、新しい順にフラット化する。

    上位DECISIONS_FULL_LIMIT件はid+title。retractedは除外される。
    """
    if not topic_ids:
        return []
    placeholders = ",".join("?" * len(topic_ids))
    rows = conn.execute(
        f"""
        SELECT DISTINCT d.id, d.decision, d.title
        FROM decisions d
        JOIN relations r ON r.source_type = 'decision' AND r.source_id = d.id
                        AND r.target_type = 'topic' AND r.relation_type = 'belongs_to'
                        AND r.target_id IN ({placeholders})
        WHERE d.retracted_at IS NULL
        ORDER BY d.id DESC
        LIMIT {DECISIONS_FULL_LIMIT}
        """,
        tuple(topic_ids),
    ).fetchall()

    decisions = []
    for row in rows:
        # title優先・decision本文fallback
        item = {"id": row["id"], "title": row["title"] or row["decision"]}
        strip_entity_id_inplace(item)
        decisions.append(item)
    return decisions


def _count_decisions_from_topics(conn: sqlite3.Connection, topic_ids: list[int]) -> int:
    """複数トピックのdecisionsの総件数を取得する（retracted除外、coverage分母用）。"""
    if not topic_ids:
        return 0
    placeholders = ",".join("?" * len(topic_ids))
    row = conn.execute(
        f"""
        SELECT COUNT(DISTINCT d.id)
        FROM decisions d
        JOIN relations r ON r.source_type = 'decision' AND r.source_id = d.id
                        AND r.target_type = 'topic' AND r.relation_type = 'belongs_to'
                        AND r.target_id IN ({placeholders})
        WHERE d.retracted_at IS NULL
        """,
        tuple(topic_ids),
    ).fetchone()
    return row[0] if row else 0



def _get_logs_catalog_from_topics(
    conn: sqlite3.Connection, topic_ids: list[int]
) -> tuple[dict | None, list[dict]]:
    """複数トピックのlogsを横断取得し、新しい順にフラット化する。

    最新1件はcontent付き、残りはid + titleのカタログとして返す。

    Returns:
        (latest_log, catalog): latest_logは最新1件(content付き)またはNone、
        catalogは残りのid+titleリスト
    """
    if not topic_ids:
        return None, []
    placeholders = ",".join("?" * len(topic_ids))
    params = tuple(topic_ids)

    # 最新1件: content付き
    latest_row = conn.execute(
        f"""
        SELECT DISTINCT l.id, l.title, l.content
        FROM discussion_logs l
        JOIN relations r ON r.source_type = 'log' AND r.source_id = l.id
                        AND r.target_type = 'topic' AND r.relation_type = 'belongs_to'
                        AND r.target_id IN ({placeholders})
        WHERE l.retracted_at IS NULL
        ORDER BY l.id DESC
        LIMIT 1
        """,
        params,
    ).fetchone()

    if not latest_row:
        return None, []

    display_title = latest_row["title"] or (latest_row["content"] or "")[:50]
    latest_log = {"id": latest_row["id"], "title": display_title, "content": latest_row["content"]}
    strip_entity_id_inplace(latest_log)

    # 残り: id + titleのみ（titleが空の場合はcontentの先頭50文字をfallback）
    catalog_rows = conn.execute(
        f"""
        SELECT DISTINCT l.id, l.title, l.content
        FROM discussion_logs l
        JOIN relations r ON r.source_type = 'log' AND r.source_id = l.id
                        AND r.target_type = 'topic' AND r.relation_type = 'belongs_to'
                        AND r.target_id IN ({placeholders})
        WHERE l.retracted_at IS NULL AND l.id != ?
        ORDER BY l.id DESC
        """,
        params + (latest_row["id"],),
    ).fetchall()

    catalog = []
    for row in catalog_rows:
        display_title = row["title"] or (row["content"] or "")[:50]
        item = {"id": row["id"], "title": display_title}
        strip_entity_id_inplace(item)
        catalog.append(item)
    return latest_log, catalog


def _get_pinned_targets(conn: sqlite3.Connection, activity_id: int) -> dict:
    """新pinsテーブル経由でpinされたtargetをcontent付きで取得する。

    1. activity自身のtag_idを取得する（activity_tags経由）
    2. source=tag（activity自身のtagsのみ）と source=activity のpinsをUNIONで取得する
    3. (target_type, target_id) でDISTINCT化し、created_at降順で並べる
    4. target_type別にcontent fetchする（decision/log/materialはretracted_at IS NULLでフィルタ）
    5. {decisions, logs, materials, topics, activities} に振り分けて返す（0件キーは省略）

    NOTE: target_type='tag' のpinは処理しない（tagにはcontent表現がないため）。
    pinsテーブルのCHECK制約では'tag'が許容されるが、注入対象は上記5種に限定する。

    NOTE: retracted_at カラムは decisions / discussion_logs / materials に存在する。
    discussion_topics / activities には存在しないため、
    retracted_at IS NULL フィルタは decision/log/material のクエリにのみ付ける。

    decision には未resolveな destabilizes エッジがあれば destabilization キーを付与する
    (supersede_service.compute_destabilization_info_batch の結果。対象が無ければキー
    自体を付けない)。

    Returns:
        0件キーを省略したdict。全種0件の場合は空dict。
    """
    # 1. activity自身のtag_idを取得
    tag_rows = conn.execute(
        "SELECT tag_id FROM activity_tags WHERE activity_id = ?",
        (activity_id,),
    ).fetchall()
    tag_ids = [row["tag_id"] for row in tag_rows]

    # 2. source=tag（activity自身のtagsのみ）と source=activity のpinsをUNIONで取得
    if tag_ids:
        tag_placeholders = ",".join("?" * len(tag_ids))
        raw_rows = conn.execute(
            f"""
            SELECT target_type, target_id, created_at
            FROM pins
            WHERE (source_type = 'tag' AND source_id IN ({tag_placeholders}))
               OR (source_type = 'activity' AND source_id = ?)
            """,
            tuple(tag_ids) + (activity_id,),
        ).fetchall()
    else:
        raw_rows = conn.execute(
            """
            SELECT target_type, target_id, created_at
            FROM pins
            WHERE source_type = 'activity' AND source_id = ?
            """,
            (activity_id,),
        ).fetchall()

    # 3. (target_type, target_id) でDISTINCT化し、created_at降順で並べる
    seen: set[tuple[str, int]] = set()
    distinct_rows: list[tuple[str, int]] = []
    # created_at降順にするため、ソートしてから処理（SQLiteのdatetimeはISO8601文字列なのでstr比較OK）
    sorted_rows = sorted(raw_rows, key=lambda r: r["created_at"] or "", reverse=True)
    for row in sorted_rows:
        key = (row["target_type"], row["target_id"])
        if key not in seen:
            seen.add(key)
            distinct_rows.append(key)

    if not distinct_rows:
        return {}

    # target_type別にIDをグルーピング
    by_type: dict[str, list[int]] = {}
    for target_type, target_id in distinct_rows:
        by_type.setdefault(target_type, []).append(target_id)

    result: dict[str, list[dict]] = {}

    # 4. target_type別にcontent fetch（target_type別に順序を保つためID→rowをmapして変換）
    if "decision" in by_type:
        ids = by_type["decision"]
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"""
            SELECT id, decision, reason, title
            FROM decisions
            WHERE id IN ({placeholders}) AND retracted_at IS NULL
            """,
            tuple(ids),
        ).fetchall()
        row_map = {row["id"]: row for row in rows}
        destabilization_map = compute_destabilization_info_batch(conn, [did for did in ids if did in row_map])
        decisions = []
        for did in ids:
            if did in row_map:
                row = row_map[did]
                # title優先・decision本文fallback
                item = {"id": row["id"], "title": row["title"] or row["decision"], "reason": row["reason"]}
                destab_info = destabilization_map.get(did)
                if destab_info is not None:
                    item["destabilization"] = destab_info
                strip_entity_id_inplace(item)
                decisions.append(item)
        if decisions:
            result["decisions"] = decisions

    if "log" in by_type:
        ids = by_type["log"]
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"""
            SELECT id, title, content
            FROM discussion_logs
            WHERE id IN ({placeholders}) AND retracted_at IS NULL
            """,
            tuple(ids),
        ).fetchall()
        row_map = {row["id"]: row for row in rows}
        logs = []
        for lid in ids:
            if lid in row_map:
                row = row_map[lid]
                item = {"id": row["id"], "title": row["title"], "content": row["content"]}
                strip_entity_id_inplace(item)
                logs.append(item)
        if logs:
            result["logs"] = logs

    if "material" in by_type:
        # materialsもretracted_at IS NULLでフィルタする（migration 0043 以降）
        ids = by_type["material"]
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"""
            SELECT id, title, content, source
            FROM materials
            WHERE id IN ({placeholders}) AND retracted_at IS NULL
            """,
            tuple(ids),
        ).fetchall()
        row_map = {row["id"]: row for row in rows}
        materials = []
        for mid in ids:
            if mid in row_map:
                row = row_map[mid]
                item = {"id": row["id"], "title": row["title"], "content": row["content"], "source": row["source"]}
                strip_entity_id_inplace(item)
                materials.append(item)
        if materials:
            result["materials"] = materials

    if "topic" in by_type:
        # discussion_topicsにはretracted_atカラムが存在しない
        ids = by_type["topic"]
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"""
            SELECT id, title
            FROM discussion_topics
            WHERE id IN ({placeholders})
            """,
            tuple(ids),
        ).fetchall()
        row_map = {row["id"]: row for row in rows}
        topics = []
        for tid in ids:
            if tid in row_map:
                row = row_map[tid]
                item = {"id": row["id"], "title": row["title"]}
                strip_entity_id_inplace(item)
                topics.append(item)
        if topics:
            result["topics"] = topics

    if "activity" in by_type:
        # activitiesにはretracted_atカラムが存在しない
        ids = by_type["activity"]
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"""
            SELECT id, title, status
            FROM activities
            WHERE id IN ({placeholders})
            """,
            tuple(ids),
        ).fetchall()
        row_map = {row["id"]: row for row in rows}
        activities = []
        for aid in ids:
            if aid in row_map:
                row = row_map[aid]
                item = {"id": row["id"], "title": row["title"], "status": row["status"]}
                strip_entity_id_inplace(item)
                activities.append(item)
        if activities:
            result["activities"] = activities

    return result

