"""launcher.pyのユニットテスト

デーモン起動ロジック、セッションライフサイクル管理、ヘルスチェックを検証する。
stdio <-> HTTP ブリッジは統合テストで検証する。
"""
import json
import subprocess
import urllib.error
import urllib.request

import pytest

from src import launcher


class TestIsServerRunning:
    def test_returns_true_when_server_responds_200(self, monkeypatch):
        """サーバーが200を返す場合はTrueを返す"""

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        monkeypatch.setattr(
            urllib.request,
            "urlopen",
            lambda req, timeout=None: FakeResponse(),
        )
        assert launcher._is_server_running() is True

    def test_returns_true_on_405(self, monkeypatch):
        """405 (Method Not Allowed) もサーバー起動済みと見なす"""

        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(
                url=req.full_url, code=405, msg="Method Not Allowed",
                hdrs={}, fp=None,
            )

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        assert launcher._is_server_running() is True

    def test_returns_true_on_400(self, monkeypatch):
        """400 (Bad Request) もサーバー起動済みと見なす"""

        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(
                url=req.full_url, code=400, msg="Bad Request",
                hdrs={}, fp=None,
            )

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        assert launcher._is_server_running() is True

    def test_returns_false_on_connection_error(self, monkeypatch):
        """接続エラーの場合はFalseを返す"""

        def fake_urlopen(req, timeout=None):
            raise ConnectionRefusedError("Connection refused")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        assert launcher._is_server_running() is False

    def test_returns_false_on_500(self, monkeypatch):
        """500エラーの場合はFalseを返す"""

        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(
                url=req.full_url, code=500, msg="Internal Server Error",
                hdrs={}, fp=None,
            )

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        assert launcher._is_server_running() is False


class TestStartHttpServer:
    def test_calls_popen_with_correct_args(self, monkeypatch):
        """正しい引数でsubprocess.Popenが呼ばれる"""
        called_with = {}

        class FakePopen:
            def __init__(self, args, **kwargs):
                called_with["args"] = args
                called_with["kwargs"] = kwargs

        monkeypatch.setattr(subprocess, "Popen", FakePopen)
        result = launcher._start_http_server()

        assert result is True
        assert called_with["args"][1:] == ["-m", "src.main", "--transport", "http"]
        assert called_with["kwargs"]["start_new_session"] is True
        assert called_with["kwargs"]["stdout"] == subprocess.DEVNULL
        assert called_with["kwargs"]["stderr"] == subprocess.DEVNULL
        assert called_with["kwargs"]["cwd"] == launcher._PROJECT_ROOT

    def test_returns_false_on_oserror(self, monkeypatch):
        """OSErrorの場合はFalseを返す"""

        def fake_popen(*args, **kwargs):
            raise OSError("Permission denied")

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        assert launcher._start_http_server() is False


class TestEnsureServerRunning:
    def test_returns_true_if_already_running(self, monkeypatch):
        """既にサーバーが起動している場合はTrueを即座に返す"""
        monkeypatch.setattr(launcher, "_is_server_running", lambda: True)
        assert launcher._ensure_server_running() is True

    def test_starts_server_and_waits(self, monkeypatch):
        """サーバーを起動し、起動確認を待つ"""
        call_count = {"check": 0}

        def fake_is_running():
            call_count["check"] += 1
            # 最初の呼び出し（_ensure_server_running冒頭）はFalse
            # 3回目の呼び出し（待機ループ2回目）でTrue
            return call_count["check"] >= 3

        monkeypatch.setattr(launcher, "_is_server_running", fake_is_running)
        monkeypatch.setattr(launcher, "_start_http_server", lambda: True)
        monkeypatch.setattr(launcher.time, "sleep", lambda _: None)

        assert launcher._ensure_server_running() is True

    def test_returns_false_on_start_failure(self, monkeypatch):
        """起動失敗でFalseを返す"""
        monkeypatch.setattr(launcher, "_is_server_running", lambda: False)
        monkeypatch.setattr(launcher, "_start_http_server", lambda: False)
        assert launcher._ensure_server_running() is False

    def test_returns_false_on_timeout(self, monkeypatch):
        """タイムアウトでFalseを返す"""
        monkeypatch.setattr(launcher, "_is_server_running", lambda: False)
        monkeypatch.setattr(launcher, "_start_http_server", lambda: True)
        monkeypatch.setattr(launcher.time, "sleep", lambda _: None)
        assert launcher._ensure_server_running() is False


