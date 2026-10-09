#!/usr/bin/env python3
"""orchのtmuxペインでclaudeを起こす。担い手以外は、返事待ちの自動askを切る。

使い方: pane_claude.py --role <役> [claudeへの引数...]
    例: pane_claude.py --role observer --plugin-dir <dir> '<依頼文>'

担い手(holder)はorchで人に届くただ1つの窓口なので、返事待ちの自動askを残す。
それ以外の役(常駐と作業役の席)は、cronや待ち受けで自分から起きるため、人の
返事を待っていない。自動askを切る環境変数の名前は、このファイルだけに置く。

役を問わず CLAUDE_CODE_SESSION_ID は外す。tmuxサーバーを起動したclaudeの値が
サーバーの環境に残り、ペインで起こすclaudeとその子に、別のセッションのIDとして
継承されるため。

役ごとの--model既定: holderは継承(何も足さない)、consultant・lesson・workerはopus、
observerはsonnet。claudeへの引数に--modelがあればそれに従う。

担い手の起動文は、--orch-title・--orch-activity-id・--old-name・--old-session-idで
渡すと、scripts/templates/holder_launch.txtの雛形から組んでclaudeの最後の引数に足す
(--old-pid・--old-pane・--board-titleは任意)。--print-promptを付けるとclaudeを起こさず
組んだ起動文だけを表示する。
"""
from __future__ import annotations

import argparse
import os
import string
from collections.abc import Mapping
from pathlib import Path

ROLES = ("holder", "consultant", "observer", "lesson", "worker")
ESCALATE_DISABLE_ENV = "CALM_LINE_ESCALATE_DISABLE"
ROLE_MODELS = {"consultant": "opus", "lesson": "opus", "worker": "opus", "observer": "sonnet"}
HOLDER_TEMPLATE = Path(__file__).resolve().parent / "templates" / "holder_launch.txt"
BOARD_STEP_MARK = "[掲示板] "


def build_env(role: str, env: Mapping[str, str]) -> dict[str, str]:
    result = dict(env)
    result.pop("CLAUDE_CODE_SESSION_ID", None)
    if role == "holder":
        # tmuxサーバーのglobal envなどから紛れ込んでも、担い手からは人に届くようにする
        result.pop(ESCALATE_DISABLE_ENV, None)
    else:
        result[ESCALATE_DISABLE_ENV] = "1"
    return result


def model_args(role: str, claude_args: list[str]) -> list[str]:
    if any(a == "--model" or a.startswith("--model=") for a in claude_args):
        return []
    model = ROLE_MODELS.get(role)
    return ["--model", model] if model else []


def render_holder_prompt(*, orch_title: str, orch_activity_id: int, old_name: str,
                         old_session_id: str, old_pid: int | None = None,
                         old_pane: str | None = None, board_title: str | None = None) -> str:
    """雛形の「* 」で始まる行を手順として番号を振り、掲示板が無ければ掲示板の手順を落とす。"""
    header: list[str] = []
    steps: list[str] = []
    for line in HOLDER_TEMPLATE.read_text(encoding="utf-8").splitlines():
        if line.startswith("* "):
            step = line[2:]
            if step.startswith(BOARD_STEP_MARK):
                if not board_title:
                    continue
                step = step[len(BOARD_STEP_MARK):]
            steps.append(step)
        else:
            header.append(line)
    numbered = [f"{i}. {s}" for i, s in enumerate(steps, 1)]
    extras = [f"pid {old_pid}" if old_pid else "", f"ペイン {old_pane}" if old_pane else ""]
    old_extra = "".join(f"、{e}" for e in extras if e)
    return string.Template("\n".join(header + numbered)).substitute(
        orch_title=orch_title, orch_activity_id=orch_activity_id, old_name=old_name,
        old_session_id=old_session_id, old_extra=old_extra, board_title=board_title or "",
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], allow_abbrev=False)
    parser.add_argument("--role", choices=ROLES, required=True)
    parser.add_argument("--orch-title")
    parser.add_argument("--orch-activity-id", type=int)
    parser.add_argument("--old-name")
    parser.add_argument("--old-session-id")
    parser.add_argument("--old-pid", type=int)
    parser.add_argument("--old-pane")
    parser.add_argument("--board-title")
    parser.add_argument("--print-prompt", action="store_true")
    args, claude_args = parser.parse_known_args(argv)

    prompt_args = [args.orch_title, args.orch_activity_id, args.old_name, args.old_session_id]
    wants_prompt = args.print_prompt or any(
        v is not None for v in (*prompt_args, args.old_pid, args.old_pane, args.board_title)
    )
    if wants_prompt:
        if args.role != "holder":
            parser.error("the holder prompt options are for --role holder")
        if any(v is None for v in prompt_args):
            parser.error("--orch-title, --orch-activity-id, --old-name and --old-session-id are required")
        prompt = render_holder_prompt(
            orch_title=args.orch_title, orch_activity_id=args.orch_activity_id,
            old_name=args.old_name, old_session_id=args.old_session_id,
            old_pid=args.old_pid, old_pane=args.old_pane, board_title=args.board_title,
        )
        if args.print_prompt:
            print(prompt)
            return
        claude_args = [*claude_args, prompt]
    claude_args = [*model_args(args.role, claude_args), *claude_args]
    os.execvpe("claude", ["claude", *claude_args], build_env(args.role, os.environ))


if __name__ == "__main__":
    main()
