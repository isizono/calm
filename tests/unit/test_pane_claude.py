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
    pane_claude.main(["--role", "worker", "--model", "sonnet", "--plugin-dir", "/p", "依頼文"])
    assert captured["file"] == "claude"
    assert captured["args"] == ["claude", "--model", "sonnet", "--plugin-dir", "/p", "依頼文"]
    assert captured["env"][ESCALATE_DISABLE_ENV] == "1"


def test_unknown_role_is_rejected():
    with pytest.raises(SystemExit):
        pane_claude.main(["--role", "orch"])


@pytest.mark.parametrize("role,expected", [
    ("holder", []),
    ("consultant", ["--model", "opus"]),
    ("lesson", ["--model", "opus"]),
    ("worker", ["--model", "opus"]),
    ("observer", ["--model", "sonnet"]),
])
def test_model_default_per_role(role, expected):
    assert pane_claude.model_args(role, ["--plugin-dir", "/p"]) == expected


@pytest.mark.parametrize("explicit", [["--model", "haiku"], ["--model=haiku"]])
def test_explicit_model_wins_over_the_role_default(explicit):
    assert pane_claude.model_args("observer", [*explicit, "x"]) == []


def test_main_puts_the_role_model_before_claude_args(monkeypatch):
    captured = {}
    monkeypatch.setattr(pane_claude.os, "execvpe", lambda f, a, e: captured.update(args=a))
    pane_claude.main(["--role", "observer", "--plugin-dir", "/p", "依頼文"])
    assert captured["args"] == ["claude", "--model", "sonnet", "--plugin-dir", "/p", "依頼文"]


_HOLDER_ARGS = ["--role", "holder", "--orch-title", "ダミーorch", "--orch-activity-id", "77",
                "--old-name", "workspace-aa", "--old-session-id", "11111111-1111-1111-1111-111111111111"]


def test_holder_prompt_is_built_from_the_template_and_appended_last(monkeypatch):
    captured = {}
    monkeypatch.setattr(pane_claude.os, "execvpe", lambda f, a, e: captured.update(args=a))
    pane_claude.main([*_HOLDER_ARGS, "--old-pid", "123", "--old-pane", "%5", "--plugin-dir", "/p"])
    args = captured["args"]
    assert args[:3] == ["claude", "--plugin-dir", "/p"]
    prompt = args[-1]
    assert "ダミーorchのorch続けて" in prompt and "activity_id=77" in prompt
    assert "workspace-aa（sessionId 11111111-1111-1111-1111-111111111111、pid 123、ペイン %5）" in prompt
    assert "$$" not in prompt and "${" not in prompt
    assert "echo $CLAUDE_PID" in prompt


def test_holder_prompt_numbers_steps_and_picks_one_board_step():
    without = pane_claude.render_holder_prompt(
        orch_title="t", orch_activity_id=1, old_name="n", old_session_id="s")
    with_board = pane_claude.render_holder_prompt(
        orch_title="t", orch_activity_id=1, old_name="n", old_session_id="s", board_title="掲示板X")
    assert "掲示板X" in with_board and "掲示板の題が渡されていない" not in with_board
    # 題が無いときも攻撃の手順は落とさず、後継に掲示板を探させる
    assert "掲示板の題が渡されていない" in without and "壊れる場面を1つ" in without
    numbers = [line.split(".")[0] for line in with_board.splitlines() if line[:1].isdigit()]
    assert numbers == [str(i) for i in range(1, len(numbers) + 1)]
    numbers = [line.split(".")[0] for line in without.splitlines() if line[:1].isdigit()]
    assert numbers == [str(i) for i in range(1, len(numbers) + 1)]
    # 担い手欄の書き換えは、掲示板の有無にかかわらず攻撃・check_inより前
    for text in (without, with_board):
        assert text.index("replace_holder_lines(") < text.index("check_in(activity_id=")
        assert text.index("壊れる場面を1つ") < text.index("get_timeline(") < text.index("check_in(activity_id=")
        # 一覧のログはcheck_inの後に読む
        assert text.index("check_in(activity_id=") < text.index("読むべきログ")


def test_print_prompt_does_not_exec(monkeypatch, capsys):
    monkeypatch.setattr(pane_claude.os, "execvpe", lambda *a: pytest.fail("exec"))
    pane_claude.main([*_HOLDER_ARGS, "--print-prompt"])
    assert "ダミーorchのorch続けて" in capsys.readouterr().out


def test_prompt_options_require_the_four_identifying_values():
    with pytest.raises(SystemExit):
        pane_claude.main(["--role", "holder", "--orch-title", "t"])
    with pytest.raises(SystemExit):
        pane_claude.main(["--role", "observer", *_HOLDER_ARGS[2:]])
