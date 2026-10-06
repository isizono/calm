"""MCPツール引数名の取り違えを、呼び出しがバリデーションに届く前に吸収する middleware。

machine_error で繰り返し観測された取り違えだけを正しい名前へ書き換える。
一意に直せないものは、正しい呼び方の例つきで ToolError を返す。
ツールのシグネチャ・docstring には別名を足さない。
"""
from __future__ import annotations

import json
import re
from typing import Any

import mcp.types as mt
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

# ツール名 -> {取り違えた名前: 正しい名前}。正しい名前が未指定のときだけ書き換える。
_RENAMES: dict[str, dict[str, str]] = {
    "search": {
        "query": "keyword",
        "type": "entity_type",
        "types": "entity_type",
        "entity_types": "entity_type",
        "type_filter": "entity_type",
    },
    "search_tags": {"keyword": "query"},
    "get_material": {"id": "material_id"},
    "get_by_ids": {"ids": "items"},
    "add_logs": {"entries": "items", "logs": "items"},
    "add_decisions": {"entries": "items", "decisions": "items"},
}

# 配列を JSON 文字列のまま渡されたときに配列へ戻す引数名。
_LIST_ARGS = {"items", "tags", "related", "targets", "changes", "entity_type"}

# get_by_ids の items に "decision:123" の文字列で書かれた要素を dict に直す。
_TYPED_ID = re.compile(r"(topic|decision|activity|log|material):(\d+)")

# get_logs / get_decisions は topic_id・activity_id を entity_type + entity_id に直す。
_ENTITY_TOOLS = {"get_logs", "get_decisions"}

# 1件の dict を items=[...] で包み忘れる書き方を吸収するツールと、(必須キー, 任意キー)。
# これ以外のキーが混ざる呼び出しは包まず、そのまま例つきエラーに回す。
_ITEMS_WRAP: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "add_logs": (("topic_id", "content"), ("title", "tags")),
    "add_decisions": (("topic_id", "decision", "reason"), ("title", "tags")),
}

# 書き換えでは直せない取り違えに添える、正しい呼び方（必須引数が欠けたときのみ）。
_USAGE: dict[str, tuple[str, str]] = {
    "get_by_ids": ("items", 'get_by_ids(items=[{"type": "decision", "id": 123}])'),
    "add_logs": ("items", 'add_logs(items=[{"topic_id": 1, "content": "..."}])'),
    "add_decisions": (
        "items",
        'add_decisions(items=[{"topic_id": 1, "decision": "...", "reason": "..."}])',
    ),
    "check_in": ("activity_id", "check_in(activity_id=123)"),
    "update_goal": (
        "goal_id",
        'update_goal(goal_id=1, changes=[{"op": "set", "id": 10, "state": "satisfied"}])'
        "（goal_id は get_goal(handle=...) で引く）",
    ),
    "add_material": (
        "source",
        'add_material(title="...", content="...", tags=["domain:x"], source="出典の説明")',
    ),
}


# 呼び出し側の書式ミスで文字列引数の末尾に混入する閉じタグの並び。最後が invoke
# の閉じタグ（名前空間付き可）のときだけ、間の空白ごと除く。本文の途中は触らない。
_CLOSE_TAG = re.compile(r"</[\w:.\-]+>")
_INVOKE_TAG = re.compile(r"</(?:[\w.\-]+:)?invoke>")


def _strip_trailing_close_tags(text: str) -> str:
    """末尾から閉じタグを1つずつ剥がす。文字列をコピーせず添字だけで進めるので線形で済む。"""

    def rstrip_end(end: int) -> int:
        while end > 0 and text[end - 1].isspace():
            end -= 1
        return end

    end = rstrip_end(len(text))
    if end == 0 or text[end - 1] != ">":
        return text
    start = text.rfind("</", 0, end)
    if start < 0 or not _INVOKE_TAG.fullmatch(text, start, end):
        return text
    cut = start
    while True:
        end = rstrip_end(cut)
        start = text.rfind("</", 0, end)
        if start < 0 or not _CLOSE_TAG.fullmatch(text, start, end):
            return text[:cut]
        cut = start


def _strip_close_tags(value: Any) -> Any:
    if isinstance(value, str):
        return _strip_trailing_close_tags(value)
    if isinstance(value, list):
        return [_strip_close_tags(v) for v in value]
    if isinstance(value, dict):
        return {k: _strip_close_tags(v) for k, v in value.items()}
    return value


def _rewrite(tool: str, args: dict[str, Any]) -> None:
    for wrong, right in _RENAMES.get(tool, {}).items():
        if wrong in args:
            value = args.pop(wrong)
            args.setdefault(right, value)

    for key in _LIST_ARGS & args.keys():
        value = args[key]
        if isinstance(value, str) and value.lstrip().startswith("["):
            try:
                decoded = json.loads(value)
            except ValueError:
                continue
            if isinstance(decoded, list):
                args[key] = decoded

    # entity_type は単一値。1要素の配列だけ中身を取り出す（複数はそのままエラーにする）。
    entity_type = args.get("entity_type")
    if tool == "search" and isinstance(entity_type, list) and len(entity_type) == 1:
        args["entity_type"] = entity_type[0]

    if tool == "get_by_ids" and isinstance(args.get("items"), list):
        args["items"] = [
            {"type": m[1], "id": int(m[2])}
            if isinstance(v, str) and (m := _TYPED_ID.fullmatch(v.strip()))
            else v
            for v in args["items"]
        ]

    if tool in _ENTITY_TOOLS and "entity_type" not in args and "entity_id" not in args:
        for kind in ("topic", "activity"):
            if f"{kind}_id" in args:
                args["entity_type"] = kind
                args["entity_id"] = args.pop(f"{kind}_id")
                break

    wrap = _ITEMS_WRAP.get(tool)
    if wrap and "items" not in args:
        required, optional = wrap
        if all(k in args for k in required) and set(args) <= {*required, *optional}:
            args["items"] = [dict(args)]
            for k in args["items"][0]:
                del args[k]


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
            args.update(_strip_close_tags(args))
            _rewrite(tool, args)
            usage = _USAGE.get(tool)
            if usage and usage[0] not in args:
                raise ToolError(
                    f"{tool}: 必須引数 {usage[0]} がありません。正しい呼び方: {usage[1]}"
                )
        return await call_next(context)
