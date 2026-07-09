-- Migration 0059: タグの退役(archived)状態を管理する列を追加
--
-- depends: 0058_add_habit_trigger_mode
--
-- 背景:
--   タグに退役フラグを持たせ、tag notes の自動注入からは完全除外しつつ、
--   search 等の取得系では削除せずラベル付きで下位表示できるようにする。
--
-- 変更内容:
--   - tags に archived_at（退役日時、既定NULL）を追加
--   - tags に archived_reason（退役理由の短いテキスト、既定NULL）を追加
--   - archived_at 用の部分インデックスを追加（archived_at IS NOT NULL の行のみ対象。
--     大多数の行は非archivedのままなので、部分インデックスでサイズを抑える）

ALTER TABLE tags ADD COLUMN archived_at TIMESTAMP DEFAULT NULL;
ALTER TABLE tags ADD COLUMN archived_reason TEXT DEFAULT NULL;

CREATE INDEX idx_tags_archived_at ON tags(archived_at) WHERE archived_at IS NOT NULL;
