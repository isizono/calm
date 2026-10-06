"""PreToolUse hook: サブエージェント内のCALMツール呼び出しに、呼び出し元の agent_id を引数として足す。

入力に agent_id があるとき（サブエージェント内の呼び出し）だけ、元の tool_input を
すべて残したまま `_calm_agent_id` を加えた updatedInput を返す。サーバー側の
AgentIdentityMiddleware がこの引数を取り出し、既出管理を親と分けて扱う。
agent_id が無い（親からの呼び出し）ときは何も出力せず、入力は変えない。

標準ライブラリだけで書く。失敗したときは何も出力せず、入力を変えずに通す（fail-open）。
"""
import json

# サーバー側の引数名（src/infra/session_identity.py の AGENT_ID_ARG）と同じ文字列。
# hook は src を import しない構成のため、値の一致はテストで固定する。
_AGENT_ID_ARG = "_calm_agent_id"


def _build_output(event: dict) -> dict | None:
    agent_id = event.get("agent_id")
    tool_input = event.get("tool_input")
    if not isinstance(agent_id, str) or not agent_id or not isinstance(tool_input, dict):
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": {**tool_input, _AGENT_ID_ARG: agent_id},
        }
    }


def run(raw: bytes) -> str | None:
    """stdin の生バイト列から、出力すべき JSON 文字列を返す。出力しないなら None。"""
    try:
        output = _build_output(json.loads(raw.decode("utf-8")))
    except Exception:
        return None
    return None if output is None else json.dumps(output)


def main() -> None:
    # sys.stdin は使わない（Windows の既定コードページで日本語入力が化けるため。
    # test_hooks_stdin_encoding_lint 参照）。fd 0 を UTF-8 として読む。
    try:
        raw = open(0, "rb", closefd=False).read()
    except OSError:
        return
    out = run(raw)
    if out is not None:
        print(out)


if __name__ == "__main__":
    main()
