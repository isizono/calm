"""server 内常駐 B-1: SSE 受信 → session inbox 振り分け → ack。

server identity 名義で発行済みの全 subscription を単一 SSE 接続で多重受信する。
relay の SSE stream は subscription レーンと stream レーン（`room:<name>` に相当する
場）の両方を相乗り配達する（`GET /events?subscription_ids=...`）。SDK 付属の
`Subscription.receive()` は `delivery_target` が `sub:` 始まりでないフレームを黙って
捨てるため、場レーンの受信は SDK の subscribe() 経路では扱えない。ここでは SDK の
http 層（`open_sse` / SSE parser）だけを利用し、フレーム振り分け・ack・再接続を
自前で組み立てる。

振り分け規則（labels 再マッチングは一切行わない）:
- subscription レーン（`delivery_target=sub:<subscription_id>`）:
  declaration file を逆引きして、その subscription_id を宣言している session の
  inbox にのみ書く。ack は `POST /subscriptions/{id}/ack`（cumulative）で、宛先の
  identity（= server identity）に紐づく。
- stream レーン（`delivery_target=stream:<identity>:<stream_name>`）:
  `room:<stream_name>` label を含む subscription を宣言した全 session の inbox に
  書く。宛先ゼロならフレームは捨て、debug ログを 1 行だけ残す（at-least-once の
  対象は「購読宣言のある宛先」に限る）。ack は `POST /streams/{stream_id}/ack`
  （cumulative、identity 単位）で outbox から掃く。

順序契約: inbox 追記 → fsync → ack、の順を厳守する。追記に失敗した場合は ack を
打たず、次回接続で再配達させる（配達契約は at-least-once。逆順は喪失モードを
作る）。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

import httpx

from relay_sdk import config as sdk_config
from relay_sdk.client.sse import parse_sse_byte_stream
from relay_sdk.errors import PermanentError, RelayProtocolError, TransientError
from relay_sdk.http.auth import make_client
from relay_sdk.http.request import open_sse, post_ack, raise_for_sse_status
from src.services.relay import config, declarations, inbox

logger = logging.getLogger(__name__)


_STREAM_PREFIX = "stream:"
_SUB_PREFIX = "sub:"
_ROOM_LABEL_PREFIX = "room:"


# ---------------------------------------------------------------------------
# 振り分け純ロジック（SSE 接続なしで単体テスト可能）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubFrame:
    subscription_id: str
    publish_id: int
    payload: dict


@dataclass(frozen=True)
class StreamFrame:
    stream_id: str        # relay wire 上の canonical id（`identity:name` そのまま）
    stream_name: str      # 上の `name` 部分（`room:<name>` 逆引き用）
    publish_id: int
    payload: dict


def parse_frame(data: dict) -> Optional[object]:
    """SSE `notification` frame の payload を SubFrame / StreamFrame に分類する。

    JSON 型不整合 / 必須フィールド欠落 / publish_id が int でない frame は None を
    返す（受信ループはその 1 件を skip して継続する）。
    """
    if not isinstance(data, dict):
        return None
    target = data.get("delivery_target")
    publish_id = data.get("publish_id")
    if not isinstance(target, str):
        return None
    if not isinstance(publish_id, int) or isinstance(publish_id, bool):
        return None
    if target.startswith(_SUB_PREFIX):
        subscription_id = target[len(_SUB_PREFIX):]
        if not subscription_id:
            return None
        return SubFrame(subscription_id=subscription_id, publish_id=publish_id, payload=data)
    if target.startswith(_STREAM_PREFIX):
        stream_id = target[len(_STREAM_PREFIX):]
        if not stream_id or ":" not in stream_id:
            return None
        _, stream_name = stream_id.split(":", 1)
        if not stream_name:
            return None
        return StreamFrame(
            stream_id=stream_id,
            stream_name=stream_name,
            publish_id=publish_id,
            payload=data,
        )
    return None


def resolve_sub_owner(declarations_snapshot: list[dict], subscription_id: str) -> Optional[str]:
    """subscription_id を宣言している session_id を返す。無ければ None。"""
    for decl in declarations_snapshot:
        for entry in decl.get("subscriptions", []):
            if entry.get("subscription_id") == subscription_id:
                sid = decl.get("session_id")
                if isinstance(sid, str):
                    return sid
    return None


def resolve_stream_targets(
    declarations_snapshot: list[dict], stream_name: str
) -> list[str]:
    """`room:<stream_name>` label を含む subscription を宣言した session_id の一覧。"""
    target_label = f"{_ROOM_LABEL_PREFIX}{stream_name}"
    result: list[str] = []
    for decl in declarations_snapshot:
        session_id = decl.get("session_id")
        if not isinstance(session_id, str):
            continue
        for entry in decl.get("subscriptions", []):
            if target_label in entry.get("labels", []):
                result.append(session_id)
                break
    return result


# ---------------------------------------------------------------------------
# ack 状態管理（cumulative の上限値を持つ）
# ---------------------------------------------------------------------------


@dataclass
class AckTracker:
    """subscription / stream それぞれの cumulative ack 上限（publish_id）を保持する。

    frame 追記成功のたびに `mark_sub()` / `mark_stream()` で更新し、`flush()` で
    まだ届いていない ack を relay に送る。ack 送信が失敗しても値は保持し、次の
    flush 契機で再送する（at-least-once 契約）。
    """

    sub_pending: dict[str, int] = field(default_factory=dict)
    stream_pending: dict[str, int] = field(default_factory=dict)

    def mark_sub(self, subscription_id: str, publish_id: int) -> None:
        current = self.sub_pending.get(subscription_id)
        if current is None or publish_id > current:
            self.sub_pending[subscription_id] = publish_id

    def mark_stream(self, stream_id: str, publish_id: int) -> None:
        current = self.stream_pending.get(stream_id)
        if current is None or publish_id > current:
            self.stream_pending[stream_id] = publish_id

    def flush(self, client: httpx.Client) -> None:
        for subscription_id, publish_id in list(self.sub_pending.items()):
            try:
                post_ack(
                    client,
                    subscription_id=subscription_id,
                    up_to_publish_id=publish_id,
                )
                self.sub_pending.pop(subscription_id, None)
            except (TransientError, PermanentError, RelayProtocolError) as exc:
                logger.warning(
                    "sub ack 送信に失敗（次回接続で再送）: subscription_id=%s publish_id=%s error=%s",
                    subscription_id,
                    publish_id,
                    exc,
                )
        for stream_id, publish_id in list(self.stream_pending.items()):
            try:
                _post_stream_ack(client, stream_id, publish_id)
                self.stream_pending.pop(stream_id, None)
            except (TransientError, PermanentError, RelayProtocolError) as exc:
                logger.warning(
                    "stream ack 送信に失敗（次回接続で再送）: stream_id=%s publish_id=%s error=%s",
                    stream_id,
                    publish_id,
                    exc,
                )


def _post_stream_ack(client: httpx.Client, stream_id: str, up_to_publish_id: int) -> None:
    """`POST /streams/{id}/ack`（cumulative、identity 単位）。

    404 は「member 権限を失った / stream 消滅」を意味するが、いずれも次回接続時
    に整合が取れるため subscription_scoped=True 相当の PermanentError に翻訳し、
    上位（`AckTracker.flush`）で握り潰させる。
    """
    from relay_sdk.http.request import raise_for_relay_status

    try:
        response = client.request(
            "POST",
            f"/streams/{stream_id}/ack",
            json={"up_to_publish_id": up_to_publish_id},
        )
    except httpx.TimeoutException as exc:
        raise TransientError(f"stream ack timeout: {exc}") from exc
    except httpx.TransportError as exc:
        raise TransientError(f"stream ack 接続不能: {exc}") from exc
    raise_for_relay_status(response, subscription_scoped=True)


# ---------------------------------------------------------------------------
# 1 frame を inbox に振り分ける（副作用ラッパ、テストで monkeypatch しやすい形）
# ---------------------------------------------------------------------------


@dataclass
class DispatchResult:
    written_sessions: list[str] = field(default_factory=list)
    dropped: bool = False
    reason: Optional[str] = None


def dispatch_frame(
    frame: object,
    declarations_snapshot: list[dict],
    tracker: AckTracker,
    *,
    inbox_append: Callable[[str, dict], None] = inbox.append,
) -> DispatchResult:
    """1 件の SubFrame / StreamFrame を対応 session inbox に追記し ack tracker を進める。

    追記が成功した session の一覧を返す。追記が失敗した session の分については
    tracker を進めず、次回接続の再配達に委ねる（順序契約: 追記 → ack）。
    宛先ゼロで捨てた場合は `dropped=True` を返す（ack tracker は進めない）。
    """
    if isinstance(frame, SubFrame):
        owner = resolve_sub_owner(declarations_snapshot, frame.subscription_id)
        if owner is None:
            # 宛先不在は relay 側の再起動直後などで起こりうる。ack せず、
            # B-2 の resubscribe で解消するまで再配達させる。
            return DispatchResult(dropped=True, reason="sub_owner_not_found")
        try:
            inbox_append(owner, frame.payload)
        except OSError as exc:
            logger.error(
                "inbox 追記に失敗しました（ack を打たず再配達に委ねます）: session=%s error=%s",
                owner,
                exc,
            )
            return DispatchResult(dropped=True, reason="inbox_append_failed")
        tracker.mark_sub(frame.subscription_id, frame.publish_id)
        return DispatchResult(written_sessions=[owner])

    if isinstance(frame, StreamFrame):
        targets = resolve_stream_targets(declarations_snapshot, frame.stream_name)
        if not targets:
            logger.debug(
                "stream frame drop: 宣言 session ゼロ stream=%s publish_id=%s",
                frame.stream_id,
                frame.publish_id,
            )
            # ack せず drop するとフレームは outbox に残るため、当該 stream の
            # 全 read member（= server identity 分）分は次回接続で再配達される。
            # 誰も購読していない場への配達義務は at-least-once 契約の外なので、
            # server identity 分の outbox は stream ack で明示的に掃く。
            tracker.mark_stream(frame.stream_id, frame.publish_id)
            return DispatchResult(dropped=True, reason="no_stream_target")
        written: list[str] = []
        for session_id in targets:
            try:
                inbox_append(session_id, frame.payload)
                written.append(session_id)
            except OSError as exc:
                logger.error(
                    "inbox 追記に失敗しました（この session 分の ack は次回に委ねます）: "
                    "session=%s stream=%s error=%s",
                    session_id,
                    frame.stream_id,
                    exc,
                )
        # 一部 session でも失敗が残るときは ack しない（再配達で埋め合わせる）。
        # 全 session に書けたときのみ stream ack を進める。
        if written and len(written) == len(targets):
            tracker.mark_stream(frame.stream_id, frame.publish_id)
        return DispatchResult(written_sessions=written)

    return DispatchResult(dropped=True, reason="unknown_frame")


# ---------------------------------------------------------------------------
# 常駐 loop（SSE 接続 → parse → dispatch → ack）
# ---------------------------------------------------------------------------


def _snapshot_subscription_ids(snapshot: list[dict]) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for decl in snapshot:
        for entry in decl.get("subscriptions", []):
            sub_id = entry.get("subscription_id")
            if isinstance(sub_id, str) and sub_id and sub_id not in seen:
                seen.add(sub_id)
                ids.append(sub_id)
    return ids


RECONFIGURE_DEBOUNCE_SECONDS = 0.5


def run(
    stop_event: threading.Event,
    reconfigure_event: threading.Event,
    *,
    rescan_interval_seconds: float = 5.0,
    reconnect_backoff_initial: float = 1.0,
    reconnect_backoff_cap: Optional[float] = None,
    reconfigure_debounce_seconds: float = RECONFIGURE_DEBOUNCE_SECONDS,
) -> None:
    """B-1 常駐 loop。stop_event が set されるまで SSE を受信し続ける。

    - subscription_ids は declaration file scan で毎接続時に組み立てる。
      lease_loop（B-2）が新規 subscribe / resubscribe を行ったら reconfigure_event
      を set してもらい、intake は現接続を切って新しい id 集合で再接続する。
    - reconfigure_event 由来の切断では、次の接続を確立する前に
      reconfigure_debounce_seconds だけ待つ。複数 session が短時間に連続して
      notify_reconfigure() を呼んだ場合（起動直後の同時多発 subscribe 等）でも、
      この待機中に来た分をまとめて 1 回の再接続に合流させ、既に接続済みの他
      session の受信に再接続が連鎖して波及するのを抑える。
    - 接続エラー / SSE 切断 / read timeout はいずれも短い backoff で reconnect する
      （backoff は SDK と同じく指数、cap で頭打ち）。
    """
    backoff_cap = (
        reconnect_backoff_cap
        if reconnect_backoff_cap is not None
        else sdk_config.env_reconnect_backoff_cap_seconds()
    )
    keepalive = sdk_config.env_sse_keepalive_seconds()
    backoff = reconnect_backoff_initial
    base_url = config.get_base_url()
    tracker = AckTracker()

    while not stop_event.is_set():
        snapshot = declarations.load_all()
        sub_ids = _snapshot_subscription_ids(snapshot)
        if not sub_ids:
            # 購読宣言がまだ 1 件も無い状態。短い間隔でリスキャンする（起動直後や
            # 全 session 退場後の再起動待ちに相当）。
            if stop_event.wait(rescan_interval_seconds):
                return
            if reconfigure_event.is_set():
                reconfigure_event.clear()
            continue

        token = config.get_token()
        try:
            with make_client(base_url, bearer_token=token, timeout=keepalive * 2) as client:
                reconfigured = _consume(
                    client,
                    subscription_ids=sub_ids,
                    stop_event=stop_event,
                    reconfigure_event=reconfigure_event,
                    tracker=tracker,
                    keepalive=keepalive,
                )
            backoff = reconnect_backoff_initial
            if reconfigured and not stop_event.is_set():
                if stop_event.wait(reconfigure_debounce_seconds):
                    return
        except (TransientError, PermanentError, RelayProtocolError) as exc:
            logger.warning("SSE 接続エラー（%.1fs 後に再接続）: %s", backoff, exc)
            if stop_event.wait(backoff):
                return
            backoff = min(backoff * 2, backoff_cap)
        except Exception:
            logger.exception("intake 予期しない例外（%.1fs 後に再接続）", backoff)
            if stop_event.wait(backoff):
                return
            backoff = min(backoff * 2, backoff_cap)


def _consume(
    client: httpx.Client,
    *,
    subscription_ids: list[str],
    stop_event: threading.Event,
    reconfigure_event: threading.Event,
    tracker: AckTracker,
    keepalive: float,
) -> bool:
    """1 本の SSE 接続で流れてくる frame を dispatch し続ける。

    reconfigure_event が set されたら接続を切って呼び出し側に戻す（戻り値 True）。
    stop_event による終了・接続の自然な終端は False を返す。read timeout 到達
    （無音）も上位に TransientError で伝播させ、backoff → 再接続経路に載せる。
    """
    read_timeout = keepalive * 2
    with open_sse(
        client, subscription_ids=subscription_ids, read_timeout=read_timeout
    ) as response:
        raise_for_sse_status(response)
        try:
            for sse_frame in parse_sse_byte_stream(
                response.iter_bytes(),
                max_frame_bytes=sdk_config.SSE_MAX_FRAME_BYTES,
                max_buffer_bytes=sdk_config.SSE_MAX_BUFFER_BYTES,
            ):
                if stop_event.is_set():
                    return False
                if reconfigure_event.is_set():
                    reconfigure_event.clear()
                    return True
                if sse_frame.kind == "overflow":
                    logger.warning("SSE frame を破棄しました（受信量上限超過）")
                    continue
                if sse_frame.kind == "comment":
                    # keepalive 契機に ack retry を回す。
                    tracker.flush(client)
                    continue
                if sse_frame.event != "notification" or not sse_frame.data:
                    continue
                try:
                    data = json.loads(sse_frame.data)
                except (ValueError, UnicodeDecodeError):
                    logger.warning("SSE frame の JSON parse に失敗しました（skip）")
                    continue
                frame_obj = parse_frame(data)
                if frame_obj is None:
                    continue
                snapshot = declarations.load_all()
                dispatch_frame(frame_obj, snapshot, tracker)
                tracker.flush(client)
        except httpx.TimeoutException as exc:
            raise TransientError(f"SSE 無音タイムアウト: {exc}") from exc
        except httpx.TransportError as exc:
            raise TransientError(f"SSE 接続エラー: {exc}") from exc
    return False


__all__ = [
    "AckTracker",
    "DispatchResult",
    "StreamFrame",
    "SubFrame",
    "dispatch_frame",
    "parse_frame",
    "resolve_stream_targets",
    "resolve_sub_owner",
    "run",
]
