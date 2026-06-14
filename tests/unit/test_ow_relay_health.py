"""relay自己修復gateのユニットテスト

カバー範囲:
- `_get_relay_health()`: 200/dict, 404, 接続断, 不正JSON
- `_kill_relay(pid)`: SIGTERM→exit, SIGKILL fallback, PID不在
- `ensure_relay_server()`: 初回起動・版一致・版不一致→kill+restart・restart失敗
- `_open_relay_lock()`/`_close_relay_lock()`: flock取得・解放のhappy path

実HTTPサーバーは立てず、urllib.request.urlopenとos.killをmockで差し替えて分岐挙動を検証する。
実サーバー起動を伴う統合テストはintegration/test_relay_health.py側で扱う。
"""
import io
import json
import os
import signal
import urllib.error
import urllib.request
from unittest.mock import MagicMock

import pytest

from src.relay import PROTOCOL_VERSION
from src.services import ow_service


def _make_http_error(code: int, message: str = "error") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://127.0.0.1:8765/health",
        code=code,
        msg=message,
        hdrs={},
        fp=None,
    )


def _fake_response(payload: dict, status: int = 200):
    """urllib.request.urlopen()のcontext managerモック相当を返す。"""
    body = json.dumps(payload).encode("utf-8")

    class _Resp:
        def __init__(self):
            self.status = status
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    return _Resp()


class TestGetRelayHealth:
    """`_get_relay_health()` の挙動: protocol_versionを含むdictを返すか、Noneにフォールバックするか。"""

    def test_returns_dict_on_200(self, monkeypatch):
        """200 OK + JSONボディ → そのままdictで返す"""
        payload = {
            "status": "ok",
            "protocol_version": PROTOCOL_VERSION,
            "pid": 12345,
            "started_at": "2026-06-14T05:00:00+00:00",
            "uptime_sec": 10,
        }
        monkeypatch.setattr(
            urllib.request, "urlopen",
            lambda req, timeout=None: _fake_response(payload, status=200),
        )
        result = ow_service._get_relay_health()
        assert result == payload

    def test_returns_none_on_404(self, monkeypatch):
        """404（旧版で/health非対応） → None。版不一致扱いでrestartへ"""
        def fake(req, timeout=None):
            raise _make_http_error(404, "not found")
        monkeypatch.setattr(urllib.request, "urlopen", fake)
        assert ow_service._get_relay_health() is None

    def test_returns_none_on_connection_refused(self, monkeypatch):
        """relay未起動相当（URLError） → None"""
        def fake(req, timeout=None):
            raise urllib.error.URLError("Connection refused")
        monkeypatch.setattr(urllib.request, "urlopen", fake)
        assert ow_service._get_relay_health() is None

    def test_returns_none_on_invalid_json(self, monkeypatch):
        """200だがJSONとしてパースできないボディ → None"""
        class _BadResp:
            status = 200
            def read(self):
                return b"not-json"
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _BadResp())
        assert ow_service._get_relay_health() is None

    def test_returns_none_on_non_dict_payload(self, monkeypatch):
        """JSONが配列等dict以外 → None"""
        monkeypatch.setattr(
            urllib.request, "urlopen",
            lambda req, timeout=None: _fake_response(["not", "a", "dict"], status=200),
        )
        assert ow_service._get_relay_health() is None


class TestKillRelay:
    """`_kill_relay(pid)` の挙動: SIGTERM→生存確認→SIGKILL fallback。"""

    def test_terminates_via_sigterm(self, monkeypatch):
        """SIGTERMで素直に終了 → SIGKILLは呼ばれない"""
        signals: list[int] = []
        alive = {"flag": True}

        def fake_kill(pid, sig):
            signals.append(sig)
            if sig == signal.SIGTERM:
                alive["flag"] = False
                return
            if sig == 0:
                if not alive["flag"]:
                    raise ProcessLookupError()
                return
            if sig == signal.SIGKILL:
                return

        monkeypatch.setattr(os, "kill", fake_kill)
        monkeypatch.setattr(ow_service.time, "sleep", lambda s: None)

        ow_service._kill_relay(99999)
        assert signal.SIGTERM in signals
        assert signal.SIGKILL not in signals

    def test_falls_back_to_sigkill(self, monkeypatch):
        """SIGTERMで死なないプロセス → SIGKILLにフォールバック"""
        signals: list[int] = []

        def fake_kill(pid, sig):
            signals.append(sig)
            return  # 一度も死なない

        monkeypatch.setattr(os, "kill", fake_kill)
        monkeypatch.setattr(ow_service.time, "sleep", lambda s: None)

        ow_service._kill_relay(99999)
        assert signals[0] == signal.SIGTERM
        assert signals[-1] == signal.SIGKILL

    def test_missing_pid_is_noop(self, monkeypatch):
        """既に死んでいるpid（ProcessLookupError） → 静かに戻る"""
        def fake_kill(pid, sig):
            raise ProcessLookupError()

        monkeypatch.setattr(os, "kill", fake_kill)
        monkeypatch.setattr(ow_service.time, "sleep", lambda s: None)
        ow_service._kill_relay(99999)  # raiseしないことが期待

    def test_zero_pid_is_noop(self, monkeypatch):
        """pid=0/None相当 → os.killを呼ばずに戻る"""
        called = []
        monkeypatch.setattr(os, "kill", lambda p, s: called.append((p, s)))
        ow_service._kill_relay(0)
        assert called == []


