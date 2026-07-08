-- Migration 0058: habitsにtrigger_mode等を追加しintelligently層を導入
--
-- depends: 0057_drop_capability_gating
--
-- 背景:
--   SessionStart全件注入だったhabitsを、常時全文表示するalwaysと、
--   マニフェストのみ表示し詳細はon-demandで引くintelligentlyに分割する。
--
-- 変更内容（スキーマのみ、データ移行は含まない）:
--   - habits に description（要旨、既定空文字）を追加
--   - habits に trigger_mode（'always'/'intelligently'、既定'always'）を追加
--   - habits に importance_score（優先度スコア、既定1.0）を追加
--   - habits に last_recalled_at（参照スタンプ、既定NULL）を追加
--
-- データ移行をmigrationに含めない理由:
--   habits にはタグ・カテゴリ相当のカラムが存在せず、どのhabitをintelligently化
--   すべきかは自動化されたデータ条件に還元できない一次棚卸し（人手判断）でしか
--   決まらない。cc-memoryは各ユーザーが個別にローカルDBを持つ構成のため、
--   ある環境で棚卸しされたhabitのidをmigrationにリテラルで埋め込むと、
--   他環境では無関係な行を書き換えてしまう（idはinstall先ごとの採番history
--   に依存する）。habit本文をmigrationに埋め込む代替も、本文が長大な個人の
--   運用知見であり移行の妥当な手段ではないため採らない。
--   trigger_mode='intelligently'への切り替えは、棚卸し結果を踏まえて
--   update_habitツールで対象environmentごとに個別適用する。

ALTER TABLE habits ADD COLUMN description TEXT NOT NULL DEFAULT '';
ALTER TABLE habits ADD COLUMN trigger_mode TEXT NOT NULL DEFAULT 'always' CHECK(trigger_mode IN ('always', 'intelligently'));
ALTER TABLE habits ADD COLUMN importance_score REAL NOT NULL DEFAULT 1.0;
ALTER TABLE habits ADD COLUMN last_recalled_at TIMESTAMP NULL;
