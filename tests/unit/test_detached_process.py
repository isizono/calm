"""src/infra/detached_process.py のユニットテスト

Windows分岐はsys.platformを差し替えることでMac上でも検証する
（実際にWindows版subprocessへ接続するわけではなく、中継プロセス起動に渡す
引数の組み立てと、中継からの出力の解釈だけを検証する。中継スクリプト自体を
実プロセスとして起動して検証するテストはtests/e2e/test_detached_process_relay.py）。
"""
import json
import os
import subprocess

import psutil
import pytest

from src.infra import detached_process


class _FakePopen:
    def __init__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs


class _FakeRelay:
    """中継プロセス(subprocess.Popen)を模したフェイク。

    communicate()は中継が標準出力に書くpid行を返す。既定では現在の
    テストプロセス自身のpidを返し、psutil.Process()が本物の生存プロセスを
    解決できるようにする。
    """

    def __init__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.returncode = 0
        self.stdout_to_return = f"{os.getpid()}\n".encode()
        self.stderr_to_return = b""
        self.communicate_input = None
        self.communicate_timeout = None
        self.killed = False

    def communicate(self, input=None, timeout=None):
        self.communicate_input = input
        self.communicate_timeout = timeout
        return self.stdout_to_return, self.stderr_to_return

    def kill(self):
        self.killed = True


