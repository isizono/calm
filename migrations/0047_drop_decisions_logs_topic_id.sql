-- Migration 0047: decisions.topic_id / discussion_logs.topic_id カラム物理削除 (Contract)
--
-- depends: 0046_relations_belongs_to_unify
--
-- 背景:
--   migration 0046 で親 topic との紐付けを relations.belongs_to に統一し、
--   `decisions.topic_id` / `discussion_logs.topic_id` カラムは NULLABLE 化したうえで
--   書き込みパス・読み取り経路を relations.belongs_to 一本に切替えた。
--   旧 FK カラムは中間状態で互換性のために残置していたが、本 migration で物理削除する。
--
-- 変更内容:
--   - decisions.topic_id カラムを DROP
--   - discussion_logs.topic_id カラムを DROP
--
-- 前提条件 (0046 で確保済):
--   - idx_decisions_topic_id / idx_logs_topic_id は DROP 済
--   - decisions / discussion_logs のトリガーは topic_id を参照しない形に再作成済
--   - 書き込みコード (add_decisions / add_logs) は topic_id を INSERT に含めない
--   - 読み取りコード (get_decisions / get_logs / search / timeline / checkin / topic / hint)
--     は relations.belongs_to 経由

ALTER TABLE decisions DROP COLUMN topic_id;

ALTER TABLE discussion_logs DROP COLUMN topic_id;
