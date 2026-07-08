"""_build_relay_inbox_section（hooks/session_start_hook.py）のユニットテスト。

subprocess経由のE2E（tests/e2e/test_session_start_hook.py）はhookプロセスが
実際のMCPリクエストコンテキストを持たないため、identity解決が常にNoneになる
「ゼロコスト」経路しか検証できない。ここでは関数を直接importして
get_relay_identity/resolve_identity_by_ancestry/count_unread/get_tokenを
monkeypatchし、identityが解決できた場合の表示ロジックを検証する。
"""
import os
import tempfile

import pytest

import src.services.relay.config as relay_config
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


def _make_inbox_file(tmp_path, identity: str):
    """count_unread/inbox_pathが実ファイルシステムに依存するため、
    identityの inbox file を実際に作成するヘルパー。
    """
    monkeypatch_state = {}
    inbox_dir = tmp_path / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    path = inbox_dir / f"session-{identity}.jsonl"
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture
def relay_configured(monkeypatch, tmp_path):
    """relay_config.get_token()がtruthyを返す状態にする。"""
    monkeypatch.setattr(relay_config, "get_state_dir", lambda: tmp_path)
    monkeypatch.setenv("RELAY_BEARER_TOKEN", "dummy-token")
    yield tmp_path


class TestIdentityUnresolved:
    def test_returns_empty_when_both_resolvers_fail(self, monkeypatch):
        """header/ctx・祖先pid経路どちらもNoneのとき、count_unreadを呼ばず空文字を返す"""
        monkeypatch.setattr(relay_identity, "get_relay_identity", lambda: None)
        monkeypatch.setattr(relay_identity, "resolve_identity_by_ancestry", lambda: None)
        called = {"count_unread": False}
        monkeypatch.setattr(
            relay_inbox,
            "count_unread",
            lambda session_id: called.__setitem__("count_unread", True) or 5,
        )
        assert _build_relay_inbox_section(None) == ""
        assert called["count_unread"] is False

    def test_falls_back_to_ancestry_when_header_resolution_fails(
        self, monkeypatch, relay_configured
    ):
        """get_relay_identity()がNoneでもresolve_identity_by_ancestry()が
        解決すれば、その識別子で表示ロジックが進む"""
        monkeypatch.setattr(relay_identity, "get_relay_identity", lambda: None)
        monkeypatch.setattr(
            relay_identity, "resolve_identity_by_ancestry", lambda: "ancestry-id-1"
        )
        _make_inbox_file(relay_configured, "ancestry-id-1")
        monkeypatch.setattr(relay_inbox, "count_unread", lambda session_id: 2)
        result = _build_relay_inbox_section(None)
        assert "relay inbox 未読: 2件" in result
        assert "Monitorツール" in result


class TestGateConditions:
    def test_returns_empty_when_relay_not_configured(self, monkeypatch, tmp_path):
        """identityは解決できてもrelay未構成（token未設定）なら空文字を返す"""
        monkeypatch.setattr(relay_config, "get_state_dir", lambda: tmp_path)
        monkeypatch.delenv("RELAY_BEARER_TOKEN", raising=False)
        monkeypatch.setattr(relay_identity, "get_relay_identity", lambda: "stable-id-1")
        assert _build_relay_inbox_section(None) == ""

    def test_does_not_resolve_identity_when_relay_not_configured(self, monkeypatch, tmp_path):
        """relay未構成（token未設定）ならget_relay_identity/
        resolve_identity_by_ancestry（祖先pidチェーン解決、ps最大5回spawn）を
        一切呼ばない（tokenチェックがidentity解決より先に実行されるゼロコスト経路）。
        """
        monkeypatch.setattr(relay_config, "get_state_dir", lambda: tmp_path)
        monkeypatch.delenv("RELAY_BEARER_TOKEN", raising=False)

        def boom_get_relay_identity():
            raise AssertionError("get_relay_identity should not be called when token unset")

        def boom_resolve_identity_by_ancestry():
            raise AssertionError(
                "resolve_identity_by_ancestry (ps spawn) should not be called "
                "when token unset"
            )

        monkeypatch.setattr(relay_identity, "get_relay_identity", boom_get_relay_identity)
        monkeypatch.setattr(
            relay_identity, "resolve_identity_by_ancestry", boom_resolve_identity_by_ancestry
        )
        assert _build_relay_inbox_section(None) == ""

    def test_returns_empty_when_inbox_file_not_created(self, monkeypatch, relay_configured):
        """identity解決・relay構成済みでも、このidentity宛のinbox fileが
        一度も作られていなければ空文字を返す（touchしない・zero-cost維持）"""
        monkeypatch.setattr(relay_identity, "get_relay_identity", lambda: "never-messaged")
        assert _build_relay_inbox_section(None) == ""
        assert not relay_inbox.inbox_path("never-messaged").exists()


