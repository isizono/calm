"""RelayRuntime supervisor の unit test。

- is_configured / start の token 未設定時の縮退（start が例外を出さず False）
- 二重 start 防止
- SDK 側 file lock が既に取られていた場合の B-3 縮退（DispatcherAlreadyRunning 握り潰し）
- supervisor が例外後に再起動し、stop で終了すること
"""
from __future__ import annotations

import threading
import time

import pytest

from src.relay_sdk.outbox.dispatcher import DispatcherAlreadyRunning
from src.services.relay import runtime as runtime_module
from src.services.relay.runtime import RelayRuntime


@pytest.fixture(autouse=True)
def relay_state(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path / "relay-state"))
    monkeypatch.delenv("RELAY_BASE_URL", raising=False)
    monkeypatch.delenv("RELAY_IDENTITY", raising=False)
    yield


class TestConfiguration:
    def test_missing_token_prevents_start(self, monkeypatch):
        monkeypatch.delenv("RELAY_BEARER_TOKEN", raising=False)
        runtime = RelayRuntime(active_sessions_getter=lambda: set())
        assert runtime.is_configured() is False
        assert runtime.start() is False

    def test_double_start_returns_false(self, monkeypatch):
        monkeypatch.setenv("RELAY_BEARER_TOKEN", "test-token")
        runtime = RelayRuntime(active_sessions_getter=lambda: set())

        # 3 thread target を no-op に差し替えて起動だけ通す。
        monkeypatch.setattr(runtime, "_run_intake", lambda: None)
        monkeypatch.setattr(runtime, "_run_lease_loop", lambda: None)
        monkeypatch.setattr(runtime, "_run_dispatcher", lambda: None)

        assert runtime.start() is True
        assert runtime.start() is False
        runtime.stop()


class TestDispatcherFallback:
    def test_dispatcher_already_running_is_swallowed(self, monkeypatch):
        """B-3 は他プロセスが lock を持っていたら log のみで無効化する。"""
        monkeypatch.setenv("RELAY_BEARER_TOKEN", "test-token")

        raised: list[str] = []

        def fake_run_dispatcher(**kwargs):
            raised.append("called")
            raise DispatcherAlreadyRunning("既に他プロセスが保持")

        # モジュール属性を差し替える（runtime._run_dispatcher 内で from import されるため）
        monkeypatch.setattr(
            "src.relay_sdk.outbox.run_dispatcher", fake_run_dispatcher
        )
        monkeypatch.setattr(
            "src.relay_sdk.outbox.dispatcher.run_dispatcher", fake_run_dispatcher
        )

        runtime = RelayRuntime(
            active_sessions_getter=lambda: set(), outbox_db_path=":memory:"
        )
        # 例外を上位まで漏らさないこと（RuntimeError にならず None が返る）。
        runtime._run_dispatcher()
        assert raised == ["called"]


class TestSupervisorRestart:
    def test_supervisor_restarts_after_exception_then_stops(self):
        runtime = RelayRuntime(active_sessions_getter=lambda: set())
        counter = {"n": 0}

        def target():
            counter["n"] += 1
            if counter["n"] < 2:
                raise RuntimeError("boom")
            # 2 回目は待って正常終了
            runtime._stop_event.wait(0.05)

        thread = threading.Thread(
            target=runtime._supervise, args=("t-test", target), daemon=True
        )
        thread.start()
        # 再起動のバックオフは 1s から始まる。1.5s 待って 2 回目を確認する。
        deadline = time.monotonic() + 5.0
        while counter["n"] < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert counter["n"] >= 2
        runtime.stop()
        thread.join(timeout=3.0)
        assert not thread.is_alive()
