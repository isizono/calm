-- Migration 038: pinsテーブルのtarget逆引きindex + 削除時CASCADEトリガー
--
-- depends: 0037_add_decisions_title
--
-- 背景:
--   supersedes時のpin自動引き継ぎ（pin_service._transfer_pins_with_conn）で
--   target_type/target_id による逆引きが必要になる。0034コメントで予告されていた
--   idx_pins_target を追加する。
--
--   また、ハードDELETE経路（将来の削除サービス・tag削除等）からpinsがorphan化
--   するのを防ぐため、relations 0033 と同形の AFTER DELETE トリガーで
--   pinsのCASCADE削除をDB層で保証する。tag削除機能は現状未実装だが、
--   将来の保険として6本（5 entity + tags）すべて貼る。
--
-- 変更内容:
--   1. idx_pins_target（target_type, target_id）を追加
--   2. pinsのCASCADE削除トリガー × 6（topic/activity/material/decision/log/tag）

CREATE INDEX idx_pins_target ON pins(target_type, target_id);

-- pinsのCASCADE削除トリガー（ポリモーフィックFKのためトリガーで実現、0033 relationsと同形）

CREATE TRIGGER trg_pins_cascade_delete_topic
AFTER DELETE ON discussion_topics
FOR EACH ROW
BEGIN
    DELETE FROM pins WHERE (source_type = 'topic' AND source_id = OLD.id)
                       OR (target_type = 'topic' AND target_id = OLD.id);
END;

CREATE TRIGGER trg_pins_cascade_delete_activity
AFTER DELETE ON activities
FOR EACH ROW
BEGIN
    DELETE FROM pins WHERE (source_type = 'activity' AND source_id = OLD.id)
                       OR (target_type = 'activity' AND target_id = OLD.id);
END;

CREATE TRIGGER trg_pins_cascade_delete_material
AFTER DELETE ON materials
FOR EACH ROW
BEGIN
    DELETE FROM pins WHERE (source_type = 'material' AND source_id = OLD.id)
                       OR (target_type = 'material' AND target_id = OLD.id);
END;

CREATE TRIGGER trg_pins_cascade_delete_decision
AFTER DELETE ON decisions
FOR EACH ROW
BEGIN
    DELETE FROM pins WHERE (source_type = 'decision' AND source_id = OLD.id)
                       OR (target_type = 'decision' AND target_id = OLD.id);
END;

CREATE TRIGGER trg_pins_cascade_delete_log
AFTER DELETE ON discussion_logs
FOR EACH ROW
BEGIN
    DELETE FROM pins WHERE (source_type = 'log' AND source_id = OLD.id)
                       OR (target_type = 'log' AND target_id = OLD.id);
END;

CREATE TRIGGER trg_pins_cascade_delete_tag
AFTER DELETE ON tags
FOR EACH ROW
BEGIN
    DELETE FROM pins WHERE (source_type = 'tag' AND source_id = OLD.id)
                       OR (target_type = 'tag' AND target_id = OLD.id);
END;
