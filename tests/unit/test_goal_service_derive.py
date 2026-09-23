"""goal_service（読み出し側）の単体テスト。

束縛先の状態の3値判定（activity/decision/ask、decisionの生きた置き換え）、
条件フラグ（reopened/broken/bound_done/recheck）、次の一手の選択規則14本、
判定待ちの未決（open_questions）、goalブロックの組み立て、get_goal、
書き込み系3ツールの応答へのgoalブロック添付を検証する。
"""
import time

from src.db import get_connection
from src.services import ask_service as ak
from src.services import goal_service as gs
from src.services import relation_service
from src.services.activity_service import add_activity, update_activity
from src.services.checkin_service import check_in
from src.services.decision_service import add_decisions
from src.services.topic_service import add_topic


def _activity(title: str = "a1") -> int:
    return add_activity(title=title, description="d", tags=["domain:test"], check_in=False)[
        "activity_id"
    ]


def _decision(title: str = "決定", topic_id: int | None = None) -> int:
    if topic_id is None:
        topic_id = add_topic(title=f"topic-{title}-{time.time()}", description="d", tags=["domain:test"])[
            "topic_id"
        ]
    result = add_decisions(
        [{"topic_id": topic_id, "decision": title, "reason": "理由", "tags": ["domain:test"]}]
    )
    return result["created"][0]["decision_id"]


def _ask(activity_id: int, question: str = "問い") -> int:
    return ak.add_ask(question, tags=["domain:test"], blocks=[activity_id], notify=False)["id"]


def _retract_decision(decision_id: int) -> None:
    conn = get_connection()
    try:
        conn.execute("UPDATE decisions SET retracted_at = CURRENT_TIMESTAMP WHERE id = ?", (decision_id,))
        conn.commit()
    finally:
        conn.close()


def _replace_decision(old_id: int, new_id: int) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO decision_supersedes (source_id, target_id, kind) VALUES (?, ?, 'replaces')",
            (new_id, old_id),
        )
        conn.commit()
    finally:
        conn.close()


def _new_goal(activity_id: int, handle: str = "g1", conditions=None, statement: str = "終わりの一文"):
    conditions = conditions or [{"statement": "条件1", "actor": "claude"}]
    return gs.set_goal(
        activity_id,
        {"new": {"handle": handle, "statement": statement, "conditions": conditions}},
    )


def _condition_ids(goal_id: int) -> list[int]:
    conn = get_connection()
    try:
        return [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM goal_conditions WHERE goal_id = ? ORDER BY id", (goal_id,)
            ).fetchall()
        ]
    finally:
        conn.close()


def _set_condition_updated_at(condition_id: int, sql_offset: str) -> None:
    """recheckフラグのテスト用に、updated_atを過去に書き換える（時刻は外部境界としてDBを直接操作）。"""
    conn = get_connection()
    try:
        conn.execute(
            f"UPDATE goal_conditions SET updated_at = datetime('now', ?) WHERE id = ?",
            (sql_offset, condition_id),
        )
        conn.commit()
    finally:
        conn.close()


class _CountingConn:
    """呼び出し回数だけを数え、実処理は素通しするspyラッパー（外部境界ではなく
    リソース契約＝N+1回避を検証するために使う。テスト規約§1-6が許容する例外）。
    """

    def __init__(self, real):
        self._real = real
        self.count = 0

    def execute(self, *args, **kwargs):
        self.count += 1
        return self._real.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


class TestBoundStatesActivity:
    def test_done_pending_gone(self, temp_db):
        done_act = _activity("done")
        update_activity(done_act, status="completed")
        pending_act = _activity("pending")
        conn = get_connection()
        try:
            states = gs._fetch_bound_states(
                conn, [("activity", done_act), ("activity", pending_act), ("activity", 999999)]
            )
        finally:
            conn.close()
        assert states[("activity", done_act)]["state"] == "done"
        assert states[("activity", pending_act)]["state"] == "pending"
        assert states[("activity", 999999)]["state"] == "gone"
        assert states[("activity", 999999)]["reason"] == "missing"

    def test_all_four_non_completed_statuses_map_to_pending(self, temp_db):
        """activityの5値（pending/in_progress/snoozed/shelved/completed）のうち、
        completed以外の4値はいずれも束縛先の状態としては「pending」になる。"""
        acts = {}
        for status in ("pending", "in_progress", "snoozed", "shelved"):
            act = _activity(status)
            if status != "pending":
                update_activity(act, status=status)
            acts[status] = act
        conn = get_connection()
        try:
            states = gs._fetch_bound_states(conn, [("activity", a) for a in acts.values()])
        finally:
            conn.close()
        for status, act in acts.items():
            assert states[("activity", act)]["state"] == "pending", status


