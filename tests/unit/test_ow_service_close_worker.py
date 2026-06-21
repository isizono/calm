"""ow_close_worker の戻り値契約テスト

tmux アダプタが返す stdout (closed / killed / failed) を読み取り、
戻り値の closed / killed bool を組み立てる契約を回帰テストする。
"""
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from src.services import ow_service


@pytest.fixture
def force_tmux_terminal(monkeypatch):
    """OW_TERMINAL=tmux + 実在するアダプタパスにフォールバックする。"""
    monkeypatch.setenv("OW_TERMINAL", "tmux")
    # adapter_path 解決が成功する必要があるが、subprocess.run はモックするので
    # 実行はされない。実在パスを返せばよい。
    yield


def _fake_run_factory(stdout: str, returncode: int = 0, stderr: str = ""):
    def _fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0] if args else [], returncode=returncode, stdout=stdout, stderr=stderr
        )
    return _fake_run


class TestOwCloseWorkerReturnContract:
    def test_returns_closed_true_killed_false_on_closed_stdout(self, force_tmux_terminal):
        with patch("src.services.ow_service.subprocess.run", side_effect=_fake_run_factory("closed\n")):
            result = ow_service.ow_close_worker(term_ref="%42")
        assert result == {"closed": True, "killed": False, "term_ref": "%42"}

    def test_returns_closed_true_killed_true_on_killed_stdout(self, force_tmux_terminal):
        with patch("src.services.ow_service.subprocess.run", side_effect=_fake_run_factory("killed\n")):
            result = ow_service.ow_close_worker(term_ref="%42")
        assert result == {"closed": True, "killed": True, "term_ref": "%42"}

    def test_returns_closed_true_killed_false_when_stdout_empty_legacy(self, force_tmux_terminal):
        """旧アダプタ (stdout 空) は後方互換で closed=True、killed=False (キー欠如しない)。"""
        with patch("src.services.ow_service.subprocess.run", side_effect=_fake_run_factory("")):
            result = ow_service.ow_close_worker(term_ref="%42")
        assert result == {"closed": True, "killed": False, "term_ref": "%42"}

    def test_returns_closed_false_on_called_process_error(self, force_tmux_terminal):
        """adapter exit 1 (failed) では closed=False と error を返す。"""
        err = subprocess.CalledProcessError(returncode=1, cmd=["bash"], stderr="failed\n")

        def _raise(*args, **kwargs):
            raise err

        with patch("src.services.ow_service.subprocess.run", side_effect=_raise):
            result = ow_service.ow_close_worker(term_ref="%42")
        assert result["closed"] is False
        assert result["term_ref"] == "%42"
        assert result["error"]["code"] == "ADAPTER_CLOSE_FAILED"

    def test_returns_closed_false_on_timeout(self, force_tmux_terminal):
        def _timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=["bash"], timeout=15)

        with patch("src.services.ow_service.subprocess.run", side_effect=_timeout):
            result = ow_service.ow_close_worker(term_ref="%42")
        assert result["closed"] is False
        assert result["term_ref"] == "%42"
        assert result["error"]["code"] == "ADAPTER_CLOSE_TIMEOUT"

    def test_uses_last_stdout_line(self, force_tmux_terminal):
        """stdout に複数行ある場合は最終行を判定に使う。"""
        with patch(
            "src.services.ow_service.subprocess.run",
            side_effect=_fake_run_factory("warning: something\nkilled\n"),
        ):
            result = ow_service.ow_close_worker(term_ref="%42")
        assert result == {"closed": True, "killed": True, "term_ref": "%42"}
