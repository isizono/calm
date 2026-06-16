"""ow_apply_state: relay history → ow_workers / ow_channels / ow_applied_msg_ids
への純粋 reducer。

設計原則:
- CQS分離: ow_*テーブルへの書き込みのみ。activities への書き込みは projector が行う
- idempotent: 同じ history を再適用しても結果は変わらない（ow_applied_msg_ids で管理）
- 解釈不能な envelope は warning ログを残して outcome=skipped で記録（適用済み扱い、再試行しない）

各 relay envelope が触る列の対応:
- command:assign (orch → worker): ow_workers 行を find_or_insert し
  activity_id / model / cwd / permission_mode / timeout_min / topic_id を反映
- event:identity (worker → *): ow_workers 行を find_or_insert し
  session_id / alias / model / cwd / activity_id を反映
- event:state(<state>): ow_workers の workload_state / cause / ready_at / terminated_at /
  last_state_msg_id / last_heartbeat_at を反映
- event:heartbeat(alive): ow_workers.last_heartbeat_at を更新
"""
import logging
from typing import Any

from src.services.ow import applied_msgs as am
from src.services.ow import channels as ch
from src.services.ow import workers as wk

logger = logging.getLogger(__name__)


_VALID_STATES = {
    "spawning", "loading", "ready", "working",
    "blocked", "escalated", "draining", "terminated",
}
_VALID_CAUSES = {"closed", "cancelled", "dead", "crashed", "crashed-during-drain"}


def _parse_msg(msg: dict) -> dict | None:
    """relay 1メッセージから ow envelope を取り出して dict として返す。

    body が dict でない / v != 1 / kind が event/command でない場合は None を返し
    呼び出し側で skipped 扱いとする。
    """
    body = msg.get("body")
    if not isinstance(body, dict):
        return None
    if body.get("v") != 1:
        return None
    kind = body.get("kind")
    if kind not in ("event", "command"):
        return None
    data = body.get("data")
    if not isinstance(data, dict):
        return None
    return {
        "msg_id": msg.get("msg_id"),
        "handle": msg.get("handle"),
        "created_at": msg.get("created_at"),
        "kind": kind,
        "from_": body.get("from"),
        "to": body.get("to"),
        "task": body.get("task"),
        "data_type": data.get("type"),
        "data": data,
    }


def _find_or_insert_worker(
    conn,
    *,
    channel_code: str,
    handle: str,
    alias: str | None,
    activity_id: int | None,
    topic_id: int,
    spawned_at: str,
    workload_state: str = "spawning",
) -> dict | None:
    """alive worker を探し、無ければ INSERT。返却は ow_workers 1行 dict。

    部分UNIQUE 違反（同 channel + handle で別 alive 行）が起きた場合は None を返す
    （reducer は次の event を保留せず処理を継続する）。
    """
    existing = wk.get_alive_worker_by_handle_with_conn(
        conn, channel_code=channel_code, handle=handle
    )
    if existing:
        return existing
    task_n = wk.allocate_task_n_with_conn(conn, channel_code)
    wid = wk.insert_worker_with_conn(
        conn,
        channel_code=channel_code,
        handle=handle,
        alias=alias or handle,
        activity_id=activity_id,
        topic_id=topic_id,
        task_n=task_n,
        spawned_at=spawned_at,
        workload_state=workload_state,
    )
    return wk.get_worker_by_id_with_conn(conn, wid)


def _apply_command_assign(
    conn, parsed: dict, *, channel_code: str, topic_id_default: int
) -> bool:
    data = parsed["data"]
    to = parsed["to"]
    if not isinstance(to, str) or to in ("*", "orch", ""):
        return False
    handle = to
    activity_id = data.get("activity_id")
    topic_id_raw = data.get("topic_id") or topic_id_default
    try:
        topic_id = int(topic_id_raw)
    except (TypeError, ValueError):
        return False
    worker = _find_or_insert_worker(
        conn,
        channel_code=channel_code,
        handle=handle,
        alias=handle,
        activity_id=activity_id,
        topic_id=topic_id,
        spawned_at=parsed["created_at"] or "",
    )
    if not worker:
        return False
    # assign metadata 反映（途中変更も許容）
    conn.execute(
        """
        UPDATE ow_workers
        SET activity_id = COALESCE(?, activity_id),
            model = COALESCE(?, model),
            cwd = COALESCE(?, cwd),
            permission_mode = COALESCE(?, permission_mode),
            timeout_min = COALESCE(?, timeout_min)
        WHERE id = ?
        """,
        (
            activity_id,
            data.get("model"),
            data.get("cwd"),
            data.get("permission_mode"),
            data.get("timeout_min"),
            worker["id"],
        ),
    )
    return True


def _apply_event_identity(
    conn, parsed: dict, *, channel_code: str, topic_id_default: int
) -> bool:
    data = parsed["data"]
    handle = parsed["from_"]
    if not isinstance(handle, str) or not handle:
        return False
    role = data.get("role")
    if role and role != "worker":
        # orch / user 等の identity は ow_workers の対象外
        return True  # skip success
    activity_id = data.get("activity_id")
    topic_id_raw = data.get("topic_id") or topic_id_default
    try:
        topic_id = int(topic_id_raw)
    except (TypeError, ValueError):
        return False
    worker = _find_or_insert_worker(
        conn,
        channel_code=channel_code,
        handle=handle,
        alias=data.get("alias") or handle,
        activity_id=activity_id,
        topic_id=topic_id,
        spawned_at=data.get("started_at") or parsed["created_at"] or "",
    )
    if not worker:
        return False
    terminated_at = None
    if data.get("terminated_at"):
        terminated_at = data["terminated_at"]
    wk.update_worker_identity_with_conn(
        conn,
        worker_id=worker["id"],
        session_id=data.get("session_id"),
        model=data.get("model"),
        cwd=data.get("cwd"),
    )
    # identity 再 append (terminated_at 付き) の場合は terminated_at を反映
    if terminated_at:
        conn.execute(
            "UPDATE ow_workers SET terminated_at = COALESCE(terminated_at, ?) WHERE id = ?",
            (terminated_at, worker["id"]),
        )
    return True