class TestBoundStatesAsk:
    def test_five_states(self, temp_db):
        act = _activity()
        open_ask = _ask(act, "open?")
        answered_ask = _ask(act, "answered?")
        ak.answer_ask(answered_ask, "回答")
        promoted_ask = _ask(act, "promoted?")
        ak.answer_ask(promoted_ask, "回答")
        ak.triage_ask(promoted_ask, "promote", decision="採用", reason="理由")
        dismissed_ask = _ask(act, "dismissed?")
        ak.answer_ask(dismissed_ask, "回答")
        ak.triage_ask(dismissed_ask, "dismiss", dismiss_reason="不要")
        withdrawn_ask = _ask(act, "withdrawn?")
        ak.withdraw_ask(withdrawn_ask, "取り下げ")

        conn = get_connection()
        try:
            states = gs._fetch_bound_states(
                conn,
                [
                    ("ask", open_ask),
                    ("ask", answered_ask),
                    ("ask", promoted_ask),
                    ("ask", dismissed_ask),
                    ("ask", withdrawn_ask),
                ],
            )
        finally:
            conn.close()
        assert states[("ask", open_ask)]["state"] == "pending"
        assert states[("ask", answered_ask)]["state"] == "done"
        assert states[("ask", promoted_ask)]["state"] == "done"
        assert states[("ask", dismissed_ask)]["state"] == "done"
        assert states[("ask", withdrawn_ask)]["state"] == "gone"
        assert states[("ask", withdrawn_ask)]["reason"] == "withdrawn"


class TestBoundStatesDecision:
    def test_normal_decision_done_retracted_replaced(self, temp_db):
        normal_done = _decision("通常-済")
        normal_retracted = _decision("通常-撤回")
        _retract_decision(normal_retracted)
        normal_replaced_old = _decision("通常-旧")
        normal_replaced_new = _decision("通常-新")
        _replace_decision(normal_replaced_old, normal_replaced_new)

        conn = get_connection()
        try:
            states = gs._fetch_bound_states(
                conn,
                [
                    ("decision", normal_done),
                    ("decision", normal_retracted),
                    ("decision", normal_replaced_old),
                ],
            )
        finally:
            conn.close()
        assert states[("decision", normal_done)]["state"] == "done"
        assert states[("decision", normal_retracted)]["state"] == "gone"
        assert states[("decision", normal_retracted)]["reason"] == "retracted"
        # 通常decisionは生きた置き換えがあっても「済」ではなく「崩れ」として出る
        assert states[("decision", normal_replaced_old)]["state"] == "gone"
        assert states[("decision", normal_replaced_old)]["reason"] == "replaced"
        assert states[("decision", normal_replaced_old)]["successor_title"] == "通常-新"

    def test_open_question_decision_pending_done_reverts(self, temp_db):
        # [議論中] decision: 未決着ならpending、結論から生きた置き換えを受けたらdone
        question = _decision("[議論中] キャッシュ方針は？")
        conclusion = _decision("キャッシュはLRUで持つ")
        conn = get_connection()
        try:
            pending_state = gs._fetch_bound_states(conn, [("decision", question)])
        finally:
            conn.close()
        assert pending_state[("decision", question)]["state"] == "pending"

        _replace_decision(question, conclusion)
        conn = get_connection()
        try:
            done_state = gs._fetch_bound_states(conn, [("decision", question)])
        finally:
            conn.close()
        assert done_state[("decision", question)]["state"] == "done"

        # 後継が撤回されたら、[議論中] decisionは未に戻る
        _retract_decision(conclusion)
        conn = get_connection()
        try:
            reverted_state = gs._fetch_bound_states(conn, [("decision", question)])
        finally:
            conn.close()
        assert reverted_state[("decision", question)]["state"] == "pending"

    def test_open_question_decision_retracted_is_gone(self, temp_db):
        question = _decision("[議論中] 撤回される問い")
        _retract_decision(question)
        conn = get_connection()
        try:
            state = gs._fetch_bound_states(conn, [("decision", question)])
        finally:
            conn.close()
        assert state[("decision", question)]["state"] == "gone"
        assert state[("decision", question)]["reason"] == "retracted"

    def test_replacement_edge_only_from_retracted_decision_is_pending(self, temp_db):
        """置き換えの辺が撤回済みのdecisionからしか無い[議論中]decisionは未である。"""
        question = _decision("[議論中] 決着待ちの問い")
        would_be_conclusion = _decision("結論案")
        _replace_decision(question, would_be_conclusion)
        _retract_decision(would_be_conclusion)
        conn = get_connection()
        try:
            state = gs._fetch_bound_states(conn, [("decision", question)])
        finally:
            conn.close()
        assert state[("decision", question)]["state"] == "pending"

    def test_missing_decision_is_gone(self, temp_db):
        conn = get_connection()
        try:
            state = gs._fetch_bound_states(conn, [("decision", 999999)])
        finally:
            conn.close()
        assert state[("decision", 999999)]["state"] == "gone"
        assert state[("decision", 999999)]["reason"] == "missing"


