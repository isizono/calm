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
    """Windowsではcreationflags=CREATE_NEW_PROCESS_GROUP|CREATE_NO_WINDOWと
    stdin=DEVNULLを渡し、start_new_sessionは渡さない"""
    monkeypatch.setattr(detached_process.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    proc = detached_process.popen_detached(["echo", "hi"], cwd="/tmp")

    assert proc.kwargs["creationflags"] == (
        detached_process._CREATE_NEW_PROCESS_GROUP | detached_process._CREATE_NO_WINDOW
    )
    assert proc.kwargs["stdin"] == subprocess.DEVNULL
    assert "start_new_session" not in proc.kwargs


def test_windows_creationflags_match_documented_values():
    """定数値がWindows APIの既知値（CREATE_NEW_PROCESS_GROUP=0x200、
    CREATE_NO_WINDOW=0x8000000）と一致すること"""
    assert detached_process._CREATE_NEW_PROCESS_GROUP == 0x00000200
    assert detached_process._CREATE_NO_WINDOW == 0x08000000


def test_args_passed_through_positionally(monkeypatch):
    monkeypatch.setattr(detached_process.sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    proc = detached_process.popen_detached(["python", "-m", "src.main"])

    assert proc.args == ["python", "-m", "src.main"]
