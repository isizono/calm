-- Migration 0059: habitsにstatusを追加
--
-- depends: 0058_add_habit_trigger_mode
--
-- 背景:
--   habits には有効/無効を表す active カラムが既にあるが、intelligently層の
--   マニフェストが対象を絞り込む軸としては別の意味の状態（棚卸し済みで
--   もう表示しないと判断したアーカイブ状態）が必要になった。active を
--   アーカイブに転用すると「一時的な無効化」と「恒久的な棚卸し済み」が
--   区別できなくなるため、独立したカラムとして追加する。
--
-- 変更内容:
--   - habits に status（'active'/'archived'、既定'active'）を追加
--   - active とは独立した軸であり、両方の条件はAND併用される
--     （マニフェスト取得は active = 1 AND status = 'active' で絞り込む）

ALTER TABLE habits ADD COLUMN status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'archived'));
