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
        monkeypatch.setattr(launcher, "unregister_launcher_session", lambda *a, **kw: None)
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
        monkeypatch.setattr(launcher, "unregister_launcher_session", lambda *a, **kw: None)
        monkeypatch.setattr(launcher, "_cleanup_done", False)
        launcher._cleanup()
        launcher._cleanup()
        assert call_count["unregister"] == 1

    def test_cleanup_calls_unregister_launcher_session(self, monkeypatch):
        """クリーンアップで登録ファイル解除（unregister_launcher_session）も呼ばれる"""
        called = {"launcher_session": False}

        monkeypatch.setattr(launcher, "_unregister_session", lambda: True)
        monkeypatch.setattr(
            launcher,
            "unregister_launcher_session",
            lambda *a, **kw: called.__setitem__("launcher_session", True),
        )
        monkeypatch.setattr(launcher, "_cleanup_done", False)
        launcher._cleanup()
        assert called["launcher_session"] is True


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


class TestBridgeIdentityHeader:
    """_bridge: 全MCPリクエストに bridge identity ヘッダを付与することの検証"""

    def _run_bridge_and_capture_http_client(self, monkeypatch):
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

        monkeypatch.setattr(
            streamable_http_module, "streamable_http_client", fake_client
        )

        with pytest.raises(_Abort):
            asyncio.run(launcher._bridge())

        return captured

    def test_bridge_attaches_bridge_session_header(self, monkeypatch):
        """streamable_http_client に渡す http_client のデフォルトヘッダに
        X-CC-Memory-Bridge-Session-Id: <_session_id> が含まれる。
        """
        captured = self._run_bridge_and_capture_http_client(monkeypatch)
        http_client = captured["http_client"]
        assert (
            http_client.headers.get(launcher.BRIDGE_SESSION_HEADER)
            == launcher._session_id
        )

    def test_bridge_uses_same_header_value_across_reconnects(self, monkeypatch):
        """複数回の再接続（リトライループの複数周回）でも毎回同じ値が使われる。"""
        first = self._run_bridge_and_capture_http_client(monkeypatch)
        second = self._run_bridge_and_capture_http_client(monkeypatch)
        assert (
            first["http_client"].headers.get(launcher.BRIDGE_SESSION_HEADER)
            == second["http_client"].headers.get(launcher.BRIDGE_SESSION_HEADER)
            == launcher._session_id
        )


class TestHeartbeatLoop:
    """_bridge 実行中、heartbeat_interval_sec ごとに _register_session 相当の

    呼び出しが発生することの検証。stdin を実パイプの読み込み端に差し替え、
    書き込み端を閉じないことで stdin EOF に達せずブリッジを稼働させ続ける。
    """

    def test_heartbeat_loop_calls_register_periodically(self, monkeypatch):
        import asyncio
        import os
        import types
        from contextlib import asynccontextmanager

        import anyio
        import mcp.client.streamable_http as streamable_http_module

        monkeypatch.setattr(launcher, "HEARTBEAT_INTERVAL_SEC", 0.05)

        register_calls: list[int] = []

        def fake_register_session() -> bool:
            register_calls.append(1)
            return True

        monkeypatch.setattr(launcher, "_register_session", fake_register_session)

        @asynccontextmanager
        async def fake_streamable_http_client(**kwargs):
            # read_stream 側には何も流さない（server_to_stdout をブロックさせ続ける）
            _read_send, read_recv = anyio.create_memory_object_stream(10)
            write_send, _write_recv = anyio.create_memory_object_stream(10)

            async def _get_session_id():
                return None

            try:
                yield (read_recv, write_send, _get_session_id)
            finally:
                await _read_send.aclose()
                await _write_recv.aclose()

        monkeypatch.setattr(
            streamable_http_module,
            "streamable_http_client",
            fake_streamable_http_client,
        )

        # stdin をEOFに達しない実パイプに差し替える（write側を閉じない限りブロックする）
        read_fd, write_fd = os.pipe()
        read_file = os.fdopen(read_fd, "rb", buffering=0)
        fake_stdin = types.SimpleNamespace(buffer=read_file)
        monkeypatch.setattr(launcher.sys, "stdin", fake_stdin)

        async def _run_with_timeout() -> None:
            # asyncio.wait_forがタイムアウトでtask groupをcancelすると、
            # server_to_stdoutのfinally節がstdin_eof=False（stdinは意図的に
            # ブロックさせ続けている）としてServerDisconnectedを送出し、
            # anyioがこれをExceptionGroupにまとめて再送出する。ここでの
            # 関心はheartbeat_loopが実際に register を複数回呼んだかどうかで
            # あり、cancel経路の具体的な例外形状は問わない。
            try:
                await asyncio.wait_for(launcher._bridge(), timeout=0.6)
            except Exception:
                pass

        try:
            asyncio.run(_run_with_timeout())
        finally:
            os.close(write_fd)
            read_file.close()

        assert len(register_calls) >= 2


