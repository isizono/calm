"""処理中リクエスト計測とシャットダウンガードのユニットテスト"""
import asyncio
import threading

import pytest

from src.infra import inflight
from src.infra.inflight import InflightMiddleware, shutdown_when_idle


@pytest.fixture(autouse=True)
def _reset_count():
    inflight._count = 0
    yield
    inflight._count = 0


def test_middleware_counts_while_handling_and_releases_after_success():
    seen = []

    async def ok(ctx):
        seen.append(inflight.inflight_count())
        return "r"

    assert asyncio.run(InflightMiddleware().on_message(None, ok)) == "r"
    assert seen == [1]
    assert inflight.inflight_count() == 0


def test_middleware_releases_count_when_handler_raises():
    async def boom(ctx):
        raise RuntimeError

    with pytest.raises(RuntimeError):
        asyncio.run(InflightMiddleware().on_message(None, boom))
    assert inflight.inflight_count() == 0


def test_idle_sends_shutdown_immediately():
    sent = []
    assert shutdown_when_idle(lambda: sent.append(1), timeout_sec=1) is True
    assert sent == [1]


def test_waits_for_inflight_to_drain_before_sending():
    inflight._count = 1
    sent = []
    threading.Timer(0.2, lambda: setattr(inflight, "_count", 0)).start()
    assert shutdown_when_idle(lambda: sent.append(1), timeout_sec=3) is True
    assert sent == [1]


def test_gives_up_after_timeout_without_sending():
    inflight._count = 1
    sent = []
    assert shutdown_when_idle(lambda: sent.append(1), timeout_sec=0.2) is False
    assert sent == []
