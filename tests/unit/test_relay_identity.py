"""relay 呼び出し元の安定 identity 解決（src.services.relay.identity）のユニットテスト。

bridge identity ヘッダ優先 + ctx.session_id フォールバックの契約を検証する。
"""
from src.services.relay import identity as relay_identity


class TestGetRelayIdentity:
    def test_returns_header_value_when_present(self, monkeypatch):
        """ヘッダが存在する場合はその値を返し、ctx.session_idにはフォールバックしない"""
        monkeypatch.setattr(
            "fastmcp.server.dependencies.get_http_headers",
            lambda: {relay_identity.BRIDGE_SESSION_HEADER: "bridge-uuid-1"},
        )
        called = {"fallback": False}

        def fake_fallback():
            called["fallback"] = True
            return "ephemeral-session-id"

        monkeypatch.setattr(relay_identity, "get_caller_session_id", fake_fallback)
        assert relay_identity.get_relay_identity() == "bridge-uuid-1"
        assert called["fallback"] is False

    def test_falls_back_when_header_absent(self, monkeypatch):
        """ヘッダが無い場合は ctx.session_id（get_caller_session_id）にフォールバックする"""
        monkeypatch.setattr(
            "fastmcp.server.dependencies.get_http_headers", lambda: {}
        )
        monkeypatch.setattr(
            relay_identity, "get_caller_session_id", lambda: "ephemeral-session-id"
        )
        assert relay_identity.get_relay_identity() == "ephemeral-session-id"

    def test_falls_back_when_header_is_blank(self, monkeypatch):
        """ヘッダが空文字列・空白のみの場合もフォールバックする（安全側）"""
        monkeypatch.setattr(
            "fastmcp.server.dependencies.get_http_headers",
            lambda: {relay_identity.BRIDGE_SESSION_HEADER: "   "},
        )
        monkeypatch.setattr(
            relay_identity, "get_caller_session_id", lambda: "ephemeral-session-id"
        )
        assert relay_identity.get_relay_identity() == "ephemeral-session-id"

    def test_falls_back_when_get_http_headers_import_fails(self, monkeypatch):
        """get_http_headers自体のimportが失敗しても例外を投げずフォールバックする"""
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "fastmcp.server.dependencies":
                raise ImportError("simulated import failure")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        monkeypatch.setattr(
            relay_identity, "get_caller_session_id", lambda: "ephemeral-session-id"
        )
        assert relay_identity.get_relay_identity() == "ephemeral-session-id"

    def test_strips_whitespace_from_header_value(self, monkeypatch):
        """ヘッダ値の前後空白は取り除いて返す"""
        monkeypatch.setattr(
            "fastmcp.server.dependencies.get_http_headers",
            lambda: {relay_identity.BRIDGE_SESSION_HEADER: "  bridge-uuid-2  "},
        )
        assert relay_identity.get_relay_identity() == "bridge-uuid-2"
