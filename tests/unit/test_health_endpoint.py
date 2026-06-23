"""MCP server /health エンドポイントのユニットテスト

worker self-exit on MCP loss のための death judgement に使われる。
"""

import json
import asyncio

import pytest
from starlette.requests import Request

from src.main import health


@pytest.fixture
def fake_request():
    """最低限の Starlette Request スタブ。/health は body も query も使わない"""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/health",
        "headers": [],
        "query_string": b"",
    }
    return Request(scope)


class TestHealthEndpoint:
    def test_returns_status_ok(self, fake_request):
        response = asyncio.run(health(fake_request))
        assert response.status_code == 200
        body = json.loads(response.body)
        assert body["status"] == "ok"

    def test_includes_pid(self, fake_request):
        import os
        response = asyncio.run(health(fake_request))
        body = json.loads(response.body)
        assert body["pid"] == os.getpid()

    def test_includes_started_at_iso(self, fake_request):
        from datetime import datetime
        response = asyncio.run(health(fake_request))
        body = json.loads(response.body)
        # ISO 8601 としてパース可能であること
        datetime.fromisoformat(body["started_at"])

    def test_includes_nonneg_uptime(self, fake_request):
        response = asyncio.run(health(fake_request))
        body = json.loads(response.body)
        assert isinstance(body["uptime_sec"], int)
        assert body["uptime_sec"] >= 0
