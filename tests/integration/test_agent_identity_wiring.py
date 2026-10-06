"""サブエージェントの識別子を、実際のMCPサーバー（src.main.mcp）経由で通す統合テスト。

in-memory の fastmcp Client に本物の middleware 列（AgentIdentityMiddleware・
DeltaNotificationMiddleware・SignalCaptureMiddleware など）を積んだまま呼び出す。
起動器ヘッダ（呼び出し元の恒久識別子）だけを外部境界として差し替え、「親」と
「親の中のサブエージェント」を同じ起動器ヘッダで呼び分ける。サブエージェントの
識別子は PreToolUse hook が足す引数 `_calm_agent_id` で表す。

検証するのは、既出管理（タグ注入・check_in初回・差分通知の既読位置）が親と別キーに
なること、サブエージェントが親の別名行と既読位置を動かさないこと、呼び出し元の
識別子（add_askの要求元）が変わらないこと。
"""
import contextlib

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from src.db import get_connection
from src.infra import session_identity
from src.main import mcp
from src.middleware.agent_identity_middleware import AgentIdentityMiddleware
from src.middleware.arg_alias_middleware import ArgAliasMiddleware
from src.middleware.delta_middleware import DeltaNotificationMiddleware, _watermarks
from src.middleware.destination_middleware import DestinationCandidateMiddleware
from src.services import search_service, session_registry_service
from src.services.activity_service import add_activity
from src.services.signal_middleware import SignalCaptureMiddleware
from src.services.tag_service import update_tag
from src.services.topic_service import add_topic

PARENT = "parent-bridge"
OTHER = "other-bridge"
SUBAGENT = "sa-1"
_TAG_NOTES = "取扱注意: 統合テスト用のdomain notes"


@pytest.fixture(autouse=True)
def _auto_disable_embedding(disable_embedding):
    """このファイル内の全テストでembedding呼び出しを無効化する"""


@pytest.fixture
def bridge(monkeypatch, temp_db, tmp_path):
    """起動器ヘッダが返す呼び出し元の識別子を、テストから切り替えられるようにする。"""
    monkeypatch.setenv(session_registry_service.REGISTRY_PATH_ENV, str(tmp_path / "aliases.json"))
    current = {"id": PARENT}

    def _headers():
        return {session_identity.BRIDGE_SESSION_HEADER: current["id"]}

    monkeypatch.setattr("fastmcp.server.dependencies.get_http_headers", _headers)
    _watermarks.clear()
    search_service._presented_records.clear()
    return current


@pytest.fixture
def scope(temp_db):
    """親とサブエージェントが同じ topic を見る2つの activity と、注意書き付きのタグを作る。"""
    topic = add_topic(title="Scope Topic", description="d", tags=["domain:test"])
    tid = topic["topic_id"]
    update_tag("domain:test", _TAG_NOTES)
    related = [{"type": "topic", "ids": [tid]}]
    parent_activity = add_activity(
        title="Parent Activity", description="d", tags=["domain:test"],
        related=related, check_in=False,
    )
    sa_activity = add_activity(
        title="Subagent Activity", description="d", tags=["domain:test"],
        related=related, check_in=False,
    )
    return {
        "topic_id": tid,
        "parent_activity": parent_activity["activity_id"],
        "sa_activity": sa_activity["activity_id"],
    }


async def _call(client, bridge, name, arguments, *, as_bridge=PARENT, agent=None):
    """起動器ヘッダを as_bridge に切り替えて1回呼ぶ。agent があればサブエージェントの呼び出し。"""
    bridge["id"] = as_bridge
    payload = dict(arguments)
    if agent is not None:
        payload[session_identity.AGENT_ID_ARG] = agent
    return await client.call_tool(name, payload)


def _env(result) -> dict:
    return result.structured_content["env"]


def _delta_text(result) -> str:
    return "\n".join(c.text for c in result.content if hasattr(c, "text"))


@pytest.mark.asyncio
async def test_subagent_checkin_receives_tag_notes_and_flow_guide_after_the_parent(scope, bridge):
    aid = scope["parent_activity"]
    async with Client(mcp) as client:
        await _call(client, bridge, "check_in", {"activity_id": aid})
        sa = await _call(client, bridge, "check_in", {"activity_id": aid}, agent=SUBAGENT)

    env = _env(sa)
    notes = {entry["tag"]: entry["notes"] for entry in env["tag_notes"]}
    assert notes.get("domain:test") == _TAG_NOTES
    assert env.get("flow_guide")


