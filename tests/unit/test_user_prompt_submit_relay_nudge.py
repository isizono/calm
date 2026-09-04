"""_build_relay_turn_nudge / _resolve_relay_identity_cached
（hooks/user_prompt_submit_hook.py）のユニットテスト。

subprocess経由のE2E（tests/e2e/test_user_prompt_submit_hook.py）は毎回新規
プロセスであり、「2回目の呼び出しではidentity解決を再実行しない」という
キャッシュの契約をプロセス跨ぎで検証するのが難しい。ここでは関数を直接
importしてin-processで呼び出し回数を数える。
"""
import pytest

import src.config as ccm_config
import src.services.relay.config as relay_config
import src.services.relay.identity as relay_identity
import src.services.relay.inbox as relay_inbox
from hooks.hook_state import HookState
from hooks.user_prompt_submit_hook import _build_relay_turn_nudge
from src.harness import ClaudeCodeHarness, CodexHarness


@pytest.fixture(autouse=True)
def relay_session_aware_on(monkeypatch):
    monkeypatch.setattr(ccm_config, "RELAY_SESSION_AWARE_ENABLED", True)


@pytest.fixture
def relay_configured(monkeypatch, tmp_path):
    """relay_config.get_token()がtruthyを返す状態にする。"""
    monkeypatch.setattr(relay_config, "get_state_dir", lambda: tmp_path)
    monkeypatch.setenv("RELAY_BEARER_TOKEN", "dummy-token")
    yield tmp_path


@pytest.fixture
def hook_state(tmp_path, monkeypatch):
    monkeypatch.setattr(HookState, "BASE_DIR", tmp_path / "hook-state")
    return HookState("test-session-relay-nudge")


class TestEnvVarGate:
    def test_returns_none_when_disabled(self, monkeypatch, relay_configured, hook_state):
        monkeypatch.setattr(ccm_config, "RELAY_SESSION_AWARE_ENABLED", False)
        monkeypatch.setattr(relay_identity, "get_relay_identity", lambda: "stable-id-1")
        assert _build_relay_turn_nudge(hook_state, ClaudeCodeHarness()) is None


class TestPersistentGuidance:
    """指摘1: Monitor未起動時の起動指示は `persistent: true` の使用を明記する。

    `persistent: false`（既定）だとMonitorはtimeout_ms既定値（5分）で自動
    終了するが、monitor_startedは一度立つと消えないため、監視が切れたことに
    誰も気づけなくなる（本PRが解決しようとしている「起動指示が読み流される」
    問題を、「監視がサイレントに止まる」形で再発させてしまう）。
    """

    def test_startup_instruction_mentions_persistent_true(
        self, monkeypatch, relay_configured, hook_state
    ):
        monkeypatch.setattr(relay_identity, "get_relay_identity", lambda: "stable-id-1")
        result = _build_relay_turn_nudge(hook_state, ClaudeCodeHarness())
        assert result is not None
        assert "persistent: true" in result

    def test_startup_instruction_explains_default_timeout_risk(
        self, monkeypatch, relay_configured, hook_state
    ):
        monkeypatch.setattr(relay_identity, "get_relay_identity", lambda: "stable-id-1")
        result = _build_relay_turn_nudge(hook_state, ClaudeCodeHarness())
        assert "persistent: false" in result
        assert "5分" in result


