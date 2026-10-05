"""ask_handover_service と、それを使う応答（check_in枠・update_activity・hook知らせ）の単体テスト。

triage後もask_blocksが残ること、隣の作業（goalの親子・depends_on）を止めるaskの
差し出し、最近決着したaskの7日窓、完了時の未決着ask一覧と付け替えを実DBで検証する。
"""
import pytest

from hooks.ask_notify_section import build_ask_notify_lines, build_neighbor_ask_lines
from hooks.hook_state import HookState
from src.db import get_connection
from src.services import ask_handover_service as ah, ask_service as ak
from src.services.activity_service import add_activity, update_activity
from src.services.checkin_tier_service import collect_and_assemble
from src.services.goal_service import set_goal
from src.services.relation_service import add_relation
from src.services.topic_service import add_topic


def _act(title: str) -> int:
    return add_activity(title=title, description="d", tags=["domain:test"], check_in=False)["activity_id"]


def _ask(question: str, act: int) -> int:
    return ak.add_ask(question, tags=["domain:test"], blocks=[act])["id"]


def _promote(ask_id: int) -> int:
    topic = add_topic(title="t", description="d", tags=["domain:test"])["topic_id"]
    ak.answer_ask(ask_id, "ans")
    return ak.triage_ask(
        ask_id, action="promote", decision="採用する", reason="理由", title="採用の見出し", topic_id=topic
    )["promoted_decision_id"]


def _parent_with_child(parent: int, child: int, handle: str) -> None:
    set_goal(parent, {"new": {"handle": handle, "statement": "s", "conditions": [
        {"statement": "c", "actor": "claude", "bound": {"type": "activity", "id": child}},
    ]}})


def _block_count(ask_id: int) -> int:
    conn = get_connection()
    try:
        return conn.execute("SELECT COUNT(*) FROM ask_blocks WHERE ask_id = ?", (ask_id,)).fetchone()[0]
    finally:
        conn.close()


class TestTriageKeepsBlocks:
    def test_promote_and_dismiss_keep_blocks_withdraw_removes(self, temp_db):
        act = _act("work")
        promoted, dismissed, withdrawn = _ask("q1", act), _ask("q2", act), _ask("q3", act)
        _promote(promoted)
        ak.answer_ask(dismissed, "a")
        ak.triage_ask(dismissed, action="dismiss", dismiss_reason="不要")
        ak.withdraw_ask(withdrawn, "mistake")

        assert _block_count(promoted) == 1
        assert _block_count(dismissed) == 1
        assert _block_count(withdrawn) == 0

    def test_settled_ask_is_not_waiting_in_check_in_own_frame(self, temp_db):
        act = _act("work")
        ask = _ask("q1", act)
        _promote(ask)

        control = collect_and_assemble(act)["control"]

        assert "asks" not in control
        conn = get_connection()
        try:
            assert ak.get_pending_asks_with_conn(conn, act) == {"awaiting_answer": [], "awaiting_triage": []}
        finally:
            conn.close()


class TestNeighbors:
    def test_goal_parent_child_and_depends_on_in_both_directions(self, temp_db):
        parent, child, dep, dependent, stranger = (_act(n) for n in ("parent", "child", "dep", "dependent", "stranger"))
        _parent_with_child(parent, child, "h1")
        add_relation("activity", child, [{"type": "activity", "ids": [dep]}], relation_type="depends_on")
        add_relation("activity", dependent, [{"type": "activity", "ids": [child]}], relation_type="depends_on")
        conn = get_connection()
        try:
            assert ah.get_neighbor_activity_ids(conn, child) == sorted([parent, dep, dependent])
            assert ah.get_neighbor_activity_ids(conn, parent) == [child]
            assert ah.get_neighbor_activity_ids(conn, stranger) == []
        finally:
            conn.close()


