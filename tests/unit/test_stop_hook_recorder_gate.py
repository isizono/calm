"""hooks/stop_hook.py::_handle_nudges の記録役ゲート判定のユニットテスト

record_missing(record系ツール未呼出)・follow_up_after_decision(decision単独呼出)・
logs_sparse(topic scopeの遅延hint)の3種類のnudgeが、記録役セッション判定
(is_recorder_attached)の結果に応じてまとめて抑制/生成されることを検証する。
is_recorder_attached自体はmockせず、write_markerで実際に目印ファイルを置き、
psコマンドの呼び出し(外部境界)だけをmonkeypatchする。check-in強制block
(turn==_CHECKIN_DEFER_TURNSでのblock)は_handle_nudgesの対象外であり本ファイル
の検証対象ではない。
"""
import os
import subprocess

import pytest

import hooks.stop_hook as stop_hook
from hooks.hook_state import HookState
from hooks.recorder_marker import write_marker
from src.infra import process_signature
from src.services.topic_service import add_topic
from tests.helpers import add_decision

_DOMAIN_TAG = "domain:stop-hook-recorder-gate-test"


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(HookState, "BASE_DIR", tmp_path)
    return tmp_path


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


def _fake_ps(lstart_output: str, returncode: int = 0):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode, stdout=lstart_output, stderr="")
    return fake_run


def _attach_recorder(monkeypatch, session_id: str) -> None:
    """write_markerで実際に目印ファイルを置き、以降のps呼び出しも同じ起動
    時刻を返すようにして、自プロセス(os.getpid())を記録役として生存させる。"""
    monkeypatch.setattr(
        process_signature.subprocess, "run", _fake_ps("Thu Jul 24 09:32:04 2026\n")
    )
    write_marker(session_id, os.getpid())


def _attach_dead_recorder_marker(monkeypatch, session_id: str) -> None:
    """目印ファイルは実在するが、以降のps呼び出しはプロセス不在を返す
    (記録役が死んでいる)状態を作る。"""
    monkeypatch.setattr(
        process_signature.subprocess, "run", _fake_ps("Thu Jul 24 09:32:04 2026\n")
    )
    write_marker(session_id, 999999999)
    monkeypatch.setattr(process_signature.subprocess, "run", _fake_ps("", returncode=1))


class TestRecordMissingGate:
    """b: record_missing nudge"""

    def _events_and_turn(self):
        # turn1でcheck_in、turn2以降記録なしのままturn4に到達(_NUDGE_INTERVAL=2の倍数)
        return [{"e": "tool", "name": "check_in", "turn": 1, "activity_id": 1}], 4

    def test_suppressed_when_recorder_attached(self, temp_db, state_dir, monkeypatch):
        _attach_recorder(monkeypatch, "recorder-session")
        events, turn = self._events_and_turn()
        state = _FakeState()

        stop_hook._handle_nudges(state, events, turn, session_id="recorder-session")

        assert state.appended == []

    def test_generated_when_recorder_not_attached(self, temp_db, state_dir):
        events, turn = self._events_and_turn()
        state = _FakeState()

        stop_hook._handle_nudges(state, events, turn, session_id="normal-session")

        types = {e["type"] for e in state.appended if e["e"] == "nudge"}
        assert types == {"record_missing"}

    def test_generated_when_marker_present_but_recorder_dead(
        self, temp_db, state_dir, monkeypatch
    ):
        """目印ファイルはあるが記録役が死んでいる → 通常どおり催促が出る"""
        _attach_dead_recorder_marker(monkeypatch, "stale-recorder-session")
        events, turn = self._events_and_turn()
        state = _FakeState()

        stop_hook._handle_nudges(state, events, turn, session_id="stale-recorder-session")

        types = {e["type"] for e in state.appended if e["e"] == "nudge"}
        assert types == {"record_missing"}


class TestFollowUpAndLogsSparseGate:
    """c: follow_up_after_decision, d: logs_sparse

    add_decisions単独呼出は同一turnでc・dを両方誘発する(記録系ツール呼出そのもの
    なのでbのhas_recent_record判定によりbとは同時に発生しない)。
    """

    def test_suppressed_when_recorder_attached(self, temp_db, state_dir, monkeypatch):
        topic_id = _seed_topic_with_sparse_logs()
        _attach_recorder(monkeypatch, "recorder-session")
        events = [{"e": "tool", "name": "add_decisions", "turn": 3, "topic_ids": [topic_id]}]
        state = _FakeState()

        stop_hook._handle_nudges(state, events, current_turn=3, session_id="recorder-session")

        assert state.appended == []

    def test_generated_when_recorder_not_attached(self, temp_db, state_dir):
        topic_id = _seed_topic_with_sparse_logs()
        events = [{"e": "tool", "name": "add_decisions", "turn": 3, "topic_ids": [topic_id]}]
        state = _FakeState()

        stop_hook._handle_nudges(state, events, current_turn=3, session_id="normal-session")

        types = {e["type"] for e in state.appended if e["e"] == "nudge"}
        assert types == {"follow_up_after_decision", "logs_sparse"}


class TestGateFailsSafeOnUnexpectedException:
    """is_recorder_attached内部で予期しない例外が起きた場合に、ゲートが
    「付いていない」側に倒れて催促が出ることを確認する。"""

    def test_nudge_generated_when_marker_check_raises_unexpected_error(
        self, temp_db, state_dir, monkeypatch
    ):
        _attach_recorder(monkeypatch, "recorder-session")

        def fake_run(cmd, **kwargs):
            raise RuntimeError("unexpected ps failure")

        monkeypatch.setattr(process_signature.subprocess, "run", fake_run)

        events = [{"e": "tool", "name": "check_in", "turn": 1, "activity_id": 1}]
        state = _FakeState()

        stop_hook._handle_nudges(state, events, current_turn=4, session_id="recorder-session")

        types = {e["type"] for e in state.appended if e["e"] == "nudge"}
        assert types == {"record_missing"}


class TestGateSkippedWithoutSessionId:
    def test_session_id_none_ignores_marker(self, temp_db, state_dir, monkeypatch):
        """session_id未指定(None)ならゲート判定自体を素通りし、他セッションの
        目印ファイルの有無に関わらず通常どおりnudgeを生成する。"""
        _attach_recorder(monkeypatch, "unrelated-session")
        events = [{"e": "tool", "name": "check_in", "turn": 1, "activity_id": 1}]
        state = _FakeState()

        stop_hook._handle_nudges(state, events, current_turn=4, session_id=None)

        types = {e["type"] for e in state.appended if e["e"] == "nudge"}
        assert types == {"record_missing"}
