"""goal_service（set_goal・update_goal・judge_goal）の単体テスト。

書き込み側のみを対象とする（ラベル・次の一手・goal ブロックの導出はderive系テストで扱う）。
情報応答（ACTIVITY_GOAL_EXISTS・GOAL_CLOSED・GOAL_ALREADY_CLOSED・
GOAL_ALREADY_OPEN）・エラー（VALIDATION_ERROR・NOT_FOUND・HANDLE_TAKEN・
GOAL_WOULD_ORPHAN・GOAL_NOT_READY・GOAL_NOTHING_SATISFIED・
GOAL_BINDING_BROKEN・DATABASE_ERROR）、トランザクションの原子性、差し戻しの
signal 記録、並行書き込みの直列化を検証する。
"""
import threading
import time

from src.db import get_connection
from src.services import ask_service as ak
from src.services import goal_service as gs
from src.services.activity_service import add_activity, update_activity
from src.services.decision_service import add_decisions
from src.services.topic_service import add_topic


def _activity(title: str = "a1") -> int:
    return add_activity(title=title, description="d", tags=["domain:test"], check_in=False)[
        "activity_id"
    ]


def _decision(title: str = "決定") -> int:
    topic_id = add_topic(title=f"topic-{title}", description="d", tags=["domain:test"])["topic_id"]
    result = add_decisions(
        [{"topic_id": topic_id, "decision": title, "reason": "理由", "tags": ["domain:test"]}]
    )
    return result["created"][0]["decision_id"]


def _ask(activity_id: int) -> int:
    result = ak.add_ask("問い", tags=["domain:test"], blocks=[activity_id], notify=False)
    return result["id"]


def _retract_decision(decision_id: int) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE decisions SET retracted_at = CURRENT_TIMESTAMP WHERE id = ?", (decision_id,)
        )
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


def _condition_row(condition_id: int):
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT * FROM goal_conditions WHERE id = ?", (condition_id,)
        ).fetchone()
    finally:
        conn.close()


def _goal_row(goal_id: int):
    conn = get_connection()
    try:
        return conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
    finally:
        conn.close()


def _activity_row(activity_id: int):
    conn = get_connection()
    try:
        return conn.execute("SELECT * FROM activities WHERE id = ?", (activity_id,)).fetchone()
    finally:
        conn.close()


def _linked_activity_count(goal_id: int) -> int:
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM goal_activities WHERE goal_id = ?", (goal_id,)
        ).fetchone()["c"]
    finally:
        conn.close()


def conn_first_condition_id(goal_id: int) -> int:
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT id FROM goal_conditions WHERE goal_id = ? ORDER BY id LIMIT 1", (goal_id,)
        ).fetchone()["id"]
    finally:
        conn.close()


class TestSetGoalForms:
    def test_new_goal_created_and_linked(self, temp_db):
        act = _activity()
        result = _new_goal(act, handle="new-form")
        assert "error" not in result
        assert result["goal_id_raw"] and result["activity_id_raw"] == act
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT goal_id FROM goal_activities WHERE activity_id = ?", (act,)
            ).fetchone()
        finally:
            conn.close()
        assert row["goal_id"] == result["goal_id_raw"]

    def test_link_to_existing_goal(self, temp_db):
        act1 = _activity("a1")
        act2 = _activity("a2")
        goal_id = _new_goal(act1, handle="shared")["goal_id_raw"]
        result = gs.set_goal(act2, {"goal_id": goal_id})
        assert "error" not in result
        assert result["goal_id_raw"] == goal_id

    def test_waiver(self, temp_db):
        act = _activity()
        result = gs.set_goal(act, {"waiver": "常駐タスクのため"})
        assert "error" not in result
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT goal_id, waiver_reason FROM goal_activities WHERE activity_id = ?", (act,)
            ).fetchone()
        finally:
            conn.close()
        assert row["goal_id"] is None
        assert row["waiver_reason"] == "常駐タスクのため"

    def test_none_removes_row(self, temp_db):
        act = _activity()
        gs.set_goal(act, {"waiver": "理由"})
        result = gs.set_goal(act, None, replace=True)
        assert "error" not in result
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT 1 FROM goal_activities WHERE activity_id = ?", (act,)
            ).fetchone()
        finally:
            conn.close()
        assert row is None

    def test_none_without_replace_is_rejected_as_activity_goal_exists(self, temp_db):
        """Noneの形で既存の行を外すにはreplace=trueが要る。無指定ではACTIVITY_GOAL_EXISTSで
        止まり、行はそのまま残る。"""
        act = _activity()
        gs.set_goal(act, {"waiver": "理由"})
        result = gs.set_goal(act, None)
        assert result["info"] == "ACTIVITY_GOAL_EXISTS"
        assert result["current"]["waiver"] == "理由"
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT waiver_reason FROM goal_activities WHERE activity_id = ?", (act,)
            ).fetchone()
        finally:
            conn.close()
        assert row["waiver_reason"] == "理由"


