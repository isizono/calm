"""サブエージェントからのMCP呼び出しに付いた識別子引数を取り出す middleware。

PreToolUse hook（hooks/agent_identity_hook.py）は、サブエージェント内のCALM呼び出しに
引数 `_calm_agent_id` を足す。どのツールのスキーマにも無いキーなので、バリデーションに
届く前に取り除く必要がある。本 middleware は取り除いた値を ContextVar に置き、
ツール本体と後続の middleware が delivery_key() で既出管理キーを引けるようにする。

他の middleware より先に登録し、一番外側に置く。SignalCapture などが引数を記録する
前に取り除くため。
"""
from __future__ import annotations

from typing import Any

import mcp.types as mt
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from src.infra.session_identity import (
    AGENT_ID_ARG,
    AGENT_ID_MAX_LEN,
    reset_current_agent_id,
    set_current_agent_id,
)


class AgentIdentityMiddleware(Middleware):
    """識別子引数を引数から外し、ツール呼び出しの間だけ ContextVar に保持する。"""

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, Any],
    ) -> Any:
        args = context.message.arguments
        agent_id = None
        if isinstance(args, dict) and AGENT_ID_ARG in args:
            raw = args.pop(AGENT_ID_ARG)
            # hook が出す値は常に文字列なので、形が不正な場合は識別子なしとして扱う。
            # 引数からは形によらず必ず取り除く（スキーマに無いキーで検証を落とさないため）。
            if isinstance(raw, str) and raw.strip() and len(raw) <= AGENT_ID_MAX_LEN:
                agent_id = raw.strip()

        token = set_current_agent_id(agent_id)
        try:
            return await call_next(context)
        finally:
            reset_current_agent_id(token)
