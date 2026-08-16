"""asks ダッシュボード向けHTTP API（/api/asks, /api/asks/{id}/answer）の統合テスト

MCPプロトコル外のプレーンHTTPエンドポイントを、ルーティングを経由せず対象の
custom_route関数を直接呼び出して検証する（tests/unit/test_health_endpoint.py と
同じ、Starlette Requestスタブ + asyncio.run による直接呼び出しパターン）。

CORSプリフライト（OPTIONS）はcustom_route関数の直接呼び出しでは経由しない
ASGIミドルウェア層の挙動のため、_build_cors_middlewareが組み立てる設定を
実際にStarletteアプリへ適用し、starlette.testclient.TestClientで検証する。
"""
import asyncio
import json

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from src.main import _build_cors_middleware, http_answer_ask, http_get_asks
from src.services import ask_service as ak
from src.services.activity_service import add_activity


def _make_activity(title: str = "a1") -> int:
    return add_activity(title=title, description="d", tags=["domain:test"], check_in=False)[
        "activity_id"
    ]


def _make_get_request(query_string: bytes = b"", origin: str | None = None) -> Request:
    headers = [(b"origin", origin.encode())] if origin is not None else []
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/asks",
        "headers": headers,
        "query_string": query_string,
    }
    return Request(scope)


def _make_post_request(ask_id, body: bytes, origin: str | None = None) -> Request:
    headers = [(b"content-type", b"application/json")]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    scope = {
        "type": "http",
        "method": "POST",
        "path": f"/api/asks/{ask_id}/answer",
        "headers": headers,
        "query_string": b"",
        "path_params": {"ask_id": str(ask_id)},
    }

    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return Request(scope, receive)


class TestHttpGetAsks:
    def test_returns_open_asks_by_default(self, temp_db):
        act = _make_activity()
        ak.add_ask("should we do X?", tags=["domain:test"], blocks=[act])

        response = asyncio.run(http_get_asks(_make_get_request()))
        assert response.status_code == 200
        body = json.loads(response.body)
        assert body["total_count"] == 1
        assert body["asks"][0]["question"] == "should we do X?"
        assert body["asks"][0]["status"] == "open"

    def test_status_query_param_filters(self, temp_db):
        act = _make_activity()
        add_result = ak.add_ask("should we do X?", tags=["domain:test"], blocks=[act])
        ak.answer_ask(add_result["id"], "yes")

        response = asyncio.run(http_get_asks(_make_get_request(query_string=b"status=answered")))
        body = json.loads(response.body)
        assert body["total_count"] == 1
        assert body["asks"][0]["status"] == "answered"

        response_open = asyncio.run(http_get_asks(_make_get_request(query_string=b"status=open")))
        assert json.loads(response_open.body)["total_count"] == 0

    def test_invalid_status_returns_400(self, temp_db):
        response = asyncio.run(http_get_asks(_make_get_request(query_string=b"status=bogus")))
        assert response.status_code == 400
        body = json.loads(response.body)
        assert body["error"]["code"] == "VALIDATION_ERROR"

    def test_choices_included_in_response(self, temp_db):
        act = _make_activity()
        ak.add_ask("pick one", tags=["domain:test"], blocks=[act], choices=["A案", "B案"])

        response = asyncio.run(http_get_asks(_make_get_request()))
        body = json.loads(response.body)
        assert body["asks"][0]["choices"] == ["A案", "B案"]


class TestHttpAnswerAsk:
    def test_valid_answer_transitions_to_answered(self, temp_db):
        act = _make_activity()
        add_result = ak.add_ask("should we do X?", tags=["domain:test"], blocks=[act])
        ask_id = add_result["id"]

        response = asyncio.run(
            http_answer_ask(
                _make_post_request(ask_id, json.dumps({"answer_body": "yes, do it"}).encode())
            )
        )
        assert response.status_code == 200
        body = json.loads(response.body)
        assert body["status"] == "answered"
        assert body["id"] == ask_id

        listed = ak.get_asks(status="answered")
        assert listed["asks"][0]["answer_body"] == "yes, do it"

    def test_nonexistent_ask_id_returns_400(self, temp_db):
        response = asyncio.run(
            http_answer_ask(_make_post_request(999999, json.dumps({"answer_body": "yes"}).encode()))
        )
        assert response.status_code == 400
        body = json.loads(response.body)
        assert body["error"]["code"] == "VALIDATION_ERROR"

    def test_invalid_json_body_returns_400(self, temp_db):
        response = asyncio.run(http_answer_ask(_make_post_request(1, b"not json")))
        assert response.status_code == 400
        body = json.loads(response.body)
        assert body["error"]["code"] == "VALIDATION_ERROR"

    def test_non_object_json_body_returns_400(self, temp_db):
        act = _make_activity()
        add_result = ak.add_ask("should we do X?", tags=["domain:test"], blocks=[act])
        ask_id = add_result["id"]

        for payload in [b"null", b"[1,2,3]", b'"just a string"', b"42"]:
            response = asyncio.run(http_answer_ask(_make_post_request(ask_id, payload)))
            assert response.status_code == 400, payload
            body = json.loads(response.body)
            assert body["error"]["code"] == "VALIDATION_ERROR"

    def test_non_string_answer_body_returns_400(self, temp_db):
        act = _make_activity()
        add_result = ak.add_ask("should we do X?", tags=["domain:test"], blocks=[act])

        response = asyncio.run(
            http_answer_ask(
                _make_post_request(add_result["id"], json.dumps({"answer_body": 123}).encode())
            )
        )
        assert response.status_code == 400
        body = json.loads(response.body)
        assert body["error"]["code"] == "VALIDATION_ERROR"


