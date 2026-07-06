"""ow v1メッセージングサービス

relay HTTPサーバーとのやり取り（ow_send / ow_history）を担う。外部HTTPのため
cc-memory DBのconn共有パターンは不要。urllib.requestベース（サードパーティ依存なし）。

relayサーバー自体の起動は手動（`python -m src.relay.server`）で行う。
"""
import json
import logging
import os
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

logger = logging.getLogger(__name__)

# ----------------------------
# 設定定数
# ----------------------------

RELAY_URL = os.environ.get("RELAY_URL", "http://127.0.0.1:8765")

_MAX_RETRIES = 3

# ----------------------------
# relay HTTPヘルパー
# ----------------------------


def _relay_request(method: str, path: str, data: dict | None = None) -> dict:
    """relay HTTPリクエストの共通ヘルパー。4xx即失敗、5xx/接続断のみリトライ。

    Args:
        method: "GET" or "POST"
        path: relayエンドポイントパス（例: "/send", "/history?channel=...&since=0"）
        data: POSTボディ（Noneの場合はGET）

    Returns:
        レスポンスJSON dictまたは {"error": ...}
    """
    url = f"{RELAY_URL}{path}"
    body_bytes = json.dumps(data).encode("utf-8") if data is not None else None

    for attempt in range(_MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(
                url,
                data=body_bytes,
                headers={"Content-Type": "application/json"} if body_bytes else {},
                method=method,
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code < 500:
                # 4xx: クライアントエラー → 即失敗（リトライしない）
                try:
                    err_body = json.loads(e.read())
                except Exception:
                    err_body = {"message": str(e)}
                return {"error": {"code": e.code, "message": err_body}}
            if attempt >= _MAX_RETRIES:
                raise
            sleep_secs = 2 ** attempt
            logger.warning(
                "relay %s %s returned %d, retrying in %ds (%d/%d)",
                method, path, e.code, sleep_secs, attempt + 1, _MAX_RETRIES,
            )
            time.sleep(sleep_secs)
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            if attempt >= _MAX_RETRIES:
                raise
            sleep_secs = 2 ** attempt
            logger.warning(
                "relay %s %s failed: %s, retrying in %ds (%d/%d)",
                method, path, e, sleep_secs, attempt + 1, _MAX_RETRIES,
            )
            time.sleep(sleep_secs)

    # ここには到達しない（ループ内でraiseされる）
    raise RuntimeError("unreachable")


def ensure_channel(channel_code: str) -> bool:
    """channelが存在しなければrelayに作成する（idempotent）。

    POST /createにchannel_codeを指定して送信する。relayサーバー側で
    既存なら何もせず、未存在なら作成する。

    Args:
        channel_code: 存在を保証したいchannel_code

    Returns:
        True: channelが存在する（作成成功・既存どちらも）
        False: 作成失敗（4xx・5xx・接続断すべて含む）
    """
    try:
        result = _relay_request("POST", "/create", {"channel_code": channel_code})
    except Exception as e:
        logger.warning("ensure_channel failed for %s: %s", channel_code, e)
        return False
    if "error" in result:
        logger.warning("ensure_channel failed for %s: %s", channel_code, result["error"])
        return False
    logger.info("ensure_channel: channel %s is ready", channel_code)
    return True


# ----------------------------
# ow_send
# ----------------------------


def _maybe_inject_term_ref(body: dict) -> dict:
    """identity event の term_ref をファイルキャッシュから補完する。

    SessionStart hook (hooks/term_ref_cache.py) が worker shell の env を
    `~/.cc-memory/ow/term_refs/<session_id>.json` に書き出している前提。
    body が identity event で term_ref 未設定なら session_id でキャッシュを引いて補完する。

    lookup 失敗時は body をそのまま返す（補完失敗時は素通し）。
    元の body / data dict は破壊せず、補完時のみ shallow copy で新 dict を返す。
    """
    if not isinstance(body, dict) or body.get("kind") != "event":
        return body
    data = body.get("data")
    if not isinstance(data, dict):
        return body
    if data.get("type") != "identity":
        return body
    if data.get("term_ref"):
        return body
    session_id = data.get("session_id")
    if not session_id:
        return body
    cache_path = Path.home() / ".cc-memory" / "ow" / "term_refs" / f"{session_id}.json"
    try:
        with cache_path.open(encoding="utf-8") as f:
            cached = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return body
    term_ref = cached.get("term_ref") if isinstance(cached, dict) else None
    if not term_ref:
        return body
    new_data = dict(data)
    new_data["term_ref"] = term_ref
    new_body = dict(body)
    new_body["data"] = new_data
    return new_body


def ow_send(
    channel: str,
    handle: str,
    body: dict,
    needs_reply: bool = False,
    in_reply_to: int | None = None,
) -> dict:
    """ow channelにメッセージを送信する。

    bodyはow固有JSONを格納するdict（relay schemaは無改修）。
    4xx即失敗、5xx/接続断のみ3回指数バックオフ。
    channel未存在（404）の場合はensure_channelで自動作成してから再送する。

    Args:
        channel: channelコード
        handle: 送信者handle（例: "orch", "w-a"）
        body: ow固有JSON（{"v":1, "kind":"command"|"event", ...}）
        needs_reply: 返信を期待するか
        in_reply_to: 返信先のmsg_id

    Returns:
        成功時: {"msg_id": int}
        失敗時: {"error": {...}}
    """
    body = _maybe_inject_term_ref(body)
    payload: dict = {
        "channel": channel,
        "handle": handle,
        "body": json.dumps(body),
        "needs_reply": needs_reply,
    }
    if in_reply_to is not None:
        payload["in_reply_to"] = in_reply_to

    result = _relay_request("POST", "/send", payload)

    # channel未存在による404 → 自動作成して再送（1回のみ）
    if "error" in result and result["error"].get("code") == 404:
        logger.info("ow_send: channel %s not found, attempting ensure_channel", channel)
        if ensure_channel(channel):
            result = _relay_request("POST", "/send", payload)

    return result


# ----------------------------
# ow_history
# ----------------------------


def ow_history(channel: str, since: int = 0, limit: int = 100) -> dict:
    """GET /history。受信処理の本体。

    since自身を含まない（msg_id > since）。

    Args:
        channel: channelコード
        since: このmsg_idより大きいものを返す（0=全件）
        limit: 最大取得件数

    Returns:
        {"messages": [{"msg_id": int, "handle": str, "body": dict, ...}, ...]}
        失敗時: {"error": {...}}
    """
    params = urllib.parse.urlencode({"channel": channel, "since": since, "limit": limit})
    path = f"/history?{params}"
    result = _relay_request("GET", path)
    if "error" in result:
        return result

    # bodyがJSON文字列の場合はパースする
    messages = result.get("messages", [])
    for msg in messages:
        if isinstance(msg.get("body"), str):
            try:
                msg["body"] = json.loads(msg["body"])
            except (json.JSONDecodeError, TypeError):
                pass  # パース失敗はそのまま文字列で返す

    return {"messages": messages}
