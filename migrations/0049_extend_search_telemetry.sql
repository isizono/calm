-- Migration 0049: search_telemetry に results_json / diagnostics_json を追加 + fetch_telemetry 新設
--
-- depends: 0048_session_identity
--
-- 背景:
--   search_telemetry (0041) は query / parameters / result_count のみを記録し、
--   「どの id を何位で返したか」「retriever 別のヒット内訳」を保持していないため、
--   検索結果が実際に後続で使われたか（pull hit 率等）を突合できない。
--   本 migration はその生データ供給のため、search_telemetry に返却ページと
--   retriever 内訳の JSON カラムを追加し、取得側の追跡テーブル fetch_telemetry を
--   新設する。集計・可視化は別コンポーネントの管轄であり、ここでは生データの
--   スキーマと書込先のみを用意する。
--
-- スキーマ:
--   search_telemetry.results_json     : 返却ページの [{"type":..,"id":..,"final_score":..}, ...]
--   search_telemetry.diagnostics_json : retriever 内訳（fts_hits/vec_hits/tag_hits/
--                                        methods_used/candidate_set_size/qe_expansions/
--                                        adaptive_weights 等）
--   fetch_telemetry.tool        : 計装元ツール名（例: 'get_by_ids'）
--   fetch_telemetry.items_json  : [{"type": "decision", "id": 3195}, ...]
--   fetch_telemetry.timestamp   : 書込時刻 (UTC)
--
--   search_telemetry と同じく、書込は daemon thread の非同期・失敗握りつぶし規約に
--   載せる。既存行の results_json / diagnostics_json は NULL のまま残る（後方互換）。

ALTER TABLE search_telemetry ADD COLUMN results_json TEXT;
ALTER TABLE search_telemetry ADD COLUMN diagnostics_json TEXT;

CREATE TABLE fetch_telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool TEXT NOT NULL,
    items_json TEXT NOT NULL,
    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_fetch_telemetry_timestamp ON fetch_telemetry(timestamp);
