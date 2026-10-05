-- Migration 0089: sessions.ended_reasonに'stale_on_startup'を追加
--
-- depends: 0087_drop_leftover_fts5_check_tables
--
-- 背景:
--   サーバー起動時、前のサーバープロセスの時代からended_atが空のまま残っている
--   行(旧サーバーが終了する前にliveness reaperが処理しきれなかったもの)を
--   閉じる契機を追加する(session_ledger_service.close_stale_sessions)。
--   既存のended_reasonの命名(unregister/ttl/superseded)に合わせ、新しい値を
--   1つ追加する。CHECK制約の値一覧を変えるにはテーブル再作成が必要。

PRAGMA legacy_alter_table = ON;

ALTER TABLE sessions RENAME TO sessions_old_0089;

CREATE TABLE sessions (
  session_id TEXT PRIMARY KEY,
  id_kind TEXT NOT NULL CHECK (id_kind IN ('bridge','ephemeral')),
  harness TEXT, host TEXT, cwd TEXT,
  cli_session_id TEXT, cli_pid INTEGER,
  cli_resolve_status TEXT CHECK (cli_resolve_status IS NULL
    OR cli_resolve_status IN ('resolved','header_missing','not_found','stale')),
  mode TEXT NOT NULL DEFAULT 'interactive' CHECK (mode IN ('interactive','headless')),
  last_heartbeat_at TIMESTAMP,
  last_tool_call_at TIMESTAMP,
  last_checkin_activity_id INTEGER, last_checkin_at TIMESTAMP,
  ended_at TIMESTAMP, ended_reason TEXT CHECK (ended_reason IS NULL
    OR ended_reason IN ('unregister','ttl','superseded','stale_on_startup')),
  CHECK ((ended_at IS NULL) = (ended_reason IS NULL))
);

INSERT INTO sessions (
  session_id, id_kind, harness, host, cwd,
  cli_session_id, cli_pid, cli_resolve_status, mode,
  last_heartbeat_at, last_tool_call_at,
  last_checkin_activity_id, last_checkin_at,
  ended_at, ended_reason
)
SELECT
  session_id, id_kind, harness, host, cwd,
  cli_session_id, cli_pid, cli_resolve_status, mode,
  last_heartbeat_at, last_tool_call_at,
  last_checkin_activity_id, last_checkin_at,
  ended_at, ended_reason
FROM sessions_old_0089;

DROP TABLE sessions_old_0089;

CREATE INDEX idx_sessions_live ON sessions(last_heartbeat_at) WHERE ended_at IS NULL;
CREATE UNIQUE INDEX idx_sessions_cli_live ON sessions(harness, cli_session_id)
  WHERE cli_session_id IS NOT NULL AND ended_at IS NULL;