class TestSetGoalExistsAndReplace:
    def test_activity_goal_exists_without_replace(self, temp_db):
        act = _activity()
        gs.set_goal(act, {"waiver": "理由A"})
        result = gs.set_goal(act, {"waiver": "理由B"})
        assert result["info"] == "ACTIVITY_GOAL_EXISTS"
        assert result["current"]["waiver"] == "理由A"
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT waiver_reason FROM goal_activities WHERE activity_id = ?", (act,)
            ).fetchone()
        finally:
            conn.close()
        assert row["waiver_reason"] == "理由A"

    def test_replace_overwrites(self, temp_db):
        act = _activity()
        gs.set_goal(act, {"waiver": "理由A"})
        conn = get_connection()
        try:
            # added_atを過去に書き換え、replace後の値と区別できるようにする
            # （時刻は外部境界としてDBを直接操作。テスト規約が許容する例外）。
            conn.execute(
                "UPDATE goal_activities SET added_at = datetime('now', '-1 day') WHERE activity_id = ?",
                (act,),
            )
            conn.commit()
            backdated_added_at = conn.execute(
                "SELECT added_at FROM goal_activities WHERE activity_id = ?", (act,)
            ).fetchone()["added_at"]
        finally:
            conn.close()

        result = gs.set_goal(act, {"waiver": "理由B"}, replace=True)
        assert "error" not in result
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT waiver_reason, added_at FROM goal_activities WHERE activity_id = ?", (act,)
            ).fetchone()
        finally:
            conn.close()
        assert row["waiver_reason"] == "理由B"
        assert row["added_at"] != backdated_added_at

    def test_relink_to_same_goal_is_noop_success(self, temp_db):
        act = _activity()
        goal_id = _new_goal(act, handle="idempotent")["goal_id_raw"]
        result = gs.set_goal(act, {"goal_id": goal_id})
        assert "error" not in result
        assert "info" not in result
        assert result["goal_id_raw"] == goal_id

    def test_relink_to_closed_goal_is_noop_success_not_goal_closed(self, temp_db):
        """同じ closed goal への再度の紐づけは、GOAL_CLOSED より先に成功で返る。"""
        act = _activity()
        goal_id = _new_goal(act, handle="closed-relink", conditions=[
            {"statement": "c1", "actor": "claude", "state": "satisfied", "note": "済"}
        ])["goal_id_raw"]
        judge = gs.judge_goal(goal_id, "achieved")
        assert "error" not in judge

        result = gs.set_goal(act, {"goal_id": goal_id})
        assert "error" not in result
        assert "info" not in result

    def test_new_resend_same_handle_is_noop_success(self, temp_db):
        """set_goal(new)の再送（同じhandle）は、handleが重複扱いにならず何もせずに成功する。"""
        act = _activity()
        first = _new_goal(act, handle="resend-new", conditions=[{"statement": "c1", "actor": "claude"}])
        goal_id = first["goal_id_raw"]

        second = gs.set_goal(
            act,
            {"new": {
                "handle": "resend-new",
                "statement": "違う一文",
                "conditions": [{"statement": "c2", "actor": "claude"}],
            }},
        )
        assert "error" not in second
        assert "info" not in second
        assert second["goal_id_raw"] == goal_id

        conn = get_connection()
        try:
            goal_count = conn.execute("SELECT COUNT(*) AS c FROM goals").fetchone()["c"]
            statement = conn.execute(
                "SELECT statement FROM goals WHERE id = ?", (goal_id,)
            ).fetchone()["statement"]
            condition_statements = {
                r["statement"]
                for r in conn.execute(
                    "SELECT statement FROM goal_conditions WHERE goal_id = ?", (goal_id,)
                ).fetchall()
            }
        finally:
            conn.close()
        assert goal_count == 1
        assert statement == "終わりの一文"
        assert condition_statements == {"c1"}


