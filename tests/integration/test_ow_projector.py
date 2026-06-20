"""ow projector 経路 (A#911 SP-2 PR-α) の integration test。

実 relay サーバープロセスを起動して identity / state / heartbeat envelope を
ow_send で投入し、project_state_to_cache → cache JSON → load_state の round-trip と
自動 fallback (cache miss / corruption / channel mismatch) を実 I/O 経由で検証する。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.services import ow_service
from src.services.ow.cache import CURRENT_SCHEMA_VERSION, load_state


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_until_healthy(url: str, timeout_sec: float = 10.0) -> bool:
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


class TestProjectorRoundTrip:
    """relay events を ow_send で投入 → project_state_to_cache → load_state で round-trip。"""

    def test_relay_to_cache_to_load_round_trip(self, live_relay):
        """実 relay に投入した identity/state/heartbeat が cache → load_state まで一貫して保持される。"""
        channel = "TestProjA"
        ow_service.ensure_channel(channel)

        _send_identity(channel, "w-a", activity_id=911)
        _send_state(channel, "w-a", "T97", "ready")
        last_hb = _send_heartbeat(channel, "w-a", "ready")

        result = ow_service.project_state_to_cache(topic_id=4001, channel=channel)

        assert result is not None
        assert result["schema_version"] == CURRENT_SCHEMA_VERSION
        assert result["channel"] == channel
        assert result["last_msg_id"] == last_hb
        assert result["workers"]["w-a"]["state"] == "ready"
        assert result["workers"]["w-a"]["task"] == "T97"
        assert result["identities"]["w-a"]["alias"] == "w-a"
        # heartbeat 直後なので presence に乗る
        assert "w-a" in result["presence"]

        # cache JSON ファイルが書き出されている
        cache_path = live_relay["state_dir"] / "topic-4001.json"
        assert cache_path.exists()
        on_disk = json.loads(cache_path.read_text(encoding="utf-8"))
        assert on_disk["channel"] == channel
        assert on_disk["last_msg_id"] == last_hb

        # load_state で読み戻すと OwState が一致する
        loaded = load_state(topic_id=4001, channel=channel)
        assert loaded is not None
        assert loaded["channel"] == channel
        assert loaded["last_msg_id"] == last_hb
        assert loaded["workers"]["w-a"]["state"] == "ready"

    def test_multiple_workers_state_aggregated(self, live_relay):
        """複数 worker / 複数 state 遷移が handle 単位で最新値に集約される。"""
        channel = "TestProjB"
        ow_service.ensure_channel(channel)

        _send_identity(channel, "w-x", activity_id=1)
        _send_state(channel, "w-x", "T1", "ready")
        _send_state(channel, "w-x", "T1", "working")
        last_x = _send_heartbeat(channel, "w-x", "ready")

        _send_identity(channel, "w-y", activity_id=2)
        _send_state(channel, "w-y", "T2", "ready")
        _send_state(channel, "w-y", "T2", "working")
        _send_state(channel, "w-y", "T2", "done")
        _send_heartbeat(channel, "w-y", "ready")

        result = ow_service.project_state_to_cache(topic_id=4002, channel=channel)

        assert result is not None
        assert result["workers"]["w-x"]["state"] == "working"
        assert result["workers"]["w-y"]["state"] == "done"
        assert "w-x" in result["identities"]
        assert "w-y" in result["identities"]


class TestGetOrRebuildStateIntegration:
    """get_or_rebuild_state の自動 fallback を実 relay 経由で検証。"""

    def test_fallback_when_cache_missing(self, live_relay):
        """cache 未生成の状態で get_or_rebuild_state を呼ぶと relay full pull で再構築される。"""
        channel = "TestProjC"
        ow_service.ensure_channel(channel)

        _send_identity(channel, "w-m", activity_id=3)
        _send_state(channel, "w-m", "T3", "working")
        _send_heartbeat(channel, "w-m", "ready")

        cache_path = live_relay["state_dir"] / "topic-4003.json"
        assert not cache_path.exists()

        result = ow_service.get_or_rebuild_state(4003, channel)

        assert result is not None
        assert result["channel"] == channel
        assert result["workers"]["w-m"]["state"] == "working"
        assert cache_path.exists()

    def test_fallback_when_cache_corrupt(self, live_relay):
        """壊れた cache JSON を置いた状態で呼ぶと、load_state が削除 → projector で再構築される。"""
        channel = "TestProjD"
        ow_service.ensure_channel(channel)

        _send_identity(channel, "w-n", activity_id=4)
        _send_state(channel, "w-n", "T4", "done")
        _send_heartbeat(channel, "w-n", "ready")

        state_dir = live_relay["state_dir"]
        state_dir.mkdir(parents=True, exist_ok=True)
        cache_path = state_dir / "topic-4004.json"
        cache_path.write_text("{not valid json", encoding="utf-8")

        result = ow_service.get_or_rebuild_state(4004, channel)

        assert result is not None
        assert result["channel"] == channel
        # 再構築後の cache JSON が読める
        on_disk = json.loads(cache_path.read_text(encoding="utf-8"))
        assert on_disk["channel"] == channel
        assert on_disk["workers"]["w-n"]["state"] == "done"

    def test_fallback_when_channel_mismatches(self, live_relay):
        """異なる channel の cache が残っている場合、load_state が削除 → 新 channel で再構築される。"""
        from src.services.ow.cache import save_state

        channel_new = "TestProjE-new"
        ow_service.ensure_channel(channel_new)

        _send_identity(channel_new, "w-p", activity_id=5)
        _send_state(channel_new, "w-p", "T5", "ready")
        _send_heartbeat(channel_new, "w-p", "ready")

        state_dir = live_relay["state_dir"]
        state_dir.mkdir(parents=True, exist_ok=True)
        save_state(
            topic_id=4005,
            state={
                "schema_version": CURRENT_SCHEMA_VERSION,
                "channel": "TestProjE-old",
                "last_msg_id": 1,
                "workers": {"w-stale": {"state": "ready", "task": "T0"}},
                "identities": {},
                "presence": [],
                "updated_at": "2026-06-19T10:00:00+00:00",
            },
        )

        result = ow_service.get_or_rebuild_state(4005, channel_new)

        assert result is not None
        assert result["channel"] == channel_new
        # 旧 channel の workers エントリが消えて新 channel の events に差し替わる
        assert "w-stale" not in result["workers"]
        assert "w-p" in result["workers"]
        assert result["workers"]["w-p"]["state"] == "ready"
