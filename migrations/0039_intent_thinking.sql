-- Migration 0039: intent:thinking タグ新設
--
-- depends: 0038_pins_target_index_and_cascade
--
-- 思考worker（深い議論・設計検討・調査を行うworker）用のintentタグ。
-- role=worker は変えず、task_file 本文に `ultratink` マーカーを埋め込んだ
-- ものを「思考worker」と扱う運用と連動する。

-- Step 1: intent:thinking タグ新設
INSERT OR IGNORE INTO tags (namespace, name) VALUES ('intent', 'thinking');

-- Step 2: description設定
UPDATE tags SET description = '深い議論・設計検討・調査を行う。コード実装には踏み込まない'
WHERE namespace = 'intent' AND name = 'thinking';

-- Step 3: notes設定
UPDATE tags SET notes = '境界: 実装・コード変更しない。議論・設計・調査と、その結果のmaterial化まで。

目的: 答えがすぐ出ない問いに対し、深い検討と整理を行う思考workerのintent。
完了条件: 検討内容がmaterial・decisionとして残されており、次の作業（実装/別議論）が始められる状態になっていること。

振る舞い:
- 実装には踏み込まず、議論・設計・調査・整理に専念する
- 結論を急がず、論点を洗い出し、選択肢を比較してからまとめる
- 検討の経緯はlog、結論はdecision、まとまった成果物はmaterialに残す
- 関連: 思考workerは task_file 本文に `ultratink`（意図的タイポ）マーカーが入っている
- 関連 intent: investigate（情報収集寄り）、design（仕様確定寄り）、discuss（要件確定寄り）'
WHERE namespace = 'intent' AND name = 'thinking';
