"""ArgAliasMiddleware: 引数名の取り違えが書き換えられ、直せないものは例つきエラーになる。"""
import re
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
    call_next.assert_awaited_once_with(ctx)
    return ctx.message.arguments


@pytest.mark.asyncio
async def test_search_query_and_type_are_renamed():
    assert await _run("search", {"query": "x", "type": "decision"}) == {
        "keyword": "x",
        "entity_type": "decision",
    }


@pytest.mark.asyncio
async def test_search_correct_name_wins_over_alias():
    assert await _run("search", {"keyword": "a", "query": "b"}) == {"keyword": "a"}


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
async def test_add_decisions_single_item_is_wrapped():
    args = {"topic_id": 1, "decision": "d", "reason": "r", "tags": ["a"]}
    assert await _run("add_decisions", dict(args)) == {"items": [args]}


@pytest.mark.asyncio
async def test_entity_args_already_given_are_kept():
    args = {"entity_type": "topic", "entity_id": 1, "activity_id": 2}
    assert await _run("get_logs", dict(args)) == args


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,args,example",
    [
        ("get_by_ids", {"entity_type": "topic"}, "get_by_ids(items="),
        ("add_logs", {"topic_id": 1}, "add_logs(items="),
        ("add_logs", {"topic_id": 1, "content": "c", "flavor": "raw"}, "add_logs(items="),
        ("add_decisions", {"entity_type": "activity"}, "add_decisions(items="),
        ("check_in", {}, "check_in(activity_id="),
        ("update_goal", {"handle": "x"}, "update_goal(goal_id=1, changes="),
        ("add_material", {"title": "t"}, 'add_material(title="...", content="...", tags='),
    ],
)
async def test_unfixable_call_raises_error_with_example(tool, args, example):
    ctx = MagicMock()
    ctx.message.name = tool
    ctx.message.arguments = args
    call_next = AsyncMock()
    with pytest.raises(ToolError, match=re.escape(example)):
        await ArgAliasMiddleware().on_call_tool(ctx, call_next)
    call_next.assert_not_awaited()


def test_middleware_is_registered_after_signal_capture():
    from src.main import mcp
    from src.services.signal_middleware import SignalCaptureMiddleware

    kinds = [type(m) for m in mcp.middleware]
    # SignalCapture が外側: 書き換え前の取り違えと例つきエラーが machine_error として観測される
    assert kinds.index(SignalCaptureMiddleware) < kinds.index(ArgAliasMiddleware)


@pytest.mark.asyncio
async def test_trailing_close_tags_are_stripped():
    out = await _run("answer_ask", {"answer_body": "本文。</answer_body>\n</invoke>"})
    assert out == {"answer_body": "本文。"}


@pytest.mark.asyncio
async def test_multiple_tags_and_trailing_newline_are_stripped():
    body = "本文</parameter>\n</invoke>\n"
    assert await _run("answer_ask", {"answer_body": body}) == {"answer_body": "本文"}


@pytest.mark.asyncio
async def test_close_tags_in_the_middle_are_kept():
    body = "説明: </invoke> が混入する。続き"
    assert await _run("answer_ask", {"answer_body": body}) == {"answer_body": body}


@pytest.mark.asyncio
async def test_trailing_tags_without_invoke_are_kept():
    body = "本文</parameter>"
    assert await _run("answer_ask", {"answer_body": body}) == {"answer_body": body}


@pytest.mark.asyncio
async def test_close_tags_in_nested_values_are_stripped():
    out = await _run(
        "add_logs",
        {"items": [{"topic_id": 1, "content": "c</content>\n</invoke>", "tags": ["a</invoke>"]}]},
    )
    assert out == {"items": [{"topic_id": 1, "content": "c", "tags": ["a"]}]}


@pytest.mark.asyncio
async def test_text_continuing_after_invoke_tag_is_kept():
    body = "本文</invoke>x"
    assert await _run("answer_ask", {"answer_body": body}) == {"answer_body": body}


@pytest.mark.asyncio
async def test_text_made_only_of_close_tags_becomes_empty():
    assert await _run("answer_ask", {"answer_body": "</invoke>"}) == {"answer_body": ""}


@pytest.mark.asyncio
async def test_non_string_values_pass_through():
    assert await _run("answer_ask", {"n": 3, "x": None}) == {"n": 3, "x": None}


@pytest.mark.asyncio
async def test_close_tags_are_stripped_before_aliases_are_applied():
    assert await _run("search", {"query": "x</invoke>"}) == {"keyword": "x"}


@pytest.mark.asyncio
async def test_many_close_tags_are_stripped_without_blowup():
    body = "x" + "</a>\n" * 50000 + "</invoke>"
    assert await _run("answer_ask", {"answer_body": body}) == {"answer_body": "x"}


@pytest.mark.asyncio
async def test_namespaced_invoke_tag_is_stripped():
    out = await _run("answer_ask", {"answer_body": "本文</ns:parameter>\n</ns:invoke>"})
    assert out == {"answer_body": "本文"}


@pytest.mark.asyncio
async def test_get_by_ids_ids_alias_with_json_string_and_typed_ids():
    out = await _run("get_by_ids", {"ids": '["decision:3321", {"type": "log", "id": 5}]'})
    assert out == {"items": [{"type": "decision", "id": 3321}, {"type": "log", "id": 5}]}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["add_logs", "add_decisions"])
async def test_items_alias_and_json_string_items(tool):
    assert await _run(tool, {"entries": '[{"topic_id": 1}]'}) == {"items": [{"topic_id": 1}]}


@pytest.mark.asyncio
async def test_flat_add_logs_with_json_string_tags_is_wrapped():
    out = await _run("add_logs", {"topic_id": 1, "content": "c", "tags": '["a"]'})
    assert out == {"items": [{"topic_id": 1, "content": "c", "tags": ["a"]}]}


@pytest.mark.asyncio
async def test_search_types_alias_unwraps_single_element():
    out = await _run("search", {"query": "x", "types": '["topic"]'})
    assert out == {"keyword": "x", "entity_type": "topic"}
    out = await _run("search", {"keyword": "x", "entity_types": ["topic", "log"]})
    assert out == {"keyword": "x", "entity_type": ["topic", "log"]}


@pytest.mark.asyncio
async def test_misc_renames():
    assert await _run("search_tags", {"keyword": "k"}) == {"query": "k"}
    assert await _run("get_material", {"id": "620"}) == {"material_id": "620"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,args,expected",
    [
        ("add_logs", {"logs": "[abc"}, {"items": "[abc"}),
        ("add_logs", {"items": '{"a": 1}'}, {"items": '{"a": 1}'}),
        ("search", {"keyword": "x", "type_filter": "topic"}, {"keyword": "x", "entity_type": "topic"}),
        ("add_decisions", {"decisions": '[{"topic_id": 1}]'}, {"items": [{"topic_id": 1}]}),
        ("add_logs", {"logs": [{"topic_id": 1}]}, {"items": [{"topic_id": 1}]}),
        ("get_by_ids", {"items": [" decision:1 ", 5]}, {"items": [{"type": "decision", "id": 1}, 5]}),
        ("get_decisions", {"entity_type": '["x"]', "entity_id": 1}, {"entity_type": '["x"]', "entity_id": 1}),
        ("add_logs", {"items": ["decision:1"]}, {"items": ["decision:1"]}),
    ],
)
async def test_edge_inputs_are_left_alone_or_normalized(tool, args, expected):
    assert await _run(tool, dict(args)) == expected