class TestBridgeStdinEofWithHeartbeat:
    """_bridge 実行中に実際の stdin EOF が発生した場合、heartbeat_loop が

    並行動作していても _bridge() が正常に return することの検証。

    heartbeat_loop は自発的に終了しない無限ループのため、
    stdin_to_server / server_to_stdout が例外なく完了しただけでは
    task group 全体は終了しない。本テストは fake の read/write ストリームを
    相互に連動させ、「送信側 (write_stream) を閉じると受信側 (read_stream) も
    自然終了する」という実際のサーバー接続の挙動を模したうえで、実パイプ経由の
    stdin EOF から _bridge() が完走することをタイムアウト付きで確認する。
    """

    def test_bridge_returns_normally_on_stdin_eof_with_heartbeat_running(
        self, monkeypatch
    ):
        import asyncio
        import os
        import types
        from contextlib import asynccontextmanager

        import anyio
        import mcp.client.streamable_http as streamable_http_module

        monkeypatch.setattr(launcher, "HEARTBEAT_INTERVAL_SEC", 0.02)

        register_calls: list[int] = []

        def fake_register_session() -> bool:
            register_calls.append(1)
            return True

        monkeypatch.setattr(launcher, "_register_session", fake_register_session)

        @asynccontextmanager
        async def fake_streamable_http_client(**kwargs):
            read_send, read_recv = anyio.create_memory_object_stream(10)
            write_send, write_recv = anyio.create_memory_object_stream(10)

            async def _get_session_id():
                return None

            async def _mirror_write_closure() -> None:
                # write_stream（stdin_to_server が stdin EOF 後に aclose する側）
                # のクローズを検知したら read_stream 側も閉じる。
                try:
                    async for _ in write_recv:
                        pass
                finally:
                    await read_send.aclose()

            async with anyio.create_task_group() as watcher_tg:
                watcher_tg.start_soon(_mirror_write_closure)
                try:
                    yield (read_recv, write_send, _get_session_id)
                finally:
                    await write_recv.aclose()
                    await read_recv.aclose()

        monkeypatch.setattr(
            streamable_http_module,
            "streamable_http_client",
            fake_streamable_http_client,
        )

        # 実パイプを使い、本物の stdin EOF を発生させる。
        # heartbeat_loop が並行動作している証拠を残すため、書き込み端は
        # 即座にではなく別スレッドで少し待ってから閉じる
        # （heartbeat_interval_sec=0.02sより十分長い待ちを挟み、EOF前に
        # 複数回 register が呼ばれることを保証する）。
        import threading

        read_fd, write_fd = os.pipe()

        def _close_write_end_later() -> None:
            import time as _time
            _time.sleep(0.1)
            os.close(write_fd)

        threading.Thread(target=_close_write_end_later, daemon=True).start()

        read_file = os.fdopen(read_fd, "rb", buffering=0)
        fake_stdin = types.SimpleNamespace(buffer=read_file)
        monkeypatch.setattr(launcher.sys, "stdin", fake_stdin)

        try:
            # ハングするバグがあればここでタイムアウトしテストが失敗する
            asyncio.run(asyncio.wait_for(launcher._bridge(), timeout=3.0))
        finally:
            read_file.close()

        # heartbeat_loopが並行して動作していたことの確認
        assert len(register_calls) >= 1


def _contains_server_disconnected(exc: BaseException) -> bool:
    """exc自体、またはBaseExceptionGroupのexceptions配下にServerDisconnectedが

    含まれるかを再帰的に判定する（anyioのtask groupはExceptionGroupへ集約するため）。
    """
    if isinstance(exc, launcher.ServerDisconnected):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_contains_server_disconnected(sub) for sub in exc.exceptions)
    return False


