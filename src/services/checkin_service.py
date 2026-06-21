"""check-inサービス"""
import logging
import sqlite3

from src.db import get_connection, row_to_dict
from src.services import activity_service
from src.services.readable_id import apply_readable_id_inplace
from src.services.material_service import get_materials_by_relation_with_conn
from src.services.relation_service import _get_map_with_conn
from src.services.tag_service import (
    collect_tag_notes_for_injection,
    get_entity_tags,
)
from src.services.topic_service import count_decisions_per_topic, count_materials_per_topic

logger = logging.getLogger(__name__)

# 1次 decisions の展開上限
DECISIONS_FULL_LIMIT = 15

# recomposeナッジhintのしきい値。
# 実運用での発火頻度を見ながら調整する前提の暫定値。
# メンテナッジ: recomposed material最終更新以降に増えたdecisionがこの件数以上で発火。
_RECOMPOSE_HINT_DELTA_THRESHOLD = 30
# ブートストラップナッジ: material未整備のtagでdecision総数がこの件数以上で発火。
_RECOMPOSE_HINT_BOOTSTRAP_THRESHOLD = 15


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
        apply_readable_id_inplace(item, "topic")
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
        apply_readable_id_inplace(item, "activity")
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
        SELECT id, decision, title
        FROM decisions
        WHERE topic_id IN ({placeholders}) AND retracted_at IS NULL
        ORDER BY id DESC
        LIMIT {DECISIONS_FULL_LIMIT}
        """,
        tuple(topic_ids),
    ).fetchall()

    decisions = []
    for row in rows:
        # title優先・decision本文fallback
        item = {"id": row["id"], "title": row["title"] or row["decision"]}
        apply_readable_id_inplace(item, "decision")
        decisions.append(item)
    return decisions


def _count_decisions_from_topics(conn: sqlite3.Connection, topic_ids: list[int]) -> int:
    """複数トピックのdecisionsの総件数を取得する（retracted除外、coverage分母用）。"""
    if not topic_ids:
        return 0
    placeholders = ",".join("?" * len(topic_ids))
    row = conn.execute(
        f"SELECT COUNT(*) FROM decisions WHERE topic_id IN ({placeholders}) AND retracted_at IS NULL",
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
        SELECT id, title, content
        FROM discussion_logs
        WHERE topic_id IN ({placeholders}) AND retracted_at IS NULL
        ORDER BY id DESC
        LIMIT 1
        """,
        params,
    ).fetchone()

    if not latest_row:
        return None, []

    display_title = latest_row["title"] or (latest_row["content"] or "")[:50]
    latest_log = {"id": latest_row["id"], "title": display_title, "content": latest_row["content"]}
    apply_readable_id_inplace(latest_log, "log")

    # 残り: id + titleのみ（titleが空の場合はcontentの先頭50文字をfallback）
    catalog_rows = conn.execute(
        f"""
        SELECT id, title, content
        FROM discussion_logs
        WHERE topic_id IN ({placeholders}) AND retracted_at IS NULL AND id != ?
        ORDER BY id DESC
        """,
        params + (latest_row["id"],),
    ).fetchall()

    catalog = []
    for row in catalog_rows:
        display_title = row["title"] or (row["content"] or "")[:50]
        item = {"id": row["id"], "title": display_title}
        apply_readable_id_inplace(item, "log")
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
        decisions = []
        for did in ids:
            if did in row_map:
                row = row_map[did]
                # title優先・decision本文fallback
                item = {"id": row["id"], "title": row["title"] or row["decision"], "reason": row["reason"]}
                apply_readable_id_inplace(item, "decision")
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
                apply_readable_id_inplace(item, "log")
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
                apply_readable_id_inplace(item, "material")
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
                apply_readable_id_inplace(item, "topic")
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
                apply_readable_id_inplace(item, "activity")
                activities.append(item)
        if activities:
            result["activities"] = activities

    return result


