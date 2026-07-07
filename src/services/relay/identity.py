"""relay 呼び出し元の安定 identity 解決。

cc-memory の caller_session_id は本来 MCP 接続単位の ephemeral な値
（`role_service.get_caller_session_id()` 経由、fastmcp の `ctx.session_id`）
であり、cc-memory server の再起動のたびに新しい値へ切り替わる。

relay の declaration/inbox/subscription は「後から同じ相手に配達を続ける」
ことを前提にした永続状態であり、この ephemeral な値をキーにすると server
再起動のたびに宛先を見失う。

launcher.py（src/launcher.py）は Claude Code セッション（正確には launcher
プロセス）ごとに 1 度だけ発行する UUID を既に保持しており、この値を
X-CC-Memory-Bridge-Session-Id ヘッダとして全 MCP リクエストに同梱する。
本モジュールはこのヘッダを優先的に読み、relay 呼び出し元の識別子を解決する。
"""
from __future__ import annotations

from typing import Optional

from src.services.role_service import get_caller_session_id

BRIDGE_SESSION_HEADER = "x-cc-memory-bridge-session-id"


def get_relay_identity() -> Optional[str]:
    """relay 呼び出し元の識別子を解決する。

    launcher.py 経由（X-CC-Memory-Bridge-Session-Id ヘッダ）の呼び出しは、
    cc-memory server の再起動をまたいで不変な識別子を返す。ヘッダが無い
    呼び出し元（本ヘッダを付与しない MCP クライアント）、および HTTP
    リクエストコンテキスト外からの呼び出し（import失敗・get_http_headers()
    自体の失敗を含む）は、従来通り ctx.session_id（ephemeral、MCP 接続単位）
    にフォールバックする。
    """
    try:
        from fastmcp.server.dependencies import get_http_headers

        headers = get_http_headers()
    except Exception:
        return get_caller_session_id()

    stable_id = headers.get(BRIDGE_SESSION_HEADER)
    if isinstance(stable_id, str) and stable_id.strip():
        return stable_id.strip()
    return get_caller_session_id()


__all__ = ["BRIDGE_SESSION_HEADER", "get_relay_identity"]
