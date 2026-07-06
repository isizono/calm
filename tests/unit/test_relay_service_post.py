"""relay_post（場への投函）の unit test。

relay の stream endpoint 群を httpx.MockTransport のスタブで模し、
未存在 stream の自動作成・作成競合リトライ・エラー伝播を検証する。
"""
import json

import httpx
import pytest

from src.services.relay import service


@pytest.fixture(autouse=True)
def relay_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path / "relay-state"))
    monkeypatch.setenv("RELAY_BEARER_TOKEN", "test-token")
    monkeypatch.delenv("RELAY_BASE_URL", raising=False)
    monkeypatch.delenv("RELAY_IDENTITY", raising=False)


class StubRelay:
    """(method, path) → handler の対応表で応答する relay スタブ。"""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.handlers: dict[tuple[str, str], object] = {}

    def _dispatch(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.handlers.get((request.method, request.url.path))
        if handler is None:
            return httpx.Response(
                500, json={"code": "UNEXPECTED", "message": "未定義の endpoint"}
            )
        return handler(request)

    def calls(self, method: str, path: str) -> list[httpx.Request]:
        return [
            r for r in self.requests if r.method == method and r.url.path == path
        ]

    def install(self, monkeypatch):
        def factory(base_url, **kwargs):
            return httpx.Client(
                transport=httpx.MockTransport(self._dispatch), base_url=base_url
            )

        monkeypatch.setattr(service, "make_client", factory)


STREAM_ID = "cc-memory:general"
MESSAGES_PATH = f"/streams/{STREAM_ID}/messages"
MEMBERS_PATH = f"/streams/{STREAM_ID}/members"


class TestAutoCreate:
    def test_missing_stream_is_created_and_post_succeeds(self, monkeypatch):
        stub = StubRelay()
        created = {"done": False}

        def post_message(request):
            if not created["done"]:
                return httpx.Response(
                    404, json={"code": "STREAM_NOT_FOUND", "message": "見つかりません"}
                )
            return httpx.Response(202, json={"publish_id": 7, "matched_members": 1})

        def create_stream(request):
            assert json.loads(request.content)["name"] == "general"
            created["done"] = True
            return httpx.Response(
                201, json={"stream_id": STREAM_ID, "created_at": "2026-07-05T00:00:00Z"}
            )

        def put_member(request):
            body = json.loads(request.content)
            assert body == {"identity": "cc-memory", "access": "read_write"}
            return httpx.Response(200, json={})

        stub.handlers[("POST", MESSAGES_PATH)] = post_message
        stub.handlers[("POST", "/streams")] = create_stream
        stub.handlers[("PUT", MEMBERS_PATH)] = put_member
        stub.install(monkeypatch)

        result = service.relay_post("general", "hello")
        assert result == {
            "stream_id": STREAM_ID,
            "publish_id": 7,
            "matched_members": 1,
        }
        assert len(stub.calls("POST", MESSAGES_PATH)) == 2
        assert len(stub.calls("POST", "/streams")) == 1
        assert len(stub.calls("PUT", MEMBERS_PATH)) == 1

    def test_concurrent_create_conflict_retries_post_once(self, monkeypatch):
        """作成が 409（同時競合）でも投函を 1 回リトライして成功する。"""
        stub = StubRelay()
        attempts = {"n": 0}

        def post_message(request):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(
                    404, json={"code": "STREAM_NOT_FOUND", "message": "見つかりません"}
                )
            return httpx.Response(202, json={"publish_id": 8, "matched_members": 2})

        stub.handlers[("POST", MESSAGES_PATH)] = post_message
        stub.handlers[("POST", "/streams")] = lambda request: httpx.Response(
            409, json={"code": "STREAM_ALREADY_EXISTS", "message": "既に存在します"}
        )
        stub.install(monkeypatch)

        result = service.relay_post("general", "hello")
        assert result["publish_id"] == 8
        assert attempts["n"] == 2
        # 409 時は member 変更を行わない（既存 stream の membership を触らない）
        assert stub.calls("PUT", MEMBERS_PATH) == []

    def test_existing_stream_posts_without_creation(self, monkeypatch):
        stub = StubRelay()
        stub.handlers[("POST", MESSAGES_PATH)] = lambda request: httpx.Response(
            202, json={"publish_id": 1, "matched_members": 0}
        )
        stub.install(monkeypatch)

        result = service.relay_post("general", "hello")
        assert result["publish_id"] == 1
        assert stub.calls("POST", "/streams") == []

    def test_ttl_is_forwarded(self, monkeypatch):
        stub = StubRelay()

        def post_message(request):
            assert json.loads(request.content) == {"body": "hello", "ttl": 120}
            return httpx.Response(202, json={"publish_id": 2, "matched_members": 0})

        stub.handlers[("POST", MESSAGES_PATH)] = post_message
        stub.install(monkeypatch)

        assert service.relay_post("general", "hello", ttl=120)["publish_id"] == 2


class TestValidation:
    def test_empty_body_is_rejected_before_any_http_call(self, monkeypatch):
        stub = StubRelay()
        stub.install(monkeypatch)
        result = service.relay_post("general", "")
        assert "error" in result
        assert "body" in result["error"]["message"]
        assert stub.requests == []

    def test_stream_name_with_separator_is_rejected(self, monkeypatch):
        stub = StubRelay()
        stub.install(monkeypatch)
        for name in ("a:b", "a/b", ""):
            assert "error" in service.relay_post(name, "hello")
        assert stub.requests == []

    def test_ttl_out_of_range_is_rejected_before_any_http_call(self, monkeypatch):
        stub = StubRelay()
        stub.install(monkeypatch)
        for bad_ttl in (59, 86401, 0, -1):
            result = service.relay_post("general", "hello", ttl=bad_ttl)
            assert result["error"]["code"] == "validation"
            assert "ttl" in result["error"]["message"]
        assert stub.requests == []

    def test_ttl_boundary_values_are_accepted(self, monkeypatch):
        stub = StubRelay()
        stub.handlers[("POST", MESSAGES_PATH)] = lambda request: httpx.Response(
            202, json={"publish_id": 3, "matched_members": 0}
        )
        stub.install(monkeypatch)
        for good_ttl in (60, 86400):
            assert (
                service.relay_post("general", "hello", ttl=good_ttl)["publish_id"] == 3
            )

    def test_ttl_non_int_is_rejected_before_any_http_call(self, monkeypatch):
        stub = StubRelay()
        stub.install(monkeypatch)
        for bad_ttl in ("120", 120.0, True):
            result = service.relay_post("general", "hello", ttl=bad_ttl)
            assert result["error"]["code"] == "validation"
            assert "ttl" in result["error"]["message"]
        assert stub.requests == []


class TestErrorPropagation:
    def test_missing_token_returns_explicit_error(self, monkeypatch):
        stub = StubRelay()
        stub.install(monkeypatch)
        monkeypatch.delenv("RELAY_BEARER_TOKEN")
        result = service.relay_post("general", "hello")
        assert result["error"]["code"] == "config_missing"
        assert "RELAY_BEARER_TOKEN" in result["error"]["message"]
        assert stub.requests == []

    def test_invalid_token_401_propagates(self, monkeypatch):
        stub = StubRelay()
        stub.handlers[("POST", MESSAGES_PATH)] = lambda request: httpx.Response(
            401, json={"code": "AUTHENTICATION_REQUIRED", "message": "token が無効です"}
        )
        stub.install(monkeypatch)
        result = service.relay_post("general", "hello")
        assert "error" in result
        assert "401" in result["error"]["message"]

    def test_closed_stream_410_propagates(self, monkeypatch):
        stub = StubRelay()
        stub.handlers[("POST", MESSAGES_PATH)] = lambda request: httpx.Response(
            410, json={"code": "STREAM_GONE", "message": "close 済みです"}
        )
        stub.install(monkeypatch)
        result = service.relay_post("general", "hello")
        assert "error" in result
        assert result["error"]["code"] == "STREAM_GONE"

    def test_connection_failure_returns_transient_error(self, monkeypatch):
        def factory(base_url, **kwargs):
            def raise_connect(request):
                raise httpx.ConnectError("connection refused")

            return httpx.Client(
                transport=httpx.MockTransport(raise_connect), base_url=base_url
            )

        monkeypatch.setattr(service, "make_client", factory)
        result = service.relay_post("general", "hello")
        assert result["error"]["code"] == "relay_unavailable"
