"""ow_service の manual fallback observability テスト。

ow_spawn_worker / ow_close_worker が manual:true を返すすべての分岐で:
- adapter_error フィールドが必ず含まれる
- logger.error が呼ばれる (warning ではない)

ow_spawn_worker が原因不明で manual:true を返したとき、payload に手掛かりが
無いと運用側で原因を辿れない。adapter_error と error ログを構造的に必須化する。
"""
import logging
import subprocess

import pytest

from src.services import ow_service


OW_SERVICE_LOGGER = "src.services.ow_service"


@pytest.fixture(autouse=True)
def _bypass_preflight(monkeypatch):
    """relay/channel/presence/identity の preflight check を全部素通しにする。"""
    monkeypatch.setattr(ow_service, "ensure_relay_server", lambda: True)
    monkeypatch.setattr(ow_service, "ensure_channel", lambda ch: True)
    monkeypatch.setattr(ow_service, "_get_presence", lambda ch: [])
    monkeypatch.setattr(ow_service, "ow_get_identity", lambda ch, h: None)
    monkeypatch.setattr(ow_service, "_ensure_worker_askuser_deny", lambda c: None)


def _build_fake_adapter(tmp_path, terminal: str = "tmux"):
    """tmp_path 配下に terminal アダプタの実体を1つだけ作る。"""
    scripts_dir = tmp_path / "scripts" / "ow" / "adapters"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    adapter = scripts_dir / f"{terminal}.sh"
    adapter.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    adapter.chmod(0o755)
    return adapter


class TestSpawnManualFallbackAdapterPathNone:
    """OW_TERMINAL に対応するアダプタが存在しないとき manual:true を返す分岐。"""

    def test_terminal_unset_payload_contains_adapter_error(
        self, monkeypatch, tmp_path, caplog
    ):
        """OW_TERMINAL 未設定 (= 内部的に 'manual') → manual:true + adapter_error。"""
        monkeypatch.delenv("OW_TERMINAL", raising=False)

        with caplog.at_level(logging.ERROR, logger=OW_SERVICE_LOGGER):
            result = ow_service.ow_spawn_worker(
                alias="w-playbook",
                channel="ch1",
                cwd=str(tmp_path),
                model="claude-opus-4-7",
                task_title="t", acceptance="d", task_n=1,
            )

        assert result.get("manual") is True
        assert "adapter_error" in result
        assert "adapter not found" in result["adapter_error"]
        assert "OW_TERMINAL='manual'" in result["adapter_error"]
        assert any(
            r.levelno == logging.ERROR and "manual fallback" in r.getMessage()
            for r in caplog.records
        )

    def test_unknown_terminal_payload_contains_terminal_name(
        self, monkeypatch, tmp_path, caplog
    ):
        """OW_TERMINAL に登録外の値 → adapter_error に terminal 名が出る。"""
        monkeypatch.setenv("OW_TERMINAL", "unknown-terminal-xyz")

        with caplog.at_level(logging.ERROR, logger=OW_SERVICE_LOGGER):
            result = ow_service.ow_spawn_worker(
                alias="w-playbook",
                channel="ch1",
                cwd=str(tmp_path),
                model="claude-opus-4-7",
                task_title="t", acceptance="d", task_n=1,
            )

        assert result.get("manual") is True
        assert "adapter_error" in result
        assert "unknown-terminal-xyz" in result["adapter_error"]