class TestBoundStatesBatching:
    def test_query_count_does_not_scale_with_condition_count(self, temp_db):
        """goalごとに束縛先の型あたり1本の問い合わせで読む（条件数に比例しない）。"""
        d1 = _decision("d1")
        conn = get_connection()
        try:
            counting = _CountingConn(conn)
            gs._fetch_bound_states(counting, [("decision", d1)])
            count_for_one = counting.count
        finally:
            conn.close()

        d2, d3, d4 = _decision("d2"), _decision("d3"), _decision("d4")
        conn = get_connection()
        try:
            counting = _CountingConn(conn)
            gs._fetch_bound_states(counting, [("decision", d1), ("decision", d2), ("decision", d3), ("decision", d4)])
            count_for_many = counting.count
        finally:
            conn.close()

        assert count_for_one == count_for_many


class TestConditionFlagsBrokenScope:
    def test_activity_bound_satisfied_condition_not_broken_when_child_reopened(self, temp_db):
        """子activityをin_progressに戻しても、activity束縛の充足済み条件は崩れにならない。"""
        parent = _activity("parent")
        child = _activity("child")
        update_activity(child, status="completed")
        goal_id = _new_goal(
            parent,
            conditions=[
                {
                    "statement": "子が終わる",
                    "actor": "claude",
                    "state": "satisfied",
                    "note": "済",
                    "bound": {"type": "activity", "id": child},
                }
            ],
        )["goal_id_raw"]

        check_in(child)  # completed→in_progressへ戻す（既存の挙動）

        conn = get_connection()
        try:
            block = gs.build_goal_block_by_goal_id(conn, goal_id)
        finally:
            conn.close()
        # 全条件が終端のままなので判定待ちになり、崩れは無い（terminalに現れる）
        assert block["label"] == "judge_ready"
        entry = next(c for c in block["terminal"] if c["statement"] == "子が終わる")
        assert "broken" not in entry.get("flags", [])
        assert "actor" not in entry
        assert "flags" not in entry
        assert block["next"]["rule"] == 8

    def test_open_condition_gone_binding_is_broken_for_any_type(self, temp_db):
        act = _activity()
        ask_id = _ask(act, "消える問い")
        ak.withdraw_ask(ask_id, "取り下げ")
        goal_id = _new_goal(
            act,
            conditions=[
                {"statement": "askで決まる", "actor": "human", "bound": {"type": "ask", "id": ask_id}}
            ],
        )["goal_id_raw"]
        conn = get_connection()
        try:
            block = gs.build_goal_block_by_goal_id(conn, goal_id)
        finally:
            conn.close()
        remaining = {c["statement"]: c for c in block["remaining"]}
        assert "broken" in remaining["askで決まる"]["flags"]

    def test_satisfied_decision_binding_retracted_is_broken(self, temp_db):
        act = _activity()
        decision_id = _decision("根拠")
        goal_id = _new_goal(
            act,
            conditions=[
                {
                    "statement": "根拠が決まる",
                    "actor": "claude",
                    "state": "satisfied",
                    "note": "済",
                    "bound": {"type": "decision", "id": decision_id},
                }
            ],
        )["goal_id_raw"]
        _retract_decision(decision_id)
        conn = get_connection()
        try:
            block = gs.build_goal_block_by_goal_id(conn, goal_id)
        finally:
            conn.close()
        remaining = {c["statement"]: c for c in block["remaining"]}
        assert "broken" in remaining["根拠が決まる"]["flags"]
        assert block["next"]["rule"] == 6


