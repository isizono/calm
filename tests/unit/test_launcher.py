"""launcher.pyのユニットテスト

デーモン起動ロジック、セッションライフサイクル管理、ヘルスチェックを検証する。
stdio <-> HTTP ブリッジは統合テストで検証する。
"""
import asyncio
import contextlib
import json
import os
import subprocess
import threading
import urllib.error
import urllib.request

import pytest

from src import launcher
from src.infra import git_repo


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
    def test_calls_popen_with_correct_args(self, tmp_path, monkeypatch):
        """正しい引数でsubprocess.Popenが呼ばれ、stderrはファイルに向く

        popen_detachedはsys.platformで分岐するため、POSIX分岐の検証であることを
        明示する(Windows分岐はtest_uses_popen_detached_windows_wiringで検証する)。
        """
        import src.db as db
        from src.infra import detached_process

        monkeypatch.setattr(detached_process.sys, "platform", "darwin")
        monkeypatch.setattr(db, "get_db_path", lambda: str(tmp_path / "discussion.db"))
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
        # DEVNULLではなく、既存のログディレクトリ配下のファイルに向ける
        stderr_file = called_with["kwargs"]["stderr"]
        assert stderr_file != subprocess.DEVNULL
        assert stderr_file.name == str(tmp_path / "logs" / "server.stderr.log")
        assert called_with["kwargs"]["cwd"] == launcher._PROJECT_ROOT

    def test_overwrites_stderr_log_on_each_start(self, tmp_path, monkeypatch):
        """肥大化しないよう、起動のたびにstderrログを上書きする"""
        import src.db as db

        monkeypatch.setattr(db, "get_db_path", lambda: str(tmp_path / "discussion.db"))
        log_path = tmp_path / "logs" / "server.stderr.log"
        log_path.parent.mkdir(parents=True)
        log_path.write_bytes(b"previous run's stale output\n" * 100)

        monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: None)
        assert launcher._start_http_server() is True
        assert log_path.read_bytes() == b""

    def test_returns_false_on_oserror(self, tmp_path, monkeypatch):
        """OSErrorの場合はFalseを返す"""
        import src.db as db

        monkeypatch.setattr(db, "get_db_path", lambda: str(tmp_path / "discussion.db"))

        def fake_popen(*args, **kwargs):
            raise OSError("Permission denied")

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        assert launcher._start_http_server() is False

    def test_falls_back_to_devnull_when_stderr_log_cannot_be_prepared(self, tmp_path, monkeypatch):
        """診断用stderrログの準備(mkdir)自体が失敗しても、サーバー起動は続行する

        logs/ディレクトリの代わりに同名の通常ファイルを置き、
        mkdir(parents=True, exist_ok=True)を実際のFileExistsErrorで失敗させる。
        """
        import src.db as db

        monkeypatch.setattr(db, "get_db_path", lambda: str(tmp_path / "discussion.db"))
        (tmp_path / "logs").write_bytes(b"")
        called_with = {}

        class FakePopen:
            def __init__(self, args, **kwargs):
                called_with["kwargs"] = kwargs

        monkeypatch.setattr(subprocess, "Popen", FakePopen)
        assert launcher._start_http_server() is True
        assert called_with["kwargs"]["stderr"] == subprocess.DEVNULL

    def test_uses_popen_detached_windows_wiring(self, tmp_path, monkeypatch):
        """popen_detached経由でWindows用kwargsが渡ること

        popen_detachedを経由せずstart_new_session=Trueで直接起動する実装に戻しても
        気づけない回帰を防ぐため、popen_detachedのWindows分岐が実際に呼び出される
        ことを確かめる。
        """
        import src.db as db
        from src.infra import detached_process

        monkeypatch.setattr(db, "get_db_path", lambda: str(tmp_path / "discussion.db"))
        called_with = {}

        class FakePopen:
            def __init__(self, args, **kwargs):
                called_with["kwargs"] = kwargs

        monkeypatch.setattr(detached_process.sys, "platform", "win32")
        monkeypatch.setattr(subprocess, "Popen", FakePopen)

        assert launcher._start_http_server() is True
        assert called_with["kwargs"]["creationflags"] == (
            detached_process._CREATE_NEW_PROCESS_GROUP | detached_process._CREATE_NO_WINDOW
        )
        assert called_with["kwargs"]["stdin"] == subprocess.DEVNULL


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

    def test_stale_lock_pid_alive_but_start_time_mismatch(self, monkeypatch, tmp_path):
        """PIDが生きていても起動時刻が記録と食い違う(PID再利用)場合はstaleとして削除する"""
        from src.infra import lock_file

        lock_dir = tmp_path / ".cc-memory"
        lock_dir.mkdir()
        lock_path = lock_dir / "server.lock"
        lock_path.write_text(
            '{"pid": 99999999, "port": 52837, "start_time": "old-sig"}', encoding="utf-8"
        )
        monkeypatch.setattr(lock_file, "LOCK_FILE", lock_path)
        monkeypatch.setattr(lock_file, "is_process_alive", lambda pid: True)
        monkeypatch.setattr(lock_file, "process_start_signature", lambda pid: "new-sig")

        call_count = {"check": 0}

        def fake_is_running():
            call_count["check"] += 1
            return call_count["check"] >= 3

        monkeypatch.setattr(launcher, "_is_server_running", fake_is_running)
        monkeypatch.setattr(launcher, "_start_http_server", lambda: True)
        monkeypatch.setattr(launcher.time, "sleep", lambda _: None)

        assert launcher._ensure_server_running() is True
        assert not lock_path.exists()

    def test_unlink_oserror_is_swallowed(self, monkeypatch, tmp_path):
        """stale lock削除がOSErrorになっても例外を外に漏らさず起動フローを続ける"""
        from src.infra import lock_file

        lock_dir = tmp_path / ".cc-memory"
        lock_dir.mkdir()
        lock_path = lock_dir / "server.lock"
        lock_path.write_text('{"pid": 99999999, "port": 52837}', encoding="utf-8")
        monkeypatch.setattr(lock_file, "is_process_alive", lambda pid: False)

        class _BoomPath:
            """unlinkだけ共有違反風のOSErrorにし、他の操作は実パスに委譲する"""

            def __getattr__(self, name):
                return getattr(lock_path, name)

            def unlink(self, missing_ok=True):
                raise OSError(32, "The process cannot access the file")

        monkeypatch.setattr(lock_file, "LOCK_FILE", _BoomPath())

        call_count = {"check": 0}

        def fake_is_running():
            call_count["check"] += 1
            return call_count["check"] >= 3

        start_calls = {"count": 0}

        def fake_start_http_server():
            start_calls["count"] += 1
            return True

        monkeypatch.setattr(launcher, "_is_server_running", fake_is_running)
        monkeypatch.setattr(launcher, "_start_http_server", fake_start_http_server)
        monkeypatch.setattr(launcher.time, "sleep", lambda _: None)

        # unlinkがOSErrorを投げても例外は外に伝播せず、新サーバーの起動まで進む
        assert launcher._ensure_server_running() is True
        assert start_calls["count"] == 1


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


