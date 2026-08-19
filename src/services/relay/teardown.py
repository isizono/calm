"""session 除去時の subscription 撤去（best-effort）。

`SessionManager` が session を除去した瞬間（正常終了の `/session/unregister` と
liveness TTL 失効の両方）をフックに、その session が宣言していた subscription を
relay 側から DELETE し、declaration file / inbox / cursor を削除する。

relay 側の DELETE は失敗しても構わない（lease 失効 + 孤児 sweep で最終的に
自然消滅する）。file 撤去だけは必ず行う。撤去漏れ（server 自身がスレッド完走前に
死ぬ等）は孤児 sweep が backstop として拾う。
"""
from __future__ import annotations

import logging
import threading

from relay_sdk.errors import PermanentError, RelayProtocolError, TransientError
from relay_sdk.http.auth import make_client
from relay_sdk.http.request import delete_subscription
from src.services.relay import config, declarations, lease_loop

logger = logging.getLogger(__name__)


def schedule(session_id: str) -> None:
    """SessionManager の session 除去イベントから呼ぶ。撤去は別スレッドで行う。

    呼び出し元（SessionManager）のロックを撤去処理の I/O で塞がないよう、
    同期的には何もせずスレッドを起こすだけで返る。
    """
    threading.Thread(target=_teardown, args=(session_id,), daemon=True, name="relay-teardown").start()


def _teardown(session_id: str) -> None:
    decl = declarations.load(session_id)
    if decl is None:
        return
    _delete_relay_subscriptions(decl)
    lease_loop.delete_orphan_state(session_id)


def _delete_relay_subscriptions(decl: dict) -> None:
    """declaration が持つ subscription を relay 側から削除する（best-effort）。

    token 未設定（relay 未接続環境）なら HTTP 呼び出し自体を試みない。
    削除に失敗しても例外は外へ伝播させない（呼び出し元は file 撤去を続行する）。
    """
    token = config.get_token()
    if not token:
        return
    try:
        with make_client(config.get_base_url(), bearer_token=token) as client:
            for entry in decl.get("subscriptions", []):
                sub_id = entry.get("subscription_id")
                if not (isinstance(sub_id, str) and sub_id):
                    continue
                try:
                    delete_subscription(client, subscription_id=sub_id)
                except PermanentError:
                    pass  # 既に relay 側で消えている（404/410）は撤去成功と同義
                except (TransientError, RelayProtocolError):
                    pass  # best effort。取り逃しは孤児 sweep が拾う
    except Exception:
        logger.warning("relay 側 unsubscribe に失敗（file 撤去は継続）", exc_info=True)


__all__ = ["schedule"]