class TestNextRulesActivityScope:
    def test_rule1_answer_waiting_ask_blocks_even_without_goal(self, temp_db):
        act = _activity()
        _ask(act, "答えて")
        conn = get_connection()
        try:
            block = gs.build_goal_block_for_activity(conn, act)
        finally:
            conn.close()
        assert block["label"] == "undefined"
        assert block["next"]["rule"] == 1
        assert "答えて" in block["next"]["what"]

    def test_rule2_triage_waiting_ask(self, temp_db):
        act = _activity()
        ask_id = _ask(act, "振り分けて")
        ak.answer_ask(ask_id, "回答")
        conn = get_connection()
        try:
            block = gs.build_goal_block_for_activity(conn, act)
        finally:
            conn.close()
        assert block["next"]["rule"] == 2

    def test_rule1_2_apply_even_with_waiver(self, temp_db):
        """不要印のactivityでもブロックしているaskがあれば規則1・2が返る。"""
        act = _activity()
        _ask(act, "答えて")
        gs.set_goal(act, {"waiver": "常駐"})
        conn = get_connection()
        try:
            block = gs.build_goal_block_for_activity(conn, act)
        finally:
            conn.close()
        assert block["label"] == "not_needed"
        assert block["next"]["rule"] == 1

    def test_rule1_2_do_not_apply_to_completed_activity(self, temp_db):
        """completedのactivityでは規則1・2が一致しない。"""
        act = _activity()
        _ask(act, "答えて")
        update_activity(act, status="completed")
        conn = get_connection()
        try:
            block = gs.build_goal_block_for_activity(conn, act)
        finally:
            conn.close()
        assert block["next"]["rule"] == 4

    def test_rule3_not_needed_without_blocking_ask(self, temp_db):
        act = _activity()
        gs.set_goal(act, {"waiver": "常駐タスク"})
        conn = get_connection()
        try:
            block = gs.build_goal_block_for_activity(conn, act)
        finally:
            conn.close()
        assert block["label"] == "not_needed"
        assert block["reason"] == "常駐タスク"
        assert "next" not in block

    def test_rule4_undefined(self, temp_db):
        act = _activity()
        conn = get_connection()
        try:
            block = gs.build_goal_block_for_activity(conn, act)
        finally:
            conn.close()
        assert block["label"] == "undefined"
        assert block["next"]["rule"] == 4


class TestRule5Judged:
    def test_activity_scope_includes_close_instruction_goal_scope_does_not(self, temp_db):
        """goal_idかhandleで指したときの規則5の文面には、このactivityの閉じ直しを含めない。"""
        act = _activity()
        goal_id = _new_goal(
            act, conditions=[{"statement": "c1", "actor": "claude", "state": "satisfied", "note": "済"}]
        )["goal_id_raw"]
        gs.judge_goal(goal_id, "achieved")
        conn = get_connection()
        try:
            activity_scoped = gs.build_goal_block_for_activity(conn, act)
            goal_scoped = gs.build_goal_block_by_goal_id(conn, goal_id)
        finally:
            conn.close()
        assert activity_scoped["next"]["rule"] == 5
        assert "update_activity" in activity_scoped["next"]["what"]
        assert goal_scoped["next"]["rule"] == 5
        assert "update_activity" not in goal_scoped["next"]["what"]


