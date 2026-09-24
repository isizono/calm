"""hooks/recorder_marker.py のユニットテスト

記録役セッションの目印ファイルの読み書き(write_marker/remove_marker)と
生存判定(is_recorder_attached)を検証する。subprocess呼び出し(ps)を外部境界
としてmonkeypatchする。目印ファイルは可能な限りwrite_marker経由で実際に書き、
実際に書かれうる形のファイルでテストする。
"""
import json
import subprocess

import pytest

from hooks.hook_state import HookState
from hooks.recorder_marker import (
    is_recorder_attached,
    marker_path,
    remove_marker,
    write_marker,
)
from src.infra import process_signature

_SESSION_ID = "test-session"


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(HookState, "BASE_DIR", tmp_path)
    return tmp_path


def _fake_ps(lstart_output: str, returncode: int = 0):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode, stdout=lstart_output, stderr="")
    return fake_run


def _write_marker_with_ps_output(session_id: str, pid: int, ps_output: str, monkeypatch) -> None:
    """write_marker経由で目印ファイルを書く。write_marker自身がps -o lstart=を
    呼ぶため、書き込み時点の出力をmonkeypatchで固定する。"""
    monkeypatch.setattr(process_signature.subprocess, "run", _fake_ps(ps_output))
    write_marker(session_id, pid)


class TestWriteMarker:
    def test_writes_pid_and_started_at(self, state_dir, monkeypatch):
        _write_marker_with_ps_output(
            _SESSION_ID, 1234, "Thu Jul 24 09:32:04 2026\n", monkeypatch
        )

        data = json.loads(marker_path(_SESSION_ID).read_text())
        assert data == {"pid": 1234, "started_at": "Thu Jul 24 09:32:04 2026"}

    def test_creates_marker_dir_if_missing(self, state_dir, monkeypatch):
        assert not marker_path(_SESSION_ID).parent.exists()

        _write_marker_with_ps_output(
            _SESSION_ID, 1234, "Thu Jul 24 09:32:04 2026\n", monkeypatch
        )

        assert marker_path(_SESSION_ID).exists()

    def test_started_at_none_when_ps_fails(self, state_dir, monkeypatch):
        """ps呼び出し失敗時はstarted_at=Noneのまま保存される
        (以後の照合は必ず不一致になり「付いていない」扱いになる)"""
        monkeypatch.setattr(process_signature.subprocess, "run", _fake_ps("", returncode=1))

        write_marker(_SESSION_ID, 1234)

        data = json.loads(marker_path(_SESSION_ID).read_text())
        assert data == {"pid": 1234, "started_at": None}


class TestRemoveMarker:
    def test_removes_existing_marker(self, state_dir, monkeypatch):
        _write_marker_with_ps_output(
            _SESSION_ID, 1234, "Thu Jul 24 09:32:04 2026\n", monkeypatch
        )
        assert marker_path(_SESSION_ID).exists()

        remove_marker(_SESSION_ID)

        assert not marker_path(_SESSION_ID).exists()

    def test_no_error_when_marker_missing(self, state_dir):
        remove_marker(_SESSION_ID)  # 例外を出さない


