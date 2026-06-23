"""SSE keepalive frame の動作検証。

無送信状態でも `: keepalive\\n\\n` が KEEPALIVE_INTERVAL_SEC 間隔で flush される
ことを実プロセスの relay に対して確認する。flush が定期的に走ることで blocked
client の subscriber リークも構造的に解決される (BrokenPipeError → finally で
remove)。
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_relay(port: int, db_path: str, keepalive_sec: int) -> subprocess.Popen:
    env = os.environ.copy()
    env["RELAY_PORT"] = str(port)
    env["RELAY_DB"] = db_path
    env["RELAY_KEEPALIVE_SEC"] = str(keepalive_sec)
    proc = subprocess.Popen(
        [sys.executable, "-m", "src.relay.server"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(_REPO_ROOT),
    )
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=0.3
            ) as r:
                if r.status == 200:
                    return proc
        except Exception:
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError("relay did not become ready in time")


def _create_channel(port: int, code: str = "kptest") -> str:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/create",
        data=json.dumps({"channel_code": code}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=2.0) as r:
        return json.loads(r.read())["channel_code"]


def _post_send(port: int, channel: str, handle: str, body: dict) -> int:
    payload = json.dumps(
        {"channel": channel, "handle": handle, "body": json.dumps(body)}
    ).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/send",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=2.0) as r:
        return json.loads(r.read())["msg_id"]


@pytest.fixture
def isolated_relay(tmp_path):
    port = _pick_free_port()
    db_path = str(tmp_path / "relay.db")
    proc = _start_relay(port, db_path, keepalive_sec=1)
    try:
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()


def _read_stream_lines(port: int, channel: str, handle: str, duration_sec: float) -> list[str]:
    """SSE ストリームを duration_sec 秒読んで受信した行を全部返す。"""
    lines: list[str] = []
    stop = threading.Event()

    def reader():
        url = f"http://127.0.0.1:{port}/stream?channel={channel}&handle={handle}"
        try:
            with urllib.request.urlopen(url, timeout=duration_sec + 5) as r:
                for raw in r:
                    if stop.is_set():
                        break
                    lines.append(raw.decode(errors="replace").rstrip("\n"))
        except Exception:
            pass

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    time.sleep(duration_sec)
    stop.set()
    t.join(timeout=1.0)
    return lines


class TestKeepaliveFrame:
    """RELAY_KEEPALIVE_SEC=1 のとき keepalive comment frame が定期的に出る。"""

    def test_idle_connection_receives_keepalive(self, isolated_relay):
        """無送信状態で SSE 接続を保持していると ': keepalive' が複数回届く。"""
        port = isolated_relay
        code = _create_channel(port, "idle-kp")

        lines = _read_stream_lines(port, code, "victim", duration_sec=3.5)

        # connected 初回 + 少なくとも 2 回の keepalive
        assert any(": connected" in line for line in lines), (
            f"connected frame 未受信: lines={lines}"
        )
        keepalives = [line for line in lines if ": keepalive" in line]
        assert len(keepalives) >= 2, (
            f"keepalive frame 期待 >=2 件、実受信 {len(keepalives)} 件: lines={lines}"
        )

    def test_keepalive_does_not_replace_data_frames(self, isolated_relay):
        """keepalive 経路は data frame の配送を妨げない。"""
        port = isolated_relay
        code = _create_channel(port, "mixed-kp")

        lines: list[str] = []
        stop = threading.Event()

        def reader():
            url = f"http://127.0.0.1:{port}/stream?channel={code}&handle=victim"
            try:
                with urllib.request.urlopen(url, timeout=10) as r:
                    for raw in r:
                        if stop.is_set():
                            break
                        lines.append(raw.decode(errors="replace").rstrip("\n"))
            except Exception:
                pass

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        time.sleep(0.5)  # subscribe 完了を確実に待つ
        _post_send(
            port, code, "attacker",
            {"v": 1, "kind": "event", "data": {"type": "test"}, "to": "victim"},
        )
        time.sleep(2.5)  # data frame + keepalive 両方拾える時間
        stop.set()
        t.join(timeout=1.0)

        data_lines = [line for line in lines if line.startswith("data: ")]
        keepalives = [line for line in lines if ": keepalive" in line]
        assert len(data_lines) >= 1, f"data frame 未受信: lines={lines}"
        assert len(keepalives) >= 1, f"keepalive 未受信: lines={lines}"
