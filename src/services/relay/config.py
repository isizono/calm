"""relay 接続・状態ディレクトリの設定解決。

環境変数は呼び出し時に解決する（モジュール import 時に固定するとテスト・
プロセス寿命中の設定変更を拾えないため）。

base_url / identity / token はいずれも env → credential.json → 既定 の順で
フォールバックする。env は override・break-glass 用に最優先を維持する。
credential.json は招待URL redeem（src/services/relay/redeem.py）が生成する。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_BASE_URL = "http://localhost:8770"
DEFAULT_IDENTITY = "cc-memory"

_TOKEN_ENV = "RELAY_BEARER_TOKEN"

TOKEN_MISSING_MESSAGE = (
    "relay の Bearer token が未設定のため接続できません。"
    "relay 側で招待URLを発行し（`python -m relay.invite new --identity cc-memory`）、"
    "cc-memory 側で `python -m src.services.relay.redeem` に招待URLを渡して"
    "credential を取得してください（標準入力に招待URLを1行渡す）。"
    "環境変数 RELAY_BEARER_TOKEN の直接設定は break-glass 用の代替経路です。"
)


class RelayConfigError(RuntimeError):
    """relay 接続に必要な設定が不足しているときに raise される。"""


def _read_credential_file() -> dict | None:
    """credential.json を読む。欠落・壊れは None（fail-safe）。"""
    path = get_state_dir() / "credential.json"
    try:
        with path.open() as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def get_base_url() -> str:
    """relay サーバーの base URL（env RELAY_BASE_URL → credential.json → 既定）。"""
    env = os.environ.get("RELAY_BASE_URL")
    if env:
        return env
    cred = _read_credential_file()
    if cred and cred.get("base_url"):
        return str(cred["base_url"])
    return DEFAULT_BASE_URL


def get_identity() -> str:
    """relay に対する自 identity 名（env RELAY_IDENTITY → credential.json → 既定）。

    Bearer token が解決される identity 文字列と一致している必要がある
    （subscription の subscriber・stream の名前空間の両方がこの値でスコープされる）。
    """
    env = os.environ.get("RELAY_IDENTITY")
    if env:
        return env
    cred = _read_credential_file()
    if cred and cred.get("identity"):
        return str(cred["identity"])
    return DEFAULT_IDENTITY


def get_token() -> str | None:
    """Bearer token（env RELAY_BEARER_TOKEN → credential.json → None）。

    env は override / break-glass として最優先する。
    """
    env = os.environ.get(_TOKEN_ENV)
    if env:
        return env
    cred = _read_credential_file()
    if cred and cred.get("bearer_token"):
        return str(cred["bearer_token"])
    return None


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
