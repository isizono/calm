"""relay v2 診断・観測性 tool（relay_status）の実装本体。

post/publish/subscribe/receive の4動詞（service.py）とは独立した観測専用の面。
outbox配送状況のローカルDB照会のみを担う。relayサーバーへのHTTPアクセスは
発生しない（runtime健全性の取得は main.py 側で RelayRuntime インスタンスから
直接行うため、本モジュールは扱わない）。
"""
from __future__ import annotations

import json
from typing import Optional

from src.db import get_connection
from src.relay_sdk import config as sdk_config


def _error(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def _row_to_status(row) -> dict:
    if row["dead_at"] is not None:
        status = "dead"
    elif row["processed_at"] is not None:
        status = "delivered"
    else:
        status = "pending"
    return {
        "outbox_id": row["id"],
        "status": status,
        "labels": json.loads(row["labels"]) if row["labels"] else [],
        "title": row["title"],
        "created_at": row["created_at"],
        "processed_at": row["processed_at"],
        "dead_at": row["dead_at"],
        "retry_count": row["retry_count"],
        "last_error": row["last_error"],
    }


def outbox_status(outbox_id: Optional[int]) -> Optional[dict]:
    """outbox_id を指定した場合のみ relay_outbox 行の配送状況を返す。

    Returns:
        outbox_id が None: None（呼び出し側で outbox キーの値を null にする合図。
            キー自体は main.py 側で常に返り値に含める）
        見つかった場合: {"outbox_id", "status", "labels", "title", "created_at",
                        "processed_at", "dead_at", "retry_count", "last_error"}
        見つからない場合: {"error": {"code": "not_found", "message": str}}
        outbox_id の型/値が不正な場合: {"error": {"code": "validation", "message": str}}
    """
    if outbox_id is None:
        return None
    if not isinstance(outbox_id, int) or isinstance(outbox_id, bool) or outbox_id <= 0:
        return _error("validation", "outbox_id は正の整数で指定してください")

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, labels, title, created_at, processed_at,"
            " retry_count, last_error, dead_at"
            " FROM relay_outbox WHERE id = ?",
            (outbox_id,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return _error(
            "not_found",
            f"outbox_id={outbox_id} の relay_outbox 行が見つかりません"
            f"（存在しないID、または dead 化から"
            f" {sdk_config.DLQ_PHYSICAL_DELETE_DAYS} 日経過し物理削除された可能性があります）",
        )
    return _row_to_status(row)
