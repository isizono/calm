-- Migration 0062: tags.last_injected_at 追加（tag notes decay述語のトラッキング用）
--
-- depends: 0061_add_tag_archived
--
-- 背景:
--   tag notesの遭遇時注入(collect_tag_notes_for_injection)が実際にnotesを全文配信
--   した機械可測な実績を記録し、180日超参照が無い場合に自動注入を1行ポインタへ
--   縮退させるdecay述語(is_decay_eligible)の入力に使う。
--
-- 変更内容:
--   - tags に last_injected_at（notes全文配信の最終実績日時、既定NULL）を追加

ALTER TABLE tags ADD COLUMN last_injected_at TIMESTAMP DEFAULT NULL;
