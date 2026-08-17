"""check-inサービス"""
import logging
import sqlite3
import threading

from src.db import get_connection, row_to_dict
from src.services import activity_service, ask_service, hint_service
from src.services.readable_id import strip_entity_id_inplace
from src.services.material_service import get_materials_by_relation_with_conn
from src.services.relation_service import _get_map_with_conn
from src.services.supersede_service import compute_destabilization_info_batch
from src.services.tag_service import (
    collect_tag_notes_for_injection,
    get_entity_tags,
)
from src.services.topic_service import count_decisions_per_topic, count_materials_per_topic

logger = logging.getLogger(__name__)

# 1次 decisions の展開上限
DECISIONS_FULL_LIMIT = 15

# セッション別のcheck_in初回呼び出し追跡（session_idキー）。
# セッション終了はこのモジュールに通知されないため、tag_serviceの_injected_tagsと
# 同様に、上限超過時は挿入順の最古セッションから追い出す（dictは挿入順を保持する）。
# 追い出された長寿セッションは次回check_inでガイドを再度受け取るだけで実害はない。
_greeted_sessions: dict[str, bool] = {}
_greeted_sessions_lock = threading.Lock()
_GREETED_SESSIONS_MAX = 256

_FLOW_GUIDE_COMPACT = (
    "深掘りの手がかり: 経緯の詳細はget_decisions・get_logsで辿れる（議論の経緯は"
    "logsにあることが多い）。キーワード探索はsearch、結果の本文取得はget_by_ids"
    "（search結果のチェリーピック・参照先の一括取得・IDで聞かれたときに使う）。"
    "長期的に参照し続けるエンティティ（ユビキタス言語のmaterial、方針を決める"
    "decision等）はupdate_pinでピン留めする。関連構造の俯瞰はget_map、時系列の"
    "変遷はget_timelineで追える。supersedes・depends_onリレーション"
    "（add_relationで設定）は差し替えやブロッカーの管理に使う。"
)


def _consume_first_call_flag(session_id: str | None) -> bool:
    """このセッションでのcheck_in初回呼び出しならTrueを返し、以後はFalseにする。"""
    session_key = session_id or "__default__"
    with _greeted_sessions_lock:
        if session_key in _greeted_sessions:
            return False
        while len(_greeted_sessions) >= _GREETED_SESSIONS_MAX:
            del _greeted_sessions[next(iter(_greeted_sessions))]
        _greeted_sessions[session_key] = True
        return True


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


def _get_pending_asks(conn: sqlite3.Connection, activity_id: int) -> dict:
    """check-in対象activityをblockしているaskを、フェーズ別に返す。

    Returns: {"awaiting_answer": [...], "awaiting_triage": [...]}
    """
    return ask_service.get_pending_asks_with_conn(conn, activity_id)


def _get_recompose_hints(conn: sqlite3.Connection, activity_id: int) -> list[str]:
    """check-in対象activityのdomain:tagについてrecomposeナッジhintメッセージを返す。

    HintService経由でdelivery_hint=immediateのhintのみtool responseに乗せる。
    orch-managed activityでは全hint suppressする。
    """
    if hint_service.is_orch_managed_activity(conn, activity_id):
        return []
    hints = hint_service.get_hints_with_conn(conn, "activity", activity_id)
    return [h["message"] for h in hints if h["delivery_hint"] == "immediate"]


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

    flow_guideの注入ルール:
    - セッション内でcheck_inが初めて呼ばれたときのみ、圧縮版のコンテキスト取得
      フローガイド（flow_guide）をレスポンスに含める。2回目以降のcheck_inでは
      含めない（_greeted_sessions管理、tag_notesの_injected_tagsと同じ方式）。

    セッション別名レジストリ（result["session"]）:
    - 呼び出し元のClaude Code CLIプロセスを解決できた場合、
      {"name": str, "alias": str, "alias_collision": bool} を含める。
      解決できない場合（非CLIクライアント、relay未構成環境の初回起動直後等）は
      {"registered": False, "reason": "cli_unresolved"} を返す。
      このレジストリ更新はベストエフォートであり、失敗してもcheck_in本体は
      成功応答を返す。

    Args:
        activity_id: アクティビティID

    Returns:
        check-in結果（coverage, activity, related_topics, related_activities, pinned,
        tag_notes, materials, recent_decisions, logs, catalog, summary, session。
        セッション内初回呼び出し時のみflow_guideも含む）
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
            strip_entity_id_inplace(dep_item)
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

        # 9. recomposeナッジhint生成（既存connを共有。hint発火時は日次クールダウン
        #    マーカーのnotes書き込みを伴うが、commitは本関数末尾でまとめて行う）
        recompose_hints = _get_recompose_hints(conn, activity_id)

        # 9a. このactivityをblockしているaskの配達（answer待ち・triage待ちフェーズ別）。
        # recompose_hintsと異なりorch-managed activityでもsuppressしない
        # （askは答え待ちというプロセス情報そのものであり、recompose系の提案とは扱いを分ける）。
        pending_asks = _get_pending_asks(conn, activity_id)

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
        strip_entity_id_inplace(activity_block)
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

        if pending_asks["awaiting_answer"] or pending_asks["awaiting_triage"]:
            result["asks"] = pending_asks

        hints = list(recompose_hints)
        if pending_asks["awaiting_triage"]:
            hints.append(
                "answered状態のaskが未トリアージです。triage_askでpromote/dismissへ振り分けてください。"
            )

        # 11. セッション別名レジストリの更新（並行セッションの現在地表示用）。
        # 呼び出し元がClaude Code CLI経由でないなどCLIが解決できない場合や、
        # 内部で予期せぬ例外が起きた場合もcheck_in本体を失敗させない。
        try:
            from src.services.relay.identity import get_relay_identity
            from src.services import session_registry_service

            bridge_id = get_relay_identity()
            reg = (
                session_registry_service.register_checkin(
                    bridge_session_id=bridge_id,
                    activity_id=activity_id,
                    activity_title=activity["title"],
                    activity_status=activity["status"],
                )
                if bridge_id
                else None
            )
        except Exception:
            logger.debug("session registry update failed", exc_info=True)
            reg = None

        if reg is None:
            result["session"] = {"registered": False, "reason": "cli_unresolved"}
        else:
            result["session"] = {
                "name": reg["name"],
                "alias": reg["alias"],
                "alias_collision": reg["collided"],
            }
            if reg["collided"]:
                hints.append(
                    f"セッション別名が他セッションと衝突したため「{reg['alias']}」になりました。"
                    "ユーザーに伝え、必要ならset_session_aliasで付け直してください。"
                )

        if hints:
            result["hints"] = hints

        result["summary"] = summary
        if _consume_first_call_flag(session_id):
            result["flow_guide"] = _FLOW_GUIDE_COMPACT

        conn.commit()
        return result

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
