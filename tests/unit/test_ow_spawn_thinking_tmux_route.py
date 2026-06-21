"""ow_spawn_worker: 思考worker (effort 指定) の tmux new-window routing テスト。

effort 指定の有無にかかわらず worker spawn は tmux 経路を通る。思考worker の場合は
is_thinking=1 を渡して `tmux new-window` で別タブを開く。

- effort 指定 + OW_TERMINAL=tmux → tmux アダプタ + is_thinking=1
- effort 未指定 + OW_TERMINAL=tmux → tmux アダプタ + is_thinking=0
- tmux アダプタが返す term_ref (`%NNN`) がそのまま spawn 戻り値に乗る
- term_ref は tmux pane ID 形式に分類され、UUID 形式は invalid と判定される (契約)
- ow_close_worker: term_ref の形式に関わらず OW_TERMINAL に従う
- OW_TERMINAL 未設定時のデフォルトは "tmux" (manual fallback 不発)
"""
import subprocess
from pathlib import Path

import pytest

from src.services import ow_service


@pytest.fixture(autouse=True)
def _bypass_preflight(monkeypatch):
    monkeypatch.setattr(ow_service, "ensure_relay_server", lambda: True)
    monkeypatch.setattr(ow_service, "ensure_channel", lambda ch: True)
    monkeypatch.setattr(ow_service, "_get_presence", lambda ch: [])
    monkeypatch.setattr(ow_service, "ow_get_identity", lambda ch, h: None)
    monkeypatch.setattr(ow_service, "_ensure_worker_askuser_deny", lambda c: None)
    monkeypatch.setattr(
        ow_service,
        "_relay_request",
        lambda *args, **kwargs: {"msg_id": 0},
    )


def _make_tmux_adapter(tmp_path: Path) -> Path:
    scripts_dir = tmp_path / "scripts" / "ow" / "adapters"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    p = scripts_dir / "tmux.sh"
    p.write_text("#!/bin/sh\necho %42\n", encoding="utf-8")
    p.chmod(0o755)
    return p


def _patch_adapter_lookup(monkeypatch, tmux_path: Path):
    monkeypatch.setattr(
        ow_service,
        "_get_adapter_path",
        lambda terminal: tmux_path if terminal == "tmux" else None,
    )


class TestSpawnThinkingRoutesToTmuxNewWindow:
    """OW_TERMINAL=tmux + effort 指定 → tmux アダプタ + is_thinking=1。"""

    def test_effort_specified_calls_tmux_adapter_with_is_thinking_1(
        self, monkeypatch, tmp_path
    ):
        tmux_path = _make_tmux_adapter(tmp_path)
        _patch_adapter_lookup(monkeypatch, tmux_path)
        monkeypatch.setenv("OW_STATE_DIR", str(tmp_path))
        monkeypatch.setenv("OW_TERMINAL", "tmux")

        captured: dict = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="%101\n", stderr=""
            )

        monkeypatch.setattr(ow_service.subprocess, "run", fake_run)

        result = ow_service.ow_spawn_worker(
            alias="w-thinking",
            channel="ch1",
            cwd=str(tmp_path),
            model="claude-opus-4-7",
            task_title="t",
            acceptance="a",
            task_n=1,
            effort="high",
        )

        assert result.get("spawning") == "ok"
        assert result.get("term_ref") == "%101"
        assert str(tmux_path) in captured["args"]
        # target_pane 未指定の思考 worker は空文字プレースホルダ + is_thinking=1 を末尾に届ける。
        # 末尾2件は [target_pane_placeholder, is_thinking_flag] の位置順。
        assert captured["args"][-2:] == ["", "1"]

    def test_effort_unspecified_calls_tmux_adapter_with_is_thinking_0(
        self, monkeypatch, tmp_path
    ):
        tmux_path = _make_tmux_adapter(tmp_path)
        _patch_adapter_lookup(monkeypatch, tmux_path)
        monkeypatch.setenv("OW_STATE_DIR", str(tmp_path))
        monkeypatch.setenv("OW_TERMINAL", "tmux")

        captured: dict = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="%99\n", stderr=""
            )

        monkeypatch.setattr(ow_service.subprocess, "run", fake_run)

        result = ow_service.ow_spawn_worker(
            alias="w-normal",
            channel="ch1",
            cwd=str(tmp_path),
            model="claude-opus-4-7",
            task_title="t",
            acceptance="a",
            task_n=1,
            tmux_target_pane="%0",
        )

        assert result.get("spawning") == "ok"
        assert str(tmux_path) in captured["args"]
        # 末尾2件は [target_pane, is_thinking_flag] の位置順。通常 worker は is_thinking=0。
        assert captured["args"][-2:] == ["%0", "0"]


