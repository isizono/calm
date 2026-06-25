"""heartbeat.sh のユニットテスト

scripts/ow/heartbeat.sh が relay /send エンドポイントへ正しい
v3 envelope を送信し、PHASE_FILE の値でインターバルを切り替え、
ファイル削除でループが停止することを検証する。
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPT_PATH = (
    Path(__file__).parent.parent.parent / "scripts" / "ow" / "heartbeat.sh"
)

# ========================================
# Mock relay server
# ========================================


class _RelayHandler(BaseHTTPRequestHandler):
    """POST /send リクエストを受け取って記録するスタブ"""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = {}
        self.server.received.append(data)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"msg_id": 1}')

    def log_message(self, *args):
        pass  # suppress output


def _start_mock_relay():
    """空きポートでスタブサーバーを起動し (server, url) を返す"""
    server = HTTPServer(("127.0.0.1", 0), _RelayHandler)
    server.received = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    return server, f"http://127.0.0.1:{port}"


# ========================================
# Fixtures
# ========================================


@pytest.fixture
def mock_relay():
    server, url = _start_mock_relay()
    yield server, url
    server.shutdown()


@pytest.fixture
def tmp_phase_file(tmp_path):
    f = tmp_path / "ow_hb_phase_test"
    f.write_text("loading")
    yield f
    if f.exists():
        f.unlink()


# ========================================
# Tests
# ========================================


class TestHeartbeatShSyntax:
    def test_script_exists(self):
        assert SCRIPT_PATH.exists(), f"heartbeat.sh が存在しない: {SCRIPT_PATH}"

    def test_script_is_executable_or_interpretable(self):
        result = subprocess.run(
            ["bash", "-n", str(SCRIPT_PATH)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"bash -n が失敗: {result.stderr}"


class TestHeartbeatShEnvelope:
    def test_sends_v3_envelope_with_loading_phase(self, mock_relay, tmp_phase_file):
        """loading フェーズで v3 envelope が /send に届く"""
        server, relay_url = mock_relay
        tmp_phase_file.write_text("loading")

        env = {
            **os.environ,
            "RELAY_URL": relay_url,
            "PHASE_FILE": str(tmp_phase_file),
            "HEARTBEAT_INTERVAL_LOADING": "0",
            "HEARTBEAT_INTERVAL_DEFAULT": "0",
        }

        proc = subprocess.Popen(
            ["bash", str(SCRIPT_PATH), "CH_TEST", "w-test"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # 最低1件届くまで待つ（最大2秒）
        deadline = time.time() + 2.0
        while not server.received and time.time() < deadline:
            time.sleep(0.05)

        # ループ停止
        tmp_phase_file.unlink()
        proc.terminate()
        proc.wait(timeout=3)

        assert len(server.received) >= 1
        req = server.received[0]
        body = req.get("body", {})
        assert body.get("v") == 1
        assert body.get("kind") == "event"
        assert body.get("from") == "w-test"
        data = body.get("data", {})
        assert data.get("type") == "heartbeat"
        assert data.get("phase") == "loading"

    def test_sends_v3_envelope_with_working_phase(self, mock_relay, tmp_phase_file):
        """working フェーズで v3 envelope の phase フィールドが working になる"""
        server, relay_url = mock_relay
        tmp_phase_file.write_text("working")

        env = {
            **os.environ,
            "RELAY_URL": relay_url,
            "PHASE_FILE": str(tmp_phase_file),
            "HEARTBEAT_INTERVAL_LOADING": "0",
            "HEARTBEAT_INTERVAL_DEFAULT": "0",
        }

        proc = subprocess.Popen(
            ["bash", str(SCRIPT_PATH), "CH_TEST", "w-test"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        deadline = time.time() + 2.0
        while not server.received and time.time() < deadline:
            time.sleep(0.05)

        tmp_phase_file.unlink()
        proc.terminate()
        proc.wait(timeout=3)

        assert len(server.received) >= 1
        body = server.received[0].get("body", {})
        assert body.get("data", {}).get("phase") == "working"

    def test_channel_and_handle_in_request(self, mock_relay, tmp_phase_file):
        """channel と handle が relay /send リクエストに含まれる"""
        server, relay_url = mock_relay
        tmp_phase_file.write_text("loading")

        env = {
            **os.environ,
            "RELAY_URL": relay_url,
            "PHASE_FILE": str(tmp_phase_file),
            "HEARTBEAT_INTERVAL_LOADING": "0",
            "HEARTBEAT_INTERVAL_DEFAULT": "0",
        }

        proc = subprocess.Popen(
            ["bash", str(SCRIPT_PATH), "MY_CHANNEL", "my-handle"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        deadline = time.time() + 2.0
        while not server.received and time.time() < deadline:
            time.sleep(0.05)

        tmp_phase_file.unlink()
        proc.terminate()
        proc.wait(timeout=3)

        assert len(server.received) >= 1
        req = server.received[0]
        assert req.get("channel") == "MY_CHANNEL"
        assert req.get("handle") == "my-handle"


class TestHeartbeatShLoopControl:
    def test_loop_stops_when_phase_file_deleted(self, mock_relay, tmp_phase_file):
        """PHASE_FILE 削除でループが終了する"""
        server, relay_url = mock_relay
        tmp_phase_file.write_text("loading")

        env = {
            **os.environ,
            "RELAY_URL": relay_url,
            "PHASE_FILE": str(tmp_phase_file),
            "HEARTBEAT_INTERVAL_LOADING": "0",
            "HEARTBEAT_INTERVAL_DEFAULT": "0",
        }

        proc = subprocess.Popen(
            ["bash", str(SCRIPT_PATH), "CH_STOP", "w-stop"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # 1件届くまで待つ
        deadline = time.time() + 2.0
        while not server.received and time.time() < deadline:
            time.sleep(0.05)

        # PHASE_FILEを削除してループ停止を誘発
        tmp_phase_file.unlink()

        # スクリプトが正常終了するまで待つ（最大3秒）
        try:
            proc.wait(timeout=3)
            exited = True
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait()
            exited = False

        assert exited, "PHASE_FILE 削除後にスクリプトが終了しなかった"

    def test_interval_switches_with_phase(self, mock_relay, tmp_phase_file):
        """PHASE_FILE を loading → working に変更すると interval が変わる"""
        server, relay_url = mock_relay
        tmp_phase_file.write_text("loading")

        env = {
            **os.environ,
            "RELAY_URL": relay_url,
            "PHASE_FILE": str(tmp_phase_file),
            "HEARTBEAT_INTERVAL_LOADING": "0",
            "HEARTBEAT_INTERVAL_DEFAULT": "0",
        }

        proc = subprocess.Popen(
            ["bash", str(SCRIPT_PATH), "CH_SWITCH", "w-switch"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # loading フェーズで数件届くのを確認
        deadline = time.time() + 3.0
        while len(server.received) < 2 and time.time() < deadline:
            time.sleep(0.05)

        loading_count = len(server.received)
        assert loading_count >= 1, (
            f"loading フェーズのメッセージが届かなかった (deadline 3.0s 内に 0 件)"
        )

        # working フェーズに切り替え
        tmp_phase_file.write_text("working")

        deadline = time.time() + 6.0
        while len(server.received) < loading_count + 1 and time.time() < deadline:
            time.sleep(0.05)

        # working フェーズのメッセージが届いているはず
        tmp_phase_file.unlink()
        proc.terminate()
        proc.wait(timeout=3)

        working_msgs = [
            m for m in server.received
            if m.get("body", {}).get("data", {}).get("phase") == "working"
        ]
        assert len(working_msgs) >= 1


class TestHeartbeatShParentWatchdog:
    """A案: OW_PARENT_PID 監視で親プロセス死亡時に自動 exit する"""

    def test_exits_when_parent_dies(self, mock_relay, tmp_phase_file):
        """OW_PARENT_PID で指定された親 PID が消えたら loop が終了する"""
        server, relay_url = mock_relay
        tmp_phase_file.write_text("working")

        # ダミー親 process を sleep で起動
        parent = subprocess.Popen(["sleep", "30"])
        parent_pid = parent.pid

        env = {
            **os.environ,
            "RELAY_URL": relay_url,
            "PHASE_FILE": str(tmp_phase_file),
            "HEARTBEAT_INTERVAL_LOADING": "0",
            "HEARTBEAT_INTERVAL_DEFAULT": "0",
            "OW_PARENT_PID": str(parent_pid),
        }

        proc = subprocess.Popen(
            ["bash", str(SCRIPT_PATH), "CH_PW", "w-pw"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # 1件届くまで待つ
        deadline = time.time() + 2.0
        while not server.received and time.time() < deadline:
            time.sleep(0.05)

        assert len(server.received) >= 1, "親生存中にheartbeatが届いていない"

        # ダミー親を kill → heartbeat.sh が次の周期で自動 exit するはず
        parent.kill()
        parent.wait()

        try:
            proc.wait(timeout=3)
            exited = True
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait()
            exited = False

        assert exited, "親プロセス死亡後に heartbeat.sh が exit しなかった"

    def test_no_parent_pid_does_not_block(self, mock_relay, tmp_phase_file):
        """OW_PARENT_PID 未指定なら従来通り動く（後方互換）"""
        server, relay_url = mock_relay
        tmp_phase_file.write_text("loading")

        env = {
            **os.environ,
            "RELAY_URL": relay_url,
            "PHASE_FILE": str(tmp_phase_file),
            "HEARTBEAT_INTERVAL_LOADING": "0",
            "HEARTBEAT_INTERVAL_DEFAULT": "0",
        }
        # OW_PARENT_PID を意図的に削除（あれば）
        env.pop("OW_PARENT_PID", None)

        proc = subprocess.Popen(
            ["bash", str(SCRIPT_PATH), "CH_NP", "w-np"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        deadline = time.time() + 2.0
        while not server.received and time.time() < deadline:
            time.sleep(0.05)

        tmp_phase_file.unlink()
        proc.wait(timeout=3)

        assert len(server.received) >= 1, "OW_PARENT_PID 未指定時にheartbeatが届かなかった"


class TestHeartbeatShTrap:
    """B案: trap EXIT で PHASE_FILE が自動掃除される"""

    def test_phase_file_removed_on_sigterm(self, mock_relay, tmp_path):
        """SIGTERM 受信で PHASE_FILE が削除される（trap cleanup）"""
        server, relay_url = mock_relay
        phase_file = tmp_path / "ow_hb_phase_trap_test"
        phase_file.write_text("working")

        env = {
            **os.environ,
            "RELAY_URL": relay_url,
            "PHASE_FILE": str(phase_file),
            "HEARTBEAT_INTERVAL_LOADING": "0",
            "HEARTBEAT_INTERVAL_DEFAULT": "0",
        }

        proc = subprocess.Popen(
            ["bash", str(SCRIPT_PATH), "CH_TRAP", "w-trap"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # 1件届くまで待つ
        deadline = time.time() + 2.0
        while not server.received and time.time() < deadline:
            time.sleep(0.05)

        assert phase_file.exists(), "起動直後に PHASE_FILE が消えている"

        # SIGTERM → trap cleanup が走るはず
        proc.terminate()
        proc.wait(timeout=3)

        assert not phase_file.exists(), "trap EXIT で PHASE_FILE が削除されなかった"


class TestHeartbeatShSelfExit:
    """MCP /health 連続失敗時に safe state の worker が self-exit する"""

    @staticmethod
    def _start_health_relay(health_alive=True):
        """relay と MCP /health を兼ねるスタブ。/send と /health を両方受ける。"""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/health":
                    if self.server.health_alive:
                        self.send_response(200)
                        self.end_headers()
                        self.wfile.write(b'{"status":"ok"}')
                    else:
                        self.send_response(503)
                        self.end_headers()
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    data = {}
                self.server.received.append(data)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"msg_id": 1}')

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server.received = []
        server.health_alive = health_alive
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        return server, f"http://127.0.0.1:{port}"

    def test_no_self_exit_when_mcp_alive(self, tmp_phase_file):
        """MCP /health が ok を返している間は self-exit しない"""
        server, url = self._start_health_relay(health_alive=True)
        tmp_phase_file.write_text("working")
        env = {
            **os.environ,
            "RELAY_URL": url,
            "OW_MCP_URL": url,
            "PHASE_FILE": str(tmp_phase_file),
            "HEARTBEAT_INTERVAL_LOADING": "0",
            "HEARTBEAT_INTERVAL_DEFAULT": "0",
            "OW_MCP_FAIL_THRESHOLD": "1",
            "OW_MCP_UPTIME_MIN_SEC": "0",
        }
        proc = subprocess.Popen(
            ["bash", str(SCRIPT_PATH), "CH_OK", "w-ok"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1.0)
        # まだ生きているはず
        alive = proc.poll() is None
        tmp_phase_file.unlink()
        proc.terminate()
        proc.wait(timeout=3)
        server.shutdown()
        assert alive, "MCP /health 応答中に self-exit してしまった"

    def test_self_exit_when_mcp_down_and_safe(self, tmp_phase_file):
        """w-* handle + PHASE=working + uptime >= 閾値 + MCP 失敗 N回 で self-exit"""
        server, url = self._start_health_relay(health_alive=False)
        tmp_phase_file.write_text("working")
        env = {
            **os.environ,
            "RELAY_URL": url,
            "OW_MCP_URL": url,
            "PHASE_FILE": str(tmp_phase_file),
            "HEARTBEAT_INTERVAL_LOADING": "0",
            "HEARTBEAT_INTERVAL_DEFAULT": "0",
            "OW_MCP_FAIL_THRESHOLD": "2",
            "OW_MCP_UPTIME_MIN_SEC": "0",
        }
        env.pop("TMUX_PANE", None)  # kill-pane は noop 化（テスト環境）
        proc = subprocess.Popen(
            ["bash", str(SCRIPT_PATH), "CH_DOWN", "w-down"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # 失敗 2回 → self-exit するはず
        try:
            proc.wait(timeout=10)
            exited = True
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait()
            exited = False
        # relay に event:self-exit が届いているか
        self_exit_events = [
            r for r in server.received
            if r.get("body", {}).get("data", {}).get("type") == "self-exit"
        ]
        if tmp_phase_file.exists():
            tmp_phase_file.unlink()
        server.shutdown()
        assert exited, "MCP down + safe state で self-exit しなかった"
        assert len(self_exit_events) >= 1, "event:self-exit が relay に届いていない"
        assert self_exit_events[0]["body"]["data"]["reason"] == "mcp-loss"

    def test_no_self_exit_when_uptime_below_threshold(self, tmp_phase_file):
        """uptime が閾値未満なら MCP 失敗続いても self-exit しない"""
        server, url = self._start_health_relay(health_alive=False)
        tmp_phase_file.write_text("working")
        env = {
            **os.environ,
            "RELAY_URL": url,
            "OW_MCP_URL": url,
            "PHASE_FILE": str(tmp_phase_file),
            "HEARTBEAT_INTERVAL_LOADING": "0",
            "HEARTBEAT_INTERVAL_DEFAULT": "0",
            "OW_MCP_FAIL_THRESHOLD": "1",
            "OW_MCP_UPTIME_MIN_SEC": "9999",  # 事実上発火させない
        }
        proc = subprocess.Popen(
            ["bash", str(SCRIPT_PATH), "CH_UP", "w-up"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1.0)
        alive = proc.poll() is None
        tmp_phase_file.unlink()
        proc.terminate()
        proc.wait(timeout=3)
        server.shutdown()
        assert alive, "uptime 閾値未満で self-exit してしまった"

    def test_no_self_exit_for_non_w_handle(self, tmp_phase_file):
        """handle が w-* 以外なら self-exit 対象外（orch / dispatcher 保護）"""
        server, url = self._start_health_relay(health_alive=False)
        tmp_phase_file.write_text("working")
        env = {
            **os.environ,
            "RELAY_URL": url,
            "OW_MCP_URL": url,
            "PHASE_FILE": str(tmp_phase_file),
            "HEARTBEAT_INTERVAL_LOADING": "0",
            "HEARTBEAT_INTERVAL_DEFAULT": "0",
            "OW_MCP_FAIL_THRESHOLD": "1",
            "OW_MCP_UPTIME_MIN_SEC": "0",
        }
        proc = subprocess.Popen(
            ["bash", str(SCRIPT_PATH), "CH_ORCH", "orch"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1.0)
        alive = proc.poll() is None
        tmp_phase_file.unlink()
        proc.terminate()
        proc.wait(timeout=3)
        server.shutdown()
        assert alive, "orch handle が self-exit してしまった"

    def test_no_self_exit_for_non_working_phase(self, tmp_phase_file):
        """PHASE != working なら self-exit 対象外（loading/draining 保護）"""
        server, url = self._start_health_relay(health_alive=False)
        tmp_phase_file.write_text("loading")
        env = {
            **os.environ,
            "RELAY_URL": url,
            "OW_MCP_URL": url,
            "PHASE_FILE": str(tmp_phase_file),
            "HEARTBEAT_INTERVAL_LOADING": "0",
            "HEARTBEAT_INTERVAL_DEFAULT": "0",
            "OW_MCP_FAIL_THRESHOLD": "1",
            "OW_MCP_UPTIME_MIN_SEC": "0",
        }
        proc = subprocess.Popen(
            ["bash", str(SCRIPT_PATH), "CH_W", "w-busy"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1.0)
        alive = proc.poll() is None
        tmp_phase_file.unlink()
        proc.terminate()
        proc.wait(timeout=3)
        server.shutdown()
        assert alive, "PHASE=loading で self-exit してしまった"

    def test_disable_flag_skips_self_exit(self, tmp_phase_file):
        """OW_DISABLE_MCP_SELF_EXIT=1 で self-exit を完全無効化できる"""
        server, url = self._start_health_relay(health_alive=False)
        tmp_phase_file.write_text("working")
        env = {
            **os.environ,
            "RELAY_URL": url,
            "OW_MCP_URL": url,
            "PHASE_FILE": str(tmp_phase_file),
            "HEARTBEAT_INTERVAL_LOADING": "0",
            "HEARTBEAT_INTERVAL_DEFAULT": "0",
            "OW_MCP_FAIL_THRESHOLD": "1",
            "OW_MCP_UPTIME_MIN_SEC": "0",
            "OW_DISABLE_MCP_SELF_EXIT": "1",
        }
        proc = subprocess.Popen(
            ["bash", str(SCRIPT_PATH), "CH_DIS", "w-dis"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1.0)
        alive = proc.poll() is None
        tmp_phase_file.unlink()
        proc.terminate()
        proc.wait(timeout=3)
        server.shutdown()
        assert alive, "OW_DISABLE_MCP_SELF_EXIT=1 でも self-exit してしまった"

    def test_recovery_resets_fail_counter(self, tmp_phase_file):
        """MCP が復活したらカウンタが 0 リセットされる（復帰後 N 回未満なら exit しない）"""
        server, url = self._start_health_relay(health_alive=False)
        tmp_phase_file.write_text("working")
        env = {
            **os.environ,
            "RELAY_URL": url,
            "OW_MCP_URL": url,
            "PHASE_FILE": str(tmp_phase_file),
            "HEARTBEAT_INTERVAL_LOADING": "0.3",
            "HEARTBEAT_INTERVAL_DEFAULT": "0.3",
            "OW_MCP_FAIL_THRESHOLD": "3",
            "OW_MCP_UPTIME_MIN_SEC": "0",
        }
        proc = subprocess.Popen(
            ["bash", str(SCRIPT_PATH), "CH_REC", "w-rec"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # 1〜2 周期失敗した段階で MCP 復活。
        # 0.1s だと CI のスケジューラ次第で最初のループの curl 中にも当たり、
        # 失敗回数が 0/1/2 でブレうる。0.5s 待てば最低 1 回は必ず失敗を経由する。
        # OW_MCP_FAIL_THRESHOLD=3 なので 2 回程度の揺れは許容される。
        time.sleep(0.5)
        server.health_alive = True
        # 0.3 * 4 周期分待つ。カウンタが 0 にリセットされて self-exit しないことを確認
        time.sleep(1.5)
        alive = proc.poll() is None
        proc.terminate()
        proc.wait(timeout=3)
        tmp_phase_file.unlink(missing_ok=True)
        server.shutdown()
        assert alive, "MCP 復活後にカウンタがリセットされず self-exit してしまった"


class TestHeartbeatShHbFailIdleTimeout:
    """機構1: relay /send 連続失敗で hb-fail idle-timeout が発火する。
    uptime gate (OW_MCP_UPTIME_MIN_SEC) で spawn 直後の relay cold start
    や transient hang を「永続 idle」と誤判定しないことを検証する。"""

    def _get_dead_url(self):
        """connect refused になる URL を返す。

        bind→close 方式だと s.close() の直後に別プロセスが同ポートを bind して
        curl が接続成功する TOCTOU 競合があるため、socket を bind したまま
        listen() せず保持する。listen() を呼ばない socket への connect() は
        OS が ECONNREFUSED を返すため、ポート占有 + 接続拒否を同時に実現できる。
        socket オブジェクトは呼び出し側で保持してテスト終了まで close しない。
        """
        import socket as _socket

        s = _socket.socket()
        s.bind(("127.0.0.1", 0))
        # listen() を呼ばないので接続は ECONNREFUSED になる
        port = s.getsockname()[1]
        return s, f"http://127.0.0.1:{port}"

    def test_hb_fail_kill_when_uptime_above_threshold(self, tmp_phase_file):
        """relay /send 失敗 N 回 + uptime >= 閾値 で idle-timeout 発火 → exit"""
        dead_sock, url = self._get_dead_url()
        tmp_phase_file.write_text("working")
        env = {
            **os.environ,
            "RELAY_URL": url,
            "OW_MCP_URL": url,
            "PHASE_FILE": str(tmp_phase_file),
            "HEARTBEAT_INTERVAL_LOADING": "0",
            "HEARTBEAT_INTERVAL_DEFAULT": "0",
            "OW_HB_FAIL_THRESHOLD": "2",
            "OW_MCP_UPTIME_MIN_SEC": "0",
            "OW_DISABLE_MCP_SELF_EXIT": "1",
            "OW_CURL_CONNECT_TIMEOUT": "1",
            "OW_CURL_TIMEOUT": "1",
        }
        env.pop("TMUX_PANE", None)
        proc = subprocess.Popen(
            ["bash", str(SCRIPT_PATH), "CH_HBDOWN", "w-hbdown"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            proc.wait(timeout=10)
            exited = True
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait()
            exited = False
        if tmp_phase_file.exists():
            tmp_phase_file.unlink()
        dead_sock.close()
        assert exited, (
            "relay /send 連続失敗 + uptime gate 通過で idle-timeout が発火しなかった"
        )

    def test_no_hb_fail_kill_when_uptime_below_threshold(self, tmp_phase_file):
        """uptime < 閾値 なら relay /send 失敗続いても idle-timeout 発火しない"""
        dead_sock, url = self._get_dead_url()
        tmp_phase_file.write_text("working")
        env = {
            **os.environ,
            "RELAY_URL": url,
            "OW_MCP_URL": url,
            "PHASE_FILE": str(tmp_phase_file),
            "HEARTBEAT_INTERVAL_LOADING": "0",
            "HEARTBEAT_INTERVAL_DEFAULT": "0",
            "OW_HB_FAIL_THRESHOLD": "1",
            "OW_MCP_UPTIME_MIN_SEC": "9999",
            "OW_DISABLE_MCP_SELF_EXIT": "1",
            "OW_CURL_CONNECT_TIMEOUT": "1",
            "OW_CURL_TIMEOUT": "1",
        }
        # uptime gate=9999s で shutdown_for_idle_timeout は発火しない前提だが、
        # 万一の誤発火時にテストランナーの tmux pane が kill されないよう
        # 防御的に TMUX_PANE を env から除去する（第1テストと対称）。
        env.pop("TMUX_PANE", None)
        proc = subprocess.Popen(
            ["bash", str(SCRIPT_PATH), "CH_HBUP", "w-hbup"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1.5)
        alive = proc.poll() is None
        tmp_phase_file.unlink()
        proc.terminate()
        proc.wait(timeout=3)
        dead_sock.close()
        assert alive, "uptime gate 未達で hb-fail idle-timeout が誤発火した"


class TestHeartbeatShCurlTimeout:
    """C案: curl --max-time でhang時に sleep に進める"""

    def test_curl_timeout_does_not_block_loop(self, tmp_phase_file):
        """relay が応答を遅延しても curl が --max-time で打ち切られ次の heartbeat が届く"""
        # 意図的に応答を遅延させる mock relay。並列受信のため ThreadingHTTPServer を使う
        # (シングルスレッドだと do_POST 内の sleep が次のリクエストを block する)。
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class _SlowRelayHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                _body = self.rfile.read(length)
                self.server.received.append(time.time())
                # 5秒hangさせる（curlのmax-timeでcutされるはず）
                time.sleep(5.0)
                try:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"msg_id": 1}')
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowRelayHandler)
        server.received = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        relay_url = f"http://127.0.0.1:{port}"

        tmp_phase_file.write_text("working")
        env = {
            **os.environ,
            "RELAY_URL": relay_url,
            "PHASE_FILE": str(tmp_phase_file),
            "HEARTBEAT_INTERVAL_LOADING": "0",
            "HEARTBEAT_INTERVAL_DEFAULT": "0",
            "OW_CURL_TIMEOUT": "1",  # 1秒で打ち切り
            "OW_CURL_CONNECT_TIMEOUT": "1",
        }

        proc = subprocess.Popen(
            ["bash", str(SCRIPT_PATH), "CH_SLOW", "w-slow"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # 4秒待つ。timeout 1秒 + interval 0 で、4秒で 2件以上の curl 試行があるはず。
        # （5秒hang が timeout で打ち切られ、次のループに進む）
        time.sleep(4.0)

        tmp_phase_file.unlink()
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

        server.shutdown()

        # 5秒hang を timeout で打ち切らなかったら 1件しか試行できないはず。
        # 2件以上 = curl timeout が機能している証拠。
        assert len(server.received) >= 2, (
            f"curl --max-time が機能していない (試行 {len(server.received)} 件、"
            f"timeout 1s なら 4秒で 2件以上届くはず)"
        )


class TestHeartbeatShDoneTimeout:
    """機構2 (D#2853): PHASE=done 継続で done-stall idle-timeout が発火する。
    DONE_SINCE_FILE 環境変数で since 記録ファイルを注入してタイマー起点を制御する。"""

    def test_empty_done_since_file_does_not_false_kill(self, mock_relay, tmp_path):
        """DONE_SINCE_FILE がゼロバイト（/tmp 容量不足等）でも即時誤 kill しない。

        空文字は算術展開で 0 扱いされ done_elapsed が epoch 秒大になり
        即時 kill されうるが、ガードが「今」起点に書き直して誤発火を防ぐ。"""
        server, relay_url = mock_relay
        phase_file = tmp_path / "ow_hb_phase_done_empty"
        phase_file.write_text("done")
        done_since_file = tmp_path / "ow_done_since_empty"
        done_since_file.write_text("")  # ゼロバイト

        env = {
            **os.environ,
            "RELAY_URL": relay_url,
            "PHASE_FILE": str(phase_file),
            "DONE_SINCE_FILE": str(done_since_file),
            "HEARTBEAT_INTERVAL_LOADING": "0.2",
            "HEARTBEAT_INTERVAL_DEFAULT": "0.2",
            "OW_DONE_TIMEOUT_SEC": "5",
            "OW_DISABLE_MCP_SELF_EXIT": "1",  # MCP self-exit を切り idle-timeout のみ検証
        }

        proc = subprocess.Popen(
            ["bash", str(SCRIPT_PATH), "CH_DONE_EMPTY", "w-done-empty"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # 閾値 5s 未満の 1.5s 経過後も生存しているはず（ガードで起点が「今」に補正）
        time.sleep(1.5)
        alive = proc.poll() is None

        phase_file.unlink(missing_ok=True)
        proc.terminate()
        proc.wait(timeout=3)
        if done_since_file.exists():
            done_since_file.unlink()

        # ガードが効いていれば done_since が「今」に書き直されているはず（空のままではない）
        assert alive, "空 DONE_SINCE_FILE で done-stall が即時誤発火し worker が kill された"

    def test_done_stall_kills_when_threshold_exceeded(self, mock_relay, tmp_path):
        """DONE_SINCE_FILE が閾値超過の過去 timestamp なら done-stall で kill + 通知。"""
        server, relay_url = mock_relay
        phase_file = tmp_path / "ow_hb_phase_done_stall"
        phase_file.write_text("done")
        done_since_file = tmp_path / "ow_done_since_stall"
        # 十分過去の epoch（done に入ってから長時間経過した状態を模擬）
        done_since_file.write_text("1000000000")

        env = {
            **os.environ,
            "RELAY_URL": relay_url,
            "PHASE_FILE": str(phase_file),
            "DONE_SINCE_FILE": str(done_since_file),
            "HEARTBEAT_INTERVAL_LOADING": "0.2",
            "HEARTBEAT_INTERVAL_DEFAULT": "0.2",
            "OW_DONE_TIMEOUT_SEC": "5",
            "OW_DISABLE_MCP_SELF_EXIT": "1",
        }
        env.pop("TMUX_PANE", None)  # kill-pane は noop 化（テスト環境）

        proc = subprocess.Popen(
            ["bash", str(SCRIPT_PATH), "CH_DONE_STALL", "w-done-stall"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            proc.wait(timeout=5)
            exited = True
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait()
            exited = False

        terminated_events = [
            r for r in server.received
            if r.get("body", {}).get("data", {}).get("state") == "terminated"
        ]

        phase_file.unlink(missing_ok=True)
        if done_since_file.exists():
            done_since_file.unlink()

        assert exited, "done-stall 閾値超過で worker が exit しなかった"
        assert len(terminated_events) >= 1, "terminated(idle-timeout) が relay に届いていない"
        data = terminated_events[0]["body"]["data"]
        assert data["cause"] == "idle-timeout"
        assert data["reason"] == "done-stall"

    def test_disable_idle_timeout_skips_done_stall(self, mock_relay, tmp_path):
        """OW_DISABLE_IDLE_TIMEOUT=1 なら done-stall 閾値超過でも kill しない。"""
        server, relay_url = mock_relay
        phase_file = tmp_path / "ow_hb_phase_done_dis"
        phase_file.write_text("done")
        done_since_file = tmp_path / "ow_done_since_dis"
        done_since_file.write_text("1000000000")  # 閾値を確実に超える過去

        env = {
            **os.environ,
            "RELAY_URL": relay_url,
            "PHASE_FILE": str(phase_file),
            "DONE_SINCE_FILE": str(done_since_file),
            "HEARTBEAT_INTERVAL_LOADING": "0.2",
            "HEARTBEAT_INTERVAL_DEFAULT": "0.2",
            "OW_DONE_TIMEOUT_SEC": "5",
            "OW_DISABLE_IDLE_TIMEOUT": "1",
            "OW_DISABLE_MCP_SELF_EXIT": "1",
        }

        proc = subprocess.Popen(
            ["bash", str(SCRIPT_PATH), "CH_DONE_DIS", "w-done-dis"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1.5)
        alive = proc.poll() is None

        phase_file.unlink(missing_ok=True)
        proc.terminate()
        proc.wait(timeout=3)
        if done_since_file.exists():
            done_since_file.unlink()

        assert alive, "OW_DISABLE_IDLE_TIMEOUT=1 でも done-stall kill が発火した"
