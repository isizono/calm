-- Migration 0056: relay_outbox テーブル追加
--
-- depends: 0055_add_migration_ledger
--
-- 背景:
--   セッション間通信の publish（labels routing 配布）は at-least-once 保証のため
--   relay へ直接 HTTP POST せず、まず本テーブルへ INSERT し、server 内の常駐
--   配達ループが pending 行を relay に配達する（transactional outbox パターン）。
--   業務 write と同一 transaction で INSERT できるよう memory.db 本体に置く。
--
-- スキーマ:
--   relay_sdk パッケージの DDL（relay_sdk/outbox/schema.py、relay リポジトリからの
--   依存パッケージ）を正とし、同一形状を migration chain に組み込む。SDK 側が
--   更新された場合は形状差分を新規 migration で追従する（本ファイルは事後改変しない）。
--
--   id               主キー（idempotency_key の生成元にも流用される）
--   ref_type         通知が指す対象の種別
--   ref_id           通知が指す対象の識別子
--   labels           JSON array（配送マッチング用 labels）
--   title            一覧表示用の見出し（NULL 可）
--   idempotency_key  relay 側 dedup 用キー（SDK が id から自動生成）
--   created_at       ISO8601 UTC
--   processed_at     NULL = 配達待ち（pending）
--   retry_count      配達リトライ回数
--   last_error       直近の配達エラー
--   dead_at          NOT NULL = 配達断念（DLQ 行き。7 日後に物理削除）

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
);

CREATE INDEX IF NOT EXISTS idx_relay_outbox_pending
  ON relay_outbox(id)
  WHERE processed_at IS NULL AND dead_at IS NULL;
