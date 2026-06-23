-- Migration 0046: sanitize_log を citation_event_log に作り直す
--
-- depends: 0045_add_activities_orch_managed
--
-- 背景:
--   既存 sanitize_log (0044) は hook 単位の集計カウンタ型 (occurrence_count /
--   sanitized_count / failed_count) で、INSERT 経路は未実装のまま据え置き。
--   この migration では集計カウンタ型を逐次行型に作り直す。
--
--   集計型 → 逐次行型の動機:
--     - 1 イベント = 1 行で source / target / before_text / after_text / verification_result
--       を保存し、ラバースタンプ機構として横断利用できる
--     - 5 つの source (write 経路の自動変換 / bulk migration / transcript hook 2 種 /
--       外部ドキュメント sanitize) を同一テーブルに集約し、view で系統別の窓を提供する
--     - target エンティティ単位で「いつ何回 sanitize されたか」が直接引ける
--
--   forward-only 方針: 0044 INSERT 経路未実装のためデータ移行不要。sanitize_log を
--   DROP した上で citation_event_log を CREATE する。down rollback は書かない。
--
-- スキーマ:
--   id                    INTEGER PK AUTOINCREMENT
--   occurred_at           TEXT NOT NULL DEFAULT (datetime('now')) -- イベント発生時刻 (UTC)
--   source                TEXT NOT NULL CHECK (...)               -- イベントの発生経路
--   tool_name             TEXT                                    -- write_auto_convert 時の MCP tool 名等
--   target_entity_type    TEXT CHECK (...)                        -- 'decision'|'activity'|'log'|'material'|'topic' OR NULL
--   target_entity_id      INTEGER
--   target_field          TEXT                                    -- field 名 (content / title 等)
--   before_text           TEXT NOT NULL                           -- 変換前テキスト (raw)
--   after_text            TEXT NOT NULL                           -- 変換後テキスト
--   verified_at           TEXT                                    -- target 存在チェック時刻 (UTC)
--   verification_result   TEXT CHECK (...)                        -- 'exists'|'dangling'|'skip' OR NULL
--   extra_json            TEXT                                    -- 追加メタ情報 (JSON 文字列)
--
-- source ENUM 値:
--   'write_auto_convert'                : MCP write tool 経由の自動変換 (add_material 等)
--   'bulk_migration'                    : 過去資産の一括変換
--   'transcript_post_tool_use'          : PostToolUse hook での sanitize
--   'transcript_session_start_backfill' : SessionStart backfill での sanitize
--   'external_doc_sanitize'             : 外部ドキュメント (issue 本文 等) の sanitize
--
-- インデックス:
--   - (target_entity_type, target_entity_id) : target 単位の取り回し / by_entity view 集約
--   - (source)                                : view 構築・source 別集計
--   - (occurred_at)                           : 時系列クエリ
--
-- view 3 本:
--   - sanitize_event_log         : transcript_post_tool_use / transcript_session_start_backfill /
--                                  external_doc_sanitize 由来 (純粋なサニタイズ系)
--   - auto_convert_event_log     : write_auto_convert / bulk_migration 由来 (自動変換系)
--   - citation_event_log_by_entity : target 単位の集約 (event_count, last_occurred_at)

DROP INDEX IF EXISTS idx_sanitize_log_session;
DROP INDEX IF EXISTS idx_sanitize_log_recorded_at;
DROP TABLE IF EXISTS sanitize_log;

CREATE TABLE citation_event_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL DEFAULT (datetime('now')),
    source TEXT NOT NULL CHECK (source IN (
        'write_auto_convert',
        'bulk_migration',
        'transcript_post_tool_use',
        'transcript_session_start_backfill',
        'external_doc_sanitize'
    )),
    tool_name TEXT,
    target_entity_type TEXT CHECK (target_entity_type IS NULL OR target_entity_type IN (
        'decision', 'activity', 'log', 'material', 'topic'
    )),
    target_entity_id INTEGER,
    target_field TEXT,
    before_text TEXT NOT NULL,
    after_text TEXT NOT NULL,
    verified_at TEXT,
    verification_result TEXT CHECK (verification_result IS NULL OR verification_result IN (
        'exists', 'dangling', 'skip'
    )),
    extra_json TEXT
);

CREATE INDEX idx_citation_event_log_target
    ON citation_event_log(target_entity_type, target_entity_id);
CREATE INDEX idx_citation_event_log_source
    ON citation_event_log(source);
CREATE INDEX idx_citation_event_log_occurred_at
    ON citation_event_log(occurred_at);

CREATE VIEW sanitize_event_log AS
SELECT *
FROM citation_event_log
WHERE source IN (
    'transcript_post_tool_use',
    'transcript_session_start_backfill',
    'external_doc_sanitize'
);

CREATE VIEW auto_convert_event_log AS
SELECT *
FROM citation_event_log
WHERE source IN ('write_auto_convert', 'bulk_migration');

CREATE VIEW citation_event_log_by_entity AS
SELECT
    target_entity_type,
    target_entity_id,
    COUNT(*) AS event_count,
    MAX(occurred_at) AS last_occurred_at
FROM citation_event_log
WHERE target_entity_type IS NOT NULL
  AND target_entity_id IS NOT NULL
GROUP BY target_entity_type, target_entity_id;
