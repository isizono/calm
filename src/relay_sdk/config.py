"""relay v2 SDK の設定 / 環境変数解決（relay-v2-sdk.md §6）。

`subscribe()` / `run_dispatcher()` は引数で明示された値を優先し、省略された値は
ここで環境変数から解決する（§6 末尾「引数で渡された値が優先される」）。CLI
entrypoint（`python -m relay_sdk.outbox`）は全設定を環境変数から読む。
"""
from __future__ import annotations

import os

# §6 の default 値。
DEFAULT_POLL_INTERVAL_SECONDS = 0.5
DEFAULT_MAX_RETRY = 5
DEFAULT_INITIAL_BACKOFF_SECONDS = 0.1
DEFAULT_BACKOFF_FACTOR = 2.0
DEFAULT_DLQ_GC_INTERVAL_SECONDS = 3600.0
DEFAULT_SSE_KEEPALIVE_SECONDS = 30.0
DEFAULT_SSE_RECONNECT_BACKOFF_CAP_SECONDS = 30.0
DEFAULT_HTTP_TIMEOUT_SECONDS = 10.0

# dead_at からの物理削除猶予（relay-v2-sdk.md §2.1）。
DLQ_PHYSICAL_DELETE_DAYS = 7

# title の上限（relay-v2-sdk.md §2.2 / wire-api.md §5.4）。
MAX_TITLE_CHARS = 200

# SSE dedup LRU の保持件数（relay-v2-sdk.md §4.2）。
DEDUP_LRU_SIZE = 10000

# SSE 受信の memory 安全上限（relay-v2-sdk.md §4.2）。relay の notification frame は
# 数 KB 程度（ref + labels + title<=200 chars）。壊れた/悪意ある巨大 frame や、改行を
# 送らないサーバに対してメモリを無制限に食わないための頭打ち。生 wire に対する上限
# なので JSON decode 前に効く。
SSE_MAX_FRAME_BYTES = 1 << 20  # 1 frame（event）の data 累積 byte 上限（1 MiB）
SSE_MAX_BUFFER_BYTES = 1 << 20  # 改行未達の 1 行としてバッファできる byte 上限（1 MiB）


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def env_base_url(explicit: str | None) -> str:
    url = explicit if explicit is not None else os.environ.get("RELAY_BASE_URL")
    if not url:
        raise ValueError("RELAY_BASE_URL（relay の base URL）が必要です")
    return url


def env_bearer_token() -> str | None:
    return os.environ.get("RELAY_BEARER_TOKEN") or None


def env_poll_interval_seconds() -> float:
    """`RELAY_OUTBOX_POLL_INTERVAL_MS`（ミリ秒）を秒に変換して返す。"""
    ms = os.environ.get("RELAY_OUTBOX_POLL_INTERVAL_MS")
    if ms in (None, ""):
        return DEFAULT_POLL_INTERVAL_SECONDS
    return int(ms) / 1000.0


def env_max_retry() -> int:
    return _env_int("RELAY_OUTBOX_MAX_RETRY", DEFAULT_MAX_RETRY)


def env_initial_backoff_seconds() -> float:
    ms = os.environ.get("RELAY_OUTBOX_INITIAL_BACKOFF_MS")
    if ms in (None, ""):
        return DEFAULT_INITIAL_BACKOFF_SECONDS
    return int(ms) / 1000.0


def env_backoff_factor() -> float:
    return _env_float("RELAY_OUTBOX_BACKOFF_FACTOR", DEFAULT_BACKOFF_FACTOR)


def env_dlq_gc_interval_seconds() -> float:
    return _env_float("RELAY_OUTBOX_DLQ_GC_INTERVAL_S", DEFAULT_DLQ_GC_INTERVAL_SECONDS)


def env_sse_keepalive_seconds() -> float:
    return _env_float("RELAY_SSE_KEEPALIVE_S", DEFAULT_SSE_KEEPALIVE_SECONDS)


def env_reconnect_max_attempts() -> int | None:
    """`RELAY_SSE_RECONNECT_MAX_ATTEMPTS`。`0` は無限（None）として扱う（§6）。"""
    n = _env_int("RELAY_SSE_RECONNECT_MAX_ATTEMPTS", 0)
    return None if n == 0 else n


def env_reconnect_backoff_cap_seconds() -> float:
    return _env_float(
        "RELAY_SSE_RECONNECT_BACKOFF_CAP_S", DEFAULT_SSE_RECONNECT_BACKOFF_CAP_SECONDS
    )


def env_http_timeout_seconds() -> float:
    return _env_float("RELAY_HTTP_TIMEOUT_S", DEFAULT_HTTP_TIMEOUT_SECONDS)
