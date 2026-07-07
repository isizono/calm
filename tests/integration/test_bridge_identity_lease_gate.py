"""bridge identity 経由で SessionManager 側 ID と declaration 側 ID が

一致し、lease_loop の生存ゲート（compute_renew_actions の active_session_ids
判定）が実際に renew 対象を検出できることを確認する統合テスト。

既存の lease_loop 単体テスト（test_relay_lease_loop.py）は決め打ち文字列で
ロジック自体を検証済みのため、ここでは
「relay_identity.get_relay_identity() が返す値」と
「SessionManager.session_ids に登録されている値」が同一の bridge identity
文字列であることそのものを確認する。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.infra.session_manager import SessionManager
from src.services.relay import declarations
from src.services.relay import identity as relay_identity
from src.services.relay.lease_loop import compute_renew_actions


@pytest.fixture(autouse=True)
def relay_state(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path / "relay-state"))
    monkeypatch.setenv("RELAY_BEARER_TOKEN", "test-token")
    monkeypatch.delenv("RELAY_BASE_URL", raising=False)
    monkeypatch.delenv("RELAY_IDENTITY", raising=False)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class TestBridgeIdentityMatchesSessionManager:
    def test_renew_gate_recognizes_session_registered_via_bridge_header(
        self, monkeypatch
    ):
        """SessionManagerにbridge identityで登録されたsessionのdeclarationが
        compute_renew_actionsのrenew対象として検出されること（両ID空間が
        本設計により一致することの end-to-end 確認）。
        """
        bridge_id = "bridge-uuid-xyz"

        # launcher.py 相当: bridge identity ヘッダを付けたリクエストが来た体で
        # get_relay_identity() を解決する
        monkeypatch.setattr(
            "fastmcp.server.dependencies.get_http_headers",
            lambda: {relay_identity.BRIDGE_SESSION_HEADER: bridge_id},
        )
        resolved_identity = relay_identity.get_relay_identity()
        assert resolved_identity == bridge_id

        # SessionManager 側にも同じ bridge_id を register する
        # （main.py の /session/register 経由、launcher.py の _session_id 相当）
        mgr = SessionManager(liveness_timeout_sec=0)
        mgr.register(bridge_id)

        # declaration は resolved_identity をキーに保存される（service.py 経由の想定）
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        decl = declarations.ensure(resolved_identity)
        decl["subscriptions"] = [
            {
                "subscription_id": "sub-1",
                "labels": ["room:test"],
                # ttl=300, margin=100s。残り50sならrenew対象
                "lease_expires_at": _iso(now + timedelta(seconds=50)),
            }
        ]
        declarations.save(decl)

        snapshot = declarations.load_all()
        actions = compute_renew_actions(
            snapshot,
            active_session_ids=mgr.session_ids,
            now=now,
        )

        assert len(actions) == 1
        assert actions[0].session_id == bridge_id
        assert actions[0].kind == "renew"

    def test_renew_gate_excludes_session_not_registered(self, monkeypatch):
        """SessionManagerに登録されていないsession_idのdeclarationはrenew対象外
        （bridge identityが安定しても、SessionManager登録自体が無ければ生存
        ゲートは通らないことの確認）。
        """
        bridge_id = "bridge-uuid-unregistered"
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        decl = declarations.ensure(bridge_id)
        decl["subscriptions"] = [
            {
                "subscription_id": "sub-1",
                "labels": ["room:test"],
                "lease_expires_at": _iso(now + timedelta(seconds=50)),
            }
        ]
        declarations.save(decl)

        mgr = SessionManager(liveness_timeout_sec=0)  # bridge_idをregisterしない

        snapshot = declarations.load_all()
        actions = compute_renew_actions(
            snapshot,
            active_session_ids=mgr.session_ids,
            now=now,
        )
        assert actions == []
