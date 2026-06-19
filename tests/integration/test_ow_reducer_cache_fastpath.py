"""ow reducer cache fastpath (A#911 SP-2 PR-β/γ) の integration test。

実 relay にイベントを投入 → project_state_to_cache で cache 生成 → reducer 4関数
(ow_get_identity / ow_list_identities / ow_get_presence / ow_get_workload_state)
と内部ヘルパー (_query_latest_event / _latest_events_by_type) を呼び、cache 経由で
正しく事象が観測されること、および relay 直 pull は走らないことを確認する。

PR-γ で reducer は relay full pull を撤去したため、本テストでは cache 未生成時の
振る舞い (None / 空) も観測する。
"""
from __future__ import annotations

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
from src.services.ow.cache import (
    CURRENT_SCHEMA_VERSION,
    find_topic_id_by_channel,
    load_state,
)


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


class TestReducerCacheFastpathEndToEnd:
    """relay → projector → cache → reducer の一貫した経路を実I/O で観測する。"""

    def test_ow_get_identity_via_cache_only(self, live_relay):
        """identity event を投入 → project_state_to_cache → ow_get_identity が cache 経由で返る。"""
        channel = "ReducerFastA"
        ow_service.ensure_channel(channel)

        _send_identity(channel, "w-a", activity_id=911)
        _send_state(channel, "w-a", "T1", "ready")
        _send_heartbeat(channel, "w-a", "ready")

        ow_service.project_state_to_cache(topic_id=5001, channel=channel)

        result = ow_service.ow_get_identity(channel, "w-a")
        assert result is not None
        assert result["alias"] == "w-a"
        assert result["activity_id"] == 911

        # cache 経路を通っていることの確認: find_topic_id_by_channel が一致する。
        assert find_topic_id_by_channel(channel) == 5001

    def test_ow_get_presence_online_via_cache(self, live_relay):
        """直近 heartbeat → cache → ow_get_presence で online 判定。"""
        channel = "ReducerFastB"
        ow_service.ensure_channel(channel)

        _send_identity(channel, "w-b", activity_id=1)
        _send_state(channel, "w-b", "T1", "working")
        _send_heartbeat(channel, "w-b", "working")

        ow_service.project_state_to_cache(topic_id=5002, channel=channel)

        result = ow_service.ow_get_presence(channel, "w-b")
        assert result["handle"] == "w-b"
        assert result["status"] == "online"

    def test_ow_get_workload_state_via_cache(self, live_relay):
        """state 遷移を投入 → projector → ow_get_workload_state が最新を返す。"""
        channel = "ReducerFastC"
        ow_service.ensure_channel(channel)

        _send_state(channel, "w-c", "T7", "loading")
        last_state_msg = _send_state(channel, "w-c", "T7", "working")
        _send_heartbeat(channel, "w-c", "working")

        ow_service.project_state_to_cache(topic_id=5003, channel=channel)

        result = ow_service.ow_get_workload_state(channel, "w-c")
        assert result is not None
        assert result["state"] == "working"
        assert result["msg_id"] == last_state_msg

    def test_ow_list_identities_returns_all_via_cache(self, live_relay):
        """複数 worker → projector → ow_list_identities が cache 経由で全件返す。"""
        channel = "ReducerFastD"
        ow_service.ensure_channel(channel)

        for alias in ("w-x", "w-y", "w-z"):
            _send_identity(channel, alias, activity_id=hash(alias) & 0xFFFF)
            _send_state(channel, alias, "T1", "ready")
            _send_heartbeat(channel, alias, "ready")

        ow_service.project_state_to_cache(topic_id=5004, channel=channel)

        result = ow_service.ow_list_identities(channel)
        aliases = sorted(e["alias"] for e in result)
        assert aliases == ["w-x", "w-y", "w-z"]

    def test_reducer_without_cache_returns_empty(self, live_relay):
        """cache を生成しないまま reducer を呼ぶ → relay は叩かず空/None を返す (PR-γ)。"""
        channel = "ReducerFastE"
        ow_service.ensure_channel(channel)

        _send_identity(channel, "w-only", activity_id=42)
        _send_state(channel, "w-only", "T1", "working")
        _send_heartbeat(channel, "w-only", "working")

        # ※ project_state_to_cache を意図的に呼ばない
        assert find_topic_id_by_channel(channel) is None

        assert ow_service.ow_get_identity(channel, "w-only") is None
        assert ow_service.ow_list_identities(channel) == []
        assert ow_service.ow_get_workload_state(channel, "w-only") is None
        presence = ow_service.ow_get_presence(channel, "w-only")
        assert presence["status"] == "unknown"
        assert presence["last_heartbeat_at"] is None


class TestQueryHelpersCacheFastpath:
    """_query_latest_event / _latest_events_by_type の cache 直接観測。"""

    def test_query_latest_event_reads_from_cache_for_state(self, live_relay):
        """projector で書いた cache の states[handle] を _query_latest_event が返す。"""
        channel = "QueryFastA"
        ow_service.ensure_channel(channel)

        _send_state(channel, "w-q", "T1", "loading")
        last_state_msg = _send_state(channel, "w-q", "T1", "working")
        _send_heartbeat(channel, "w-q", "working")

        ow_service.project_state_to_cache(topic_id=5101, channel=channel)

        result = ow_service._query_latest_event(channel, "w-q", "state")
        assert result is not None
        assert result["msg_id"] == last_state_msg
        assert result["body"]["data"]["state"] == "working"

    def test_query_latest_event_unsupported_type_returns_none(self, live_relay):
        """対応しない data_type は cache にもなく None を返す (relay 直叩きしない)。"""
        channel = "QueryFastB"
        ow_service.ensure_channel(channel)

        # relay には arbitrary な event を投入する
        body = {
            "v": 1,
            "kind": "event",
            "from": "w-r",
            "to": "orch",
            "data": {"type": "custom_type", "payload": "x"},
        }
        ow_service.ow_send(channel=channel, handle="w-r", body=body)

        ow_service.project_state_to_cache(topic_id=5102, channel=channel)

        # custom_type は cache 化対象外、reducer は None を返す
        assert ow_service._query_latest_event(channel, "w-r", "custom_type") is None

    def test_latest_events_by_type_reads_cache(self, live_relay):
        """_latest_events_by_type が cache の identity/state/heartbeat を一括取得する。"""
        channel = "QueryFastC"
        ow_service.ensure_channel(channel)

        _send_identity(channel, "w-c", activity_id=7)
        _send_state(channel, "w-c", "T1", "ready")
        _send_heartbeat(channel, "w-c", "ready")

        ow_service.project_state_to_cache(topic_id=5103, channel=channel)

        bundle = ow_service._latest_events_by_type(
            channel, "w-c", ("identity", "state", "heartbeat")
        )
        assert set(bundle.keys()) == {"identity", "state", "heartbeat"}
        assert bundle["identity"]["body"]["data"]["alias"] == "w-c"
        assert bundle["state"]["body"]["data"]["state"] == "ready"
        assert bundle["heartbeat"]["body"]["data"]["phase"] == "ready"

    def test_query_returns_none_when_cache_missing(self, live_relay):
        """cache が未生成なら _query_latest_event は relay を叩かず None を返す。"""
        channel = "QueryFastD"
        ow_service.ensure_channel(channel)
        _send_state(channel, "w-d", "T1", "working")

        # projector を呼ばない
        assert ow_service._query_latest_event(channel, "w-d", "state") is None