class _FakePsutilProcess:
    def __init__(self, pid):
        self.pid = pid
        self.terminated = False
        self.killed = False
        self._wait_result = None
        self._wait_exc = None

    def wait(self, timeout=None):
        if self._wait_exc is not None:
            raise self._wait_exc
        return self._wait_result

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def test_posix_uses_start_new_session(monkeypatch):
    """POSIXではstart_new_session=Trueを渡し、Windows専用kwargsは渡さない"""
    monkeypatch.setattr(detached_process.sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    proc = detached_process.popen_detached(["echo", "hi"], cwd="/tmp")

    assert proc.kwargs["start_new_session"] is True
    assert "creationflags" not in proc.kwargs
    assert "stdin" not in proc.kwargs
    assert proc.kwargs["cwd"] == "/tmp"
    assert proc.kwargs["stdout"] == subprocess.DEVNULL
    assert proc.kwargs["stderr"] == subprocess.DEVNULL


def test_windows_creationflags_match_documented_values():
    """定数値がWindows APIの既知値（CREATE_NEW_PROCESS_GROUP=0x200、
    CREATE_NO_WINDOW=0x8000000、CREATE_BREAKAWAY_FROM_JOB=0x1000000）と一致すること"""
    assert detached_process._CREATE_NEW_PROCESS_GROUP == 0x00000200
    assert detached_process._CREATE_NO_WINDOW == 0x08000000
    assert detached_process._CREATE_BREAKAWAY_FROM_JOB == 0x01000000


def test_args_passed_through_positionally(monkeypatch):
    monkeypatch.setattr(detached_process.sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    proc = detached_process.popen_detached(["python", "-m", "src.main"])

    assert proc.args == ["python", "-m", "src.main"]


# ========================================
# Windows分岐: 中継プロセスの起動配線
# ========================================


def test_windows_spawns_relay_with_breakaway_pipes_and_returns_target_pid(monkeypatch):
    """中継はCREATE_BREAKAWAY_FROM_JOB付きでstdin/stdout/stderrをPIPEにして起動され、
    戻り値のpidは中継が報告した本命のpidになる(中継自身のpidではない)"""
    created = []

    def fake_popen(args, **kwargs):
        relay = _FakeRelay(args, **kwargs)
        relay.stdout_to_return = b"424242\n"
        created.append(relay)
        return relay

    monkeypatch.setattr(detached_process.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        detached_process.psutil, "Process", lambda pid: _FakePsutilProcess(pid)
    )

    proc = detached_process.popen_detached(["python", "-m", "src.main"], cwd="/tmp")

    assert len(created) == 1
    relay_kwargs = created[0].kwargs
    assert relay_kwargs["creationflags"] == (
        detached_process._CREATE_NEW_PROCESS_GROUP
        | detached_process._CREATE_NO_WINDOW
        | detached_process._CREATE_BREAKAWAY_FROM_JOB
    )
    assert relay_kwargs["stdin"] == subprocess.PIPE
    assert relay_kwargs["stdout"] == subprocess.PIPE
    assert relay_kwargs["stderr"] == subprocess.PIPE
    assert created[0].args == [detached_process.sys.executable, "-c", detached_process._RELAY_SCRIPT]
    assert proc.pid == 424242


def test_windows_builds_payload_with_shared_stdout_stderr_file(tmp_path, monkeypatch):
    """stdout/stderrに同じファイルオブジェクトが渡された場合、中継へのJSONは
    2重オープンにならない'same_as_stdout'指示に変換すること"""
    created = []

    def fake_popen(args, **kwargs):
        relay = _FakeRelay(args, **kwargs)
        created.append(relay)
        return relay

    monkeypatch.setattr(detached_process.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    log_path = tmp_path / "out.log"
    with open(log_path, "w") as log_file:
        detached_process.popen_detached(
            ["uv", "run", "python", "-m", "src.launcher"],
            cwd=str(tmp_path),
            stdout=log_file,
            stderr=log_file,
        )

    payload = json.loads(created[0].communicate_input.decode("utf-8"))
    assert payload["argv"] == ["uv", "run", "python", "-m", "src.launcher"]
    assert payload["cwd"] == str(tmp_path)
    assert payload["stdout"] == {"kind": "path", "path": str(log_path)}
    assert payload["stderr"] == {"kind": "same_as_stdout"}


def test_windows_devnull_stdout_and_stderr_builds_devnull_payload(monkeypatch):
    created = []

    def fake_popen(args, **kwargs):
        relay = _FakeRelay(args, **kwargs)
        created.append(relay)
        return relay

    monkeypatch.setattr(detached_process.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    detached_process.popen_detached(["python", "-m", "src.infra.embedding_server"])

    payload = json.loads(created[0].communicate_input.decode("utf-8"))
    assert payload["stdout"] == {"kind": "devnull"}
    assert payload["stderr"] == {"kind": "devnull"}
    assert payload["cwd"] is None


def test_windows_unsupported_stream_raises_before_spawning_relay(monkeypatch):
    """DEVNULLでもファイルでもないstream（例: PIPE）は中継を起動する前にValueErrorにする"""
    spawn_calls = []
    monkeypatch.setattr(detached_process.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawn_calls.append(1))

    with pytest.raises(ValueError):
        detached_process.popen_detached(["echo", "hi"], stdout=subprocess.PIPE)

    assert spawn_calls == []


def test_windows_retries_relay_spawn_without_breakaway_on_access_denied(monkeypatch):
    """中継起動がwinerror=5のOSErrorを投げたら、BREAKAWAYを外して1回だけ再試行すること"""
    calls = []

    def fake_popen(args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            err = OSError("access denied")
            err.winerror = 5
            raise err
        return _FakeRelay(args, **kwargs)

    monkeypatch.setattr(detached_process.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    proc = detached_process.popen_detached(["echo", "hi"])

    assert len(calls) == 2
    assert calls[0]["creationflags"] == (
        detached_process._CREATE_NEW_PROCESS_GROUP
        | detached_process._CREATE_NO_WINDOW
        | detached_process._CREATE_BREAKAWAY_FROM_JOB
    )
    assert calls[1]["creationflags"] == (
        detached_process._CREATE_NEW_PROCESS_GROUP | detached_process._CREATE_NO_WINDOW
    )
    assert proc.pid == os.getpid()


def test_windows_relay_spawn_other_oserror_not_retried(monkeypatch):
    """winerror=5以外のOSError（例: winerror=2）は再試行せずそのまま上げること"""
    calls = []

    def fake_popen(args, **kwargs):
        calls.append(kwargs)
        err = OSError("not found")
        err.winerror = 2
        raise err

    monkeypatch.setattr(detached_process.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    with pytest.raises(OSError) as exc_info:
        detached_process.popen_detached(["echo", "hi"])

    assert exc_info.value.winerror == 2
    assert len(calls) == 1


def test_windows_relay_nonzero_returncode_raises_oserror_with_stderr(monkeypatch):
    """中継が非0で終了したら、中継のstderrを含むOSErrorにすること"""
    relay = _FakeRelay(["x"])
    relay.returncode = 1
    relay.stderr_to_return = b"boom: target command not found"

    monkeypatch.setattr(detached_process.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kwargs: relay)

    with pytest.raises(OSError, match="boom: target command not found"):
        detached_process.popen_detached(["echo", "hi"])


def test_windows_relay_empty_stdout_raises_oserror(monkeypatch):
    """中継がpidを1行も書かずに終了コード0で終わった場合も失敗として扱うこと"""
    relay = _FakeRelay(["x"])
    relay.stdout_to_return = b""

    monkeypatch.setattr(detached_process.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kwargs: relay)

    with pytest.raises(OSError):
        detached_process.popen_detached(["echo", "hi"])


def test_windows_relay_timeout_kills_relay_and_raises_oserror(monkeypatch):
    """中継がタイムアウト時間内に応答しなければkillしてOSErrorにすること"""

    class _HangingRelay(_FakeRelay):
        def communicate(self, input=None, timeout=None):
            if not self.killed:
                raise subprocess.TimeoutExpired(cmd=self.args, timeout=timeout)
            return b"", b"relay hung"

    relay = _HangingRelay(["x"])
    monkeypatch.setattr(detached_process.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kwargs: relay)

    with pytest.raises(OSError):
        detached_process.popen_detached(["echo", "hi"])

    assert relay.killed is True


# ========================================
# _stream_spec: stdout/stderr -> JSON変換
# ========================================


def test_stream_spec_devnull():
    assert detached_process._stream_spec(subprocess.DEVNULL) == {"kind": "devnull"}


def test_stream_spec_path(tmp_path):
    with open(tmp_path / "out.log", "wb") as f:
        spec = detached_process._stream_spec(f)
    assert spec == {"kind": "path", "path": str(tmp_path / "out.log")}


def test_stream_spec_same_as_stdout(tmp_path):
    with open(tmp_path / "out.log", "wb") as f:
        spec = detached_process._stream_spec(f, stdout_stream=f)
    assert spec == {"kind": "same_as_stdout"}


def test_stream_spec_unsupported_raises():
    with pytest.raises(ValueError):
        detached_process._stream_spec(subprocess.PIPE)


# ========================================
# _RelayedProcess: psutilベースのPopen相当インターフェース
# ========================================


def test_relayed_process_poll_returns_none_while_running(monkeypatch):
    fake = _FakePsutilProcess(4242)
    fake._wait_exc = psutil.TimeoutExpired(0, 4242)
    monkeypatch.setattr(detached_process.psutil, "Process", lambda pid: fake)

    proc = detached_process._RelayedProcess(4242)

    assert proc.pid == 4242
    assert proc.poll() is None
    assert proc.returncode is None


def test_relayed_process_poll_returns_and_caches_exit_code(monkeypatch):
    fake = _FakePsutilProcess(4242)
    fake._wait_result = 3
    monkeypatch.setattr(detached_process.psutil, "Process", lambda pid: fake)

    proc = detached_process._RelayedProcess(4242)

    assert proc.poll() == 3
    assert proc.returncode == 3
    # 一度確定したら以後はpsutilへ問い合わせずキャッシュ値を返す
    fake._wait_result = 99
    assert proc.poll() == 3


def test_relayed_process_wait_converts_psutil_timeout_to_subprocess_timeout(monkeypatch):
    fake = _FakePsutilProcess(4242)
    fake._wait_exc = psutil.TimeoutExpired(0.1, 4242)
    monkeypatch.setattr(detached_process.psutil, "Process", lambda pid: fake)

    proc = detached_process._RelayedProcess(4242)

    with pytest.raises(subprocess.TimeoutExpired):
        proc.wait(timeout=0.1)


def test_relayed_process_terminate_and_kill_delegate_to_psutil(monkeypatch):
    fake = _FakePsutilProcess(4242)
    monkeypatch.setattr(detached_process.psutil, "Process", lambda pid: fake)

    proc = detached_process._RelayedProcess(4242)
    proc.terminate()
    proc.kill()

    assert fake.terminated is True
    assert fake.killed is True


def test_relayed_process_already_gone_at_construction_does_not_raise(monkeypatch):
    def raise_no_such_process(pid):
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(detached_process.psutil, "Process", raise_no_such_process)

    proc = detached_process._RelayedProcess(4242)

    assert proc.poll() is None
    proc.terminate()
    proc.kill()
