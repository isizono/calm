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
"""
from __future__ import annotations

import argparse
import os
from collections.abc import Mapping

ROLES = ("holder", "consultant", "observer", "lesson", "worker")
ESCALATE_DISABLE_ENV = "CALM_LINE_ESCALATE_DISABLE"


def build_env(role: str, env: Mapping[str, str]) -> dict[str, str]:
    result = dict(env)
    result.pop("CLAUDE_CODE_SESSION_ID", None)
    if role == "holder":
        # tmuxサーバーのglobal envなどから紛れ込んでも、担い手からは人に届くようにする
        result.pop(ESCALATE_DISABLE_ENV, None)
    else:
        result[ESCALATE_DISABLE_ENV] = "1"
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], allow_abbrev=False)
    parser.add_argument("--role", choices=ROLES, required=True)
    args, claude_args = parser.parse_known_args(argv)
    os.execvpe("claude", ["claude", *claude_args], build_env(args.role, os.environ))


if __name__ == "__main__":
    main()
