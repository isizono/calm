"""session_managerモジュールのユニットテスト"""
import threading
import time

from src.infra.session_manager import (
    DEFAULT_GRACE_PERIOD_SEC,
    DEFAULT_LIVENESS_TIMEOUT_SEC,
    GRACE_PERIOD_ENV,
    LIVENESS_TIMEOUT_ENV,
    SessionManager,
    _read_grace_period_sec,
    _read_liveness_timeout_sec,
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
    """env CALM_AUTO_SHUTDOWN_SEC を読み取るヘルパーの単体テスト"""

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


class TestReadLivenessTimeoutSec:
    """env CALM_SESSION_LIVENESS_TIMEOUT_SEC を読み取るヘルパーの単体テスト"""

    def test_env_unset_returns_default(self, monkeypatch):
        """env未設定時はデフォルト値を返す"""
        monkeypatch.delenv(LIVENESS_TIMEOUT_ENV, raising=False)
        assert _read_liveness_timeout_sec() == DEFAULT_LIVENESS_TIMEOUT_SEC

    def test_env_numeric_returns_value(self, monkeypatch):
        """env数値指定時はその値を返す"""
        monkeypatch.setenv(LIVENESS_TIMEOUT_ENV, "120")
        assert _read_liveness_timeout_sec() == 120.0

    def test_env_zero_returns_zero(self, monkeypatch):
        """env=0 は0をそのまま返す（reaper無効化マーカー）"""
        monkeypatch.setenv(LIVENESS_TIMEOUT_ENV, "0")
        assert _read_liveness_timeout_sec() == 0.0

    def test_env_invalid_returns_default(self, monkeypatch, capsys):
        """env無効値時はデフォルトにフォールバック + stderr警告"""
        monkeypatch.setenv(LIVENESS_TIMEOUT_ENV, "abc")
        assert _read_liveness_timeout_sec() == DEFAULT_LIVENESS_TIMEOUT_SEC
        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert "Invalid" in captured.err

    def test_env_negative_returns_default(self, monkeypatch, capsys):
        """env負値時はデフォルトにフォールバック + stderr警告"""
        monkeypatch.setenv(LIVENESS_TIMEOUT_ENV, "-1")
        assert _read_liveness_timeout_sec() == DEFAULT_LIVENESS_TIMEOUT_SEC
        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert ">= 0" in captured.err


class TestRegisterHeartbeat:
    """register() の heartbeat 再送（is_new/last_seen 更新）に関する契約"""

    def test_second_register_returns_false_but_updates_last_seen(self):
        """1回目はTrue（新規）・2回目はFalse（heartbeat）を返し、両方last_seenが更新される"""
        mgr = SessionManager(liveness_timeout_sec=0)
        assert mgr.register("s1") is True
        first_seen = mgr._last_seen["s1"]
        time.sleep(0.05)
        assert mgr.register("s1") is False
        second_seen = mgr._last_seen["s1"]
        assert second_seen > first_seen


class TestLivenessReaper:
    """liveness TTL 失効（heartbeat 途絶セッションの自動 unregister）の検証

    reaperのスキャン間隔（LIVENESS_SWEEP_INTERVAL_SEC、既定30秒）はテストを
    高速化するため小さい値に差し替える。ワーカーはこの定数をループのたびに
    モジュールグローバルとして参照するため、monkeypatchが次回スキャンから
    反映される。
    """

    def test_stale_session_evicted_after_timeout(self, monkeypatch):
        """TTLを超えてregister()（heartbeat）が呼ばれないsessionは失効する"""
        from src.infra import session_manager as sm_module

        monkeypatch.setattr(sm_module, "LIVENESS_SWEEP_INTERVAL_SEC", 0.05)
        mgr = SessionManager(grace_period_sec=0, liveness_timeout_sec=0.2)
        mgr.register("s1")
        mgr.start_watchdog()
        try:
            deadline = time.monotonic() + 5
            while "s1" in mgr.session_ids and time.monotonic() < deadline:
                time.sleep(0.05)
            assert "s1" not in mgr.session_ids
        finally:
            mgr._liveness_stop_event.set()

    def test_heartbeat_within_ttl_prevents_eviction(self, monkeypatch):
        """TTL内にregister()（heartbeat）が再度呼ばれたsessionは失効しない"""
        from src.infra import session_manager as sm_module

        monkeypatch.setattr(sm_module, "LIVENESS_SWEEP_INTERVAL_SEC", 0.05)
        mgr = SessionManager(grace_period_sec=0, liveness_timeout_sec=0.3)
        mgr.register("s1")
        mgr.start_watchdog()
        try:
            # TTLの半分程度でheartbeatを送り続け、失効しないことを確認する
            for _ in range(4):
                time.sleep(0.15)
                mgr.register("s1")
            assert "s1" in mgr.session_ids
        finally:
            mgr._liveness_stop_event.set()

    def test_liveness_timeout_zero_disables_reaper_thread(self):
        """liveness_timeout_sec=0 の場合、reaperスレッドを起動しない"""
        mgr = SessionManager(liveness_timeout_sec=0)
        mgr._start_liveness_reaper()
        assert mgr._liveness_thread is None

    def test_evict_if_still_stale_toctou_race(self):
        """staleスナップショット取得後、除去直前にheartbeatが届いた場合は

        失効させない（TOCTOUレース対策）。reaperがスキャン時にstale判定
        した後・実際に除去する前にregister()（heartbeat）が割り込むケースを
        直接シミュレートする。
        """
        mgr = SessionManager(grace_period_sec=0, liveness_timeout_sec=0.2)
        mgr.register("s1")
        # reaperのスキャン時点でstaleだった状態を模す
        with mgr._lock:
            mgr._last_seen["s1"] = time.monotonic() - 10
        # スナップショット取得後、除去呼び出し前にheartbeatが届いたケース
        mgr.register("s1")
        mgr._evict_if_still_stale("s1")
        assert "s1" in mgr.session_ids

    def test_evict_if_still_stale_removes_when_genuinely_stale(self):
        """再チェック時点でもTTL超過していれば失効させる"""
        mgr = SessionManager(grace_period_sec=0, liveness_timeout_sec=0.2)
        mgr.register("s1")
        with mgr._lock:
            mgr._last_seen["s1"] = time.monotonic() - 10
        mgr._evict_if_still_stale("s1")
        assert "s1" not in mgr.session_ids

    def test_evict_if_still_stale_missing_session_is_noop(self):
        """既に unregister 済みの session_id を渡してもエラーにならない"""
        mgr = SessionManager(grace_period_sec=0, liveness_timeout_sec=0.2)
        mgr._evict_if_still_stale("not-registered")  # raiseしないことの確認


class TestOnSessionRemovedHook:
    """on_session_removed コールバックが除去経路ごとに正しく発火するかの検証"""

    def test_unregister_fires_hook(self):
        """unregister() 経由の除去でコールバックが呼ばれる"""
        removed: list[str] = []
        mgr = SessionManager(
            grace_period_sec=0, liveness_timeout_sec=0, on_session_removed=removed.append
        )
        mgr.register("s1")
        mgr.unregister("s1")
        assert removed == ["s1"]

    def test_unregister_of_unknown_session_does_not_fire_hook(self):
        """未登録 session の unregister() 失敗時はコールバックを呼ばない"""
        removed: list[str] = []
        mgr = SessionManager(
            grace_period_sec=0, liveness_timeout_sec=0, on_session_removed=removed.append
        )
        assert mgr.unregister("not-registered") is False
        assert removed == []

    def test_liveness_ttl_eviction_fires_hook(self):
        """liveness TTL 失効（_evict_if_still_stale 経由）でもコールバックが呼ばれる

        kill -9 等の異常終了は unregister() を経由しないため、この経路が
        フックされていないと撤去が一切走らない。
        """
        removed: list[str] = []
        mgr = SessionManager(
            grace_period_sec=0, liveness_timeout_sec=0.2, on_session_removed=removed.append
        )
        mgr.register("s1")
        with mgr._lock:
            mgr._last_seen["s1"] = time.monotonic() - 10
        mgr._evict_if_still_stale("s1")
        assert removed == ["s1"]

    def test_evict_toctou_race_does_not_fire_hook(self):
        """TOCTOU 再チェックで生存判定に戻った場合はコールバックを呼ばない"""
        removed: list[str] = []
        mgr = SessionManager(
            grace_period_sec=0, liveness_timeout_sec=0.2, on_session_removed=removed.append
        )
        mgr.register("s1")
        with mgr._lock:
            mgr._last_seen["s1"] = time.monotonic() - 10
        mgr.register("s1")  # heartbeat が割り込み、再チェック時点では生存
        mgr._evict_if_still_stale("s1")
        assert removed == []

    def test_hook_exception_does_not_propagate(self):
        """コールバックが例外を投げても unregister() 自体は正常に完了する"""
        def boom(session_id: str) -> None:
            raise RuntimeError("boom")

        mgr = SessionManager(grace_period_sec=0, liveness_timeout_sec=0, on_session_removed=boom)
        mgr.register("s1")
        assert mgr.unregister("s1") is True
        assert mgr.active_count == 0

    def test_no_hook_configured_is_noop(self):
        """on_session_removed 未指定でも unregister() は従来通り動く"""
        mgr = SessionManager(grace_period_sec=0, liveness_timeout_sec=0)
        mgr.register("s1")
        assert mgr.unregister("s1") is True
