"""hooks/recorder_marker.py のユニットテスト

記録役セッションの目印ファイル判定(is_recorder_attached)を検証する。
subprocess呼び出し(ps)を外部境界としてmonkeypatchする。
"""
import json
import subprocess

import pytest

from hooks.hook_state import HookState
from hooks.recorder_marker import is_recorder_attached, marker_path
from src.services import restart_service

_SESSION_ID = "test-session"


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(HookState, "BASE_DIR", tmp_path)
    return tmp_path


def _write_marker(session_id: str, pid: int, started_at) -> None:
    path = marker_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "started_at": started_at}))


def _fake_ps(lstart_output: str, returncode: int = 0):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode, stdout=lstart_output, stderr="")
    return fake_run


class TestRecorderAttached:
    def test_true_when_pid_alive_and_start_signature_matches(self, state_dir, monkeypatch):
        _write_marker(_SESSION_ID, pid=1234, started_at="Thu Jul 24 09:32:04 2026")
        monkeypatch.setattr(
            restart_service.subprocess, "run", _fake_ps("  Thu Jul 24 09:32:04 2026  \n")
        )

        assert is_recorder_attached(_SESSION_ID) is True

    def test_false_when_pid_dead(self, state_dir, monkeypatch):
        """psがプロセス不在を返す場合 → 付いていない扱い"""
        _write_marker(_SESSION_ID, pid=1234, started_at="Thu Jul 24 09:32:04 2026")
        monkeypatch.setattr(restart_service.subprocess, "run", _fake_ps("", returncode=1))

        assert is_recorder_attached(_SESSION_ID) is False

    def test_false_when_pid_reused_by_different_process(self, state_dir, monkeypatch):
        """同じpidだが起動時刻が目印ファイル記録時と異なる(pid再利用) → 付いていない扱い"""
        _write_marker(_SESSION_ID, pid=1234, started_at="Thu Jul 24 09:32:04 2026")
        monkeypatch.setattr(
            restart_service.subprocess, "run", _fake_ps("Fri Jul 25 10:00:00 2026\n")
        )

        assert is_recorder_attached(_SESSION_ID) is False

    def test_false_when_ps_call_times_out(self, state_dir, monkeypatch):
        """ps呼び出し自体が失敗(タイムアウト) → 催促を出す側に倒す"""
        _write_marker(_SESSION_ID, pid=1234, started_at="Thu Jul 24 09:32:04 2026")

        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

        monkeypatch.setattr(restart_service.subprocess, "run", fake_run)

        assert is_recorder_attached(_SESSION_ID) is False

    def test_false_when_marker_file_missing(self, state_dir):
        assert is_recorder_attached(_SESSION_ID) is False

    def test_false_when_marker_file_is_broken_json(self, state_dir):
        path = marker_path(_SESSION_ID)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json")

        assert is_recorder_attached(_SESSION_ID) is False

    def test_false_when_started_at_missing(self, state_dir, monkeypatch):
        """started_atが無い目印ファイル(壊れた/旧形式) → 付いていない扱い"""
        path = marker_path(_SESSION_ID)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": 1234}))
        monkeypatch.setattr(
            restart_service.subprocess, "run", _fake_ps("Thu Jul 24 09:32:04 2026\n")
        )

        assert is_recorder_attached(_SESSION_ID) is False
