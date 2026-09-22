"""set_goal・update_goal・judge_goal・get_goal MCPツールのユニットテスト。

src.main 経由の薄い配線（goal_service への委譲、update_goal での
_current_session_id() の注入）だけを検証する。分岐の詳細は
tests/unit/test_goal_service.py・test_goal_service_derive.py が担う。
"""
from src.db import get_connection
from src.main import set_goal, update_goal, judge_goal, get_goal
from src.services.activity_service import add_activity


def _activity(title: str = "a1") -> int:
    return add_activity(title=title, description="d", tags=["domain:test"], check_in=False)[
        "activity_id"
    ]


def test_set_goal_new_delegates_to_goal_service(temp_db):
    act = _activity()
    result = set_goal(
        act,
        {"new": {"handle": "tool-goal", "statement": "終わる", "conditions": [
            {"statement": "c1", "actor": "claude"},
        ]}},
    )
    assert "error" not in result
    assert result["goal"]["handle"] == "tool-goal"


def test_get_goal_reads_back_by_handle(temp_db):
    act = _activity()
    set_goal(
        act,
        {"new": {"handle": "tool-goal-2", "statement": "終わる", "conditions": [
            {"statement": "c1", "actor": "claude"},
        ]}},
    )
    result = get_goal(handle="tool-goal-2")
    assert "error" not in result
    assert len(result["conditions"]) == 1


def test_judge_goal_closes_and_reports_activities(temp_db):
    act = _activity()
    created = set_goal(
        act,
        {"new": {"handle": "tool-goal-3", "statement": "終わる", "conditions": [
            {"statement": "c1", "actor": "claude", "state": "satisfied", "note": "済"},
        ]}},
    )
    goal_id = created["goal"]["goal_id_raw"]
    result = judge_goal(goal_id, "achieved")
    assert "error" not in result
    assert {a["id_raw"] for a in result["closed_activities"]} == {act}


def test_update_goal_reopen_records_signal_without_exposing_session_id_arg(temp_db):
    """update_goal は session_id を引数に取らず、main.py が自動で注入する。

    MCP実行コンテキスト外（このテスト）では _current_session_id() は None を
    返すため、session_id 無しの差し戻しでも signal_events への記録自体は
    成功することを確かめる（main.py側の配線がこの経路を壊していないこと）。
    """
    act = _activity()
    created = set_goal(
        act,
        {"new": {"handle": "tool-goal-4", "statement": "終わる", "conditions": [
            {"statement": "c1", "actor": "claude", "state": "satisfied", "note": "済"},
        ]}},
    )
    goal_id = created["goal"]["goal_id_raw"]
    judge_goal(goal_id, "achieved")

    result = update_goal(goal_id, reopen_reason="判定が誤りだった")
    assert "error" not in result

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT session_id FROM signal_events WHERE kind = 'goal_rollback'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["session_id"] is None