class TestBridgeStdinEofGraceTimeout:
    """_bridge: stdin EOF後、サーバー側がread_streamを閉じない「沈黙ゾンビ化」

    (M#725) 状態でも、STDIN_EOF_GRACE_SEC 経過後に強制的に退場することの検証。
    fixした対策が無ければ、read_streamが永遠に閉じないため_bridge()はハングし
    続ける（テストがタイムアウトで失敗する）。
    """

    def test_bridge_exits_after_grace_period_when_server_stays_silent(
        self, monkeypatch
    ):
        import asyncio
        import os
        import time
        import types
        from contextlib import asynccontextmanager

        import anyio
        import mcp.client.streamable_http as streamable_http_module

        # grace期間を短縮し、テストの実時間を抑える
        monkeypatch.setattr(launcher, "STDIN_EOF_GRACE_SEC", 0.1)
        monkeypatch.setattr(launcher, "HEARTBEAT_INTERVAL_SEC", 1000.0)

        @asynccontextmanager
        async def fake_streamable_http_client(**kwargs):
            # read_stream には何も流さず、write_stream のクローズも監視しない
            # (=サーバー側が応答しない「沈黙ゾンビ化」を模す)。
            read_send, read_recv = anyio.create_memory_object_stream(10)
            write_send, write_recv = anyio.create_memory_object_stream(10)

            async def _get_session_id():
                return None

            try:
                yield (read_recv, write_send, _get_session_id)
            finally:
                await read_send.aclose()
                await write_recv.aclose()

        monkeypatch.setattr(
            streamable_http_module,
            "streamable_http_client",
            fake_streamable_http_client,
        )

        # 実パイプで本物の stdin EOF を即座に発生させる
        read_fd, write_fd = os.pipe()
        os.close(write_fd)  # 即EOF
        read_file = os.fdopen(read_fd, "rb", buffering=0)
        fake_stdin = types.SimpleNamespace(buffer=read_file)
        monkeypatch.setattr(launcher.sys, "stdin", fake_stdin)

        start = time.monotonic()
        try:
            # 対策が無ければここでハングし、外側のwait_forタイムアウト(2.0s)で
            # 失敗する。対策が効いていればgrace(0.1s)経過後すぐ正常return する。
            asyncio.run(asyncio.wait_for(launcher._bridge(), timeout=2.0))
        finally:
            read_file.close()
        elapsed = time.monotonic() - start

        # grace期間(0.1s)経過後まもなく退場していること（2.0sタイムアウトに
        # 頼らずに済んでいること）を確認する
        assert elapsed < 1.0, f"grace timeoutが効いていない可能性: {elapsed:.2f}s"


class TestServerToStdoutConsecutiveExceptionCap:
    """server_to_stdout: read_streamから例外オブジェクトを

    MAX_CONSECUTIVE_STREAM_EXCEPTIONS 回連続で受け取った場合、無限にcontinueせず
    ServerDisconnectedへ倒して外側のリトライに接続することの検証 (M#725)。
    """

    def test_gives_up_after_max_consecutive_exceptions(self, monkeypatch):
        import asyncio
        import os
        import types
        from contextlib import asynccontextmanager

        import anyio
        import mcp.client.streamable_http as streamable_http_module

        monkeypatch.setattr(launcher, "MAX_CONSECUTIVE_STREAM_EXCEPTIONS", 3)
        monkeypatch.setattr(launcher, "HEARTBEAT_INTERVAL_SEC", 1000.0)
        monkeypatch.setattr(launcher, "STDIN_EOF_GRACE_SEC", 1000.0)

        @asynccontextmanager
        async def fake_streamable_http_client(**kwargs):
            read_send, read_recv = anyio.create_memory_object_stream(10)
            write_send, write_recv = anyio.create_memory_object_stream(10)

            async def _get_session_id():
                return None

            for _ in range(5):
                await read_send.send(RuntimeError("stream hiccup"))

            try:
                yield (read_recv, write_send, _get_session_id)
            finally:
                await read_send.aclose()
                await write_recv.aclose()

        monkeypatch.setattr(
            streamable_http_module,
            "streamable_http_client",
            fake_streamable_http_client,
        )

        # stdin は EOF に達しない実パイプ (書き込み端を閉じない)
        read_fd, write_fd = os.pipe()
        read_file = os.fdopen(read_fd, "rb", buffering=0)
        fake_stdin = types.SimpleNamespace(buffer=read_file)
        monkeypatch.setattr(launcher.sys, "stdin", fake_stdin)

        try:
            with pytest.raises(BaseException) as excinfo:
                asyncio.run(asyncio.wait_for(launcher._bridge(), timeout=2.0))
            assert _contains_server_disconnected(excinfo.value), (
                f"ServerDisconnectedへ倒れていない: {excinfo.value!r}"
            )
        finally:
            os.close(write_fd)
            read_file.close()


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
        monkeypatch.setattr(launcher, "register_launcher_session", lambda *a, **kw: None)
        monkeypatch.setattr(launcher, "unregister_launcher_session", lambda *a, **kw: None)
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
        monkeypatch.setattr(launcher, "register_launcher_session", lambda *a, **kw: None)
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


