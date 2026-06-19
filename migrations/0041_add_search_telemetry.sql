-- Migration 041: search_telemetry テーブル追加
--
-- depends: 0039_intent_thinking
--
-- 背景:
--   search() 呼出ごとに query / parameters / 結果件数のスナップショットを記録し、
--   検索精度の Phase 1 検証 (recency / RRF 重み / query expansion 等の挙動分析)
--   の基盤データを蓄積する。
--
--   search() のレスポンスタイムに影響しないよう、書込は別スレッドで非同期に行う。
--   書込失敗時は logger.warning に出すだけで search 本体には影響させない。
--
-- スキーマ:
--   id          : 主キー
--   query       : 検索キーワード (str または list[str] を JSON serialize)
--   parameters  : tags / entity_type / limit / offset / keyword_mode / domain /
--                 date_after / date_before / include_retracted / include_details
--                 の snapshot を JSON で保存
--   result_count: 返却件数 (total_count)
--   timestamp   : 書込時刻 (UTC)
--
--   timestamp に index を張る (時系列での集計が主な読み出しパターン)。

CREATE TABLE search_telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    parameters TEXT NOT NULL,
    result_count INTEGER NOT NULL,
    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_search_telemetry_timestamp ON search_telemetry(timestamp);