class TestIdentityResolved:
    def test_shows_zero_count_with_monitor_instruction(self, monkeypatch, relay_configured):
        """未読0件でも、inbox fileが存在すればMonitor監視の指示を表示する
        （バックログではなく今後の着信を待ち受けるための指示のため）"""
        monkeypatch.setattr(relay_identity, "get_relay_identity", lambda: "stable-id-1")
        _make_inbox_file(relay_configured, "stable-id-1")
        monkeypatch.setattr(relay_inbox, "count_unread", lambda session_id: 0)
        result = _build_relay_inbox_section(None)
        assert "relay inbox 未読: 0件" in result
        assert "Monitorツール" in result
        assert "relay_receive" in result

    def test_shows_count_when_unread_is_positive(self, monkeypatch, relay_configured):
        monkeypatch.setattr(relay_identity, "get_relay_identity", lambda: "stable-id-1")
        _make_inbox_file(relay_configured, "stable-id-1")
        monkeypatch.setattr(relay_inbox, "count_unread", lambda session_id: 3)
        result = _build_relay_inbox_section(None)
        assert "relay inbox 未読: 3件" in result
        assert "Monitorツール" in result

    def test_passes_resolved_identity_to_count_unread(self, monkeypatch, relay_configured):
        """count_unreadに渡される引数がget_relay_identity()の返り値と一致する"""
        received = {}
        monkeypatch.setattr(relay_identity, "get_relay_identity", lambda: "stable-id-42")
        _make_inbox_file(relay_configured, "stable-id-42")

        def fake_count_unread(session_id):
            received["session_id"] = session_id
            return 1

        monkeypatch.setattr(relay_inbox, "count_unread", fake_count_unread)
        _build_relay_inbox_section(None)
        assert received["session_id"] == "stable-id-42"

    def test_instruction_contains_absolute_inbox_path(self, monkeypatch, relay_configured):
        """指示行にはそのidentity固有のinbox file絶対パスが含まれる"""
        monkeypatch.setattr(relay_identity, "get_relay_identity", lambda: "stable-id-1")
        path = _make_inbox_file(relay_configured, "stable-id-1")
        monkeypatch.setattr(relay_inbox, "count_unread", lambda session_id: 1)
        result = _build_relay_inbox_section(None)
        assert str(path) in result


class TestSessionContextProtection:
    def test_relay_section_exception_does_not_break_other_sections(
        self, temp_db, monkeypatch, relay_configured
    ):
        """本セクションが例外を投げても、buildersループの他セクション（静的
        セクション含む）の出力は失われない（per-builder try/exceptの保護範囲）。

        tokenチェックがidentity解決より先に実行されるため、relay_configured
        （token設定済み）でなければget_relay_identity()自体が呼ばれず本テストの
        意図（例外発生時の保護）を検証できない。
        """

        def boom():
            raise RuntimeError("boom")

        monkeypatch.setattr(relay_identity, "get_relay_identity", boom)
        context = _build_session_context()
        # SessionStartからは撤去された（check_in初回呼び出し時埋め込みに変更）ため
        # 「# コンテキスト取得フロー」ではなく、デフォルトseed済みhabitsを持つ
        # 「# 振る舞い」セクションで他セクションの生存を確認する
        assert "# 振る舞い" in context
