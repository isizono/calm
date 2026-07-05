"""protocol 層の HTTP リクエスト組み立て（relay-v2-sdk.md §4.1, §4.4）。

publisher / subscriber 両側から内部利用される薄い httpx ラッパ。各関数は
`httpx.Client` を受け取り、relay の該当 endpoint を叩いて `relay-v2-wire-api.md` の
status code を §4.4 の 3 例外へ翻訳する。

| 関数 | endpoint | 呼び出し元 |
|---|---|---|
| `post_publish`        | `POST /publish`                        | dispatcher |
| `post_subscription`   | `POST /subscriptions`                  | subscribe() |
| `put_lease`           | `PUT /subscriptions/{id}/lease`        | Subscription 裏側 |
| `delete_subscription` | `DELETE /subscriptions/{id}`           | Subscription.close() |
| `post_ack`            | `POST /subscriptions/{id}/ack`         | Subscription.ack() |
| `open_sse`            | `GET /events?subscription_ids=...`     | Subscription.receive() |
"""
from __future__ import annotations

from typing import Any, Sequence

import httpx

from src.relay_sdk.errors import PermanentError, RelayProtocolError, TransientError


def _error_code(response: httpx.Response) -> tuple[str | None, str]:
    """error envelope（{code, message}）を best-effort で取り出す。"""
    try:
        body = response.json()
    except Exception:
        return None, response.text[:200]
    if isinstance(body, dict):
        return body.get("code"), body.get("message", "")
    return None, ""


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def raise_for_relay_status(
    response: httpx.Response, *, subscription_scoped: bool = False
) -> None:
    """relay 応答の status code を §4.4 の例外に翻訳する。

    Args:
        subscription_scoped: 既存 subscription_id を参照する操作なら True。この場合
            `404` / `410` は「subscription 失効」を意味し `PermanentError` になる
            （wire-api.md §5.7）。False（`POST /publish` 等）では `404` は caller の
            リクエスト誤りとして `RelayProtocolError`。
    """
    status = response.status_code
    if status < 400:
        return
    code, message = _error_code(response)
    detail = f"HTTP {status}" + (f" {code}" if code else "") + (f": {message}" if message else "")

    if status == 429:
        raise TransientError(detail, status_code=status, retry_after=_retry_after(response))
    if subscription_scoped and status in (404, 410):
        raise PermanentError(detail, status_code=status)
    if 400 <= status < 500:
        raise RelayProtocolError(detail, status_code=status, code=code)
    raise TransientError(detail, status_code=status)


def _request(client: httpx.Client, method: str, url: str, **kwargs: Any) -> httpx.Response:
    """httpx リクエストを投げ、transport 由来のエラーを `TransientError` に翻訳する。"""
    try:
        return client.request(method, url, **kwargs)
    except httpx.TimeoutException as exc:
        raise TransientError(f"timeout: {exc}") from exc
    except httpx.TransportError as exc:  # ConnectError / RemoteProtocolError 等
        raise TransientError(f"接続不能: {exc}") from exc


# ---------------------------------------------------------------------------
# publisher / subscriber 両用の request 関数
# ---------------------------------------------------------------------------


def post_publish(
    client: httpx.Client,
    *,
    ref: dict[str, Any],
    labels: Sequence[str],
    title: str | None,
    idempotency_key: str,
) -> dict[str, Any]:
    """`POST /publish`（subscription レーン publish）。dispatcher が呼ぶ。"""
    body: dict[str, Any] = {
        "ref": ref,
        "labels": list(labels),
        "idempotency_key": idempotency_key,
    }
    if title is not None:
        body["title"] = title
    response = _request(client, "POST", "/publish", json=body)
    raise_for_relay_status(response)
    return response.json()


def post_subscription(
    client: httpx.Client,
    *,
    subscriber: str,
    labels: Sequence[str],
    lease_ttl: int | None = None,
    retain_seconds: int | None = None,
) -> dict[str, Any]:
    """`POST /subscriptions`（subscribe）。`{subscription_id, lease_expires_at}` を返す。"""
    body: dict[str, Any] = {"subscriber": subscriber, "labels": list(labels)}
    if lease_ttl is not None:
        body["lease_ttl"] = lease_ttl
    if retain_seconds is not None:
        body["delivery_options"] = {"retain_seconds": retain_seconds}
    response = _request(client, "POST", "/subscriptions", json=body)
    raise_for_relay_status(response)
    return response.json()


def put_lease(
    client: httpx.Client, *, subscription_id: str, lease_ttl: int | None = None
) -> dict[str, Any]:
    """`PUT /subscriptions/{id}/lease`（lease renew）。`{lease_expires_at}` を返す。"""
    body: dict[str, Any] = {}
    if lease_ttl is not None:
        body["lease_ttl"] = lease_ttl
    response = _request(
        client, "PUT", f"/subscriptions/{subscription_id}/lease", json=body
    )
    raise_for_relay_status(response, subscription_scoped=True)
    return response.json()


def delete_subscription(client: httpx.Client, *, subscription_id: str) -> None:
    """`DELETE /subscriptions/{id}`（unsubscribe）。"""
    response = _request(client, "DELETE", f"/subscriptions/{subscription_id}")
    raise_for_relay_status(response, subscription_scoped=True)


def post_ack(
    client: httpx.Client, *, subscription_id: str, up_to_publish_id: int
) -> None:
    """`POST /subscriptions/{id}/ack`（cumulative ack）。"""
    response = _request(
        client,
        "POST",
        f"/subscriptions/{subscription_id}/ack",
        json={"up_to_publish_id": up_to_publish_id},
    )
    raise_for_relay_status(response, subscription_scoped=True)


def open_sse(
    client: httpx.Client, *, subscription_ids: Sequence[str], read_timeout: float | None = None
):
    """`GET /events?subscription_ids=...` を SSE stream として開く。

    `httpx.Client.stream(...)` の context manager を返す（呼び出し側が `with` で使う）。
    status 検証は stream に入ってから `raise_for_sse_status` で行う（stream 前に
    body を読めないため）。

    Args:
        read_timeout: この request だけに適用する read timeout（秒）。省略時は
            `client` の既定 timeout をそのまま使う。SSE stream は通常の HTTP request
            より無音期間が長くなりうる（keepalive 間隔ぶん、§4.2）ため、呼び出し側
            （`Subscription`）が keepalive 間隔の倍数を明示的に渡す。read timeout を
            `client` 全体で無効化すると通常の HTTP request（`POST /publish` 等）まで
            無応答時に永久ブロックしうるため、上書きは SSE request 単位に限定する。
    """
    params = {"subscription_ids": ",".join(subscription_ids)}
    kwargs: dict[str, Any] = {}
    if read_timeout is not None:
        base = client.timeout
        kwargs["timeout"] = httpx.Timeout(
            connect=base.connect, read=read_timeout, write=base.write, pool=base.pool
        )
    return client.stream(
        "GET", "/events", params=params, headers={"Accept": "text/event-stream"}, **kwargs
    )


def raise_for_sse_status(response: httpx.Response) -> None:
    """`GET /events` stream の status を検証する（`subscription_scoped=True`）。

    エラー時は body を読んでから翻訳する（stream レスポンスは明示 read が必要）。
    """
    if response.status_code >= 400:
        response.read()
    raise_for_relay_status(response, subscription_scoped=True)
