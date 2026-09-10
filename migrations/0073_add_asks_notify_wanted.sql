-- Migration 0073: asksに通知希望フラグ(notify_wanted)を追加
--
-- depends: 0072_repair_search_index_fts_orphans
--
-- 背景:
--   askの回答・却下をpull（次回check_in/get_asks）だけでなく低遅延で受け取りたい
--   同一セッション待ち型のユースケースに対応する。answer_ask/triage_ask(dismiss)側が
--   ファイルベースの通知（notify_path）を書くかどうかを、ask単位で制御する。
--
-- 変更内容:
--   asksにnotify_wanted（既定1=通知希望あり）を追加する。add_ask呼び出し元が
--   明示的にfalseを指定しない限り既定で通知希望として扱う。既存行は全てNOT NULL
--   DEFAULT 1のALTER TABLEにより既定値1で埋まる（後方互換）。

ALTER TABLE asks ADD COLUMN notify_wanted INTEGER NOT NULL DEFAULT 1
  CHECK (notify_wanted IN (0, 1));
