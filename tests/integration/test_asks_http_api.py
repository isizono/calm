"""asks ダッシュボード向けHTTP API（/api/asks, /api/asks/{id}/answer）の統合テスト

MCPプロトコル外のプレーンHTTPエンドポイントを、ルーティングを経由せず対象の
custom_route関数を直接呼び出して検証する（tests/unit/test_health_endpoint.py と
同じ、Starlette Requestスタブ + asyncio.run による直接呼び出しパターン）。
"""
import asyncio
import json

from starlette.requests import Request

from src.main import http_answer_ask, http_get_asks
from src.services import ask_service as ak
from src.services.activity_service import add_activity


def _make_activity(title: str = "a1") -> int:
    return add_activity(title=title, description="d", tags=["domain:test"], check_in=False)[
        "activity_id"
    ]


def _make_get_request(query_string: bytes = b"") -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/asks",
        "headers": [],
        "query_string": query_string,
    }
    return Request(scope)


def _make_post_request(ask_id, body: bytes) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": f"/api/asks/{ask_id}/answer",
        "headers": [(b"content-type", b"application/json")],
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
