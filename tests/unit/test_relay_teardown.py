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


def _write_declaration(session_id: str, subscription_id: str) -> dict:
    decl = declarations.ensure(session_id)
    decl["subscriptions"] = [
        {
            "subscription_id": subscription_id,
            "labels": ["room:planning"],
            "lease_expires_at": declarations.now_iso(),
            "created_at": declarations.now_iso(),
        }
    ]
    declarations.save(decl)
    return declarations.load(session_id)


class TestTeardownNoSnapshot:
    def test_none_snapshot_skips_relay_and_file_teardown(self, monkeypatch):
        """snapshot が None（declaration不在）なら relay 呼び出しも file 削除も行わない。"""
        relay_calls: list[dict] = []
        file_calls: list[str] = []
        monkeypatch.setattr(teardown, "_delete_relay_subscriptions", relay_calls.append)
        monkeypatch.setattr(teardown.lease_loop, "delete_orphan_state", file_calls.append)

        teardown._teardown("no-such-session", None)

        assert relay_calls == []
        assert file_calls == []


class TestTeardownDeletesRelaySubscriptionAndFileState:
    def test_relay_subscription_and_file_state_removed(self, monkeypatch):
        with FakeRelay() as fake:
            monkeypatch.setenv("RELAY_BASE_URL", fake.base_url)

            result = service.relay_subscribe(["room:planning"], caller_session_id="sess-1")
            assert "error" not in result, result
            subscription_id = result["subscription_id"]
            assert subscription_id in fake._subs

            snapshot = declarations.load("sess-1")
            teardown._teardown("sess-1", snapshot)

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

        decl = _write_declaration("sess-1", "sub-unreachable")
        inbox.append("sess-1", {"delivery_target": "sub:sub-unreachable", "publish_id": 1})

        teardown._teardown("sess-1", decl)

        assert declarations.load("sess-1") is None
        assert not inbox.inbox_path("sess-1").exists()


class TestTeardownSkipsRelayCallWithoutToken:
    def test_no_http_call_and_file_state_removed_without_token(self, monkeypatch):
        """token 未設定（relay 未接続環境）では HTTP クライアントを一切生成せず、
        file 撤去のみ行う。

        `_delete_relay_subscriptions` は例外を握り潰す実装なので、file 削除の
        成否だけでは「本当に HTTP を試みなかったか」を判定できない。ここでは
        `make_client` の呼び出し回数を直接観測する。
        """
        monkeypatch.delenv("RELAY_BEARER_TOKEN", raising=False)
        make_client_calls: list[tuple] = []
        monkeypatch.setattr(
            teardown, "make_client", lambda *a, **k: make_client_calls.append((a, k))
        )

        decl = _write_declaration("sess-1", "sub-1")

        teardown._teardown("sess-1", decl)

        assert make_client_calls == []
        assert declarations.load("sess-1") is None


class TestTeardownAbortsOnReSubscribeRace:
    """除去判定後に session が再登録・再購読した場合、撤去を中止することの検証。

    liveness TTL 失効は heartbeat 途絶のヒューリスティック判定であり誤検知しうる。
    除去判定（snapshot 取得）から撤去実行までの間に session が復帰して新しい
    subscription を作った場合、遅れて実行される撤去がその新しい宣言を破壊
    してはならない。
    """

    def test_skips_deletion_when_declaration_changed_since_snapshot(self, monkeypatch):
        snapshot = _write_declaration("sess-1", "sub-old")

        # 除去判定後、実は生きていた session が再登録・再購読して宣言を更新した。
        new_decl = declarations.load("sess-1")
        new_decl["subscriptions"] = [
            {
                "subscription_id": "sub-new",
                "labels": ["room:planning"],
                "lease_expires_at": declarations.now_iso(),
                "created_at": declarations.now_iso(),
            }
        ]
        declarations.save(new_decl)

        relay_calls: list[dict] = []
        monkeypatch.setattr(teardown, "_delete_relay_subscriptions", relay_calls.append)

        teardown._teardown("sess-1", snapshot)

        # 削除は中止され、新しい宣言がそのまま残っている。
        assert relay_calls == []
        current = declarations.load("sess-1")
        assert current is not None
        assert current["subscriptions"][0]["subscription_id"] == "sub-new"

    def test_proceeds_when_declaration_unchanged_since_snapshot(self, monkeypatch):
        """snapshot と実行時点の宣言が一致する場合は通常通り撤去する（回帰防止）。"""
        snapshot = _write_declaration("sess-1", "sub-1")

        relay_calls: list[dict] = []
        monkeypatch.setattr(teardown, "_delete_relay_subscriptions", relay_calls.append)

        teardown._teardown("sess-1", snapshot)

        assert len(relay_calls) == 1
        assert declarations.load("sess-1") is None


class TestSchedule:
    def test_schedule_runs_teardown_asynchronously_with_snapshot(self, monkeypatch):
        """schedule() はスレッドを起こし、除去判定時点の declaration を
        スナップショットとして _teardown に渡す。"""
        calls: list[tuple] = []
        done = threading.Event()

        def fake_teardown(session_id: str, snapshot) -> None:
            calls.append((session_id, snapshot))
            done.set()

        expected_snapshot = _write_declaration("sess-1", "sub-1")
        monkeypatch.setattr(teardown, "_teardown", fake_teardown)
        teardown.schedule("sess-1")

        assert done.wait(timeout=2.0), "schedule() が _teardown を実行しなかった"
        assert calls == [("sess-1", expected_snapshot)]

    def test_schedule_does_not_spawn_thread_without_declaration(self, monkeypatch):
        """declaration が存在しない session_id ではスレッドを起こさない
        （撤去対象の無い session のために無条件でスレッドを生成しない）。"""
        thread_calls: list[tuple] = []
        original_thread = teardown.threading.Thread

        def spy_thread(*args, **kwargs):
            thread_calls.append((args, kwargs))
            return original_thread(*args, **kwargs)

        monkeypatch.setattr(teardown.threading, "Thread", spy_thread)

        teardown.schedule("no-such-session")

        assert thread_calls == []
