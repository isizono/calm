"""_build_relay_inbox_section（hooks/session_start_hook.py）のユニットテスト。

subprocess経由のE2E（tests/e2e/test_session_start_hook.py）はhookプロセスが
実際のMCPリクエストコンテキストを持たないため、identity解決が常にNoneになる
「ゼロコスト」経路しか検証できない。ここでは関数を直接importして
get_relay_identity/count_unreadをmonkeypatchし、identityが解決できた場合の
表示ロジックを検証する。
"""
import os
import tempfile

import pytest

import src.services.relay.identity as relay_identity
import src.services.relay.inbox as relay_inbox
from src.db import init_database
from hooks.session_start_hook import _build_relay_inbox_section, _build_session_context


@pytest.fixture
def temp_db():
    """テスト用の一時的なデータベースを作成する"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


class TestIdentityUnresolved:
    def test_returns_empty_when_identity_unresolved(self, monkeypatch):
        """get_relay_identity()がNoneを返すとき、count_unreadを呼ばず空文字を返す"""
        monkeypatch.setattr(relay_identity, "get_relay_identity", lambda: None)
        called = {"count_unread": False}
        monkeypatch.setattr(
            relay_inbox,
            "count_unread",
            lambda session_id: called.__setitem__("count_unread", True) or 5,
        )
        assert _build_relay_inbox_section(None) == ""
        assert called["count_unread"] is False


class TestIdentityResolved:
    def test_returns_empty_when_unread_is_zero(self, monkeypatch):
        monkeypatch.setattr(relay_identity, "get_relay_identity", lambda: "stable-id-1")
        monkeypatch.setattr(relay_inbox, "count_unread", lambda session_id: 0)
        assert _build_relay_inbox_section(None) == ""

    def test_shows_count_when_unread_is_positive(self, monkeypatch):
        monkeypatch.setattr(relay_identity, "get_relay_identity", lambda: "stable-id-1")
        monkeypatch.setattr(relay_inbox, "count_unread", lambda session_id: 3)
        assert (
            _build_relay_inbox_section(None)
            == "relay inbox 未読: 3件 → relay_receive で確認\n"
        )

    def test_passes_resolved_identity_to_count_unread(self, monkeypatch):
        """count_unreadに渡される引数がget_relay_identity()の返り値と一致する"""
        received = {}
        monkeypatch.setattr(relay_identity, "get_relay_identity", lambda: "stable-id-42")

        def fake_count_unread(session_id):
            received["session_id"] = session_id
            return 1

        monkeypatch.setattr(relay_inbox, "count_unread", fake_count_unread)
        _build_relay_inbox_section(None)
        assert received["session_id"] == "stable-id-42"


class TestSessionContextProtection:
    def test_relay_section_exception_does_not_break_other_sections(
        self, temp_db, monkeypatch
    ):
        """本セクションが例外を投げても、buildersループの他セクション（静的
        セクション含む）の出力は失われない（per-builder try/exceptの保護範囲）。
        """

        def boom():
            raise RuntimeError("boom")

        monkeypatch.setattr(relay_identity, "get_relay_identity", boom)
        context = _build_session_context()
        assert "# コンテキスト取得フロー" in context
