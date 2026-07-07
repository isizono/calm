"""relay_publish/relay_subscribe/relay_receive（src.main の MCP tool wrapper）が

- caller_session_id の解決に relay_identity.get_relay_identity() を使うこと
- 成功応答に "identity" キーを追加すること
- エラー応答には "identity" キーを追加しないこと

を検証する。relay_session_service 自体のロジックは他のユニットテストで
検証済みのため、ここでは main.py 層の配線のみに焦点を絞る。
"""
import pytest

from src.services.relay import identity as relay_identity


@pytest.fixture(autouse=True)
def _fixed_identity(monkeypatch):
    """get_relay_identity() を固定値に差し替える（bridge identityヘッダの有無は
    identity.py 自体のテスト（test_relay_identity.py）で別途検証済み）。
    """
    monkeypatch.setattr(
        relay_identity, "get_relay_identity", lambda: "bridge-uuid-fixed"
    )


class TestRelayPublishIdentity:
    def test_success_response_includes_identity(self, monkeypatch):
        from src.main import relay_publish, relay_session_service

        monkeypatch.setattr(
            relay_session_service,
            "relay_publish",
            lambda labels, body, title=None, caller_session_id=None: {
                "outbox_id": 1,
                "labels": labels,
                "handle": "handle:abc",
                "_caller_session_id_seen": caller_session_id,
            },
        )
        result = relay_publish(["room:test"], "hello")
        assert result["identity"] == "bridge-uuid-fixed"
        assert result["_caller_session_id_seen"] == "bridge-uuid-fixed"

    def test_error_response_has_no_identity_key(self, monkeypatch):
        from src.main import relay_publish, relay_session_service

        monkeypatch.setattr(
            relay_session_service,
            "relay_publish",
            lambda *a, **k: {"error": {"code": "validation", "message": "bad"}},
        )
        result = relay_publish(["room:test"], "hello")
        assert "identity" not in result
        assert result["error"]["code"] == "validation"


class TestRelaySubscribeIdentity:
    def test_success_response_includes_identity(self, monkeypatch):
        from src.main import relay_subscribe, relay_session_service

        monkeypatch.setattr(
            relay_session_service,
            "relay_subscribe",
            lambda labels, caller_session_id=None: {
                "subscription_id": "sub-1",
                "labels": labels,
                "lease_expires_at": "2099-01-01T00:00:00Z",
                "handle": "handle:abc",
                "reused": False,
                "_caller_session_id_seen": caller_session_id,
            },
        )
        result = relay_subscribe(["room:test"])
        assert result["identity"] == "bridge-uuid-fixed"
        assert result["_caller_session_id_seen"] == "bridge-uuid-fixed"

    def test_error_response_has_no_identity_key(self, monkeypatch):
        from src.main import relay_subscribe, relay_session_service

        monkeypatch.setattr(
            relay_session_service,
            "relay_subscribe",
            lambda *a, **k: {"error": {"code": "session_unresolved", "message": "x"}},
        )
        result = relay_subscribe(["room:test"])
        assert "identity" not in result


class TestRelayReceiveIdentity:
    def test_success_response_includes_identity(self, monkeypatch):
        from src.main import relay_receive, relay_session_service

        monkeypatch.setattr(
            relay_session_service,
            "relay_receive",
            lambda limit, caller_session_id=None: {
                "messages": [],
                "count": 0,
                "_caller_session_id_seen": caller_session_id,
            },
        )
        result = relay_receive()
        assert result["identity"] == "bridge-uuid-fixed"
        assert result["_caller_session_id_seen"] == "bridge-uuid-fixed"

    def test_error_response_has_no_identity_key(self, monkeypatch):
        from src.main import relay_receive, relay_session_service

        monkeypatch.setattr(
            relay_session_service,
            "relay_receive",
            lambda *a, **k: {"error": {"code": "session_unresolved", "message": "x"}},
        )
        result = relay_receive()
        assert "identity" not in result