class TestIdentityCaching:
    """指摘2: identity解決結果をHookStateにキャッシュし、毎ターンのps spawnを避ける。"""

    def test_second_call_does_not_resolve_identity_again(
        self, monkeypatch, relay_configured, hook_state
    ):
        """1回目の呼び出しでidentityが解決されキャッシュされたら、2回目の
        呼び出しではget_relay_identity/resolve_identity_by_ancestryのどちらも
        追加で呼ばれない（ps spawn回避）"""
        call_count = {"get": 0, "ancestry": 0}

        def fake_get_relay_identity():
            call_count["get"] += 1
            return None

        def fake_resolve_identity_by_ancestry():
            call_count["ancestry"] += 1
            return "ancestry-id-1"

        monkeypatch.setattr(relay_identity, "get_relay_identity", fake_get_relay_identity)
        monkeypatch.setattr(
            relay_identity, "resolve_identity_by_ancestry", fake_resolve_identity_by_ancestry
        )
        monkeypatch.setattr(relay_inbox, "count_unread", lambda session_id: 0)

        _build_relay_turn_nudge(hook_state, ClaudeCodeHarness())
        assert call_count == {"get": 1, "ancestry": 1}

        hook_state.set_monitor_started()  # 2回目は起動済み扱いにして早期returnを防ぐ
        _build_relay_turn_nudge(hook_state, ClaudeCodeHarness())
        # 2回目はHookStateキャッシュがヒットし、どちらの解決関数も呼ばれない
        assert call_count == {"get": 1, "ancestry": 1}

    def test_resolved_identity_is_persisted_to_hook_state(
        self, monkeypatch, relay_configured, hook_state
    ):
        monkeypatch.setattr(relay_identity, "get_relay_identity", lambda: None)
        monkeypatch.setattr(
            relay_identity, "resolve_identity_by_ancestry", lambda: "ancestry-id-1"
        )
        _build_relay_turn_nudge(hook_state, ClaudeCodeHarness())
        assert hook_state.get_cached_relay_identity() == "ancestry-id-1"

    def test_failed_resolution_is_not_cached(self, monkeypatch, relay_configured, hook_state):
        """identity解決に失敗した(None)場合はキャッシュしない。launcher登録が
        後から間に合うタイミング差を想定し、次回呼び出しで再試行できるようにする"""
        monkeypatch.setattr(relay_identity, "get_relay_identity", lambda: None)
        monkeypatch.setattr(relay_identity, "resolve_identity_by_ancestry", lambda: None)

        assert _build_relay_turn_nudge(hook_state, ClaudeCodeHarness()) is None
        assert hook_state.get_cached_relay_identity() is None

    def test_uses_cached_identity_even_if_resolvers_would_now_fail(
        self, monkeypatch, relay_configured, hook_state
    ):
        """一度キャッシュされたidentityは、以降resolve関数がNoneを返すように
        なっても（launcher登録ファイルが後から消えた等）使われ続ける"""
        hook_state.set_cached_relay_identity("pre-cached-id")

        def boom_get_relay_identity():
            raise AssertionError("get_relay_identity should not be called when cache hits")

        def boom_resolve_identity_by_ancestry():
            raise AssertionError(
                "resolve_identity_by_ancestry should not be called when cache hits"
            )

        monkeypatch.setattr(relay_identity, "get_relay_identity", boom_get_relay_identity)
        monkeypatch.setattr(
            relay_identity, "resolve_identity_by_ancestry", boom_resolve_identity_by_ancestry
        )
        monkeypatch.setattr(relay_inbox, "count_unread", lambda session_id: 2)

        result = _build_relay_turn_nudge(hook_state, ClaudeCodeHarness())
        assert result is not None
        assert "relay inbox 未読: 2件" in result


class TestEnsureInboxFileCalled:
    """指摘3: SessionStart側のensure_inbox_fileが例外等でスキップされていた
    場合のフォールバックとして、この経路でもinbox fileを先行生成する。"""

    def test_calls_ensure_inbox_file_when_monitor_not_started(
        self, monkeypatch, relay_configured, hook_state
    ):
        monkeypatch.setattr(relay_identity, "get_relay_identity", lambda: "stable-id-1")
        result = _build_relay_turn_nudge(hook_state, ClaudeCodeHarness())
        assert result is not None
        assert relay_inbox.inbox_path("stable-id-1").exists()

    def test_does_not_touch_inbox_file_when_monitor_already_started(
        self, monkeypatch, relay_configured, hook_state
    ):
        """起動済み・未読ありの経路では起動指示自体を出さないため、
        ensure_inbox_fileは呼ばない（Monitorタッチの根拠がない）"""
        monkeypatch.setattr(relay_identity, "get_relay_identity", lambda: "stable-id-2")
        monkeypatch.setattr(relay_inbox, "count_unread", lambda session_id: 1)
        hook_state.set_monitor_started()

        result = _build_relay_turn_nudge(hook_state, ClaudeCodeHarness())
        assert result is not None
        assert "relay inbox 未読: 1件" in result
        assert not relay_inbox.inbox_path("stable-id-2").exists()


class TestMonitorUnsupportedHarness:
    """Monitorツールが無いハーネス（Codex）では起動指示を注入しない（#616）。

    存在しないツールの起動指示は実行不能なノイズとして毎ターン注入され
    続けるため、supports_monitor_watch=Falseのハーネスでは消化指示のみ返す。
    """

    def test_returns_none_when_no_unread(self, monkeypatch, relay_configured, hook_state):
        """Monitor未起動でも、起動指示を出せない以上未読0件なら注入なし"""
        monkeypatch.setattr(relay_identity, "get_relay_identity", lambda: "codex-id-1")
        monkeypatch.setattr(relay_inbox, "count_unread", lambda session_id: 0)
        assert _build_relay_turn_nudge(hook_state, CodexHarness()) is None

    def test_unread_nudge_without_startup_instruction(
        self, monkeypatch, relay_configured, hook_state
    ):
        """未読>0のときは消化指示のみ。Monitor起動指示は含まない"""
        monkeypatch.setattr(relay_identity, "get_relay_identity", lambda: "codex-id-1")
        monkeypatch.setattr(relay_inbox, "count_unread", lambda session_id: 2)
        result = _build_relay_turn_nudge(hook_state, CodexHarness())
        assert result is not None
        assert "relay inbox 未読: 2件" in result
        assert "relay_receive" in result
        assert "Monitor" not in result