class TestSpawnManualFallbackSubprocessFailure:
    """adapter は存在するが subprocess.run が失敗するときの manual fallback。"""

    def test_called_process_error_passes_stderr_to_adapter_error_and_logs_error(
        self, monkeypatch, tmp_path, caplog
    ):
        """CalledProcessError → adapter_error=stderr, level=ERROR (warning でない)。"""
        fake_adapter = _build_fake_adapter(tmp_path, terminal="tmux")
        monkeypatch.setattr(
            ow_service,
            "_get_adapter_path",
            lambda terminal: fake_adapter if terminal == "tmux" else None,
        )
        monkeypatch.setenv("OW_TERMINAL", "tmux")

        def fake_run(args, **kwargs):
            raise subprocess.CalledProcessError(
                returncode=2, cmd=args, stderr="boom from adapter"
            )

        monkeypatch.setattr(ow_service.subprocess, "run", fake_run)

        with caplog.at_level(logging.WARNING, logger=OW_SERVICE_LOGGER):
            result = ow_service.ow_spawn_worker(
                alias="w-playbook",
                channel="ch1",
                cwd=str(tmp_path),
                model="claude-opus-4-7",
                task_title="t", acceptance="d", task_n=1,
            )

        assert result.get("manual") is True
        assert result.get("adapter_error") == "boom from adapter"
        # logger.error に格上げされている (manual fallback の warning は残さない)
        warn_msgs = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert not any("adapter spawn failed" in m for m in warn_msgs)
        assert any(
            r.levelno == logging.ERROR and "adapter spawn failed" in r.getMessage()
            for r in caplog.records
        )

    def test_timeout_passes_message_to_adapter_error_and_logs_error(
        self, monkeypatch, tmp_path, caplog
    ):
        """TimeoutExpired → adapter_error 固定文言, level=ERROR。"""
        fake_adapter = _build_fake_adapter(tmp_path, terminal="tmux")
        monkeypatch.setattr(
            ow_service,
            "_get_adapter_path",
            lambda terminal: fake_adapter if terminal == "tmux" else None,
        )
        monkeypatch.setenv("OW_TERMINAL", "tmux")

        def fake_run(args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args, timeout=30)

        monkeypatch.setattr(ow_service.subprocess, "run", fake_run)

        with caplog.at_level(logging.WARNING, logger=OW_SERVICE_LOGGER):
            result = ow_service.ow_spawn_worker(
                alias="w-playbook",
                channel="ch1",
                cwd=str(tmp_path),
                model="claude-opus-4-7",
                task_title="t", acceptance="d", task_n=1,
            )

        assert result.get("manual") is True
        assert result.get("adapter_error") == "adapter spawn timed out"
        warn_msgs = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert not any("timed out" in m for m in warn_msgs)
        assert any(
            r.levelno == logging.ERROR and "timed out" in r.getMessage()
            for r in caplog.records
        )


class TestCloseManualFallback:
    """ow_close_worker: アダプタ不在時の manual:true と adapter_error。"""

    def test_terminal_unset_payload_contains_adapter_error(self, monkeypatch, caplog):
        monkeypatch.delenv("OW_TERMINAL", raising=False)
        with caplog.at_level(logging.ERROR, logger=OW_SERVICE_LOGGER):
            result = ow_service.ow_close_worker(term_ref="%99")
        assert result.get("manual") is True
        assert "adapter_error" in result
        assert "adapter not found" in result["adapter_error"]
        assert any(
            r.levelno == logging.ERROR and "manual fallback" in r.getMessage()
            for r in caplog.records
        )

    def test_unknown_terminal_payload_contains_terminal_name(
        self, monkeypatch, caplog
    ):
        monkeypatch.setenv("OW_TERMINAL", "unknown-terminal-xyz")
        with caplog.at_level(logging.ERROR, logger=OW_SERVICE_LOGGER):
            result = ow_service.ow_close_worker(term_ref="%99")
        assert result.get("manual") is True
        assert "unknown-terminal-xyz" in result["adapter_error"]


class TestCloseAdapterFailureLogs:
    """ow_close_worker の adapter 起動失敗時の logger.error 格上げ検証。"""

    def test_called_process_error_logs_error(self, monkeypatch, tmp_path, caplog):
        fake_adapter = _build_fake_adapter(tmp_path, terminal="tmux")
        monkeypatch.setattr(
            ow_service,
            "_get_adapter_path",
            lambda terminal: fake_adapter if terminal == "tmux" else None,
        )
        monkeypatch.setenv("OW_TERMINAL", "tmux")

        def fake_run(args, **kwargs):
            raise subprocess.CalledProcessError(
                returncode=3, cmd=args, stderr="close failed"
            )

        monkeypatch.setattr(ow_service.subprocess, "run", fake_run)

        with caplog.at_level(logging.WARNING, logger=OW_SERVICE_LOGGER):
            result = ow_service.ow_close_worker(term_ref="%99")

        assert result["error"]["code"] == "ADAPTER_CLOSE_FAILED"
        warn_msgs = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert not any("adapter close failed" in m for m in warn_msgs)
        assert any(
            r.levelno == logging.ERROR and "adapter close failed" in r.getMessage()
            for r in caplog.records
        )

    def test_timeout_logs_error(self, monkeypatch, tmp_path, caplog):
        fake_adapter = _build_fake_adapter(tmp_path, terminal="tmux")
        monkeypatch.setattr(
            ow_service,
            "_get_adapter_path",
            lambda terminal: fake_adapter if terminal == "tmux" else None,
        )
        monkeypatch.setenv("OW_TERMINAL", "tmux")

        def fake_run(args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args, timeout=15)

        monkeypatch.setattr(ow_service.subprocess, "run", fake_run)

        with caplog.at_level(logging.WARNING, logger=OW_SERVICE_LOGGER):
            result = ow_service.ow_close_worker(term_ref="%99")

        assert result["error"]["code"] == "ADAPTER_CLOSE_TIMEOUT"
        warn_msgs = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert not any("close timed out" in m for m in warn_msgs)
        assert any(
            r.levelno == logging.ERROR and "close timed out" in r.getMessage()
            for r in caplog.records
        )
