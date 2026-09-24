"""set_goal・update_goal・judge_goal・get_goal MCPツールのユニットテスト。

src.main 経由の薄い配線（goal_service への委譲、update_goal での
_current_session_id() の注入）だけを検証する。分岐の詳細は
tests/unit/test_goal_service.py・test_goal_service_derive.py が担う。

update_activity・check_in が goal 機構と接続する main.py 側の薄い配線
（closed_by/closed_reason/goal_hint の受け渡し、check_in の flavor 展開）も
ここで扱う。activity_service.update_activity 自体の分岐は
tests/unit/test_update_activity.py が担う。
"""
from src.db import get_connection
from src.main import check_in, set_goal, update_activity, update_goal, judge_goal, get_goal
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


def test_update_activity_tool_passes_closed_by_and_reason_through(temp_db):
    """src.main.update_activityがclosed_by/closed_reasonをactivity_serviceへ
    そのまま渡す配線を確かめる。"""
    act = _activity()

    result = update_activity(act, status="completed", closed_by="user", closed_reason="ユーザーが完了を宣言")
    assert "error" not in result

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT closed_by, closed_reason FROM activities WHERE id = ?", (act,)
        ).fetchone()
    finally:
        conn.close()
    assert row["closed_by"] == "user"
    assert row["closed_reason"] == "ユーザーが完了を宣言"


def test_update_activity_tool_surfaces_goal_hint(temp_db):
    """未判定goal付きのactivityをcompletedにすると、main.py経由でもgoal_hintが返る。"""
    act = _activity()
    set_goal(
        act,
        {"new": {"handle": "tool-hint-1", "statement": "終わる", "conditions": [
            {"statement": "c1", "actor": "claude"},
        ]}},
    )

    result = update_activity(act, status="completed", closed_by="user")

    assert "error" not in result
    assert "goal_hint" in result
    assert result["goal_hint"]["handle"] == "tool-hint-1"
    assert result["goal_hint"]["warning"].startswith("goalが未判定のまま")


def test_check_in_flavor_readable_expands_goal_bound_titles(temp_db):
    """check_inツールのflavor='readable'指定で、goalブロックのremainingの
    bound文字列がcitation_renderer.expandを通る（配線の確認。展開の変換内容
    自体はcitation_renderer側のテストが担う）。
    """
    bound_target = _activity("bound-target")
    act = _activity("a-with-goal")
    set_goal(
        act,
        {"new": {"handle": "flavor-g1", "statement": "終わりの一文", "conditions": [
            {
                "statement": "条件A",
                "actor": "claude",
                "bound": {"type": "activity", "id": bound_target},
            },
        ]}},
    )

    import src.main as main_module

    expanded_inputs = []
    original_expand = main_module.citation_renderer.expand

    def spy_expand(content, flavor, conn):
        expanded_inputs.append(content)
        return original_expand(content, flavor, conn)

    main_module.citation_renderer.expand = spy_expand
    try:
        result = check_in(act, flavor="readable")
    finally:
        main_module.citation_renderer.expand = original_expand

    assert "error" not in result
    remaining = result["control"]["goal"]["remaining"]
    assert len(remaining) == 1
    assert any("activity『" in text for text in expanded_inputs)


def test_check_in_flavor_raw_does_not_expand_goal_block(temp_db):
    """flavor='raw'はcitation展開自体をスキップする（_apply_flavor_to_check_in_result
    がそもそも呼ばれない既存の分岐）。"""
    act = _activity()
    set_goal(
        act,
        {"new": {"handle": "flavor-g3", "statement": "終わりの一文3", "conditions": [
            {"statement": "条件1", "actor": "claude"},
        ]}},
    )

    import src.main as main_module

    called = []
    orig = main_module._apply_flavor_to_check_in_result

    def spy(*args, **kwargs):
        called.append(True)
        return orig(*args, **kwargs)

    main_module._apply_flavor_to_check_in_result = spy
    try:
        result = check_in(act, flavor="raw")
    finally:
        main_module._apply_flavor_to_check_in_result = orig

    assert "error" not in result
    assert called == []
