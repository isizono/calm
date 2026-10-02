-- Migration 0084: hint自動クールダウンの専用テーブルを追加
--
-- depends: 0081_vec_cosine_rebuild
--
-- 背景:
--   hint_serviceが発火に伴い自動追記する日次クールダウンマーカーは、これまで
--   tags.notes本文に追記していた。tags.notesはmigration 0066のラチェット天井
--   (4000字)を持つため、既に天井を超えているタグでは追記がIntegrityErrorで
--   拒否され、同じhintが判定のたびに再発火し続けていた。
--
-- 変更内容:
--   hint_cooldownsテーブルを新設し、自動クールダウンの保存先をtags.notesから
--   分離する。既存でtags.notes本文に残っている自動マーカー（過去に書き込まれた
--   `<marker>-until:YYYY-MM-DD`）は本テーブルへ移行しない。notesの読み取り判定
--   (hint_service._is_marker_active)は従来どおりnotes本文も参照するため、
--   移行しなくても読み取りには影響しない。

CREATE TABLE hint_cooldowns (
    tag_id      INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    marker      TEXT NOT NULL,
    until_date  TEXT NOT NULL,
    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tag_id, marker)
);