class TestPropagatePluginRootEnv:
    """_propagate_plugin_root_env: プラグイン実行時のCALM_PROJECT_ROOT自動設定"""

    def test_sets_calm_project_root_from_claude_plugin_root(self, monkeypatch):
        """CLAUDE_PLUGIN_ROOTが設定されCALM_PROJECT_ROOTが未設定なら、その値を設定する"""
        monkeypatch.delenv("CALM_PROJECT_ROOT", raising=False)
        monkeypatch.delenv("CCM_PROJECT_ROOT", raising=False)
        monkeypatch.delenv("CC_MEMORY_PROJECT_ROOT", raising=False)
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/plugin/cache/calm/1.0.0")

        launcher._propagate_plugin_root_env()

        assert os.environ["CALM_PROJECT_ROOT"] == "/plugin/cache/calm/1.0.0"

    def test_does_not_override_existing_calm_project_root(self, monkeypatch):
        """CALM_PROJECT_ROOTが既に設定済みなら上書きしない"""
        monkeypatch.setenv("CALM_PROJECT_ROOT", "/explicit/root")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/plugin/cache/calm/1.0.0")

        launcher._propagate_plugin_root_env()

        assert os.environ["CALM_PROJECT_ROOT"] == "/explicit/root"

    def test_respects_legacy_env_name_as_already_set(self, monkeypatch):
        """旧名(CC_MEMORY_PROJECT_ROOT)が設定済みの場合も「既に設定済み」として扱い上書きしない"""
        monkeypatch.delenv("CALM_PROJECT_ROOT", raising=False)
        monkeypatch.delenv("CCM_PROJECT_ROOT", raising=False)
        monkeypatch.setenv("CC_MEMORY_PROJECT_ROOT", "/legacy/root")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/plugin/cache/calm/1.0.0")

        launcher._propagate_plugin_root_env()

        assert "CALM_PROJECT_ROOT" not in os.environ
        assert os.environ["CC_MEMORY_PROJECT_ROOT"] == "/legacy/root"

    def test_falls_back_to_cwd_when_not_a_git_repository(self, monkeypatch, tmp_path):
        """CLAUDE_PLUGIN_ROOT・CALM_PROJECT_ROOTともに未設定で、cwdがgitリポジトリでない
        場合、cwd自身をCALM_PROJECT_ROOTに設定する(CLAUDE_PLUGIN_ROOTがClaude Code本体側の
        間欠バグで渡らないケースのfallback)。

        git-common-dir解決自体のアルゴリズムはtests/unit/test_git_repo.pyで検証済みのため、
        ここではgit_repo.subprocessのrun関数（外部境界）のみモックし、os.getcwd()が
        resolve_main_repo_root()へ正しく渡り、戻り値がそのままCALM_PROJECT_ROOTに
        反映される配線を検証する。
        """
        monkeypatch.delenv("CALM_PROJECT_ROOT", raising=False)
        monkeypatch.delenv("CCM_PROJECT_ROOT", raising=False)
        monkeypatch.delenv("CC_MEMORY_PROJECT_ROOT", raising=False)
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        monkeypatch.chdir(tmp_path)

        def fake_run(cmd, **kwargs):
            raise subprocess.CalledProcessError(128, cmd, stderr="fatal: not a git repository")

        monkeypatch.setattr(git_repo.subprocess, "run", fake_run)

        launcher._propagate_plugin_root_env()

        assert os.environ["CALM_PROJECT_ROOT"] == str(tmp_path.resolve())

    def test_falls_back_to_main_repo_root_when_cwd_is_a_git_worktree(self, monkeypatch, tmp_path):
        """cwdがgit worktree配下の場合、main repoルートに正規化して設定する
        (worktreeルートをそのまま設定しない)。gitリポジトリ配下ではgit解決が先に
        成功するため、この経路ではos.getcwd()の値がそのまま使われることはない。
        """
        monkeypatch.delenv("CALM_PROJECT_ROOT", raising=False)
        monkeypatch.delenv("CCM_PROJECT_ROOT", raising=False)
        monkeypatch.delenv("CC_MEMORY_PROJECT_ROOT", raising=False)
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)

        worktree_root = tmp_path / "worktree"
        worktree_root.mkdir()
        main_repo_root = tmp_path / "main-repo"
        git_common_dir = main_repo_root / ".git"

        def fake_run(cmd, **kwargs):
            assert cmd == ["git", "rev-parse", "--git-common-dir"]
            assert kwargs["cwd"] == worktree_root
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{git_common_dir}\n", stderr="")

        monkeypatch.setattr(git_repo.subprocess, "run", fake_run)
        monkeypatch.chdir(worktree_root)

        launcher._propagate_plugin_root_env()

        assert os.environ["CALM_PROJECT_ROOT"] == str(main_repo_root.resolve())


class TestBridgeSessionTermination:
    def test_bridge_passes_terminate_on_close_true(self, monkeypatch):
        """_bridge: streamable_http_clientにterminate_on_close=Trueを渡す

        DELETEを送らないとサーバー側のStreamableHTTPSessionManagerが
        切断済みセッションを保持し続けるため、この値の回帰を検知する。
        """
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
            asyncio.run(launcher._bridge(launcher._StdinBridgeState()))

        assert captured["terminate_on_close"] is True


