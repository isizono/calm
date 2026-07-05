"""relay v2 Python SDK（relay-v2-sdk.md）。

publisher 側は `relay_sdk.outbox`、subscriber 側は `relay_sdk.client`、protocol 層は
`relay_sdk.http`。例外分類は `relay_sdk.errors`。
"""
from __future__ import annotations

from src.relay_sdk.errors import PermanentError, RelayProtocolError, TransientError

__all__ = ["RelayProtocolError", "TransientError", "PermanentError"]
