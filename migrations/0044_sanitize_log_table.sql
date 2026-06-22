-- Migration 0044: sanitize_log テーブル追加
--
-- depends: 0043_add_materials_retracted_at
--
-- 背景:
--   transcript sanitize hook (PostToolUse + SessionStart backfill) の実行結果を記録する。
--   各 hook 実行で 1 行 INSERT、漏れ件数・成功件数・失敗件数・失敗理由を蓄積。
--   post-hoc 集計で漏れ検出ループを作る (sanitize_log の集計が日常運用監視)。
--
-- スキーマ:
--   id               : 主キー
--   session_id       : Claude Code セッション ID (運用時の追跡用、NULL 許容)
--   transcript_path  : transcript ファイルパス (NULL 許容、SessionStart backfill 時に有用)
--   hook_kind        : hook 種別 ('post_tool_use' / 'session_start_backfill')
--   occurrence_count : sanitize 対象として検出した件数
--   sanitized_count  : 実際に置換に成功した件数
--   failed_count     : 置換失敗件数
--   failure_reason   : 失敗時の理由 (NULL 許容)
--   recorded_at      : INSERT 時刻 (UTC、NOT NULL)
--
-- 整合性 CHECK:
--   - sanitized_count + failed_count <= occurrence_count
--     (置換に成功・失敗した件数が検出件数を超えないこと)
--   - session_id IS NOT NULL OR transcript_path IS NOT NULL
--     (両方 NULL の行は追跡不能になるため許可しない)
--
-- ライフサイクル:
--   transient テーブル。運用安定 (failed_count=0 が一定期間継続) で DROP migration を
--   別途発行する想定。本 PR ではテーブル定義のみ追加し、INSERT 経路は別 PR で実装する。

CREATE TABLE sanitize_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    transcript_path TEXT,
    hook_kind TEXT NOT NULL CHECK(hook_kind IN ('post_tool_use', 'session_start_backfill')),
    occurrence_count INTEGER NOT NULL DEFAULT 0,
    sanitized_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    failure_reason TEXT,
    recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK(sanitized_count + failed_count <= occurrence_count),
    CHECK(session_id IS NOT NULL OR transcript_path IS NOT NULL)
);

CREATE INDEX idx_sanitize_log_session ON sanitize_log(session_id);
CREATE INDEX idx_sanitize_log_recorded_at ON sanitize_log(recorded_at);
