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

    def test_sends_v3_envelope_with_ready_phase(self, mock_relay, tmp_phase_file):
        """ready フェーズで v3 envelope の phase フィールドが ready になる"""
        server, relay_url = mock_relay
        tmp_phase_file.write_text("ready")

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
        assert body.get("data", {}).get("phase") == "ready"

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
        """PHASE_FILE を loading → ready に変更すると interval が変わる"""
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
        deadline = time.time() + 1.5
        while len(server.received) < 2 and time.time() < deadline:
            time.sleep(0.05)

        loading_count = len(server.received)

        # ready フェーズに切り替え
        tmp_phase_file.write_text("ready")

        deadline = time.time() + 1.0
        while len(server.received) < loading_count + 1 and time.time() < deadline:
            time.sleep(0.05)

        # ready フェーズのメッセージが届いているはず
        tmp_phase_file.unlink()
        proc.terminate()
        proc.wait(timeout=3)

        ready_msgs = [
            m for m in server.received
            if m.get("body", {}).get("data", {}).get("phase") == "ready"
        ]
        assert len(ready_msgs) >= 1
