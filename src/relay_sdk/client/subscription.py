"""subscriber 側 SDK（relay-v2-sdk.md §3）。

`subscribe()` → `Subscription.receive()` → `Event` yield の受信ループ、`auto_ack`、
`ack()`、`close()` を実装する。再接続・resubscribe・lease renew・dedup は receive()
ループ内に畳み込む（v1 は同期 API のみ、§4.1）。

title 分離（§3.2.1）: `Event` は wire payload の `title` を持たない。title は
`EventDisplay` にのみ流し、業務判定コードから型レベルで塞ぐ。
"""
from __future__ import annotations

import json
import logging
import time
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Sequence

import httpx

from src.relay_sdk import config as sdk_config
from src.relay_sdk.client.sse import parse_sse_byte_stream
from src.relay_sdk.errors import PermanentError, RelayProtocolError, TransientError
from src.relay_sdk.http import (
    delete_subscription,
    make_client,
    open_sse,
    post_ack,
    post_subscription,
    put_lease,
    raise_for_sse_status,
)

# 受信 event の表示メタを流す専用 logger（§3.2.1）。
_events_logger = logging.getLogger("relay_sdk.client.events")


@dataclass(frozen=True)
class Event:
    """receive() が yield する業務判定用イベント（§3.2）。

    分岐・判定に使ってよいのは `ref_type` / `ref_id` / `labels` のみ。`publish_id` は
    ack カーソル、`delivered_at` は reconcile の since_ts（§3.5）に使う。`title` は
    意図的に持たない（§3.2.1、業務判定を型で ref+labels に限定するため）。
    """

    publish_id: int
    subscription_id: str
    ref_type: str
    ref_id: str | int
    labels: list[str]
    delivered_at: str


@dataclass(frozen=True)
class EventDisplay:
    """ロギング・通知表示専用のイベントメタデータ（§3.2.1）。業務判定に使ってはならない。"""

    publish_id: int
    ref_type: str
    ref_id: str | int
    title: str | None


