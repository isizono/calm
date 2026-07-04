-- Migration 0052: タグjunctionテーブルへのtag_id逆引きindex + search_index.created_atのindex
--
-- depends: 0048_session_identity
--
-- 背景:
--   topic_tags / activity_tags / decision_tags / log_tags はPK(entity_id, tag_id)
--   のみでtag_id側の逆引きindexが無く、タグでの絞り込みクエリが実質フルスキャンに
--   なっている（material_tagsのみ0023でidx_material_tags_tagを持つ）。
--   search_index.created_atも0030でカラム追加のみでindex未整備であり、日付フィルタ
--   クエリの補助が無い。
--
-- 変更内容:
--   - idx_topic_tags_tag / idx_activity_tags_tag / idx_decision_tags_tag /
--     idx_log_tags_tag を追加
--   - idx_search_index_created_at を追加

CREATE INDEX idx_topic_tags_tag    ON topic_tags(tag_id);
CREATE INDEX idx_activity_tags_tag ON activity_tags(tag_id);
CREATE INDEX idx_decision_tags_tag ON decision_tags(tag_id);
CREATE INDEX idx_log_tags_tag      ON log_tags(tag_id);

CREATE INDEX idx_search_index_created_at ON search_index(created_at);