class TestSetGoalOrphanAndClosed:
    def test_goal_would_orphan(self, temp_db):
        act = _activity()
        goal_id = _new_goal(act, handle="orphan-guard")["goal_id_raw"]
        result = gs.set_goal(act, None, replace=True)
        assert result["error"]["code"] == "GOAL_WOULD_ORPHAN"
        assert _linked_activity_count(goal_id) == 1

    def test_orphan_not_triggered_when_sibling_exists(self, temp_db):
        act1 = _activity("a1")
        act2 = _activity("a2")
        goal_id = _new_goal(act1, handle="has-sibling")["goal_id_raw"]
        gs.set_goal(act2, {"goal_id": goal_id})
        result = gs.set_goal(act1, None, replace=True)
        assert "error" not in result

    def test_link_to_closed_goal_returns_goal_closed(self, temp_db):
        act1 = _activity("a1")
        goal_id = _new_goal(act1, handle="closed-target", conditions=[
            {"statement": "c1", "actor": "claude", "state": "satisfied", "note": "済"}
        ])["goal_id_raw"]
        gs.judge_goal(goal_id, "achieved")

        act2 = _activity("a2")
        result = gs.set_goal(act2, {"goal_id": goal_id})
        assert result["info"] == "GOAL_CLOSED"

    def test_unlink_from_closed_goal_returns_goal_closed(self, temp_db):
        act1 = _activity("a1")
        act2 = _activity("a2")
        goal_id = _new_goal(act1, handle="closed-source", conditions=[
            {"statement": "c1", "actor": "claude", "state": "satisfied", "note": "済"}
        ])["goal_id_raw"]
        gs.set_goal(act2, {"goal_id": goal_id})
        gs.judge_goal(goal_id, "achieved")

        result = gs.set_goal(act2, None, replace=True)
        assert result["info"] == "GOAL_CLOSED"


class TestSetGoalValidation:
    def test_not_found_activity(self, temp_db):
        result = gs.set_goal(999999, {"waiver": "理由"})
        assert result["error"]["code"] == "NOT_FOUND"

    def test_not_found_bound_target(self, temp_db):
        act = _activity()
        result = _new_goal(
            act,
            conditions=[{"statement": "c1", "actor": "claude", "bound": {"type": "activity", "id": 999999}}],
        )
        assert result["error"]["code"] == "NOT_FOUND"
        conn = get_connection()
        try:
            assert conn.execute("SELECT COUNT(*) AS c FROM goals").fetchone()["c"] == 0
        finally:
            conn.close()

    def test_handle_taken(self, temp_db):
        act1 = _activity("a1")
        act2 = _activity("a2")
        _new_goal(act1, handle="dup-handle")
        result = _new_goal(act2, handle="dup-handle")
        assert result["error"]["code"] == "HANDLE_TAKEN"

    def test_zero_conditions_rejected(self, temp_db):
        act = _activity()
        result = gs.set_goal(
            act, {"new": {"handle": "no-cond", "statement": "終わり", "conditions": []}}
        )
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_handle_too_long_rejected(self, temp_db):
        act = _activity()
        result = _new_goal(act, handle="a" * 41)
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_waived_condition_without_note_rejected(self, temp_db):
        act = _activity()
        result = _new_goal(
            act, conditions=[{"statement": "c1", "actor": "claude", "state": "waived"}]
        )
        assert result["error"]["code"] == "VALIDATION_ERROR"


