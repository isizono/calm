"""recv_poll.sh の動作検証 (pull-polling fallback wrapper)。

SSE push が不発に陥った場合に、/history を周期 pull して自分宛 msg を
Monitor (= 親の Claude Code セッション) に確実に届けるための fallback。
本テストでは recv.sh と並列起動を模さず、recv_poll.sh 単体の動作だけを
確認する (filter / dedup / 差分取得)。
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
_RECV_POLL_SH = _REPO_ROOT / "scripts" / "ow" / "recv_poll.sh"


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_relay(port: int, db_path: str) -> subprocess.Popen:
    env = os.environ.copy()
    env["RELAY_PORT"] = str(port)
    env["RELAY_DB"] = db_path
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


def _create_channel(port: int, code: str) -> str:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/create",
        data=json.dumps({"channel_code": code}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=2.0) as r:
        return json.loads(r.read())["channel_code"]


def _send_msg(port: int, channel: str, handle: str, to: str, seq: int) -> int:
    body = {"v": 1, "kind": "event", "data": {"type": "test"}, "to": to, "seq": seq}
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
    proc = _start_relay(port, db_path)
    try:
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()


def _start_recv_poll(
    port: int, channel: str, handle: str, state_file: str, interval_sec: int = 1
) -> subprocess.Popen:
    env = os.environ.copy()
    env["RELAY_URL"] = f"http://127.0.0.1:{port}"
    env["OW_POLL_INTERVAL_SEC"] = str(interval_sec)
    env["OW_POLL_STATE_FILE"] = state_file
    return subprocess.Popen(
        ["bash", str(_RECV_POLL_SH), channel, handle],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(_REPO_ROOT),
    )


def _extract_seqs(data_lines: list[str]) -> list[int]:
    """`data: {...}` 形式の行群から body 内 seq を抽出する。"""
    seqs: list[int] = []
    for line in data_lines:
        assert line.startswith("data: "), line
        payload = json.loads(line[len("data: "):])
        body = json.loads(payload["body"])
        seqs.append(body["seq"])
    return seqs


class _LineCollector:
    """Process の stdout を別 thread で読み続け、累積を保持する。

    `select + readline` だと text mode の内部バッファが select に
    可視化されない隙で行を取り逃すため、blocking readline を thread に
    乗せて全部回収する形にする。
    """

    def __init__(self, proc: subprocess.Popen) -> None:
        self._proc = proc
        self._lines: list[str] = []
        self._lock = threading.Lock()
        self._t = threading.Thread(target=self._reader, daemon=True)
        self._t.start()

    def _reader(self) -> None:
        assert self._proc.stdout is not None
        for raw in self._proc.stdout:
            with self._lock:
                self._lines.append(raw.rstrip("\n"))

    def wait(self, duration_sec: float) -> None:
        time.sleep(duration_sec)

    def get(self) -> list[str]:
        with self._lock:
            return list(self._lines)

    def reset(self) -> None:
        with self._lock:
            self._lines.clear()


def _stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        proc.kill()


class TestRecvPollFiltering:
    """recv_filter.py と同等の filter が pull 経路にも適用される。"""

    def test_pulls_self_and_broadcast_drops_other(self, isolated_relay, tmp_path):
        """自分宛 + broadcast(to=*) は stdout に出力、別人宛は drop。"""
        port = isolated_relay
        code = _create_channel(port, "poll-filter")
        _send_msg(port, code, "attacker", "victim", seq=1)        # self
        _send_msg(port, code, "attacker", "someone_else", seq=2)  # other (drop 対象)
        _send_msg(port, code, "attacker", "*", seq=3)             # broadcast

        state_file = str(tmp_path / "state")
        proc = _start_recv_poll(port, code, "victim", state_file, interval_sec=1)
        collector = _LineCollector(proc)
        try:
            collector.wait(2.5)
            lines = collector.get()
        finally:
            _stop(proc)

        data_lines = [l for l in lines if l.startswith("data: ")]
        assert len(data_lines) == 2, (
            f"期待 2 行 (self + broadcast)、実 {len(data_lines)} 行: {lines}"
        )
        seqs = _extract_seqs(data_lines)
        assert sorted(seqs) == [1, 3], (
            f"期待 [1, 3] (self + broadcast)、実 {seqs}: 2 (other) が落ちてない / 1 or 3 が落ちた"
        )


class TestRecvPollStateAdvance:
    """state file が advance し、2 周目以降は同じ msg を流さない。"""

    def test_state_file_advances_and_no_duplicate(self, isolated_relay, tmp_path):
        port = isolated_relay
        code = _create_channel(port, "poll-dedup")
        _send_msg(port, code, "attacker", "victim", seq=1)

        state_file = str(tmp_path / "state")
        proc = _start_recv_poll(port, code, "victim", state_file, interval_sec=1)
        collector = _LineCollector(proc)
        try:
            collector.wait(2.0)
            r1 = collector.get()
            # 1 周目で state file 更新済みのはず
            assert Path(state_file).exists(), "state file が作られていない"
            advanced = Path(state_file).read_text().strip()
            assert advanced != "0" and int(advanced) > 0, (
                f"state file の last_msg_id が advance してない: {advanced!r}"
            )
            collector.reset()
            # 2 周目以降は新規 msg なしなので 0 行
            collector.wait(2.0)
            r2 = collector.get()
        finally:
            _stop(proc)

        d1 = [l for l in r1 if l.startswith("data: ")]
        d2 = [l for l in r2 if l.startswith("data: ")]
        assert len(d1) == 1, f"round1 で初回分が来てない: {r1}"
        assert len(d2) == 0, f"round2 で重複出力: {r2}"


class TestRecvPollIncremental:
    """初回 pull 後に投入した新規 msg は次周期で拾える。"""

    def test_new_message_after_first_poll_is_picked_up(self, isolated_relay, tmp_path):
        port = isolated_relay
        code = _create_channel(port, "poll-incr")
        _send_msg(port, code, "attacker", "victim", seq=1)

        state_file = str(tmp_path / "state")
        proc = _start_recv_poll(port, code, "victim", state_file, interval_sec=1)
        collector = _LineCollector(proc)
        try:
            collector.wait(2.0)
            r1 = collector.get()
            collector.reset()
            _send_msg(port, code, "attacker", "victim", seq=99)
            collector.wait(2.5)
            r2 = collector.get()
        finally:
            _stop(proc)

        d1 = [l for l in r1 if l.startswith("data: ")]
        d2 = [l for l in r2 if l.startswith("data: ")]
        assert len(d1) == 1, f"r1: {r1}"
        assert len(d2) == 1, f"新規 msg 拾えてない: r2={r2}"
        assert _extract_seqs(d2) == [99]