def _count_tag_scope_decisions(
    conn: sqlite3.Connection, tag_id: int, after: str | None = None
) -> int:
    """対象tagのスコープに属するdecision件数を数える（retracted除外）。

    tagスコープは以下2経路のUNIONで定義する:
      1. decision_tags直付け: decision_tags.tag_id = tag_id
      2. topic_tags継承: decisions.topic_id → topic_tags.tag_id = tag_id
    両経路をORで結合し、いずれかに該当すれば対象とする。

    Args:
        conn: SQLiteコネクション
        tag_id: 対象tagのID
        after: 指定時、created_at > after のdecisionのみ数える（増分カウント用）。
            Noneのときは時刻フィルタなし（総数カウント）。

    Returns:
        条件を満たすdecisionの件数。
    """
    sql = """
        SELECT COUNT(*) FROM decisions d
        WHERE d.retracted_at IS NULL
          AND (
            EXISTS (
                SELECT 1 FROM decision_tags dt
                WHERE dt.decision_id = d.id AND dt.tag_id = ?
            )
            OR EXISTS (
                SELECT 1 FROM topic_tags tt
                WHERE tt.topic_id = d.topic_id AND tt.tag_id = ?
            )
          )
    """
    params: list = [tag_id, tag_id]
    if after is not None:
        # 厳密大なり（>=ではなく>）。after=recomposed materialの最終更新時刻なので、
        # その時点までのdecisionは統合済み。>=にすると最終秒に読んだdecisionを
        # 翌check-inで再カウントしてしまうため、>で「更新後に新規追加された分」だけを数える。
        sql += " AND d.created_at > ?"
        params.append(after)
    row = conn.execute(sql, tuple(params)).fetchone()
    return row[0] if row else 0


def _get_recompose_hints(conn: sqlite3.Connection, activity_id: int) -> list[str]:
    """check-in対象activityのtagについてrecomposeナッジhintを生成する。

    対象tagはactivityに紐づくtagのうち素タグ（namespace=''）のみ。
    domain:/intent: などnamespace付きタグは対象外とする。

    各tagについて、pinされたmaterial（pins: source_type='tag' AND source_id=tag_id
    AND target_type='material'）の有無で2種類のナッジを判定する:

      - pinされたmaterialがある場合（メンテナッジ）:
        基準時刻 T = pinされたmaterialの COALESCE(updated_at, created_at) のmax。
        tagスコープ内でcreated_at > T のdecision（retracted除外）が
        _RECOMPOSE_HINT_DELTA_THRESHOLD 件以上なら発火する。

      - pinされたmaterialが無い場合（ブートストラップナッジ）:
        tagスコープ内のdecision総数（retracted除外）が
        _RECOMPOSE_HINT_BOOTSTRAP_THRESHOLD 件以上なら発火する。

    Returns:
        hint文字列のリスト。発火するtagが無ければ空リスト。
    """
    # activityに紐づく素タグ（namespace=''）のみ取得する
    tag_rows = conn.execute(
        """
        SELECT t.id, t.name
        FROM activity_tags at
        JOIN tags t ON t.id = at.tag_id
        WHERE at.activity_id = ? AND t.namespace = ''
        """,
        (activity_id,),
    ).fetchall()

    hints: list[str] = []
    for tag_row in tag_rows:
        tag_id = tag_row["id"]
        tag_name = tag_row["name"]

        # 該当tagにpinされたmaterialの最終更新時刻T（複数あればmax）を取得する
        t_row = conn.execute(
            """
            SELECT MAX(COALESCE(m.updated_at, m.created_at)) AS t
            FROM pins p
            JOIN materials m ON m.id = p.target_id
            WHERE p.source_type = 'tag' AND p.source_id = ?
              AND p.target_type = 'material'
            """,
            (tag_id,),
        ).fetchone()
        base_time = t_row["t"] if t_row else None

        if base_time is not None:
            # メンテナッジ（増分）: 最終更新以降のdecision増分で判定
            delta_count = _count_tag_scope_decisions(conn, tag_id, after=base_time)
            if delta_count >= _RECOMPOSE_HINT_DELTA_THRESHOLD:
                hints.append(
                    f"tag「{tag_name}」はrecomposed materialの最終更新以降にdecisionが"
                    f"{delta_count}件増えています。recompose-context skillでのメンテを"
                    f"ユーザーに提案してください。"
                )
        else:
            # ブートストラップナッジ（初回）: decision総数で判定
            total_count = _count_tag_scope_decisions(conn, tag_id)
            if total_count >= _RECOMPOSE_HINT_BOOTSTRAP_THRESHOLD:
                hints.append(
                    f"tag「{tag_name}」にdecisionが{total_count}件蓄積していますが、"
                    f"統合material（recomposed material）がありません。"
                    f"recompose-context skillでの初回整理をユーザーに提案してください。"
                )

    return hints