class TestUpdateGoalAtomicity:
    def test_error_leaves_nothing_written(self, temp_db):
        """1回の呼び出し内で1件目のopが成功し2件目が失敗したら、
        1件目の書き込みもstatementの修正も残らない（全件ロールバック）。"""
        act = _activity()
        goal_id = _new_goal(act)["goal_id_raw"]
        cond_id = conn_first_condition_id(goal_id)
        before_state = _condition_row(cond_id)["state"]
        before_statement = _goal_row(goal_id)["statement"]

        result = gs.update_goal(
            goal_id,
            changes=[
                {"op": "set", "id": cond_id, "state": "satisfied", "note": "済"},
                {"op": "set", "id": 999999, "state": "satisfied", "note": "済"},
            ],
            statement="変えたい一文",
        )

        assert result["error"]["code"] == "NOT_FOUND"
        assert _condition_row(cond_id)["state"] == before_state
        assert _goal_row(goal_id)["statement"] == before_statement

    def test_condition_rewrite_is_atomic_in_one_call(self, temp_db):
        act = _activity()
        result = _new_goal(act, conditions=[{"statement": "旧条件", "actor": "claude"}])
        goal_id = result["goal_id_raw"]
        conn = get_connection()
        try:
            old_id = conn.execute(
                "SELECT id FROM goal_conditions WHERE goal_id = ?", (goal_id,)
            ).fetchone()["id"]
        finally:
            conn.close()

        update = gs.update_goal(
            goal_id,
            changes=[
                {"op": "set", "id": old_id, "state": "waived", "note": "文言を直すため差し替え"},
                {"op": "add", "statement": "新条件", "actor": "claude"},
            ],
        )
        assert "error" not in update
        assert update["applied"] == 2
        old_row = _condition_row(old_id)
        assert old_row["state"] == "waived"
        conn = get_connection()
        try:
            new_row = conn.execute(
                "SELECT * FROM goal_conditions WHERE goal_id = ? AND statement = '新条件'", (goal_id,)
            ).fetchone()
        finally:
            conn.close()
        assert new_row is not None
        assert new_row["state"] == "open"

    def test_condition_rewrite_intermediate_state_not_visible_to_other_connection(self, temp_db, monkeypatch):
        """update_goalが旧条件のwaivedと新条件のaddを1トランザクションで書く間、
        別の接続はコミット前の途中状態（片方だけ書かれた状態）を読めない。"""
        act = _activity()
        goal_id = _new_goal(act, conditions=[{"statement": "旧条件", "actor": "claude"}])["goal_id_raw"]
        old_id = conn_first_condition_id(goal_id)

        real_get_connection = gs.get_connection

        class _DelayingConn:
            """2件目のINSERT直前にsleepを挟み、1件目の書き込み後・2件目の書き込み前の
            観測窓を作る（外部境界ではなく、テストの計測窓を作るためのラッパー。
            テスト規約§1-6が許容する例外）。1件目の直後ではなく2件目の直前で止める
            のは、1件目の完了後に何か（誤って挟まれたcommit等）が起きても、その
            直後から観測窓が始まるようにするため。"""

            def __init__(self, real):
                self._real = real
                self._triggered = False

            def execute(self, sql, *args, **kwargs):
                if not self._triggered and "INSERT INTO goal_conditions" in sql:
                    self._triggered = True
                    time.sleep(0.5)
                return self._real.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._real, name)

        def _delaying_connection():
            return _DelayingConn(real_get_connection())

        monkeypatch.setattr(gs, "get_connection", _delaying_connection)

        observed = {}

        def writer():
            observed["result"] = gs.update_goal(
                goal_id,
                changes=[
                    {"op": "set", "id": old_id, "state": "waived", "note": "文言を直すため差し替え"},
                    {"op": "add", "statement": "新条件", "actor": "claude"},
                ],
            )

        thread = threading.Thread(target=writer)
        thread.start()
        time.sleep(0.2)  # writerが1件目のUPDATEを終え、2件目の直前のsleep中に入るのを待つ

        conn = real_get_connection()
        try:
            mid_old = conn.execute(
                "SELECT state FROM goal_conditions WHERE id = ?", (old_id,)
            ).fetchone()["state"]
            mid_new = conn.execute(
                "SELECT COUNT(*) AS c FROM goal_conditions WHERE goal_id = ? AND statement = '新条件'",
                (goal_id,),
            ).fetchone()["c"]
        finally:
            conn.close()

        thread.join()
        assert "error" not in observed["result"]

        # コミット前は、旧条件のwaivedも新条件のaddも一切見えない（両方未反映のまま）。
        # 「片方だけ書かれた」途中状態が外部から読めないことを示す。
        assert mid_old == "open"
        assert mid_new == 0

        old_row = _condition_row(old_id)
        assert old_row["state"] == "waived"
        conn = get_connection()
        try:
            new_count = conn.execute(
                "SELECT COUNT(*) AS c FROM goal_conditions WHERE goal_id = ? AND statement = '新条件'",
                (goal_id,),
            ).fetchone()["c"]
        finally:
            conn.close()
        assert new_count == 1


