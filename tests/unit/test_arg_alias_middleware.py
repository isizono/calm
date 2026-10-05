"""ArgAliasMiddleware: 引数名の取り違えが書き換えられ、直せないものは例つきエラーになる。"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from src.middleware.arg_alias_middleware import ArgAliasMiddleware


async def _run(tool, arguments):
    ctx = MagicMock()
    ctx.message.name = tool
    ctx.message.arguments = arguments
    call_next = AsyncMock(return_value="ok")
    await ArgAliasMiddleware().on_call_tool(ctx, call_next)
    return ctx.message.arguments


@pytest.mark.asyncio
async def test_search_query_and_type_are_renamed():
    assert await _run("search", {"query": "x", "type": "decision"}) == {
        "keyword": "x",
        "entity_type": "decision",
    }


@pytest.mark.asyncio
async def test_search_correct_name_wins_over_alias():
    assert (await _run("search", {"keyword": "a", "query": "b"}))["keyword"] == "a"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["get_logs", "get_decisions"])
@pytest.mark.parametrize("kind", ["topic", "activity"])
async def test_get_logs_decisions_id_alias(tool, kind):
    out = await _run(tool, {f"{kind}_id": "5", "limit": 3})
    assert out == {"entity_type": kind, "entity_id": "5", "limit": 3}


@pytest.mark.asyncio
async def test_single_item_is_wrapped_into_items():
    out = await _run("add_logs", {"topic_id": 1, "content": "c", "title": "t"})
    assert out == {"items": [{"topic_id": 1, "content": "c", "title": "t"}]}


@pytest.mark.asyncio
async def test_unfixable_call_raises_error_with_example():
    with pytest.raises(ToolError, match=r"get_by_ids\(items="):
        await _run("get_by_ids", {"ids": "[1]"})
