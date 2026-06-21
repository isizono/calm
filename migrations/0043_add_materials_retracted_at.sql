-- Migration 0043: materials に retracted_at カラムを追加
--
-- depends: 0042_citations_table
--
-- 背景:
--   誤って作成した material を論理削除（取り消し）するための準備。
--   NULL が有効（未取り消し）、値ありが取り消し済み。
--   decisions/discussion_logs と同様の retract 機構を materials にも対称的に持たせる
--   （migration 0031 の materials 版に相当）。
--
-- 変更内容:
--   materials テーブルに retracted_at TIMESTAMP NULL を追加

ALTER TABLE materials ADD COLUMN retracted_at TIMESTAMP NULL;
