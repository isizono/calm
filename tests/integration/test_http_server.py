"""HTTPサーバーモードの統合テスト

セッションエンドポイント・ウォッチドッグ・ロックファイルの結合動作を検証する。
"""
import json
import os
import tempfile
import threading
import time

import pytest

from src.infra import lock_file
from src.infra.session_manager import SessionManager


@pytest.fixture(autouse=True)
def isolate_lock_file(tmp_path, monkeypatch):
    """ロックファイルのパスを一時ディレクトリに差し替える"""
    lock_dir = tmp_path / ".cc-memory"
    lock_dir.mkdir()
    monkeypatch.setattr(lock_file, "LOCK_DIR", lock_dir)
    monkeypatch.setattr(lock_file, "LOCK_FILE", lock_dir / "server.lock")


class TestSessionLifecycle:
    """セッションライフサイクル全体の統合テスト"""

    def test_full_lifecycle_acquire_register_unregister_release(self):
        """ロック取得 → セッション登録 → セッション解除 → ロック解放"""
        # ロック取得
        assert lock_file.acquire(52837) is True
        info = lock_file.read()
        assert info is not None
        assert info["port"] == 52837

        # セッション管理
        mgr = SessionManager(grace_period_sec=60)
        mgr.register("session-1")
        assert mgr.active_count == 1

        mgr.register("session-2")
        assert mgr.active_count == 2

        mgr.unregister("session-1")
        assert mgr.active_count == 1

        mgr.unregister("session-2")
        assert mgr.active_count == 0

        # ロック解放
        lock_file.release()
        assert lock_file.read() is None

    def test_watchdog_triggers_shutdown_after_all_sessions_leave(self):
        """全セッション離脱後、ウォッチドッグが猶予期間経過後にshutdownを呼ぶ"""
        shutdown_called = threading.Event()
        mgr = SessionManager(grace_period_sec=1)
        mgr.set_shutdown_callback(shutdown_called.set)

        # セッション登録→解除
        mgr.register("s1")
        mgr.unregister("s1")

        # 猶予期間後にshutdown
        assert shutdown_called.wait(timeout=3) is True

    def test_watchdog_does_not_trigger_while_sessions_active(self):
        """セッションが残っている間はshutdownが呼ばれない"""
        shutdown_called = threading.Event()
        mgr = SessionManager(grace_period_sec=10)
        mgr.set_shutdown_callback(shutdown_called.set)
        mgr.start_watchdog()

        # 1秒後にセッション登録（grace_period=10sなので十分余裕がある）
        time.sleep(1)
        mgr.register("s1")

        # キャンセル後、少し待ってもshutdownは呼ばれない
        assert shutdown_called.wait(timeout=3) is False

    def test_lock_prevents_double_start(self, monkeypatch):
        """ロック取得済みの場合、2つ目のacquireは失敗する"""
        monkeypatch.setattr(lock_file, "is_port_listening", lambda port, **kw: True)
        assert lock_file.acquire(52837) is True
        assert lock_file.acquire(52837) is False

    def test_stale_lock_recovery_and_new_session(self, monkeypatch):
        """staleロック回収後に新しいセッションが動作する"""
        monkeypatch.setattr(lock_file, "is_process_alive", lambda pid: False)
        lock_file.LOCK_FILE.write_text(
            json.dumps({"pid": 99999999, "port": 52837}), encoding="utf-8"
        )

        # staleロック回収
        assert lock_file.acquire(52837) is True
        info = lock_file.read()
        assert info["pid"] == os.getpid()

        # セッション管理が正常動作
        mgr = SessionManager()
        mgr.register("s1")
        assert mgr.active_count == 1


class TestInitialGracePeriod:
    """サーバー起動直後の猶予期間テスト"""

    def test_startup_grace_period_with_no_sessions(self):
        """起動直後にセッション登録がない場合、猶予期間後にshutdown"""
        shutdown_called = threading.Event()
        mgr = SessionManager(grace_period_sec=1)
        mgr.set_shutdown_callback(shutdown_called.set)
        mgr.start_watchdog()

        assert shutdown_called.wait(timeout=3) is True

    def test_startup_grace_cancelled_by_first_session(self):
        """起動直後の猶予期間中に最初のセッションが来るとキャンセル"""
        shutdown_called = threading.Event()
        mgr = SessionManager(grace_period_sec=10)
        mgr.set_shutdown_callback(shutdown_called.set)
        mgr.start_watchdog()

        time.sleep(1)
        mgr.register("s1")

        assert shutdown_called.wait(timeout=3) is False
