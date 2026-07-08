"""relay_subscribe tool wrapper（main.py）の notify_reconfigure 配線の unit test。

service 層の relay_subscribe 呼び出し結果（reused / error の有無）に応じて、
RelayRuntime.notify_reconfigure() を呼ぶかどうかを分岐する main.py 側の薄い glue を
検証する。service.py 自体の subscribe ロジック（reused 判定・declaration file 更新）は
tests/unit/test_relay_service_subscribe.py が担うためここでは扱わない。
"""
from unittest.mock import MagicMock

import pytest

import src.main as main_module


@pytest.fixture(autouse=True)
def _fixed_caller_session_id(monkeypatch):
    monkeypatch.setattr(
        main_module.relay_identity, "get_relay_identity", lambda: "sess-1"
    )


def _stub_service_result(monkeypatch, result: dict) -> None:
    monkeypatch.setattr(
        main_module.relay_session_service,
        "relay_subscribe",
        lambda labels, caller_session_id=None: result,
    )


_NEW_SUBSCRIPTION_RESULT = {
    "subscription_id": "sub-1",
    "labels": ["a"],
    "lease_expires_at": "2099-01-01T00:00:00Z",
    "handle": "cc-memory-1",
    "reused": False,
}

_REUSED_SUBSCRIPTION_RESULT = {
    "subscription_id": "sub-1",
    "labels": ["a"],
    "lease_expires_at": "2099-01-01T00:00:00Z",
    "handle": "cc-memory-1",
    "reused": True,
}

_ERROR_RESULT = {"error": {"code": "config_missing", "message": "RELAY_BEARER_TOKEN 未設定"}}


class TestNotifyReconfigureWiring:
    def test_new_subscription_notifies_runtime(self, monkeypatch):
        runtime = MagicMock()
        monkeypatch.setattr(main_module, "_relay_runtime", runtime)
        _stub_service_result(monkeypatch, dict(_NEW_SUBSCRIPTION_RESULT))

        result = main_module.relay_subscribe(["a"])

        assert "error" not in result
        runtime.notify_reconfigure.assert_called_once()

    def test_reused_subscription_does_not_notify_runtime(self, monkeypatch):
        runtime = MagicMock()
        monkeypatch.setattr(main_module, "_relay_runtime", runtime)
        _stub_service_result(monkeypatch, dict(_REUSED_SUBSCRIPTION_RESULT))

        main_module.relay_subscribe(["a"])

        runtime.notify_reconfigure.assert_not_called()

    def test_error_result_does_not_notify_runtime(self, monkeypatch):
        runtime = MagicMock()
        monkeypatch.setattr(main_module, "_relay_runtime", runtime)
        _stub_service_result(monkeypatch, dict(_ERROR_RESULT))

        main_module.relay_subscribe(["a"])

        runtime.notify_reconfigure.assert_not_called()

    def test_runtime_none_does_not_raise_and_returns_result_unchanged(self, monkeypatch):
        """get_relay_runtime() が None（stdio 相当）でも例外を出さず成功応答をそのまま返す。"""
        monkeypatch.setattr(main_module, "_relay_runtime", None)
        expected = dict(_NEW_SUBSCRIPTION_RESULT)
        expected["identity"] = "sess-1"
        _stub_service_result(monkeypatch, dict(_NEW_SUBSCRIPTION_RESULT))

        result = main_module.relay_subscribe(["a"])

        assert result == expected


class TestGetRelayRuntimeGetterContract:
    """_session_manager / get_session_manager() と対称の契約であることを確認する。"""

    def test_returns_none_before_assignment(self, monkeypatch):
        monkeypatch.setattr(main_module, "_relay_runtime", None)
        assert main_module.get_relay_runtime() is None

    def test_returns_assigned_instance(self, monkeypatch):
        sentinel = object()
        monkeypatch.setattr(main_module, "_relay_runtime", sentinel)
        assert main_module.get_relay_runtime() is sentinel
