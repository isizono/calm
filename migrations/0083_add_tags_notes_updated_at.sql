-- Migration 0083: tags.notes_updated_at 追加（notes本文の更新実績トラッキング用）
--
-- depends: 0081_vec_cosine_rebuild
--
-- 背景:
--   tag notesのdecay述語(is_decay_eligible)は、notes全文配信の実績
--   (last_injected_at)だけを参照実績として見ていた。update_tagでnotesを
--   書き込んでもlast_injected_atは更新されないため、作成からTAG_NOTES_DECAY_DAYS
--   を超えたタグにnotesを書いた直後でも、次に遭遇した瞬間に1行ポインタへ縮退する。
--
-- 変更内容:
--   - tags に notes_updated_at（notes本文の最終更新日時、既定NULL）を追加

ALTER TABLE tags ADD COLUMN notes_updated_at TIMESTAMP DEFAULT NULL;