class TestRecorderAttached:
    def test_true_when_pid_alive_and_start_signature_matches(self, state_dir, monkeypatch):
        _write_marker_with_ps_output(
            _SESSION_ID, 1234, "  Thu Jul 24 09:32:04 2026  \n", monkeypatch
        )

        assert is_recorder_attached(_SESSION_ID) is True

    def test_false_when_pid_dead(self, state_dir, monkeypatch):
        """psがプロセス不在を返す場合 → 付いていない扱い"""
        _write_marker_with_ps_output(
            _SESSION_ID, 1234, "Thu Jul 24 09:32:04 2026\n", monkeypatch
        )
        monkeypatch.setattr(process_signature.subprocess, "run", _fake_ps("", returncode=1))

        assert is_recorder_attached(_SESSION_ID) is False

    def test_marker_file_removed_when_pid_dead(self, state_dir, monkeypatch):
        """記録役が死んでいると判定した場合、目印ファイル自体を削除する
        (溜まり続けるのを防ぐ)"""
        _write_marker_with_ps_output(
            _SESSION_ID, 1234, "Thu Jul 24 09:32:04 2026\n", monkeypatch
        )
        monkeypatch.setattr(process_signature.subprocess, "run", _fake_ps("", returncode=1))

        is_recorder_attached(_SESSION_ID)

        assert not marker_path(_SESSION_ID).exists()

    def test_false_when_pid_reused_by_different_process(self, state_dir, monkeypatch):
        """同じpidだが起動時刻が目印ファイル記録時と異なる(pid再利用) → 付いていない扱い"""
        _write_marker_with_ps_output(
            _SESSION_ID, 1234, "Thu Jul 24 09:32:04 2026\n", monkeypatch
        )
        monkeypatch.setattr(
            process_signature.subprocess, "run", _fake_ps("Fri Jul 25 10:00:00 2026\n")
        )

        assert is_recorder_attached(_SESSION_ID) is False

    def test_marker_file_removed_when_pid_reused(self, state_dir, monkeypatch):
        _write_marker_with_ps_output(
            _SESSION_ID, 1234, "Thu Jul 24 09:32:04 2026\n", monkeypatch
        )
        monkeypatch.setattr(
            process_signature.subprocess, "run", _fake_ps("Fri Jul 25 10:00:00 2026\n")
        )

        is_recorder_attached(_SESSION_ID)

        assert not marker_path(_SESSION_ID).exists()

    def test_false_when_ps_call_times_out(self, state_dir, monkeypatch):
        """ps呼び出し自体が失敗(タイムアウト) → 催促を出す側に倒す"""
        _write_marker_with_ps_output(
            _SESSION_ID, 1234, "Thu Jul 24 09:32:04 2026\n", monkeypatch
        )

        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

        monkeypatch.setattr(process_signature.subprocess, "run", fake_run)

        assert is_recorder_attached(_SESSION_ID) is False

    def test_false_when_marker_file_missing(self, state_dir):
        assert is_recorder_attached(_SESSION_ID) is False

    def test_false_when_marker_file_is_broken_json(self, state_dir):
        path = marker_path(_SESSION_ID)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json")

        assert is_recorder_attached(_SESSION_ID) is False

    def test_broken_json_marker_is_not_deleted(self, state_dir):
        """壊れたJSON・想定外の例外の場合は『死んでいる』と確定できないため
        削除しない(削除するのは死亡を確定できた場合のみ)"""
        path = marker_path(_SESSION_ID)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json")

        is_recorder_attached(_SESSION_ID)

        assert path.exists()

    def test_false_when_started_at_missing(self, state_dir, monkeypatch):
        """started_atが無い目印ファイル(壊れた/旧形式) → 付いていない扱い"""
        path = marker_path(_SESSION_ID)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": 1234}))
        monkeypatch.setattr(
            process_signature.subprocess, "run", _fake_ps("Thu Jul 24 09:32:04 2026\n")
        )

        assert is_recorder_attached(_SESSION_ID) is False

    def test_false_when_unexpected_exception_occurs(self, state_dir, monkeypatch):
        """ps呼び出し中にTimeoutExpired以外の予期しない例外が出た場合も
        (importの失敗を含め)催促を出す側に倒す。"""
        _write_marker_with_ps_output(
            _SESSION_ID, 1234, "Thu Jul 24 09:32:04 2026\n", monkeypatch
        )

        def fake_run(cmd, **kwargs):
            raise RuntimeError("unexpected ps failure")

        monkeypatch.setattr(process_signature.subprocess, "run", fake_run)

        assert is_recorder_attached(_SESSION_ID) is False

    def test_marker_not_deleted_when_unexpected_exception_occurs(self, state_dir, monkeypatch):
        """判定を確定できない(予期しない例外)場合はファイルを消さない"""
        _write_marker_with_ps_output(
            _SESSION_ID, 1234, "Thu Jul 24 09:32:04 2026\n", monkeypatch
        )

        def fake_run(cmd, **kwargs):
            raise RuntimeError("unexpected ps failure")

        monkeypatch.setattr(process_signature.subprocess, "run", fake_run)

        is_recorder_attached(_SESSION_ID)

        assert marker_path(_SESSION_ID).exists()