class TestSessionRegistrationGating:
    """main(): セッション登録の _IS_LOCAL による致命度の切り替え検証"""

    def _setup(self, monkeypatch, is_local: bool, register_result: bool):
        monkeypatch.setattr(launcher, "MAX_RETRIES", 0)
        monkeypatch.setattr(launcher, "_IS_LOCAL", is_local)
        monkeypatch.setattr(launcher, "_cleanup_done", False)
        monkeypatch.setattr(launcher, "_ensure_server_running", lambda: True)
        monkeypatch.setattr(launcher, "_register_session", lambda: register_result)
        monkeypatch.setattr(launcher, "_unregister_session", lambda: True)
        monkeypatch.setattr(launcher, "register_launcher_session", lambda *a, **kw: None)
        monkeypatch.setattr(launcher, "unregister_launcher_session", lambda *a, **kw: None)
        monkeypatch.setattr(launcher.time, "sleep", lambda _: None)

        def fake_asyncio_run(coro):
            coro.close()
            return None

        monkeypatch.setattr(launcher.asyncio, "run", fake_asyncio_run)

    def test_local_register_failure_exits(self, monkeypatch):
        """_IS_LOCAL=True で登録失敗すると sys.exit(1) する"""
        self._setup(monkeypatch, is_local=True, register_result=False)
        with pytest.raises(SystemExit) as exc_info:
            launcher.main()
        assert exc_info.value.code == 1

    def test_remote_register_failure_continues_with_warning(self, monkeypatch, caplog):
        """_IS_LOCAL=False で登録失敗しても警告ログのみで _bridge() に進む"""
        self._setup(monkeypatch, is_local=False, register_result=False)
        import logging

        with caplog.at_level(logging.WARNING, logger="src.launcher"):
            launcher.main()  # 例外を出さず正常終了する
        assert any(
            "Session register failed" in record.message for record in caplog.records
        )

    def test_remote_register_success_no_warning(self, monkeypatch, caplog):
        """_IS_LOCAL=False で登録成功時は警告ログを出さない"""
        self._setup(monkeypatch, is_local=False, register_result=True)
        import logging

        with caplog.at_level(logging.WARNING, logger="src.launcher"):
            launcher.main()
        assert not any(
            "Session register failed" in record.message for record in caplog.records
        )