@pytest.mark.asyncio
async def test_subagent_reads_do_not_move_the_parent_watermark(scope, bridge):
    tid = scope["topic_id"]
    async with Client(mcp) as client:
        await _call(client, bridge, "check_in", {"activity_id": scope["parent_activity"]})
        await _call(
            client, bridge, "add_logs",
            {"items": [{"topic_id": tid, "content": "他セッションの記録", "title": "from-other"}]},
            as_bridge=OTHER,
        )
        await _call(
            client, bridge, "check_in", {"activity_id": scope["sa_activity"]},
            agent=SUBAGENT,
        )
        parent_read = await _call(
            client, bridge, "search", {"keyword": "zzzz"},
        )

    assert "from-other" in _delta_text(parent_read)


@pytest.mark.asyncio
async def test_parent_checkin_repeat_still_skips_already_injected_notes(scope, bridge):
    """親だけの呼び出しは、キーの分け方を変えても今までどおり注入済みを再配達しない。"""
    aid = scope["parent_activity"]
    async with Client(mcp) as client:
        first = await _call(client, bridge, "check_in", {"activity_id": aid})
        second = await _call(client, bridge, "check_in", {"activity_id": aid})

    assert "domain:test" in {e["tag"] for e in _env(first)["tag_notes"]}
    assert "domain:test" not in {e["tag"] for e in _env(second).get("tag_notes", [])}
    assert "flow_guide" not in _env(second)


@pytest.mark.asyncio
async def test_subagent_checkin_does_not_register_the_parents_alias(scope, bridge, monkeypatch):
    calls = []
    original = session_registry_service.register_checkin

    def spy(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(session_registry_service, "register_checkin", spy)
    async with Client(mcp) as client:
        await _call(client, bridge, "check_in", {"activity_id": scope["parent_activity"]})
        sa = await _call(
            client, bridge, "check_in", {"activity_id": scope["sa_activity"]}, agent=SUBAGENT,
        )

    assert [c["activity_id"] for c in calls] == [scope["parent_activity"]]
    assert _env(sa)["session"] == {"registered": False, "reason": "subagent"}


@pytest.mark.asyncio
async def test_add_ask_from_a_subagent_keeps_the_parents_requester(scope, bridge):
    async with Client(mcp) as client:
        ask = await _call(
            client, bridge, "add_ask",
            {
                "question": "統合テストの問い",
                "tags": ["domain:test"],
                "blocks": [scope["parent_activity"]],
                "notify": False,
            },
            agent=SUBAGENT,
        )
    assert "error" not in ask.structured_content

    with contextlib.closing(get_connection()) as conn:
        rows = conn.execute("SELECT requester_session_id FROM ask_requesters").fetchall()
    assert [r[0] for r in rows] == [PARENT]


def test_identity_middleware_is_registered_before_the_other_calm_middleware():
    """FastMCP 内蔵の middleware の後ろに、CALM の middleware の中で最も外側に置く。"""
    calm_middleware = [
        AgentIdentityMiddleware,
        SignalCaptureMiddleware,
        ArgAliasMiddleware,
        DeltaNotificationMiddleware,
        DestinationCandidateMiddleware,
    ]
    registered = [type(m) for m in mcp.middleware]
    positions = {cls: registered.index(cls) for cls in calm_middleware}
    assert positions[AgentIdentityMiddleware] == min(positions.values())


@pytest.mark.asyncio
async def test_identity_argument_never_reaches_the_signal_capture(scope, bridge):
    """失敗した呼び出しの記録（引数のkey:型ダイジェスト）に識別子の引数が現れないこと。
    取り除き忘れがあれば、ダイジェストに残って検出される。登録順そのものは
    test_identity_middleware_is_registered_before_the_other_calm_middleware で固定する。"""
    async with Client(mcp) as client:
        with pytest.raises(ToolError):
            await _call(
                client, bridge, "check_in", {"activity_id": "not-an-int"}, agent=SUBAGENT,
            )

    with contextlib.closing(get_connection()) as conn:
        details = [
            row[0] for row in conn.execute(
                "SELECT detail FROM signal_events WHERE kind = 'machine_error'"
            ).fetchall()
        ]
    assert details, "失敗呼び出しが記録されていない（検証が空振りしている）"
    assert all(session_identity.AGENT_ID_ARG not in d for d in details)
    assert all("activity_id" in d for d in details)
