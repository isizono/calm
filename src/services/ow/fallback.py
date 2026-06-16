"""read-fallback シム（Phase 1 限定、Phase 2 で削除予定）。

ow_workers MV にデータが入っていない過渡期に、旧 queue-t<topic_id>.md を読みに行く
互換層。新schema切替（順序3）前後で、ow_status / ow_recover 等が「データ移行前」も
「移行後」も同じインタフェースで読めるようにする。

設計書 M#288 §3.6 Phase 1:
- ow_workers が空 / 該当レコードなしの場合、旧 queue-t*.md にフォールバック
- Phase 2 cutover 後にシム削除
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from src.services.ow import workers as wk


def _read_legacy_queue_workers(topic_id: int | str) -> list[dict]:
    """旧 queue-t<topic_id>.md から worker 相当のリストを返す。

    ow_service.py の private ヘルパーを再利用する（Phase 1 限定の暫定依存）。
    Phase 2 で queue ファイル自体が廃止される際にこの関数ごと削除する。

    返却 dict は ow_workers 行に擬似的に整形した最低限の情報:
      {"handle", "alias", "task_n", "workload_state", "term_ref", "title"}
    """
    # 局所 import で副作用（relayサーバー起動等）の発生を遅延させる
    from src.services.ow_service import _get_queue_dir, _parse_queue_file

    queue_dir = _get_queue_dir()
    queue_file = Path(queue_dir) / f"queue-t{topic_id}.md"
    if not queue_file.exists():
        return []
    _, tasks = _parse_queue_file(queue_file)
    legacy: list[dict] = []
    for t in tasks:
        task_raw = t.get("task") or ""
        try:
            task_n = int(task_raw.lstrip("T") or "0")
        except ValueError:
            task_n = 0
        worker = t.get("worker") or ""
        # status (queued/spawning/in_progress/done) → workload_state 緩いマッピング
        status = (t.get("status") or "").lower()
        workload_state = {
            "queued": "spawning",
            "spawning": "spawning",
            "in_progress": "working",
            "working": "working",
            "done": "terminated",
            "awaiting_verify": "draining",
        }.get(status, "spawning")
        legacy.append({
            "handle": worker,
            "alias": worker,
            "task_n": task_n,
            "workload_state": workload_state,
            "term_ref": t.get("term_ref"),
            "title": t.get("title"),
            "_source": "legacy_queue",
        })
    return legacy


def read_workers_with_fallback_with_conn(
    conn: sqlite3.Connection,
    *,
    channel_code: str,
    topic_id: int | str,
    alive_only: bool = True,
) -> dict:
    """ow_workers を優先、空なら旧queue.mdへフォールバックする読み出し。

    Returns:
        {
            "source": "ow_workers" | "legacy_queue",
            "workers": list[dict],
        }
        source="ow_workers" の場合は ow_workers テーブル行をそのまま返す。
        source="legacy_queue" の場合は queue.md パース結果を擬似 ow_workers 形式に
        変換した dict のリストを返す。
    """
    rows = wk.list_workers_with_conn(
        conn, channel_code=channel_code, alive_only=alive_only,
    )
    if rows:
        return {"source": "ow_workers", "workers": rows}
    legacy = _read_legacy_queue_workers(topic_id)
    return {"source": "legacy_queue", "workers": legacy}
