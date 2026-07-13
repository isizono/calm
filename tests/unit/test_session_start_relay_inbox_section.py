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

import src.config as ccm_config
import src.services.relay.config as relay_config
import src.services.relay.identity as relay_identity
import src.services.relay.inbox as relay_inbox
from src.db import init_database
from hooks.session_start_hook import _build_relay_inbox_section, _build_session_context


@pytest.fixture(autouse=True)
def relay_session_aware_on(monkeypatch):
    """本ファイルの大半のテストはCCM_RELAY_SESSION_AWARE=1（ON）時の表示ロジックを
    検証するため、autouseでデフォルトONにする。OFF時（kill switch）の振る舞いは
    TestEnvVarGateで個別にFalseへ上書きして検証する。"""
    monkeypatch.setattr(ccm_config, "RELAY_SESSION_AWARE_ENABLED", True)


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

    def test_shows_monitor_instruction_when_inbox_file_not_created(
        self, monkeypatch, relay_configured
    ):
        """identity解決・relay構成済みなら、このidentity宛のinbox fileが
        一度も作られていなくてもMonitor監視指示を返す（新着を取りこぼさないため
        既存メッセージの有無を問わず常時発火する）"""
        monkeypatch.setattr(relay_identity, "get_relay_identity", lambda: "never-messaged")
        result = _build_relay_inbox_section(None)
        assert "Monitorツール" in result
        assert "未読" not in result

    def test_precreates_inbox_file_when_not_yet_created(self, monkeypatch, relay_configured):
        """一度もappendされていないidentityでも、指示を返す前にinbox fileを
        先行生成する（tail -f等のfile不在即死ツールを回避するため）"""
        monkeypatch.setattr(relay_identity, "get_relay_identity", lambda: "never-messaged")
        assert not relay_inbox.inbox_path("never-messaged").exists()
        _build_relay_inbox_section(None)
        assert relay_inbox.inbox_path("never-messaged").exists()

    def test_does_not_truncate_existing_inbox_file_content(self, monkeypatch, relay_configured):
        """既にメッセージが届いているinbox fileは、先行生成処理によって
        中身を消されない"""
        monkeypatch.setattr(relay_identity, "get_relay_identity", lambda: "already-messaged")
        relay_inbox.append("already-messaged", {"n": 1})
        _build_relay_inbox_section(None)
        assert relay_inbox.count_unread("already-messaged") == 1


class TestIdentityResolved:
    def test_shows_monitor_instruction_only_when_unread_is_zero(
        self, monkeypatch, relay_configured
    ):
        """未読0件でもMonitor監視指示は返す（未読N件の報告行のみ省く）"""
        monkeypatch.setattr(relay_identity, "get_relay_identity", lambda: "stable-id-1")
        _make_inbox_file(relay_configured, "stable-id-1")
        monkeypatch.setattr(relay_inbox, "count_unread", lambda session_id: 0)
        result = _build_relay_inbox_section(None)
        assert "Monitorツール" in result
        assert "未読" not in result

    def test_shows_count_when_unread_is_positive(self, monkeypatch, relay_configured):
        """未読>0のときは「未読N件 → relay_receiveで消化」+ Monitor監視指示の2行以内"""
        monkeypatch.setattr(relay_identity, "get_relay_identity", lambda: "stable-id-1")
        _make_inbox_file(relay_configured, "stable-id-1")
        monkeypatch.setattr(relay_inbox, "count_unread", lambda session_id: 3)
        result = _build_relay_inbox_section(None)
        assert "relay inbox 未読: 3件" in result
        assert "relay_receive" in result
        assert "Monitorツール" in result
        assert len([line for line in result.splitlines() if line]) <= 2

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


class TestEnvVarGate:
    """CCM_RELAY_SESSION_AWARE（kill switch）の振る舞い。

    autouse fixtureがデフォルトONにしているため、ここでは個別にFalseへ
    上書きしてOFF時の振る舞いを検証する。
    """

    def test_returns_empty_when_disabled_even_if_fully_configured(
        self, monkeypatch, relay_configured
    ):
        """OFF時は、token設定済み・identity解決可能・未読ありでも空文字を返す
        （relayを使わないセッションへの注入を止める入口ゲート）"""
        monkeypatch.setattr(ccm_config, "RELAY_SESSION_AWARE_ENABLED", False)
        monkeypatch.setattr(relay_identity, "get_relay_identity", lambda: "stable-id-1")
        _make_inbox_file(relay_configured, "stable-id-1")
        monkeypatch.setattr(relay_inbox, "count_unread", lambda session_id: 3)
        assert _build_relay_inbox_section(None) == ""

    def test_does_not_check_token_when_disabled(self, monkeypatch, tmp_path):
        """OFF時はget_token()すら呼ばない（tokenチェックより手前のゼロコスト経路）"""
        monkeypatch.setattr(ccm_config, "RELAY_SESSION_AWARE_ENABLED", False)

        def boom_get_token():
            raise AssertionError("get_token should not be called when disabled")

        monkeypatch.setattr(relay_config, "get_token", boom_get_token)
        assert _build_relay_inbox_section(None) == ""


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