def _extract_intent_tag(tags: list[str]) -> str:
    """タグリストからintent:プレフィックスのタグを抽出する。なければ「(未設定)」。"""
    for tag in tags:
        if tag.startswith("intent:"):
            return tag.split(":", 1)[1]
    return "(未設定)"


def _build_summary(
    activity: dict,
    tags: list[str],
) -> str:
    """summary文字列を生成する。

    フォーマット:
        check-in: タイトル
          intent: xxx
    """
    intent = _extract_intent_tag(tags)

    line1 = f"check-in: {activity['title']}"
    line2 = f"  intent: {intent}"

    return f"{line1}\n{line2}"


def check_in(activity_id: int, session_id: str | None = None) -> dict:
    """アクティビティにcheck-inする。

    関連情報（tag_notes, materials, decisions, logs catalog, catalog）を集約取得し、
    status自動更新とsummary生成を行う。

    リレーション対応:
    - 1次（直接関連）: 関連topicのdecisions（フラット15件、新しい順）+ 関連activityの概要
    - 2次: get_mapによるカタログ（id, type, title, tags）

    statusがin_progress以外（pending, completed含む）の場合はin_progressに自動更新する。
    completedのアクティビティも再オープンされる（追加作業が発生したケースに対応）。

    tag_notesの注入ルール:
    - 通常タグ: セッション内初回遭遇時のみ注入される（_injected_tags管理）。
      同一セッションで同じタグを持つアクティビティに2回check-inすると、
      2回目のtag_notesは空になる。
    - always_inject_namespaces対象タグ（例: intent:）: 毎回注入される。
      _injected_tagsによるフィルタをスキップし、check-inのたびにnotesを返す。

    pinsテーブル経由のpinned targets注入:
    - activity自身のtag（source=tag）と activity自身（source=activity）のpinsを取得する
    - (target_type, target_id) でDISTINCT化してレスポンスのpinnedキーに注入する
    - retracted済みのdecision/logはpins経由でも注入されない（retracted_at IS NULL除外）

    Args:
        activity_id: アクティビティID

    Returns:
        check-in結果（coverage, activity, related_topics, related_activities, pinned,
        tag_notes, materials, recent_decisions, logs, catalog, summary）
    """
    if session_id is None:
        try:
            from fastmcp.server.dependencies import get_context
            ctx = get_context()
            session_id = ctx.session_id
        except (RuntimeError, ImportError):
            pass
    conn = get_connection()
    try:
        # 1. activity取得
        row = conn.execute(
            "SELECT * FROM activities WHERE id = ?",
            (activity_id,),
        ).fetchone()
        if row is None:
            return {
                "error": {
                    "code": "NOT_FOUND",
                    "message": f"Activity with id {activity_id} not found",
                }
            }

        activity = row_to_dict(row)
        tags = get_entity_tags(conn, "activity_tags", "activity_id", activity_id)

        # 2. tag_notes収集
        tag_notes = collect_tag_notes_for_injection(conn, tags, session_id=session_id, always_inject_namespaces=["intent"]) or []

        # 3. 直接関連エンティティ取得（1次）
        direct = _get_direct_relations(conn, "activity", activity_id)

        # 3a. 関連トピック情報
        related_topics = _get_topics_info(conn, direct["topic"])

        # 3b. 関連アクティビティ概要
        related_activities = _get_activities_overview(conn, direct["activity"])

        # 3c. depends_on情報取得
        dep_rows = conn.execute(
            """SELECT a.id, a.title, a.status
               FROM activity_dependencies ad
               JOIN activities a ON a.id = ad.dependency_id
               WHERE ad.dependent_id = ?""",
            (activity_id,),
        ).fetchall()
        dependencies = []
        for r in dep_rows:
            dep_item = {"id": r["id"], "title": r["title"], "status": r["status"]}
            apply_readable_id_inplace(dep_item, "activity")
            dependencies.append(dep_item)

        # 4. pinsテーブル経由のpinned targets取得（新pinsテーブル経由）
        pinned_targets = _get_pinned_targets(conn, activity_id)

        # 5. materials取得（リレーション経由、カタログ形式）
        materials = get_materials_by_relation_with_conn(conn, activity_id)

        # 5a. recent_decisions取得（関連topic横断、フラット15件）
        recent_decisions = _get_decisions_from_topics(conn, direct["topic"])

        # 5b. logs取得（最新1件はcontent付き、残りはカタログ）
        latest_log, logs_catalog = _get_logs_catalog_from_topics(conn, direct["topic"])

        # 6. coverage算出（pins注入targetsは含めない）
        total_decisions = _count_decisions_from_topics(conn, direct["topic"])
        total_materials_row = conn.execute(
            "SELECT COUNT(*) FROM relations WHERE source_type = 'activity' AND source_id = ? AND target_type = 'material'",
            (activity_id,),
        ).fetchone()
        total_materials = total_materials_row[0] if total_materials_row else 0
        total_logs = (1 if latest_log else 0) + len(logs_catalog)
        loaded_logs = 1 if latest_log else 0

        coverage = {
            "decisions": f"{len(recent_decisions)}/{total_decisions}",
            "materials": f"{len(materials)}/{total_materials}",
            "logs": f"{loaded_logs}/{total_logs}",
        }

        # 7. 2次カタログ取得（depth 1-2）
        catalog = _get_map_with_conn(conn, "activity", activity_id, min_depth=1, max_depth=2)

        # 8. status自動更新（in_progress以外ならin_progressに変更）
        # NOTE: update_activityは内部で別コネクションを使用する（既存APIの制約）。
        # check_inのトランザクションとは独立してコミットされる。
        if activity["status"] != "in_progress":
            update_result = activity_service.update_activity(activity_id, status="in_progress")
            if "error" in update_result:
                logger.warning(
                    "Failed to update activity %d status: %s",
                    activity_id,
                    update_result["error"],
                )
            else:
                activity["status"] = "in_progress"

        # 9. recomposeナッジhint生成（既存connを共有して読み取り）
        recompose_hints = _get_recompose_hints(conn, activity_id)

        # 10. summary生成
        summary = _build_summary(activity, tags)

        # 戻り値組み立て（coverageをトップレベルの最初のキーに）
        activity_block = {
            "id": activity["id"],
            "title": activity["title"],
            "description": activity["description"],
            "status": activity["status"],
            "tags": tags,
        }
        apply_readable_id_inplace(activity_block, "activity")
        result = {
            "coverage": coverage,
            "activity": activity_block,
        }

        if related_topics:
            if len(related_topics) == 1:
                result["topic"] = related_topics[0]
            result["related_topics"] = related_topics

        if related_activities:
            result["related_activities"] = related_activities

        if dependencies:
            result["dependencies"] = dependencies

        if pinned_targets:
            result["pinned"] = pinned_targets

        result["tag_notes"] = tag_notes
        result["materials"] = materials
        result["recent_decisions"] = recent_decisions
        result["latest_log"] = latest_log
        result["logs"] = logs_catalog
        if catalog:
            result["catalog"] = catalog
        if recompose_hints:
            result["hints"] = recompose_hints
        result["summary"] = summary

        return result

    except Exception as e:
        return {
            "error": {
                "code": "DATABASE_ERROR",
                "message": str(e),
            }
        }
    finally:
        conn.close()
