"""RelayRuntime supervisor の unit test。

- is_configured / start の token 未設定時の縮退（start が例外を出さず False）
- 二重 start 防止
- SDK 側 file lock が既に取られていた場合の B-3 縮退（DispatcherAlreadyRunning 握り潰し）
- supervisor が例外後に再起動し、stop で終了すること
- `_run_dispatcher` の retry 関連 kwargs 解決（env 明示設定時の反映、未設定時の
  cc-memory 組み込み既定値へのフォールバック）
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


class TestDispatcherRetryTuning:
    """`_run_dispatcher` が retry 関連 kwargs をどう解決するかの検証。"""

    def _capture_kwargs(self, monkeypatch) -> dict:
        captured: dict = {}

        def fake_run_dispatcher(**kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(
            "src.relay_sdk.outbox.run_dispatcher", fake_run_dispatcher
        )
        monkeypatch.setattr(
            "src.relay_sdk.outbox.dispatcher.run_dispatcher", fake_run_dispatcher
        )
        return captured

    def test_explicit_env_vars_are_forwarded(self, monkeypatch):
        """RELAY_OUTBOX_* を明示設定すると、その値がそのまま run_dispatcher に渡る。"""
        monkeypatch.setenv("RELAY_BEARER_TOKEN", "test-token")
        monkeypatch.setenv("RELAY_OUTBOX_MAX_RETRY", "7")
        monkeypatch.setenv("RELAY_OUTBOX_INITIAL_BACKOFF_MS", "500")
        monkeypatch.setenv("RELAY_OUTBOX_BACKOFF_FACTOR", "3.0")
        captured = self._capture_kwargs(monkeypatch)

        runtime = RelayRuntime(
            active_sessions_getter=lambda: set(), outbox_db_path=":memory:"
        )
        runtime._run_dispatcher()

        assert captured["max_retry"] == 7
        assert captured["initial_backoff_seconds"] == 0.5
        assert captured["backoff_factor"] == 3.0

    def test_unset_env_falls_back_to_embedded_defaults(self, monkeypatch):
        """RELAY_OUTBOX_* 未設定時は cc-memory 組み込み既定値（10 / 1.0 / 2.0）を使い、
        vendored SDK 自身の既定値（5 / 0.1 / 2.0）とは異なる値になる。
        """
        monkeypatch.setenv("RELAY_BEARER_TOKEN", "test-token")
        monkeypatch.delenv("RELAY_OUTBOX_MAX_RETRY", raising=False)
        monkeypatch.delenv("RELAY_OUTBOX_INITIAL_BACKOFF_MS", raising=False)
        monkeypatch.delenv("RELAY_OUTBOX_BACKOFF_FACTOR", raising=False)
        captured = self._capture_kwargs(monkeypatch)

        runtime = RelayRuntime(
            active_sessions_getter=lambda: set(), outbox_db_path=":memory:"
        )
        runtime._run_dispatcher()

        assert captured["max_retry"] == 10
        assert captured["initial_backoff_seconds"] == 1.0
        assert captured["backoff_factor"] == 2.0
        # vendored SDK 自身の既定値（DispatcherConfig 相当）とは異なることを明示確認する。
        from src.relay_sdk import config as sdk_config

        assert captured["max_retry"] != sdk_config.DEFAULT_MAX_RETRY
        assert (
            captured["initial_backoff_seconds"]
            != sdk_config.DEFAULT_INITIAL_BACKOFF_SECONDS
        )

    def test_poll_and_timeout_use_sdk_env_defaults(self, monkeypatch):
        """poll_interval / dlq_gc_interval / http_timeout は SDK 側の env 解決
        ヘルパーがそのまま使われ、cc-memory 固有の既定値は持たない。
        """
        monkeypatch.setenv("RELAY_BEARER_TOKEN", "test-token")
        monkeypatch.delenv("RELAY_OUTBOX_POLL_INTERVAL_MS", raising=False)
        monkeypatch.delenv("RELAY_OUTBOX_DLQ_GC_INTERVAL_S", raising=False)
        monkeypatch.delenv("RELAY_HTTP_TIMEOUT_S", raising=False)
        captured = self._capture_kwargs(monkeypatch)

        runtime = RelayRuntime(
            active_sessions_getter=lambda: set(), outbox_db_path=":memory:"
        )
        runtime._run_dispatcher()

        from src.relay_sdk import config as sdk_config

        assert captured["poll_interval_seconds"] == sdk_config.DEFAULT_POLL_INTERVAL_SECONDS
        assert (
            captured["dlq_gc_interval_seconds"]
            == sdk_config.DEFAULT_DLQ_GC_INTERVAL_SECONDS
        )
        assert captured["http_timeout_seconds"] == sdk_config.DEFAULT_HTTP_TIMEOUT_SECONDS


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
