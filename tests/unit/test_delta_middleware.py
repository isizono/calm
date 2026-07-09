"""DeltaNotificationMiddleware のintegrationテスト

複数session_idを模擬し、check_in→baseline記録、別セッションの書き込みに
よるベル注入、announce-once（同じ差分は一度しか通知しない）、
自己通知抑制、再check_inでのscopeリセットを検証する。

temp_db / disable_embedding フィクスチャは tests/conftest.py で共有。
"""
from unittest.mock import MagicMock

import pytest
from fastmcp.tools.tool import ToolResult

from src.services.activity_service import add_activity
from src.services.checkin_service import check_in
from src.services.decision_service import add_decisions
from src.services.discussion_log_service import add_logs
from src.services.material_service import add_material
from src.services.topic_service import add_topic
from src.middleware.delta_middleware import DeltaNotificationMiddleware, _watermarks
from tests.helpers import add_decision


@pytest.fixture(autouse=True)
def _auto_disable_embedding(disable_embedding):
    """このファイル内の全テストでembedding呼び出しを無効化する"""


@pytest.fixture(autouse=True)
def _clear_watermarks():
    """モジュールレベルのwatermark状態をテスト間で共有しないようにする"""
    _watermarks.clear()
    yield
    _watermarks.clear()


def _make_context(tool_name: str, session_id):
    message = MagicMock()
    message.name = tool_name
    fastmcp_ctx = MagicMock()
    fastmcp_ctx.session_id = session_id
    context = MagicMock()
    context.message = message
    context.fastmcp_context = fastmcp_ctx
    return context


def _call_next_returning(tool_result: ToolResult):
    async def _inner(_ctx):
        return tool_result
    return _inner


def _noop_tool_result() -> ToolResult:
    return ToolResult(structured_content={"noop": True})


@pytest.fixture
def scope(temp_db):
    """スコープ用のtopicとそれに紐づくactivityを1組作成する"""
    topic = add_topic(title="Scope Topic", description="d", tags=["domain:test"])
    tid = topic["topic_id"]
    activity = add_activity(
        title="Scope Activity", description="d", tags=["domain:test"],
        related=[{"type": "topic", "ids": [tid]}], check_in=False,
    )
    aid = activity["activity_id"]
    return tid, aid


@pytest.mark.asyncio
async def test_check_in_records_baseline_and_scope(scope):
    tid, aid = scope
    middleware = DeltaNotificationMiddleware()

    checkin_result = check_in(aid)
    ctx = _make_context("check_in", "session-A")
    await middleware.on_call_tool(ctx, _call_next_returning(ToolResult(structured_content=checkin_result)))

    wm = _watermarks["session-A"]
    assert wm["activity_id"] == aid
    assert wm["topic_ids"] == [tid]
    assert wm["decision_id"] == 0
    assert wm["log_id"] == 0
    assert wm["material_id"] == 0


@pytest.mark.asyncio
async def test_add_activity_default_checkin_records_baseline(temp_db):
    """add_activity(check_in=True、デフォルト)経由でもbaselineが記録されること。

    check_in結果はresult["check_in_result"]にネストして返るため、ツール名
    "check_in"の場合と同じ処理では拾えない。add_activityは"典型的な使い方"の
    主要経路（新規アクティビティ作成時にそのまま着手する）のため、これが
    未対応だとbaselineが一切セットされずデルタ通知が発動しない（PR #550レビュー指摘）。
    """
    topic = add_topic(title="Scope Topic via add_activity", description="d", tags=["domain:test"])
    tid = topic["topic_id"]
    middleware = DeltaNotificationMiddleware()

    add_activity_result = add_activity(
        title="Activity via default check_in", description="d", tags=["domain:test"],
        related=[{"type": "topic", "ids": [tid]}],
    )
    aid = add_activity_result["activity_id"]

    await middleware.on_call_tool(
        _make_context("add_activity", "session-A"),
        _call_next_returning(ToolResult(structured_content=add_activity_result)),
    )

    wm = _watermarks["session-A"]
    assert wm["activity_id"] == aid
    assert wm["topic_ids"] == [tid]


@pytest.mark.asyncio
async def test_add_activity_explicit_no_checkin_does_not_record_baseline(temp_db):
    """add_activity(check_in=False)はcheck_in_resultを含まないため、baselineは記録されない。"""
    topic = add_topic(title="Scope Topic no checkin", description="d", tags=["domain:test"])
    tid = topic["topic_id"]
    middleware = DeltaNotificationMiddleware()

    add_activity_result = add_activity(
        title="Activity without check_in", description="d", tags=["domain:test"],
        related=[{"type": "topic", "ids": [tid]}], check_in=False,
    )

    await middleware.on_call_tool(
        _make_context("add_activity", "session-B"),
        _call_next_returning(ToolResult(structured_content=add_activity_result)),
    )

    assert "session-B" not in _watermarks


