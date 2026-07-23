-- Migration 0066: tags.notesラチェット則
--
-- depends: 0065_add_habits_always_pool_ratchet_trigger
--
-- 背景:
--   tag notes（tags.notes）はSessionStart系の遭遇時注入(collect_tag_notes_for_injection)
--   で全文表示される。1タグあたりのnotesが際限なく伸びるのを防ぐため、4000字を
--   超える「増加」更新のみを拒否するラチェットをDBトリガーで課す。縮む更新は
--   4000字超過中でも常に許可する。
--
-- 変更内容:
--   - notesが4000字を超え、かつ増加する INSERT/UPDATE を RAISE(ABORT) で拒否する
--     2トリガーを追加する
--
-- 注意: 対象は「タグ1件ごとのnotes長」であり、habitsのようなプール合計ではない
--   （tag notesにはalwaysプールに相当する固定注入枠の概念が現状存在しないため）。
--
-- 注意（将来のmigration作者向け）: tagsテーブルは0039で一度DROP+RENAME再構築されて
--   おり(trg_pins_cascade_delete_tagを再作成した実績あり)、将来また再構築する
--   migrationを書く場合は本トリガー2本も同様に再作成すること。

CREATE TRIGGER trg_tags_notes_ratchet_ceiling_ins
BEFORE INSERT ON tags
FOR EACH ROW
WHEN NEW.notes IS NOT NULL AND LENGTH(NEW.notes) > 4000
BEGIN
    SELECT RAISE(ABORT, 'tag notes ratchet ceiling (4000 chars) exceeded');
END;

CREATE TRIGGER trg_tags_notes_ratchet_ceiling_upd
BEFORE UPDATE OF notes ON tags
FOR EACH ROW
WHEN NEW.notes IS NOT NULL
     AND LENGTH(NEW.notes) > 4000
     AND (OLD.notes IS NULL OR LENGTH(NEW.notes) > LENGTH(OLD.notes))
BEGIN
    SELECT RAISE(ABORT, 'tag notes ratchet ceiling (4000 chars) exceeded and increasing');
END;
