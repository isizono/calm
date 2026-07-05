"""AgentCard 読み込み + Bearer / JWS 認証（relay-v2-sdk.md §4.3）。

SDK が relay へ送る `Authorization` ヘッダを組み立てる。認証情報の解決順序:

1. 明示引数 `bearer_token`
2. 環境変数 `RELAY_BEARER_TOKEN`
3. `jws_key_path`（ES256 私鍵）から JWS を生成した Bearer token

relay v2 の最小セット authN（identity-authz.md §1.5.1 / relay `identity.py`）は
`Authorization: Bearer <token>` の静的照合であり、現行 relay 実装は JWS Bearer を
consume しない。JWS 署名（`sign_jws`）は A2A 準拠を見据えた MAY 機能として実装するが、
`make_client` の既定経路は plain Bearer token を使う。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx


def load_agent_card(agent_card_path: str | Path | None) -> dict[str, Any] | None:
    """AgentCard JSON を読み込む。パスが None なら None。"""
    if agent_card_path is None:
        return None
    return json.loads(Path(agent_card_path).read_text(encoding="utf-8"))


def resolve_bearer_token(
    *,
    bearer_token: str | None = None,
    jws_key_path: str | Path | None = None,
    agent_card: dict[str, Any] | None = None,
    subscriber_identity: str | None = None,
) -> str | None:
    """Bearer token を解決する（§4.3 の優先順位）。

    `jws_key_path` 指定時は ES256 JWS を生成して token にする。いずれも解決できなければ
    None（認証ヘッダなし。FakeRelay など authN を強制しない相手向け）。
    """
    if bearer_token:
        return bearer_token
    env_token = os.environ.get("RELAY_BEARER_TOKEN")
    if env_token:
        return env_token
    if jws_key_path is not None:
        return sign_jws(
            private_key_pem=Path(jws_key_path).read_text(encoding="utf-8"),
            agent_card=agent_card,
            subject=subscriber_identity,
        )
    return None


def build_auth_headers(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def make_client(
    base_url: str,
    *,
    bearer_token: str | None = None,
    jws_key_path: str | Path | None = None,
    agent_card_path: str | Path | None = None,
    subscriber_identity: str | None = None,
    timeout: float = 10.0,
) -> httpx.Client:
    """auth ヘッダを既定装備した `httpx.Client` を組み立てる。

    `timeout` は connect/read/write/pool の全軸に適用する。通常の HTTP request
    （`POST /publish` 等）が無応答のまま永久ブロックしないようにするためで、read
    timeout をここで無効化しない。SSE stream だけは通常より長い無音期間が正常
    （keepalive 間隔ぶん）なので、read timeout はこの client 全体ではなく
    `open_sse()` 呼び出し単位（`read_timeout` 引数）で個別に上書きする
    （read timeout を client 全体で無効化すると、通常 request まで応答が返らない
    ケースで永久ブロックしうる）。
    """
    agent_card = load_agent_card(agent_card_path)
    token = resolve_bearer_token(
        bearer_token=bearer_token,
        jws_key_path=jws_key_path,
        agent_card=agent_card,
        subscriber_identity=subscriber_identity,
    )
    headers = build_auth_headers(token)
    return httpx.Client(base_url=base_url, headers=headers, timeout=timeout)


# ---------------------------------------------------------------------------
# JWS 署名 / 検証（MAY、§4.3）
# ---------------------------------------------------------------------------


def sign_jws(
    *,
    private_key_pem: str,
    agent_card: dict[str, Any] | None = None,
    subject: str | None = None,
) -> str:
    """ES256 JWS Compact 署名を生成する（pyjwt 使用、§4.3）。

    payload は最小限（`sub` に subscriber identity、`iat`）。relay 側が JWS Bearer を
    consume する実装に移行した際の相互運用を見据えた雛形であり、現行 relay の静的
    Bearer 照合には使われない。
    """
    import time

    import jwt

    payload: dict[str, Any] = {"iat": int(time.time())}
    if subject:
        payload["sub"] = subject
    if agent_card and agent_card.get("name"):
        payload["iss"] = agent_card["name"]
    return jwt.encode(payload, private_key_pem, algorithm="ES256")


def verify_relay_agent_card(card: dict[str, Any], *, public_key_pem: str) -> bool:
    """relay 側 AgentCard の JWS 署名を検証する（§4.3、MAY）。

    relay `identity.verify_agent_card_signature` と対称の JCS(detached) 形式に対応する。
    署名が無い / 検証失敗なら False。
    """
    import base64

    import rfc8785
    from joserfc import jws as joserfc_jws
    from joserfc.jwk import ECKey

    signatures = card.get("signatures")
    if not signatures:
        return False
    sig = signatures[0]
    stripped = _strip_default_values({k: v for k, v in card.items() if k != "signatures"})
    payload = rfc8785.dumps(stripped)
    payload_b64 = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    compact = f"{sig['protected']}.{payload_b64}.{sig['signature']}"
    try:
        result = joserfc_jws.deserialize_compact(compact, ECKey.import_key(public_key_pem))
    except Exception:
        return False
    return result.payload == payload


def _strip_default_values(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for k, v in value.items():
            if v is None or v is False or v == "" or v == [] or v == {}:
                continue
            result[k] = _strip_default_values(v)
        return result
    if isinstance(value, list):
        return [_strip_default_values(v) for v in value]
    return value