class TestRule7Reopened:
    def test_reopened_claude_condition(self, temp_db):
        act = _activity()
        goal_id = _new_goal(
            act,
            conditions=[
                {"statement": "c1", "actor": "claude", "state": "satisfied", "note": "済"},
                {"statement": "c2", "actor": "claude"},
            ],
        )["goal_id_raw"]
        cond_ids = _condition_ids(goal_id)
        gs.update_goal(goal_id, changes=[{"op": "set", "id": cond_ids[0], "state": "open", "note": "やり直し"}])
        conn = get_connection()
        try:
            block = gs.build_goal_block_by_goal_id(conn, goal_id)
        finally:
            conn.close()
        assert block["next"]["rule"] == 7
        assert block["next"]["condition_id_raw"] == cond_ids[0]


class TestRule8And9:
    def test_rule8_achieved_ready(self, temp_db):
        act = _activity()
        goal_id = _new_goal(
            act, conditions=[{"statement": "c1", "actor": "claude", "state": "satisfied", "note": "済"}]
        )["goal_id_raw"]
        conn = get_connection()
        try:
            block = gs.build_goal_block_by_goal_id(conn, goal_id)
        finally:
            conn.close()
        assert block["label"] == "judge_ready"
        assert block["next"]["rule"] == 8

    def test_rule9_nothing_satisfied(self, temp_db):
        act = _activity()
        goal_id = _new_goal(
            act, conditions=[{"statement": "c1", "actor": "claude", "state": "waived", "note": "不要になった"}]
        )["goal_id_raw"]
        conn = get_connection()
        try:
            block = gs.build_goal_block_by_goal_id(conn, goal_id)
        finally:
            conn.close()
        assert block["next"]["rule"] == 9


class TestRule10BoundDone:
    def test_open_condition_with_done_binding(self, temp_db):
        act = _activity()
        other = _activity("other")
        update_activity(other, status="completed")
        goal_id = _new_goal(
            act,
            conditions=[{"statement": "otherが終わる", "actor": "human", "bound": {"type": "activity", "id": other}}],
        )["goal_id_raw"]
        conn = get_connection()
        try:
            block = gs.build_goal_block_by_goal_id(conn, goal_id)
        finally:
            conn.close()
        assert block["next"]["rule"] == 10


class TestRule11ClaudeTurn:
    def test_plain_claude_condition(self, temp_db):
        act = _activity()
        goal_id = _new_goal(act, conditions=[{"statement": "PRを出す", "actor": "claude"}])["goal_id_raw"]
        conn = get_connection()
        try:
            block = gs.build_goal_block_by_goal_id(conn, goal_id)
        finally:
            conn.close()
        assert block["next"]["rule"] == 11
        assert block["next"]["what"] == "PRを出す"

    def test_activity_bound_claude_condition_shows_activity_title(self, temp_db):
        act = _activity()
        child = _activity("子作業")
        goal_id = _new_goal(
            act,
            conditions=[{"statement": "子作業を終える", "actor": "claude", "bound": {"type": "activity", "id": child}}],
        )["goal_id_raw"]
        conn = get_connection()
        try:
            block = gs.build_goal_block_by_goal_id(conn, goal_id)
        finally:
            conn.close()
        assert block["next"]["rule"] == 11
        assert block["next"]["what"] == "activity『子作業』を進める"


class TestRule12NoStopLine:
    def test_only_human_conditions(self, temp_db):
        act = _activity()
        goal_id = _new_goal(act, conditions=[{"statement": "人間が決める", "actor": "human"}])["goal_id_raw"]
        conn = get_connection()
        try:
            block = gs.build_goal_block_by_goal_id(conn, goal_id)
        finally:
            conn.close()
        assert block["next"]["rule"] == 12


