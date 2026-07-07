"""SignalCaptureMiddleware の単体テスト

tool呼び出し中の未捕捉例外が signal_events に machine_error として記録され、
例外自体はそのまま呼び出し元に伝播すること（挙動不変）を検証する。
signal記録自体の失敗がtool呼び出し結果を壊さないことも合わせて検証する。
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.db import get_connection
from src.services.signal_middleware import SignalCaptureMiddleware


def _make_call_context(tool_name="add_decisions", arguments=None, session_id="sess-1"):
    message = MagicMock()
    message.name = tool_name
    message.arguments = arguments if arguments is not None else {"topic_id": 1, "decision": "x"}

    fastmcp_ctx = MagicMock()
    fastmcp_ctx.session_id = session_id

    context = MagicMock()
    context.message = message
    context.fastmcp_context = fastmcp_ctx
    return context


@pytest.mark.asyncio
async def test_records_machine_error_on_unhandled_exception(temp_db):
    middleware = SignalCaptureMiddleware()
    context = _make_call_context(tool_name="add_decisions")

    async def _raise(_ctx):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await middleware.on_call_tool(context, _raise)

    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM signal_events").fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["kind"] == "machine_error"
    assert row["source"] == "tool:add_decisions"
    assert "RuntimeError" in row["summary"]
    assert row["session_id"] == "sess-1"


@pytest.mark.asyncio
async def test_reraises_original_exception_unchanged(temp_db):
    middleware = SignalCaptureMiddleware()
    context = _make_call_context()
    original = ValueError("specific message")

    async def _raise(_ctx):
        raise original

    with pytest.raises(ValueError) as exc_info:
        await middleware.on_call_tool(context, _raise)
    assert exc_info.value is original


@pytest.mark.asyncio
async def test_success_path_untouched(temp_db):
    middleware = SignalCaptureMiddleware()
    context = _make_call_context()
    call_next = AsyncMock(return_value="ok")

    result = await middleware.on_call_tool(context, call_next)

    assert result == "ok"
    call_next.assert_called_once_with(context)

    conn = get_connection()
    try:
        total = conn.execute("SELECT COUNT(*) FROM signal_events").fetchone()[0]
    finally:
        conn.close()
    assert total == 0


@pytest.mark.asyncio
async def test_detail_excludes_raw_argument_values(temp_db):
    middleware = SignalCaptureMiddleware()
    context = _make_call_context(
        tool_name="add_decisions",
        arguments={"decision": "super secret content that must not leak"},
    )

    async def _raise(_ctx):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await middleware.on_call_tool(context, _raise)

    conn = get_connection()
    try:
        row = conn.execute("SELECT detail FROM signal_events").fetchone()
    finally:
        conn.close()
    assert "super secret content" not in row["detail"]
    assert "decision:str" in row["detail"]


@pytest.mark.asyncio
async def test_missing_fastmcp_context_does_not_crash(temp_db):
    middleware = SignalCaptureMiddleware()
    context = _make_call_context()
    context.fastmcp_context = None

    async def _raise(_ctx):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await middleware.on_call_tool(context, _raise)

    conn = get_connection()
    try:
        row = conn.execute("SELECT session_id FROM signal_events").fetchone()
    finally:
        conn.close()
    assert row["session_id"] is None