class TestEnsureServerRunningStaleLock:
    """_ensure_server_running のstale lock処理のテスト"""

    def test_stale_lock_pid_dead(self, monkeypatch, tmp_path):
        """PIDが死んでいるロックファイルはstaleとして削除し、サーバーを起動する"""
        from src.infra import lock_file

        lock_dir = tmp_path / ".cc-memory"
        lock_dir.mkdir()
        lock_path = lock_dir / "server.lock"
        lock_path.write_text('{"pid": 99999999, "port": 52837}', encoding="utf-8")
        monkeypatch.setattr(lock_file, "LOCK_FILE", lock_path)
        monkeypatch.setattr(lock_file, "is_process_alive", lambda pid: False)

        call_count = {"check": 0}

        def fake_is_running():
            call_count["check"] += 1
            return call_count["check"] >= 3

        monkeypatch.setattr(launcher, "_is_server_running", fake_is_running)
        monkeypatch.setattr(launcher, "_start_http_server", lambda: True)
        monkeypatch.setattr(launcher.time, "sleep", lambda _: None)

        assert launcher._ensure_server_running() is True
        # ロックファイルが削除されている
        assert not lock_path.exists()

    def test_lock_pid_alive_waits_for_server(self, monkeypatch, tmp_path):
        """PIDが生きているロックファイルがあれば、サーバーの準備完了を待つ"""
        from src.infra import lock_file

        lock_dir = tmp_path / ".cc-memory"
        lock_dir.mkdir()
        lock_path = lock_dir / "server.lock"
        lock_path.write_text('{"pid": 99999999, "port": 52837}', encoding="utf-8")
        monkeypatch.setattr(lock_file, "LOCK_FILE", lock_path)
        monkeypatch.setattr(lock_file, "is_process_alive", lambda pid: True)

        started = {"called": False}

        def fake_start():
            started["called"] = True
            return True

        call_count = {"check": 0}

        def fake_is_running():
            call_count["check"] += 1
            return call_count["check"] >= 3

        monkeypatch.setattr(launcher, "_is_server_running", fake_is_running)
        monkeypatch.setattr(launcher, "_start_http_server", fake_start)
        monkeypatch.setattr(launcher.time, "sleep", lambda _: None)

        assert launcher._ensure_server_running() is True
        # PIDが生きているので_start_http_serverは呼ばれない
        assert started["called"] is False
        # ロックファイルはそのまま
        assert lock_path.exists()


class TestSessionRegistration:
    def test_register_success(self, monkeypatch):
        """セッション登録が成功する"""

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self):
                return json.dumps({"registered": True, "active_sessions": 1}).encode()

        monkeypatch.setattr(
            urllib.request,
            "urlopen",
            lambda req, timeout=None: FakeResponse(),
        )
        assert launcher._register_session() is True

    def test_register_failure(self, monkeypatch):
        """セッション登録が失敗する"""

        def fake_urlopen(req, timeout=None):
            raise ConnectionRefusedError("Connection refused")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        assert launcher._register_session() is False

    def test_unregister_success(self, monkeypatch):
        """セッション解除が成功する"""

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self):
                return json.dumps({"unregistered": True, "active_sessions": 0}).encode()

        monkeypatch.setattr(
            urllib.request,
            "urlopen",
            lambda req, timeout=None: FakeResponse(),
        )
        assert launcher._unregister_session() is True

    def test_unregister_failure(self, monkeypatch):
        """セッション解除が失敗する"""

        def fake_urlopen(req, timeout=None):
            raise ConnectionRefusedError("Connection refused")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        assert launcher._unregister_session() is False