class TestOriginCheck:
    """CSRF対策のOriginヘッダチェック（_check_origin）をGET/POST両エンドポイントで検証する。"""

    def test_get_allowed_origin_localhost_with_port_passes(self, temp_db):
        response = asyncio.run(
            http_get_asks(_make_get_request(origin="http://localhost:3000"))
        )
        assert response.status_code == 200

    def test_get_allowed_origin_127_0_0_1_passes(self, temp_db):
        response = asyncio.run(
            http_get_asks(_make_get_request(origin="http://127.0.0.1:8080"))
        )
        assert response.status_code == 200

    def test_get_disallowed_origin_returns_403(self, temp_db):
        response = asyncio.run(
            http_get_asks(_make_get_request(origin="https://evil.example.com"))
        )
        assert response.status_code == 403
        body = json.loads(response.body)
        assert body["error"]["code"] == "FORBIDDEN"

    def test_get_missing_origin_header_passes(self, temp_db):
        response = asyncio.run(http_get_asks(_make_get_request()))
        assert response.status_code == 200

    def test_post_allowed_origin_passes(self, temp_db):
        act = _make_activity()
        add_result = ak.add_ask("should we do X?", tags=["domain:test"], blocks=[act])

        response = asyncio.run(
            http_answer_ask(
                _make_post_request(
                    add_result["id"],
                    json.dumps({"answer_body": "yes"}).encode(),
                    origin="http://localhost:5173",
                )
            )
        )
        assert response.status_code == 200

    def test_post_disallowed_origin_returns_403_and_does_not_answer(self, temp_db):
        act = _make_activity()
        add_result = ak.add_ask("should we do X?", tags=["domain:test"], blocks=[act])

        response = asyncio.run(
            http_answer_ask(
                _make_post_request(
                    add_result["id"],
                    json.dumps({"answer_body": "yes"}).encode(),
                    origin="https://evil.example.com",
                )
            )
        )
        assert response.status_code == 403
        body = json.loads(response.body)
        assert body["error"]["code"] == "FORBIDDEN"

        # 403で拒否された時点でask_service.answer_askは呼ばれておらず、askはopenのまま
        listed = ak.get_asks(status="open")
        assert listed["asks"][0]["id_raw"] == add_result["id"]

    def test_post_missing_origin_header_passes(self, temp_db):
        act = _make_activity()
        add_result = ak.add_ask("should we do X?", tags=["domain:test"], blocks=[act])

        response = asyncio.run(
            http_answer_ask(
                _make_post_request(add_result["id"], json.dumps({"answer_body": "yes"}).encode())
            )
        )
        assert response.status_code == 200


def _make_cors_test_client() -> TestClient:
    """_build_cors_middlewareの設定を実際のStarletteアプリへ適用したテストクライアント。"""

    async def dummy_endpoint(request):
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[
            Route("/api/asks", dummy_endpoint, methods=["GET"]),
            Route("/api/asks/{ask_id}/answer", dummy_endpoint, methods=["POST"]),
        ],
        middleware=[_build_cors_middleware()],
    )
    return TestClient(app)


class TestCorsPreflight:
    """_build_cors_middlewareが組み立てるCORS設定をプリフライト（OPTIONS）で検証する。"""

    def test_preflight_from_allowed_origin_gets_cors_headers(self):
        client = _make_cors_test_client()
        response = client.options(
            "/api/asks",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
        assert "GET" in response.headers["access-control-allow-methods"]
        assert "POST" in response.headers["access-control-allow-methods"]

    def test_preflight_from_disallowed_origin_is_rejected(self):
        client = _make_cors_test_client()
        response = client.options(
            "/api/asks",
            headers={
                "Origin": "https://evil.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        # starlette CORSMiddlewareは許可外オリジンのプリフライトを400で拒否し、
        # Access-Control-Allow-Originヘッダを付与しない
        assert response.status_code == 400
        assert "access-control-allow-origin" not in response.headers

    def test_actual_request_from_disallowed_origin_omits_cors_header(self):
        client = _make_cors_test_client()
        response = client.get("/api/asks", headers={"Origin": "https://evil.example.com"})
        # CORSMiddlewareはブラウザ側のJS実行をブロックする仕組みであり、リクエスト自体は
        # サーバーに到達し200を返す。ブラウザはAllow-Originヘッダが無いレスポンスの内容を
        # スクリプトから読み取れずブロックする（アプリ層のOrigin拒否は_check_origin側が担う）
        assert response.status_code == 200
        assert "access-control-allow-origin" not in response.headers
