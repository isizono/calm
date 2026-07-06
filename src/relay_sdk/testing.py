"""unit test 用の in-memory fake relay（relay-v2-sdk.md §7.1）。

relay の HTTP / SSE 振る舞いを模した stub。SDK の publisher / subscriber コードを実 relay
なしに駆動する。実 relay の永続性 / DLQ / 7 日 GC / SSE keepalive 30 秒は模さない
（integration test 側で見る）。

実装は stdlib の `http.server`（ThreadingHTTPServer）で実 TCP socket を張る。SDK が使う
httpx / SSE parse / 再接続の実経路をそのまま通すため、`httpx.MockTransport` ではなく
本物の socket を採用した。auth は強制しない（`Authorization` ヘッダは無視する。認証は
integration test 側で real relay に対して検証する）。

提供する fault 注入:
- ``simulate_outage(enabled)`` — 制御 endpoint が ``503``（TransientError）を返す
- ``simulate_subscription_loss(subscription_id)`` — 当該 subscription を失効させ、以後の
  操作を ``404`` にし SSE を drop する（PermanentError → resubscribe 経路の検証）
- ``drop_connections()`` — アクティブな SSE 接続を全て切る（再接続 + 未 ack 再 push の検証）
- ``simulate_silence(enabled)`` — SSE 接続を張ったまま event も keepalive も一切書き込まない
  （半死 TCP 接続を模す。read timeout による無音検知の検証用）
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class _SubRecord:
    __slots__ = ("subscription_id", "subscriber", "labels", "lease_expires_at", "retain_seconds")

    def __init__(self, subscription_id, subscriber, labels, lease_expires_at, retain_seconds):
        self.subscription_id = subscription_id
        self.subscriber = subscriber
        self.labels = frozenset(labels)
        self.lease_expires_at = lease_expires_at
        self.retain_seconds = retain_seconds


class FakeRelay:
    """relay の最小 stub（§7.1）。context manager として使う。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subs: dict[str, _SubRecord] = {}
        self._outbox: dict[str, list[dict[str, Any]]] = {}
        self._publish_counter = 0
        self._sub_counter = 0
        self._lost: set[str] = set()
        self._outage = False
        self._silence = False
        self._drop_generation = 0
        self._stop = False
        self._raw_injections: list[bytes] = []

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._server.fake = self  # handler から参照
        self._port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._agent_card_path: str | None = None

    # -- lifecycle --------------------------------------------------------

    def __enter__(self) -> "FakeRelay":
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._stop = True
            self._drop_generation += 1
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def fake_agent_card_path(self) -> str:
        """最小 AgentCard JSON を temp file に書き出しパスを返す（§7.1 の signature 用）。"""
        if self._agent_card_path is None:
            fd = tempfile.NamedTemporaryFile(
                mode="w", suffix="-agent-card.json", delete=False, encoding="utf-8"
            )
            json.dump(
                {
                    "name": "fake-relay-client",
                    "version": "0.0.0",
                    "securitySchemes": {"bearer": {"httpAuthSecurityScheme": {"scheme": "bearer"}}},
                },
                fd,
            )
            fd.close()
            self._agent_card_path = fd.name
        return self._agent_card_path

    # -- fault injection --------------------------------------------------

    def simulate_outage(self, enabled: bool = True) -> None:
        with self._lock:
            self._outage = enabled

    def simulate_subscription_loss(self, subscription_id: str) -> None:
        with self._lock:
            self._lost.add(subscription_id)
            self._subs.pop(subscription_id, None)
            self._outbox.pop(subscription_id, None)
            self._drop_generation += 1

    def drop_connections(self) -> None:
        """アクティブな SSE 接続を全て切る（cursor リセット → 未 ack 再 push を誘発）。"""
        with self._lock:
            self._drop_generation += 1

    def simulate_silence(self, enabled: bool = True) -> None:
        """SSE 接続を張ったまま event も keepalive も一切書き込まない状態にする。

        接続は明示的に close しない（TCP 上は生存したまま無音が続く半死接続を模す）。
        blocker2（SSE 無音検知 / read timeout）のテスト用。
        """
        with self._lock:
            self._silence = enabled

    def inject_raw_sse(self, blob: bytes) -> None:
        """アクティブな SSE stream に任意の生バイト列をそのまま書き込む。

        壊れた JSON / 未知 event 型 / フィールド欠落 / 途中切断された行など、正規経路
        （`publish`）では作れない不正フレームを注入し、受信ループの耐性を検証する用途。
        """
        with self._lock:
            self._raw_injections.append(blob)

    # -- test convenience -------------------------------------------------

    def publish(
        self,
        *,
        ref_type: str,
        ref_id: Any,
        labels: list[str],
        title: str | None = None,
    ) -> int:
        """subset マッチする subscription の outbox に event を積む（§7.1）。publish_id を返す。"""
        ref = {"type": ref_type, "id": ref_id}
        return self._do_publish(ref, labels, title)

    def outbox_size(self, subscription_id: str) -> int:
        with self._lock:
            return len(self._outbox.get(subscription_id, []))

    # -- internal publish -------------------------------------------------

    def _do_publish(self, ref: dict[str, Any], labels: list[str], title: str | None) -> int:
        labels_set = frozenset(labels)
        with self._lock:
            self._publish_counter += 1
            publish_id = self._publish_counter
            matched = 0
            for sub in self._subs.values():
                if sub.labels.issubset(labels_set):
                    matched += 1
                    self._outbox.setdefault(sub.subscription_id, []).append(
                        {
                            "delivery_target": f"sub:{sub.subscription_id}",
                            "publish_id": publish_id,
                            "ref": ref,
                            "labels": list(labels),
                            "title": title,
                            "delivered_at": _iso(_now()),
                        }
                    )
            return publish_id

    # -- handler ----------------------------------------------------------

    def _make_handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args, **kwargs):  # silence
                pass

            # -- helpers --
            def _drain_body(self) -> bytes:
                """request body を読み切ってバッファする。

                HTTP/1.1 keep-alive 接続では、body を読まずに次のレスポンスを
                書いてしまうと、未読の body バイトが後続 request の先頭に混入し
                パース位置がずれる（`BaseHTTPRequestHandler` が次の request line を
                誤読し `501 Unsupported method` 等の破損応答を返す）。outage 等の
                早期 return 分岐でも必ず body を drain してから応答するため、
                routing の最初で一度だけ読んでバッファに保持する。
                """
                length = int(self.headers.get("Content-Length", 0) or 0)
                return self.rfile.read(length) if length else b""

            def _read_json(self) -> dict:
                raw = getattr(self, "_body_bytes", b"")
                if not raw:
                    return {}
                try:
                    return json.loads(raw.decode("utf-8"))
                except Exception:
                    return {}

            def _send_json(self, status: int, body: dict, extra_headers: dict | None = None):
                data = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (extra_headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def _send_empty(self, status: int):
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _outage_active(self) -> bool:
                with fake._lock:
                    return fake._outage

            # -- routing --
            def do_POST(self):
                path = urlparse(self.path).path
                self._body_bytes = self._drain_body()
                if self._outage_active():
                    self._send_json(503, {"code": "OutboxUnavailableError", "message": "outage"})
                    return
                if path == "/subscriptions":
                    self._handle_subscribe()
                elif path == "/publish":
                    self._handle_publish()
                elif path.startswith("/subscriptions/") and path.endswith("/ack"):
                    self._handle_ack(path.split("/")[2])
                else:
                    self._send_json(404, {"code": "NotFound", "message": path})

            def do_PUT(self):
                path = urlparse(self.path).path
                self._body_bytes = self._drain_body()
                if self._outage_active():
                    self._send_json(503, {"code": "OutboxUnavailableError", "message": "outage"})
                    return
                if path.startswith("/subscriptions/") and path.endswith("/lease"):
                    self._handle_lease(path.split("/")[2])
                else:
                    self._send_json(404, {"code": "NotFound", "message": path})

            def do_DELETE(self):
                path = urlparse(self.path).path
                self._drain_body()  # DELETE は通常 body 無しだが、念のため drain する。
                if path.startswith("/subscriptions/"):
                    self._handle_unsubscribe(path.split("/")[2])
                else:
                    self._send_json(404, {"code": "NotFound", "message": path})

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path == "/events":
                    self._handle_events(parsed)
                else:
                    self._send_json(404, {"code": "NotFound", "message": parsed.path})

            # -- endpoint impls --
            def _handle_subscribe(self):
                body = self._read_json()
                labels = body.get("labels")
                if not isinstance(labels, list) or not labels:
                    self._send_json(400, {"code": "LabelValidationError", "message": "labels"})
                    return
                subscriber = body.get("subscriber", "")
                lease_ttl = body.get("lease_ttl", 300)
                retain = (body.get("delivery_options") or {}).get("retain_seconds")
                with fake._lock:
                    fake._sub_counter += 1
                    sub_id = f"sub-{fake._sub_counter}"
                    rec = _SubRecord(
                        sub_id,
                        subscriber,
                        labels,
                        _iso(_now() + timedelta(seconds=lease_ttl)),
                        retain,
                    )
                    fake._subs[sub_id] = rec
                    fake._outbox.setdefault(sub_id, [])
                self._send_json(
                    201, {"subscription_id": sub_id, "lease_expires_at": rec.lease_expires_at}
                )

            def _handle_publish(self):
                body = self._read_json()
                ref = body.get("ref")
                labels = body.get("labels")
                if not isinstance(ref, dict) or not isinstance(labels, list):
                    self._send_json(400, {"code": "InvalidRequestError", "message": "ref/labels"})
                    return
                pid = fake._do_publish(ref, labels, body.get("title"))
                with fake._lock:
                    matched = sum(
                        1 for s in fake._subs.values() if s.labels.issubset(frozenset(labels))
                    )
                self._send_json(202, {"publish_id": pid, "matched_subscriptions": matched})

            def _handle_ack(self, sub_id: str):
                body = self._read_json()
                up_to = body.get("up_to_publish_id")
                with fake._lock:
                    if sub_id in fake._lost or sub_id not in fake._subs:
                        self._send_json(404, {"code": "SubscriptionNotFoundError", "message": sub_id})
                        return
                    entries = fake._outbox.get(sub_id, [])
                    fake._outbox[sub_id] = [e for e in entries if e["publish_id"] > up_to]
                self._send_json(200, {})

            def _handle_lease(self, sub_id: str):
                body = self._read_json()
                with fake._lock:
                    if sub_id in fake._lost or sub_id not in fake._subs:
                        self._send_json(404, {"code": "SubscriptionNotFoundError", "message": sub_id})
                        return
                    rec = fake._subs[sub_id]
                    ttl = body.get("lease_ttl", 300)
                    rec.lease_expires_at = _iso(_now() + timedelta(seconds=ttl))
                    expires = rec.lease_expires_at
                self._send_json(200, {"lease_expires_at": expires})

            def _handle_unsubscribe(self, sub_id: str):
                with fake._lock:
                    if sub_id in fake._lost or sub_id not in fake._subs:
                        self._send_json(404, {"code": "SubscriptionNotFoundError", "message": sub_id})
                        return
                    fake._subs.pop(sub_id, None)
                    fake._outbox.pop(sub_id, None)
                self._send_empty(204)

            def _handle_events(self, parsed):
                qs = parse_qs(parsed.query)
                ids = [s for s in (qs.get("subscription_ids", [""])[0]).split(",") if s]
                with fake._lock:
                    for sub_id in ids:
                        if sub_id in fake._lost or sub_id not in fake._subs:
                            self._send_json(
                                404, {"code": "SubscriptionNotFoundError", "message": sub_id}
                            )
                            return
                    my_generation = fake._drop_generation

                # SSE stream 開始（Connection: close で body-until-close、httpx が
                # incremental に読む）。
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()

                cursors = {sub_id: 0 for sub_id in ids}
                try:
                    while True:
                        with fake._lock:
                            if fake._stop or fake._drop_generation != my_generation:
                                break
                            for sub_id in ids:
                                if sub_id in fake._lost or sub_id not in fake._subs:
                                    return
                            silent = fake._silence
                            injected: list[bytes] = []
                            pending: list[dict] = []
                            if not silent:
                                injected = fake._raw_injections
                                fake._raw_injections = []
                                for sub_id in ids:
                                    for entry in fake._outbox.get(sub_id, []):
                                        if entry["publish_id"] > cursors[sub_id]:
                                            pending.append(entry)
                                pending.sort(key=lambda e: e["publish_id"])
                        if silent:
                            # 半死接続を模す: event も keepalive も一切書き込まない
                            # （接続自体は close しない）。
                            time.sleep(0.02)
                            continue
                        for blob in injected:
                            self.wfile.write(blob)
                            self.wfile.flush()
                        for entry in pending:
                            self._write_sse_event(entry)
                            sid = entry["delivery_target"].split(":", 1)[1]
                            cursors[sid] = max(cursors[sid], entry["publish_id"])
                        if not pending:
                            self._write_keepalive()
                        time.sleep(0.02)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return

            def _write_sse_event(self, entry: dict):
                data = json.dumps(entry, ensure_ascii=False)
                frame = (
                    f"event: notification\n"
                    f"id: {entry['publish_id']}\n"
                    f"data: {data}\n\n"
                ).encode("utf-8")
                self.wfile.write(frame)
                self.wfile.flush()

            def _write_keepalive(self):
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()

        return Handler