class TestUpdateGoalStateTransitions:
    def test_same_state_set_does_not_change_last_satisfied_at(self, temp_db):
        act = _activity()
        result = _new_goal(act)
        goal_id = result["goal_id_raw"]
        cond_id = conn_first_condition_id(goal_id)
        gs.update_goal(goal_id, changes=[{"op": "set", "id": cond_id, "state": "satisfied", "note": "済"}])
        first_ts = _condition_row(cond_id)["last_satisfied_at"]
        time.sleep(1.1)
        gs.update_goal(goal_id, changes=[{"op": "set", "id": cond_id, "state": "satisfied", "note": "再確認"}])
        second_ts = _condition_row(cond_id)["last_satisfied_at"]
        assert first_ts == second_ts

    def test_open_to_open_bumps_updated_at_only(self, temp_db):
        act = _activity()
        goal_id = _new_goal(act)["goal_id_raw"]
        cond_id = conn_first_condition_id(goal_id)
        before = _condition_row(cond_id)
        time.sleep(1.1)
        gs.update_goal(goal_id, changes=[{"op": "set", "id": cond_id, "state": "open", "note": "確認済み"}])
        after = _condition_row(cond_id)
        assert before["state"] == after["state"] == "open"
        assert after["updated_at"] != before["updated_at"]
        assert after["last_satisfied_at"] == before["last_satisfied_at"] is None

    def test_write_to_closed_goal_returns_goal_closed(self, temp_db):
        act = _activity()
        goal_id = _new_goal(
            act, conditions=[{"statement": "c1", "actor": "claude", "state": "satisfied", "note": "済"}]
        )["goal_id_raw"]
        gs.judge_goal(goal_id, "achieved")
        result = gs.update_goal(goal_id, statement="変更したい")
        assert result["info"] == "GOAL_CLOSED"

    def test_set_and_edit_can_target_same_condition(self, temp_db):
        act = _activity()
        goal_id = _new_goal(act)["goal_id_raw"]
        cond_id = conn_first_condition_id(goal_id)
        result = gs.update_goal(
            goal_id,
            changes=[
                {"op": "set", "id": cond_id, "state": "satisfied", "note": "済"},
                {"op": "edit", "id": cond_id, "actor": "human"},
            ],
        )
        assert "error" not in result
        row = _condition_row(cond_id)
        assert row["state"] == "satisfied"
        assert row["actor"] == "human"

    def test_same_op_twice_on_same_condition_rejected(self, temp_db):
        act = _activity()
        goal_id = _new_goal(act)["goal_id_raw"]
        cond_id = conn_first_condition_id(goal_id)
        result = gs.update_goal(
            goal_id,
            changes=[
                {"op": "set", "id": cond_id, "state": "satisfied", "note": "済"},
                {"op": "set", "id": cond_id, "state": "open"},
            ],
        )
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_nonexistent_condition_id_not_found(self, temp_db):
        act = _activity()
        goal_id = _new_goal(act)["goal_id_raw"]
        result = gs.update_goal(
            goal_id, changes=[{"op": "set", "id": 999999, "state": "satisfied", "note": "済"}]
        )
        assert result["error"]["code"] == "NOT_FOUND"

    def test_applied_counts_changes(self, temp_db):
        act = _activity()
        goal_id = _new_goal(act)["goal_id_raw"]
        result = gs.update_goal(
            goal_id,
            changes=[
                {"op": "add", "statement": "c2", "actor": "claude"},
                {"op": "add", "statement": "c3", "actor": "human"},
            ],
        )
        assert result["applied"] == 2

    def test_edit_can_clear_bound(self, temp_db):
        act = _activity()
        ask_id = _ask(act)
        goal_id = _new_goal(
            act,
            conditions=[
                {"statement": "回答を待つ", "actor": "human", "bound": {"type": "ask", "id": ask_id}}
            ],
        )["goal_id_raw"]
        cond_id = conn_first_condition_id(goal_id)
        assert _condition_row(cond_id)["bound_type"] == "ask"

        result = gs.update_goal(goal_id, changes=[{"op": "edit", "id": cond_id, "bound": None}])
        assert "error" not in result
        row = _condition_row(cond_id)
        assert row["bound_type"] is None
        assert row["bound_id"] is None


