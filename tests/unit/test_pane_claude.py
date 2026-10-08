"""scripts/pane_claude.py の単体テスト。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts import pane_claude  # noqa: E402
from scripts.pane_claude import ESCALATE_DISABLE_ENV, build_env  # noqa: E402


@pytest.mark.parametrize("role", ["consultant", "observer", "lesson", "worker"])
def test_non_holder_roles_disable_escalation(role):
    env = build_env(role, {"PATH": "/bin"})
    assert env[ESCALATE_DISABLE_ENV] == "1"
    assert env["PATH"] == "/bin"


def test_holder_keeps_escalation_even_if_inherited():
    env = build_env("holder", {ESCALATE_DISABLE_ENV: "1", "PATH": "/bin"})
    assert ESCALATE_DISABLE_ENV not in env
    assert env["PATH"] == "/bin"


@pytest.mark.parametrize("role", ["holder", "observer"])
def test_inherited_session_id_is_dropped_for_every_role(role):
    env = build_env(role, {"CLAUDE_CODE_SESSION_ID": "9c291449-abe5-426d-9c34-7b2c14116efb", "PATH": "/bin"})
    assert "CLAUDE_CODE_SESSION_ID" not in env
    assert env["PATH"] == "/bin"


def test_main_passes_claude_args_through_and_sets_env(monkeypatch):
    captured = {}

    def fake_exec(file, args, env):
        captured.update(file=file, args=args, env=env)

    monkeypatch.setattr(pane_claude.os, "execvpe", fake_exec)
    pane_claude.main(["--role", "observer", "--plugin-dir", "/p", "依頼文"])
    assert captured["file"] == "claude"
    assert captured["args"] == ["claude", "--plugin-dir", "/p", "依頼文"]
    assert captured["env"][ESCALATE_DISABLE_ENV] == "1"


def test_unknown_role_is_rejected():
    with pytest.raises(SystemExit):
        pane_claude.main(["--role", "orch"])