class TestRule13And14:
    """規則13・14は、担い手claudeの条件が総数1件以上あって初めて評価対象になる
    （0件なら規則12の停止線の不在が先に一致するため、各テストで停止線を1本置く）。
    """

    def test_rule14_recent_wait(self, temp_db):
        act = _activity()
        goal_id = _new_goal(
            act,
            conditions=[
                {"statement": "停止線", "actor": "claude", "state": "satisfied", "note": "済"},
                {"statement": "レビュー待ち", "actor": "human"},
            ],
        )["goal_id_raw"]
        conn = get_connection()
        try:
            block = gs.build_goal_block_by_goal_id(conn, goal_id)
        finally:
            conn.close()
        assert block["next"]["rule"] == 14
        assert block["next"]["actor"] == "human"

    def test_rule13_recheck_after_threshold(self, temp_db, monkeypatch):
        monkeypatch.setattr(gs, "GOAL_RECHECK_HOURS", 0)
        act = _activity()
        goal_id = _new_goal(
            act,
            conditions=[
                {"statement": "停止線", "actor": "claude", "state": "satisfied", "note": "済"},
                {"statement": "マージ待ち", "actor": "human"},
            ],
        )["goal_id_raw"]
        _, human_cond_id = _condition_ids(goal_id)
        _set_condition_updated_at(human_cond_id, "-1 hours")
        conn = get_connection()
        try:
            block = gs.build_goal_block_by_goal_id(conn, goal_id)
        finally:
            conn.close()
        assert block["next"]["rule"] == 13
        recheck_entry = next(c for c in block["remaining"] if c["statement"] == "マージ待ち")
        assert "recheck" in recheck_entry["flags"]


class TestOpenQuestions:
    def test_open_ask_and_pending_open_question_decision_appear(self, temp_db):
        act = _activity()
        goal_id = _new_goal(
            act, conditions=[{"statement": "c1", "actor": "claude", "state": "satisfied", "note": "済"}]
        )["goal_id_raw"]
        _ask(act, "残ってる問い")
        question_decision = _decision("[議論中] 未決着の論点")
        relation_service.add_relation("activity", act, [{"type": "decision", "ids": [question_decision]}])

        conn = get_connection()
        try:
            block = gs.build_goal_block_by_goal_id(conn, goal_id)
        finally:
            conn.close()
        assert block["label"] == "judge_ready"
        types_titles = {(q["type"], q["title"]) for q in block["open_questions"]}
        assert ("ask", "残ってる問い") in types_titles
        assert ("decision", "[議論中] 未決着の論点") in types_titles

    def test_resolved_question_does_not_appear(self, temp_db):
        act = _activity()
        goal_id = _new_goal(
            act, conditions=[{"statement": "c1", "actor": "claude", "state": "satisfied", "note": "済"}]
        )["goal_id_raw"]
        answered_ask = _ask(act, "決着済みの問い")
        ak.answer_ask(answered_ask, "回答")
        question_decision = _decision("[議論中] 決着した論点")
        conclusion = _decision("結論")
        _replace_decision(question_decision, conclusion)
        relation_service.add_relation("activity", act, [{"type": "decision", "ids": [question_decision]}])

        conn = get_connection()
        try:
            block = gs.build_goal_block_by_goal_id(conn, goal_id)
        finally:
            conn.close()
        assert "open_questions" not in block

    def test_overflow_shows_count(self, temp_db):
        act = _activity()
        goal_id = _new_goal(
            act, conditions=[{"statement": "c1", "actor": "claude", "state": "satisfied", "note": "済"}]
        )["goal_id_raw"]
        for i in range(4):
            _ask(act, f"問い{i}")
        conn = get_connection()
        try:
            block = gs.build_goal_block_by_goal_id(conn, goal_id)
        finally:
            conn.close()
        assert len(block["open_questions"]) == 3
        assert block["open_questions_more"] == 1


class TestRemainingCap:
    def test_more_than_three_open_conditions_are_capped(self, temp_db):
        act = _activity()
        conditions = [{"statement": f"c{i}", "actor": "claude"} for i in range(5)]
        goal_id = _new_goal(act, conditions=conditions)["goal_id_raw"]
        conn = get_connection()
        try:
            block = gs.build_goal_block_by_goal_id(conn, goal_id)
        finally:
            conn.close()
        assert len(block["remaining"]) == 3
        assert block["others"] == "他 2 件"


class TestOtherActivities:
    def test_excludes_current_activity_and_caps_at_three(self, temp_db):
        main = _activity("main")
        goal_id = _new_goal(main, conditions=[{"statement": "c1", "actor": "claude"}])["goal_id_raw"]
        for i in range(4):
            sib = _activity(f"sib{i}")
            gs.set_goal(sib, {"goal_id": goal_id})
        conn = get_connection()
        try:
            block = gs.build_goal_block_for_activity(conn, main)
        finally:
            conn.close()
        assert len(block["other_activities"]) == 3
        assert all(item["title"] != "main" for item in block["other_activities"])

    def test_omitted_when_only_one_linked_activity(self, temp_db):
        act = _activity()
        _new_goal(act, conditions=[{"statement": "c1", "actor": "claude"}])
        conn = get_connection()
        try:
            block = gs.build_goal_block_for_activity(conn, act)
        finally:
            conn.close()
        assert "other_activities" not in block


