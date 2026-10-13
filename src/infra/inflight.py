"""処理中のMCPリクエスト数の計測と、全リクエストが捌けるのを待つシャットダウンガード。

SIGINT直後に処理中だった呼び出しは、DB書き込みが完了していてもレスポンスが
切れて失敗に見え、クライアントのリトライで二重書き込みになる。これを避けるため、
シャットダウンは処理中が0件のときにだけ行う。
"""
import logging
import threading
import time
from collections.abc import Callable

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

logger = logging.getLogger(__name__)

DEFAULT_IDLE_WAIT_SEC = 60.0
_POLL_SEC = 0.05

_lock = threading.Lock()
_count = 0


def inflight_count() -> int:
    with _lock:
        return _count


class InflightMiddleware(Middleware):
    """MCPリクエストの処理中件数を数える。最も外側に登録する。"""

    async def on_message(self, context: MiddlewareContext, call_next: CallNext):
        global _count
        with _lock:
            _count += 1
        try:
            return await call_next(context)
        finally:
            with _lock:
                _count -= 1


def shutdown_when_idle(
    send_shutdown: Callable[[], None],
    timeout_sec: float = DEFAULT_IDLE_WAIT_SEC,
) -> bool:
    """処理中が0件になるまで待ってから send_shutdown を呼ぶ。

    timeout_sec 内に0件にならなければ呼ばずに False を返す（呼び出し元が次の
    周期で再試行する）。0件確認から send_shutdown までの間に届いた新規リクエストは
    防げない。待機中に新規を拒否するゲートは入れない: 拒否もクライアントから見れば
    失敗で、停止を見送った場合には不要な失敗を生むため。この窓は極小で、取りこぼしは
    SIGINT直後の失敗と同じ症状になる。
    """
    deadline = time.monotonic() + timeout_sec
    while inflight_count() > 0:
        if time.monotonic() >= deadline:
            logger.warning(
                "Shutdown deferred: %d request(s) still in flight after %.0fs",
                inflight_count(), timeout_sec,
            )
            return False
        time.sleep(_POLL_SEC)
    send_shutdown()
    return True
