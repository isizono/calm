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


class TestBuildAskNotifyLinesBudgetAware:
    """budget_chars指定時、呼び出し元(compose())のハード切り詰めで表示が
    欠落する行のask_idを消費済みにしてしまわないことを検証する。"""

    def test_all_lines_within_budget_consumes_all(self, temp_db, hook_state_dir):
        act = _make_activity()
        r1 = ak.add_ask("q1", tags=["domain:test"], blocks=[act])
        ak.answer_ask(r1["id"], "short answer")
        state = HookState(SESSION_ID)
        state.add_tracked_ask_ids([r1["id"]])

        lines = build_ask_notify_lines(SESSION_ID, budget_chars=600)

        assert len(lines) == 2
        assert "short answer" in lines[1]
        assert state.get_tracked_ask_ids() == []

    def test_line_exceeding_budget_is_excluded_and_stays_tracked(self, temp_db, hook_state_dir):
        """1件だけ追跡中で、その回答が予算を大きく超える長さの場合、
        (compose()側のハード切り詰めで表示が欠けてしまうため) 何も返さず、
        ask_idも消費しない（次回以降に持ち越す）。"""
        act = _make_activity()
        r1 = ak.add_ask("q1", tags=["domain:test"], blocks=[act])
        ak.answer_ask(r1["id"], "x" * 8000)
        state = HookState(SESSION_ID)
        state.add_tracked_ask_ids([r1["id"]])

        lines = build_ask_notify_lines(SESSION_ID, budget_chars=600)

        assert lines == []
        assert state.get_tracked_ask_ids() == [r1["id"]]

    def test_mixed_lengths_consumes_only_the_fitting_prefix(self, temp_db, hook_state_dir):
        """get_asksはlast_seen_at DESC, id DESC順で返す（最近更新された/IDが
        新しい方が先頭）。id採番順でr_longを先に作ってr_shortを後に作ることで、
        r_shortが常に先頭に来る（last_seen_atの秒解像度に依存しない）。
        先頭から収まる分（r_short）だけを消費し、収まらないr_longは
        追跡対象に残す（prefix方式）。"""
        act = _make_activity()
        r_long = ak.add_ask("long one", tags=["domain:test"], blocks=[act])
        r_short = ak.add_ask("short one", tags=["domain:test"], blocks=[act])
        ak.answer_ask(r_long["id"], "y" * 8000)
        ak.answer_ask(r_short["id"], "fits fine")
        state = HookState(SESSION_ID)
        state.add_tracked_ask_ids([r_long["id"], r_short["id"]])

        lines = build_ask_notify_lines(SESSION_ID, budget_chars=600)

        assert len(lines) == 2  # ヘッダ行 + r_shortの1件のみ
        # ヘッダーの件数表記は実際に含めた件数（1件）に差し替わる（resolved全体の
        # 件数である2件のままだと、1行しか出ないのに「2件」と主張する矛盾になる）
        assert "1件" in lines[0]
        assert "2件" not in lines[0]
        assert "fits fine" in lines[1]
        assert "y" * 8000 not in "\n".join(lines)
        assert state.get_tracked_ask_ids() == [r_long["id"]]

    def test_budget_too_small_for_header_consumes_nothing(self, temp_db, hook_state_dir):
        act = _make_activity()
        r1 = ak.add_ask("q1", tags=["domain:test"], blocks=[act])
        ak.answer_ask(r1["id"], "the answer")
        state = HookState(SESSION_ID)
        state.add_tracked_ask_ids([r1["id"]])

        lines = build_ask_notify_lines(SESSION_ID, budget_chars=5)

        assert lines == []
        assert state.get_tracked_ask_ids() == [r1["id"]]

    def test_none_budget_consumes_all_even_when_line_is_very_long(self, temp_db, hook_state_dir):
        """budget_chars省略時（UserPromptSubmit hook経路）は予算を意識せず
        全件を消費する。"""
        act = _make_activity()
        r1 = ak.add_ask("q1", tags=["domain:test"], blocks=[act])
        ak.answer_ask(r1["id"], "z" * 8000)
        state = HookState(SESSION_ID)
        state.add_tracked_ask_ids([r1["id"]])

        lines = build_ask_notify_lines(SESSION_ID)

        assert len(lines) == 2
        assert "z" * 8000 in lines[1]
        assert state.get_tracked_ask_ids() == []