class TestLauncherSessionRegistrationWiring:
    """main(): register_launcher_session の呼び出しタイミング・引数の検証"""

    def test_main_registers_launcher_session_with_own_session_id(self, monkeypatch):
        """register_launcher_session が自身の _session_id で呼ばれる"""
        received = {}

        def fake_register(session_id, pid=None):
            received["session_id"] = session_id

        monkeypatch.setattr(launcher, "MAX_RETRIES", 0)
        monkeypatch.setattr(launcher, "_IS_LOCAL", True)
        monkeypatch.setattr(launcher, "_cleanup_done", False)
        monkeypatch.setattr(launcher, "_ensure_server_running", lambda: True)
        monkeypatch.setattr(launcher, "_register_session", lambda: True)
        monkeypatch.setattr(launcher, "_unregister_session", lambda: True)
        monkeypatch.setattr(launcher, "register_launcher_session", fake_register)
        monkeypatch.setattr(launcher, "unregister_launcher_session", lambda *a, **kw: None)
        monkeypatch.setattr(launcher.time, "sleep", lambda _: None)
        monkeypatch.setattr(launcher.relay_config, "get_token", lambda: "dummy-token")

        def fake_asyncio_run(coro):
            coro.close()
            return None

        monkeypatch.setattr(launcher.asyncio, "run", fake_asyncio_run)
        launcher.main()
        assert received["session_id"] == launcher._session_id

    def test_main_registers_before_server_wait(self, monkeypatch):
        """register_launcher_session は _ensure_server_running（最大30秒待機）より前に呼ばれる"""
        order: list[str] = []

        def fake_register(session_id, pid=None):
            order.append("register_launcher_session")

        def fake_ensure_server_running():
            order.append("_ensure_server_running")
            return True

        monkeypatch.setattr(launcher, "MAX_RETRIES", 0)
        monkeypatch.setattr(launcher, "_IS_LOCAL", True)
        monkeypatch.setattr(launcher, "_cleanup_done", False)
        monkeypatch.setattr(launcher, "_ensure_server_running", fake_ensure_server_running)
        monkeypatch.setattr(launcher, "_register_session", lambda: True)
        monkeypatch.setattr(launcher, "_unregister_session", lambda: True)
        monkeypatch.setattr(launcher, "register_launcher_session", fake_register)
        monkeypatch.setattr(launcher, "unregister_launcher_session", lambda *a, **kw: None)
        monkeypatch.setattr(launcher.time, "sleep", lambda _: None)
        monkeypatch.setattr(launcher.relay_config, "get_token", lambda: "dummy-token")

        def fake_asyncio_run(coro):
            coro.close()
            return None

        monkeypatch.setattr(launcher.asyncio, "run", fake_asyncio_run)
        launcher.main()
        assert order == ["register_launcher_session", "_ensure_server_running"]


class TestLauncherSessionRegistrationTokenGate:
    """main(): register_launcher_session はrelay token未設定時は呼ばれず、
    祖先pidチェーン解決（ps最大5回spawn）のコストを払わないことの検証。
    """

    def _setup_common(self, monkeypatch):
        monkeypatch.setattr(launcher, "MAX_RETRIES", 0)
        monkeypatch.setattr(launcher, "_IS_LOCAL", True)
        monkeypatch.setattr(launcher, "_cleanup_done", False)
        monkeypatch.setattr(launcher, "_ensure_server_running", lambda: True)
        monkeypatch.setattr(launcher, "_register_session", lambda: True)
        monkeypatch.setattr(launcher, "_unregister_session", lambda: True)
        monkeypatch.setattr(launcher, "unregister_launcher_session", lambda *a, **kw: None)
        monkeypatch.setattr(launcher.time, "sleep", lambda _: None)

        def fake_asyncio_run(coro):
            coro.close()
            return None

        monkeypatch.setattr(launcher.asyncio, "run", fake_asyncio_run)

    def test_skips_registration_when_token_unset(self, monkeypatch):
        """token未設定時はregister_launcher_session自体が呼ばれない"""
        called = {"count": 0}

        def fake_register(session_id, pid=None):
            called["count"] += 1

        monkeypatch.setattr(launcher, "register_launcher_session", fake_register)
        self._setup_common(monkeypatch)
        monkeypatch.setattr(launcher.relay_config, "get_token", lambda: None)
        launcher.main()
        assert called["count"] == 0

    def test_ancestor_pid_resolution_not_invoked_when_token_unset(self, monkeypatch):
        """token未設定時は、本体のregister_launcher_session実装が使う
        ancestor_pids（ps最大5回spawnの実体）が一切実行されない（ゼロコスト）。
        """
        import src.services.relay.identity as relay_identity

        def boom(*a, **kw):
            raise AssertionError(
                "ancestor_pids should not run when relay token is unset"
            )

        monkeypatch.setattr(relay_identity, "ancestor_pids", boom)
        self._setup_common(monkeypatch)
        monkeypatch.setattr(launcher.relay_config, "get_token", lambda: None)
        launcher.main()  # 例外なく完走すれば ancestor_pids は呼ばれていない

    def test_registers_when_token_set(self, monkeypatch):
        """token設定済みなら従来通りregister_launcher_sessionを呼ぶ"""
        called = {"count": 0}

        def fake_register(session_id, pid=None):
            called["count"] += 1

        monkeypatch.setattr(launcher, "register_launcher_session", fake_register)
        self._setup_common(monkeypatch)
        monkeypatch.setattr(launcher.relay_config, "get_token", lambda: "dummy-token")
        launcher.main()
        assert called["count"] == 1


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
