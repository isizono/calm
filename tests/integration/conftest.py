"""Integration test 共通ヘルパー / fixture。

`test_ow_projector.py` と `test_ow_reducer_cache_fastpath.py` で重複していた
`_free_port` / `_wait_until_healthy` / `_send_*` / `live_relay` fixture を統合。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from src.services import ow_service


def _free_port() -> int:
    """空いている TCP ポートを返す。

    NOTE(TOCTOU): bind→close 直後に同ポートが他プロセスに奪われる可能性が
    ある (TOCTOU race)。relay 起動側で SO_REUSEADDR / fail-fast 検出を持つ
    ため実害は小さいが、CI で連続失敗するようなら ``SO_REUSEPORT`` の活用
    や retry loop を検討する。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_until_healthy(url: str, timeout_sec: float = 10.0) -> bool:
    """``GET {url}/health`` が 200 を返すまでポーリングして待つ。"""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(f"{url}/health", method="GET")
            with urllib.request.urlopen(req, timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.1)
    return False


@pytest.fixture
def live_relay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """別 port で relay サーバーを起動し、ow_service の RELAY_URL を差し替える。"""
    repo_root = Path(__file__).resolve().parents[2]
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    db_path = tmp_path / "relay.db"

    bootstrap = (
        f"import sys; sys.path.insert(0, {repr(str(repo_root))}); "
        f"from src.relay import server; server.PORT = {port}; "
        f"server.main({repr(str(db_path))})"
    )

    env = os.environ.copy()
    env["RELAY_DB"] = str(db_path)

    proc = subprocess.Popen(
        [sys.executable, "-c", bootstrap],
        cwd=str(repo_root),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        if not _wait_until_healthy(base_url, timeout_sec=10.0):
            stdout, stderr = proc.communicate(timeout=2)
            pytest.fail(
                f"relay did not become healthy on port {port}. "
                f"stdout={stdout!r} stderr={stderr!r}"
            )

        monkeypatch.setattr(ow_service, "RELAY_URL", base_url)
        # OW_STATE_DIR を tmp_path 配下に向けて cache JSON を隔離
        state_dir = tmp_path / "cache"
        monkeypatch.setenv("OW_STATE_DIR", str(state_dir))

        yield {"url": base_url, "tmp_path": tmp_path, "state_dir": state_dir}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


def _send_identity(channel: str, alias: str, activity_id: int) -> int:
    body = {
        "v": 1,
        "kind": "event",
        "from": alias,
        "to": "*",
        "data": {
            "type": "identity",
            "role": "worker",
            "handle": alias,
            "alias": alias,
            "activity_id": activity_id,
        },
    }
    result = ow_service.ow_send(channel=channel, handle=alias, body=body)
    assert "msg_id" in result, f"send failed: {result}"
    return result["msg_id"]


def _send_state(channel: str, alias: str, task: str, state: str) -> int:
    body = {
        "v": 1,
        "kind": "event",
        "from": alias,
        "to": "orch",
        "task": task,
        "data": {"type": "state", "state": state},
    }
    result = ow_service.ow_send(channel=channel, handle=alias, body=body)
    assert "msg_id" in result, f"send failed: {result}"
    return result["msg_id"]


def _send_heartbeat(channel: str, alias: str, phase: str = "ready") -> int:
    body = {
        "v": 1,
        "kind": "event",
        "from": alias,
        "to": "*",
        "data": {"type": "heartbeat", "phase": phase},
    }
    result = ow_service.ow_send(channel=channel, handle=alias, body=body)
    assert "msg_id" in result, f"send failed: {result}"
    return result["msg_id"]
