"""capability matrix に基づく role 別 visibility の middleware。

initialize 時にセッションの role を解決し、その role から見える tool 集合だけ
tools/list に出るよう FastMCP の session-level visibility rules を設定する。
本 middleware は二層構造の hide 側のみを担い、実際の呼び出し可否判定 (reject) は
別途 guard 層が担当する。
"""
from __future__ import annotations

from typing import Any

import mcp.types as mt

from fastmcp.server.middleware import Middleware, MiddlewareContext, CallNext
from fastmcp.server.transforms.visibility import disable_components

from src.db import get_connection
from src.services import capability_matrix, role_service


class CapabilityVisibilityMiddleware(Middleware):
    """role に応じて tools/list から見えない tool を hide する middleware。

    on_initialize で session role を解決し、その role の hidden set を
    session-level visibility rule として登録する。role 未解決 (grace period)
    の場合は何も hide しない (guard 層で reject されるため見えても問題ない)。
    """

    async def on_initialize(
        self,
        context: MiddlewareContext[mt.InitializeRequest],
        call_next: CallNext[mt.InitializeRequest, mt.InitializeResult | None],
    ) -> mt.InitializeResult | None:
        result = await call_next(context)
        fastmcp_ctx = context.fastmcp_context
        if fastmcp_ctx is None:
            return result

        session_id = self._safe_session_id(fastmcp_ctx)
        role = self._resolve_role(session_id)
        if role is None:
            return result

        hidden = capability_matrix.hidden_tools_for(role)
        if not hidden:
            return result

        await disable_components(
            fastmcp_ctx,
            names=hidden,
            components={"tool"},
        )
        return result

    @staticmethod
    def _safe_session_id(fastmcp_ctx: Any) -> str | None:
        try:
            return fastmcp_ctx.session_id
        except RuntimeError:
            return None
        except AttributeError:
            return None

    @staticmethod
    def _resolve_role(session_id: str | None) -> str | None:
        conn = get_connection()
        try:
            return role_service.lookup_role(conn, session_id)
        finally:
            conn.close()