def _parse_iso(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Subscription:
    """1 subscription の受信ループ + lifecycle（context manager）。

    `subscribe()` が生成する。`with subscribe(...) as sub:` で使うと終了時に unsubscribe
    まで自動で呼ぶ（§3.1）。
    """

    def __init__(
        self,
        *,
        client,
        subscriber: str,
        labels: Sequence[str],
        subscription_id: str,
        lease_expires_at: str,
        lease_ttl: int,
        retain_seconds: int | None,
        auto_ack: bool,
        on_display: Callable[[EventDisplay], None] | None,
        reconnect_max_attempts: int | None,
        keepalive_seconds: float,
        reconnect_backoff_cap_seconds: float,
    ) -> None:
        self._client = client
        self._subscriber = subscriber
        self._labels = list(labels)
        self._subscription_id = subscription_id
        self._lease_expires_at = lease_expires_at
        self._lease_ttl = lease_ttl
        self._retain_seconds = retain_seconds
        self._auto_ack = auto_ack
        self._on_display = on_display
        self._reconnect_max_attempts = reconnect_max_attempts
        self._keepalive_seconds = keepalive_seconds
        self._backoff_cap = reconnect_backoff_cap_seconds

        self._closed = False
        self._response = None
        self._attempt = 0
        self._ack_buffer: int | None = None
        # (subscription_id, publish_id) の dedup LRU（§4.2、直近 10000 件）。
        self._seen: "OrderedDict[tuple[str, int], None]" = OrderedDict()

    # -- properties -------------------------------------------------------

    @property
    def subscription_id(self) -> str:
        return self._subscription_id

    @property
    def lease_expires_at(self) -> str:
        return self._lease_expires_at

    # -- context manager --------------------------------------------------

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- dedup ------------------------------------------------------------

    def _seen_add(self, key: tuple[str, int]) -> None:
        self._seen[key] = None
        if len(self._seen) > sdk_config.DEDUP_LRU_SIZE:
            self._seen.popitem(last=False)

    # -- ack --------------------------------------------------------------

    def ack(self, up_to_publish_id: int) -> None:
        """cumulative ack（auto_ack=False のとき呼ぶ、§3.2）。

        `POST /subscriptions/{id}/ack {up_to_publish_id}`。relay は
        `publish_id <= up_to_publish_id` を outbox から削除する。同じ値を 2 回送っても冪等。
        """
        if self._closed:
            raise RuntimeError("close 済み subscription には ack できません")
        post_ack(
            self._client,
            subscription_id=self._subscription_id,
            up_to_publish_id=up_to_publish_id,
        )

    def _flush_ack(self) -> None:
        """auto_ack バッファを flush する。成功時のみバッファをクリアする。"""
        if self._ack_buffer is None:
            return
        target = self._ack_buffer
        post_ack(
            self._client,
            subscription_id=self._subscription_id,
            up_to_publish_id=target,
        )
        if self._ack_buffer == target:
            self._ack_buffer = None

    def _buffer_and_flush_ack(self, publish_id: int) -> None:
        self._ack_buffer = (
            publish_id if self._ack_buffer is None else max(self._ack_buffer, publish_id)
        )
        try:
            self._flush_ack()
        except TransientError:
            # ack が届かなくても outbox は残り、次回再接続で再 push される（at-least-once）。
            _events_logger.warning(
                "ack flush transient failure (subscription=%s, up_to=%s)",
                self._subscription_id,
                self._ack_buffer,
            )
        # PermanentError は receive() ループへ伝播 → resubscribe。

    # -- lease renew ------------------------------------------------------

    def _maybe_renew_lease(self) -> None:
        """lease 切れ間近（残り <= lease_ttl / 3）なら PUT lease で renew する（§3.2）。

        呼び出し契機は `_run_periodic_maintenance()`（event / keepalive いずれの
        frame でも呼ばれる。keepalive frame だけを契機にすると、event が keepalive
        間隔より高頻度に届く状況（relay は push が無い間だけ keepalive を送るため、
        event が絶えず届く限り keepalive 自体が来ない）で一切 renew されないまま
        lease が失効するバグがあった）。

        PermanentError（404/410）は receive() ループへ伝播 → resubscribe。TransientError は
        次回の呼び出し契機で再試行する。
        """
        remaining = (_parse_iso(self._lease_expires_at) - _now()).total_seconds()
        if remaining > self._lease_ttl / 3:
            return
        try:
            result = put_lease(
                self._client,
                subscription_id=self._subscription_id,
                lease_ttl=self._lease_ttl,
            )
            self._lease_expires_at = result["lease_expires_at"]
        except TransientError:
            _events_logger.warning(
                "lease renew transient failure (subscription=%s)", self._subscription_id
            )

    def _run_periodic_maintenance(self) -> None:
        """event / keepalive いずれの frame でも呼ぶ保守処理。

        - lease 切れ間近なら PUT lease で renew する（`_maybe_renew_lease`）。
        - auto_ack の未 flush ack が残っていれば再送を試みる。直前の resume 時の
          flush が TransientError で失敗した場合、`_buffer_and_flush_ack` は次に
          新しい event が yield されるまで再試行の契機を持たない。以後 event が
          来なければ未 ack のまま放置されるため、event が来ない間も keepalive
          frame を契機に retry できるようにする。
        """
        self._maybe_renew_lease()
        if self._auto_ack and self._ack_buffer is not None:
            try:
                self._flush_ack()
            except TransientError:
                _events_logger.warning(
                    "ack flush retry transient failure (subscription=%s, up_to=%s)",
                    self._subscription_id,
                    self._ack_buffer,
                )

    # -- resubscribe ------------------------------------------------------

    def _resubscribe(self) -> None:
        """subscription_id 失効時に新規 POST /subscriptions を行い id を更新する（§3.4）。

        transient 失敗は backoff して再試行。RelayProtocolError（labels 不正等）は caller へ。
        """
        self._ack_buffer = None  # 旧 subscription 宛の ack は無効。
        while not self._closed:
            try:
                result = post_subscription(
                    self._client,
                    subscriber=self._subscriber,
                    labels=self._labels,
                    lease_ttl=self._lease_ttl,
                    retain_seconds=self._retain_seconds,
                )
                self._subscription_id = result["subscription_id"]
                self._lease_expires_at = result["lease_expires_at"]
                self._attempt = 0
                return
            except TransientError:
                delay = self._next_reconnect_delay()
                # reconnect_max_attempts 到達後（delay is None）も resubscribe 自体は
                # 諦めない。ここでの None を「sleep 無し」と読むと、_next_reconnect_delay
                # が None を返し続ける間 backoff_cap を無視して POST /subscriptions を
                # 連打するホットループになる（medium3）。その場合は backoff_cap で待つ。
                time.sleep(delay if delay is not None else self._backoff_cap)

    # -- reconnect backoff ------------------------------------------------

    def _next_reconnect_delay(self) -> float | None:
        """次の再接続までの待機秒。max_attempts 到達なら None（resubscribe へ切替、§3.4）。

        即時 1 回 → 1s, 2s, 4s, 8s, 16s, cap（既定 30s）。
        """
        if (
            self._reconnect_max_attempts is not None
            and self._attempt >= self._reconnect_max_attempts
        ):
            return None
        delay = 0.0 if self._attempt == 0 else min(2.0 ** (self._attempt - 1), self._backoff_cap)
        self._attempt += 1
        return delay

    # -- receive ----------------------------------------------------------

    def receive(self) -> Iterator[Event]:
        """SSE から event を 1 件ずつ yield する（§3.2）。

        - dedup 通過 event を yield する直前に `relay_sdk.client.events` へ INFO 記録し、
          `on_display` があれば呼ぶ（§3.2.1）。
        - caller が次に進めた瞬間、auto_ack=True なら直前 event の publish_id を
          cumulative ack する。
        - SSE 切断 → 再接続（未 ack 分は relay が黙って再 push）。
        - 404 / 410 → 新規 subscribe で自己修復。
        - lease 切れ間近は裏で PUT lease。
        """
        if self._closed:
            raise RuntimeError("close 済み subscription からは receive できません")
        while not self._closed:
            try:
                yield from self._stream_once()
            except PermanentError:
                self._resubscribe()
                continue
            except TransientError:
                delay = self._next_reconnect_delay()
                if delay is None:
                    self._resubscribe()
                elif delay:
                    time.sleep(delay)
                continue
            # clean EOF（relay 側 stream 終了）→ 再接続。
            if self._closed:
                break
            delay = self._next_reconnect_delay()
            if delay is None:
                self._resubscribe()
            elif delay:
                time.sleep(delay)

    def _stream_once(self) -> Iterator[Event]:
        """1 本の SSE 接続を張り、切断まで event を yield する。

        SSE 無音検知（§4.2）: `open_sse` に keepalive 間隔の 2 倍（既定 60 秒）の
        read timeout を渡す。relay は push が無い間だけ keepalive を送るため
        （push が高頻度なら keepalive 自体が来ない）、read timeout は「keepalive
        間隔そのもの」ではなく「直近 2 周期分の無音」を基準にする（keepalive 送出の
        揺らぎで誤検知しないための余裕）。timeout は `TransientError` に翻訳し、
        `receive()` の再接続ループに乗せる（無応答のまま永久ブロックしない）。
        """
        read_timeout = self._keepalive_seconds * 2
        try:
            with open_sse(
                self._client,
                subscription_ids=[self._subscription_id],
                read_timeout=read_timeout,
            ) as resp:
                raise_for_sse_status(resp)  # 404/410 → PermanentError, 5xx → TransientError
                self._response = resp
                try:
                    for frame in parse_sse_byte_stream(
                        resp.iter_bytes(),
                        max_frame_bytes=sdk_config.SSE_MAX_FRAME_BYTES,
                        max_buffer_bytes=sdk_config.SSE_MAX_BUFFER_BYTES,
                    ):
                        if self._closed:
                            return
                        self._attempt = 0  # bytes 受信 = 接続健全。backoff をリセット。
                        # event / keepalive いずれの frame でも保守処理を回す
                        # （event 専用の分岐にすると、event が keepalive 間隔より
                        # 高頻度に届く状況で lease renew も ack retry も発火しなくなる）。
                        self._run_periodic_maintenance()
                        if frame.kind == "overflow":
                            # 受信量上限超過で破棄した frame。受信は継続する。
                            _events_logger.warning(
                                "SSE frame を破棄しました（受信量上限超過, subscription=%s）",
                                self._subscription_id,
                            )
                            continue
                        if frame.kind == "comment":
                            continue
                        if frame.event != "notification" or not frame.data:
                            continue
                        event = self._handle_notification(frame.data)
                        if event is None:
                            continue
                        yield event
                        # caller が resume（= 処理完了とみなせる時点、§3.3）。
                        if self._auto_ack:
                            self._buffer_and_flush_ack(event.publish_id)
                finally:
                    self._response = None
        except httpx.TimeoutException as exc:
            raise TransientError(f"SSE 無音タイムアウト: {exc}") from exc
        except httpx.TransportError as exc:
            raise TransientError(f"SSE 接続エラー: {exc}") from exc

    def _handle_notification(self, data_str: str) -> Event | None:
        # 不正フレーム（壊れた JSON / 型不整合 / 必須フィールド欠落）は skip して受信を
        # 継続する。サーバ由来の 1 フレームの破損が受信ループ全体を落とさないようにする。
        try:
            data = json.loads(data_str)
        except ValueError:  # JSONDecodeError を含む
            _events_logger.warning(
                "SSE frame を skip しました（不正な JSON payload, subscription=%s）",
                self._subscription_id,
            )
            return None
        if not isinstance(data, dict):
            _events_logger.warning(
                "SSE frame を skip しました（payload が object でない, subscription=%s）",
                self._subscription_id,
            )
            return None
        target = data.get("delivery_target", "")
        if not isinstance(target, str) or not target.startswith("sub:"):
            # 場レーン（body のみ）は Event 型（ref ベース）の対象外。subscribe() は
            # subscription を張るだけで場 membership を張らないため通常到達しない。
            return None
        publish_id = data.get("publish_id")
        # publish_id は dedup key / ack カーソルに使うため int 必須（bool は除外）。
        if not isinstance(publish_id, int) or isinstance(publish_id, bool):
            _events_logger.warning(
                "SSE frame を skip しました（publish_id 欠落/不正, subscription=%s）",
                self._subscription_id,
            )
            return None
        key = (self._subscription_id, publish_id)
        if key in self._seen:
            _events_logger.debug(
                "dedup dropped resend frame (subscription=%s, publish_id=%s)",
                self._subscription_id,
                publish_id,
            )
            return None
        self._seen_add(key)

        ref = data.get("ref")
        if not isinstance(ref, dict):
            ref = {}
        ref_type = ref.get("type", "")
        ref_id = ref.get("id", "")
        labels = data.get("labels")
        if not isinstance(labels, list):
            labels = []
        title = data.get("title")

        # yield 前に INFO 記録（handler が例外で落ちても受信文脈がログに残る、§3.2.1）。
        _events_logger.info(
            "event publish_id=%s ref=%s:%s labels=%s title=%r",
            publish_id,
            ref_type,
            ref_id,
            labels,
            title,
        )
        if self._on_display is not None:
            display = EventDisplay(
                publish_id=publish_id, ref_type=ref_type, ref_id=ref_id, title=title
            )
            try:
                self._on_display(display)
            except Exception:  # noqa: BLE001 — 表示 callback の失敗は配達に影響させない（§3.2.1）
                _events_logger.exception("on_display callback が例外を投げました")

        return Event(
            publish_id=publish_id,
            subscription_id=self._subscription_id,
            ref_type=ref_type,
            ref_id=ref_id,
            labels=list(labels),
            delivered_at=data.get("delivered_at", ""),
        )

    # -- close ------------------------------------------------------------

    def close(self) -> None:
        """unsubscribe して SSE を切断する（§3.2）。以後 receive() は RuntimeError。"""
        if self._closed:
            return
        self._closed = True
        if self._response is not None:
            with suppress(Exception):
                self._response.close()
        if self._auto_ack and self._ack_buffer is not None:
            with suppress(Exception):
                self._flush_ack()
        with suppress(PermanentError, TransientError, RelayProtocolError, Exception):
            delete_subscription(self._client, subscription_id=self._subscription_id)
        with suppress(Exception):
            self._client.close()


def subscribe(
    *,
    relay_base_url: str,
    subscriber_identity: str,
    labels: Sequence[str],
    agent_card_path: Path | str,
    jws_key_path: Path | str | None = None,
    lease_ttl_seconds: int = 300,
    retain_seconds: int | None = None,
    auto_ack: bool = True,
    on_display: Callable[[EventDisplay], None] | None = None,
    reconnect_max_attempts: int | None = None,
) -> Subscription:
    """relay に `POST /subscriptions` を投げて subscription_id を採番する（§3.1）。

    SSE 接続（`GET /events`）は最初の `receive()` 呼び出しで張る（stream generator の
    lifecycle を receive() に閉じるため。subscribe→receive 間の publish は relay が
    未 ack outbox として保持し、接続時に再 push するので取りこぼさない）。

    Raises:
        ValueError: labels が空。
        RelayProtocolError: relay からの 4xx 応答。
        TransientError: relay 到達不能 / 5xx。
    """
    labels_list = list(labels)
    if not labels_list:
        raise ValueError("labels は空にできません（relay 側で 400 になる、firehose 防止）")

    client = make_client(
        relay_base_url,
        jws_key_path=jws_key_path,
        agent_card_path=agent_card_path,
        subscriber_identity=subscriber_identity,
        timeout=sdk_config.env_http_timeout_seconds(),
    )
    try:
        result = post_subscription(
            client,
            subscriber=subscriber_identity,
            labels=labels_list,
            lease_ttl=lease_ttl_seconds,
            retain_seconds=retain_seconds,
        )
    except Exception:
        client.close()
        raise

    return Subscription(
        client=client,
        subscriber=subscriber_identity,
        labels=labels_list,
        subscription_id=result["subscription_id"],
        lease_expires_at=result["lease_expires_at"],
        lease_ttl=lease_ttl_seconds,
        retain_seconds=retain_seconds,
        auto_ack=auto_ack,
        on_display=on_display,
        reconnect_max_attempts=reconnect_max_attempts,
        keepalive_seconds=sdk_config.env_sse_keepalive_seconds(),
        reconnect_backoff_cap_seconds=sdk_config.env_reconnect_backoff_cap_seconds(),
    )
