"""relay自己修復gateのユニットテスト

カバー範囲:
- `_get_relay_health()`: 200/dict, 404, 接続断, 不正JSON
- `_kill_relay(pid)`: SIGTERM→exit, SIGKILL fallback, PID不在
- `_find_port_owners(port)` / `_clear_relay_port()`: lsofパース・不在時フォールバック
- `ensure_relay_server()`: 初回起動・版一致・版不一致→kill+restart・restart失敗・/health 404+port占有→port掃除して再起動
- `_open_relay_lock()`/`_close_relay_lock()`: flock取得・解放のhappy path

実HTTPサーバーは立てず、urllib.request.urlopenとos.killをmockで差し替えて分岐挙動を検証する。
実サーバー起動を伴う統合テストはintegration/test_relay_health.py側で扱う。
"""
import io
import json
import os
import signal
import subprocess
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
        monkeypatch.setattr(ow_service, "_clear_relay_port", lambda: 0)
        assert ow_service.ensure_relay_server() is False

    def test_clears_port_owner_when_health_404(self, monkeypatch):
        """/health 404→Noneかつport占有プロセスあり → killしてから起動 → 新版がhealthyになりTrue"""
        # health=None（旧版が404を返す相当）
        monkeypatch.setattr(ow_service, "_get_relay_health", lambda: None)
        monkeypatch.setattr(
            ow_service, "_wait_for_relay_health",
            lambda timeout_sec=10.0, interval_sec=0.5: {"protocol_version": PROTOCOL_VERSION, "pid": 9999},
        )
        # ポート8765を占有する旧版PIDが1つ存在
        monkeypatch.setattr(ow_service, "_find_port_owners", lambda port: [75346])
        killed: list[int] = []
        monkeypatch.setattr(ow_service, "_kill_relay", lambda pid: killed.append(pid))
        started: list[bool] = []
        monkeypatch.setattr(ow_service, "_start_relay_server", lambda: started.append(True) or True)

        assert ow_service.ensure_relay_server() is True
        # 起動前にport占有プロセスがkillされている（旧版居座りケースの自己修復成立）
        assert killed == [75346]
        assert started == [True]

    def test_retries_after_clearing_port_when_health_does_not_converge(self, monkeypatch):
        """1回目の起動後/healthが揃わずport占有も残っている → portを掃除して再起動 → 揃ってTrue"""
        # 初回get_health=None。以後はwait_for_relay_health経由
        monkeypatch.setattr(ow_service, "_get_relay_health", lambda: None)
        # 1回目wait→None（揃わない）、2回目wait→揃う
        wait_calls = iter([None, {"protocol_version": PROTOCOL_VERSION, "pid": 1111}])
        monkeypatch.setattr(
            ow_service, "_wait_for_relay_health",
            lambda timeout_sec=10.0, interval_sec=0.5: next(wait_calls),
        )
        # _clear_relay_port: 1回目=占有なし(0)、2回目=占有を見つけてkill(1)
        clear_calls = iter([0, 1])
        cleared_count: list[int] = []
        def fake_clear():
            n = next(clear_calls)
            cleared_count.append(n)
            return n
        monkeypatch.setattr(ow_service, "_clear_relay_port", fake_clear)
        started: list[bool] = []
        monkeypatch.setattr(ow_service, "_start_relay_server", lambda: started.append(True) or True)
        monkeypatch.setattr(ow_service, "_kill_relay", lambda pid: None)

        assert ow_service.ensure_relay_server() is True
        # 2回起動（初回 + リトライ）、_clear_relay_port は2回呼ばれている
        assert started == [True, True]
        assert cleared_count == [0, 1]

    def test_retries_even_when_clear_finds_nothing(self, monkeypatch):
        """1回目wait失敗 + 2回目のclearでも0件 → それでもretryは1回必ず実行される

        clear=0でretryをスキップすると、_start_relay_server が起動途中で死んだだけのレース
        ケースが救えなくなる。flock保持中なので無条件retryでも無限ループにはならない。
        """
        monkeypatch.setattr(ow_service, "_get_relay_health", lambda: None)
        # 1回目wait→None、2回目wait→揃う（retryが走らないとTrueにならない）
        wait_calls = iter([None, {"protocol_version": PROTOCOL_VERSION, "pid": 2222}])
        monkeypatch.setattr(
            ow_service, "_wait_for_relay_health",
            lambda timeout_sec=10.0, interval_sec=0.5: next(wait_calls),
        )
        # 全 clear で 0件 = 占有なし
        cleared_count: list[int] = []
        def fake_clear():
            cleared_count.append(0)
            return 0
        monkeypatch.setattr(ow_service, "_clear_relay_port", fake_clear)
        started: list[bool] = []
        monkeypatch.setattr(ow_service, "_start_relay_server", lambda: started.append(True) or True)
        monkeypatch.setattr(ow_service, "_kill_relay", lambda pid: None)

        assert ow_service.ensure_relay_server() is True
        # 2回起動・2回clear（retryがclear件数に依らず必ず実行される）
        assert started == [True, True]
        assert cleared_count == [0, 0]

    def test_returns_false_when_retry_also_fails(self, monkeypatch):
        """1回目wait失敗 → port掃除しても2回目wait失敗 → False（無限リトライ防止）"""
        monkeypatch.setattr(ow_service, "_get_relay_health", lambda: None)
        monkeypatch.setattr(
            ow_service, "_wait_for_relay_health",
            lambda timeout_sec=10.0, interval_sec=0.5: None,
        )
        # 2回ともport占有あり(自己修復対象)だが新版が立ち上がらない
        monkeypatch.setattr(ow_service, "_clear_relay_port", lambda: 1)
        monkeypatch.setattr(ow_service, "_start_relay_server", lambda: True)
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


