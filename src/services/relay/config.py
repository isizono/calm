"""relay 接続・状態ディレクトリの設定解決。

環境変数は呼び出し時に解決する（モジュール import 時に固定するとテスト・
プロセス寿命中の設定変更を拾えないため）。
"""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_BASE_URL = "http://localhost:8770"
DEFAULT_IDENTITY = "cc-memory"

_TOKEN_ENV = "RELAY_TOKEN"

TOKEN_MISSING_MESSAGE = (
    "RELAY_TOKEN が未設定のため relay に接続できません。"
    "relay サーバー側 RELAY_AUTH_TOKENS に登録済みの Bearer token を"
    "環境変数 RELAY_TOKEN に設定してください（例: export RELAY_TOKEN=<token>）。"
)


class RelayConfigError(RuntimeError):
    """relay 接続に必要な設定が不足しているときに raise される。"""


def get_base_url() -> str:
    """relay サーバーの base URL（env RELAY_BASE_URL、既定 http://localhost:8770）。"""
    return os.environ.get("RELAY_BASE_URL") or DEFAULT_BASE_URL


def get_identity() -> str:
    """relay に対する自 identity 名（env RELAY_IDENTITY、既定 cc-memory）。

    Bearer token が解決される identity 文字列と一致している必要がある
    （subscription の subscriber・stream の名前空間の両方がこの値でスコープされる）。
    """
    return os.environ.get("RELAY_IDENTITY") or DEFAULT_IDENTITY


def get_token() -> str | None:
    """Bearer token（env RELAY_TOKEN）。未設定なら None。"""
    return os.environ.get(_TOKEN_ENV) or None


def require_token() -> str:
    """Bearer token を返す。未設定なら設定方法を含む RelayConfigErrorを raise する。"""
    token = get_token()
    if not token:
        raise RelayConfigError(TOKEN_MISSING_MESSAGE)
    return token


def get_state_dir() -> Path:
    """ランタイム状態のルートディレクトリ（env RELAY_STATE_DIR、既定 ~/.cc-memory/relay）。"""
    raw = os.environ.get("RELAY_STATE_DIR")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".cc-memory" / "relay"


def subscriptions_dir() -> Path:
    """subscription declaration file の置き場。"""
    return get_state_dir() / "subscriptions"


def inbox_dir() -> Path:
    """per-session inbox（JSONL）と cursor file の置き場。"""
    return get_state_dir() / "inbox"
