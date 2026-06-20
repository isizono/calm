"""ow_spawn_worker: 思考worker (effort 指定) の iTerm2 別タブ routing テスト。

OW_TERMINAL=tmux でも effort 指定時は iterm2 アダプタが呼ばれる経路 (D#2601 supersede)。

- effort 指定 + OW_TERMINAL=tmux → iterm2 アダプタが呼ばれ、is_thinking 引数は付かない
- effort 未指定 + OW_TERMINAL=tmux → tmux アダプタ + is_thinking=0
- iterm2 アダプタが存在しない場合は tmux + is_thinking=1 にフォールバック
- iTerm2 spawn 失敗 (CalledProcessError) → manual + adapter_error
- ow_close_worker: term_ref=iterm2 UUID 形式 → iterm2 アダプタが呼ばれる
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


def _make_adapters(tmp_path: Path, *names: str) -> dict[str, Path]:
    scripts_dir = tmp_path / "scripts" / "ow" / "adapters"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for n in names:
        p = scripts_dir / f"{n}.sh"
        p.write_text("#!/bin/sh\necho %42\n", encoding="utf-8")
        p.chmod(0o755)
        paths[n] = p
    return paths


def _patch_adapter_lookup(monkeypatch, paths: dict[str, Path]):
    monkeypatch.setattr(
        ow_service, "_get_adapter_path",
        lambda terminal: paths.get(terminal),
    )


class TestSpawnThinkingRoutesToIterm2:
    """OW_TERMINAL=tmux + effort 指定 → iterm2 アダプタにルーティング。"""

    def test_effort_specified_calls_iterm2_adapter(
        self, monkeypatch, tmp_path
    ):
        paths = _make_adapters(tmp_path, "tmux", "iterm2")
        _patch_adapter_lookup(monkeypatch, paths)
        monkeypatch.setenv("OW_STATE_DIR", str(tmp_path))
        monkeypatch.setenv("OW_TERMINAL", "tmux")

        captured: dict = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            return subprocess.CompletedProcess(
                args=args, returncode=0,
                stdout="ABCDEF12-3456-7890-ABCD-EF1234567890\n", stderr=""
            )

        monkeypatch.setattr(ow_service.subprocess, "run", fake_run)

        result = ow_service.ow_spawn_worker(
            alias="w-thinking",
            channel="ch1",
            cwd=str(tmp_path),
            model="claude-opus-4-7",
            task_title="t", acceptance="a", task_n=1,
            effort="high",
        )

        assert result.get("spawning") == "ok"
        assert result.get("term_ref") == "ABCDEF12-3456-7890-ABCD-EF1234567890"
        # iterm2.sh が呼ばれていること
        assert str(paths["iterm2"]) in captured["args"]
        assert str(paths["tmux"]) not in captured["args"]
        # iterm2 経路は positional 拡張なし (target_pane / is_thinking を持ち込まない)
        assert "1" not in captured["args"][5:]

    def test_effort_unspecified_calls_tmux_adapter(self, monkeypatch, tmp_path):
        paths = _make_adapters(tmp_path, "tmux", "iterm2")
        _patch_adapter_lookup(monkeypatch, paths)
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
            alias="w-normal0",
            channel="ch1",
            cwd=str(tmp_path),
            model="claude-opus-4-7",
            task_title="t", acceptance="a", task_n=1,
            # effort 未指定
        )

        assert result.get("spawning") == "ok"
        assert str(paths["tmux"]) in captured["args"]
        assert str(paths["iterm2"]) not in captured["args"]

    def test_iterm2_adapter_missing_falls_back_to_tmux_thinking(
        self, monkeypatch, tmp_path
    ):
        """iterm2.sh が存在しない場合は tmux + is_thinking=1 (従来の挙動)。"""
        paths = _make_adapters(tmp_path, "tmux")  # iterm2 なし
        _patch_adapter_lookup(monkeypatch, paths)
        monkeypatch.setenv("OW_STATE_DIR", str(tmp_path))
        monkeypatch.setenv("OW_TERMINAL", "tmux")

        captured: dict = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="%77\n", stderr=""
            )

        monkeypatch.setattr(ow_service.subprocess, "run", fake_run)

        result = ow_service.ow_spawn_worker(
            alias="w-thinker",
            channel="ch1",
            cwd=str(tmp_path),
            model="claude-opus-4-7",
            task_title="t", acceptance="a", task_n=1,
            effort="xhigh",
        )

        assert result.get("spawning") == "ok"
        assert str(paths["tmux"]) in captured["args"]
        # is_thinking=1 が末尾に届く (target_pane 空のプレースホルダ + "1")
        assert captured["args"][-1] == "1"

    def test_iterm2_spawn_failure_manual_fallback_with_adapter_error(
        self, monkeypatch, tmp_path, caplog
    ):
        """iTerm2 spawn 失敗 (CalledProcessError) → manual + adapter_error (D#2772)。"""
        import logging

        paths = _make_adapters(tmp_path, "tmux", "iterm2")
        _patch_adapter_lookup(monkeypatch, paths)
        monkeypatch.setenv("OW_STATE_DIR", str(tmp_path))
        monkeypatch.setenv("OW_TERMINAL", "tmux")

        def fake_run(args, **kwargs):
            raise subprocess.CalledProcessError(
                returncode=1, cmd=args, stderr="osascript: iTerm2 not running"
            )

        monkeypatch.setattr(ow_service.subprocess, "run", fake_run)

        with caplog.at_level(logging.ERROR, logger="src.services.ow_service"):
            result = ow_service.ow_spawn_worker(
                alias="w-thinker",
                channel="ch1",
                cwd=str(tmp_path),
                model="claude-opus-4-7",
                task_title="t", acceptance="a", task_n=1,
                effort="max",
            )

        assert result.get("manual") is True
        assert "adapter_error" in result
        assert "iTerm2 not running" in result["adapter_error"]
        assert any(
            r.levelno == logging.ERROR and "adapter spawn failed" in r.getMessage()
            for r in caplog.records
        )


class TestCloseRoutesByTermRef:
    """ow_close_worker: term_ref 形式から adapter を選ぶ。"""

    def test_iterm2_uuid_term_ref_routes_to_iterm2_adapter(
        self, monkeypatch, tmp_path
    ):
        paths = _make_adapters(tmp_path, "tmux", "iterm2")
        _patch_adapter_lookup(monkeypatch, paths)
        # OW_TERMINAL=tmux でも term_ref が iterm2 UUID なら iterm2.sh が呼ばれる
        monkeypatch.setenv("OW_TERMINAL", "tmux")

        captured: dict = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="", stderr=""
            )

        monkeypatch.setattr(ow_service.subprocess, "run", fake_run)

        result = ow_service.ow_close_worker(
            term_ref="ABCDEF12-3456-7890-ABCD-EF1234567890"
        )

        assert result.get("closed") is True
        assert str(paths["iterm2"]) in captured["args"]
        assert str(paths["tmux"]) not in captured["args"]

    def test_tmux_pane_term_ref_routes_to_tmux_adapter(
        self, monkeypatch, tmp_path
    ):
        paths = _make_adapters(tmp_path, "tmux", "iterm2")
        _patch_adapter_lookup(monkeypatch, paths)
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
        assert str(paths["tmux"]) in captured["args"]
        assert str(paths["iterm2"]) not in captured["args"]

    def test_terminal_manual_preserves_manual_fallback_for_tmux_shaped_term_ref(
        self, monkeypatch
    ):
        """OW_TERMINAL 未設定 (manual) は classify を迂回して manual のまま。"""
        monkeypatch.delenv("OW_TERMINAL", raising=False)
        result = ow_service.ow_close_worker(term_ref="%99")
        assert result.get("manual") is True
        assert "adapter_error" in result
