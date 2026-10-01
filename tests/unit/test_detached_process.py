"""src/infra/detached_process.py のユニットテスト

Windows分岐はsys.platformを差し替えることでMac上でも検証する
（実際にWindows版subprocessへ接続するわけではなく、Popenに渡す引数の
組み立てだけを検証する）。
"""
import subprocess

from src.infra import detached_process


class _FakePopen:
    def __init__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs


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


def test_windows_uses_creationflags_and_devnull_stdin(monkeypatch):
    """Windowsではcreationflags=CREATE_NEW_PROCESS_GROUP|CREATE_NO_WINDOW|
    CREATE_BREAKAWAY_FROM_JOBとstdin=DEVNULLを渡し、start_new_sessionは渡さない"""
    monkeypatch.setattr(detached_process.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    proc = detached_process.popen_detached(["echo", "hi"], cwd="/tmp")

    assert proc.kwargs["creationflags"] == (
        detached_process._CREATE_NEW_PROCESS_GROUP
        | detached_process._CREATE_NO_WINDOW
        | detached_process._CREATE_BREAKAWAY_FROM_JOB
    )
    assert proc.kwargs["stdin"] == subprocess.DEVNULL
    assert "start_new_session" not in proc.kwargs


def test_windows_creationflags_match_documented_values():
    """定数値がWindows APIの既知値（CREATE_NEW_PROCESS_GROUP=0x200、
    CREATE_NO_WINDOW=0x8000000、CREATE_BREAKAWAY_FROM_JOB=0x1000000）と一致すること"""
    assert detached_process._CREATE_NEW_PROCESS_GROUP == 0x00000200
    assert detached_process._CREATE_NO_WINDOW == 0x08000000
    assert detached_process._CREATE_BREAKAWAY_FROM_JOB == 0x01000000


def test_windows_retries_without_breakaway_on_access_denied(monkeypatch):
    """BREAKAWAY付きのPopenがwinerror=5のOSErrorを投げたら、BREAKAWAYを外した
    creationflagsで1回だけ再試行すること"""
    monkeypatch.setattr(detached_process.sys, "platform", "win32")
    calls = []

    def fake_popen(args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            err = OSError("access denied")
            err.winerror = 5
            raise err
        return _FakePopen(args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    proc = detached_process.popen_detached(["echo", "hi"], cwd="/tmp")

    assert len(calls) == 2
    assert calls[0]["creationflags"] == (
        detached_process._CREATE_NEW_PROCESS_GROUP
        | detached_process._CREATE_NO_WINDOW
        | detached_process._CREATE_BREAKAWAY_FROM_JOB
    )
    assert calls[1]["creationflags"] == (
        detached_process._CREATE_NEW_PROCESS_GROUP | detached_process._CREATE_NO_WINDOW
    )
    assert proc.kwargs["creationflags"] == calls[1]["creationflags"]


def test_windows_other_oserror_not_retried(monkeypatch):
    """winerror=5以外のOSError（例: winerror=2）は再試行せずそのまま上げること"""
    monkeypatch.setattr(detached_process.sys, "platform", "win32")
    calls = []

    def fake_popen(args, **kwargs):
        calls.append(kwargs)
        err = OSError("not found")
        err.winerror = 2
        raise err

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    try:
        detached_process.popen_detached(["echo", "hi"], cwd="/tmp")
        assert False, "OSErrorが上がってくるはず"
    except OSError as e:
        assert e.winerror == 2

    assert len(calls) == 1


def test_args_passed_through_positionally(monkeypatch):
    monkeypatch.setattr(detached_process.sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    proc = detached_process.popen_detached(["python", "-m", "src.main"])

    assert proc.args == ["python", "-m", "src.main"]