class TestGetGoal:
    def test_requires_exactly_one_arg(self, temp_db):
        assert gs.get_goal()["error"]["code"] == "VALIDATION_ERROR"
        assert gs.get_goal(goal_id=1, handle="x")["error"]["code"] == "VALIDATION_ERROR"

    def test_not_found(self, temp_db):
        assert gs.get_goal(goal_id=999999)["error"]["code"] == "NOT_FOUND"
        assert gs.get_goal(activity_id=999999)["error"]["code"] == "NOT_FOUND"
        assert gs.get_goal(handle="no-such-handle")["error"]["code"] == "NOT_FOUND"

    def test_undefined_and_not_needed_shapes(self, temp_db):
        act = _activity()
        result = gs.get_goal(activity_id=act)
        assert result["label"] == "undefined"
        assert set(result.keys()) == {"label", "next"}
        assert result["next"]["rule"] == 4

        gs.set_goal(act, {"waiver": "理由"})
        result = gs.get_goal(activity_id=act)
        assert result["label"] == "not_needed"
        assert result["reason"] == "理由"
        assert "next" not in result

    def test_full_conditions_and_activities_with_bound(self, temp_db):
        act = _activity("メイン")
        sibling = _activity("サブ")
        decision_id = _decision("根拠")
        goal_id = _new_goal(
            act,
            conditions=[
                {
                    "statement": "c1",
                    "actor": "claude",
                    "state": "satisfied",
                    "note": "済",
                    "bound": {"type": "decision", "id": decision_id},
                },
                {"statement": "c2", "actor": "human"},
            ],
        )["goal_id_raw"]
        gs.set_goal(sibling, {"goal_id": goal_id})
        update_activity(sibling, status="completed")
        # closed_by/closed_reasonはupdate_activityが書く列で、この関数では書かない。
        # ここではget_goalの読み出し側だけを見たいので、値を直接置く。
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE activities SET closed_by = 'user', closed_reason = '別件で先に閉じた', "
                "closed_at = CURRENT_TIMESTAMP WHERE id = ?",
                (sibling,),
            )
            conn.commit()
        finally:
            conn.close()

        result = gs.get_goal(goal_id=goal_id)
        assert len(result["conditions"]) == 2
        bound_cond = next(c for c in result["conditions"] if c["statement"] == "c1")
        assert bound_cond["bound"]["type"] == "decision"
        assert bound_cond["bound"]["state"] == "done"
        assert "successor_title" not in bound_cond["bound"]

        activities_by_title = {a["title"]: a for a in result["activities"]}
        assert activities_by_title["サブ"]["closed_by"] == "user"
        assert activities_by_title["メイン"]["status"] != "completed"

    def test_rule5_wording_excludes_close_instruction_for_goal_scope(self, temp_db):
        act = _activity()
        goal_id = _new_goal(
            act, conditions=[{"statement": "c1", "actor": "claude", "state": "satisfied", "note": "済"}]
        )["goal_id_raw"]
        gs.judge_goal(goal_id, "achieved")
        by_goal = gs.get_goal(goal_id=goal_id)
        by_handle = gs.get_goal(handle=by_goal["handle"])
        by_activity = gs.get_goal(activity_id=act)
        assert "update_activity" not in by_goal["next"]["what"]
        assert "update_activity" not in by_handle["next"]["what"]
        assert "update_activity" in by_activity["next"]["what"]

    def test_judge_ready_includes_open_questions(self, temp_db):
        act = _activity()
        goal_id = _new_goal(
            act, conditions=[{"statement": "c1", "actor": "claude", "state": "satisfied", "note": "済"}]
        )["goal_id_raw"]
        _ask(act, "残ってる問い")

        by_goal = gs.get_goal(goal_id=goal_id)
        assert by_goal["label"] == "judge_ready"
        assert {q["title"] for q in by_goal["open_questions"]} == {"残ってる問い"}

        by_activity = gs.get_goal(activity_id=act)
        assert {q["title"] for q in by_activity["open_questions"]} == {"残ってる問い"}

    def test_active_label_has_no_open_questions_key(self, temp_db):
        act = _activity()
        goal_id = _new_goal(act, conditions=[{"statement": "c1", "actor": "claude"}])["goal_id_raw"]
        _ask(act, "問い")
        result = gs.get_goal(goal_id=goal_id)
        assert result["label"] == "active"
        assert "open_questions" not in result

    def test_open_questions_overflow_shows_count(self, temp_db):
        act = _activity()
        goal_id = _new_goal(
            act, conditions=[{"statement": "c1", "actor": "claude", "state": "satisfied", "note": "済"}]
        )["goal_id_raw"]
        for i in range(4):
            _ask(act, f"問い{i}")
        result = gs.get_goal(goal_id=goal_id)
        assert len(result["open_questions"]) == 3
        assert result["open_questions_more"] == 1


