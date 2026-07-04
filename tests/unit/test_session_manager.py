"""session_managerモジュールのユニットテスト"""
import threading
import time

from src.infra.session_manager import (
    DEFAULT_GRACE_PERIOD_SEC,
    GRACE_PERIOD_ENV,
    SessionManager,
    _read_grace_period_sec,
)


class TestRegisterUnregister:
    def test_register_new_session(self):
        """新規セッション登録でTrueを返す"""
        mgr = SessionManager()
        assert mgr.register("s1") is True
        assert mgr.active_count == 1

    def test_register_duplicate_session(self):
        """同じセッションIDの再登録はFalseを返す"""
        mgr = SessionManager()
        mgr.register("s1")
        assert mgr.register("s1") is False
        assert mgr.active_count == 1

    def test_register_multiple_sessions(self):
        """複数セッションの登録"""
        mgr = SessionManager()
        mgr.register("s1")
        mgr.register("s2")
        mgr.register("s3")
        assert mgr.active_count == 3

    def test_unregister_existing_session(self):
        """登録済みセッションの解除でTrueを返す"""
        mgr = SessionManager()
        mgr.register("s1")
        assert mgr.unregister("s1") is True
        assert mgr.active_count == 0

    def test_unregister_nonexistent_session(self):
        """未登録セッションの解除はFalseを返す"""
        mgr = SessionManager()
        assert mgr.unregister("s1") is False

    def test_session_ids_returns_copy(self):
        """session_idsはコピーを返す"""
        mgr = SessionManager()
        mgr.register("s1")
        mgr.register("s2")
        ids = mgr.session_ids
        assert ids == {"s1", "s2"}
        # コピーなので元に影響しない
        ids.add("s3")
        assert mgr.active_count == 2


class TestGraceTimer:
    def test_shutdown_after_grace_period(self):
        """セッション0 → 猶予期間経過 → shutdownコールバックが呼ばれる"""
        shutdown_called = threading.Event()
        mgr = SessionManager(grace_period_sec=1)
        mgr.set_shutdown_callback(shutdown_called.set)
        mgr.start_watchdog()

        # 1秒の猶予期間 + マージン
        assert shutdown_called.wait(timeout=3) is True
        assert mgr.is_shutdown_requested is True

    def test_grace_timer_cancelled_by_register(self):
        """猶予期間中にセッション登録があるとタイマーがキャンセルされる"""
        shutdown_called = threading.Event()
        mgr = SessionManager(grace_period_sec=10)
        mgr.set_shutdown_callback(shutdown_called.set)
        mgr.start_watchdog()

        # 1秒後にセッション登録（grace_period=10sなので十分余裕がある）
        time.sleep(1)
        mgr.register("s1")

        # キャンセル後、少し待ってもshutdownは呼ばれない
        assert shutdown_called.wait(timeout=3) is False
        assert mgr.is_shutdown_requested is False

    def test_grace_timer_restarted_on_last_unregister(self):
        """最後のセッション解除で猶予タイマーが再開される"""
        shutdown_called = threading.Event()
        mgr = SessionManager(grace_period_sec=1)
        mgr.set_shutdown_callback(shutdown_called.set)

        mgr.register("s1")
        # セッション解除 → 猶予タイマー開始
        mgr.unregister("s1")

        assert shutdown_called.wait(timeout=3) is True
        assert mgr.is_shutdown_requested is True

    def test_no_shutdown_if_sessions_remain(self):
        """セッションが残っていればshutdownは呼ばれない"""
        shutdown_called = threading.Event()
        mgr = SessionManager(grace_period_sec=1)
        mgr.set_shutdown_callback(shutdown_called.set)

        mgr.register("s1")
        mgr.register("s2")
        mgr.unregister("s1")

        # 猶予期間+マージンを待ってもshutdownは呼ばれない
        assert shutdown_called.wait(timeout=3) is False
        assert mgr.is_shutdown_requested is False

    def test_register_during_grace_resets_timer(self):
        """猶予期間中にregister→unregisterすると猶予がリセットされる"""
        shutdown_called = threading.Event()
        mgr = SessionManager(grace_period_sec=3)
        mgr.set_shutdown_callback(shutdown_called.set)
        mgr.start_watchdog()

        # 1秒後にregister→即unregister（タイマーリセット）
        time.sleep(1)
        mgr.register("s1")
        mgr.unregister("s1")

        # リセット後の新しい猶予期間（3秒）の前にはshutdownされない
        time.sleep(1)
        assert mgr.is_shutdown_requested is False

        # 合計で猶予期間分待てばshutdownされる
        assert shutdown_called.wait(timeout=5) is True