class TestCleanup:
    def test_cleanup_calls_unregister(self, monkeypatch):
        """クリーンアップでunregisterが呼ばれる"""
        called = {"unregister": False}

        def fake_unregister():
            called["unregister"] = True
            return True

        monkeypatch.setattr(launcher, "_unregister_session", fake_unregister)
        # _cleanup_doneをリセット
        monkeypatch.setattr(launcher, "_cleanup_done", False)
        launcher._cleanup()
        assert called["unregister"] is True

    def test_cleanup_idempotent(self, monkeypatch):
        """クリーンアップは2回呼んでも1回しか実行されない"""
        call_count = {"unregister": 0}

        def fake_unregister():
            call_count["unregister"] += 1
            return True

        monkeypatch.setattr(launcher, "_unregister_session", fake_unregister)
        monkeypatch.setattr(launcher, "_cleanup_done", False)
        launcher._cleanup()
        launcher._cleanup()
        assert call_count["unregister"] == 1


class TestSessionId:
    def test_session_id_is_valid_uuid(self):
        """セッションIDが有効なUUIDである"""
        import uuid
        # ValueError が出なければOK
        uuid.UUID(launcher._session_id)

    def test_session_id_is_string(self):
        """セッションIDが文字列である"""
        assert isinstance(launcher._session_id, str)


class TestProjectRoot:
    def test_project_root_points_to_package_root(self):
        """_PROJECT_ROOTがパッケージルートを指している"""
        import os
        assert os.path.isdir(launcher._PROJECT_ROOT)
        assert os.path.isfile(os.path.join(launcher._PROJECT_ROOT, "pyproject.toml"))


class TestBridgeSessionTermination:
    def test_bridge_passes_terminate_on_close_true(self, monkeypatch):
        """_bridge: streamable_http_clientにterminate_on_close=Trueを渡す

        DELETEを送らないとサーバー側のStreamableHTTPSessionManagerが
        切断済みセッションを保持し続けるため、この値の回帰を検知する。
        """
        import asyncio
        from contextlib import asynccontextmanager

        import mcp.client.streamable_http as streamable_http_module

        captured = {}

        class _Abort(Exception):
            """接続確立前にブリッジを打ち切るためのセンチネル例外"""

        @asynccontextmanager
        async def fake_client(**kwargs):
            captured.update(kwargs)
            raise _Abort()
            yield  # pragma: no cover

        # _bridge内の遅延import（from mcp.client.streamable_http import ...）が
        # 参照するモジュール属性を差し替える
        monkeypatch.setattr(
            streamable_http_module, "streamable_http_client", fake_client
        )

        with pytest.raises(_Abort):
            asyncio.run(launcher._bridge())

        assert captured["terminate_on_close"] is True


class TestServerDisconnected:
    def test_is_exception(self):
        """ServerDisconnectedがExceptionのサブクラスである"""
        assert issubclass(launcher.ServerDisconnected, Exception)

    def test_can_be_raised_and_caught(self):
        """ServerDisconnectedをraise/catchできる"""
        with pytest.raises(launcher.ServerDisconnected, match="test message"):
            raise launcher.ServerDisconnected("test message")


