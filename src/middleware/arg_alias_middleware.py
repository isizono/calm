"""MCPツール引数名の取り違えを、呼び出しがバリデーションに届く前に吸収する middleware。

machine_error で繰り返し観測された取り違えだけを正しい名前へ書き換える。
一意に直せないものは、正しい呼び方の例つきで ToolError を返す。
ツールのシグネチャ・docstring には別名を足さない。
"""
from __future__ import annotations

from typing import Any

import mcp.types as mt

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

# ツール名 -> {取り違えた名前: 正しい名前}。正しい名前が未指定のときだけ書き換える。
_RENAMES: dict[str, dict[str, str]] = {
    "search": {"query": "keyword", "type": "entity_type"},
}

# get_logs / get_decisions は topic_id・activity_id を entity_type + entity_id に直す。
_ENTITY_TOOLS = {"get_logs", "get_decisions"}

# 1件の dict を items=[...] で包み忘れる書き方を吸収するツールと、その必須キー。
_ITEMS_WRAP: dict[str, tuple[str, ...]] = {
    "add_logs": ("topic_id", "content"),
    "add_decisions": ("topic_id", "decision", "reason"),
}

# 書き換えでは直せない取り違えに添える、正しい呼び方（必須引数が欠けたときのみ）。
_USAGE: dict[str, tuple[str, str]] = {
    "get_by_ids": ("items", 'get_by_ids(items=[{"type": "decision", "id": 123}])'),
    "add_logs": ("items", 'add_logs(items=[{"topic_id": 1, "content": "..."}])'),
    "add_decisions": (
        "items",
        'add_decisions(items=[{"topic_id": 1, "decision": "...", "reason": "..."}])',
    ),
}


def _rewrite(tool: str, args: dict[str, Any]) -> None:
    for wrong, right in _RENAMES.get(tool, {}).items():
        if wrong in args and right not in args:
            args[right] = args.pop(wrong)

    if tool in _ENTITY_TOOLS and "entity_type" not in args and "entity_id" not in args:
        for kind in ("topic", "activity"):
            if f"{kind}_id" in args:
                args["entity_type"] = kind
                args["entity_id"] = args.pop(f"{kind}_id")
                break

    required = _ITEMS_WRAP.get(tool)
    if required and "items" not in args and all(k in args for k in required):
        keys = [k for k in args if k not in ("flavor",)]
        args["items"] = [{k: args.pop(k) for k in keys}]


class ArgAliasMiddleware(Middleware):
    """観測済みの引数名の取り違えを書き換え、直せないものは例つきエラーで返す。"""

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, Any],
    ) -> Any:
        tool = context.message.name
        args = context.message.arguments
        if isinstance(args, dict):
            _rewrite(tool, args)
            usage = _USAGE.get(tool)
            if usage and usage[0] not in args:
                raise ToolError(
                    f"{tool}: 必須引数 {usage[0]} がありません。正しい呼び方: {usage[1]}"
                )
        return await call_next(context)
