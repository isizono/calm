-- Migration 0068: asks に kind カラムとタグ機構（ask_tags）を追加
--
-- depends: 0067_add_injection_telemetry
--
-- 背景:
--   asks（migration 0062）はv1では「タグ・リレーションには接続しない」設計だったが、
--   同型の問いが繰り返され裁定が一貫していると判断された場合にメタask（kind='meta'）
--   として一般化ルールの起票を検討するワークフロー（ask-distill skill）を導入する
--   にあたり、asksにもタグ体系を接続する。
--
-- 変更内容:
--   - asks.kind カラム追加。'ask'（通常ask、デフォルト）と'meta'（メタask）の2値のみ
--     CHECK制約で固定する
--   - ask_tags 中間テーブルを追加。decision_tags（0009）・material_tags（0023）と
--     全く同型（PRIMARY KEY (ask_id, tag_id)、tag_id側にFK逆引きインデックス）
--
-- 既存データへの遡及適用は行わない:
--   既存31件のaskはkind='ask'（デフォルト値）で埋まり、タグは付与しない
--   （タグは必須項目のためadd_ask経由の新規askにのみ強制される。既存行は
--   ask_tagsに行を持たないだけで、get_asksのtags=[]返却として自然に扱える）。

ALTER TABLE asks ADD COLUMN kind TEXT NOT NULL DEFAULT 'ask'
    CHECK (kind IN ('ask', 'meta'));

CREATE TABLE ask_tags (
    ask_id  INTEGER NOT NULL,
    tag_id  INTEGER NOT NULL,
    PRIMARY KEY (ask_id, tag_id),
    FOREIGN KEY (ask_id) REFERENCES asks(id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
);

CREATE INDEX idx_ask_tags_tag ON ask_tags(tag_id);