class TestMainRetryLoop:
    """main()のリトライループの動作検証"""

    def _setup_main(self, monkeypatch, bridge_side_effects, max_retries=3):
        """main()テスト用の共通セットアップ

        bridge_side_effectsにはasyncio.run(_bridge())の戻り値/例外のリストを渡す。
        max_retries で MAX_RETRIES を明示的に上書きする（None で無限）。
        """
        monkeypatch.setattr(launcher, "MAX_RETRIES", max_retries)
        monkeypatch.setattr(launcher, "_IS_LOCAL", True)
        monkeypatch.setattr(launcher, "_cleanup_done", False)
        monkeypatch.setattr(launcher, "_ensure_server_running", lambda: True)
        monkeypatch.setattr(launcher, "_register_session", lambda: True)
        monkeypatch.setattr(launcher, "_unregister_session", lambda: True)
        monkeypatch.setattr(launcher.time, "sleep", lambda _: None)

        call_count = {"bridge": 0}

        def fake_asyncio_run(coro):
            # コルーチンを破棄（awaitしない）
            coro.close()
            idx = call_count["bridge"]
            call_count["bridge"] += 1
            effect = bridge_side_effects[idx]
            if isinstance(effect, Exception):
                raise effect
            return effect

        monkeypatch.setattr(launcher.asyncio, "run", fake_asyncio_run)
        return call_count

    def test_normal_exit_no_retry(self, monkeypatch):
        """stdin EOF（正常終了）ではリトライしない"""
        call_count = self._setup_main(monkeypatch, [None])  # bridge returns None
        launcher.main()
        assert call_count["bridge"] == 1

    def test_server_disconnected_retries(self, monkeypatch):
        """ServerDisconnectedでリトライし、次の接続で成功する"""
        call_count = self._setup_main(monkeypatch, [
            launcher.ServerDisconnected("lost"),  # attempt 0: fail
            None,  # attempt 1: success
        ])
        launcher.main()
        assert call_count["bridge"] == 2

    def test_max_retries_exceeded(self, monkeypatch):
        """MAX_RETRIES回リトライしても失敗したら終了する。max_retries=3 → 4 回呼ばれる"""
        call_count = self._setup_main(monkeypatch, [
            launcher.ServerDisconnected("lost"),  # attempt 0
            launcher.ServerDisconnected("lost"),  # attempt 1
            launcher.ServerDisconnected("lost"),  # attempt 2
            launcher.ServerDisconnected("lost"),  # attempt 3 (max)
        ])
        launcher.main()
        assert call_count["bridge"] == 4  # max_retries=3 → 1 初回 + 3 リトライ

    def test_unexpected_exception_retries(self, monkeypatch):
        """予期しない例外でもリトライする"""
        call_count = self._setup_main(monkeypatch, [
            ConnectionError("connection reset"),  # attempt 0: fail
            None,  # attempt 1: success
        ])
        launcher.main()
        assert call_count["bridge"] == 2

    def test_ensure_server_called_each_attempt(self, monkeypatch):
        """リトライのたびに_ensure_server_runningが呼ばれる"""
        ensure_count = {"calls": 0}

        def counting_ensure():
            ensure_count["calls"] += 1
            return True

        self._setup_main(monkeypatch, [
            launcher.ServerDisconnected("lost"),
            None,
        ])
        # _setup_mainの後にcounting_ensureで再上書き
        monkeypatch.setattr(launcher, "_ensure_server_running", counting_ensure)
        launcher.main()
        assert ensure_count["calls"] == 2

    def test_backoff_values(self, monkeypatch):
        """バックオフが2秒, 4秒, 8秒の順で適用される"""
        sleep_values = []

        def tracking_sleep(seconds):
            sleep_values.append(seconds)

        self._setup_main(monkeypatch, [
            launcher.ServerDisconnected("lost"),
            launcher.ServerDisconnected("lost"),
            launcher.ServerDisconnected("lost"),
            launcher.ServerDisconnected("lost"),
        ])
        # _setup_mainのsleep上書きの後にtracking_sleepで再上書き
        monkeypatch.setattr(launcher.time, "sleep", tracking_sleep)
        launcher.main()
        assert sleep_values == [2, 4, 8]

    def test_cleanup_called_once(self, monkeypatch):
        """main()終了時にcleanupが1回だけ呼ばれる"""
        cleanup_count = {"calls": 0}

        def counting_cleanup():
            cleanup_count["calls"] += 1

        monkeypatch.setattr(launcher, "MAX_RETRIES", 3)
        monkeypatch.setattr(launcher, "_IS_LOCAL", True)
        monkeypatch.setattr(launcher, "_cleanup_done", False)
        monkeypatch.setattr(launcher, "_cleanup", counting_cleanup)
        monkeypatch.setattr(launcher, "_ensure_server_running", lambda: True)
        monkeypatch.setattr(launcher, "_register_session", lambda: True)
        monkeypatch.setattr(launcher.time, "sleep", lambda _: None)

        def fake_asyncio_run(coro):
            coro.close()
            return None

        monkeypatch.setattr(launcher.asyncio, "run", fake_asyncio_run)
        launcher.main()
        assert cleanup_count["calls"] == 1

    def test_backoff_capped_at_60_seconds(self, monkeypatch):
        """backoff は BACKOFF_CAP_SEC (60秒) で頭打ちになる"""
        sleep_values = []

        def tracking_sleep(seconds):
            sleep_values.append(seconds)

        # attempt 0..7 で失敗させる（max_retries=8 で 8 回 sleep が発生）
        # 期待: 2, 4, 8, 16, 32, 60, 60, 60
        self._setup_main(
            monkeypatch,
            [launcher.ServerDisconnected("lost")] * 9,
            max_retries=8,
        )
        monkeypatch.setattr(launcher.time, "sleep", tracking_sleep)
        launcher.main()
        assert sleep_values == [2, 4, 8, 16, 32, 60, 60, 60]

    def test_infinite_retries_stops_on_success(self, monkeypatch):
        """MAX_RETRIES=None (無限) のとき、成功するまでリトライし続けて終了する"""
        # 5 回失敗 → 6 回目で成功
        call_count = self._setup_main(
            monkeypatch,
            [launcher.ServerDisconnected("lost")] * 5 + [None],
            max_retries=None,
        )
        launcher.main()
        assert call_count["bridge"] == 6