class TestJudgeGoal:
    def test_goal_not_ready_when_open_conditions_remain(self, temp_db):
        act = _activity()
        goal_id = _new_goal(act)["goal_id_raw"]  # デフォルト open のまま
        result = gs.judge_goal(goal_id, "achieved")
        assert result["error"]["code"] == "GOAL_NOT_READY"
        assert _goal_row(goal_id)["closed"] == 0

    def test_goal_nothing_satisfied_when_all_waived(self, temp_db):
        act = _activity()
        goal_id = _new_goal(
            act, conditions=[{"statement": "c1", "actor": "claude", "state": "waived", "note": "不要"}]
        )["goal_id_raw"]
        result = gs.judge_goal(goal_id, "achieved")
        assert result["error"]["code"] == "GOAL_NOTHING_SATISFIED"

    def test_goal_binding_broken_when_satisfied_decision_retracted(self, temp_db):
        act = _activity()
        decision_id = _decision("方針")
        goal_id = _new_goal(
            act,
            conditions=[
                {
                    "statement": "方針が決まる",
                    "actor": "claude",
                    "state": "satisfied",
                    "note": "済",
                    "bound": {"type": "decision", "id": decision_id},
                }
            ],
        )["goal_id_raw"]
        _retract_decision(decision_id)
        result = gs.judge_goal(goal_id, "achieved")
        assert result["error"]["code"] == "GOAL_BINDING_BROKEN"

    def test_goal_binding_broken_when_satisfied_decision_has_living_replacement(self, temp_db):
        act = _activity()
        old_decision = _decision("旧方針")
        new_decision = _decision("新方針")
        _replace_decision(old_decision, new_decision)
        goal_id = _new_goal(
            act,
            conditions=[
                {
                    "statement": "方針が決まる",
                    "actor": "claude",
                    "state": "satisfied",
                    "note": "済",
                    "bound": {"type": "decision", "id": old_decision},
                }
            ],
        )["goal_id_raw"]
        result = gs.judge_goal(goal_id, "achieved")
        assert result["error"]["code"] == "GOAL_BINDING_BROKEN"

    def test_goal_binding_not_broken_when_decision_still_alive(self, temp_db):
        act = _activity()
        decision_id = _decision("方針2")
        goal_id = _new_goal(
            act,
            conditions=[
                {
                    "statement": "方針が決まる",
                    "actor": "claude",
                    "state": "satisfied",
                    "note": "済",
                    "bound": {"type": "decision", "id": decision_id},
                }
            ],
        )["goal_id_raw"]
        result = gs.judge_goal(goal_id, "achieved")
        assert "error" not in result

    def test_goal_binding_not_broken_when_open_question_decision_replaced(self, temp_db):
        act = _activity()
        question_id = _decision("[議論中] キャッシュ方針は？")
        _replace_decision(question_id, _decision("キャッシュはLRUで持つ"))
        goal_id = _new_goal(
            act,
            conditions=[
                {
                    "statement": "方針が決まる",
                    "actor": "claude",
                    "state": "satisfied",
                    "note": "済",
                    "bound": {"type": "decision", "id": question_id},
                }
            ],
        )["goal_id_raw"]
        result = gs.judge_goal(goal_id, "achieved")
        assert "error" not in result

    def test_failed_closes_even_with_open_conditions(self, temp_db):
        act = _activity()
        goal_id = _new_goal(act)["goal_id_raw"]
        result = gs.judge_goal(goal_id, "failed", note="不要になった")
        assert "error" not in result
        assert _goal_row(goal_id)["closed"] == 1

    def test_failed_without_note_rejected(self, temp_db):
        act = _activity()
        goal_id = _new_goal(act)["goal_id_raw"]
        result = gs.judge_goal(goal_id, "failed")
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_double_judge_returns_already_closed(self, temp_db):
        act = _activity()
        goal_id = _new_goal(act)["goal_id_raw"]
        gs.judge_goal(goal_id, "failed", note="取り下げ")
        result = gs.judge_goal(goal_id, "failed", note="再度")
        assert result["info"] == "GOAL_ALREADY_CLOSED"

    def test_only_incomplete_activity_is_closed(self, temp_db):
        act1 = _activity("a1")
        act2 = _activity("a2")
        goal_id = _new_goal(
            act1, conditions=[{"statement": "c1", "actor": "claude", "state": "satisfied", "note": "済"}]
        )["goal_id_raw"]
        gs.set_goal(act2, {"goal_id": goal_id})
        update_activity(act2, status="completed")
        before1 = _activity_row(act1)
        before2 = _activity_row(act2)
        time.sleep(1.1)  # CURRENT_TIMESTAMPは秒精度のため、closed_at/updated_atの変化を区別できるようにする

        result = gs.judge_goal(goal_id, "achieved", note="達成した")
        closed_ids = {item["id_raw"] for item in result["closed_activities"]}
        assert closed_ids == {act1}

        after1 = _activity_row(act1)
        assert after1["status"] == "completed"
        assert after1["closed_by"] == "goal_judge"
        assert after1["closed_reason"] == "達成した"
        assert after1["closed_at"] is not None
        assert after1["closed_at"] != before1["closed_at"]
        assert after1["updated_at"] != before1["updated_at"]

        after2 = _activity_row(act2)
        assert after2["closed_at"] == before2["closed_at"]
        assert after2["closed_by"] == before2["closed_by"]
        assert after2["closed_reason"] == before2["closed_reason"]