class TestGoalBlockOnWriteTools:
    def test_set_goal_success_attaches_goal_block(self, temp_db):
        act = _activity()
        result = _new_goal(act)
        assert "goal" in result
        assert result["goal"]["label"] in ("active", "judge_ready")

    def test_set_goal_activity_goal_exists_has_no_goal_block(self, temp_db):
        act = _activity()
        _new_goal(act, handle="first")
        result = _new_goal(act, handle="second")
        assert result["info"] == "ACTIVITY_GOAL_EXISTS"
        assert "goal" not in result

    def test_set_goal_goal_closed_returns_target_goal_block(self, temp_db):
        act1 = _activity("a1")
        goal_id = _new_goal(
            act1,
            handle="closed-target",
            conditions=[{"statement": "c1", "actor": "claude", "state": "satisfied", "note": "済"}],
        )["goal_id_raw"]
        gs.judge_goal(goal_id, "achieved")
        act2 = _activity("a2")
        result = gs.set_goal(act2, {"goal_id": goal_id})
        assert result["info"] == "GOAL_CLOSED"
        assert result["goal"]["label"] == "closed"
        assert result["goal"]["goal_id_raw"] == goal_id

    def test_update_goal_success_attaches_goal_block(self, temp_db):
        act = _activity()
        goal_id = _new_goal(act)["goal_id_raw"]
        cond_id = _condition_ids(goal_id)[0]
        result = gs.update_goal(goal_id, changes=[{"op": "set", "id": cond_id, "state": "satisfied", "note": "済"}])
        assert result["goal"]["label"] == "judge_ready"
        assert result["goal"]["next"]["rule"] == 8

    def test_update_goal_already_open_attaches_goal_block(self, temp_db):
        act = _activity()
        goal_id = _new_goal(act)["goal_id_raw"]
        result = gs.update_goal(goal_id, reopen_reason="理由")
        assert result["info"] == "GOAL_ALREADY_OPEN"
        assert result["goal"]["goal_id_raw"] == goal_id

    def test_judge_goal_success_attaches_closed_goal_block(self, temp_db):
        act = _activity()
        goal_id = _new_goal(
            act, conditions=[{"statement": "c1", "actor": "claude", "state": "satisfied", "note": "済"}]
        )["goal_id_raw"]
        result = gs.judge_goal(goal_id, "achieved")
        assert result["goal"]["label"] == "closed"
        assert result["goal"]["last_verdict"]["verdict"] == "achieved"

    def test_judge_goal_already_closed_attaches_goal_block(self, temp_db):
        act = _activity()
        goal_id = _new_goal(act)["goal_id_raw"]
        gs.judge_goal(goal_id, "failed", note="取り下げ")
        result = gs.judge_goal(goal_id, "failed", note="再度")
        assert result["info"] == "GOAL_ALREADY_CLOSED"
        assert result["goal"]["label"] == "closed"

    def test_judge_goal_error_has_no_goal_block(self, temp_db):
        act = _activity()
        goal_id = _new_goal(act)["goal_id_raw"]
        result = gs.judge_goal(goal_id, "achieved")
        assert result["error"]["code"] == "GOAL_NOT_READY"
        assert "goal" not in result
