"""relay セッション面 4 動詞（post / publish / subscribe / receive）の実装本体。

- post: 場（stream）への投函。未存在 stream は自動作成して投函する
- publish: labels routing 配布。relay_outbox への INSERT で完結し、配達は
  server 内の常駐配達ループが担う（transactional outbox、at-least-once）
- subscribe: 購読宣言。declaration file と relay の POST /subscriptions を同期させる
- receive: 自 session の inbox を cursor から drain する（ローカル完結、relay を叩かない）

失敗は {"error": {"code", "message"}} で明示的に返す（silent fallback しない）。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from src.db import get_connection
from src.relay_sdk.errors import PermanentError, RelayProtocolError, TransientError
from src.relay_sdk.http.auth import make_client
from src.relay_sdk.http.request import post_subscription, raise_for_relay_status
from src.relay_sdk.outbox import publish as outbox_publish
from src.services.relay import config, declarations, inbox
from src.services.relay.config import RelayConfigError

logger = logging.getLogger(__name__)

_ROLE_PREFIX = "role:"
_HANDLE_PREFIX = "handle:"

# publish で outbox 行に載せる ref の種別。ref_id にメッセージ本文をそのまま格納し、
# 配達に必要な情報が outbox 行だけで閉じるようにする（別置きの本文ストアを作ると
# at-least-once の再配達時に本文が先に消えるモードが生まれるため持たない）。
MESSAGE_REF_TYPE = "message"

SESSION_UNRESOLVED_MESSAGE = (
    "呼び出し元の session_id を解決できません。"
    "MCP セッション経由で呼び出してください（session 外からの実行は非対応）。"
)


def _error(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def _relay_error(exc: Exception) -> dict:
    if isinstance(exc, RelayProtocolError):
        return _error(exc.code or "relay_protocol_error", str(exc))
    if isinstance(exc, TransientError):
        return _error("relay_unavailable", str(exc))
    if isinstance(exc, PermanentError):
        return _error("relay_gone", str(exc))
    return _error("relay_error", str(exc))


def _send(client: httpx.Client, method: str, url: str, **kwargs: Any) -> httpx.Response:
    """httpx リクエストを投げ、transport 由来のエラーを TransientError に翻訳する。"""
    try:
        return client.request(method, url, **kwargs)
    except httpx.TimeoutException as exc:
        raise TransientError(f"timeout: {exc}") from exc
    except httpx.TransportError as exc:
        raise TransientError(f"接続不能: {exc}") from exc


# ---------------------------------------------------------------------------
# labels 検証
# ---------------------------------------------------------------------------


def validate_labels(labels: Any, *, allow_empty: bool = False) -> Optional[str]:
    """labels の妥当性を検査し、問題があればエラーメッセージを返す（正常は None）。

    role: prefix のみ拒否する（廃止済み namespace）。handle:/channel:/task:・
    cc-memory 既存語彙・未知 prefix は不透明 label として受理する。
    """
    if not isinstance(labels, list):
        return "labels は文字列の配列で指定してください"
    if not labels and not allow_empty:
        return "labels は 1 個以上指定してください（宛先が決まらない発話は受け付けない）"
    for label in labels:
        if not isinstance(label, str) or not label:
            return "labels の各要素は非空文字列で指定してください"
        if label.startswith(_ROLE_PREFIX):
            return (
                f"label '{label}' は使用できません。"
                "role: namespace は廃止済みです（routing には handle:/channel:/task: を使う）"
            )
    return None


def _attach_handle(labels: list[str], handle: str) -> list[str]:
    """自 handle の routing label を付与する（既にあれば重複させない）。"""
    handle_label = f"{_HANDLE_PREFIX}{handle}"
    result = list(labels)
    if handle_label not in result:
        result.append(handle_label)
    return result


# ---------------------------------------------------------------------------
# post（場への投函）
# ---------------------------------------------------------------------------


def relay_post(stream_name: str, body: str, ttl: Optional[int] = None) -> dict:
    """stream にメッセージを投函する。未存在 stream は自動作成して 1 回だけ再試行する。"""
    if not isinstance(stream_name, str) or not stream_name:
        return _error("validation", "stream_name は非空文字列で指定してください")
    if ":" in stream_name or "/" in stream_name:
        return _error("validation", "stream_name に ':' と '/' は使用できません")
    if not isinstance(body, str) or not body:
        return _error("validation", "body は非空文字列で指定してください")

    try:
        token = config.require_token()
    except RelayConfigError as exc:
        return _error("config_missing", str(exc))

    identity = config.get_identity()
    stream_id = f"{identity}:{stream_name}"
    payload: dict[str, Any] = {"body": body}
    if ttl is not None:
        payload["ttl"] = ttl

    try:
        with make_client(config.get_base_url(), bearer_token=token) as client:
            response = _send(client, "POST", f"/streams/{stream_id}/messages", json=payload)
            if response.status_code == 404:
                # v0 は自 identity 名義の stream のみ扱うため、404 は「未作成」を意味する
                _ensure_stream(client, stream_name, stream_id, identity)
                response = _send(
                    client, "POST", f"/streams/{stream_id}/messages", json=payload
                )
            raise_for_relay_status(response)
            result = response.json()
    except (RelayProtocolError, TransientError, PermanentError) as exc:
        return _relay_error(exc)

    return {
        "stream_id": stream_id,
        "publish_id": result.get("publish_id"),
        "matched_members": result.get("matched_members", 0),
    }


def _ensure_stream(
    client: httpx.Client, stream_name: str, stream_id: str, identity: str
) -> None:
    """stream を作成し、自 identity を read_write member にする。

    同時作成競合（409）は「既に存在する」として成功扱いにする（呼び出し側が
    投函を 1 回だけ再試行する）。
    """
    response = _send(client, "POST", "/streams", json={"name": stream_name})
    if response.status_code == 409:
        return
    raise_for_relay_status(response)
    # 作成者の初期 access は write のみで、自分の投函を受信できない。
    # 投函と受信の両方を成立させるため read_write へ引き上げる。
    member = _send(
        client,
        "PUT",
        f"/streams/{stream_id}/members",
        json={"identity": identity, "access": "read_write"},
    )
    raise_for_relay_status(member)


# ---------------------------------------------------------------------------
# publish（labels routing 配布）
# ---------------------------------------------------------------------------


def publish_with_conn(
    conn,
    *,
    caller_session_id: Optional[str],
    labels: list[str],
    body: str,
    title: Optional[str] = None,
) -> dict:
    """relay_outbox へ 1 行 INSERT する（commit は呼び出し側の transaction に委ねる）。"""
    if not caller_session_id:
        return _error("session_unresolved", SESSION_UNRESOLVED_MESSAGE)
    message = validate_labels(labels)
    if message:
        return _error("validation", message)
    if not isinstance(body, str) or not body:
        return _error("validation", "body は非空文字列で指定してください")

    try:
        config.require_token()
    except RelayConfigError as exc:
        return _error("config_missing", str(exc))

    decl = declarations.ensure(caller_session_id)
    handle = decl["handle"]
    labels_final = _attach_handle(labels, handle)

    try:
        outbox_id = outbox_publish(
            conn,
            ref_type=MESSAGE_REF_TYPE,
            ref_id=body,
            labels=labels_final,
            title=title,
        )
    except ValueError as exc:
        return _error("validation", str(exc))

    return {"outbox_id": outbox_id, "labels": labels_final, "handle": handle}


def relay_publish(
    labels: list[str],
    body: str,
    title: Optional[str] = None,
    *,
    caller_session_id: Optional[str] = None,
) -> dict:
    """publish_with_conn の自前 connection 版（成功時に commit する）。"""
    conn = get_connection()
    try:
        result = publish_with_conn(
            conn,
            caller_session_id=caller_session_id,
            labels=labels,
            body=body,
            title=title,
        )
        if "error" not in result:
            conn.commit()
        return result
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# subscribe（購読宣言）
# ---------------------------------------------------------------------------


def relay_subscribe(
    labels: list[str], *, caller_session_id: Optional[str] = None
) -> dict:
    """labels の購読を宣言する。同一 labels 集合の再呼び出しは冪等。

    lease が有効な既存宣言はそのまま返し、失効・不明なら新規に subscribe して
    declaration file の subscription_id を差し替える。
    """
    if not caller_session_id:
        return _error("session_unresolved", SESSION_UNRESOLVED_MESSAGE)
    message = validate_labels(labels, allow_empty=True)
    if message:
        return _error("validation", message)

    try:
        token = config.require_token()
    except RelayConfigError as exc:
        return _error("config_missing", str(exc))

    decl = declarations.ensure(caller_session_id)
    handle = decl["handle"]
    labels_final = _attach_handle(labels, handle)

    existing = declarations.find_subscription(decl, labels_final)
    if existing and declarations.lease_active(existing):
        return {
            "subscription_id": existing["subscription_id"],
            "labels": sorted(set(labels_final)),
            "lease_expires_at": existing.get("lease_expires_at"),
            "handle": handle,
            "reused": True,
        }

    identity = config.get_identity()
    try:
        with make_client(config.get_base_url(), bearer_token=token) as client:
            created = post_subscription(
                client, subscriber=identity, labels=sorted(set(labels_final))
            )
    except (RelayProtocolError, TransientError, PermanentError) as exc:
        return _relay_error(exc)

    entry = {
        "subscription_id": created["subscription_id"],
        "labels": sorted(set(labels_final)),
        "lease_expires_at": created.get("lease_expires_at"),
        "created_at": declarations.now_iso(),
    }
    declarations.upsert_subscription(decl, entry)
    declarations.save(decl)

    return {
        "subscription_id": entry["subscription_id"],
        "labels": entry["labels"],
        "lease_expires_at": entry["lease_expires_at"],
        "handle": handle,
        "reused": False,
    }


# ---------------------------------------------------------------------------
# receive（inbox drain）
# ---------------------------------------------------------------------------


def relay_receive(
    limit: Optional[int] = None, *, caller_session_id: Optional[str] = None
) -> dict:
    """自 session の inbox を cursor から drain して返す。未読が無ければ空リスト。"""
    if not caller_session_id:
        return _error("session_unresolved", SESSION_UNRESOLVED_MESSAGE)
    if limit is not None and (not isinstance(limit, int) or limit <= 0):
        return _error("validation", "limit は 1 以上の整数で指定してください")

    messages = inbox.drain(caller_session_id, limit)
    return {"messages": messages, "count": len(messages)}
