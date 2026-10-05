"""MCP server /health エンドポイントのユニットテスト

worker self-exit on MCP loss のための death judgement に使われる。
"""

import asyncio
import json

import pytest
from starlette.requests import Request

from src.main import health, _version_id_for_root


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

    def test_includes_version_key(self, fake_request):
        """versionキー自体は常に含む（値はNoneの場合がある。_version_id_for_root参照）"""
        response = asyncio.run(health(fake_request))
        body = json.loads(response.body)
        assert "version" in body


class TestVersionIdForRoot:
    """版識別子の導出(_version_id_for_root)の分岐を検証する。"""

    def test_returns_directory_name_when_not_a_git_checkout(self, tmp_path):
        version_dir = tmp_path / "4a4a61c14dde"
        version_dir.mkdir()

        assert _version_id_for_root(version_dir) == "4a4a61c14dde"

    def test_returns_none_when_git_checkout(self, tmp_path):
        """gitチェックアウト(worktree含む)から直接実行している場合は版不明として扱う"""
        repo_root = tmp_path / "calm"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()

        assert _version_id_for_root(repo_root) is None
