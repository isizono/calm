"""ow_project_activities: ow_workers の状態を activities テーブルに反映する projector。

reducer (純粋・ow_*のみ書き込み) と分離した副作用許可レイヤ。`ow:managed` タグ付きの
activity に限定して書き込むことで、人間が手動で update_activity した内容を踏み潰さない。

遷移ルール:
- alive worker が working/blocked/escalated/draining → activity.status = in_progress
- 全 worker が terminated で cause=closed → activity.status = completed
- terminated worker の最新 cause = cancelled → activity.status = completed + tag outcome:cancelled
- terminated worker の最新 cause が crashed/dead/crashed-during-drain → completed + tag outcome:failed

last_heartbeat_at: alive worker があればそのうち最大の last_heartbeat_at を activities に同期。
"""
import sqlite3

from src.services.tag_service import (
    ensure_tag_ids,
    get_entity_tags,
    link_tags,
)

OW_MANAGED_TAG = "ow:managed"

# alive worker と判定する workload_state（terminated 以外）
_ALIVE_STATES = (
    "spawning", "loading", "ready", "working", "blocked", "escalated", "draining",
)
# activity を in_progress とみなす workload_state（spawning/loading/ready は worker 起動段階で
# まだ実作業が始まっていないため除外）
_PROGRESSING_STATES = ("working", "blocked", "escalated", "draining")

_OUTCOME_TAG_BY_CAUSE = {
    "cancelled": "outcome:cancelled",
    "crashed": "outcome:failed",
    "dead": "outcome:failed",
    "crashed-during-drain": "outcome:failed",
}


def _has_tag(conn: sqlite3.Connection, activity_id: int, tag_str: str) -> bool:
    tags = get_entity_tags(conn, "activity_tags", "activity_id", activity_id)
    return tag_str in tags


def _add_tag(conn: sqlite3.Connection, activity_id: int, tag_str: str) -> None:
    if ":" in tag_str:
        ns, name = tag_str.split(":", 1)
        parsed = [(ns, name)]
    else:
        parsed = [("", tag_str)]
    tag_ids = ensure_tag_ids(conn, parsed)
    link_tags(conn, "activity_tags", "activity_id", activity_id, tag_ids)


def _list_managed_activity_ids(
    conn: sqlite3.Connection, *, topic_id: int | None = None
) -> list[int]:
    """ow:managed タグの付いた activity の id 一覧を返す。

    topic_id 指定時は relations テーブルで topic↔activity 紐付け（source='activity'<
    target='topic' の正規化）を経由して絞り込む（migration 0033 で polymorphic 化済み）。
    """
    if topic_id is None:
        rows = conn.execute(
            """
            SELECT DISTINCT a.id FROM activities a
            JOIN activity_tags at ON at.activity_id = a.id
            JOIN tags t ON t.id = at.tag_id
            WHERE t.namespace = 'ow' AND t.name = 'managed'
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT DISTINCT a.id FROM activities a
            JOIN activity_tags at ON at.activity_id = a.id
            JOIN tags t ON t.id = at.tag_id
            JOIN relations r
              ON r.source_type = 'activity' AND r.source_id = a.id
             AND r.target_type = 'topic' AND r.target_id = ?
            WHERE t.namespace = 'ow' AND t.name = 'managed'
            """,
            (topic_id,),
        ).fetchall()
    return [r["id"] for r in rows]


def _workers_for_activity(
    conn: sqlite3.Connection, activity_id: int
) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM ow_workers WHERE activity_id = ? "
        "ORDER BY spawned_at, id",
        (activity_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _derive_activity_status(workers: list[dict]) -> tuple[str | None, str | None]:
    """workers から (next_status, outcome_tag) を導出する。

    どちらも変更不要なら (None, None) を返す。
    """
    if not workers:
        return (None, None)
    alive = [w for w in workers if w["workload_state"] in _ALIVE_STATES]
    progressing = [w for w in workers if w["workload_state"] in _PROGRESSING_STATES]
    if progressing:
        return ("in_progress", None)
    if alive:
        # spawning/loading/ready のみ。activityは pending のまま
        return (None, None)
    # 全員 terminated
    # 最新の terminated_at を持つ worker の cause を採用
    terminated = sorted(
        [w for w in workers if w["workload_state"] == "terminated"],
        key=lambda w: (w["terminated_at"] or "", w["id"]),
        reverse=True,
    )
    latest_cause = terminated[0]["cause"] if terminated else None
    outcome_tag = _OUTCOME_TAG_BY_CAUSE.get(latest_cause) if latest_cause else None
    return ("completed", outcome_tag)


def _project_one(
    conn: sqlite3.Connection, activity_id: int
) -> dict:
    workers = _workers_for_activity(conn, activity_id)
    next_status, outcome_tag = _derive_activity_status(workers)
    row = conn.execute(
        "SELECT status FROM activities WHERE id = ?", (activity_id,)
    ).fetchone()
    if row is None:
        return {"changed_status": False, "added_tag": None, "heartbeat_synced": False}
    current = row["status"]
    changed = False
    if next_status and next_status != current:
        # downgrade を避ける（completed → in_progress は許さない）
        if not (current == "completed" and next_status == "in_progress"):
            conn.execute(
                "UPDATE activities SET status = ? WHERE id = ?",
                (next_status, activity_id),
            )
            changed = True
    added_tag = None
    if outcome_tag and not _has_tag(conn, activity_id, outcome_tag):
        _add_tag(conn, activity_id, outcome_tag)
        added_tag = outcome_tag

    # heartbeat 同期: alive worker のうち最新の last_heartbeat_at
    alive_hbs = [
        w["last_heartbeat_at"] for w in workers
        if w["workload_state"] in _ALIVE_STATES and w["last_heartbeat_at"]
    ]
    heartbeat_synced = False
    if alive_hbs:
        latest_hb = max(alive_hbs)
        conn.execute(
            "UPDATE activities SET last_heartbeat_at = ? WHERE id = ?",
            (latest_hb, activity_id),
        )
        heartbeat_synced = True
    return {
        "activity_id": activity_id,
        "changed_status": changed,
        "added_tag": added_tag,
        "heartbeat_synced": heartbeat_synced,
    }


def ow_project_activities_with_conn(
    conn: sqlite3.Connection,
    *,
    topic_id: int | None = None,
) -> dict:
    """ow:managed タグ付き activity を ow_workers の状態に基づいて更新する。

    Args:
        topic_id: 指定時はそのtopic配下のみ。None なら全 ow:managed activity が対象

    Returns:
        {"projected": [activity_id ...], "details": [...]}
    """
    activity_ids = _list_managed_activity_ids(conn, topic_id=topic_id)
    details = [_project_one(conn, aid) for aid in activity_ids]
    return {
        "projected": [d.get("activity_id") for d in details if d.get("activity_id")],
        "details": details,
    }
