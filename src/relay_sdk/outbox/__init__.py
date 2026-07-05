"""publisher 側 SDK（relay-v2-sdk.md §2）。

publisher アプリは `from relay_sdk.outbox import publish, run_dispatcher` を使う。
"""
from __future__ import annotations

from src.relay_sdk.outbox.dispatcher import DispatcherAlreadyRunning, run_dispatcher
from src.relay_sdk.outbox.publisher import OutboxRow, mark_delivered, poll, publish
from src.relay_sdk.outbox.schema import (
    CREATE_OUTBOX_INDEX,
    CREATE_OUTBOX_TABLE,
    create_outbox_table,
)

__all__ = [
    "publish",
    "poll",
    "mark_delivered",
    "OutboxRow",
    "run_dispatcher",
    "DispatcherAlreadyRunning",
    "create_outbox_table",
    "CREATE_OUTBOX_TABLE",
    "CREATE_OUTBOX_INDEX",
]
