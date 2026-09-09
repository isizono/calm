"""hooks/ask_notify_section.py の単体テスト。

SessionStart/UserPromptSubmit両hookが共有する、tracked_ask_ids（HookState）を
get_asksで直接照会して表示・消費するロジックを、実DB + 実HookStateファイルで
検証する（identity解決には一切触れない経路であることの確認も兼ねる）。
"""
from pathlib import Path

import pytest

from hooks.ask_notify_section import build_ask_notify_lines
from hooks.hook_state import HookState
from src.db import get_connection
from src.services import ask_service as ak
from src.services.activity_service import add_activity


def _make_activity() -> int:
    return add_activity(
        title="a1", description="d", tags=["domain:test"], check_in=False
    )["activity_id"]


@pytest.fixture
def hook_state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(HookState, "BASE_DIR", tmp_path)
    return tmp_path


SESSION_ID = "sess-ask-notify-1"


class TestBuildAskNotifyLines:
    def test_no_session_id_returns_empty(self, temp_db, hook_state_dir):
        assert build_ask_notify_lines(None) == []
        assert build_ask_notify_lines("") == []

    def test_no_tracked_ids_returns_empty(self, temp_db, hook_state_dir):
        assert build_ask_notify_lines(SESSION_ID) == []

    def test_still_open_ask_produces_no_lines_and_stays_tracked(self, temp_db, hook_state_dir):
        act = _make_activity()
        r1 = ak.add_ask("q1", tags=["domain:test"], blocks=[act])
        state = HookState(SESSION_ID)
        state.add_tracked_ask_ids([r1["id"]])

        lines = build_ask_notify_lines(SESSION_ID)

        assert lines == []
        assert state.get_tracked_ask_ids() == [r1["id"]]

    def test_answered_ask_is_reported_and_consumed(self, temp_db, hook_state_dir):
        act = _make_activity()
        r1 = ak.add_ask("q1", tags=["domain:test"], blocks=[act])
        ak.answer_ask(r1["id"], "the answer")
        state = HookState(SESSION_ID)
        state.add_tracked_ask_ids([r1["id"]])

        lines = build_ask_notify_lines(SESSION_ID)

        assert len(lines) == 2  # ヘッダ行 + 1件
        assert "q1" in lines[1]
        assert "the answer" in lines[1]
        assert state.get_tracked_ask_ids() == []

    def test_dismissed_ask_is_reported_and_consumed(self, temp_db, hook_state_dir):
        act = _make_activity()
        r1 = ak.add_ask("q1", tags=["domain:test"], blocks=[act])
        ak.answer_ask(r1["id"], "the answer")
        ak.triage_ask(r1["id"], "dismiss", dismiss_reason="not needed")
        state = HookState(SESSION_ID)
        state.add_tracked_ask_ids([r1["id"]])

        lines = build_ask_notify_lines(SESSION_ID)

        assert any("却下" in line and "not needed" in line for line in lines)
        assert state.get_tracked_ask_ids() == []

    def test_mixed_open_and_resolved_only_consumes_resolved(self, temp_db, hook_state_dir):
        act = _make_activity()
        r1 = ak.add_ask("still open", tags=["domain:test"], blocks=[act])
        r2 = ak.add_ask("already answered", tags=["domain:test"], blocks=[act])
        ak.answer_ask(r2["id"], "done")
        state = HookState(SESSION_ID)
        state.add_tracked_ask_ids([r1["id"], r2["id"]])

        lines = build_ask_notify_lines(SESSION_ID)

        assert len(lines) == 2  # ヘッダ行 + r2の1件のみ
        assert "already answered" in lines[1]
        assert state.get_tracked_ask_ids() == [r1["id"]]

    def test_second_call_after_consumption_returns_empty(self, temp_db, hook_state_dir):
        """一度消費されたaskは、以降の呼び出しで再表示されない。"""
        act = _make_activity()
        r1 = ak.add_ask("q1", tags=["domain:test"], blocks=[act])
        ak.answer_ask(r1["id"], "the answer")
        state = HookState(SESSION_ID)
        state.add_tracked_ask_ids([r1["id"]])

        first = build_ask_notify_lines(SESSION_ID)
        second = build_ask_notify_lines(SESSION_ID)

        assert first != []
        assert second == []

    def test_conn_argument_reuses_caller_conn_instead_of_opening_a_new_one(
        self, temp_db, hook_state_dir, monkeypatch
    ):
        """conn引数を渡した場合はget_asks_with_conn経由でそれを使い回し、
        自前でget_connection()を呼ばない（session_start_hookの他セクション
        ビルダーと同じconn共有規約に合わせる契約）。"""
        act = _make_activity()
        r1 = ak.add_ask("q1", tags=["domain:test"], blocks=[act])
        ak.answer_ask(r1["id"], "the answer")
        state = HookState(SESSION_ID)
        state.add_tracked_ask_ids([r1["id"]])

        def _fail_get_connection(*args, **kwargs):
            raise AssertionError(
                "build_ask_notify_lines(conn=...) must not open its own connection"
            )

        monkeypatch.setattr(ak, "get_connection", _fail_get_connection)

        conn = get_connection()
        try:
            lines = build_ask_notify_lines(SESSION_ID, conn=conn)
        finally:
            conn.close()

        assert len(lines) == 2  # ヘッダ行 + 1件（conn共有でも通常通り解決済みが拾える）
        assert "the answer" in lines[1]
        assert state.get_tracked_ask_ids() == []
