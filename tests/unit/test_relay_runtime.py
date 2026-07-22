"""RelayRuntime supervisor の unit test。

- is_configured / start の token 未設定時の縮退（start が例外を出さず False）
- 二重 start 防止
- SDK 側 file lock が既に取られていた場合の B-3 縮退（DispatcherAlreadyRunning 握り潰し）
- supervisor が例外後に再起動し、stop で終了すること
- `_run_dispatcher` の retry 関連 kwargs 解決（env 明示設定時の反映、未設定時の
  SDK 既定値へのフォールバック）
"""
from __future__ import annotations

import threading
import time

import pytest

from relay_sdk.outbox.dispatcher import DispatcherAlreadyRunning
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
            "relay_sdk.outbox.run_dispatcher", fake_run_dispatcher
        )
        monkeypatch.setattr(
            "relay_sdk.outbox.dispatcher.run_dispatcher", fake_run_dispatcher
        )

        runtime = RelayRuntime(
            active_sessions_getter=lambda: set(), outbox_db_path=":memory:"
        )
        # 例外を上位まで漏らさないこと（RuntimeError にならず None が返る）。
        runtime._run_dispatcher()
        assert raised == ["called"]


class TestDispatcherRetryTuning:
    """`_run_dispatcher` が SDK 側の env 解決ヘルパーの結果をそのまま run_dispatcher に渡すことの検証。"""

    def _capture_kwargs(self, monkeypatch) -> dict:
        captured: dict = {}

        def fake_run_dispatcher(**kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(
            "relay_sdk.outbox.run_dispatcher", fake_run_dispatcher
        )
        monkeypatch.setattr(
            "relay_sdk.outbox.dispatcher.run_dispatcher", fake_run_dispatcher
        )
        return captured

    def test_explicit_retry_env_vars_are_forwarded(self, monkeypatch):
        """SDK 側の RELAY_OUTBOX_RETRY_BACKOFF_* / TRANSIENT_RETRY_DEADLINE_S を
        明示設定すると、その値がそのまま run_dispatcher に渡る。
        """
        monkeypatch.setenv("RELAY_BEARER_TOKEN", "test-token")
        monkeypatch.setenv("RELAY_OUTBOX_RETRY_BACKOFF_BASE_MS", "500")
        monkeypatch.setenv("RELAY_OUTBOX_RETRY_BACKOFF_CAP_S", "60")
        monkeypatch.setenv("RELAY_OUTBOX_TRANSIENT_RETRY_DEADLINE_S", "3600")
        captured = self._capture_kwargs(monkeypatch)

        runtime = RelayRuntime(
            active_sessions_getter=lambda: set(), outbox_db_path=":memory:"
        )
        runtime._run_dispatcher()

        assert captured["retry_backoff_base_seconds"] == 0.5
        assert captured["retry_backoff_cap_seconds"] == 60.0
        assert captured["transient_retry_deadline_seconds"] == 3600.0

    def test_poll_and_retry_use_sdk_env_defaults(self, monkeypatch):
        """poll_interval / retry_backoff_* / dlq_gc_interval / http_timeout は
        SDK 側の env 解決ヘルパーがそのまま使われ、cc-memory 固有の既定値は持たない。
        """
        monkeypatch.setenv("RELAY_BEARER_TOKEN", "test-token")
        monkeypatch.delenv("RELAY_OUTBOX_POLL_INTERVAL_MS", raising=False)
        monkeypatch.delenv("RELAY_OUTBOX_RETRY_BACKOFF_BASE_MS", raising=False)
        monkeypatch.delenv("RELAY_OUTBOX_RETRY_BACKOFF_CAP_S", raising=False)
        monkeypatch.delenv("RELAY_OUTBOX_TRANSIENT_RETRY_DEADLINE_S", raising=False)
        monkeypatch.delenv("RELAY_OUTBOX_DLQ_GC_INTERVAL_S", raising=False)
        monkeypatch.delenv("RELAY_HTTP_TIMEOUT_S", raising=False)
        captured = self._capture_kwargs(monkeypatch)

        runtime = RelayRuntime(
            active_sessions_getter=lambda: set(), outbox_db_path=":memory:"
        )
        runtime._run_dispatcher()

        from relay_sdk import config as sdk_config

        assert captured["poll_interval_seconds"] == sdk_config.DEFAULT_POLL_INTERVAL_SECONDS
        assert (
            captured["retry_backoff_base_seconds"]
            == sdk_config.DEFAULT_RETRY_BACKOFF_BASE_SECONDS
        )
        assert (
            captured["retry_backoff_cap_seconds"]
            == sdk_config.DEFAULT_RETRY_BACKOFF_CAP_SECONDS
        )
        assert (
            captured["transient_retry_deadline_seconds"]
            == sdk_config.DEFAULT_TRANSIENT_RETRY_DEADLINE_SECONDS
        )
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


class TestHealthSnapshot:
    def test_before_start_is_unconfigured_and_empty(self, monkeypatch):
        monkeypatch.delenv("RELAY_BEARER_TOKEN", raising=False)
        runtime = RelayRuntime(active_sessions_getter=lambda: set())
        snapshot = runtime.health_snapshot()
        assert snapshot == {"configured": False, "running": False, "threads": {}}

    def test_after_start_all_three_threads_are_registered_alive(self, monkeypatch):
        monkeypatch.setenv("RELAY_BEARER_TOKEN", "test-token")
        runtime = RelayRuntime(active_sessions_getter=lambda: set())

        def _block_until_stop():
            runtime._stop_event.wait()

        monkeypatch.setattr(runtime, "_run_intake", _block_until_stop)
        monkeypatch.setattr(runtime, "_run_lease_loop", _block_until_stop)
        monkeypatch.setattr(runtime, "_run_dispatcher", _block_until_stop)

        assert runtime.start() is True
        try:
            snapshot = runtime.health_snapshot()
            assert snapshot["configured"] is True
            assert snapshot["running"] is True
            assert set(snapshot["threads"].keys()) == {
                "relay-intake",
                "relay-lease-loop",
                "relay-dispatcher",
            }
            for info in snapshot["threads"].values():
                assert info == {
                    "alive": True,
                    "restart_count": 0,
                    "last_restart_at": None,
                    "last_error": None,
                }
        finally:
            runtime.stop()

    def test_restart_increments_count_and_records_last_error(self, monkeypatch):
        monkeypatch.setenv("RELAY_BEARER_TOKEN", "test-token")
        runtime = RelayRuntime(active_sessions_getter=lambda: set())
        counter = {"n": 0}

        def _fail_once_then_block():
            counter["n"] += 1
            if counter["n"] == 1:
                raise RuntimeError("boom")
            runtime._stop_event.wait()

        monkeypatch.setattr(runtime, "_run_intake", _fail_once_then_block)
        monkeypatch.setattr(runtime, "_run_lease_loop", lambda: runtime._stop_event.wait())
        monkeypatch.setattr(runtime, "_run_dispatcher", lambda: runtime._stop_event.wait())

        assert runtime.start() is True
        try:
            deadline = time.monotonic() + 5.0
            while (
                runtime.health_snapshot()["threads"]["relay-intake"]["restart_count"] < 1
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
            snapshot = runtime.health_snapshot()
            intake_health = snapshot["threads"]["relay-intake"]
            assert intake_health["restart_count"] == 1
            assert intake_health["last_restart_at"] is not None
            assert "RuntimeError: boom" in intake_health["last_error"]
        finally:
            runtime.stop()

    def test_supervise_direct_call_without_spawn_does_not_raise_keyerror(self):
        """_spawn() を経由せず _supervise() を直接呼んでも health_snapshot 更新側で例外にならない。

        既存回帰テスト（TestSupervisorRestart）と同じ呼び出し形を踏襲し、
        self._health への setdefault 自己登録が機能することを確認する。
        """
        runtime = RelayRuntime(active_sessions_getter=lambda: set())
        counter = {"n": 0}

        def target():
            counter["n"] += 1
            if counter["n"] < 2:
                raise RuntimeError("boom")
            runtime._stop_event.wait(0.05)

        thread = threading.Thread(
            target=runtime._supervise, args=("t-test", target), daemon=True
        )
        thread.start()
        deadline = time.monotonic() + 5.0
        while counter["n"] < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert counter["n"] >= 2
        with runtime._health_lock:
            assert runtime._health["t-test"]["restart_count"] == 1
        runtime.stop()
        thread.join(timeout=3.0)
