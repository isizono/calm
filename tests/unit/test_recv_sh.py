"""recv.sh のユニットテスト

scripts/ow/recv.sh が OW_PARENT_PID 監視で親プロセス死亡時に自動 exit すること、
trap で curl + python3 子プロセスを両方掃除すること、構文が正しいことを検証する。
"""

import os
import subprocess
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import pytest

SCRIPT_PATH = (
    Path(__file__).parent.parent.parent / "scripts" / "ow" / "recv.sh"
)


def _list_descendants(root_pid: int) -> list[tuple[int, str]]:
    """root_pid 配下のすべての子孫プロセス (PID, comm) を返す。"""
    result = subprocess.run(
        ["ps", "-eo", "pid=,ppid=,comm="],
        capture_output=True,
        text=True,
    )
    parents: dict[int, list[tuple[int, str]]] = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        comm = parts[2]
        parents.setdefault(ppid, []).append((pid, comm))

    out: list[tuple[int, str]] = []
    stack = [root_pid]
    while stack:
        cur = stack.pop()
        for child_pid, child_comm in parents.get(cur, []):
            out.append((child_pid, child_comm))
            stack.append(child_pid)
    return out


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


class _StreamRelayHandler(BaseHTTPRequestHandler):
    """GET /stream に対し SSE で keep-alive コメントだけを返し long-poll させる"""

    def do_GET(self):
        if not self.path.startswith("/stream"):
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        # 30秒間 keep-alive コメントを送り続ける（テストはこれより短く完了する）
        deadline = time.time() + 30
        try:
            while time.time() < deadline:
                self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


