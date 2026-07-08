"""relay_subscribe（購読宣言）と relay_receive（inbox drain）の unit test。

subscribe の冪等性契約: 同一 labels 集合の再呼び出しは lease 有効なら既存を返し、
lease 失効・不明なら新規 subscribe して declaration file の id を差し替える。
"""
import json

import httpx
import pytest

from src.services.relay import declarations, inbox, service


@pytest.fixture(autouse=True)
def relay_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path / "relay-state"))
    monkeypatch.setenv("RELAY_BEARER_TOKEN", "test-token")
    monkeypatch.delenv("RELAY_BASE_URL", raising=False)
    monkeypatch.delenv("RELAY_IDENTITY", raising=False)


class SubscriptionStub:
    """POST /subscriptions を模し、採番と呼び出し記録を保持する。"""

    def __init__(self, lease_expires_at="2099-01-01T00:00:00Z"):
        self.lease_expires_at = lease_expires_at
        self.requests: list[dict] = []
        self.counter = 0

    def _dispatch(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/subscriptions"
        body = json.loads(request.content)
        self.requests.append(body)
        self.counter += 1
        return httpx.Response(
            201,
            json={
                "subscription_id": f"sub-{self.counter}",
                "lease_expires_at": self.lease_expires_at,
            },
        )

    def install(self, monkeypatch):
        def factory(base_url, **kwargs):
            return httpx.Client(
                transport=httpx.MockTransport(self._dispatch), base_url=base_url
            )

        monkeypatch.setattr(service, "make_client", factory)


class TestSubscribe:
    def test_first_subscribe_creates_declaration_entry(self, monkeypatch):
        stub = SubscriptionStub()
        stub.install(monkeypatch)

        result = service.relay_subscribe(["task:123"], caller_session_id="sess-1")
        assert result["subscription_id"] == "sub-1"
        assert result["reused"] is False

        decl = declarations.load("sess-1")
        assert len(decl["subscriptions"]) == 1
        entry = decl["subscriptions"][0]
        assert entry["subscription_id"] == "sub-1"
        assert f"handle:{decl['handle']}" in entry["labels"]
        assert entry["lease_expires_at"] == "2099-01-01T00:00:00Z"
        assert entry["created_at"]

    def test_subscriber_is_server_identity(self, monkeypatch):
        stub = SubscriptionStub()
        stub.install(monkeypatch)
        service.relay_subscribe(["task:123"], caller_session_id="sess-1")
        assert stub.requests[0]["subscriber"] == "cc-memory"

    def test_same_labels_with_active_lease_is_reused(self, monkeypatch):
        stub = SubscriptionStub()
        stub.install(monkeypatch)

        first = service.relay_subscribe(["a", "b"], caller_session_id="sess-1")
        second = service.relay_subscribe(["b", "a"], caller_session_id="sess-1")
        assert second["reused"] is True
        assert second["subscription_id"] == first["subscription_id"]
        assert stub.counter == 1  # relay への POST は 1 回だけ

    def test_expired_lease_resubscribes_and_replaces_id(self, monkeypatch):
        stub = SubscriptionStub(lease_expires_at="2020-01-01T00:00:00Z")
        stub.install(monkeypatch)

        first = service.relay_subscribe(["a"], caller_session_id="sess-1")
        assert first["subscription_id"] == "sub-1"

        second = service.relay_subscribe(["a"], caller_session_id="sess-1")
        assert second["reused"] is False
        assert second["subscription_id"] == "sub-2"

        decl = declarations.load("sess-1")
        assert len(decl["subscriptions"]) == 1
        assert decl["subscriptions"][0]["subscription_id"] == "sub-2"

    def test_empty_labels_subscribes_own_handle_only(self, monkeypatch):
        stub = SubscriptionStub()
        stub.install(monkeypatch)
        result = service.relay_subscribe([], caller_session_id="sess-1")
        handle = declarations.load("sess-1")["handle"]
        assert result["labels"] == [f"handle:{handle}"]

    def test_opaque_prefixes_are_accepted(self, monkeypatch):
        stub = SubscriptionStub()
        stub.install(monkeypatch)
        result = service.relay_subscribe(
            ["room:planning", "task:build", "custom:thing"],
            caller_session_id="sess-1",
        )
        assert "error" not in result

    def test_role_prefix_rejected(self, monkeypatch):
        stub = SubscriptionStub()
        stub.install(monkeypatch)
        result = service.relay_subscribe(
            ["role:navigator"], caller_session_id="sess-1"
        )
        assert result["error"]["code"] == "validation"
        assert stub.counter == 0

    @pytest.mark.parametrize(
        "entity_type",
        ["topic", "activity", "decision", "log", "material", "tag", "habit"],
    )
    def test_core_entity_prefix_is_accepted(self, monkeypatch, entity_type):
        """cc-memory の予約 namespace は relay_publish と異なり subscribe では許可される。

        entity 更新 → relay publish の labels（例: ["activity:1183", "event:updated"]）を
        購読するのに必要（material 522 D.5）。
        """
        stub = SubscriptionStub()
        stub.install(monkeypatch)
        result = service.relay_subscribe(
            ["task:build", f"{entity_type}:1"], caller_session_id="sess-1"
        )
        assert "error" not in result
        assert stub.counter == 1

    @pytest.mark.parametrize("meta_namespace", ["entity", "event"])
    def test_meta_namespace_is_accepted(self, monkeypatch, meta_namespace):
        stub = SubscriptionStub()
        stub.install(monkeypatch)
        result = service.relay_subscribe(
            [f"{meta_namespace}:decision"], caller_session_id="sess-1"
        )
        assert "error" not in result

    def test_entity_publish_subscribe_examples_from_spec(self, monkeypatch):
        """material 522 D.5 の購読例が通ることを確認する。"""
        stub = SubscriptionStub()
        stub.install(monkeypatch)
        for labels in (
            ["activity:1183", "event:updated"],
            ["domain:cc-memory", "entity:activity", "event:updated"],
            ["entity:decision", "event:retracted"],
        ):
            result = service.relay_subscribe(labels, caller_session_id="sess-1")
            assert "error" not in result, result

    def test_missing_token_returns_explicit_error(self, monkeypatch):
        stub = SubscriptionStub()
        stub.install(monkeypatch)
        monkeypatch.delenv("RELAY_BEARER_TOKEN")
        result = service.relay_subscribe(["a"], caller_session_id="sess-1")
        assert result["error"]["code"] == "config_missing"
        assert "RELAY_BEARER_TOKEN" in result["error"]["message"]
        assert stub.counter == 0

    def test_unresolved_session_returns_explicit_error(self, monkeypatch):
        stub = SubscriptionStub()
        stub.install(monkeypatch)
        result = service.relay_subscribe(["a"], caller_session_id=None)
        assert result["error"]["code"] == "session_unresolved"

    def test_relay_error_does_not_update_declaration(self, monkeypatch):
        def factory(base_url, **kwargs):
            def deny(request):
                return httpx.Response(
                    401, json={"code": "AUTHENTICATION_REQUIRED", "message": "無効"}
                )

            return httpx.Client(
                transport=httpx.MockTransport(deny), base_url=base_url
            )

        monkeypatch.setattr(service, "make_client", factory)
        result = service.relay_subscribe(["a"], caller_session_id="sess-1")
        assert "error" in result
        assert "401" in result["error"]["message"]
        decl = declarations.load("sess-1")
        assert decl is None or decl.get("subscriptions") == []

    def test_429_returns_rate_limited_code_with_retry_after(self, monkeypatch):
        """429 は relay_post と同様 `rate_limited` に分類され、retry_after が付与される。"""

        def factory(base_url, **kwargs):
            def rate_limited(request):
                return httpx.Response(
                    429,
                    json={"code": "RATE_LIMITED", "message": "しばらく待ってください"},
                    headers={"Retry-After": "3"},
                )

            return httpx.Client(
                transport=httpx.MockTransport(rate_limited), base_url=base_url
            )

        monkeypatch.setattr(service, "make_client", factory)
        result = service.relay_subscribe(["a"], caller_session_id="sess-1")
        assert result["error"]["code"] == "rate_limited"
        assert result["error"]["retry_after"] == 3.0


class TestReceive:
    def test_no_inbox_returns_empty_list(self):
        result = service.relay_receive(caller_session_id="sess-1")
        assert result == {"messages": [], "count": 0, "has_more": False}

    def test_drains_only_unread_records(self):
        inbox.append("sess-1", {"n": 1})
        inbox.append("sess-1", {"n": 2})
        first = service.relay_receive(caller_session_id="sess-1")
        assert [m["n"] for m in first["messages"]] == [1, 2]

        inbox.append("sess-1", {"n": 3})
        second = service.relay_receive(caller_session_id="sess-1")
        assert [m["n"] for m in second["messages"]] == [3]

    def test_limit_is_respected(self):
        for n in range(3):
            inbox.append("sess-1", {"n": n})
        result = service.relay_receive(limit=2, caller_session_id="sess-1")
        assert result["count"] == 2
        assert result["has_more"] is True

    def test_invalid_limit_rejected(self):
        result = service.relay_receive(limit=0, caller_session_id="sess-1")
        assert result["error"]["code"] == "validation"

    def test_bool_limit_rejected(self):
        """bool は int のサブクラスのため、明示チェックが無いと誤って通過する。"""
        result = service.relay_receive(limit=True, caller_session_id="sess-1")
        assert result["error"]["code"] == "validation"

    def test_unresolved_session_returns_explicit_error(self):
        result = service.relay_receive(caller_session_id=None)
        assert result["error"]["code"] == "session_unresolved"

    def test_other_sessions_inbox_is_not_visible(self):
        inbox.append("sess-other", {"n": 99})
        result = service.relay_receive(caller_session_id="sess-1")
        assert result["messages"] == []

    def test_default_limit_is_50(self):
        for n in range(60):
            inbox.append("sess-1", {"n": n})
        result = service.relay_receive(caller_session_id="sess-1")
        assert result["count"] == 50
        assert result["has_more"] is True

    def test_limit_above_max_is_clamped_to_200(self):
        for n in range(210):
            inbox.append("sess-1", {"n": n})
        result = service.relay_receive(limit=500, caller_session_id="sess-1")
        assert result["count"] == 200
        assert result["has_more"] is True

    def test_peek_does_not_consume(self):
        inbox.append("sess-1", {"n": 1})
        peeked = service.relay_receive(peek=True, caller_session_id="sess-1")
        assert [m["n"] for m in peeked["messages"]] == [1]

        again = service.relay_receive(peek=True, caller_session_id="sess-1")
        assert [m["n"] for m in again["messages"]] == [1]

    def test_peek_then_default_consumes(self):
        inbox.append("sess-1", {"n": 1})
        service.relay_receive(peek=True, caller_session_id="sess-1")
        consumed = service.relay_receive(caller_session_id="sess-1")
        assert [m["n"] for m in consumed["messages"]] == [1]

        after = service.relay_receive(caller_session_id="sess-1")
        assert after["messages"] == []

    def test_invalid_peek_type_rejected(self):
        result = service.relay_receive(peek="yes", caller_session_id="sess-1")
        assert result["error"]["code"] == "validation"
