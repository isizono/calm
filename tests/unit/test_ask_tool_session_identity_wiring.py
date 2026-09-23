"""ask系4ツール（main.py）のsession_id解決 配線のunit test。

add_ask/answer_ask/triage_ask/withdraw_askのツール関数が、caller_session_idの
解決を`_current_session_id()`（MCP接続単位のephemeral ID）ではなく
`get_caller_session_id()`（server再起動をまたいで安定な恒久ID）に
委ねていることを検証する。ask_service自体のロジックはtests/unit/test_ask_service.pyが
担うためここでは扱わない。
"""
from unittest.mock import MagicMock

import pytest

import src.main as main_module


@pytest.fixture(autouse=True)
def _fixed_caller_session_id(monkeypatch):
    monkeypatch.setattr(main_module, "get_caller_session_id", lambda: "sess-1")


@pytest.fixture(autouse=True)
def _reject_current_session_id(monkeypatch):
    """`_current_session_id()`が使われたら即座に検出できるよう別値を返す。"""
    monkeypatch.setattr(
        main_module, "_current_session_id", lambda: "stale-ephemeral-id"
    )


class TestAddAskUsesStableSessionIdentity:
    def test_passes_resolved_identity_as_session_id(self, monkeypatch):
        stub = MagicMock(return_value={"id": 1, "deduped": False})
        monkeypatch.setattr(main_module.ask_service, "add_ask", stub)

        main_module.add_ask("q", blocks=[1], tags=["domain:test"])

        assert stub.call_args.kwargs["session_id"] == "sess-1"


class TestAnswerAskUsesStableSessionIdentity:
    def test_passes_resolved_identity_as_session_id(self, monkeypatch):
        stub = MagicMock(return_value={"id": 1, "status": "answered"})
        monkeypatch.setattr(main_module.ask_service, "answer_ask", stub)

        main_module.answer_ask(1, "answer body")

        assert stub.call_args.kwargs["session_id"] == "sess-1"


class TestTriageAskUsesStableSessionIdentity:
    def test_passes_resolved_identity_as_session_id(self, monkeypatch):
        stub = MagicMock(return_value={"id": 1, "status": "dismissed"})
        monkeypatch.setattr(main_module.ask_service, "triage_ask", stub)

        main_module.triage_ask(1, "dismiss", dismiss_reason="not needed")

        assert stub.call_args.kwargs["session_id"] == "sess-1"


class TestWithdrawAskUsesStableSessionIdentity:
    def test_passes_resolved_identity_as_session_id(self, monkeypatch):
        stub = MagicMock(return_value={"id": 1, "status": "withdrawn"})
        monkeypatch.setattr(main_module.ask_service, "withdraw_ask", stub)

        main_module.withdraw_ask(1, "mistake")

        assert stub.call_args.kwargs["session_id"] == "sess-1"
