"""check_inのtier形（anchor/control/context/catalog/env）応答を収集・組み立てる。

全体予算（応答全体の字数切り詰め）はこのモジュールの責務に含めない。呼び出し側が
必要に応じて別途適用する。

収集した情報は、以下の5枠に分けて返す（中身が空の枠・キーは省く。ただし
anchor.activity・control.goal・env.coverage・env.sessionは常に置く）。

    anchor:  activity, pinned
    control: goal, asks, dependencies
    context: topics, activities, decisions, latest_log, materials
    catalog: logs, map
    env:     tag_notes, hints, coverage, session, flow_guide
"""
from __future__ import annotations

import logging
import sqlite3
import threading

from src.config import (
    CHECKIN_BUDGET_CHARS,
    CHECKIN_CONTROL_CAP_CHARS,
    CHECKIN_HARD_MAX_CHARS,
    CHECKIN_PINNED_SLOT_CHARS,
    CHECKIN_TAG_NOTES_CAP_CHARS,
)
from src.db import get_connection, row_to_dict
from src.infra import session_identity
from src.services import activity_service, goal_service, hint_service, response_budget, session_ledger_service
from src.services.ask_service import get_pending_asks_with_conn
from src.services.checkin_service import (
    _get_activities_overview,
    _get_decisions_from_topics,
    _get_direct_relations,
    _get_logs_catalog_from_topics,
    _get_pinned_targets,
    _get_topics_info,
    _count_decisions_from_topics,
    _pinned_item_pointer,
)
from src.services.material_service import get_materials_by_relation_with_conn
from src.services.readable_id import strip_entity_id_inplace
from src.services.relation_service import _get_map_with_conn
from src.services.response_budget import BudgetPolicy, CappedSection, CutStep, PinnedPolicy
from src.services.signal_service import record_signal
from src.services.tag_service import _decay_pointer_text, collect_tag_notes_for_injection, get_entity_tags

logger = logging.getLogger(__name__)

# 件数の上限（末尾＝新しい順リストの後方を切り捨てる）。
RELATED_ACTIVITIES_MAX = 20
MATERIALS_MAX = 20
CATALOG_LOGS_MAX = 30
CATALOG_MAP_MAX = 30

# control.asksの上限。awaiting_answer/awaiting_triageを合わせて新しい順に数える。
ASKS_MAX = 5
ASK_ANSWER_BODY_MAX_CHARS = 300

# control.dependenciesの上限。
DEPENDENCIES_MAX = 10

# env.hintsの上限（即時配達hintのみ。7月仕様の値を踏襲）。
HINTS_MAX = 5

# セッション別のcheck_in初回呼び出し追跡（session_idキー、256セッションのLRUで追い出す）。
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
    """このセッションでのcheck_in初回呼び出しならTrueを返し、以後はFalseにする。

    session_idが解決できない（None）場合は記録を読み書きせず、毎回Trueを返す。
    """
    if session_id is None:
        return True
    with _greeted_sessions_lock:
        if session_id in _greeted_sessions:
            return False
        while len(_greeted_sessions) >= _GREETED_SESSIONS_MAX:
            del _greeted_sessions[next(iter(_greeted_sessions))]
        _greeted_sessions[session_id] = True
        return True


