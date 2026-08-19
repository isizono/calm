"""session 除去時の subscription 撤去（`src/services/relay/teardown.py`）の unit test。

`_teardown` の副作用（relay 側 DELETE / declaration・inbox・cursor 削除）を、
`schedule()` のスレッド生成を経由せず直接呼び出して検証する。`schedule()` 自体は
専用のスレッド join テストで別途カバーする（daemon thread がテストの
tmp_path cleanup 後まで生き残ると、本来の分離を破って実 state dir に書きかねない
ため、他のテストでは _teardown を直接呼ぶ）。
"""
from __future__ import annotations

import threading

import pytest

from relay_sdk.testing import FakeRelay
from src.services.relay import declarations, inbox, service, teardown


@pytest.fixture(autouse=True)
def relay_state(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path / "relay-state"))
    monkeypatch.setenv("RELAY_BEARER_TOKEN", "test-token")
    monkeypatch.delenv("RELAY_BASE_URL", raising=False)
    monkeypatch.delenv("RELAY_IDENTITY", raising=False)


class TestTeardownNoDeclaration:
    def test_missing_declaration_is_noop(self):
        """declaration が存在しない session_id を渡してもエラーにならない。"""
        teardown._teardown("no-such-session")  # raiseしないことの確認


class TestTeardownDeletesRelaySubscriptionAndFileState:
    def test_relay_subscription_and_file_state_removed(self, monkeypatch):
        with FakeRelay() as fake:
            monkeypatch.setenv("RELAY_BASE_URL", fake.base_url)

            result = service.relay_subscribe(["room:planning"], caller_session_id="sess-1")
            assert "error" not in result, result
            subscription_id = result["subscription_id"]
            assert subscription_id in fake._subs

            teardown._teardown("sess-1")

            # relay 側から削除されている。
            assert subscription_id not in fake._subs
            # declaration file / inbox / cursor が削除されている。
            assert declarations.load("sess-1") is None
            assert not inbox.inbox_path("sess-1").exists()
            assert not inbox.cursor_path("sess-1").exists()


class TestTeardownContinuesFileDeletionOnRelayFailure:
    def test_file_state_removed_even_if_relay_unreachable(self, monkeypatch, tmp_path):
        """relay に到達できなくても declaration/inbox/cursor の削除は完遂する。"""
        # 誰も listen していないポートを指し、接続失敗を発生させる。
        monkeypatch.setenv("RELAY_BASE_URL", "http://127.0.0.1:1")

        decl = declarations.ensure("sess-1")
        decl["subscriptions"] = [
            {
                "subscription_id": "sub-unreachable",
                "labels": ["room:planning"],
                "lease_expires_at": declarations.now_iso(),
                "created_at": declarations.now_iso(),
            }
        ]
        declarations.save(decl)
        inbox.append("sess-1", {"delivery_target": "sub:sub-unreachable", "publish_id": 1})

        teardown._teardown("sess-1")

        assert declarations.load("sess-1") is None
        assert not inbox.inbox_path("sess-1").exists()


class TestTeardownSkipsRelayCallWithoutToken:
    def test_file_state_removed_without_token(self, monkeypatch):
        """token 未設定（relay 未接続環境）では HTTP を試みず file 撤去のみ行う。"""
        monkeypatch.delenv("RELAY_BEARER_TOKEN", raising=False)

        decl = declarations.ensure("sess-1")
        decl["subscriptions"] = [
            {
                "subscription_id": "sub-1",
                "labels": ["room:planning"],
                "lease_expires_at": declarations.now_iso(),
                "created_at": declarations.now_iso(),
            }
        ]
        declarations.save(decl)

        teardown._teardown("sess-1")  # HTTP を試みず例外も出さないことの確認

        assert declarations.load("sess-1") is None


class TestSchedule:
    def test_schedule_runs_teardown_asynchronously(self, monkeypatch):
        """schedule() はスレッドを起こして _teardown を実行する。"""
        calls: list[str] = []
        done = threading.Event()

        def fake_teardown(session_id: str) -> None:
            calls.append(session_id)
            done.set()

        monkeypatch.setattr(teardown, "_teardown", fake_teardown)
        teardown.schedule("sess-1")

        assert done.wait(timeout=2.0), "schedule() が _teardown を実行しなかった"
        assert calls == ["sess-1"]
