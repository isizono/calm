"""MCP ツール呼び出し中の未捕捉例外を signal_events へ自動捕捉する middleware。

例外はそのまま re-raise するため、呼び出し元から見た挙動は不変。記録は
ベストエフォート（capture_signal_safe 経由でいかなる例外も外に漏らさない）で
あり、記録自体の失敗がツール呼び出しを壊すことはない。
"""
from __future__ import annotations

import traceback
from typing import Any

import mcp.types as mt

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from src.services import signal_service
from src.services.guard_service import CapabilityError

# detail に書き込む traceback + 引数ダイジェストの最大文字数。値そのものは含めず
# key と型のみを記録する（機密情報混入の回避）。DB カラムに上限は無いが、
# detail が肥大化しないよう妥当な長さで切る。
_DETAIL_MAX_LEN = 500


class SignalCaptureMiddleware(Middleware):
    """role guard による正常な拒否 (CapabilityError) を除く全ての例外を machine_error として記録する。"""

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, Any],
    ) -> Any:
        try:
            return await call_next(context)
        except CapabilityError:
            raise  # ガードによる正常な拒否は故障ではない
        except Exception as e:
            signal_service.capture_signal_safe(
                kind="machine_error",
                summary=f"{type(e).__name__}: {str(e)[:200]}",
                source=f"tool:{context.message.name}",
                detail=_traceback_and_args_digest(e, context),
                session_id=_safe_session_id(context),
            )
            raise


def _traceback_and_args_digest(exc: Exception, context: MiddlewareContext) -> str:
    """例外の traceback と呼び出し引数の (key, 型) ダイジェストを生成する。

    引数の値そのものは含めない。全体を _DETAIL_MAX_LEN 文字に切り詰める際は
    末尾（例外メッセージ・直近フレーム）を優先して残すため、末尾から切り出す。
    """
    tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))

    args_summary = ""
    try:
        arguments = context.message.arguments or {}
        args_summary = ", ".join(f"{k}:{type(v).__name__}" for k, v in arguments.items())
    except Exception:
        pass

    digest = f"{tb_text}\nargs: {args_summary}"
    return digest[-_DETAIL_MAX_LEN:]


def _safe_session_id(context: MiddlewareContext) -> str | None:
    try:
        return context.fastmcp_context.session_id if context.fastmcp_context else None
    except Exception:
        return None