class TestBridgeIdentityHeader:
    """_bridge: 全MCPリクエストに bridge identity ヘッダを付与することの検証"""

    def _run_bridge_and_capture_http_client(self, monkeypatch):
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
            asyncio.run(launcher._bridge(launcher._StdinBridgeState()))

        return captured

    def test_bridge_attaches_bridge_session_header(self, monkeypatch):
        """streamable_http_client に渡す http_client のデフォルトヘッダに
        X-Calm-Bridge-Session-Id: <_session_id> が含まれる。
        """
        captured = self._run_bridge_and_capture_http_client(monkeypatch)
        http_client = captured["http_client"]
        assert (
            http_client.headers.get(launcher.BRIDGE_SESSION_HEADER)
            == launcher._session_id
        )

    def test_bridge_also_attaches_legacy_header_with_same_value(self, monkeypatch):
        """移行期間中は改名前のサーバー向けに旧ヘッダにも同じ値を載せる。"""
        captured = self._run_bridge_and_capture_http_client(monkeypatch)
        http_client = captured["http_client"]
        assert (
            http_client.headers.get(launcher.LEGACY_BRIDGE_SESSION_HEADER)
            == launcher._session_id
        )

    def test_header_names_match_server_side(self):
        """launcher が送るヘッダ名とサーバー側が読むヘッダ名が一致する
        （HTTPヘッダ名は大文字小文字を区別しないため小文字で比較）。"""
        from src.infra import session_identity

        assert launcher.BRIDGE_SESSION_HEADER.lower() == session_identity.BRIDGE_SESSION_HEADER
        assert (
            launcher.LEGACY_BRIDGE_SESSION_HEADER.lower()
            == session_identity.LEGACY_BRIDGE_SESSION_HEADER
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

    def test_bridge_disables_trust_env_when_local(self, monkeypatch):
        """ローカルモードでは環境のプロキシ設定(trust_env)を無視する。

        手動プロキシ設定はあるが環境変数が無い環境で、ループバック接続が
        社内プロキシへ誤って送られるのを防ぐため。
        """
        monkeypatch.setattr(launcher, "_IS_LOCAL", True)
        captured = self._run_bridge_and_capture_http_client(monkeypatch)
        assert captured["http_client"].trust_env is False

    def test_bridge_keeps_trust_env_when_remote(self, monkeypatch):
        """リモートモード(CALM_URL指定時)ではtrust_envの既定(True)を変えない。"""
        monkeypatch.setattr(launcher, "_IS_LOCAL", False)
        captured = self._run_bridge_and_capture_http_client(monkeypatch)
        assert captured["http_client"].trust_env is True


class TestHeartbeatLoop:
    """_bridge 実行中、heartbeat_interval_sec ごとに _register_session 相当の

    呼び出しが発生することの検証。共有stateのoutboundキューを空のまま、
    stdin_eofも立てないことで、stdinがまだ開いている状態を模しブリッジを
    稼働させ続ける。
    """

    def test_heartbeat_loop_calls_register_periodically(self, monkeypatch):
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

        state = launcher._StdinBridgeState()

        async def _run_with_timeout() -> None:
            # asyncio.wait_forがタイムアウトでtask groupをcancelすると、
            # server_to_stdoutのfinally節がstdin_eof=False（stdinは意図的に
            # ブロックさせ続けている）としてServerDisconnectedを送出し、
            # anyioがこれをExceptionGroupにまとめて再送出する。ここでの
            # 関心はheartbeat_loopが実際に register を複数回呼んだかどうかで
            # あり、cancel経路の具体的な例外形状は問わない。
            try:
                await asyncio.wait_for(launcher._bridge(state), timeout=0.6)
            except Exception:
                pass

        asyncio.run(_run_with_timeout())

        assert len(register_calls) >= 2


class TestBridgeStdinEofWithHeartbeat:
    """_bridge 実行中に stdin EOF（state.stdin_eof）が確定した場合、

    heartbeat_loop が並行動作していても _bridge() が正常に return することの
    検証。

    heartbeat_loop は自発的に終了しない無限ループのため、
    queue_to_server / server_to_stdout が例外なく完了しただけでは
    task group 全体は終了しない。本テストは fake の read/write ストリームを
    相互に連動させ、「送信側 (write_stream) を閉じると受信側 (read_stream) も
    自然終了する」という実際のサーバー接続の挙動を模したうえで、heartbeat が
    最低1回動いてから stdin EOF を確定させ、_bridge() が完走することを
    タイムアウト付きで確認する。
    """

    def test_bridge_returns_normally_on_stdin_eof_with_heartbeat_running(
        self, monkeypatch
    ):
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
                # write_stream（queue_to_server が stdin EOF 後に aclose する側）
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

        state = launcher._StdinBridgeState()

        async def _set_eof_after_heartbeat() -> None:
            # 固定sleepだとheartbeatが一度も走らずEOFに達しうるため条件待ちにする
            while not register_calls:
                await asyncio.sleep(0.005)
            # queue_to_serverは番兵(None)でのみEOFを検知するため、Eventだけで
            # なくキューにも積む
            state.outbound.put_nowait(None)
            state.stdin_eof.set()

        async def _drive() -> None:
            setter = asyncio.ensure_future(_set_eof_after_heartbeat())
            try:
                # ハングするバグがあればここでタイムアウトしテストが失敗する
                await asyncio.wait_for(launcher._bridge(state), timeout=3.0)
            finally:
                setter.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await setter

        asyncio.run(_drive())

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

    状態でも、STDIN_EOF_GRACE_SEC 経過後に強制的に退場することの検証。
    fixした対策が無ければ、read_streamが永遠に閉じないため_bridge()はハングし
    続ける（テストがタイムアウトで失敗する）。
    """

    def test_bridge_exits_after_grace_period_when_server_stays_silent(
        self, monkeypatch
    ):
        import time
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

        # stdin EOFを即座に確定させる（queue_to_serverは番兵(None)を
        # 素のQueue.get()で受け取って検知するため、_bridge呼び出し前に
        # キューへ積んでおく）
        state = launcher._StdinBridgeState()
        state.outbound.put_nowait(None)
        state.stdin_eof.set()

        start = time.monotonic()
        # 対策が無ければここでハングし、外側のwait_forタイムアウト(2.0s)で
        # 失敗する。対策が効いていればgrace(0.1s)経過後すぐ正常return する。
        asyncio.run(asyncio.wait_for(launcher._bridge(state), timeout=2.0))
        elapsed = time.monotonic() - start

        # grace期間(0.1s)経過後まもなく退場していること（2.0sタイムアウトに
        # 頼らずに済んでいること）を確認する
        assert elapsed < 1.0, f"grace timeoutが効いていない可能性: {elapsed:.2f}s"


class TestServerToStdoutConsecutiveExceptionCap:
    """server_to_stdout: read_streamから例外オブジェクトを

    MAX_CONSECUTIVE_STREAM_EXCEPTIONS 回連続で受け取った場合、無限にcontinueせず
    ServerDisconnectedへ倒して外側のリトライに接続することの検証。
    """

    def test_gives_up_after_max_consecutive_exceptions(self, monkeypatch):
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

        # stdinはまだ開いている状態を模す（stdin_eofを立てず、キューも空のまま）
        state = launcher._StdinBridgeState()

        with pytest.raises(BaseException) as excinfo:
            asyncio.run(asyncio.wait_for(launcher._bridge(state), timeout=2.0))
        assert _contains_server_disconnected(excinfo.value), (
            f"ServerDisconnectedへ倒れていない: {excinfo.value!r}"
        )


class TestServerDisconnected:
    def test_is_exception(self):
        """ServerDisconnectedがExceptionのサブクラスである"""
        assert issubclass(launcher.ServerDisconnected, Exception)

    def test_can_be_raised_and_caught(self):
        """ServerDisconnectedをraise/catchできる"""
        with pytest.raises(launcher.ServerDisconnected, match="test message"):
            raise launcher.ServerDisconnected("test message")


class TestDiscoverRequestHandledLocally:
    """_handle_stdin_line: Claude Codeがinitializeより前に送るバージョン交渉

    probe（server/discover）を、サーバー接続の有無に関わらずlauncher自身が
    即座にエラー応答することの検証。
    """

    def _fake_stdout(self, monkeypatch):
        import io
        import types

        buf = io.BytesIO()
        fake_stdout = types.SimpleNamespace(buffer=buf)
        monkeypatch.setattr(launcher.sys, "stdout", fake_stdout)
        return buf

    def test_discover_request_gets_method_not_found_without_server_connection(
        self, monkeypatch
    ):
        """discoverリクエスト(idあり)には、サーバーへ転送せず-32601
        (Method not found) がstdoutへ書かれ、outboundキューには何も積まれない。
        """
        buf = self._fake_stdout(monkeypatch)
        state = launcher._StdinBridgeState()

        launcher._handle_stdin_line(
            b'{"jsonrpc":"2.0","id":0,"method":"server/discover","params":{}}',
            state,
        )

        assert state.outbound.qsize() == 0
        response = json.loads(buf.getvalue().decode("utf-8").strip())
        assert response["id"] == 0
        assert response["error"]["code"] == -32601

    def test_discover_notification_without_id_is_dropped_silently(self, monkeypatch):
        """discoverが通知（idなし）として来た場合は応答せず捨てる。"""
        buf = self._fake_stdout(monkeypatch)
        state = launcher._StdinBridgeState()

        launcher._handle_stdin_line(
            b'{"jsonrpc":"2.0","method":"server/discover","params":{}}',
            state,
        )

        assert state.outbound.qsize() == 0
        assert buf.getvalue() == b""


class TestOutboundQueuePersistsAcrossBridgeFailure:
    """stdinの共有outboundキューは_bridgeの呼び出しを跨いで生き続け、

    1回目のbridge失敗時にサーバーへ転送できなかったメッセージが、2回目の
    bridgeで転送されることの検証。
    """

    def test_message_arriving_during_outage_is_forwarded_by_next_bridge(
        self, monkeypatch
    ):
        from contextlib import asynccontextmanager

        import anyio
        import mcp.client.streamable_http as streamable_http_module

        state = launcher._StdinBridgeState()

        class _Abort(Exception):
            """接続確立前にbridgeを打ち切るためのセンチネル例外"""

        @asynccontextmanager
        async def failing_client(**kwargs):
            raise _Abort()
            yield  # pragma: no cover

        monkeypatch.setattr(
            streamable_http_module, "streamable_http_client", failing_client
        )

        with pytest.raises(_Abort):
            asyncio.run(launcher._bridge(state))

        # bridgeが落ちている間にstdinへ届いたメッセージとして、キューへ積む
        launcher._handle_stdin_line(
            b'{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{}}',
            state,
        )
        assert state.outbound.qsize() == 1

        forwarded: list[dict] = []

        @asynccontextmanager
        async def succeeding_client(**kwargs):
            read_send, read_recv = anyio.create_memory_object_stream(10)
            write_send, write_recv = anyio.create_memory_object_stream(10)

            async def _get_session_id():
                return None

            async def _capture_writes() -> None:
                try:
                    async for session_msg in write_recv:
                        forwarded.append(session_msg.message.model_dump(mode="json"))
                finally:
                    await read_send.aclose()

            async with anyio.create_task_group() as watcher_tg:
                watcher_tg.start_soon(_capture_writes)
                try:
                    yield (read_recv, write_send, _get_session_id)
                finally:
                    await write_recv.aclose()
                    await read_recv.aclose()

        monkeypatch.setattr(
            streamable_http_module, "streamable_http_client", succeeding_client
        )
        monkeypatch.setattr(launcher, "HEARTBEAT_INTERVAL_SEC", 1000.0)

        async def _finish_once_forwarded() -> None:
            while not forwarded:
                await asyncio.sleep(0.005)
            state.outbound.put_nowait(None)
            state.stdin_eof.set()

        async def _drive() -> None:
            finisher = asyncio.ensure_future(_finish_once_forwarded())
            try:
                await asyncio.wait_for(launcher._bridge(state), timeout=3.0)
            finally:
                finisher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await finisher

        asyncio.run(_drive())

        assert len(forwarded) == 1
        assert forwarded[0]["id"] == 7


class TestPendingRequestFailsOnBridgeDisconnect:
    """bridgeがサーバー切断で失敗したとき、送信済みで応答の無かった

    リクエストidにJSON-RPCエラーが返ることの検証。
    """

    def test_in_flight_request_gets_connection_lost_error_on_bridge_failure(
        self, monkeypatch
    ):
        import io
        import types
        from contextlib import asynccontextmanager

        import anyio
        import mcp.client.streamable_http as streamable_http_module

        state = launcher._StdinBridgeState()
        launcher._handle_stdin_line(
            b'{"jsonrpc":"2.0","id":42,"method":"tools/call","params":{}}',
            state,
        )

        @asynccontextmanager
        async def dying_client(**kwargs):
            read_send, read_recv = anyio.create_memory_object_stream(10)
            write_send, write_recv = anyio.create_memory_object_stream(10)

            async def _get_session_id():
                return None

            async def _kill_after_send() -> None:
                # 送信を1件確認したらread_stream側を閉じ、サーバーが応答せず
                # 切断する状況を模す
                async for _ in write_recv:
                    await read_send.aclose()
                    break

            async with anyio.create_task_group() as watcher_tg:
                watcher_tg.start_soon(_kill_after_send)
                try:
                    yield (read_recv, write_send, _get_session_id)
                finally:
                    await write_recv.aclose()
                    await read_recv.aclose()

        monkeypatch.setattr(
            streamable_http_module, "streamable_http_client", dying_client
        )
        monkeypatch.setattr(launcher, "HEARTBEAT_INTERVAL_SEC", 1000.0)
        monkeypatch.setattr(launcher, "STDIN_EOF_GRACE_SEC", 1000.0)

        with pytest.raises(BaseException) as excinfo:
            asyncio.run(asyncio.wait_for(launcher._bridge(state), timeout=2.0))
        assert _contains_server_disconnected(excinfo.value)
        assert 42 in state.pending_ids

        buf = io.BytesIO()
        fake_stdout = types.SimpleNamespace(buffer=buf)
        monkeypatch.setattr(launcher.sys, "stdout", fake_stdout)

        launcher._fail_pending_requests(state, "CALM server connection lost")

        assert state.pending_ids == set()
        response = json.loads(buf.getvalue().decode("utf-8").strip())
        assert response["id"] == 42
        assert response["error"]["code"] == -32603
        assert "CALM server connection lost" in response["error"]["message"]


class TestPendingIdsDistinguishMessageDirection:
    """pending_idsへの追加・削除が、メッセージの向き（クライアント→サーバーの

    Requestか、サーバー→クライアントのResponse/Errorか）を区別することの検証。
    idの有無だけで判定する実装（`_message_id`ベース）に戻すと、stdin側から
    紛れ込んだResponse形のメッセージが誤って追跡対象になったり、サーバーから
    来たRequest形のメッセージで正規のpendingが誤って消えたりする。
    """

    def test_error_response_targets_only_the_genuine_pending_request(self, monkeypatch):
        import io
        import types
        from contextlib import asynccontextmanager

        import anyio
        import mcp.client.streamable_http as streamable_http_module
        from mcp import types as mcp_types
        from mcp.shared.message import SessionMessage

        state = launcher._StdinBridgeState()
        # 正規のクライアント→サーバーのリクエスト(id=1)
        launcher._handle_stdin_line(
            b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{}}', state
        )
        # stdin側から紛れ込んだResponse形のメッセージ(id=99)。Requestではない
        # ため、pending_idsには追加されないはず
        launcher._handle_stdin_line(
            b'{"jsonrpc":"2.0","id":99,"result":{}}', state
        )

        @asynccontextmanager
        async def fake_streamable_http_client(**kwargs):
            read_send, read_recv = anyio.create_memory_object_stream(10)
            write_send, write_recv = anyio.create_memory_object_stream(10)

            async def _get_session_id():
                return None

            async def _echo_request_with_same_id() -> None:
                # サーバー側から、pendingにあるid=1と同じidのJSONRPCRequestを
                # 送る。Response/Errorではないため、これを受け取ってもpending_ids
                # からid=1が消えないはず
                async for _ in write_recv:
                    bogus_request = mcp_types.JSONRPCMessage(
                        root=mcp_types.JSONRPCRequest(
                            jsonrpc="2.0", id=1, method="server/bogus", params={}
                        )
                    )
                    await read_send.send(SessionMessage(bogus_request))
                await read_send.aclose()

            async with anyio.create_task_group() as watcher_tg:
                watcher_tg.start_soon(_echo_request_with_same_id)
                try:
                    yield (read_recv, write_send, _get_session_id)
                finally:
                    await write_recv.aclose()
                    await read_recv.aclose()

        monkeypatch.setattr(
            streamable_http_module, "streamable_http_client", fake_streamable_http_client
        )
        monkeypatch.setattr(launcher, "HEARTBEAT_INTERVAL_SEC", 1000.0)
        monkeypatch.setattr(launcher, "STDIN_EOF_GRACE_SEC", 1000.0)

        with pytest.raises(BaseException) as excinfo:
            asyncio.run(asyncio.wait_for(launcher._bridge(state), timeout=2.0))
        assert _contains_server_disconnected(excinfo.value)

        # id=1はまだpendingのまま（サーバーから来たRequest形では消えない）
        assert 1 in state.pending_ids
        # id=99はそもそもRequestではないためpendingに載っていない
        assert 99 not in state.pending_ids

        out = io.BytesIO()
        monkeypatch.setattr(launcher.sys, "stdout", types.SimpleNamespace(buffer=out))
        launcher._fail_pending_requests(state, "CALM server connection lost")

        responses = [
            json.loads(line)
            for line in out.getvalue().decode("utf-8").splitlines()
            if line.strip()
        ]
        # エラー応答はid=1にだけ返り、id=99には返らない
        assert [r["id"] for r in responses] == [1]
        assert responses[0]["error"]["code"] == -32603


class TestRetryLoopCancellationWhileBridgeConnected:
    """接続中のbridgeを外側からcancelしたとき、再接続せずキャンセルとして

    終わることの検証。`server_to_stdout`のfinally節は、stdin_eofがFalseの
    まま中断されるとServerDisconnected（Exceptionのサブクラス）を送出するため、
    対策が無いと外部からのcancel（CancelledError、BaseExceptionのサブクラス）が
    リトライループの`except Exception`に「ただのbridge失敗」として吸収され、
    再接続してしまう。
    """

    def test_cancel_ends_the_loop_without_reconnecting(self, monkeypatch):
        from contextlib import asynccontextmanager

        import anyio
        import mcp.client.streamable_http as streamable_http_module

        monkeypatch.setattr(launcher, "_ensure_server_running", lambda: True)
        monkeypatch.setattr(launcher, "_register_session", lambda: True)
        monkeypatch.setattr(launcher, "_unregister_session", lambda: True)
        monkeypatch.setattr(launcher, "_IS_LOCAL", True)
        monkeypatch.setattr(launcher, "MAX_RETRIES", None)
        monkeypatch.setattr(launcher, "HEARTBEAT_INTERVAL_SEC", 1000.0)

        connect_count = {"n": 0}
        connected = {}

        @asynccontextmanager
        async def fake_streamable_http_client(**kwargs):
            connect_count["n"] += 1
            connected.setdefault("event", asyncio.Event())
            connected["event"].set()
            # read_stream には何も流さない（接続を維持したまま応答待ちにする）
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

        async def fake_stdin_reader_task(state):
            # 実stdinに触れず、キャンセルされるまで待つだけ
            await asyncio.Event().wait()

        monkeypatch.setattr(launcher, "_stdin_reader_task", fake_stdin_reader_task)

        async def drive():
            task = asyncio.ensure_future(launcher._run_retry_loop())

            deadline = asyncio.get_running_loop().time() + 3.0
            while "event" not in connected and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
            assert "event" in connected, "bridge接続が確立しなかった"
            await connected["event"].wait()

            task.cancel()

            # ここでは意図的にawait taskしない。対策が無いとcancelがリトライへ
            # 吸収され、taskが自発的には終わらないため、awaitすると
            # ハングしうる。自然完了をポーリングで待つだけにする。
            deadline2 = asyncio.get_running_loop().time() + 3.0
            while not task.done() and asyncio.get_running_loop().time() < deadline2:
                await asyncio.sleep(0.01)

            if not task.done():
                raise AssertionError(
                    "cancel後3秒以内にリトライループが終了しなかった"
                    "（cancelがリトライに吸収された可能性）"
                )
            if not task.cancelled():
                exc = task.exception()
                raise AssertionError(
                    f"CancelledErrorではなく{exc!r}で終了した"
                    "（cancelがリトライに吸収された可能性）"
                )
            return connect_count["n"]

        loop = asyncio.new_event_loop()
        try:
            attempts = loop.run_until_complete(drive())
        finally:
            loop.close()

        # cancel後に再接続していないこと（接続は最初の1回だけ）
        assert attempts == 1


class TestStdinReaderTaskFailureHandling:
    """_stdin_reader_task: 読み取りスレッドの開始・継続の失敗時も、

    stdin EOFと同じ終了経路（WARNINGログ・state.outbound への番兵・
    state.stdin_eof）に必ず乗ることの検証。
    """

    def test_fileno_failure_logs_warning_and_reaches_eof(self, monkeypatch):
        """stdin.buffer.fileno()自体が失敗する（読み取り開始の失敗）場合"""
        import types

        broken_buffer = types.SimpleNamespace(
            fileno=lambda: (_ for _ in ()).throw(OSError("no fd"))
        )
        monkeypatch.setattr(
            launcher.sys, "stdin", types.SimpleNamespace(buffer=broken_buffer)
        )
        warnings = []
        monkeypatch.setattr(
            launcher.logger,
            "warning",
            lambda msg, *a, **kw: warnings.append(msg),
        )

        async def drive():
            state = launcher._StdinBridgeState()
            await asyncio.wait_for(launcher._stdin_reader_task(state), timeout=5.0)
            return state

        state = asyncio.run(drive())
        assert state.stdin_eof.is_set()
        assert state.outbound.get_nowait() is None
        assert "Failed to read stdin" in warnings

    def test_os_read_failure_logs_warning_and_reaches_eof(self, monkeypatch):
        """fdは取れるがos.readが失敗する（読み取り継続の失敗）場合"""
        import types

        monkeypatch.setattr(
            launcher.sys, "stdin", types.SimpleNamespace(buffer=types.SimpleNamespace(fileno=lambda: 999))
        )

        def failing_read(fd, n):
            raise OSError("bad fd")

        monkeypatch.setattr(launcher.os, "read", failing_read)
        warnings = []
        monkeypatch.setattr(
            launcher.logger,
            "warning",
            lambda msg, *a, **kw: warnings.append(msg),
        )

        async def drive():
            state = launcher._StdinBridgeState()
            await asyncio.wait_for(launcher._stdin_reader_task(state), timeout=5.0)
            return state

        state = asyncio.run(drive())
        assert state.stdin_eof.is_set()
        assert state.outbound.get_nowait() is None
        assert "Failed to read stdin" in warnings

    def test_thread_start_failure_logs_warning_and_reaches_eof(self, monkeypatch):
        """threading.Thread(...).start()自体が失敗する（スレッド生成の失敗）場合"""
        import types

        class _FailingThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("can't start new thread")

        # launcher.threading（モジュール参照）だけを差し替える。グローバルな
        # threading.Threadを差し替えるとasyncio.to_threadの内部
        # ThreadPoolExecutorの動作まで巻き込んでしまう。
        monkeypatch.setattr(launcher, "threading", types.SimpleNamespace(Thread=_FailingThread))
        warnings = []
        monkeypatch.setattr(
            launcher.logger,
            "warning",
            lambda msg, *a, **kw: warnings.append(msg),
        )

        async def drive():
            state = launcher._StdinBridgeState()
            await asyncio.wait_for(launcher._stdin_reader_task(state), timeout=5.0)
            return state

        state = asyncio.run(drive())
        assert state.stdin_eof.is_set()
        assert state.outbound.get_nowait() is None
        assert "stdin reader ended unexpectedly" in warnings

    def test_handle_stdin_line_failure_does_not_crash_the_task(self, monkeypatch):
        """_handle_stdin_line呼び出し先（discover応答のstdout書き込み等）が

        失敗しても、タスク自体は例外を外へ伝播させずEOF終了経路に乗ること
        （ここで吸収しないと_run_retry_loopのCancelledErrorだけをsuppressする
        後始末をすり抜けてプロセスがクラッシュする）。
        """
        import types

        read_fd, write_fd = os.pipe()
        read_file = os.fdopen(read_fd, "rb", buffering=0)
        monkeypatch.setattr(
            launcher.sys, "stdin", types.SimpleNamespace(buffer=read_file)
        )

        def failing_write(data):
            raise BrokenPipeError("broken")

        monkeypatch.setattr(
            launcher.sys,
            "stdout",
            types.SimpleNamespace(
                buffer=types.SimpleNamespace(write=failing_write, flush=lambda: None)
            ),
        )
        warnings = []
        monkeypatch.setattr(
            launcher.logger,
            "warning",
            lambda msg, *a, **kw: warnings.append(msg),
        )

        os.write(
            write_fd,
            b'{"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}}\n',
        )
        os.close(write_fd)

        async def drive():
            state = launcher._StdinBridgeState()
            await asyncio.wait_for(launcher._stdin_reader_task(state), timeout=5.0)
            return state

        threads_before = set(threading.enumerate())
        try:
            state = asyncio.run(drive())
        finally:
            # 読み取りスレッドが完全に終わってからfdを閉じる。スレッドが
            # 生きたままfdを閉じると、別テストのos.pipe()がfd番号を再利用した
            # ときにこのスレッドがそちらを読んでしまい、まれに関係ないテストを
            # タイムアウトさせる。
            for t in set(threading.enumerate()) - threads_before:
                t.join(timeout=5.0)
            read_file.close()

        assert state.stdin_eof.is_set()
        assert "stdin reader ended unexpectedly" in warnings


class TestReadStdinChunkWindowsPipe:
    """_read_stdin_chunk_windows_pipe: PeekNamedPipeとos.readの偽物で駆動する。

    実際のWindows API（ctypes.WinDLL）には触れず、`peek`引数に渡す関数と
    `launcher.os.read`だけを差し替えて検証する。
    """

    def test_polls_while_zero_then_reads_once_available(self, monkeypatch):
        """読める量が0の間はos.readを呼ばず、正になったら1回だけ読む"""
        available_sequence = iter([0, 0, 5])
        peek_calls = []

        def fake_peek():
            peek_calls.append(True)
            return next(available_sequence)

        sleeps = []
        monkeypatch.setattr(launcher.time, "sleep", lambda s: sleeps.append(s))

        read_calls = []

        def fake_read(fd, n):
            read_calls.append((fd, n))
            return b"hello"

        monkeypatch.setattr(launcher.os, "read", fake_read)

        result = launcher._read_stdin_chunk_windows_pipe(999, fake_peek)

        assert result == b"hello"
        assert len(peek_calls) == 3
        assert read_calls == [(999, 5)]
        assert sleeps == [
            launcher._WINDOWS_PIPE_POLL_INTERVAL_SEC,
            launcher._WINDOWS_PIPE_POLL_INTERVAL_SEC,
        ]

    def test_broken_pipe_returns_eof_without_warning(self, monkeypatch):
        """ERROR_BROKEN_PIPE(109)はEOFとして扱い、WARNINGは出さない"""

        def fake_peek():
            raise launcher._PeekNamedPipeFailed(launcher._ERROR_BROKEN_PIPE)

        warnings = []
        monkeypatch.setattr(
            launcher.logger, "warning", lambda msg, *a, **kw: warnings.append(msg)
        )
        read_calls = []
        monkeypatch.setattr(
            launcher.os, "read", lambda fd, n: read_calls.append((fd, n))
        )

        result = launcher._read_stdin_chunk_windows_pipe(999, fake_peek)

        assert result == b""
        assert read_calls == []
        assert warnings == []

    def test_other_failure_logs_warning_and_returns_eof(self, monkeypatch):
        """ERROR_BROKEN_PIPE以外の失敗はWARNINGを出したうえでEOF扱いにする"""

        def fake_peek():
            raise launcher._PeekNamedPipeFailed(5)

        warnings = []
        monkeypatch.setattr(
            launcher.logger, "warning", lambda msg, *a, **kw: warnings.append(msg)
        )
        read_calls = []
        monkeypatch.setattr(
            launcher.os, "read", lambda fd, n: read_calls.append((fd, n))
        )

        result = launcher._read_stdin_chunk_windows_pipe(999, fake_peek)

        assert result == b""
        assert read_calls == []
        assert len(warnings) == 1
        assert "winerror=5" in warnings[0]


class TestStdinChunkReaderSelection:
    """_stdin_chunk_reader: プラットフォーム・stdinの種別に応じた読み方の選択。"""

    def test_non_windows_uses_blocking_read(self, monkeypatch):
        """Windows以外は常にブロッキングのos.readを選ぶ（_win_stdin_pipe_apiは呼ばない）"""
        monkeypatch.setattr(launcher.sys, "platform", "darwin")
        monkeypatch.setattr(
            launcher,
            "_win_stdin_pipe_api",
            lambda fd: (_ for _ in ()).throw(AssertionError("should not be called")),
        )
        read_calls = []
        monkeypatch.setattr(
            launcher.os, "read", lambda fd, n: read_calls.append((fd, n)) or b"x"
        )

        read_chunk = launcher._stdin_chunk_reader(42)
        assert read_chunk() == b"x"
        assert read_calls == [(42, 65536)]

    def test_windows_non_pipe_uses_blocking_read(self, monkeypatch):
        """Windowsでもstdinがパイプでない場合（ファイルリダイレクト等）はブロッキング読み取り"""
        monkeypatch.setattr(launcher.sys, "platform", "win32")

        def fake_peek():
            raise AssertionError("peek should not be called when stdin is not a pipe")

        monkeypatch.setattr(
            launcher, "_win_stdin_pipe_api", lambda fd: (1, fake_peek)
        )
        read_calls = []
        monkeypatch.setattr(
            launcher.os, "read", lambda fd, n: read_calls.append((fd, n)) or b"x"
        )

        read_chunk = launcher._stdin_chunk_reader(42)
        assert read_chunk() == b"x"
        assert read_calls == [(42, 65536)]

    def test_windows_pipe_uses_peek_reader(self, monkeypatch):
        """Windowsでstdinが無名パイプの場合はPeekNamedPipe方式を選ぶ"""
        monkeypatch.setattr(launcher.sys, "platform", "win32")

        peek_calls = []

        def fake_peek():
            peek_calls.append(True)
            return 3

        monkeypatch.setattr(
            launcher, "_win_stdin_pipe_api", lambda fd: (launcher._FILE_TYPE_PIPE, fake_peek)
        )
        read_calls = []
        monkeypatch.setattr(
            launcher.os, "read", lambda fd, n: read_calls.append((fd, n)) or b"abc"
        )

        read_chunk = launcher._stdin_chunk_reader(42)
        assert read_chunk() == b"abc"
        assert peek_calls == [True]
        assert read_calls == [(42, 3)]


class TestStdinReaderThreadSurvivesClosedLoop:
    """読み取りスレッドが、イベントループが閉じた後にfeed_data/feed_eofを

    呼んでも（`contextlib.suppress(RuntimeError)`により）クラッシュしないこと、
    誤った「Failed to read stdin」WARNINGを出さないことの検証。max retries超過や
    sys.exit経由でのキャンセル後も読み取りスレッドだけが生き残る状況を再現する。
    """

    def test_feed_after_loop_closed_does_not_crash_thread_or_warn(self, monkeypatch):
        import types

        unhandled_exceptions = []
        original_excepthook = threading.excepthook
        monkeypatch.setattr(
            threading,
            "excepthook",
            lambda args: unhandled_exceptions.append(args.exc_value),
        )

        read_fd, write_fd = os.pipe()
        read_file = os.fdopen(read_fd, "rb", buffering=0)
        monkeypatch.setattr(
            launcher.sys, "stdin", types.SimpleNamespace(buffer=read_file)
        )
        warnings = []
        monkeypatch.setattr(
            launcher.logger,
            "warning",
            lambda msg, *a, **kw: warnings.append(msg),
        )

        threads_before = set(threading.enumerate())

        async def drive():
            state = launcher._StdinBridgeState()
            task = asyncio.ensure_future(launcher._stdin_reader_task(state))
            # 読み取りスレッドがos.readでブロック中（まだ何も書いていない）の
            # 状態でタスクをキャンセルする。max retries超過やsys.exit経路での
            # 強制終了を模す。
            await asyncio.sleep(0.2)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        try:
            asyncio.run(drive())
            # ここでイベントループは既に閉じている。読み取りスレッドは
            # os.readでブロックしたまま生き残っているはず。
            os.write(write_fd, b"irrelevant\n")
            os.close(write_fd)

            for t in set(threading.enumerate()) - threads_before:
                t.join(timeout=5.0)
        finally:
            monkeypatch.setattr(threading, "excepthook", original_excepthook)
            read_file.close()

        assert unhandled_exceptions == []
        assert "Failed to read stdin" not in warnings


class TestStdinAndRetryLoopIntegration:
    """実stdin（os.pipe経由）・実`_stdin_reader_task`・実`_run_retry_loop`を使い、

    HTTP層（`streamable_http_client`）だけをfakeにした統合寄りの検証。
    stdinを丸ごとスタブに差し替える単体テストでは、(1)stdinの読み取りスレッドを
    1回だけ作って使い回すこと (2)リトライループが`_fail_pending_requests`を
    呼ぶこと (3)readerのEOF通知、のいずれを壊しても検出できない。このテストは
    それら3つの配線を一括で保証する。
    """

    @pytest.mark.timeout(20)
    def test_disconnect_reconnect_and_eof_over_real_stdin_pipe(self, monkeypatch):
        import io
        import types as std_types
        from contextlib import asynccontextmanager

        import anyio
        import mcp.client.streamable_http as streamable_http_module
        from mcp import types
        from mcp.shared.message import SessionMessage

        # --- stdin: 実パイプ ---
        # write_fdは正常系ではdrive()の途中で閉じるが、途中のassertで
        # テストが失敗した場合にも両方のfdを確実に閉じるため、後始末は
        # 最後のtry/finallyに一本化する（close_write_fdは多重close安全）。
        read_fd, write_fd = os.pipe()
        read_file = os.fdopen(read_fd, "rb", buffering=0)
        write_fd_closed = {"done": False}

        def close_write_fd() -> None:
            if not write_fd_closed["done"]:
                write_fd_closed["done"] = True
                os.close(write_fd)

        monkeypatch.setattr(
            launcher.sys, "stdin", std_types.SimpleNamespace(buffer=read_file)
        )

        # --- stdout: キャプチャ ---
        out_buf = io.BytesIO()
        monkeypatch.setattr(
            launcher.sys, "stdout", std_types.SimpleNamespace(buffer=out_buf)
        )

        def stdout_messages():
            text = out_buf.getvalue().decode("utf-8")
            return [json.loads(line) for line in text.splitlines() if line.strip()]

        # サーバー起動確認・セッション登録は外部境界としてモックする
        monkeypatch.setattr(launcher, "_ensure_server_running", lambda: True)
        monkeypatch.setattr(launcher, "_register_session", lambda: True)
        monkeypatch.setattr(launcher, "_unregister_session", lambda: True)
        monkeypatch.setattr(launcher, "_IS_LOCAL", True)
        monkeypatch.setattr(launcher, "MAX_RETRIES", None)
        monkeypatch.setattr(launcher, "HEARTBEAT_INTERVAL_SEC", 1000.0)

        # 読み取りスレッドがbridgeのリトライを跨いで1本だけ使い回されることを
        # 検証するため、Thread生成回数を数える。launcher.threading（モジュール
        # 参照）だけを差し替える。グローバルなthreading.Threadを差し替えると
        # asyncio.to_threadの内部ThreadPoolExecutorの動作まで巻き込んでしまう。
        thread_create_count = {"n": 0}

        def counting_thread(*args, **kwargs):
            thread_create_count["n"] += 1
            return threading.Thread(*args, **kwargs)

        monkeypatch.setattr(launcher, "threading", std_types.SimpleNamespace(Thread=counting_thread))

        call_count = {"n": 0}

        @asynccontextmanager
        async def fake_streamable_http_client(**kwargs):
            call_count["n"] += 1
            call_no = call_count["n"]
            read_send, read_recv = anyio.create_memory_object_stream(10)
            write_send, write_recv = anyio.create_memory_object_stream(10)

            async def _get_session_id():
                return None

            async def _serve_one_then_disconnect() -> None:
                # 1件受け取ったら切断する。1回目は応答せずに切断（サーバー側
                # 切断を模す）、2回目以降は応答してから切断する
                # （応答後に接続が切れても、応答済みのidへ二重にエラーが出ない
                # ことを検証するため）。
                async for session_msg in write_recv:
                    if call_no >= 2:
                        req = session_msg.message.root
                        resp = types.JSONRPCMessage(
                            root=types.JSONRPCResponse(
                                jsonrpc="2.0", id=req.id, result={}
                            )
                        )
                        await read_send.send(SessionMessage(resp))
                    await read_send.aclose()
                    return

            async with anyio.create_task_group() as watcher_tg:
                watcher_tg.start_soon(_serve_one_then_disconnect)
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

        # 「2回目のbridgeが失敗し、バックオフ待ちに入った」ことを、内部stateを
        # 直接覗かずに観測するため、リトライループ自身が失敗のたびに出す
        # "Bridge failed"ログの回数を数える。バックオフ中にEOFを発生させたい
        # テストの意図を、タイミングの偶然ではなく確実に満たすための同期点。
        bridge_failure_count = {"n": 0}
        original_warning = launcher.logger.warning

        def counting_warning(msg, *args, **kwargs):
            if isinstance(msg, str) and msg.startswith("Bridge failed"):
                bridge_failure_count["n"] += 1
            return original_warning(msg, *args, **kwargs)

        monkeypatch.setattr(launcher.logger, "warning", counting_warning)

        def write_line(obj: dict) -> None:
            os.write(write_fd, (json.dumps(obj) + "\n").encode("utf-8"))

        async def wait_until(predicate, timeout: float) -> None:
            deadline = asyncio.get_running_loop().time() + timeout
            while asyncio.get_running_loop().time() < deadline:
                if predicate():
                    return
                await asyncio.sleep(0.02)
            raise AssertionError("timed out waiting for condition")

        async def drive() -> float:
            import time as time_module

            loop_task = asyncio.ensure_future(launcher._run_retry_loop())

            # discoverはサーバー接続が無くても即座に-32601が返るはず
            write_line({"jsonrpc": "2.0", "id": 0, "method": "server/discover", "params": {}})
            await wait_until(
                lambda: any(m.get("id") == 0 for m in stdout_messages()), timeout=5.0
            )

            # 1回目のbridgeで送信されるが応答が来ず切断される
            write_line({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}})
            await wait_until(
                lambda: any(
                    m.get("id") == 1 and "error" in m for m in stdout_messages()
                ),
                timeout=5.0,
            )

            # 1回目の失敗後（2回目のbridge接続が確立する前）にstdinへ届いた行
            write_line({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {}})
            await wait_until(
                lambda: any(
                    m.get("id") == 2 and "result" in m for m in stdout_messages()
                ),
                timeout=5.0,
            )

            # id=2は応答後に接続が切れて2回目のbridgeも失敗扱いになる。
            # タイミングの偶然に頼らず、2回目の失敗（バックオフ待ちへの突入）を
            # ログで確認してからstdin EOFを発生させ、
            # 「バックオフとEOFの早い方で抜ける」（backoffを待ち切らない）ことを計測する。
            await wait_until(lambda: bridge_failure_count["n"] >= 2, timeout=5.0)
            # 2回のbridge失敗（＝2回の再接続）を経ても、読み取りスレッドは
            # 最初の1回しか作られていないこと。
            assert thread_create_count["n"] == 1
            eof_start = time_module.monotonic()
            close_write_fd()
            await asyncio.wait_for(loop_task, timeout=5.0)
            return time_module.monotonic() - eof_start

        # fdの後始末は、途中のassert失敗時にも漏れないようfinallyで行う。
        try:
            elapsed_after_eof = asyncio.run(drive())
        finally:
            close_write_fd()
            read_file.close()

        messages = stdout_messages()

        discover_resp = next(m for m in messages if m.get("id") == 0)
        assert discover_resp["error"]["code"] == -32601

        id1_resp = next(m for m in messages if m.get("id") == 1)
        assert id1_resp["error"]["code"] == -32603

        id2_responses = [m for m in messages if m.get("id") == 2]
        # id=2は応答済みのため、その後bridgeが失敗扱いになっても二重にエラーが
        # 出ていないこと（成功応答1件だけであること）
        assert len(id2_responses) == 1
        assert "result" in id2_responses[0]

        # 2回目の失敗後のバックオフ（4秒）を待ち切らず、stdin EOFで即座に
        # ループを抜けたこと（対策が無ければここが約4秒に張り付く）
        assert elapsed_after_eof < 2.0, (
            f"backoffがEOFで打ち切られていない可能性: {elapsed_after_eof:.2f}s"
        )


class TestMainRetryLoop:
    """main()のリトライループの動作検証"""

    def _setup_main(self, monkeypatch, bridge_side_effects, max_retries=3):
        """main()テスト用の共通セットアップ

        bridge_side_effectsには_bridge(state)呼び出しの戻り値/例外のリストを渡す。
        max_retries で MAX_RETRIES を明示的に上書きする（None で無限）。
        main()は1回のasyncio.runでリトライ全体を回すため、_bridge自体とstdinを
        永続的に読み続ける_stdin_reader_taskをそれぞれ差し替える
        （後者は実stdinに触れず、キャンセルされるまで待つだけにする）。
        """
        monkeypatch.setattr(launcher, "MAX_RETRIES", max_retries)
        monkeypatch.setattr(launcher, "_IS_LOCAL", True)
        monkeypatch.setattr(launcher, "_cleanup_done", False)
        monkeypatch.setattr(launcher, "_ensure_server_running", lambda: True)
        monkeypatch.setattr(launcher, "_register_session", lambda: True)
        monkeypatch.setattr(launcher, "_unregister_session", lambda: True)
        monkeypatch.setattr(launcher, "register_launcher_session", lambda *a, **kw: None)
        monkeypatch.setattr(launcher, "unregister_launcher_session", lambda *a, **kw: None)

        async def no_op_wait_for(aw, timeout=None):
            # バックオフは`asyncio.wait_for(state.stdin_eof.wait(), timeout=...)`で
            # 行われる。stdin_eofが立たない（＝実際には待ち切る）状況を、
            # 実時間を待たずに再現するため即座にTimeoutErrorを送出する。
            if hasattr(aw, "close"):
                aw.close()
            raise asyncio.TimeoutError()

        monkeypatch.setattr(launcher.asyncio, "wait_for", no_op_wait_for)

        call_count = {"bridge": 0}

        async def fake_bridge(state):
            idx = call_count["bridge"]
            call_count["bridge"] += 1
            effect = bridge_side_effects[idx]
            if isinstance(effect, Exception):
                raise effect
            return effect

        monkeypatch.setattr(launcher, "_bridge", fake_bridge)

        async def fake_stdin_reader_task(state):
            await asyncio.Event().wait()

        monkeypatch.setattr(launcher, "_stdin_reader_task", fake_stdin_reader_task)
        return call_count

    def _track_sleep(self, monkeypatch):
        """バックオフ長さ記録用の共通セットアップ

        テスト本体のスレッドから呼ばれた分だけを記録するリストを返す。
        バックオフは`asyncio.wait_for(state.stdin_eof.wait(), timeout=...)`で
        行われるため、`launcher.asyncio.wait_for`のtimeout引数を記録し、
        実時間を待たずに即座にTimeoutErrorを送出する。
        """
        sleep_values = []
        test_thread_id = threading.get_ident()

        async def tracking_wait_for(aw, timeout=None):
            # ほかのテストのスレッドの呼び出しを拾わないため
            if threading.get_ident() == test_thread_id:
                sleep_values.append(timeout)
            if hasattr(aw, "close"):
                aw.close()
            raise asyncio.TimeoutError()

        monkeypatch.setattr(launcher.asyncio, "wait_for", tracking_wait_for)
        return sleep_values

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
        self._setup_main(monkeypatch, [
            launcher.ServerDisconnected("lost"),
            launcher.ServerDisconnected("lost"),
            launcher.ServerDisconnected("lost"),
            launcher.ServerDisconnected("lost"),
        ])
        # _setup_mainのsleep上書きの後にtracking_sleepで再上書き
        sleep_values = self._track_sleep(monkeypatch)
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

        async def fake_bridge(state):
            return None

        monkeypatch.setattr(launcher, "_bridge", fake_bridge)

        async def fake_stdin_reader_task(state):
            await asyncio.Event().wait()

        monkeypatch.setattr(launcher, "_stdin_reader_task", fake_stdin_reader_task)
        launcher.main()
        assert cleanup_count["calls"] == 1

    def test_backoff_capped_at_60_seconds(self, monkeypatch):
        """backoff は BACKOFF_CAP_SEC (60秒) で頭打ちになる"""
        # attempt 0..7 で失敗させる（max_retries=8 で 8 回 sleep が発生）
        # 期待: 2, 4, 8, 16, 32, 60, 60, 60
        self._setup_main(
            monkeypatch,
            [launcher.ServerDisconnected("lost")] * 9,
            max_retries=8,
        )
        sleep_values = self._track_sleep(monkeypatch)
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

    def test_registers_sigbreak_alongside_sigterm_when_present(self, monkeypatch):
        """SIGBREAK（Windows専用、存在する場合のみ）をSIGTERMと同じハンドラで登録する"""
        self._setup_main(monkeypatch, [None])
        monkeypatch.setattr(launcher.signal, "SIGBREAK", 99, raising=False)
        registered: dict = {}
        monkeypatch.setattr(
            launcher.signal,
            "signal",
            lambda sig, handler: registered.setdefault(sig, handler),
        )
        launcher.main()
        assert launcher.signal.SIGTERM in registered
        assert 99 in registered
        assert registered[99] is registered[launcher.signal.SIGTERM]


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

        async def fake_bridge(state):
            return None

        monkeypatch.setattr(launcher, "_bridge", fake_bridge)

        async def fake_stdin_reader_task(state):
            await asyncio.Event().wait()

        monkeypatch.setattr(launcher, "_stdin_reader_task", fake_stdin_reader_task)

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

        async def fake_bridge(state):
            return None

        monkeypatch.setattr(launcher, "_bridge", fake_bridge)

        async def fake_stdin_reader_task(state):
            await asyncio.Event().wait()

        monkeypatch.setattr(launcher, "_stdin_reader_task", fake_stdin_reader_task)
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

        async def fake_bridge(state):
            return None

        monkeypatch.setattr(launcher, "_bridge", fake_bridge)

        async def fake_stdin_reader_task(state):
            await asyncio.Event().wait()

        monkeypatch.setattr(launcher, "_stdin_reader_task", fake_stdin_reader_task)
        launcher.main()
        assert order == ["register_launcher_session", "_ensure_server_running"]


class TestLauncherSessionRegistrationUnconditional:
    """main(): register_launcher_session は常に（無条件で）呼ばれる
    （セッション別名解決の入力として登録ファイルが要るため）。
    """

    def _setup_common(self, monkeypatch):
        monkeypatch.setattr(launcher, "MAX_RETRIES", 0)
        monkeypatch.setattr(launcher, "_IS_LOCAL", True)
        monkeypatch.setattr(launcher, "_cleanup_done", False)
        monkeypatch.setattr(launcher, "_ensure_server_running", lambda: True)
        monkeypatch.setattr(launcher, "_register_session", lambda: True)
        monkeypatch.setattr(launcher, "_unregister_session", lambda: True)
        monkeypatch.setattr(launcher, "unregister_launcher_session", lambda *a, **kw: None)

        async def fake_bridge(state):
            return None

        monkeypatch.setattr(launcher, "_bridge", fake_bridge)

        async def fake_stdin_reader_task(state):
            await asyncio.Event().wait()

        monkeypatch.setattr(launcher, "_stdin_reader_task", fake_stdin_reader_task)

    def test_registers_unconditionally(self, monkeypatch):
        """main()の実行経路上でregister_launcher_sessionが呼ばれる"""
        called = {"count": 0}

        def fake_register(session_id, pid=None):
            called["count"] += 1

        monkeypatch.setattr(launcher, "register_launcher_session", fake_register)
        self._setup_common(monkeypatch)
        launcher.main()
        assert called["count"] == 1


class TestReadMaxRetries:
    """_read_max_retries() のテスト"""

    def test_returns_none_when_env_unset(self, monkeypatch):
        """env 未設定時は None（無限）を返す"""
        monkeypatch.delenv("CALM_LAUNCHER_MAX_RETRIES", raising=False)
        assert launcher._read_max_retries() is None

    def test_returns_none_when_env_empty(self, monkeypatch):
        """env が空文字列のときは None を返す"""
        monkeypatch.setenv("CALM_LAUNCHER_MAX_RETRIES", "")
        assert launcher._read_max_retries() is None

    def test_returns_int_when_env_valid(self, monkeypatch):
        """env が有効な数値のときはその値を返す"""
        monkeypatch.setenv("CALM_LAUNCHER_MAX_RETRIES", "5")
        assert launcher._read_max_retries() == 5

    def test_returns_zero_when_env_zero(self, monkeypatch):
        """env が 0 のときは 0 を返す（リトライしないという有効値）"""
        monkeypatch.setenv("CALM_LAUNCHER_MAX_RETRIES", "0")
        assert launcher._read_max_retries() == 0

    def test_returns_none_on_invalid_string(self, monkeypatch):
        """env が数値に変換できない文字列のときは None にフォールバック"""
        monkeypatch.setenv("CALM_LAUNCHER_MAX_RETRIES", "abc")
        assert launcher._read_max_retries() is None

    def test_returns_none_on_negative(self, monkeypatch):
        """env が負値のときは None にフォールバック"""
        monkeypatch.setenv("CALM_LAUNCHER_MAX_RETRIES", "-1")
        assert launcher._read_max_retries() is None


class TestReadStdinEofGraceSec:
    """_read_stdin_eof_grace_sec() のテスト"""

    def test_returns_default_when_env_unset(self, monkeypatch):
        """env 未設定時は既定値 (10.0秒) を返す"""
        monkeypatch.delenv("CALM_LAUNCHER_STDIN_EOF_GRACE_SEC", raising=False)
        assert launcher._read_stdin_eof_grace_sec() == launcher.DEFAULT_STDIN_EOF_GRACE_SEC

    def test_returns_float_when_env_valid(self, monkeypatch):
        """env が有効な数値のときはその値を返す"""
        monkeypatch.setenv("CALM_LAUNCHER_STDIN_EOF_GRACE_SEC", "3.5")
        assert launcher._read_stdin_eof_grace_sec() == 3.5

    def test_returns_default_on_invalid_string(self, monkeypatch):
        """env が数値に変換できない文字列のときは既定値にフォールバック"""
        monkeypatch.setenv("CALM_LAUNCHER_STDIN_EOF_GRACE_SEC", "abc")
        assert launcher._read_stdin_eof_grace_sec() == launcher.DEFAULT_STDIN_EOF_GRACE_SEC

    def test_returns_default_on_zero_or_negative(self, monkeypatch):
        """env が 0 以下のときは既定値にフォールバック"""
        monkeypatch.setenv("CALM_LAUNCHER_STDIN_EOF_GRACE_SEC", "0")
        assert launcher._read_stdin_eof_grace_sec() == launcher.DEFAULT_STDIN_EOF_GRACE_SEC
        monkeypatch.setenv("CALM_LAUNCHER_STDIN_EOF_GRACE_SEC", "-1")
        assert launcher._read_stdin_eof_grace_sec() == launcher.DEFAULT_STDIN_EOF_GRACE_SEC


class TestReadMaxConsecutiveStreamExceptions:
    """_read_max_consecutive_stream_exceptions() のテスト"""

    def test_returns_default_when_env_unset(self, monkeypatch):
        """env 未設定時は既定値 (5) を返す"""
        monkeypatch.delenv(
            "CALM_LAUNCHER_MAX_CONSECUTIVE_STREAM_EXCEPTIONS", raising=False
        )
        assert (
            launcher._read_max_consecutive_stream_exceptions()
            == launcher.DEFAULT_MAX_CONSECUTIVE_STREAM_EXCEPTIONS
        )

    def test_returns_int_when_env_valid(self, monkeypatch):
        """env が有効な数値のときはその値を返す"""
        monkeypatch.setenv("CALM_LAUNCHER_MAX_CONSECUTIVE_STREAM_EXCEPTIONS", "8")
        assert launcher._read_max_consecutive_stream_exceptions() == 8

    def test_returns_default_on_invalid_string(self, monkeypatch):
        """env が数値に変換できない文字列のときは既定値にフォールバック"""
        monkeypatch.setenv("CALM_LAUNCHER_MAX_CONSECUTIVE_STREAM_EXCEPTIONS", "abc")
        assert (
            launcher._read_max_consecutive_stream_exceptions()
            == launcher.DEFAULT_MAX_CONSECUTIVE_STREAM_EXCEPTIONS
        )

    def test_returns_default_on_zero_or_negative(self, monkeypatch):
        """env が 0 以下のときは既定値にフォールバック"""
        monkeypatch.setenv("CALM_LAUNCHER_MAX_CONSECUTIVE_STREAM_EXCEPTIONS", "0")
        assert (
            launcher._read_max_consecutive_stream_exceptions()
            == launcher.DEFAULT_MAX_CONSECUTIVE_STREAM_EXCEPTIONS
        )
        monkeypatch.setenv("CALM_LAUNCHER_MAX_CONSECUTIVE_STREAM_EXCEPTIONS", "-1")
        assert (
            launcher._read_max_consecutive_stream_exceptions()
            == launcher.DEFAULT_MAX_CONSECUTIVE_STREAM_EXCEPTIONS
        )


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

        monkeypatch.delenv("CALM_LAUNCHER_MAX_RETRIES", raising=False)
        importlib.reload(launcher)
        try:
            assert launcher.MAX_RETRIES is None
        finally:
            launcher.MAX_RETRIES = None

    def test_override_via_env(self, monkeypatch):
        """env で数値指定するとモジュール再読み込みで MAX_RETRIES がその値になる"""
        import importlib

        monkeypatch.setenv("CALM_LAUNCHER_MAX_RETRIES", "7")
        importlib.reload(launcher)
        try:
            assert launcher.MAX_RETRIES == 7
        finally:
            launcher.MAX_RETRIES = None
