-- Migration 0051: precedent_telemetry テーブル追加
--
-- depends: 0050_add_topic_vec
--
-- 背景:
--   判例（decision）を topic 単位で網羅列挙して提示する機能のための計測テーブル。
--   呼び出しごとに「何を問い、どの topic が routing で選ばれ、判例が何件
--   提示されたか」を記録し、routing の当たり外れやカバレッジを事後分析できる
--   ようにする。書込元のサービスは別 PR で実装する。
--
-- スキーマ:
--   id               : 主キー
--   context          : routing クエリ全文
--   parameters       : topic_ids / k / budget 等の呼び出しパラメータ（JSON）
--   guarantee        : routing・列挙が保証成立したかを表す状態文字列
--   routing_json     : routing 候補 + 採用 topic + 距離（JSON）
--   decisions_total  : 列挙対象になった判例の総数
--   full_count       : 本文まで展開された判例の件数
--   timestamp        : 書込時刻
--
--   search_telemetry（migration 0041）と同型。書込は別スレッドで非同期に行い、
--   失敗しても呼び出し元の処理には影響させない方針を踏襲する（実装側の責務）。

CREATE TABLE precedent_telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    context TEXT NOT NULL,
    parameters TEXT NOT NULL,
    guarantee TEXT NOT NULL,
    routing_json TEXT NOT NULL,
    decisions_total INTEGER NOT NULL,
    full_count INTEGER NOT NULL,
    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_precedent_telemetry_timestamp ON precedent_telemetry(timestamp);
