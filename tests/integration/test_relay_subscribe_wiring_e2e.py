"""relay_subscribe tool wrapper（main.py）から RelayRuntime.notify_reconfigure() までの
一気通貫 integration test。

シナリオ:
1. FakeRelay を起動し、1件目の subscription を張って RelayRuntime（intake 実体入り）を
   起動する。intake が最初の SSE 接続を確立し、subscribe 済み labels のメッセージを
   受信できることを確認する。
2. `src.main.relay_subscribe`（tool wrapper 自体）経由で 2件目（別 labels）を subscribe
   する。`reused: false` のはずで、wrapper が `get_relay_runtime()` 経由で
   `notify_reconfigure()` を呼ぶ配線を実際に通過する。
3. 2件目の labels にマッチする publish を FakeRelay 側に積み、intake の SSE 再接続後に
   取りこぼしなく inbox へ届くことを確認する。

`tests/integration/test_relay_intake_e2e.py` の FakeRelay + 手動 intake 駆動という下回り
を流用しつつ、2件目の subscribe 呼び出しだけは `src.main.relay_subscribe` を直接 import
して呼ぶ（`tests/integration/test_worker_guard_tools.py` のパターンに倣う）。これにより
`RelayRuntime` インスタンスの生成・`get_relay_runtime()` の返り値配線・
`notify_reconfigure()` 呼び出しの3点を実際に通過した上での snapshot 反映を検証できる。
"""
from __future__ import annotations

import time

import pytest

import src.main as main_module
from src.relay_sdk.testing import FakeRelay
from src.services.relay import service
from src.services.relay.runtime import RelayRuntime


@pytest.fixture(autouse=True)
def relay_state(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path / "relay-state"))
    monkeypatch.setenv("RELAY_BEARER_TOKEN", "test-token")
    monkeypatch.setenv("RELAY_IDENTITY", "cc-memory")


