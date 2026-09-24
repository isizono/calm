-- Migration 0078: セッション台帳(sessions)テーブルを追加
--
-- depends: 0077_add_goals
--
-- 背景:
--   起動器(launcher)ごとのセッション状態が、アクティビティの打刻・セッション
--   別名ファイル・launcherのin-memoryセッション管理という複数の置き場に
--   分散していた。単一の真実源となるテーブルを新設する。
--
-- スキーマ:
--   sessions  起動器プロセスごとに1行。主キーは起動器の識別子(session_id)。
--             会話識別子(cli_session_id)は解決関数が充填する列で、解決できない
--             場合はNULLのまま行を作る。行は削除しない。unregister/TTL失効/
--             世代交代のいずれもended_at/ended_reasonを立てるだけで残す。
--
-- 制約:
--   - id_kindは起動器の識別子が取れたか('bridge')/取れず揮発識別子で
--     代替したか('ephemeral')の2値
--   - ended_atとended_reasonは常に両方NULLか両方非NULLのいずれかになる
--     (CHECK制約で強制)
--   - cli_session_idの一意性は「NULLでなく、かつ終了していない」行に限定した
--     部分一意索引で担保する(会話識別子NULLの行は複数存在してよい)。索引は
--     (harness, cli_session_id)の複合にする。ハーネスごとに会話識別子の番号
--     体系が異なるため、一意性は同じharnessの中でのみ担保する。harnessが
--     NULLの行同士は(SQLiteのUNIQUE制約がNULLを区別しない挙動により)既存の
--     cli_session_id NULL行と同じく複数存在してよい
--   - cli_resolve_statusの'not_found'はハーネス中立な名称。特定ハーネスの
--     実装（CLIセッションファイル等）を名指ししない

CREATE TABLE sessions (
  session_id TEXT PRIMARY KEY,             -- 起動器の識別子。取れなければ 'eph:'||接続単位の揮発識別子
  id_kind TEXT NOT NULL CHECK (id_kind IN ('bridge','ephemeral')),
  harness TEXT, host TEXT, cwd TEXT,       -- 起動器の申告。hostは到達判定、cwdは診断
  cli_session_id TEXT, cli_pid INTEGER,    -- 会話識別子。解決関数が充填する
  cli_resolve_status TEXT CHECK (cli_resolve_status IS NULL
    OR cli_resolve_status IN ('resolved','header_missing','not_found','stale')),
  mode TEXT NOT NULL DEFAULT 'interactive' CHECK (mode IN ('interactive','headless')),
  last_heartbeat_at TIMESTAMP,             -- 起動器の心拍(60秒)
  last_tool_call_at TIMESTAMP,             -- 全ツール呼び出しの touch(60秒スロットル)
  last_checkin_activity_id INTEGER, last_checkin_at TIMESTAMP,
  ended_at TIMESTAMP, ended_reason TEXT CHECK (ended_reason IS NULL OR ended_reason IN ('unregister','ttl','superseded')),
  CHECK ((ended_at IS NULL) = (ended_reason IS NULL))
);
CREATE INDEX idx_sessions_live ON sessions(last_heartbeat_at) WHERE ended_at IS NULL;
CREATE UNIQUE INDEX idx_sessions_cli_live ON sessions(harness, cli_session_id)
  WHERE cli_session_id IS NOT NULL AND ended_at IS NULL;
