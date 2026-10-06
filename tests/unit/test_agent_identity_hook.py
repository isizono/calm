"""agent_identity_hook.py の単体テスト。

PreToolUse hook が、サブエージェント内の呼び出しにだけ agent_id を引数として足し、
元の tool_input を一切落とさないことを検証する。親からの呼び出し（agent_id なし）と
入力が壊れている場合は何も出力せず、入力を変えずに通す（fail-open）。
"""
import json
import sys
from pathlib import Path

_HOOKS_DIR = Path(__file__).resolve().parents[2] / "hooks"
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

import agent_identity_hook  # type: ignore  # noqa: E402

from src.infra import session_identity  # noqa: E402


def _event(**overrides) -> bytes:
    event = {
        "session_id": "parent-session",
        "tool_name": "mcp__plugin_calm_calm__check_in",
        "tool_input": {"activity_id": 7, "flavor": "readable"},
        "agent_id": "a3ca93b7f7c1af2ce",
        "agent_type": "Explore",
    }
    event.update(overrides)
    return json.dumps(event, ensure_ascii=False).encode("utf-8")


def _updated_input(out: str) -> dict:
    payload = json.loads(out)
    assert payload["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    return payload["hookSpecificOutput"]["updatedInput"]


def test_adds_agent_id_and_keeps_every_original_field():
    out = agent_identity_hook.run(_event())

    updated = _updated_input(out)
    assert updated == {
        "activity_id": 7,
        "flavor": "readable",
        session_identity.AGENT_ID_ARG: "a3ca93b7f7c1af2ce",
    }


def test_output_has_no_permission_decision():
    # 権限判定を hook 側で上書きすると、サブエージェントのCALM呼び出しが
    # 権限確認を素通りする。注入だけを行い、判定は harness に任せる。
    payload = json.loads(agent_identity_hook.run(_event()))
    assert set(payload["hookSpecificOutput"]) == {"hookEventName", "updatedInput"}


def test_emits_nothing_for_a_parent_call_without_agent_id():
    raw = json.dumps({"session_id": "parent", "tool_input": {"activity_id": 7}}).encode()
    assert agent_identity_hook.run(raw) is None


def test_emits_nothing_when_agent_id_is_empty():
    assert agent_identity_hook.run(_event(agent_id="")) is None


def test_emits_nothing_when_tool_input_is_not_an_object():
    assert agent_identity_hook.run(_event(tool_input=["not", "a", "dict"])) is None


def test_emits_nothing_for_undecodable_input():
    assert agent_identity_hook.run(b"{not json") is None


def test_non_ascii_arguments_survive_the_round_trip():
    out = agent_identity_hook.run(_event(tool_input={"text": "日本語のメモ"}))

    updated = _updated_input(out)
    assert updated["text"] == "日本語のメモ"
    # 出力は ASCII のみ。Windows の既定コードページで stdout が化けないように
    assert out.isascii()


def test_argument_name_matches_the_server_side_constant():
    # hook は src を import しないため、サーバー側の定数と文字列が一致することを
    # ここで固定する。片方だけ変わると、識別子が取り出されずに検証で落ちる。
    assert agent_identity_hook._AGENT_ID_ARG == session_identity.AGENT_ID_ARG == "_calm_agent_id"