@pytest.mark.asyncio
async def test_cross_session_delta_injected_then_announce_once(scope):
    tid, aid = scope
    middleware = DeltaNotificationMiddleware()

    # (a) Aがcheck_in
    checkin_result = check_in(aid)
    await middleware.on_call_tool(
        _make_context("check_in", "session-A"),
        _call_next_returning(ToolResult(structured_content=checkin_result)),
    )

    # (b) 別セッションBがscope topicにdecisionを追加（Bはmiddlewareを経由しない
    # 素の書き込みとして表現。ピアセッションからの書き込みを模している）
    b_decision = add_decision("Bの決定", "reason", topic_id=tid)

    # (c) Aの次のツール呼び出しでdeltaがcontentに出る
    result1 = await middleware.on_call_tool(
        _make_context("get_topics", "session-A"),
        _call_next_returning(_noop_tool_result()),
    )
    injected_text = result1.content[-1].text
    assert "デルタ通知" in injected_text
    assert "Bの決定" in injected_text
    assert result1.structured_content["delta"]["new_decisions"] == [
        {"id": b_decision["decision_id"], "title": "Bの決定"}
    ]

    # (d) 同じ呼び出しを再度実行 → announce-onceで出ない
    result2 = await middleware.on_call_tool(
        _make_context("get_topics", "session-A"),
        _call_next_returning(_noop_tool_result()),
    )
    assert len(result2.content) == 1
    assert "delta" not in (result2.structured_content or {})


@pytest.mark.asyncio
async def test_self_write_not_notified(scope):
    tid, aid = scope
    middleware = DeltaNotificationMiddleware()

    checkin_result = check_in(aid)
    await middleware.on_call_tool(
        _make_context("check_in", "session-A"),
        _call_next_returning(ToolResult(structured_content=checkin_result)),
    )

    # (e) A自身がscope topicにdecisionを追加。add_decisionsのツール呼び出しを
    # middleware経由で処理させ、自己通知抑制のwatermark前進を確認する
    own_write_result = add_decisions([{"topic_id": tid, "decision": "自分の決定", "reason": "r"}])
    await middleware.on_call_tool(
        _make_context("add_decisions", "session-A"),
        _call_next_returning(ToolResult(structured_content=own_write_result)),
    )

    result = await middleware.on_call_tool(
        _make_context("get_topics", "session-A"),
        _call_next_returning(_noop_tool_result()),
    )
    assert len(result.content) == 1
    assert "delta" not in (result.structured_content or {})


@pytest.mark.asyncio
async def test_self_write_not_notified_for_logs(scope):
    """add_decisionsだけでなくadd_logs経由の自己通知抑制も別コードパスとして確認する。"""
    tid, aid = scope
    middleware = DeltaNotificationMiddleware()

    checkin_result = check_in(aid)
    await middleware.on_call_tool(
        _make_context("check_in", "session-A"),
        _call_next_returning(ToolResult(structured_content=checkin_result)),
    )

    own_write_result = add_logs([{"topic_id": tid, "content": "自分のログ"}])
    await middleware.on_call_tool(
        _make_context("add_logs", "session-A"),
        _call_next_returning(ToolResult(structured_content=own_write_result)),
    )

    result = await middleware.on_call_tool(
        _make_context("get_topics", "session-A"),
        _call_next_returning(_noop_tool_result()),
    )
    assert len(result.content) == 1
    assert "delta" not in (result.structured_content or {})


@pytest.mark.asyncio
async def test_self_write_not_notified_for_materials(scope):
    """add_materialはcreated配列を持たずtop-levelにmaterial_idを返す特殊系のため、
    add_decisions/add_logsとは別コードパス（_handle_writeのmaterial分岐）を確認する。
    """
    tid, aid = scope
    middleware = DeltaNotificationMiddleware()

    checkin_result = check_in(aid)
    await middleware.on_call_tool(
        _make_context("check_in", "session-A"),
        _call_next_returning(ToolResult(structured_content=checkin_result)),
    )

    own_write_result = add_material(
        title="自分のmaterial", content="x", tags=["domain:test"], source="test",
        related=[{"type": "topic", "ids": [tid]}],
    )
    await middleware.on_call_tool(
        _make_context("add_material", "session-A"),
        _call_next_returning(ToolResult(structured_content=own_write_result)),
    )

    result = await middleware.on_call_tool(
        _make_context("get_topics", "session-A"),
        _call_next_returning(_noop_tool_result()),
    )
    assert len(result.content) == 1
    assert "delta" not in (result.structured_content or {})


