"""asks機能の統合テスト。

add_ask→answer_ask→triage_ask(promote/dismiss)→check_in配達という
サービス間連携の全体像、withdraw経路、dedup経路を検証する。
個々のバリデーション・TOCTOU等の詳細はtests/unit/test_ask_service.pyが担保する。
"""
from src.db import get_connection
from src.services import ask_service as ak
from src.services.activity_service import add_activity
from src.services.checkin_service import check_in


def _make_activity(title: str = "a1", orch_managed: bool = False) -> int:
    return add_activity(
        title=title,
        description="d",
        tags=["domain:test"],
        check_in=False,
        orch_managed=orch_managed,
    )["activity_id"]


class TestAnswerPromoteCheckInFlow:
    def test_full_lifecycle_delivers_and_clears_via_checkin(self, temp_db):
        activity_id = _make_activity()

        ask = ak.add_ask("Should we use approach A?", tags=["domain:test"], blocks=[activity_id])

        result = check_in(activity_id)
        assert result["asks"]["awaiting_answer"][0]["id_raw"] == ask["id"]
        assert "asks" in result
        assert not any("triage" in h for h in result.get("hints", []))

        ak.answer_ask(ask["id"], "yes, use approach A")

        result = check_in(activity_id)
        assert result["asks"]["awaiting_answer"] == []
        assert result["asks"]["awaiting_triage"][0]["answer_body"] == "yes, use approach A"
        assert any("triage" in h for h in result["hints"])

        promoted = ak.triage_ask(
            ask["id"], action="promote", decision="use approach A", reason="because Y"
        )
        assert promoted["status"] == "promoted"

        conn = get_connection()
        try:
            decision_row = conn.execute(
                "SELECT decision FROM decisions WHERE id = ?",
                (promoted["promoted_decision_id"],),
            ).fetchone()
        finally:
            conn.close()
        assert decision_row["decision"] == "use approach A"

        result = check_in(activity_id)
        assert "asks" not in result


class TestAnswerDismissCheckInFlow:
    def test_dismiss_clears_delivery_without_creating_decision(self, temp_db):
        activity_id = _make_activity()

        ask = ak.add_ask("Should we use approach B?", tags=["domain:test"], blocks=[activity_id])
        ak.answer_ask(ask["id"], "no, skip it")

        dismissed = ak.triage_ask(ask["id"], action="dismiss", dismiss_reason="not worth it")
        assert dismissed["status"] == "dismissed"

        result = check_in(activity_id)
        assert "asks" not in result

        listed = ak.get_asks(status="dismissed")
        assert listed["asks"][0]["question"] == "Should we use approach B?"
        assert listed["asks"][0]["answer_body"] == "no, skip it"


class TestWithdrawFlow:
    def test_withdraw_clears_delivery(self, temp_db):
        activity_id = _make_activity()

        ask = ak.add_ask("Should we use approach C?", tags=["domain:test"], blocks=[activity_id])
        result = check_in(activity_id)
        assert "asks" in result

        withdrawn = ak.withdraw_ask(ask["id"], "posted by mistake")
        assert withdrawn["status"] == "withdrawn"

        result = check_in(activity_id)
        assert "asks" not in result


class TestDedupFlow:
    def test_repeated_add_ask_accumulates_then_resolves(self, temp_db):
        activity_id = _make_activity()

        first = ak.add_ask("Should we refactor module X?", tags=["domain:test"], blocks=[activity_id])
        second = ak.add_ask("should we refactor module x?", tags=["domain:test"], blocks=[activity_id])
        assert second["id"] == first["id"]
        assert second["occurrence_count"] == 2

        result = check_in(activity_id)
        assert len(result["asks"]["awaiting_answer"]) == 1

        ak.answer_ask(first["id"], "yes")
        ak.triage_ask(first["id"], action="dismiss", dismiss_reason="done")

        result = check_in(activity_id)
        assert "asks" not in result


class TestMultipleBlockedActivities:
    def test_answering_resolves_delivery_for_all_blocked_activities(self, temp_db):
        activity_a = _make_activity("a")
        activity_b = _make_activity("b")

        ask = ak.add_ask("Shared blocking question?", tags=["domain:test"], blocks=[activity_a, activity_b])

        result_a = check_in(activity_a)
        result_b = check_in(activity_b)
        assert result_a["asks"]["awaiting_answer"][0]["id_raw"] == ask["id"]
        assert result_b["asks"]["awaiting_answer"][0]["id_raw"] == ask["id"]

        ak.answer_ask(ask["id"], "resolved")
        ak.triage_ask(ask["id"], action="dismiss", dismiss_reason="done")

        assert "asks" not in check_in(activity_a)
        assert "asks" not in check_in(activity_b)


class TestOrchManagedActivityStillReceivesAskHints:
    def test_triage_pending_hint_survives_orch_managed_suppression(self, temp_db):
        """recompose系hintはorch-managed activityで全suppressされるが、
        askは答え待ちのプロセス情報そのものとして扱い、suppressしない。"""
        activity_id = _make_activity("orch-managed-activity", orch_managed=True)

        ask = ak.add_ask("Orch-managed blocking question?", tags=["domain:test"], blocks=[activity_id])
        ak.answer_ask(ask["id"], "answered")

        result = check_in(activity_id)

        assert result["asks"]["awaiting_triage"][0]["id_raw"] == ask["id"]
        assert any("triage" in h for h in result["hints"])
