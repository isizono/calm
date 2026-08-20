"""FakeRelay を使った B-1 intake の一気通貫 integration test。

シナリオ:
1. FakeRelay を起動する
2. cc-memory の `relay_subscribe` で subscription を張り declaration file を作る
3. FakeRelay 側で当該 labels に対して publish する
4. B-1 intake（`intake.run`）を短時間だけ回す
5. session の inbox に書き込まれていることを `relay_receive` で確認する

subscription レーンのフレームが購読 session の inbox に到達することと、受け入れ基準
「FakeRelay 一気通貫テストが pass」を担保する。stream レーンは FakeRelay が模して
いないため、intake の unit test（stream 分岐は `dispatch_frame` 単体で検証済）と
integration の subscribe 経路の組み合わせで確認する。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from relay_sdk.testing import FakeRelay
from src.services.relay import declarations, intake, service


@pytest.fixture(autouse=True)
def relay_state(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path / "relay-state"))
    monkeypatch.setenv("RELAY_BEARER_TOKEN", "test-token")
    monkeypatch.setenv("RELAY_IDENTITY", "cc-memory")


def _run_intake_briefly(stop_event: threading.Event, seconds: float) -> None:
    """短時間だけ intake を回し、stop_event で終わらせる。"""
    reconfigure = threading.Event()

    def stopper():
        time.sleep(seconds)
        stop_event.set()

    t = threading.Thread(target=stopper, daemon=True)
    t.start()
    intake.run(
        stop_event,
        reconfigure,
        rescan_interval_seconds=0.2,
        reconnect_backoff_initial=0.1,
        reconnect_backoff_cap=0.5,
    )


def test_publish_reaches_session_inbox_via_intake(monkeypatch):
    with FakeRelay() as fake:
        monkeypatch.setenv("RELAY_BASE_URL", fake.base_url)

        # cc-memory の relay_subscribe を通して subscription を張る。
        # "topic:" は cc-memory の中核 entity namespace として予約済みのため使えない
        # （validate_labels が拒否する）。ここでは非予約 namespace の "room:" を使う。
        result = service.relay_subscribe(
            ["room:planning"], caller_session_id="sess-1"
        )
        assert "error" not in result, result
        subscription_id = result["subscription_id"]
        assert subscription_id.startswith("sub-")

        # relay_subscribe が declaration に「handle:<handle>」を付ける仕様。
        decl = declarations.load("sess-1")
        subscribed_labels = decl["subscriptions"][0]["labels"]

        # FakeRelay で subset マッチする publish を投げる。
        fake.publish(
            ref_type="message",
            ref_id="hello",
            labels=subscribed_labels,
        )

        # B-1 intake を少しの時間だけ回す。
        stop = threading.Event()
        _run_intake_briefly(stop, seconds=2.5)

        # session の inbox に到達している。
        received = service.relay_receive(caller_session_id="sess-1")
        assert received["count"] >= 1, received
        payload = received["messages"][0]
        assert payload["delivery_target"] == f"sub:{subscription_id}"
        assert payload["ref"] == {"type": "message", "id": "hello"}

        # FakeRelay 側 outbox は ack で掃かれている。
        # （ack が届いていれば outbox_size == 0）
        assert fake.outbox_size(subscription_id) == 0


def test_stale_declaration_does_not_block_live_session_receive(monkeypatch):
    """失効した残骸宣言（他 session 由来）が、生存 session の受信を道連れにしない。

    lease 事前フィルタが無いと、relay に存在しない subscription_id が接続集合に
    1件でも混ざると `GET /events` が全体で拒否され、無関係な生存 session の
    受信まで止まる。ここでは relay 側に一度も登録されていない id を持つ、
    lease 期限切れの残骸宣言を別 session 名義で直接書き込み、生存 session
    （sess-1）の publish が届くことを確認する。
    """
    with FakeRelay() as fake:
        monkeypatch.setenv("RELAY_BASE_URL", fake.base_url)

        result = service.relay_subscribe(
            ["room:planning"], caller_session_id="sess-1"
        )
        assert "error" not in result, result
        subscription_id = result["subscription_id"]

        decl = declarations.load("sess-1")
        subscribed_labels = decl["subscriptions"][0]["labels"]

        # 残骸宣言: relay 側に存在しない subscription_id、lease は既に失効。
        stale_decl = declarations.ensure("sess-2")
        stale_decl["subscriptions"] = [
            {
                "subscription_id": "sub-orphan-not-registered",
                "labels": ["room:planning"],
                "lease_expires_at": (
                    datetime.now(timezone.utc) - timedelta(seconds=60)
                ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "created_at": declarations.now_iso(),
            }
        ]
        declarations.save(stale_decl)

        fake.publish(
            ref_type="message",
            ref_id="hello",
            labels=subscribed_labels,
        )

        stop = threading.Event()
        _run_intake_briefly(stop, seconds=2.5)

        received = service.relay_receive(caller_session_id="sess-1")
        assert received["count"] >= 1, received
        payload = received["messages"][0]
        assert payload["delivery_target"] == f"sub:{subscription_id}"
        assert payload["ref"] == {"type": "message", "id": "hello"}


def test_relay_missing_env_does_not_break_server_startup(monkeypatch):
    """受け入れ基準: RELAY_BEARER_TOKEN 未設定で RelayRuntime.start() が例外を出さず False を返す。"""
    from src.services.relay.runtime import RelayRuntime

    monkeypatch.delenv("RELAY_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("RELAY_BASE_URL", raising=False)
    rt = RelayRuntime(active_sessions_getter=lambda: set())
    assert rt.start() is False
    rt.stop()  # 二度呼んでも安全
