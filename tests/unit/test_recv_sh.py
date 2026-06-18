"""recv.sh のユニットテスト

scripts/ow/recv.sh が OW_PARENT_PID 監視で親プロセス死亡時に自動 exit すること、
trap で curl 子プロセスを掃除すること、構文が正しいことを検証する。
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
        """SIGTERM で recv.sh が exit する (pipeline 末尾 python3 を kill → curl は SIGPIPE 連鎖死)

        TODO: curl 自体のPID追跡アサーションは未実装。SSE 無音時に curl が
        孤児として残るリスクは別 issue 扱い。
        """
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
