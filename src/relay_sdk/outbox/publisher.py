"""publisher 側 `publish()` 本体 + debug 用 `poll` / `mark_delivered`（relay-v2-sdk.md §2.2, §2.4）。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from sqlite3 import Connection
from typing import Sequence, TypedDict

from src.relay_sdk.config import MAX_TITLE_CHARS


class OutboxRow(TypedDict):
    id: int
    ref_type: str
    ref_id: str
    labels: list[str]
    title: str | None
    idempotency_key: str
    created_at: str
    processed_at: str | None
    retry_count: int
    last_error: str | None
    dead_at: str | None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def publish(
    conn: Connection,
    *,
    ref_type: str,
    ref_id: str | int,
    labels: Sequence[str],
    title: str | None = None,
) -> int:
    """呼び出し元 `conn` の transaction に乗って outbox 行を INSERT する（§2.2）。

    SDK は commit / rollback を呼ばない。業務 write と本 INSERT の atomicity は呼び出し側が
    同一 transaction で両方走らせることで成立する。

    Returns:
        INSERT された outbox 行の id。

    Raises:
        ValueError: labels が空 / title が 200 chars 超 / ref_id が空。
    """
    labels_list = list(labels)
    if not labels_list:
        raise ValueError("labels は空にできません（AND set として最低 1 件必要）")
    if not all(isinstance(label, str) for label in labels_list):
        raise ValueError("labels は文字列の配列で指定してください")
    if ref_id is None or (isinstance(ref_id, str) and ref_id == ""):
        raise ValueError("ref_id は空にできません")
    if title is not None and len(title) > MAX_TITLE_CHARS:
        raise ValueError(
            f"title は {MAX_TITLE_CHARS} chars 以内にしてください"
            "（SDK は truncate しない。publisher 側で切り詰めること）"
        )

    labels_json = json.dumps(labels_list, ensure_ascii=False)
    created_at = _now_iso()
    # idempotency_key は NOT NULL のため placeholder で INSERT → lastrowid を str() 化して
    # 同一 tx で UPDATE（autoincrement の id をそのまま key に流用、§2.2）。
    cur = conn.execute(
        "INSERT INTO relay_outbox"
        " (ref_type, ref_id, labels, title, idempotency_key, created_at)"
        " VALUES (?, ?, ?, ?, '', ?)",
        (ref_type, str(ref_id), labels_json, title, created_at),
    )
    row_id = cur.lastrowid
    conn.execute(
        "UPDATE relay_outbox SET idempotency_key = ? WHERE id = ?", (str(row_id), row_id)
    )
    return row_id


def _row_to_outbox_row(row) -> OutboxRow:
    return OutboxRow(
        id=row["id"],
        ref_type=row["ref_type"],
        ref_id=row["ref_id"],
        labels=json.loads(row["labels"]) if row["labels"] else [],
        title=row["title"],
        idempotency_key=row["idempotency_key"],
        created_at=row["created_at"],
        processed_at=row["processed_at"],
        retry_count=row["retry_count"],
        last_error=row["last_error"],
        dead_at=row["dead_at"],
    )


def poll(conn: Connection, limit: int = 100) -> list[OutboxRow]:
    """pending な outbox 行を id 昇順で `limit` 件返す（dispatcher と同じ条件、§2.4）。

    用途は debug / 検査のみ。読み出すだけで publish しないため、dispatcher と並行しても
    二重配達は起きない。
    """
    rows = conn.execute(
        "SELECT * FROM relay_outbox"
        " WHERE processed_at IS NULL AND dead_at IS NULL"
        " ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()
    return [_row_to_outbox_row(row) for row in rows]


def mark_delivered(conn: Connection, ids: Sequence[int]) -> None:
    """指定 id の outbox 行に `processed_at` をセットする（運用救済用、§2.4）。

    通常パスでは dispatcher が UPDATE するため呼ばない。
    """
    ids_list = list(ids)
    if not ids_list:
        return
    now = _now_iso()
    placeholders = ",".join("?" for _ in ids_list)
    conn.execute(
        f"UPDATE relay_outbox SET processed_at = ? WHERE id IN ({placeholders})",
        (now, *ids_list),
    )
    conn.commit()
