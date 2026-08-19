"""リモートMCPサーバー（GitHub OAuth認証付き）

mount方式でmain.pyのmcpインスタンスを取り込み、
GitHub OAuth認証付きの別サーバーとして起動する。
internal版（main.py）への変更はゼロ。

環境変数:
    GITHUB_CLIENT_ID: GitHub OAuth AppのClient ID
    GITHUB_CLIENT_SECRET: GitHub OAuth AppのClient Secret
    CALM_BASE_URL: 公開URL（例: https://cc-memory.example.com）
    CALM_ALLOWED_USERS: 許可するGitHubユーザー名（カンマ区切り、例: "alice,bob"）
    CALM_REMOTE_PORT: リモートサーバーのポート（デフォルト: 8001）

    CALM_ 系は旧名（CCM_ / CC_MEMORY_）へのフォールバックが効く（src/env_compat.py）。
"""
import logging
import os

from fastmcp import FastMCP
from fastmcp.server.auth.providers.github import GitHubProvider

from src.db import init_database, verify_sqlite_vec
from src.env_compat import env_get, env_names
from src.main import mcp as internal_mcp

logger = logging.getLogger(__name__)

REMOTE_PORT = int(env_get("CALM_REMOTE_PORT", "8001"))


def _require_env(key: str) -> str:
    value = os.environ.get(key)
    if not value:
        raise SystemExit(f"環境変数 {key} が未設定です")
    return value


def _require_calm_env(name: str) -> str:
    """CALM_ 系の環境変数を旧名フォールバック込みで必須読みする。"""
    value = env_get(name)
    if not value:
        candidates = " / ".join(env_names(name))
        raise SystemExit(f"環境変数 {candidates} がいずれも未設定です")
    return value


def _parse_allowed_users(raw: str) -> frozenset[str]:
    return frozenset(u.strip().lower() for u in raw.split(",") if u.strip())


class RestrictedGitHubProvider(GitHubProvider):
    """許可リストに基づいてGitHubユーザーを制限するプロバイダー"""

    def __init__(self, *, allowed_users: frozenset[str], **kwargs):
        super().__init__(**kwargs)
        self._allowed_users = allowed_users

    async def verify_token(self, token: str):
        access_token = await super().verify_token(token)
        if access_token is None:
            return None
        login = access_token.claims.get("login")
        if login is None or login.lower() not in self._allowed_users:
            logger.warning("Rejected user: %s (not in allowed_users)", login)
            return None
        return access_token


def create_remote_server() -> FastMCP:
    """認証付きリモートサーバーを作成する"""
    allowed_users = _parse_allowed_users(_require_calm_env("CALM_ALLOWED_USERS"))

    auth = RestrictedGitHubProvider(
        allowed_users=allowed_users,
        client_id=_require_env("GITHUB_CLIENT_ID"),
        client_secret=_require_env("GITHUB_CLIENT_SECRET"),
        base_url=_require_calm_env("CALM_BASE_URL"),
    )

    remote = FastMCP("calm-remote", auth=auth)
    remote.mount(internal_mcp)
    return remote


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    verify_sqlite_vec()
    init_database()

    server = create_remote_server()
    logger.info(f"Starting remote server on port {REMOTE_PORT}")
    server.run(transport="streamable-http", host="127.0.0.1", port=REMOTE_PORT)