class TestRollback:
    def _achieved_goal_with_two_activities(self):
        act1 = _activity("a1")
        act2 = _activity("a2")
        goal_id = _new_goal(
            act1, handle="rollback-goal",
            conditions=[{"statement": "c1", "actor": "claude", "state": "satisfied", "note": "済"}],
        )["goal_id_raw"]
        gs.set_goal(act2, {"goal_id": goal_id})
        gs.judge_goal(goal_id, "achieved", note="達成した")
        return goal_id, act1, act2

    def test_reopen_resets_closed_and_keeps_judgment(self, temp_db):
        goal_id, act1, act2 = self._achieved_goal_with_two_activities()
        result = gs.update_goal(goal_id, reopen_reason="早すぎた")
        assert "error" not in result
        row = _goal_row(goal_id)
        assert row["closed"] == 0
        assert row["verdict"] == "achieved"
        assert row["judge_note"] == "達成した"

    def test_reopen_only_moves_goal_judge_closed_activities_to_pending(self, temp_db):
        goal_id, act1, act2 = self._achieved_goal_with_two_activities()
        update_activity(act2, status="pending")  # goal_judgeでなく再開させておく（closed_byはgoal_judgeのまま残る想定と区別するための対照）
        gs.update_goal(goal_id, reopen_reason="要確認")
        assert _activity_row(act1)["status"] == "pending"

    def test_reopen_records_signal_row(self, temp_db):
        goal_id, act1, act2 = self._achieved_goal_with_two_activities()
        gs.update_goal(goal_id, reopen_reason="判定が誤りだった", session_id="sess-1")
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM signal_events WHERE kind = 'goal_rollback'"
            ).fetchall()
            rollback_rows = conn.execute(
                "SELECT * FROM signal_events WHERE kind = 'rollback'"
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        assert rows[0]["detail"] == "判定が誤りだった"
        assert "achieved" in rows[0]["context"]
        assert len(rollback_rows) == 0

    def test_second_rollback_is_a_separate_row_with_own_context_and_reason(self, temp_db):
        goal_id, act1, act2 = self._achieved_goal_with_two_activities()
        gs.update_goal(goal_id, reopen_reason="1回目の理由")
        # 全条件が終端のままなので判定待ちに戻る。再度判定してから2回目の差し戻しをする。
        # signalのdedupはkind・source・summaryのfingerprintで行われ、summaryには
        # judged_atの秒精度の値が入るため、1回目と2回目の判定が同じ秒に収まると
        # 同一fingerprintになってしまう。実運用では起き得ない衝突なので、テストでは
        # judged_atを1秒以上ずらして区別できるようにする。
        time.sleep(1.1)
        cond_id = conn_first_condition_id(goal_id)
        gs.update_goal(goal_id, changes=[{"op": "set", "id": cond_id, "state": "satisfied", "note": "済"}])
        gs.judge_goal(goal_id, "achieved", note="2回目の達成")
        gs.update_goal(goal_id, reopen_reason="2回目の理由")

        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM signal_events WHERE kind = 'goal_rollback' ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 2
        assert rows[0]["detail"] == "1回目の理由"
        assert rows[1]["detail"] == "2回目の理由"
        assert "達成した" in rows[0]["context"] or "achieved" in rows[0]["context"]
        assert "2回目の達成" in rows[1]["context"]

    def test_reopen_on_unjudged_goal_returns_already_open(self, temp_db):
        act = _activity()
        goal_id = _new_goal(act)["goal_id_raw"]
        result = gs.update_goal(goal_id, reopen_reason="理由")
        assert result["info"] == "GOAL_ALREADY_OPEN"

    def test_rollback_writes_are_one_transaction(self, temp_db):
        """差し戻しの3つの書き込み（goals・activities・signal_events）が
        同じupdate_goal呼び出しの中で揃って行われることを確認する。"""
        goal_id, act1, act2 = self._achieved_goal_with_two_activities()
        gs.update_goal(goal_id, reopen_reason="理由")
        assert _goal_row(goal_id)["closed"] == 0
        assert _activity_row(act1)["status"] == "pending"
        conn = get_connection()
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM signal_events WHERE kind = 'goal_rollback'"
            ).fetchone()["c"]
        finally:
            conn.close()
        assert count == 1

    def test_reopen_together_with_failing_change_rolls_back_all_three_writes(self, temp_db):
        """差し戻しの3つの書き込み（goals・activities・signal_events）は、
        同じ呼び出し内の別の変更がエラーになると、まとめてロールバックする
        （後続処理の失敗が先行した差し戻しの書き込みを道連れにできることで、
        3つが同一トランザクションであることを示す）。"""
        goal_id, act1, act2 = self._achieved_goal_with_two_activities()
        before_goal = _goal_row(goal_id)
        before_act1 = _activity_row(act1)
        assert before_goal["closed"] == 1
        assert before_act1["status"] == "completed"

        result = gs.update_goal(
            goal_id,
            reopen_reason="理由",
            changes=[{"op": "set", "id": 999999, "state": "satisfied", "note": "済"}],
        )
        assert result["error"]["code"] == "NOT_FOUND"

        after_goal = _goal_row(goal_id)
        after_act1 = _activity_row(act1)
        assert after_goal["closed"] == 1
        assert after_act1["status"] == "completed"
        conn = get_connection()
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM signal_events WHERE kind = 'goal_rollback'"
            ).fetchone()["c"]
        finally:
            conn.close()
        assert count == 0