class TestCheckInFrames:
    def test_neighbor_asks_show_question_and_work_title_without_answer(self, temp_db):
        parent, child = _act("親の作業"), _act("子の作業")
        _parent_with_child(parent, child, "h2")
        open_id = _ask("子の未決の問い", child)
        answered_id = _ask("子の回答済みの問い", child)
        ak.answer_ask(answered_id, "秘密の回答本文")
        own = _ask("親自身の問い", parent)

        control = collect_and_assemble(parent)["control"]

        items = {i["id_raw"]: i for i in control["neighbor_asks"]["items"]}
        assert set(items) == {open_id, answered_id}
        assert items[open_id] == {
            "id_raw": open_id, "question": "子の未決の問い", "status": "open", "activity": "子の作業",
        }
        assert items[answered_id]["status"] == "answered"
        assert "秘密の回答本文" not in str(control["neighbor_asks"])
        assert [a["id_raw"] for a in control["asks"]["awaiting_answer"]] == [own]

    def test_ask_blocking_both_is_only_in_own_frame(self, temp_db):
        parent, child = _act("p"), _act("c")
        _parent_with_child(parent, child, "h3")
        both = ak.add_ask("両方", tags=["domain:test"], blocks=[parent, child])["id"]

        control = collect_and_assemble(parent)["control"]

        assert [a["id_raw"] for a in control["asks"]["awaiting_answer"]] == [both]
        assert "neighbor_asks" not in control

    def test_neighbor_frame_is_capped_and_overflow_is_pointed_to(self, temp_db):
        parent, child = _act("p"), _act("c")
        _parent_with_child(parent, child, "h4")
        for i in range(5):
            _ask(f"q{i}", child)

        frame = collect_and_assemble(parent)["control"]["neighbor_asks"]

        assert len(frame["items"]) == 3
        assert frame["more"] == 2
        assert frame["next"] == [
            {"tool": "get_asks", "args": {"blocking_activity_id": child, "status": None}}
        ]

    def test_overflow_pointers_name_each_neighbor_holding_overflow(self, temp_db):
        parent, child_a, child_b = _act("p"), _act("a"), _act("b")
        _parent_with_child(parent, child_a, "h8")
        add_relation("activity", parent, [{"type": "activity", "ids": [child_b]}], relation_type="depends_on")
        for i in range(3):
            _ask(f"a{i}", child_a)
        _ask("b0", child_b)
        conn = get_connection()
        try:
            # 並びを固定: b側のaskが最も古く、超過に入る
            conn.execute("UPDATE asks SET last_seen_at = datetime('now', '-1 day') WHERE question = 'b0'")
            conn.commit()
        finally:
            conn.close()

        frame = collect_and_assemble(parent)["control"]["neighbor_asks"]

        assert frame["more"] == 1
        assert [n["args"]["blocking_activity_id"] for n in frame["next"]] == [child_b]

    def test_recent_settled_asks_within_seven_days_with_detail(self, temp_db):
        parent, child = _act("p"), _act("c")
        _parent_with_child(parent, child, "h5")
        promoted = _ask("昇格する問い", child)
        _promote(promoted)
        dismissed = _ask("却下する問い", parent)
        ak.answer_ask(dismissed, "a")
        ak.triage_ask(dismissed, action="dismiss", dismiss_reason="前提が変わった")
        old = _ask("古い問い", child)
        _promote(old)
        conn = get_connection()
        try:
            conn.execute("UPDATE asks SET triaged_at = datetime('now', '-8 days') WHERE id = ?", (old,))
            conn.commit()
        finally:
            conn.close()

        frame = collect_and_assemble(parent)["control"]["recent_settled_asks"]

        by_id = {i["id_raw"]: i for i in frame["items"]}
        assert set(by_id) == {promoted, dismissed}
        assert by_id[promoted]["outcome"] == "promoted"
        assert by_id[promoted]["detail"] == "採用の見出し"
        assert by_id[promoted]["activity"] == "c"
        assert by_id[dismissed]["outcome"] == "dismissed"
        assert by_id[dismissed]["detail"] == "前提が変わった"

    def test_frames_do_not_count_toward_total_budget(self, temp_db):
        # control枠は全体予算に数えない方針のため、枠を足しても切り詰め対象は増えない
        parent, child = _act("p"), _act("c")
        _parent_with_child(parent, child, "h6")
        for i in range(5):
            _ask("長い問い" * 30 + str(i), child)

        from src.services.checkin_tier_service import TIER_FORM_BUDGET_POLICY
        from src.services.response_budget import apply_budget

        result = apply_budget(collect_and_assemble(parent), TIER_FORM_BUDGET_POLICY)

        assert "truncated" not in result


class TestUpdateActivityCompletion:
    def test_completed_lists_pending_asks_without_blocking_completion(self, temp_db):
        act = _act("work")
        open_id = _ask("未決", act)
        answered_id = _ask("回答済み未triage", act)
        ak.answer_ask(answered_id, "本文は出ない")
        settled = _ask("決着済み", act)
        _promote(settled)

        result = update_activity(act, status="completed", closed_by="user", closed_reason="done")

        assert result["status"] == "completed"
        assert [(a["id_raw"], a["status"]) for a in result["pending_asks"]] == [
            (open_id, "open"), (answered_id, "answered"),
        ]
        assert "本文は出ない" not in str(result)

    def test_no_pending_asks_key_when_none(self, temp_db):
        act = _act("work")
        assert "pending_asks" not in update_activity(act, status="completed", closed_by="user")