class TestReadGracePeriodSec:
    """env CC_MEMORY_AUTO_SHUTDOWN_SEC を読み取るヘルパーの単体テスト"""

    def test_env_unset_returns_default(self, monkeypatch):
        """env未設定時はデフォルト値を返す"""
        monkeypatch.delenv(GRACE_PERIOD_ENV, raising=False)
        assert _read_grace_period_sec() == DEFAULT_GRACE_PERIOD_SEC

    def test_env_empty_returns_default(self, monkeypatch):
        """env空文字時はデフォルトにフォールバック"""
        monkeypatch.setenv(GRACE_PERIOD_ENV, "")
        assert _read_grace_period_sec() == DEFAULT_GRACE_PERIOD_SEC

    def test_env_numeric_returns_value(self, monkeypatch):
        """env数値指定時はその値を返す"""
        monkeypatch.setenv(GRACE_PERIOD_ENV, "120")
        assert _read_grace_period_sec() == 120

    def test_env_zero_returns_zero(self, monkeypatch):
        """env=0 は0をそのまま返す（auto-shutdown無効化マーカー）"""
        monkeypatch.setenv(GRACE_PERIOD_ENV, "0")
        assert _read_grace_period_sec() == 0

    def test_env_invalid_returns_default(self, monkeypatch, capsys):
        """env無効値時はデフォルトにフォールバック + stderr警告"""
        monkeypatch.setenv(GRACE_PERIOD_ENV, "abc")
        assert _read_grace_period_sec() == DEFAULT_GRACE_PERIOD_SEC
        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert "Invalid" in captured.err

    def test_env_negative_returns_default(self, monkeypatch, capsys):
        """env負値時はデフォルトにフォールバック + stderr警告"""
        monkeypatch.setenv(GRACE_PERIOD_ENV, "-1")
        assert _read_grace_period_sec() == DEFAULT_GRACE_PERIOD_SEC
        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert ">= 0" in captured.err


class TestEnvOverride:
    """SessionManager コンストラクタの env override 挙動"""

    def test_default_uses_env_value(self, monkeypatch):
        """grace_period_sec未指定時はenvから読む"""
        monkeypatch.setenv(GRACE_PERIOD_ENV, "120")
        mgr = SessionManager()
        assert mgr._grace_period == 120

    def test_default_unset_env_uses_default(self, monkeypatch):
        """env未設定時はデフォルト値を採用する"""
        monkeypatch.delenv(GRACE_PERIOD_ENV, raising=False)
        mgr = SessionManager()
        assert mgr._grace_period == DEFAULT_GRACE_PERIOD_SEC

    def test_explicit_arg_overrides_env(self, monkeypatch):
        """grace_period_sec明示指定時はenvより優先される"""
        monkeypatch.setenv(GRACE_PERIOD_ENV, "120")
        mgr = SessionManager(grace_period_sec=5)
        assert mgr._grace_period == 5

    def test_explicit_zero_overrides_env(self, monkeypatch):
        """grace_period_sec=0明示時もenvより優先される"""
        monkeypatch.setenv(GRACE_PERIOD_ENV, "120")
        mgr = SessionManager(grace_period_sec=0)
        assert mgr._grace_period == 0
        assert mgr.is_auto_shutdown_disabled is True


class TestAutoShutdownDisabled:
    """env=0 で auto-shutdown が完全に無効化されることの検証"""

    def test_env_zero_disables_watchdog(self, monkeypatch):
        """env=0 で start_watchdog 呼び出してもシャットダウンが発火しない"""
        monkeypatch.setenv(GRACE_PERIOD_ENV, "0")
        mgr = SessionManager()
        assert mgr.is_auto_shutdown_disabled is True

        shutdown_called = threading.Event()
        mgr.set_shutdown_callback(shutdown_called.set)
        mgr.start_watchdog()

        # シャットダウンが発火しないこと
        assert shutdown_called.wait(timeout=2) is False
        assert mgr.is_shutdown_requested is False

    def test_env_zero_disables_unregister_trigger(self, monkeypatch):
        """env=0 で最後のセッション解除でもシャットダウンが発火しない"""
        monkeypatch.setenv(GRACE_PERIOD_ENV, "0")
        mgr = SessionManager()

        shutdown_called = threading.Event()
        mgr.set_shutdown_callback(shutdown_called.set)

        mgr.register("s1")
        mgr.unregister("s1")

        # 無効化されているのでシャットダウンは呼ばれない
        assert shutdown_called.wait(timeout=2) is False
        assert mgr.is_shutdown_requested is False

    def test_register_unregister_still_work_when_disabled(self, monkeypatch):
        """env=0 でも register/unregister 自体は正常動作する"""
        monkeypatch.setenv(GRACE_PERIOD_ENV, "0")
        mgr = SessionManager()

        assert mgr.register("s1") is True
        assert mgr.register("s2") is True
        assert mgr.active_count == 2
        assert mgr.session_ids == {"s1", "s2"}
        assert mgr.unregister("s1") is True
        assert mgr.active_count == 1