class TestReadMaxRetries:
    """_read_max_retries() のテスト"""

    def test_returns_none_when_env_unset(self, monkeypatch):
        """env 未設定時は None（無限）を返す"""
        monkeypatch.delenv("CC_MEMORY_LAUNCHER_MAX_RETRIES", raising=False)
        assert launcher._read_max_retries() is None

    def test_returns_none_when_env_empty(self, monkeypatch):
        """env が空文字列のときは None を返す"""
        monkeypatch.setenv("CC_MEMORY_LAUNCHER_MAX_RETRIES", "")
        assert launcher._read_max_retries() is None

    def test_returns_int_when_env_valid(self, monkeypatch):
        """env が有効な数値のときはその値を返す"""
        monkeypatch.setenv("CC_MEMORY_LAUNCHER_MAX_RETRIES", "5")
        assert launcher._read_max_retries() == 5

    def test_returns_zero_when_env_zero(self, monkeypatch):
        """env が 0 のときは 0 を返す（リトライしないという有効値）"""
        monkeypatch.setenv("CC_MEMORY_LAUNCHER_MAX_RETRIES", "0")
        assert launcher._read_max_retries() == 0

    def test_returns_none_on_invalid_string(self, monkeypatch):
        """env が数値に変換できない文字列のときは None にフォールバック"""
        monkeypatch.setenv("CC_MEMORY_LAUNCHER_MAX_RETRIES", "abc")
        assert launcher._read_max_retries() is None

    def test_returns_none_on_negative(self, monkeypatch):
        """env が負値のときは None にフォールバック"""
        monkeypatch.setenv("CC_MEMORY_LAUNCHER_MAX_RETRIES", "-1")
        assert launcher._read_max_retries() is None


class TestBackoffCap:
    def test_backoff_cap_constant(self):
        """BACKOFF_CAP_SEC が 60 秒に設定されている"""
        assert launcher.BACKOFF_CAP_SEC == 60


class TestMaxRetriesDefault:
    """env による MAX_RETRIES のロードを importlib.reload で検証する。

    `importlib.reload` の副作用（モジュールレベル変数の書き換え）はテスト終了後も
    残るため、各テストの末尾で `MAX_RETRIES` を None に戻す（monkeypatch だけでは
    モジュール属性の reload 結果は元に戻らない）。
    """

    def test_default_is_none_when_env_unset(self, monkeypatch):
        """env 未設定でモジュールを再読み込みすると MAX_RETRIES は None"""
        import importlib

        monkeypatch.delenv("CC_MEMORY_LAUNCHER_MAX_RETRIES", raising=False)
        importlib.reload(launcher)
        try:
            assert launcher.MAX_RETRIES is None
        finally:
            launcher.MAX_RETRIES = None

    def test_override_via_env(self, monkeypatch):
        """env で数値指定するとモジュール再読み込みで MAX_RETRIES がその値になる"""
        import importlib

        monkeypatch.setenv("CC_MEMORY_LAUNCHER_MAX_RETRIES", "7")
        importlib.reload(launcher)
        try:
            assert launcher.MAX_RETRIES == 7
        finally:
            launcher.MAX_RETRIES = None