def _apply_event_state(
    conn, parsed: dict, *, channel_code: str, topic_id_default: int
) -> bool:
    data = parsed["data"]
    handle = parsed["from_"]
    if not isinstance(handle, str) or not handle:
        return False
    state = data.get("state")
    if state not in _VALID_STATES:
        return False
    cause = data.get("cause")
    if cause is not None and cause not in _VALID_CAUSES:
        cause = None
    worker = _find_or_insert_worker(
        conn,
        channel_code=channel_code,
        handle=handle,
        alias=handle,
        activity_id=None,
        topic_id=topic_id_default,
        spawned_at=parsed["created_at"] or "",
        workload_state=state,
    )
    if not worker:
        return False
    ready_at = parsed["created_at"] if state == "ready" else None
    terminated_at = parsed["created_at"] if state == "terminated" else None
    wk.update_worker_state_with_conn(
        conn,
        worker_id=worker["id"],
        workload_state=state,
        cause=cause,
        last_state_msg_id=parsed["msg_id"],
        last_heartbeat_at=parsed["created_at"],
        ready_at=ready_at,
        terminated_at=terminated_at,
        session_id=data.get("session_id"),
    )
    return True


def _apply_event_heartbeat(
    conn, parsed: dict, *, channel_code: str, topic_id_default: int
) -> bool:
    handle = parsed["from_"]
    if not isinstance(handle, str) or not handle:
        return False
    worker = wk.get_alive_worker_by_handle_with_conn(
        conn, channel_code=channel_code, handle=handle
    )
    if not worker:
        # heartbeat 先行はあり得る（identity 前の alive 信号）。記録対象 worker が
        # まだ無いだけなのでスキップ成功扱い。
        return True
    wk.update_worker_heartbeat_with_conn(
        conn,
        worker_id=worker["id"],
        last_heartbeat_at=parsed["created_at"] or "",
    )
    return True


def ow_apply_state_with_conn(
    conn,
    *,
    channel_code: str,
    topic_id: int,
    history: list[dict],
    now: str,
) -> dict:
    """relay history を消化して ow_* テーブルを更新する純粋 reducer。

    Args:
        history: relay /history の messages 配列。事前取得済みのものを渡す
            （reducer をテスト可能にし、副作用範囲を呼び出し側に明示するため）
        now: applied_at に使う現在時刻文字列（ISO8601 UTC）

    Returns:
        {
            "applied": int,         # 新規に適用した msg 数
            "skipped": int,         # 解釈不能で skipped 記録した数
            "duplicate": int,       # 既適用で何もしなかった数
            "last_msg_id": int,     # 入力 history の最大 msg_id（0 if empty）
        }
    """
    applied_set = am.get_applied_msg_id_set_with_conn(
        conn, channel_code=channel_code
    )
    counters = {"applied": 0, "skipped": 0, "duplicate": 0, "last_msg_id": 0}
    # msg_id 昇順で安定処理（relay履歴は単調増加だが念のため）
    for msg in sorted(history, key=lambda m: m.get("msg_id") or 0):
        mid = msg.get("msg_id")
        if not isinstance(mid, int):
            continue
        counters["last_msg_id"] = max(counters["last_msg_id"], mid)
        if mid in applied_set:
            counters["duplicate"] += 1
            continue
        parsed = _parse_msg(msg)
        if parsed is None:
            am.mark_msg_applied_with_conn(
                conn, channel_code=channel_code,
                msg_id=mid, applied_at=now, outcome="skipped",
            )
            counters["skipped"] += 1
            continue
        success = _dispatch(
            conn, parsed,
            channel_code=channel_code, topic_id_default=topic_id,
        )
        if success:
            am.mark_msg_applied_with_conn(
                conn, channel_code=channel_code,
                msg_id=mid, applied_at=now, outcome="applied",
            )
            counters["applied"] += 1
        else:
            logger.warning(
                "ow_apply_state: msg_id=%s dispatch failed (type=%s)",
                mid, parsed.get("data_type"),
            )
            am.mark_msg_applied_with_conn(
                conn, channel_code=channel_code,
                msg_id=mid, applied_at=now, outcome="skipped",
            )
            counters["skipped"] += 1
    if counters["last_msg_id"] > 0:
        ch.update_channel_last_seen_with_conn(
            conn, channel_code=channel_code,
            last_seen_msg_id=counters["last_msg_id"], now=now,
        )
    return counters


def _dispatch(
    conn, parsed: dict, *, channel_code: str, topic_id_default: int
) -> bool:
    kind = parsed["kind"]
    dtype = parsed["data_type"]
    if kind == "command" and dtype == "assign":
        return _apply_command_assign(
            conn, parsed,
            channel_code=channel_code, topic_id_default=topic_id_default,
        )
    if kind == "event":
        if dtype == "identity":
            return _apply_event_identity(
                conn, parsed,
                channel_code=channel_code, topic_id_default=topic_id_default,
            )
        if dtype == "state":
            return _apply_event_state(
                conn, parsed,
                channel_code=channel_code, topic_id_default=topic_id_default,
            )
        if dtype == "heartbeat":
            return _apply_event_heartbeat(
                conn, parsed,
                channel_code=channel_code, topic_id_default=topic_id_default,
            )
    # 未対応の type は skipped 扱い（reducer はビジネスロジックを持たない）
    return False