class TestSpawnReturnsTmuxPaneIdFormat:
    """思考 worker spawn 時の term_ref は tmux pane ID 形式であり、UUID 形式は返らない契約。"""

    def test_thinking_worker_term_ref_is_tmux_pane_id(self, monkeypatch, tmp_path):
        tmux_path = _make_tmux_adapter(tmp_path)
        _patch_adapter_lookup(monkeypatch, tmux_path)
        monkeypatch.setenv("OW_STATE_DIR", str(tmp_path))
        monkeypatch.setenv("OW_TERMINAL", "tmux")

        def fake_run(args, **kwargs):
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="%151\n", stderr=""
            )

        monkeypatch.setattr(ow_service.subprocess, "run", fake_run)

        result = ow_service.ow_spawn_worker(
            alias="w-thinking",
            channel="ch1",
            cwd=str(tmp_path),
            model="claude-opus-4-7",
            task_title="t",
            acceptance="a",
            task_n=1,
            effort="ultrathink",
        )

        term_ref = result.get("term_ref")
        assert term_ref == "%151"
        assert ow_service.classify_term_ref(term_ref) == "tmux"
        assert ow_service.is_valid_term_ref(term_ref) is True

    def test_uuid_format_is_not_valid_term_ref(self):
        """UUID 形式は classify_term_ref で invalid (認める形式は tmux / manual のみ)。"""
        uuid_value = "4ED18320-BF6C-4577-9FF2-C35065061FA3"
        assert ow_service.classify_term_ref(uuid_value) is None
        assert ow_service.is_valid_term_ref(uuid_value) is False


class TestCloseWorkerSingleBranch:
    """ow_close_worker: term_ref の形式判定による分岐は廃止され、OW_TERMINAL のみで決まる。"""

    def test_tmux_pane_term_ref_routes_to_tmux_adapter(self, monkeypatch, tmp_path):
        tmux_path = _make_tmux_adapter(tmp_path)
        _patch_adapter_lookup(monkeypatch, tmux_path)
        monkeypatch.setenv("OW_TERMINAL", "tmux")

        captured: dict = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="", stderr=""
            )

        monkeypatch.setattr(ow_service.subprocess, "run", fake_run)

        result = ow_service.ow_close_worker(term_ref="%42")
        assert result.get("closed") is True
        assert str(tmux_path) in captured["args"]

    def test_close_worker_default_terminal_is_tmux(self, monkeypatch, tmp_path):
        """OW_TERMINAL 未設定時のデフォルトは "tmux" (manual fallback ではない)。"""
        tmux_path = _make_tmux_adapter(tmp_path)
        _patch_adapter_lookup(monkeypatch, tmux_path)
        monkeypatch.delenv("OW_TERMINAL", raising=False)

        captured: dict = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="", stderr=""
            )

        monkeypatch.setattr(ow_service.subprocess, "run", fake_run)

        result = ow_service.ow_close_worker(term_ref="%42")
        assert result.get("closed") is True
        assert str(tmux_path) in captured["args"]

    def test_close_worker_manual_explicit_returns_manual_fallback(
        self, monkeypatch, tmp_path
    ):
        """明示的に OW_TERMINAL=manual を指定したケースは従来通り manual fallback。"""
        _patch_adapter_lookup(monkeypatch, _make_tmux_adapter(tmp_path))
        monkeypatch.setenv("OW_TERMINAL", "manual")
        result = ow_service.ow_close_worker(term_ref="%99")
        assert result.get("manual") is True
        assert "adapter_error" in result


class TestSpawnDefaultTerminalIsTmux:
    """OW_TERMINAL 未設定時のデフォルトは "tmux" (manual fallback ではない)。"""

    def test_spawn_default_terminal_is_tmux(self, monkeypatch, tmp_path):
        tmux_path = _make_tmux_adapter(tmp_path)
        _patch_adapter_lookup(monkeypatch, tmux_path)
        monkeypatch.setenv("OW_STATE_DIR", str(tmp_path))
        monkeypatch.delenv("OW_TERMINAL", raising=False)

        def fake_run(args, **kwargs):
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="%200\n", stderr=""
            )

        monkeypatch.setattr(ow_service.subprocess, "run", fake_run)

        result = ow_service.ow_spawn_worker(
            alias="w-default",
            channel="ch1",
            cwd=str(tmp_path),
            model="claude-opus-4-7",
            task_title="t",
            acceptance="a",
            task_n=1,
        )

        assert result.get("spawning") == "ok"
        assert result.get("term_ref") == "%200"
