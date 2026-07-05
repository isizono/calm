"""outbox テーブル定義（relay-v2-sdk.md §2.1）。

利用アプリの migration chain に組み込んで使う。SDK は `CREATE TABLE IF NOT EXISTS`
形式の DDL を公開し、`create_outbox_table(conn)` で一括適用できる薄いヘルパも提供する。
"""
from __future__ import annotations

from sqlite3 import Connection

OUTBOX_TABLE_NAME = "relay_outbox"

CREATE_OUTBOX_TABLE = """
CREATE TABLE IF NOT EXISTS relay_outbox (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ref_type        TEXT    NOT NULL,
  ref_id          TEXT    NOT NULL,
  labels          TEXT    NOT NULL,             -- JSON array
  title           TEXT,
  idempotency_key TEXT    NOT NULL,             -- SDK が auto-generate（id を流用）
  created_at      TEXT    NOT NULL,             -- ISO8601 UTC
  processed_at    TEXT,                         -- NULL = pending
  retry_count     INTEGER NOT NULL DEFAULT 0,
  last_error      TEXT,
  dead_at         TEXT                          -- NOT NULL = DLQ 行き
)
""".strip()

CREATE_OUTBOX_INDEX = """
CREATE INDEX IF NOT EXISTS idx_relay_outbox_pending
  ON relay_outbox(id)
  WHERE processed_at IS NULL AND dead_at IS NULL
""".strip()


def create_outbox_table(conn: Connection, *, commit: bool = True) -> None:
    """`relay_outbox` テーブルと pending index を作成する。

    migration ツールを使わないアプリ向けの one-shot ヘルパ。`commit=False` にすると
    呼び出し側の transaction に委ねる。
    """
    conn.execute(CREATE_OUTBOX_TABLE)
    conn.execute(CREATE_OUTBOX_INDEX)
    if commit:
        conn.commit()
