#!/usr/bin/env python3
"""ow stagnation detector — Phase A (案B: orch 側 watcher)。

relay の /history を polling し、worker の state 遷移を追跡する。
auto 遷移すべき state で閾値を超えた場合 ``ow_sentinel`` handle で
stagnation event を relay に append する。

- 監視対象: state=ready (60秒で working/terminated に遷移すべき),
  state=draining (90秒で terminated に遷移すべき)
- loading→ready は対象外 (heartbeat 継続中の長時間 loading は許容、
  巨大 context warm-up を誤検知するため)
- 既存 watchdog (heartbeat 途絶検知) とは責務分離して併走する
  (watchdog=死活、stagnation=詰まり)

D#2752 / M#388 の仕様に基づく Phase A 実装。Phase B (ow_service
projector への push hook 統合) で本ファイルは廃止予定。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

SENTINEL_HANDLE = "ow_sentinel"

# state -> 閾値秒。loading は意図的に含めない (D#2752 仕様)。
DEFAULT_THRESHOLDS: dict[str, int] = {
    "ready": 60,
    "draining": 90,
}

DEFAULT_POLL_INTERVAL_SEC = 5
DEFAULT_RELAY_URL = "http://127.0.0.1:8765"


@dataclass
class WatchEntry:
    """1 worker handle の現在 watch 中 state エントリ。"""

    handle: str
    state: str
    transitioned_at: float
    task: Optional[str] = None
    emitted: bool = False


class SentinelState:
    """state 遷移追跡と stagnation 判定の純粋ロジック。

    relay event を observe_event で 1件ずつ取り込み、scan(now) で
    閾値超え未通知の watch entry から stagnation envelope を生成する。
    HTTP 副作用を含まないため、deterministic に unit test できる。
    """

    def __init__(self, thresholds: Optional[dict[str, int]] = None) -> None:
        self.thresholds = dict(thresholds) if thresholds else dict(DEFAULT_THRESHOLDS)
        self.watches: dict[str, WatchEntry] = {}

    def observe_event(self, message: dict, now: float) -> None:
        """relay message 1件を観測し watch entry を更新する。

        body の data.type に応じて分岐する:
        - state: 監視対象 state なら entry を新規作成 (再武装も兼ねる)、
          それ以外の state なら entry を削除する
        - identity: terminated_at が入っていれば entry を削除する

        body が JSON 文字列のまま渡された場合は内部で dict に正規化する
        (呼び出し側で _coerce_message_body を事前に呼ぶ必要はない)。
        """
        message = _coerce_message_body(message)
        body = message.get("body")
        if not isinstance(body, dict):
            return
        data = body.get("data") or {}
        etype = data.get("type")
        handle = body.get("from") or message.get("handle")
        if not handle:
            return

        if etype == "state":
            state = data.get("state")
            if state in self.thresholds:
                self.watches[handle] = WatchEntry(
                    handle=handle,
                    state=state,
                    transitioned_at=now,
                    task=body.get("task"),
                )
            else:
                self.watches.pop(handle, None)
        elif etype == "identity":
            if data.get("terminated_at"):
                self.watches.pop(handle, None)

    def scan(self, now: float) -> list[dict]:
        """全 watch entry をスキャンし、閾値超え未通知の entry から envelope を返す。

        この関数は副作用なしで pending envelope を返すだけ。発火を確定
        させるには呼び出し側で送信成功を確認後 mark_emitted(handle, state)
        を呼ぶ。送信に失敗した場合 mark しなければ次の scan で再度返り、
        retry になる (stagnation が永続的に失われない)。
        """
        envelopes: list[dict] = []
        for handle, entry in self.watches.items():
            if entry.emitted:
                continue
            threshold = self.thresholds[entry.state]
            elapsed = now - entry.transitioned_at
            if elapsed < threshold:
                continue
            envelope: dict = {
                "v": 1,
                "kind": "event",
                "from": SENTINEL_HANDLE,
                "to": "orch",
                "data": {
                    "type": "stagnation",
                    "target_handle": handle,
                    "target_state": entry.state,
                    "elapsed_sec": int(elapsed),
                    "threshold_sec": threshold,
                },
            }
            if entry.task is not None:
                envelope["task"] = entry.task
            envelopes.append(envelope)
        return envelopes

    def mark_emitted(self, handle: str, state: str) -> None:
        """送信成功した stagnation を確定して同一 state 内重複発火を抑止する。

        scan で返した envelope を呼び出し側が relay に送信成功した直後に
        呼ぶ。entry が既に別 state に差し替わっていた場合 (再武装) は
        何もしない (古い state の mark は新しい watch entry に影響させない)。
        """
        entry = self.watches.get(handle)
        if entry is None or entry.state != state:
            return
        entry.emitted = True


class RelayClient:
    """relay HTTP API への薄い wrapper (history pull / send)。"""

    def __init__(self, relay_url: str = DEFAULT_RELAY_URL, timeout: float = 10.0) -> None:
        self.relay_url = relay_url.rstrip("/")
        self.timeout = timeout

    def fetch_history(self, channel: str, since: int) -> list[dict]:
        query = urllib.parse.urlencode({"channel": channel, "since": since})
        url = f"{self.relay_url}/history?{query}"
        with urllib.request.urlopen(url, timeout=self.timeout) as resp:
            payload = json.load(resp)
        return list(payload.get("messages") or [])

    def send_event(self, channel: str, envelope: dict) -> None:
        body = json.dumps(
            {
                "channel": channel,
                "handle": SENTINEL_HANDLE,
                "body": envelope,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self.relay_url}/send",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            resp.read()


def _coerce_message_body(message: dict) -> dict:
    """body が JSON 文字列で返ってきた場合に dict に正規化する。"""
    body = message.get("body")
    if isinstance(body, str):
        try:
            message = dict(message)
            message["body"] = json.loads(body)
        except json.JSONDecodeError:
            return message
    return message


def run(
    channel: str,
    relay_url: str = DEFAULT_RELAY_URL,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SEC,
    thresholds: Optional[dict[str, int]] = None,
) -> None:
    state = SentinelState(thresholds=thresholds)
    client = RelayClient(relay_url=relay_url)
    last_msg_id = 0
    print(
        f"[ow_sentinel] start channel={channel} relay={relay_url} "
        f"poll={poll_interval}s thresholds={state.thresholds}",
        file=sys.stderr,
        flush=True,
    )
    while True:
        now = time.time()
        try:
            messages = client.fetch_history(channel, last_msg_id)
        except (urllib.error.URLError, TimeoutError) as exc:
            print(f"[ow_sentinel] fetch_history error: {exc}", file=sys.stderr, flush=True)
            messages = []
        for msg in messages:
            # body 文字列 → dict 正規化は observe_event 内部で行われる
            state.observe_event(msg, now)
            msg_id = msg.get("msg_id")
            if isinstance(msg_id, int) and msg_id > last_msg_id:
                last_msg_id = msg_id

        for envelope in state.scan(now):
            target_handle = envelope["data"]["target_handle"]
            target_state = envelope["data"]["target_state"]
            try:
                client.send_event(channel, envelope)
            except (urllib.error.URLError, TimeoutError) as exc:
                # 送信失敗時は mark_emitted を呼ばないので次の scan で
                # 再度同じ envelope が返り retry される (stagnation を失わない)
                print(f"[ow_sentinel] send_event error: {exc}", file=sys.stderr, flush=True)
                continue
            state.mark_emitted(target_handle, target_state)
            print(
                f"[ow_sentinel] sent stagnation handle={target_handle} "
                f"state={target_state} "
                f"elapsed={envelope['data']['elapsed_sec']}s",
                file=sys.stderr,
                flush=True,
            )

        time.sleep(poll_interval)


def main(argv: Optional[list[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 1:
        print(
            "Usage: sentinel.py <channel_code> [poll_interval_sec]",
            file=sys.stderr,
        )
        return 2
    channel = args[0]
    poll_interval: float = DEFAULT_POLL_INTERVAL_SEC
    if len(args) >= 2:
        try:
            poll_interval = float(args[1])
        except ValueError:
            print(f"invalid poll_interval: {args[1]}", file=sys.stderr)
            return 2
    relay_url = os.environ.get("RELAY_URL", DEFAULT_RELAY_URL)
    try:
        run(channel, relay_url=relay_url, poll_interval=poll_interval)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