def _get_dependencies(conn: sqlite3.Connection, activity_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT a.id, a.title, a.status
           FROM activity_dependencies ad
           JOIN activities a ON a.id = ad.dependency_id
           WHERE ad.dependent_id = ?""",
        (activity_id,),
    ).fetchall()
    result = []
    for r in rows:
        item = {"id": r["id"], "title": r["title"], "status": r["status"]}
        strip_entity_id_inplace(item)
        result.append(item)
    return result


def _get_immediate_hints(conn: sqlite3.Connection, activity_id: int) -> list[str]:
    hints = hint_service.get_hints_with_conn(conn, "activity", activity_id)
    return [h["message"] for h in hints if h["delivery_hint"] == "immediate"]


def _cap_asks(pending_asks: dict, activity_id: int) -> dict | None:
    """awaiting_answer/awaiting_triageを合わせて新しい順にASKS_MAX件へ絞る。

    6件目以降はmoreに件数、nextにget_asksへのポインタを付ける
    （黙って落とさない）。awaiting_triageの残った要素はanswer_bodyを
    ASK_ANSWER_BODY_MAX_CHARSで切り、answer_truncated: trueを付ける。
    """
    combined = [(item, "awaiting_answer") for item in pending_asks.get("awaiting_answer") or []]
    combined += [(item, "awaiting_triage") for item in pending_asks.get("awaiting_triage") or []]
    if not combined:
        return None
    combined.sort(key=lambda pair: pair[0].get("last_seen_at") or "", reverse=True)
    kept, overflow = combined[:ASKS_MAX], combined[ASKS_MAX:]

    result: dict = {"awaiting_answer": [], "awaiting_triage": []}
    for item, kind in kept:
        if kind == "awaiting_triage":
            item = dict(item)
            body = item.get("answer_body") or ""
            if len(body) > ASK_ANSWER_BODY_MAX_CHARS:
                item["answer_body"] = body[:ASK_ANSWER_BODY_MAX_CHARS]
                item["answer_truncated"] = True
        result[kind].append(item)

    if overflow:
        result["more"] = len(overflow)
        result["next"] = [{"tool": "get_asks", "args": {"blocking_activity_id": activity_id}}]
    return result


def _cap_dependencies(dependencies: list[dict], activity_id: int):
    """DEPENDENCIES_MAX件へ絞る。超えた分は件数とget_mapへのポインタにする
    （黙って落とさない）。超過が無ければ素のリストのまま返す。
    """
    if not dependencies:
        return None
    kept, overflow = dependencies[:DEPENDENCIES_MAX], dependencies[DEPENDENCIES_MAX:]
    if not overflow:
        return kept
    return {
        "items": kept,
        "more": len(overflow),
        "next": [{"tool": "get_map", "args": {"entity_type": "activity", "entity_id": activity_id}}],
    }


def _collect_static(conn: sqlite3.Connection, activity_id: int, session_id: str | None) -> dict | None:
    """statusを変更する前に完結する読み取り（tag_notesの注入済み記録更新は除く）。

    activityが見つからない場合はNoneを返す。
    """
    row = conn.execute("SELECT * FROM activities WHERE id = ?", (activity_id,)).fetchone()
    if row is None:
        return None
    activity = row_to_dict(row)
    tags = get_entity_tags(conn, "activity_tags", "activity_id", activity_id)
    tag_notes = collect_tag_notes_for_injection(
        conn, tags, session_id=session_id, always_inject_namespaces=["intent"]
    ) or []

    direct = _get_direct_relations(conn, "activity", activity_id)
    related_topics = _get_topics_info(conn, direct["topic"])
    related_activities = _get_activities_overview(conn, direct["activity"])
    dependencies = _get_dependencies(conn, activity_id)
    pinned_targets = _get_pinned_targets(conn, activity_id)

    materials_full = get_materials_by_relation_with_conn(conn, activity_id)
    recent_decisions = _get_decisions_from_topics(conn, direct["topic"])
    latest_log, logs_catalog_full = _get_logs_catalog_from_topics(conn, direct["topic"])

    total_decisions = _count_decisions_from_topics(conn, direct["topic"])
    total_materials_row = conn.execute(
        "SELECT COUNT(*) FROM relations WHERE source_type = 'activity' AND source_id = ? AND target_type = 'material'",
        (activity_id,),
    ).fetchone()
    total_materials = total_materials_row[0] if total_materials_row else 0
    total_logs = (1 if latest_log else 0) + len(logs_catalog_full)

    catalog_map_full = _get_map_with_conn(conn, "activity", activity_id, min_depth=1, max_depth=2)

    return {
        "activity": activity,
        "tags": tags,
        "tag_notes": tag_notes,
        "related_topics": related_topics,
        "related_activities": related_activities[:RELATED_ACTIVITIES_MAX],
        "dependencies": dependencies,
        "pinned_targets": pinned_targets,
        "materials": materials_full[:MATERIALS_MAX],
        "recent_decisions": recent_decisions,
        "latest_log": latest_log,
        "logs_catalog": logs_catalog_full[:CATALOG_LOGS_MAX],
        "catalog_map": catalog_map_full[:CATALOG_MAP_MAX],
        "coverage": {
            "decisions": f"{len(recent_decisions)}/{total_decisions}",
            "materials": f"{min(len(materials_full), MATERIALS_MAX)}/{total_materials}",
            "logs": f"{1 if latest_log else 0}/{total_logs}",
        },
    }


def _transition_status(activity_id: int, activity: dict) -> None:
    """statusがin_progress以外ならin_progressへ更新する（completed等も再オープン）。

    update_activityは内部で別コネクションを使用するため、check_inのトランザクション
    とは独立にコミットされる。呼び出しは、このconnで後続の書き込み（hintの
    クールダウンマーカー等）を始める前に行う必要がある。同じconnが未コミットの
    書き込みトランザクションを抱えたままだと、別コネクション側がSQLiteの
    writerロックで待たされうるため。
    """
    if activity["status"] == "in_progress":
        return
    update_result = activity_service.update_activity(activity_id, status="in_progress")
    if "error" in update_result:
        logger.warning(
            "Failed to update activity %d status: %s", activity_id, update_result["error"]
        )
    else:
        activity["status"] = "in_progress"


def _build_goal_block(conn: sqlite3.Connection, activity_id: int, session_id: str | None) -> dict:
    """goalブロックの組み立てを本体と別のtryで囲む。失敗しても他のキーは失わず、

    goalにerrorの形を置くだけにする。machine_errorのsignalを同じ接続で記録する。
    """
    try:
        return goal_service.build_goal_block_for_activity(conn, activity_id)
    except Exception as e:
        try:
            record_signal(
                "machine_error",
                f"check_inでgoalブロック組み立てに失敗: activity {activity_id}",
                source="tool:check_in",
                detail=str(e),
                session_id=session_id,
                conn=conn,
            )
        except Exception:
            logger.debug("failed to record machine_error signal for goal block", exc_info=True)
        return {"error": {"code": "DATABASE_ERROR", "message": "goal ブロックを組み立てられなかった"}}


def _register_session(activity_id: int, activity: dict) -> tuple[dict, str | None]:
    """セッション別名レジストリを更新する。呼び出し元識別子を解決できない場合や

    予期せぬ例外が起きた場合もcheck_in本体は失敗させない。

    Returns: (env.session block, bridge_id)
    """
    bridge_id = None
    reg = None
    try:
        from src.services import session_registry_service

        bridge_id = session_identity.get_caller_session_id()
        if bridge_id:
            reg = session_registry_service.register_checkin(
                bridge_session_id=bridge_id,
                activity_id=activity_id,
                activity_title=activity["title"],
                activity_status=activity["status"],
            )
    except Exception:
        logger.debug("session registry update failed", exc_info=True)
        reg = None

    if reg is None:
        return {"registered": False, "reason": "cli_unresolved"}, bridge_id
    return (
        {"name": reg["name"], "alias": reg["alias"], "alias_collision": reg["collided"]},
        bridge_id,
    )


def _drop_empty(d: dict) -> dict:
    """値が None・空list・空dictのキーを除いた新しいdictを返す（0や空文字は残す）。"""
    return {k: v for k, v in d.items() if v not in (None, [], {})}


def collect_and_assemble(activity_id: int, session_id: str | None = None) -> dict:
    """アクティビティにcheck-inし、tier形（anchor/control/context/catalog/env）の

    応答を組み立てる。全体予算の適用は行わない。

    Args:
        activity_id: アクティビティID

    Returns:
        tier形のcheck-in結果。中身が空の枠・キーは省く（anchor.activity・
        control.goal・env.coverage・env.sessionは常に置く）。activityが
        存在しない場合は{"error": {"code": "NOT_FOUND", ...}}、内部エラー時は
        {"error": {"code": "DATABASE_ERROR", ...}}を返す。
    """
    if session_id is None:
        session_id = session_identity.get_caller_session_id()
    conn = get_connection()
    try:
        static = _collect_static(conn, activity_id, session_id)
        if static is None:
            return {"error": {"code": "NOT_FOUND", "message": f"Activity with id {activity_id} not found"}}

        activity = static["activity"]
        _transition_status(activity_id, activity)

        immediate_hints = _get_immediate_hints(conn, activity_id)
        pending_asks = get_pending_asks_with_conn(conn, activity_id)
        goal_block = _build_goal_block(conn, activity_id, session_id)
        session_block, bridge_id = _register_session(activity_id, activity)
        flow_guide = _FLOW_GUIDE_COMPACT if _consume_first_call_flag(session_id) else None

        activity_block = {
            "id": activity["id"],
            "title": activity["title"],
            "description": activity["description"],
            "status": activity["status"],
            "tags": static["tags"],
        }
        strip_entity_id_inplace(activity_block)

        # activity・goal・coverage・sessionは値が常に非空dictのため、_drop_emptyで
        # 削られることはない（anchor.activity・control.goal・env.coverage・
        # env.sessionを常に置くという契約は、この非空性がそのまま満たす）。
        anchor = _drop_empty({"activity": activity_block, "pinned": static["pinned_targets"]})

        control = _drop_empty(
            {
                "goal": goal_block,
                "asks": _cap_asks(pending_asks, activity_id),
                "dependencies": _cap_dependencies(static["dependencies"], activity_id),
            }
        )

        context = _drop_empty(
            {
                "topics": static["related_topics"],
                "activities": static["related_activities"],
                "decisions": static["recent_decisions"],
                "latest_log": static["latest_log"],
                "materials": static["materials"],
            }
        )

        catalog = _drop_empty({"logs": static["logs_catalog"], "map": static["catalog_map"]})

        env = _drop_empty(
            {
                "tag_notes": static["tag_notes"],
                "hints": immediate_hints[:HINTS_MAX],
                "coverage": static["coverage"],
                "session": session_block,
                "flow_guide": flow_guide,
            }
        )

        result: dict = {"anchor": anchor, "control": control}
        if context:
            result["context"] = context
        if catalog:
            result["catalog"] = catalog
        result["env"] = env

        conn.commit()

        try:
            session_ledger_service.record_checkin(bridge_id, activity_id)
        except Exception:
            logger.debug("session ledger check-in record failed", exc_info=True)

        return result

    except Exception as e:
        conn.rollback()
        return {"error": {"code": "DATABASE_ERROR", "message": str(e)}}
    finally:
        conn.close()


# --- 全体予算（tier形の方針） -------------------------------------------------
#
# response_budget.apply_budgetはこのモジュールの応答形（anchor/control/context/
# catalog/env）を知らない。パスをドット区切りで教える方針定数をここに置く。


def _topic_ids(response: dict) -> list[int]:
    topics = response.get("context", {}).get("topics") if isinstance(response.get("context"), dict) else None
    return [t["id_raw"] for t in topics or [] if isinstance(t, dict) and "id_raw" in t]


def _activity_id(response: dict) -> int | None:
    anchor = response.get("anchor")
    activity = anchor.get("activity") if isinstance(anchor, dict) else None
    return activity.get("id_raw") if isinstance(activity, dict) else None


def _first_topic_pointer(tool: str):
    """context.topicsの各topicごとに、指定toolへのポインタ一覧を組み立てる関数を返す。"""
    def _pointer(response: dict) -> list[dict]:
        return [
            {"tool": tool, "args": {"entity_type": "topic", "entity_id": tid}}
            for tid in _topic_ids(response)
        ]
    return _pointer


def _catalog_map_pointer(response: dict) -> list[dict]:
    return [{"tool": "get_map", "args": {"entity_type": "activity", "entity_id": _activity_id(response)}}]


def _materials_pointer(response: dict) -> list[dict]:
    return [
        {"tool": "get_timeline", "args": {"activity_id": _activity_id(response), "entity_types": ["material"]}}
    ]


def _activity_description_pointer(response: dict) -> list[dict]:
    return [{"tool": "get_by_ids", "args": {"items": [{"type": "activity", "id": _activity_id(response)}]}}]


def _fold_tag_notes(response: dict) -> None:
    """env.tag_notesの天井超過分を、大きいnotesから順にdecayと同じ1行ポインタへ縮退させる。"""
    env = response.get("env")
    notes = env.get("tag_notes") if isinstance(env, dict) else None
    if not isinstance(notes, list) or not notes:
        return
    sized = sorted(
        (item for item in notes if isinstance(item, dict)),
        key=lambda item: response_budget.measure_chars(item.get("notes", "")),
        reverse=True,
    )
    for item in sized:
        if response_budget.measure_chars(notes) <= CHECKIN_TAG_NOTES_CAP_CHARS:
            break
        tag = item.get("tag")
        if isinstance(tag, str):
            item["notes"] = _decay_pointer_text(tag)


# tier形のcheck_in応答に対する予算方針。保護パス（予算に数えるが削らない）は
# anchor.activity・context.topics・env.hints・env.session・env.coverage・
# env.flow_guideの6つ。control全体とenv.tag_notesは全体予算10,000字には
# 数えず、それぞれ独立の天井（3,000字・6,000字）を持つ。
TIER_FORM_BUDGET_POLICY = BudgetPolicy(
    budget_chars=CHECKIN_BUDGET_CHARS,
    hard_max_chars=CHECKIN_HARD_MAX_CHARS,
    coverage_path="env.coverage",
    activity_path="anchor.activity",
    protected_paths=frozenset({
        "anchor.activity", "context.topics", "env.hints", "env.session",
        "env.coverage", "env.flow_guide",
    }),
    capped_sections=(
        CappedSection(
            name="control", paths=("control",),
            cap_chars=CHECKIN_CONTROL_CAP_CHARS, fold=None,
        ),
        CappedSection(
            name="tag_notes", paths=("env.tag_notes",),
            cap_chars=CHECKIN_TAG_NOTES_CAP_CHARS, fold=_fold_tag_notes,
        ),
    ),
    pinned=PinnedPolicy(
        path="anchor.pinned",
        slot_chars=CHECKIN_PINNED_SLOT_CHARS,
        content_field={"decisions": "reason", "logs": "content", "materials": "content"},
        pointer=_pinned_item_pointer,
    ),
    cut_steps=(
        CutStep(path="catalog.map", mode="tail_list", pointer=_catalog_map_pointer),
        CutStep(path="catalog.logs", mode="tail_list", pointer=_first_topic_pointer("get_logs")),
        CutStep(path="context.materials", mode="tail_list", coverage_key="materials", pointer=_materials_pointer),
        CutStep(path="context.activities", mode="tail_list"),
        CutStep(path="context.decisions", mode="tail_list", coverage_key="decisions", pointer=_first_topic_pointer("get_decisions")),
        CutStep(path="context.latest_log", mode="stub_dict", coverage_key="logs", pointer=_first_topic_pointer("get_logs")),
    ),
    hard_max_pointer=_activity_description_pointer,
)
