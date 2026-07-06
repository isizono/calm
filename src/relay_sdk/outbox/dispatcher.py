"""outbox polling dispatcher daemon（relay-v2-sdk.md §2.3）。

outbox を polling して relay の `POST /publish` を呼ぶ常駐ループ。プロセス内シングルトン
（同一 db_path 上で複数 dispatcher が走ると二重 publish を起こすため、SQLite ファイル
lock で enforce する）。

retry backoff は「同一ループ内で待つのではなく次回 polling まで待つ」（§2.3.1 手順4）。
outbox schema には次回リトライ時刻の列が無いため、backoff の刻みは dispatcher プロセスの
in-memory state（`_backoff_until`）で管理する。dispatcher は単一プロセスのため in-memory で
十分で、プロセス再起動時は backoff state を失って即リトライになる（at-least-once を壊さない）。
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from src.relay_sdk import config as sdk_config
from src.relay_sdk.errors import PermanentError, RelayProtocolError, TransientError
from src.relay_sdk.http import make_client, post_publish

logger = logging.getLogger("relay_sdk.outbox.dispatcher")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


# ---------------------------------------------------------------------------
# dispatcher 単一プロセス enforcement（file lock）
# ---------------------------------------------------------------------------


class DispatcherAlreadyRunning(RuntimeError):
    """同一 db_path 上で別の dispatcher が既に lock を保持している。"""


def _acquire_lock(lock_path: str) -> int:
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        raise DispatcherAlreadyRunning(
            f"dispatcher lock '{lock_path}' は既に取得されています（二重起動）"
        ) from exc
    return fd


def _release_lock(fd: int) -> None:
    with contextlib.suppress(OSError):
        fcntl.flock(fd, fcntl.LOCK_UN)
    with contextlib.suppress(OSError):
        os.close(fd)


# ---------------------------------------------------------------------------
# 1 行を relay へ配達する
# ---------------------------------------------------------------------------


class _RowResult:
    DELIVERED = "delivered"
    TRANSIENT = "transient"
    DEAD = "dead"


def _deliver_row(client: httpx.Client, row: sqlite3.Row) -> tuple[str, str | None, float | None]:
    """1 行を `POST /publish` する。

    Returns:
        `(result, error_message, retry_after)`。result は `_RowResult` のいずれか。
    """
    try:
        ref = {"type": row["ref_type"], "id": row["ref_id"]}
        labels = json.loads(row["labels"]) if row["labels"] else []
    except (json.JSONDecodeError, TypeError) as exc:
        # labels 列が壊れている行はリトライしても直らない。dead 化してスキップし、
        # daemon ループ全体をクラッシュさせない（この decode を try 外に置くと、
        # daemon ループは sqlite3.Error しか捕捉しないため 1 行の不正データで
        # 全配達が止まるクラッシュループになる）。
        return _RowResult.DEAD, f"labels のデコードに失敗しました: {exc}", None

    try:
        post_publish(
            client,
            ref=ref,
            labels=labels,
            title=row["title"],
            idempotency_key=row["idempotency_key"],
        )
        return _RowResult.DELIVERED, None, None
    except RelayProtocolError as exc:
        # 400 / 403 / 404 → 即 dead（retry しても直らない、§4.4）。
        return _RowResult.DEAD, str(exc), None
    except TransientError as exc:
        # 429 / 5xx / 接続不能 → retry（§4.4）。
        return _RowResult.TRANSIENT, str(exc), exc.retry_after
    except PermanentError as exc:
        # publish には subscription_id が無く本来発生しないが、防御的に dead 扱い。
        return _RowResult.DEAD, str(exc), None


# ---------------------------------------------------------------------------
# 1 polling cycle
# ---------------------------------------------------------------------------


def _dispatch_once(
    conn: sqlite3.Connection,
    client: httpx.Client,
    *,
    max_retry: int,
    initial_backoff_seconds: float,
    backoff_factor: float,
    backoff_until: dict[int, float],
) -> int:
    """pending 行を 100 件 SELECT して配達を試みる。配達成功件数を返す。"""
    rows = conn.execute(
        "SELECT * FROM relay_outbox"
        " WHERE processed_at IS NULL AND dead_at IS NULL"
        " ORDER BY id LIMIT 100"
    ).fetchall()
    now_monotonic = time.monotonic()
    delivered = 0
    for row in rows:
        row_id = row["id"]
        # backoff 中の行は次回まで待つ（§2.3.1 手順4）。
        if backoff_until.get(row_id, 0.0) > now_monotonic:
            continue

        result, error, retry_after = _deliver_row(client, row)
        if result == _RowResult.DELIVERED:
            conn.execute(
                "UPDATE relay_outbox SET processed_at = ? WHERE id = ?", (_now_iso(), row_id)
            )
            conn.commit()
            backoff_until.pop(row_id, None)
            delivered += 1
        elif result == _RowResult.TRANSIENT:
            new_retry_count = row["retry_count"] + 1
            if new_retry_count >= max_retry:
                conn.execute(
                    "UPDATE relay_outbox"
                    " SET retry_count = ?, last_error = ?, dead_at = ? WHERE id = ?",
                    (new_retry_count, error, _now_iso(), row_id),
                )
                conn.commit()
                backoff_until.pop(row_id, None)
                logger.warning("outbox row %s を dead 化（retry 上限到達）: %s", row_id, error)
            else:
                conn.execute(
                    "UPDATE relay_outbox SET retry_count = ?, last_error = ? WHERE id = ?",
                    (new_retry_count, error, row_id),
                )
                conn.commit()
                # 429 は Retry-After を尊重、それ以外は指数バックオフ（§4.4 / §2.3.1）。
                delay = (
                    retry_after
                    if retry_after is not None
                    else initial_backoff_seconds * (backoff_factor ** row["retry_count"])
                )
                backoff_until[row_id] = time.monotonic() + delay
        else:  # DEAD（permanent error）
            conn.execute(
                "UPDATE relay_outbox SET last_error = ?, dead_at = ? WHERE id = ?",
                (error, _now_iso(), row_id),
            )
            conn.commit()
            backoff_until.pop(row_id, None)
            logger.warning("outbox row %s を dead 化（permanent error）: %s", row_id, error)
    return delivered


def _gc_dlq(conn: sqlite3.Connection) -> int:
    """`dead_at` から 7 日経過した行を物理 DELETE する（§2.1 / §2.3.1 手順6）。"""
    cutoff = (_now() - timedelta(days=sdk_config.DLQ_PHYSICAL_DELETE_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    cur = conn.execute(
        "DELETE FROM relay_outbox WHERE dead_at IS NOT NULL AND dead_at < ?", (cutoff,)
    )
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# 常駐ループ
# ---------------------------------------------------------------------------


def run_dispatcher(
    *,
    db_path: Path | str,
    relay_base_url: str,
    agent_card_path: Path | str | None = None,
    jws_key_path: Path | str | None = None,
    bearer_token: str | None = None,
    poll_interval_seconds: float = 0.5,
    max_retry: int = 5,
    initial_backoff_seconds: float = 0.1,
    backoff_factor: float = 2.0,
    dlq_gc_interval_seconds: float = 3600.0,
    http_timeout_seconds: float = 10.0,
    stop_event: threading.Event | None = None,
) -> None:
    """outbox を polling して relay へ配達する常駐ループ（§2.3.1）。

    終了は `stop_event.set()` で行う。プロセス内シングルトン（`<db_path>.dispatcher.lock`
    のファイル lock で二重起動を防ぐ。既に他プロセスが保持していれば
    `DispatcherAlreadyRunning`）。
    """
    db_path = str(db_path)
    lock_path = f"{db_path}.dispatcher.lock"
    stop = stop_event if stop_event is not None else threading.Event()

    lock_fd = _acquire_lock(lock_path)
    client = make_client(
        relay_base_url,
        bearer_token=bearer_token,
        jws_key_path=jws_key_path,
        agent_card_path=agent_card_path,
        timeout=http_timeout_seconds,
    )
    conn = _connect(db_path)
    backoff_until: dict[int, float] = {}
    last_gc = time.monotonic()

    logger.info("dispatcher 起動: db=%s relay=%s", db_path, relay_base_url)
    try:
        while not stop.is_set():
            try:
                _dispatch_once(
                    conn,
                    client,
                    max_retry=max_retry,
                    initial_backoff_seconds=initial_backoff_seconds,
                    backoff_factor=backoff_factor,
                    backoff_until=backoff_until,
                )
                if time.monotonic() - last_gc >= dlq_gc_interval_seconds:
                    _gc_dlq(conn)
                    last_gc = time.monotonic()
            except sqlite3.Error:
                logger.exception("dispatcher: outbox DB エラー（次回 poll で再試行）")
            stop.wait(poll_interval_seconds)
    finally:
        conn.close()
        client.close()
        _release_lock(lock_fd)
        logger.info("dispatcher 停止")