class TestMoveAsks:
    def test_moves_all_pending_asks_with_completion_and_leaves_none_pending(self, temp_db):
        old, new = _act("old"), _act("new")
        open_id = _ask("未決", old)
        answered_id = _ask("回答済み", old)
        ak.answer_ask(answered_id, "a")

        result = update_activity(old, status="completed", closed_by="user", move_asks_to=new)

        assert {a["id_raw"] for a in result["moved_asks"]} == {open_id, answered_id}
        assert "pending_asks" not in result
        conn = get_connection()
        try:
            rows = conn.execute("SELECT ask_id, activity_id FROM ask_blocks ORDER BY ask_id").fetchall()
        finally:
            conn.close()
        assert [(r["ask_id"], r["activity_id"]) for r in rows] == [(open_id, new), (answered_id, new)]

    def test_move_ask_ids_moves_only_selected_and_rest_stay_pending(self, temp_db):
        old, new = _act("old"), _act("new")
        keep, move = _ask("残す", old), _ask("動かす", old)

        result = update_activity(old, status="completed", closed_by="user", move_asks_to=new, move_ask_ids=[move])

        assert [a["id_raw"] for a in result["moved_asks"]] == [move]
        assert [a["id_raw"] for a in result["pending_asks"]] == [keep]

    @pytest.mark.parametrize("target", ["completed", "missing", "self"])
    def test_invalid_target_rejected_and_nothing_changes(self, temp_db, target):
        old = _act("old")
        ask = _ask("q", old)
        if target == "completed":
            move_to = _act("done")
            update_activity(move_to, status="completed", closed_by="user")
        elif target == "missing":
            move_to = 999999
        else:
            move_to = old

        result = update_activity(old, status="completed", closed_by="user", move_asks_to=move_to)

        assert result["error"]["code"] == "VALIDATION_ERROR"
        conn = get_connection()
        try:
            assert conn.execute("SELECT status FROM activities WHERE id = ?", (old,)).fetchone()[0] != "completed"
        finally:
            conn.close()
        assert _block_count(ask) == 1

    def test_ask_ids_not_pending_on_activity_rejected(self, temp_db):
        old, new, other = _act("old"), _act("new"), _act("other")
        foreign = _ask("他の作業のask", other)

        result = update_activity(old, move_asks_to=new, move_ask_ids=[foreign])

        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "not pending on this activity" in result["error"]["message"]
        assert _block_count(foreign) == 1

    def test_move_alone_without_other_fields_succeeds(self, temp_db):
        old, new = _act("old"), _act("new")
        ask = _ask("q", old)

        result = update_activity(old, move_asks_to=new)

        assert [a["id_raw"] for a in result["moved_asks"]] == [ask]
        conn = get_connection()
        try:
            assert conn.execute("SELECT activity_id FROM ask_blocks WHERE ask_id = ?", (ask,)).fetchone()[0] == new
        finally:
            conn.close()

    def test_move_ask_ids_without_target_rejected(self, temp_db):
        old = _act("old")
        assert update_activity(old, status="in_progress", move_ask_ids=[1])["error"]["code"] == "VALIDATION_ERROR"


@pytest.fixture
def hook_state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(HookState, "BASE_DIR", tmp_path)
    return tmp_path


class TestNeighborAskLines:
    SESSION = "sess-handover-1"

    def _checked_in(self, act: int, since: str = "2000-01-01 00:00:00") -> HookState:
        state = HookState(self.SESSION)
        state.set_checked_in_activity(act)
        state.set_checked_in_at(since)
        return state

    def test_lists_asks_answered_after_check_in_without_body_and_does_not_consume(self, temp_db, hook_state_dir):
        parent, child = _act("親"), _act("子の作業")
        _parent_with_child(parent, child, "h7")
        ask = _ask("子の問い", child)
        ak.answer_ask(ask, "回答本文")
        self._checked_in(parent)

        lines, ids = build_neighbor_ask_lines(self.SESSION)

        assert ids == [ask]
        assert "子の問い" in lines[1] and "子の作業" in lines[1]
        assert "回答本文" not in "\n".join(lines)
        assert build_neighbor_ask_lines(self.SESSION)[1] == [ask]

    def test_answered_in_the_same_second_as_check_in_is_included(self, temp_db, hook_state_dir):
        act = _act("work")
        ask = _ask("q", act)
        ak.answer_ask(ask, "a")
        conn = get_connection()
        try:
            answered_at = conn.execute("SELECT answered_at FROM asks WHERE id = ?", (ask,)).fetchone()[0]
        finally:
            conn.close()
        self._checked_in(act, since=answered_at)

        assert build_neighbor_ask_lines(self.SESSION)[1] == [ask]

    def test_answered_before_check_in_is_excluded(self, temp_db, hook_state_dir):
        act = _act("work")
        ask = _ask("q", act)
        ak.answer_ask(ask, "a")
        self._checked_in(act, since="2999-01-01 00:00:00")

        assert build_neighbor_ask_lines(self.SESSION) == ([], [])

    def test_already_notified_ask_is_excluded(self, temp_db, hook_state_dir):
        act = _act("work")
        ask = _ask("q", act)
        ak.answer_ask(ask, "a")
        state = self._checked_in(act)
        state.add_notified_ask_ids([ask])

        assert build_neighbor_ask_lines(self.SESSION) == ([], [])

    def test_own_tracked_ask_consumed_by_own_lines_is_not_repeated(self, temp_db, hook_state_dir):
        act = _act("work")
        ask = _ask("q", act)
        ak.answer_ask(ask, "a")
        state = self._checked_in(act)
        state.add_tracked_ask_ids([ask])

        assert build_ask_notify_lines(self.SESSION) != []
        assert build_neighbor_ask_lines(self.SESSION) == ([], [])

    def test_no_check_in_state_returns_empty(self, temp_db, hook_state_dir):
        assert build_neighbor_ask_lines(self.SESSION) == ([], [])