class TestConcurrencyAndFailure:
    def test_set_goal_database_error_on_lock_leaves_nothing_written(self, temp_db, monkeypatch):
        """別接続がBEGIN IMMEDIATEで書き込みロックを持っている間、set_goalの
        呼び出しはbusy_timeout超過でDATABASE_ERRORを返し、何も書かない。

        本番のbusy_timeout（5秒）をそのまま待つとテストが遅くなるため、
        goal_service内で使うget_connectionだけ、接続直後にbusy_timeoutを
        短く上書きしたものへ差し替える。
        """
        act = _activity()

        real_get_connection = gs.get_connection

        def _fast_timeout_connection():
            conn = real_get_connection()
            conn.execute("PRAGMA busy_timeout=200")
            return conn

        monkeypatch.setattr(gs, "get_connection", _fast_timeout_connection)

        blocker = real_get_connection()
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute("UPDATE activities SET title = title WHERE id = ?", (act,))
        try:
            result = gs.set_goal(act, {"waiver": "理由"})
            assert result["error"]["code"] == "DATABASE_ERROR"
        finally:
            blocker.rollback()
            blocker.close()

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT 1 FROM goal_activities WHERE activity_id = ?", (act,)
            ).fetchone()
        finally:
            conn.close()
        assert row is None

    def test_update_goal_database_error_on_lock_leaves_nothing_written(self, temp_db, monkeypatch):
        act = _activity()
        goal_id = _new_goal(act)["goal_id_raw"]
        cond_id = conn_first_condition_id(goal_id)
        before_state = _condition_row(cond_id)["state"]
        before_statement = _goal_row(goal_id)["statement"]

        real_get_connection = gs.get_connection

        def _fast_timeout_connection():
            conn = real_get_connection()
            conn.execute("PRAGMA busy_timeout=200")
            return conn

        monkeypatch.setattr(gs, "get_connection", _fast_timeout_connection)

        blocker = real_get_connection()
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute("UPDATE activities SET title = title WHERE id = ?", (act,))
        try:
            result = gs.update_goal(
                goal_id,
                changes=[{"op": "set", "id": cond_id, "state": "satisfied", "note": "済"}],
                statement="変えたい一文",
            )
            assert result["error"]["code"] == "DATABASE_ERROR"
        finally:
            blocker.rollback()
            blocker.close()

        assert _condition_row(cond_id)["state"] == before_state
        assert _goal_row(goal_id)["statement"] == before_statement

    def test_judge_goal_database_error_on_lock_leaves_nothing_written(self, temp_db, monkeypatch):
        act = _activity()
        goal_id = _new_goal(
            act, conditions=[{"statement": "c1", "actor": "claude", "state": "satisfied", "note": "済"}]
        )["goal_id_raw"]

        real_get_connection = gs.get_connection

        def _fast_timeout_connection():
            conn = real_get_connection()
            conn.execute("PRAGMA busy_timeout=200")
            return conn

        monkeypatch.setattr(gs, "get_connection", _fast_timeout_connection)

        blocker = real_get_connection()
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute("UPDATE activities SET title = title WHERE id = ?", (act,))
        try:
            result = gs.judge_goal(goal_id, "achieved")
            assert result["error"]["code"] == "DATABASE_ERROR"
        finally:
            blocker.rollback()
            blocker.close()

        assert _goal_row(goal_id)["closed"] == 0
        assert _activity_row(act)["status"] != "completed"

    def test_concurrent_judge_goal_second_gets_closed_or_database_error_not_double_judged(self, temp_db):
        """同じgoalへの同時のjudge_goalの2件目は、GOAL_ALREADY_CLOSEDかDATABASE_ERROR
        になり、二重に判定されない。"""
        act = _activity()
        goal_id = _new_goal(
            act, conditions=[{"statement": "c1", "actor": "claude", "state": "satisfied", "note": "済"}]
        )["goal_id_raw"]

        results: dict[int, dict] = {}
        barrier = threading.Barrier(2)

        def worker(key: int) -> None:
            barrier.wait()
            results[key] = gs.judge_goal(goal_id, "achieved", note=f"判定{key}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in (1, 2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        outcomes = list(results.values())
        successes = [r for r in outcomes if "error" not in r and "info" not in r]
        rejected = [
            r
            for r in outcomes
            if r.get("info") == "GOAL_ALREADY_CLOSED" or r.get("error", {}).get("code") == "DATABASE_ERROR"
        ]
        assert len(successes) == 1
        assert len(rejected) == 1
        assert _goal_row(goal_id)["closed"] == 1

    def test_concurrent_unlink_of_last_two_activities_does_not_orphan_goal(self, temp_db):
        """未判定goalの残り2つのactivityを2つの接続から同時に外しても、
        goalは孤立しない（片方はGOAL_WOULD_ORPHANで拒否される）。"""
        act1 = _activity("a1")
        act2 = _activity("a2")
        goal_id = _new_goal(act1, handle="concurrent-orphan-guard")["goal_id_raw"]
        gs.set_goal(act2, {"goal_id": goal_id})

        results: dict[int, dict] = {}
        barrier = threading.Barrier(2)

        def worker(key: int, act: int) -> None:
            barrier.wait()
            results[key] = gs.set_goal(act, None, replace=True)

        threads = [
            threading.Thread(target=worker, args=(1, act1)),
            threading.Thread(target=worker, args=(2, act2)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        outcomes = list(results.values())
        successes = [r for r in outcomes if "error" not in r]
        orphan_rejections = [r for r in outcomes if r.get("error", {}).get("code") == "GOAL_WOULD_ORPHAN"]
        assert len(successes) == 1
        assert len(orphan_rejections) == 1
        assert _linked_activity_count(goal_id) == 1
