"""hooks/stop_hook.py::_handle_nudges の記録役ゲート判定のユニットテスト

record_missing(record系ツール未呼出)・follow_up_after_decision(decision単独呼出)・
logs_sparse(topic scopeの遅延hint)の3種類のnudgeが、記録役セッション判定
(is_recorder_attached)がTrueのときはまとめて抑制され、Falseのときは通常どおり
生成されることを検証する。check-in強制block(turn==_CHECKIN_DEFER_TURNSでの
block)は_handle_nudgesの対象外であり本ファイルの検証対象ではない。
"""
import hooks.stop_hook as stop_hook
from src.services.topic_service import add_topic
from tests.helpers import add_decision

_DOMAIN_TAG = "domain:stop-hook-recorder-gate-test"


class _FakeState:
    """HookState.append_eventsだけを差し替えて生成イベントを捕捉するダブル。"""

    def __init__(self):
        self.appended: list[dict] = []

    def append_events(self, events):
        self.appended.extend(events)


def _seed_topic_with_sparse_logs() -> int:
    """decision1件・log0件のtopicを作り、logs_sparse hintが出る状態にする。"""
    topic = add_topic(title="t", description="d", tags=[_DOMAIN_TAG])
    add_decision(decision="d", reason="r", topic_id=topic["topic_id"])
    return topic["topic_id"]


class TestRecordMissingGate:
    """b: record_missing nudge"""

    def _events_and_turn(self):
        # turn1でcheck_in、turn2以降記録なしのままturn4に到達(_NUDGE_INTERVAL=2の倍数)
        return [{"e": "tool", "name": "check_in", "turn": 1, "activity_id": 1}], 4

    def test_suppressed_when_recorder_attached(self, temp_db, monkeypatch):
        monkeypatch.setattr(stop_hook, "is_recorder_attached", lambda session_id: True)
        events, turn = self._events_and_turn()
        state = _FakeState()

        stop_hook._handle_nudges(state, events, turn, session_id="recorder-session")

        assert state.appended == []

    def test_generated_when_recorder_not_attached(self, temp_db, monkeypatch):
        monkeypatch.setattr(stop_hook, "is_recorder_attached", lambda session_id: False)
        events, turn = self._events_and_turn()
        state = _FakeState()

        stop_hook._handle_nudges(state, events, turn, session_id="normal-session")

        types = {e["type"] for e in state.appended if e["e"] == "nudge"}
        assert types == {"record_missing"}


class TestFollowUpAndLogsSparseGate:
    """c: follow_up_after_decision, d: logs_sparse

    add_decisions単独呼出は同一turnでc・dを両方誘発する(記録系ツール呼出そのもの
    なのでbのhas_recent_record判定によりbとは同時に発生しない)。
    """

    def test_suppressed_when_recorder_attached(self, temp_db, monkeypatch):
        topic_id = _seed_topic_with_sparse_logs()
        monkeypatch.setattr(stop_hook, "is_recorder_attached", lambda session_id: True)
        events = [{"e": "tool", "name": "add_decisions", "turn": 3, "topic_ids": [topic_id]}]
        state = _FakeState()

        stop_hook._handle_nudges(state, events, current_turn=3, session_id="recorder-session")

        assert state.appended == []

    def test_generated_when_recorder_not_attached(self, temp_db, monkeypatch):
        topic_id = _seed_topic_with_sparse_logs()
        monkeypatch.setattr(stop_hook, "is_recorder_attached", lambda session_id: False)
        events = [{"e": "tool", "name": "add_decisions", "turn": 3, "topic_ids": [topic_id]}]
        state = _FakeState()

        stop_hook._handle_nudges(state, events, current_turn=3, session_id="normal-session")

        types = {e["type"] for e in state.appended if e["e"] == "nudge"}
        assert types == {"follow_up_after_decision", "logs_sparse"}


class TestGateSkippedWithoutSessionId:
    def test_session_id_none_does_not_call_is_recorder_attached(self, temp_db, monkeypatch):
        """session_id未指定(None)ならゲート判定自体を素通りし、
        is_recorder_attachedは呼ばれず通常どおりnudgeを生成する。"""
        calls: list[str] = []
        monkeypatch.setattr(
            stop_hook, "is_recorder_attached",
            lambda session_id: calls.append(session_id) or True,
        )
        events = [{"e": "tool", "name": "check_in", "turn": 1, "activity_id": 1}]
        state = _FakeState()

        stop_hook._handle_nudges(state, events, current_turn=4, session_id=None)

        assert calls == []
        types = {e["type"] for e in state.appended if e["e"] == "nudge"}
        assert types == {"record_missing"}
