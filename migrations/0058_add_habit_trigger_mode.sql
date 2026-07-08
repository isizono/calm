-- Migration 0058: habitsにtrigger_mode等を追加しintelligently層を導入
--
-- depends: 0057_drop_capability_gating
--
-- 背景:
--   SessionStart全件注入だったhabitsを、常時全文表示するalwaysと、
--   マニフェストのみ表示し詳細はon-demandで引くintelligentlyに分割する。
--   分割の一次分類は棚卸し済みで、本migrationはその結果を機械的に適用する。
--
-- 変更内容:
--   - habits に description（要旨、既定空文字）を追加
--   - habits に trigger_mode（'always'/'intelligently'、既定'always'）を追加
--   - habits に importance_score（優先度スコア、既定1.0）を追加
--   - habits に last_recalled_at（参照スタンプ、既定NULL）を追加
--   - 棚卸し済みの20件をtrigger_mode='intelligently'に一括更新
--
-- 棚卸し対象からの除外について:
--   棚卸しで承認されたのは25件だが、本migrationで更新するのは20件のみ。
--   残り5件（orch/dispatcher/worker体系の指揮フローに紐づくもの）は、
--   その指揮層自体が既に解体済みのため、intelligently化ではなく
--   retract（無効化）が妥当な可能性がある。always/intelligentlyの
--   二択に押し込めず、無効化の要否をユーザー判断待ちとして今回は据え置いた。

ALTER TABLE habits ADD COLUMN description TEXT NOT NULL DEFAULT '';
ALTER TABLE habits ADD COLUMN trigger_mode TEXT NOT NULL DEFAULT 'always' CHECK(trigger_mode IN ('always', 'intelligently'));
ALTER TABLE habits ADD COLUMN importance_score REAL NOT NULL DEFAULT 1.0;
ALTER TABLE habits ADD COLUMN last_recalled_at TIMESTAMP NULL;

UPDATE habits SET trigger_mode = 'intelligently'
WHERE id IN (12, 14, 15, 16, 17, 18, 20, 41, 26, 28, 30, 31, 37, 39, 34, 40, 58, 25, 36, 60);
