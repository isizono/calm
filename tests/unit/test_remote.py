"""remote.pyのユニットテスト"""
import os
import pytest
from unittest.mock import AsyncMock, patch

from src.remote import (
    _require_env,
    _parse_allowed_users,
    RestrictedGitHubProvider,
    REMOTE_PORT,
)


class TestRequireEnv:
    """_require_envの動作テスト"""

    def test_returns_value_when_set(self, monkeypatch):
        """環境変数が設定されている場合はその値を返す"""
        monkeypatch.setenv("TEST_KEY", "test_value")
        assert _require_env("TEST_KEY") == "test_value"

    def test_exits_when_unset(self):
        """環境変数が未設定の場合はSystemExitを送出する"""
        with pytest.raises(SystemExit, match="TEST_MISSING"):
            _require_env("TEST_MISSING")

    def test_exits_when_empty_string(self, monkeypatch):
        """環境変数が空文字列の場合もSystemExitを送出する"""
        monkeypatch.setenv("TEST_EMPTY", "")
        with pytest.raises(SystemExit, match="TEST_EMPTY"):
            _require_env("TEST_EMPTY")


class TestParseAllowedUsers:
    """_parse_allowed_usersのパース動作テスト"""

    def test_single_user(self):
        """単一ユーザーをパースできる"""
        assert _parse_allowed_users("alice") == frozenset({"alice"})

    def test_multiple_users(self):
        """カンマ区切りの複数ユーザーをパースできる"""
        assert _parse_allowed_users("alice,bob") == frozenset({"alice", "bob"})

    def test_strips_whitespace(self):
        """前後の空白を除去する"""
        assert _parse_allowed_users(" alice , bob ") == frozenset({"alice", "bob"})

    def test_lowercases(self):
        """ユーザー名を小文字に正規化する"""
        assert _parse_allowed_users("Alice,BOB") == frozenset({"alice", "bob"})

    def test_ignores_empty_entries(self):
        """空エントリを無視する"""
        assert _parse_allowed_users("alice,,bob,") == frozenset({"alice", "bob"})


class TestRestrictedGitHubProvider:
    """RestrictedGitHubProviderのユーザー制限テスト"""

    @pytest.mark.asyncio
    async def test_allows_permitted_user(self):
        """許可リスト内のユーザーはアクセスできる"""
        mock_token = AsyncMock()
        mock_token.claims = {"login": "alice"}

        with patch.object(
            GitHubProvider_base(), "verify_token", return_value=mock_token
        ) as mock_verify:
            provider = _make_provider({"alice"})
            provider.verify_token = _wrap_verify(mock_verify, provider)
            result = await provider.verify_token("dummy-token")
            assert result is not None
            assert result.claims["login"] == "alice"

    @pytest.mark.asyncio
    async def test_rejects_unpermitted_user(self):
        """許可リスト外のユーザーはNoneが返る"""
        mock_token = AsyncMock()
        mock_token.claims = {"login": "mallory"}

        with patch.object(
            GitHubProvider_base(), "verify_token", return_value=mock_token
        ) as mock_verify:
            provider = _make_provider({"alice", "bob"})
            provider.verify_token = _wrap_verify(mock_verify, provider)
            result = await provider.verify_token("dummy-token")
            assert result is None

    @pytest.mark.asyncio
    async def test_case_insensitive_matching(self):
        """ユーザー名の比較は大文字小文字を区別しない"""
        mock_token = AsyncMock()
        mock_token.claims = {"login": "Alice"}

        with patch.object(
            GitHubProvider_base(), "verify_token", return_value=mock_token
        ) as mock_verify:
            provider = _make_provider({"alice"})
            provider.verify_token = _wrap_verify(mock_verify, provider)
            result = await provider.verify_token("dummy-token")
            assert result is not None

    @pytest.mark.asyncio
    async def test_returns_none_when_parent_returns_none(self):
        """親クラスがNoneを返した場合はそのままNoneを返す"""
        with patch.object(
            GitHubProvider_base(), "verify_token", return_value=None
        ) as mock_verify:
            provider = _make_provider({"alice"})
            provider.verify_token = _wrap_verify(mock_verify, provider)
            result = await provider.verify_token("invalid-token")
            assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_login_missing(self):
        """claimsにloginキーがないトークンはNoneを返す"""
        mock_token = AsyncMock()
        mock_token.claims = {}

        with patch.object(
            GitHubProvider_base(), "verify_token", return_value=mock_token
        ) as mock_verify:
            provider = _make_provider({"alice"})
            provider.verify_token = _wrap_verify(mock_verify, provider)
            result = await provider.verify_token("dummy-token")
            assert result is None


class TestRemotePort:
    """REMOTE_PORTのデフォルト値テスト"""

    def test_default_port(self):
        """デフォルトポートは8001"""
        # importした時点の値をテスト（環境変数未設定時）
        assert REMOTE_PORT == int(os.environ.get("CALM_REMOTE_PORT", "8001"))


# --- ヘルパー ---

def GitHubProvider_base():
    """モック用のGitHubProviderインスタンス（実際のOAuth設定不要）"""
    from fastmcp.server.auth.providers.github import GitHubProvider
    return GitHubProvider


def _make_provider(allowed: set[str]) -> RestrictedGitHubProvider:
    """テスト用のRestrictedGitHubProviderを作成（__init__をバイパス）"""
    provider = object.__new__(RestrictedGitHubProvider)
    provider._allowed_users = frozenset(u.lower() for u in allowed)
    return provider


def _wrap_verify(mock_verify, provider):
    """verify_tokenをRestrictedGitHubProviderのロジックでラップする"""
    async def wrapped(token):
        access_token = await mock_verify(token)
        if access_token is None:
            return None
        login = access_token.claims.get("login")
        if login is None or login.lower() not in provider._allowed_users:
            return None
        return access_token
    return wrapped