@pytest.mark.asyncio
async def test_recheckin_resets_scope(temp_db):
    topic1 = add_topic(title="Topic1", description="d", tags=["domain:test"])
    tid1 = topic1["topic_id"]
    activity1 = add_activity(
        title="Activity1", description="d", tags=["domain:test"],
        related=[{"type": "topic", "ids": [tid1]}], check_in=False,
    )
    aid1 = activity1["activity_id"]

    topic2 = add_topic(title="Topic2", description="d", tags=["domain:test"])
    tid2 = topic2["topic_id"]
    activity2 = add_activity(
        title="Activity2", description="d", tags=["domain:test"],
        related=[{"type": "topic", "ids": [tid2]}], check_in=False,
    )
    aid2 = activity2["activity_id"]

    middleware = DeltaNotificationMiddleware()

    checkin1 = check_in(aid1)
    await middleware.on_call_tool(
        _make_context("check_in", "session-A"),
        _call_next_returning(ToolResult(structured_content=checkin1)),
    )
    assert _watermarks["session-A"]["topic_ids"] == [tid1]
    assert _watermarks["session-A"]["activity_id"] == aid1

    # (f) 再check_in（別activity）でscopeが上書きされる
    checkin2 = check_in(aid2)
    await middleware.on_call_tool(
        _make_context("check_in", "session-A"),
        _call_next_returning(ToolResult(structured_content=checkin2)),
    )
    assert _watermarks["session-A"]["topic_ids"] == [tid2]
    assert _watermarks["session-A"]["activity_id"] == aid2


@pytest.mark.asyncio
async def test_session_without_checkin_gets_no_notification(scope):
    tid, _aid = scope
    middleware = DeltaNotificationMiddleware()

    add_decision("誰かの決定", "reason", topic_id=tid)

    result = await middleware.on_call_tool(
        _make_context("get_topics", "session-without-checkin"),
        _call_next_returning(_noop_tool_result()),
    )
    assert len(result.content) == 1
    assert "delta" not in (result.structured_content or {})
    assert "session-without-checkin" not in _watermarks


@pytest.mark.asyncio
async def test_session_id_none_falls_back_to_default(scope):
    _tid, aid = scope
    middleware = DeltaNotificationMiddleware()

    checkin_result = check_in(aid)
    await middleware.on_call_tool(
        _make_context("check_in", None),
        _call_next_returning(ToolResult(structured_content=checkin_result)),
    )
    assert "__default__" in _watermarks


@pytest.mark.asyncio
async def test_out_of_scope_write_does_not_suppress_future_in_scope_deltas(temp_db):
    """scope外topicへの自己書き込みはwatermarkを進めず、後続の別セッションの
    scope内書き込みも正しく検出され続けることを確認する。
    """
    scope_topic = add_topic(title="Scope Topic", description="d", tags=["domain:test"])
    tid = scope_topic["topic_id"]
    other_topic = add_topic(title="Other Topic", description="d", tags=["domain:test"])
    other_tid = other_topic["topic_id"]
    activity = add_activity(
        title="Activity", description="d", tags=["domain:test"],
        related=[{"type": "topic", "ids": [tid]}], check_in=False,
    )
    aid = activity["activity_id"]

    middleware = DeltaNotificationMiddleware()
    checkin_result = check_in(aid)
    await middleware.on_call_tool(
        _make_context("check_in", "session-A"),
        _call_next_returning(ToolResult(structured_content=checkin_result)),
    )
    assert _watermarks["session-A"]["decision_id"] == 0

    # Aがscope外topicにdecisionを書く
    out_of_scope_write = add_decisions([
        {"topic_id": other_tid, "decision": "scope外の決定", "reason": "r"}
    ])
    await middleware.on_call_tool(
        _make_context("add_decisions", "session-A"),
        _call_next_returning(ToolResult(structured_content=out_of_scope_write)),
    )
    # scope外への書き込みはwatermarkを進めない
    assert _watermarks["session-A"]["decision_id"] == 0

    # 別セッションBがscope内topicにdecisionを追加
    b_decision = add_decision("scope内の決定", "reason", topic_id=tid)

    result = await middleware.on_call_tool(
        _make_context("get_topics", "session-A"),
        _call_next_returning(_noop_tool_result()),
    )
    assert result.structured_content["delta"]["new_decisions"] == [
        {"id": b_decision["decision_id"], "title": "scope内の決定"}
    ]