def _wait_until(predicate, *, timeout: float, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    return None


def test_second_subscribe_via_tool_wrapper_notifies_runtime_and_is_received(
    monkeypatch, temp_db
):
    with FakeRelay() as fake:
        monkeypatch.setenv("RELAY_BASE_URL", fake.base_url)

        # 1件目の subscription を先に張っておく。RelayRuntime._run_intake は既定の
        # rescan_interval_seconds（5秒）しか指定できないため、intake 起動時点で
        # declaration が既に存在していれば、rescan 待ちなしに初回接続へ入れる。
        first = service.relay_subscribe(
            ["topic:planning"], caller_session_id="sess-1"
        )
        assert "error" not in first, first
        assert first["reused"] is False

        runtime = RelayRuntime(active_sessions_getter=lambda: {"sess-1"})
        # lease renew / outbox dispatcher は本テストの対象外。intake の実接続だけを
        # 実際に走らせる（tests/unit/test_relay_runtime.py と同じ縮退パターン）。
        monkeypatch.setattr(
            runtime, "_run_lease_loop", lambda: runtime._stop_event.wait()
        )
        monkeypatch.setattr(
            runtime, "_run_dispatcher", lambda: runtime._stop_event.wait()
        )
        assert runtime.start() is True

        try:
            # intake が1件目の subscription で実際に接続確立していることを、
            # メッセージ到達で確認する。
            fake.publish(
                ref_type="message", ref_id="warm-up", labels=first["labels"]
            )

            def _first_delivered():
                result = service.relay_receive(caller_session_id="sess-1")
                return result if result["count"] >= 1 else None

            assert _wait_until(_first_delivered, timeout=5.0) is not None, (
                "1件目 subscription が intake の初回接続で反映されなかった"
            )

            # ここから配線本体の検証: tool wrapper 経由で2件目（別labels）を subscribe。
            monkeypatch.setattr(main_module, "_relay_runtime", runtime)
            monkeypatch.setattr(
                main_module, "get_caller_session_id", lambda: "sess-1"
            )

            second = main_module.relay_subscribe(["topic:other"])
            assert "error" not in second, second
            assert second["reused"] is False
            assert second["subscription_id"] != first["subscription_id"]

            # 2件目の subscription_id は intake が最初に接続した時点の snapshot には
            # 含まれていない。notify_reconfigure() 配線がなければ、次の偶発的な
            # 再接続（エラー / read timeout）が起きるまで一切届かない。
            fake.publish(
                ref_type="message", ref_id="hello-2", labels=second["labels"]
            )

            def _second_delivered():
                result = service.relay_receive(caller_session_id="sess-1")
                for msg in result["messages"]:
                    if msg["ref"] == {"type": "message", "id": "hello-2"}:
                        return msg
                return None

            delivered = _wait_until(_second_delivered, timeout=5.0)
            assert delivered is not None, (
                "relay_subscribe tool wrapper の notify_reconfigure() 配線が機能せず、"
                "2件目 subscription 宛のメッセージが反映されなかった"
            )
        finally:
            runtime.stop()


def test_reused_subscribe_via_tool_wrapper_does_not_trigger_reconfigure(
    monkeypatch, temp_db
):
    """同一 labels での再 subscribe（reused: true）は SSE 再接続をトリガーしない
    ことを、実際の RelayRuntime + FakeRelay を使った統合レベルで検証する。

    reused: false 側の再接続反映は上のテストで検証済み。ここでは reused: true が
    reconfigure_event を set しない（＝無用な再接続 churn を起こさない）ことと、
    それに伴い既存 subscription の受信が継続して機能することを確認する。
    """
    with FakeRelay() as fake:
        monkeypatch.setenv("RELAY_BASE_URL", fake.base_url)

        first = service.relay_subscribe(
            ["topic:planning"], caller_session_id="sess-1"
        )
        assert "error" not in first, first
        assert first["reused"] is False

        runtime = RelayRuntime(active_sessions_getter=lambda: {"sess-1"})
        monkeypatch.setattr(
            runtime, "_run_lease_loop", lambda: runtime._stop_event.wait()
        )
        monkeypatch.setattr(
            runtime, "_run_dispatcher", lambda: runtime._stop_event.wait()
        )
        assert runtime.start() is True

        try:
            fake.publish(
                ref_type="message", ref_id="warm-up", labels=first["labels"]
            )

            def _first_delivered():
                result = service.relay_receive(caller_session_id="sess-1")
                return result if result["count"] >= 1 else None

            assert _wait_until(_first_delivered, timeout=5.0) is not None, (
                "1件目 subscription が intake の初回接続で反映されなかった"
            )
            assert not runtime._reconfigure_event.is_set()

            monkeypatch.setattr(main_module, "_relay_runtime", runtime)
            monkeypatch.setattr(
                main_module, "get_caller_session_id", lambda: "sess-1"
            )

            # 同一 labels での再 subscribe。lease が有効なので reused: true のはず。
            second = main_module.relay_subscribe(["topic:planning"])
            assert "error" not in second, second
            assert second["reused"] is True
            assert second["subscription_id"] == first["subscription_id"]

            # reused: true では notify_reconfigure() が呼ばれないため、
            # reconfigure_event は set されないまま（再接続 churn が起きていない）。
            time.sleep(0.2)
            assert not runtime._reconfigure_event.is_set()

            # 既存 subscription 宛のメッセージが reused 呼び出し後も引き続き正常に
            # 届くこと（再接続 churn で受信経路が壊れていないことの傍証）。
            fake.publish(
                ref_type="message", ref_id="after-reused", labels=first["labels"]
            )

            def _after_reused_delivered():
                result = service.relay_receive(caller_session_id="sess-1")
                for msg in result["messages"]:
                    if msg["ref"] == {"type": "message", "id": "after-reused"}:
                        return msg
                return None

            delivered = _wait_until(_after_reused_delivered, timeout=5.0)
            assert delivered is not None, (
                "reused: true 後、既存 subscription 宛のメッセージ受信が機能しなくなった"
            )
        finally:
            runtime.stop()