class TestFindPortOwners:
    """`_find_port_owners(port)` の挙動: lsof出力パース、不在・失敗時のフォールバック。"""

    def test_returns_pids_when_lsof_lists_owners(self, monkeypatch):
        """lsof stdoutに複数行のPIDがある → intリストで返す"""
        def fake_run(cmd, capture_output, text, timeout):
            class _R:
                stdout = "75346\n75412\n"
                returncode = 0
            return _R()
        monkeypatch.setattr(ow_service.subprocess, "run", fake_run)
        assert ow_service._find_port_owners(8765) == [75346, 75412]

    def test_returns_empty_when_no_owner(self, monkeypatch):
        """占有プロセスなし（lsof空出力＋returncode=1） → 空リスト"""
        def fake_run(cmd, capture_output, text, timeout):
            class _R:
                stdout = ""
                returncode = 1
            return _R()
        monkeypatch.setattr(ow_service.subprocess, "run", fake_run)
        assert ow_service._find_port_owners(8765) == []

    def test_returns_empty_when_lsof_not_found(self, monkeypatch):
        """lsof未インストール → 空リスト（フォールバック）"""
        def fake_run(*a, **kw):
            raise FileNotFoundError("lsof: command not found")
        monkeypatch.setattr(ow_service.subprocess, "run", fake_run)
        assert ow_service._find_port_owners(8765) == []

    def test_returns_empty_on_timeout(self, monkeypatch):
        """lsofがtimeout → 空リスト"""
        def fake_run(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="lsof", timeout=5)
        monkeypatch.setattr(ow_service.subprocess, "run", fake_run)
        assert ow_service._find_port_owners(8765) == []

    def test_skips_non_numeric_lines(self, monkeypatch):
        """lsof出力に数値以外の行が混じっていたらスキップして数値だけ集める"""
        def fake_run(cmd, capture_output, text, timeout):
            class _R:
                stdout = "75346\nnot-a-pid\n75412\n"
                returncode = 0
            return _R()
        monkeypatch.setattr(ow_service.subprocess, "run", fake_run)
        assert ow_service._find_port_owners(8765) == [75346, 75412]


class TestClearRelayPort:
    """`_clear_relay_port()` の挙動: port抽出 → 占有プロセス全kill → 件数返却。"""

    def test_kills_all_owners(self, monkeypatch):
        """占有プロセスが複数 → 全てkillして件数を返す"""
        monkeypatch.setattr(ow_service, "_find_port_owners", lambda port: [11111, 22222])
        killed: list[int] = []
        monkeypatch.setattr(ow_service, "_kill_relay", lambda pid: killed.append(pid))
        assert ow_service._clear_relay_port() == 2
        assert killed == [11111, 22222]

    def test_returns_zero_when_no_owner(self, monkeypatch):
        """占有なし → killせず0を返す"""
        monkeypatch.setattr(ow_service, "_find_port_owners", lambda port: [])
        killed: list[int] = []
        monkeypatch.setattr(ow_service, "_kill_relay", lambda pid: killed.append(pid))
        assert ow_service._clear_relay_port() == 0
        assert killed == []

    def test_returns_zero_when_port_unparseable(self, monkeypatch):
        """RELAY_URLからportが取れない（不正URL等） → 0でフォールバック"""
        monkeypatch.setattr(ow_service, "_get_relay_port", lambda: None)
        find_called = []
        monkeypatch.setattr(ow_service, "_find_port_owners", lambda port: find_called.append(port) or [99999])
        assert ow_service._clear_relay_port() == 0
        # port不明時は_find_port_ownersも呼ばれないこと
        assert find_called == []


class TestGetRelayPort:
    """`_get_relay_port()` の挙動: RELAY_URLからportを抽出する。"""

    def test_extracts_port_from_default_url(self, monkeypatch):
        """デフォルトの http://127.0.0.1:8765 → 8765"""
        monkeypatch.setattr(ow_service, "RELAY_URL", "http://127.0.0.1:8765")
        assert ow_service._get_relay_port() == 8765

    def test_returns_none_when_no_port(self, monkeypatch):
        """portが省略されたURL → None"""
        monkeypatch.setattr(ow_service, "RELAY_URL", "http://relay.example.com")
        assert ow_service._get_relay_port() is None


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