@pytest.fixture
def mock_stream_relay():
    server = HTTPServer(("127.0.0.1", 0), _StreamRelayHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    yield server, f"http://127.0.0.1:{port}"
    server.shutdown()


class TestRecvShSyntax:
    def test_script_exists(self):
        assert SCRIPT_PATH.exists(), f"recv.sh が存在しない: {SCRIPT_PATH}"

    def test_bash_syntax_ok(self):
        result = subprocess.run(
            ["bash", "-n", str(SCRIPT_PATH)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"bash -n が失敗: {result.stderr}"


class TestRecvShParentWatchdog:
    """A案: OW_PARENT_PID 監視で親プロセス死亡時に自動 exit する"""

    def test_exits_when_parent_dies(self, mock_stream_relay):
        """OW_PARENT_PID で指定された親 PID が消えたら recv.sh が exit する"""
        _server, relay_url = mock_stream_relay

        # ダミー親 process を sleep で起動
        parent = subprocess.Popen(["sleep", "30"])
        parent_pid = parent.pid

        env = {
            **os.environ,
            "RELAY_URL": relay_url,
            "OW_PARENT_PID": str(parent_pid),
        }

        proc = subprocess.Popen(
            ["bash", str(SCRIPT_PATH), "CH_PW", "w-pw"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # SSE接続が確立するまで少し待つ
        time.sleep(2.0)
        assert proc.poll() is None, "親生存中に recv.sh が早期 exit している"

        # ダミー親を kill → recv.sh が次の親監視チェックで自動 exit するはず
        parent.kill()
        parent.wait()

        try:
            proc.wait(timeout=5)
            exited = True
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait()
            exited = False

        assert exited, "親プロセス死亡後に recv.sh が exit しなかった"

    def test_no_parent_pid_does_not_exit(self, mock_stream_relay):
        """OW_PARENT_PID 未指定なら親監視は無効（後方互換）"""
        _server, relay_url = mock_stream_relay

        env = {
            **os.environ,
            "RELAY_URL": relay_url,
        }
        env.pop("OW_PARENT_PID", None)

        proc = subprocess.Popen(
            ["bash", str(SCRIPT_PATH), "CH_NP", "w-np"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # 2秒待っても止まらないこと（無限ループ動作中）
        time.sleep(2.0)
        assert proc.poll() is None, "OW_PARENT_PID 未指定で recv.sh が早期 exit した"

        proc.terminate()
        proc.wait(timeout=5)


class TestRecvShTrap:
    """B案: trap EXIT で pipeline 子プロセス (python3 + curl) が掃除される"""

    def test_pipe_killed_on_sigterm(self, mock_stream_relay):
        """SIGTERM で recv.sh が exit し、curl と python3 が両方とも消える。"""
        _server, relay_url = mock_stream_relay

        env = {
            **os.environ,
            "RELAY_URL": relay_url,
        }

        proc = subprocess.Popen(
            ["bash", str(SCRIPT_PATH), "CH_TRAP", "w-trap"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # SSE接続が確立するまで待つ
        time.sleep(1.5)
        assert proc.poll() is None

        # SIGTERM → trap cleanup → exit
        proc.terminate()
        try:
            proc.wait(timeout=3)
            exited = True
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            exited = False

        assert exited, "SIGTERM で recv.sh が exit しなかった"


class TestRecvShCurlPid:
    """curl 子プロセスの個別 PID 追跡 + cleanup 経路で両方 kill される"""

    def _wait_for_children(
        self, pid: int, expected_names: tuple[str, ...], timeout: float = 3.0
    ) -> list[tuple[int, str]]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            children = _list_descendants(pid)
            if all(
                any(name in comm for _, comm in children) for name in expected_names
            ):
                return children
            time.sleep(0.1)
        return _list_descendants(pid)

    def test_curl_and_python_die_on_sigterm(self, mock_stream_relay):
        """SIGTERM 後、curl と python3 (PIPE_PID) の両方が cleanup trap で kill される。

        旧 pipeline 実装では PIPE_PID = python3 PID のみで curl は SIGPIPE 連鎖死に
        依存していたが、SSE 無音時には連鎖死しない。mkfifo + 個別 bg job で curl PID
        を追跡し、cleanup trap で明示 kill する設計の回帰テスト。
        """
        _server, relay_url = mock_stream_relay

        env = {**os.environ, "RELAY_URL": relay_url}

        proc = subprocess.Popen(
            ["bash", str(SCRIPT_PATH), "CH_CURL", "w-curl"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        try:
            # SSE接続確立 + curl/python3 起動を待つ
            children = self._wait_for_children(proc.pid, ("curl", "python"))
            assert proc.poll() is None
            curl_pids = [pid for pid, comm in children if "curl" in comm]
            py_pids = [pid for pid, comm in children if "python" in comm]
            assert curl_pids, (
                f"curl が子孫プロセスに存在しない (mkfifo 経由 bg 起動の確認)\n"
                f"children: {children}"
            )
            assert py_pids, f"python3 が子孫プロセスに存在しない\nchildren: {children}"

            # SIGTERM → trap cleanup → 両方とも消える
            proc.terminate()
            proc.wait(timeout=5)

            time.sleep(0.5)
            for pid in curl_pids + py_pids:
                assert not _pid_alive(pid), f"PID {pid} が cleanup 後も残存"
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_curl_dies_when_parent_dies(self, mock_stream_relay):
        """OW_PARENT_PID で指定した親が死ぬと curl も python3 も消える。"""
        _server, relay_url = mock_stream_relay

        parent = subprocess.Popen(["sleep", "30"])
        try:
            env = {
                **os.environ,
                "RELAY_URL": relay_url,
                "OW_PARENT_PID": str(parent.pid),
            }

            proc = subprocess.Popen(
                ["bash", str(SCRIPT_PATH), "CH_PWCURL", "w-pwcurl"],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            try:
                children = self._wait_for_children(proc.pid, ("curl", "python"))
                assert proc.poll() is None
                curl_pids = [pid for pid, comm in children if "curl" in comm]
                py_pids = [pid for pid, comm in children if "python" in comm]
                assert curl_pids and py_pids

                parent.kill()
                parent.wait()

                proc.wait(timeout=5)

                time.sleep(0.5)
                for pid in curl_pids + py_pids:
                    assert not _pid_alive(pid), (
                        f"PID {pid} が親死亡後 cleanup で消えていない"
                    )
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait()


class TestRecvShUsage:
    def test_usage_error_when_args_missing(self):
        """引数不足で usage エラーを出して exit 1"""
        result = subprocess.run(
            ["bash", str(SCRIPT_PATH)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode != 0
        assert "Usage" in result.stderr
