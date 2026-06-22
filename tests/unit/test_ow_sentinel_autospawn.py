"""ow_service.ensure_sentinel_process の auto-start ロジックを検証する unit test。

PR #432 で導入された「ow_status 呼び出し時に sentinel.py を channel ごと 1 プロセス
起動する」配線の振る舞いを確認する。

- 既に走っていれば spawn しない (pgrep でヒット時)
- 走っていなければ subprocess.Popen で spawn する
- OW_SKIP_SENTINEL_AUTOSPAWN=1 で skip
- spawn 失敗 (OSError) は logger.warning に流して False を返すだけ (例外を伝播しない)
"""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from src.services import ow_service


@pytest.fixture
def unset_skip_env(monkeypatch):
    """テストで OW_SKIP_SENTINEL_AUTOSPAWN が外部から渡っていても落とす。"""
    monkeypatch.delenv("OW_SKIP_SENTINEL_AUTOSPAWN", raising=False)


def _completed_process(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


class TestIsSentinelRunning:
    def test_returns_true_when_pgrep_hits(self, unset_skip_env):
        with patch("src.services.ow_service.subprocess.run") as mock_run:
            mock_run.return_value = _completed_process(stdout="12345\n", returncode=0)
            assert ow_service._is_sentinel_running("CH123") is True

    def test_returns_false_when_pgrep_no_match(self, unset_skip_env):
        with patch("src.services.ow_service.subprocess.run") as mock_run:
            mock_run.return_value = _completed_process(stdout="", returncode=1)
            assert ow_service._is_sentinel_running("CH123") is False

    def test_returns_false_when_pgrep_missing(self, unset_skip_env):
        with patch("src.services.ow_service.subprocess.run", side_effect=FileNotFoundError):
            assert ow_service._is_sentinel_running("CH123") is False

    def test_returns_false_on_timeout(self, unset_skip_env):
        with patch(
            "src.services.ow_service.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="pgrep", timeout=2),
        ):
            assert ow_service._is_sentinel_running("CH123") is False

    def test_pgrep_pattern_uses_trailing_anchor(self, unset_skip_env):
        """`ow1` を渡したとき `ow10` / `ow100` などの prefix 衝突で誤検知しないこと。"""
        with patch("src.services.ow_service.subprocess.run") as mock_run:
            mock_run.return_value = _completed_process(stdout="", returncode=1)
            ow_service._is_sentinel_running("ow1")
            args, _ = mock_run.call_args
            cmd = args[0]
            # cmd = ["pgrep", "-f", "<pattern>"]
            assert cmd[0] == "pgrep"
            assert cmd[-1].endswith("ow1$"), f"pattern must end with $ anchor, got: {cmd[-1]!r}"


class TestEnsureSentinelProcess:
    def test_spawns_when_not_running(self, unset_skip_env, tmp_path, monkeypatch):
        # log open 先を tmp_path に逃がして /tmp を汚さない
        monkeypatch.setattr(
            ow_service, "_sentinel_log_path", lambda code: tmp_path / f"sentinel-{code}.log"
        )
        with patch("src.services.ow_service._is_sentinel_running", return_value=False), \
             patch("src.services.ow_service._SENTINEL_SCRIPT") as mock_script, \
             patch("src.services.ow_service.subprocess.Popen") as mock_popen:
            mock_script.is_file.return_value = True
            mock_script.__str__ = lambda self: "/path/to/sentinel.py"
            assert ow_service.ensure_sentinel_process("CH123") is True
            mock_popen.assert_called_once()
            # spawn 引数の末尾に channel_code が乗ること
            args, _ = mock_popen.call_args
            cmd = args[0]
            assert cmd[-1] == "CH123"

    def test_spawn_uses_uv_run_with_project_directory(self, unset_skip_env, tmp_path, monkeypatch):
        """hooks/hooks.json と一貫させるため `uv run --directory <root> python ...` で起動すること。"""
        monkeypatch.setattr(
            ow_service, "_sentinel_log_path", lambda code: tmp_path / f"sentinel-{code}.log"
        )
        with patch("src.services.ow_service._is_sentinel_running", return_value=False), \
             patch("src.services.ow_service._SENTINEL_SCRIPT") as mock_script, \
             patch("src.services.ow_service.subprocess.Popen") as mock_popen:
            mock_script.is_file.return_value = True
            ow_service.ensure_sentinel_process("CH123")
            args, _ = mock_popen.call_args
            cmd = args[0]
            assert cmd[0] == "uv"
            assert cmd[1] == "run"
            assert "--directory" in cmd
            assert "python" in cmd
            assert "scripts/ow/sentinel.py" in cmd

    def test_spawn_redirects_stderr_to_log_file(self, unset_skip_env, tmp_path, monkeypatch):
        """sentinel の stderr を /tmp/sentinel-<channel>.log に追記すること。"""
        log_path = tmp_path / "sentinel-CH123.log"
        monkeypatch.setattr(ow_service, "_sentinel_log_path", lambda code: log_path)
        with patch("src.services.ow_service._is_sentinel_running", return_value=False), \
             patch("src.services.ow_service._SENTINEL_SCRIPT") as mock_script, \
             patch("src.services.ow_service.subprocess.Popen") as mock_popen:
            mock_script.is_file.return_value = True
            ow_service.ensure_sentinel_process("CH123")
            _, kwargs = mock_popen.call_args
            stderr = kwargs.get("stderr")
            # DEVNULL ではなく実ファイル fd が渡されること
            assert stderr is not subprocess.DEVNULL
            assert stderr is not None
            # ログファイルが open 済みであること (Popen に渡した時点で create される)
            assert log_path.exists()

    def test_skips_when_already_running(self, unset_skip_env):
        with patch("src.services.ow_service._is_sentinel_running", return_value=True), \
             patch("src.services.ow_service._SENTINEL_SCRIPT") as mock_script, \
             patch("src.services.ow_service.subprocess.Popen") as mock_popen:
            mock_script.is_file.return_value = True
            assert ow_service.ensure_sentinel_process("CH123") is True
            mock_popen.assert_not_called()

    def test_skips_when_env_set(self, monkeypatch):
        monkeypatch.setenv("OW_SKIP_SENTINEL_AUTOSPAWN", "1")
        with patch("src.services.ow_service.subprocess.Popen") as mock_popen:
            assert ow_service.ensure_sentinel_process("CH123") is False
            mock_popen.assert_not_called()

    def test_skips_when_sentinel_script_missing(self, unset_skip_env):
        with patch("src.services.ow_service._SENTINEL_SCRIPT") as mock_script, \
             patch("src.services.ow_service.subprocess.Popen") as mock_popen:
            mock_script.is_file.return_value = False
            assert ow_service.ensure_sentinel_process("CH123") is False
            mock_popen.assert_not_called()

    def test_returns_false_and_does_not_raise_on_oserror(self, unset_skip_env, tmp_path, monkeypatch):
        monkeypatch.setattr(
            ow_service, "_sentinel_log_path", lambda code: tmp_path / f"sentinel-{code}.log"
        )
        with patch("src.services.ow_service._is_sentinel_running", return_value=False), \
             patch("src.services.ow_service._SENTINEL_SCRIPT") as mock_script, \
             patch(
                 "src.services.ow_service.subprocess.Popen",
                 side_effect=OSError("fork failed"),
             ):
            mock_script.is_file.return_value = True
            # 例外を伝播しないこと
            assert ow_service.ensure_sentinel_process("CH123") is False
