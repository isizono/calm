"""B-2 lease loop（renew / resubscribe / 孤児 sweep）の unit test。

renew/resubscribe 判定・孤児 sweep 判定・active session の生存ゲートを純関数として
検証する（時刻・active session 集合は引数で注入し、副作用は Mock で検証する）。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src.services.relay import config, declarations, inbox, lease_loop
from src.services.relay.lease_loop import (
    RenewAction,
    apply_action,
    compute_orphan_inbox_files,
    compute_orphan_sessions,
    compute_renew_actions,
    delete_orphan_inbox_file,
    delete_orphan_state,
)


@pytest.fixture(autouse=True)
def relay_state(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path / "relay-state"))
    monkeypatch.setenv("RELAY_BEARER_TOKEN", "test-token")
    monkeypatch.delenv("RELAY_BASE_URL", raising=False)
    monkeypatch.delenv("RELAY_IDENTITY", raising=False)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_declaration(session_id: str, subs: list[dict]) -> None:
    decl = declarations.ensure(session_id)
    decl["subscriptions"] = subs
    declarations.save(decl)


# ---------------------------------------------------------------------------
# compute_renew_actions
# ---------------------------------------------------------------------------


class TestComputeRenewActions:
    def test_returns_renew_for_near_expiry(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # ttl=300, margin=100s。残り 50s なら renew
        _write_declaration(
            "sess-a",
            [
                {
                    "subscription_id": "s-1",
                    "labels": ["x"],
                    "lease_expires_at": _iso(now + timedelta(seconds=50)),
                }
            ],
        )
        actions = compute_renew_actions(
            declarations.load_all(),
            active_session_ids={"sess-a"},
            lease_ttl_seconds=300,
            now=now,
        )
        assert actions == [
            RenewAction(
                session_id="sess-a",
                subscription_id="s-1",
                labels=["x"],
                kind="renew",
            )
        ]

    def test_returns_resubscribe_for_expired(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        _write_declaration(
            "sess-a",
            [
                {
                    "subscription_id": "s-1",
                    "labels": ["x"],
                    "lease_expires_at": _iso(now - timedelta(seconds=1)),
                }
            ],
        )
        actions = compute_renew_actions(
            declarations.load_all(),
            active_session_ids={"sess-a"},
            lease_ttl_seconds=300,
            now=now,
        )
        assert actions[0].kind == "resubscribe"

    def test_skips_healthy_lease(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        _write_declaration(
            "sess-a",
            [
                {
                    "subscription_id": "s-1",
                    "labels": ["x"],
                    "lease_expires_at": _iso(now + timedelta(seconds=250)),
                }
            ],
        )
        actions = compute_renew_actions(
            declarations.load_all(),
            active_session_ids={"sess-a"},
            lease_ttl_seconds=300,
            now=now,
        )
        assert actions == []

    def test_inactive_session_is_skipped(self):
        """SessionManager 登録外の session は renew しない（自然失効させる）。"""
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        _write_declaration(
            "sess-dead",
            [
                {
                    "subscription_id": "s-1",
                    "labels": ["x"],
                    "lease_expires_at": _iso(now + timedelta(seconds=10)),
                }
            ],
        )
        actions = compute_renew_actions(
            declarations.load_all(),
            active_session_ids=set(),  # 誰も active でない
            lease_ttl_seconds=300,
            now=now,
        )
        assert actions == []

    def test_missing_lease_field_triggers_resubscribe(self):
        _write_declaration(
            "sess-a",
            [{"subscription_id": "s-1", "labels": ["x"]}],
        )
        actions = compute_renew_actions(
            declarations.load_all(),
            active_session_ids={"sess-a"},
            lease_ttl_seconds=300,
        )
        assert actions[0].kind == "resubscribe"


# ---------------------------------------------------------------------------
# compute_orphan_sessions
# ---------------------------------------------------------------------------


class TestComputeOrphanSessions:
    def test_declaration_older_than_threshold_is_orphan(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        _write_declaration(
            "sess-old",
            [
                {
                    "subscription_id": "s-1",
                    "labels": ["x"],
                    "lease_expires_at": _iso(now - timedelta(hours=25)),
                }
            ],
        )
        orphans = compute_orphan_sessions(
            declarations.load_all(),
            now=now,
            threshold_seconds=24 * 3600,
        )
        assert orphans == ["sess-old"]

    def test_declaration_within_threshold_is_not_orphan(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        _write_declaration(
            "sess-fresh",
            [
                {
                    "subscription_id": "s-1",
                    "labels": ["x"],
                    "lease_expires_at": _iso(now - timedelta(hours=23)),
                }
            ],
        )
        orphans = compute_orphan_sessions(
            declarations.load_all(),
            now=now,
            threshold_seconds=24 * 3600,
        )
        assert orphans == []

    def test_max_lease_governs_when_multiple_subscriptions(self):
        """複数 subscription がある場合、最大 lease が閾値内なら孤児ではない。"""
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        _write_declaration(
            "sess-mixed",
            [
                {
                    "subscription_id": "old",
                    "labels": ["a"],
                    "lease_expires_at": _iso(now - timedelta(hours=48)),
                },
                {
                    "subscription_id": "fresh",
                    "labels": ["b"],
                    "lease_expires_at": _iso(now - timedelta(hours=1)),
                },
            ],
        )
        orphans = compute_orphan_sessions(
            declarations.load_all(),
            now=now,
            threshold_seconds=24 * 3600,
        )
        assert orphans == []

    def test_empty_subscriptions_treated_as_orphan(self):
        """subscription が 1 つも無い declaration は「無限に古い」扱いで孤児。"""
        _write_declaration("sess-empty", [])
        orphans = compute_orphan_sessions(declarations.load_all())
        assert "sess-empty" in orphans


# ---------------------------------------------------------------------------
# delete_orphan_state
# ---------------------------------------------------------------------------


class TestDeleteOrphanState:
    def test_removes_declaration_inbox_and_cursor(self):
        _write_declaration(
            "sess-old",
            [{"subscription_id": "s-1", "labels": ["x"]}],
        )
        inbox.append("sess-old", {"n": 1})
        # cursor を作るために drain を呼んで途中まで進める
        inbox.drain("sess-old")
        assert declarations.load("sess-old") is not None
        assert inbox.inbox_path("sess-old").exists()

        delete_orphan_state("sess-old")

        assert declarations.load("sess-old") is None
        assert not inbox.inbox_path("sess-old").exists()
        assert not inbox.cursor_path("sess-old").exists()


# ---------------------------------------------------------------------------
# compute_orphan_inbox_files
# ---------------------------------------------------------------------------


class TestComputeOrphanInboxFiles:
    def test_file_older_than_threshold_without_declaration_is_orphan(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        inbox.ensure_inbox_file("precreated-only")
        path = inbox.inbox_path("precreated-only")
        old_ts = (now - timedelta(hours=25)).timestamp()
        os.utime(path, (old_ts, old_ts))

        orphans = compute_orphan_inbox_files(
            inbox.list_inbox_files(),
            declared_session_ids=set(),
            now=now,
            threshold_seconds=24 * 3600,
        )
        assert orphans == [("precreated-only", path)]

    def test_file_within_threshold_is_not_orphan(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        inbox.ensure_inbox_file("recent")
        path = inbox.inbox_path("recent")
        recent_ts = (now - timedelta(hours=1)).timestamp()
        os.utime(path, (recent_ts, recent_ts))

        orphans = compute_orphan_inbox_files(
            inbox.list_inbox_files(),
            declared_session_ids=set(),
            now=now,
            threshold_seconds=24 * 3600,
        )
        assert orphans == []

    def test_file_with_declaration_is_excluded_even_if_old(self):
        """declarationがあるsessionのinbox fileはcompute_orphan_sessions側が
        扱うため、ここでは古くても孤児判定しない(declared_session_idsで除外)。"""
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        inbox.ensure_inbox_file("has-declaration")
        path = inbox.inbox_path("has-declaration")
        old_ts = (now - timedelta(hours=25)).timestamp()
        os.utime(path, (old_ts, old_ts))

        orphans = compute_orphan_inbox_files(
            inbox.list_inbox_files(),
            declared_session_ids={"has-declaration"},
            now=now,
            threshold_seconds=24 * 3600,
        )
        assert orphans == []


# ---------------------------------------------------------------------------
# delete_orphan_inbox_file
# ---------------------------------------------------------------------------


class TestDeleteOrphanInboxFile:
    def test_removes_inbox_and_cursor_file(self):
        inbox.append("precreated-only", {"n": 1})
        inbox.drain("precreated-only", limit=0)  # cursor fileを作る
        path = inbox.inbox_path("precreated-only")
        assert path.exists()
        assert inbox.cursor_path("precreated-only").exists()

        delete_orphan_inbox_file("precreated-only", path)

        assert not path.exists()
        assert not inbox.cursor_path("precreated-only").exists()

    def test_missing_cursor_file_does_not_raise(self):
        inbox.ensure_inbox_file("no-cursor")
        path = inbox.inbox_path("no-cursor")
        assert not inbox.cursor_path("no-cursor").exists()

        delete_orphan_inbox_file("no-cursor", path)

        assert not path.exists()


# ---------------------------------------------------------------------------
# _sweep_orphans（declarationベース・precreateベース両方の配線確認）
# ---------------------------------------------------------------------------


class TestSweepOrphans:
    def test_sweeps_both_declaration_and_precreated_inbox_orphans(self):
        now = datetime.now(timezone.utc)
        _write_declaration(
            "declared-orphan",
            [
                {
                    "subscription_id": "s-1",
                    "labels": ["x"],
                    "lease_expires_at": _iso(now - timedelta(hours=25)),
                }
            ],
        )
        inbox.ensure_inbox_file("precreated-orphan")
        old_ts = (now - timedelta(hours=25)).timestamp()
        os.utime(inbox.inbox_path("precreated-orphan"), (old_ts, old_ts))

        lease_loop._sweep_orphans(24 * 3600)

        assert declarations.load("declared-orphan") is None
        assert not inbox.inbox_path("declared-orphan").exists()
        assert not inbox.inbox_path("precreated-orphan").exists()

    def test_does_not_sweep_recent_precreated_inbox_file(self):
        inbox.ensure_inbox_file("precreated-recent")

        lease_loop._sweep_orphans(24 * 3600)

        assert inbox.inbox_path("precreated-recent").exists()


# ---------------------------------------------------------------------------
# apply_action（httpx.MockTransport 経由）
# ---------------------------------------------------------------------------


class TestApplyAction:
    def _make_client(self, dispatcher) -> httpx.Client:
        return httpx.Client(
            transport=httpx.MockTransport(dispatcher), base_url="http://relay.test"
        )

    def test_renew_updates_lease_expires_at(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        _write_declaration(
            "sess-a",
            [
                {
                    "subscription_id": "s-1",
                    "labels": ["x"],
                    "lease_expires_at": _iso(now + timedelta(seconds=10)),
                }
            ],
        )
        renewed_at = _iso(now + timedelta(seconds=300))
        recorded: list[tuple[str, str]] = []

        def dispatch(request: httpx.Request) -> httpx.Response:
            recorded.append((request.method, request.url.path))
            return httpx.Response(200, json={"lease_expires_at": renewed_at})

        action = RenewAction(
            session_id="sess-a",
            subscription_id="s-1",
            labels=["x"],
            kind="renew",
        )
        with self._make_client(dispatch) as client:
            apply_action(
                action,
                client,
                lease_ttl_seconds=300,
                subscriber_identity="cc-memory",
            )
        assert recorded == [("PUT", "/subscriptions/s-1/lease")]
        decl = declarations.load("sess-a")
        assert decl["subscriptions"][0]["lease_expires_at"] == renewed_at

    def test_renew_404_falls_back_to_resubscribe(self):
        """relay 側 subscription が消えていたら新規 subscribe で自己修復する。"""
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        _write_declaration(
            "sess-a",
            [
                {
                    "subscription_id": "s-old",
                    "labels": ["x", "handle:foo"],
                    "lease_expires_at": _iso(now + timedelta(seconds=10)),
                }
            ],
        )
        recorded: list[tuple[str, str, dict]] = []
        new_expires = _iso(now + timedelta(seconds=300))

        def dispatch(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content) if request.content else {}
            recorded.append((request.method, request.url.path, body))
            if request.method == "PUT" and request.url.path == "/subscriptions/s-old/lease":
                return httpx.Response(
                    404, json={"code": "SubscriptionNotFoundError", "message": "gone"}
                )
            if request.method == "POST" and request.url.path == "/subscriptions":
                return httpx.Response(
                    201,
                    json={"subscription_id": "s-new", "lease_expires_at": new_expires},
                )
            return httpx.Response(400, json={})

        reconfigure = __import__("threading").Event()
        action = RenewAction(
            session_id="sess-a",
            subscription_id="s-old",
            labels=["x", "handle:foo"],
            kind="renew",
        )
        with self._make_client(dispatch) as client:
            apply_action(
                action,
                client,
                lease_ttl_seconds=300,
                subscriber_identity="cc-memory",
                reconfigure_event=reconfigure,
            )
        methods = [(m, p) for m, p, _ in recorded]
        assert ("PUT", "/subscriptions/s-old/lease") in methods
        assert ("POST", "/subscriptions") in methods
        decl = declarations.load("sess-a")
        assert decl["subscriptions"][0]["subscription_id"] == "s-new"
        assert decl["subscriptions"][0]["lease_expires_at"] == new_expires
        assert reconfigure.is_set(), "intake reconfigure イベントが立っていない"

    def test_resubscribe_transient_error_leaves_state_unchanged(self):
        _write_declaration(
            "sess-a",
            [
                {
                    "subscription_id": "s-old",
                    "labels": ["x"],
                    "lease_expires_at": "2020-01-01T00:00:00Z",
                }
            ],
        )

        def dispatch(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"code": "TransientError"})

        action = RenewAction(
            session_id="sess-a",
            subscription_id="s-old",
            labels=["x"],
            kind="resubscribe",
        )
        with self._make_client(dispatch) as client:
            apply_action(
                action,
                client,
                lease_ttl_seconds=300,
                subscriber_identity="cc-memory",
            )
        decl = declarations.load("sess-a")
        # state は据え置き（次回スキャンで再試行される）
        assert decl["subscriptions"][0]["subscription_id"] == "s-old"


# ---------------------------------------------------------------------------
# integration-ish: run() 1 周分（stop_event を短時間で立てる）
# ---------------------------------------------------------------------------


class TestRunLoopSmoke:
    def test_run_processes_actions_and_stops(self, monkeypatch):
        import threading

        now = datetime.now(timezone.utc)
        _write_declaration(
            "sess-a",
            [
                {
                    "subscription_id": "s-1",
                    "labels": ["x"],
                    "lease_expires_at": _iso(now - timedelta(seconds=1)),
                }
            ],
        )

        stop = threading.Event()
        called: list[str] = []

        def dispatch(request: httpx.Request) -> httpx.Response:
            called.append(request.url.path)
            if request.method == "POST" and request.url.path == "/subscriptions":
                stop.set()
                return httpx.Response(
                    201,
                    json={
                        "subscription_id": "s-new",
                        "lease_expires_at": _iso(now + timedelta(seconds=300)),
                    },
                )
            return httpx.Response(200, json={})

        def factory(base_url, **kwargs):
            return httpx.Client(
                transport=httpx.MockTransport(dispatch), base_url=base_url
            )

        monkeypatch.setattr(lease_loop, "make_client", factory)

        lease_loop.run(
            stop,
            lambda: {"sess-a"},
            renew_interval_seconds=0.01,
            sweep_interval_seconds=3600,
        )
        assert "/subscriptions" in called
