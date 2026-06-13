-- Migration 035: pinned列の撤去（pins有向関係への移行完了）
--
-- depends: 0034_pins_directed_relation
--
-- 背景:
--   0034でpinsテーブルへの有向関係移行が完了した。
--   従来の discussion_logs / decisions / materials の pinned 列は不要になるため DROP する。
--
-- NOTE: 不可逆（down無し）。データ移行は0034で完了済みのため本migrationはDROPのみ。
--   SQLite 3.35+ で ALTER TABLE ... DROP COLUMN が使用可能。

ALTER TABLE discussion_logs DROP COLUMN pinned;
ALTER TABLE decisions DROP COLUMN pinned;
ALTER TABLE materials DROP COLUMN pinned;