class TestEnsureRelayServer:
    """`ensure_relay_server()` の自己修復フロー全分岐。"""

    @pytest.fixture(autouse=True)
    def _isolate_lock(self, monkeypatch, tmp_path):
        """テスト間でflockファイルが衝突しないようロックパスをtmp_pathへ差し替える。"""
        lock_path = tmp_path / "relay.lock"
        monkeypatch.setattr(ow_service, "_RELAY_STATE_DIR", tmp_path)
        monkeypatch.setattr(ow_service, "_RELAY_LOCK_PATH", lock_path)
        yield

    def test_returns_true_when_already_healthy_matching_version(self, monkeypatch):
        """既存relayが版一致でhealthy → 起動せずTrue"""
        start_called = []
        monkeypatch.setattr(
            ow_service, "_get_relay_health",
            lambda: {"protocol_version": PROTOCOL_VERSION, "pid": 1234},
        )
        monkeypatch.setattr(ow_service, "_start_relay_server", lambda: start_called.append(True) or True)
        monkeypatch.setattr(ow_service, "_kill_relay", lambda pid: None)
        assert ow_service.ensure_relay_server() is True
        assert start_called == []

    def test_starts_when_not_running(self, monkeypatch):
        """relay未起動（health=None） → 起動して/health確認後にTrue"""
        # 1回目None（未起動）→ start→ 待機中に2回目で揃う
        health_calls = iter([None, {"protocol_version": PROTOCOL_VERSION, "pid": 5678}])
        monkeypatch.setattr(ow_service, "_get_relay_health", lambda: next(health_calls))
        monkeypatch.setattr(ow_service, "_wait_for_relay_health",
                            lambda timeout_sec=10.0, interval_sec=0.5: {"protocol_version": PROTOCOL_VERSION, "pid": 5678})

        started = []
        monkeypatch.setattr(ow_service, "_start_relay_server", lambda: started.append(True) or True)
        monkeypatch.setattr(ow_service, "_kill_relay", lambda pid: None)
        assert ow_service.ensure_relay_server() is True
        assert started == [True]

    def test_kills_and_restarts_on_version_mismatch(self, monkeypatch):
        """版不一致のrelayが応答 → killして起動 → 新版で再確認"""
        stale = {"protocol_version": PROTOCOL_VERSION - 99, "pid": 7777}
        fresh = {"protocol_version": PROTOCOL_VERSION, "pid": 8888}
        monkeypatch.setattr(ow_service, "_get_relay_health", lambda: stale)
        monkeypatch.setattr(ow_service, "_wait_for_relay_health",
                            lambda timeout_sec=10.0, interval_sec=0.5: fresh)

        killed: list[int] = []
        monkeypatch.setattr(ow_service, "_kill_relay", lambda pid: killed.append(pid))
        started = []
        monkeypatch.setattr(ow_service, "_start_relay_server", lambda: started.append(True) or True)

        assert ow_service.ensure_relay_server() is True
        assert killed == [7777]
        assert started == [True]

    def test_returns_false_when_start_fails(self, monkeypatch):
        """relay未起動 + 起動失敗 → False"""
        monkeypatch.setattr(ow_service, "_get_relay_health", lambda: None)
        monkeypatch.setattr(ow_service, "_start_relay_server", lambda: False)
        monkeypatch.setattr(ow_service, "_kill_relay", lambda pid: None)
        assert ow_service.ensure_relay_server() is False

    def test_returns_false_when_restart_does_not_converge(self, monkeypatch):
        """版不一致 → killしてstartしたが/healthが揃わない → False"""
        stale = {"protocol_version": PROTOCOL_VERSION - 1, "pid": 7777}
        monkeypatch.setattr(ow_service, "_get_relay_health", lambda: stale)
        monkeypatch.setattr(ow_service, "_wait_for_relay_health",
                            lambda timeout_sec=10.0, interval_sec=0.5: None)
        monkeypatch.setattr(ow_service, "_kill_relay", lambda pid: None)
        monkeypatch.setattr(ow_service, "_start_relay_server", lambda: True)
        assert ow_service.ensure_relay_server() is False

    def test_returns_false_when_restart_still_mismatched(self, monkeypatch):
        """版不一致 → restart後も版不一致 → False（無限restart防止）"""
        stale = {"protocol_version": PROTOCOL_VERSION - 1, "pid": 7777}
        still_stale = {"protocol_version": PROTOCOL_VERSION - 1, "pid": 8888}
        monkeypatch.setattr(ow_service, "_get_relay_health", lambda: stale)
        monkeypatch.setattr(ow_service, "_wait_for_relay_health",
                            lambda timeout_sec=10.0, interval_sec=0.5: still_stale)
        monkeypatch.setattr(ow_service, "_kill_relay", lambda pid: None)
        monkeypatch.setattr(ow_service, "_start_relay_server", lambda: True)
        assert ow_service.ensure_relay_server() is False


class TestRelayLock:
    """flockの取得・解放が例外なく回ること（同一プロセス内の動作確認）。"""

    def test_acquire_and_release(self, tmp_path, monkeypatch):
        """1回open→closeで例外が出ないこと"""
        monkeypatch.setattr(ow_service, "_RELAY_STATE_DIR", tmp_path)
        monkeypatch.setattr(ow_service, "_RELAY_LOCK_PATH", tmp_path / "relay.lock")
        fd = ow_service._open_relay_lock()
        try:
            assert fd >= 0
            assert (tmp_path / "relay.lock").exists()
        finally:
            ow_service._close_relay_lock(fd)
