-- Migration 0048: session_identity テーブル追加 + 全 entity に caller_session_id カラム追加
--
-- depends: 0047_drop_decisions_logs_topic_id
--
-- 背景:
--   複数の Claude Code セッション (orch / worker / standalone) が並行稼働する構成において、
--   各セッションの identity (role / handle / topic 紐付け) と alive 状態を一元管理するテーブルが必要。
--   また、各 entity (decisions / logs / topics / activities / materials) を作成・更新したセッションを
--   追跡できるよう caller_session_id カラムを追加する。
--
-- 設計:
--   - session_identity はセッション単位の identity レコード (1 セッション 1 行)。
--   - parent_session_id は relax FK (FOREIGN KEY 制約自体張らない)。
--     削除済み親への参照や、session_identity 外部での親セッションへの参照を許容する。
--   - handle は UNIQUE 制約を張らない。同名 handle の別セッション再利用を許容する。
--   - topic_id は既存 discussion_topics を参照。NULL 許容のため topic 削除時は ON DELETE SET NULL。
--   - caller_session_id は全 entity テーブルに NULL 許容で追加。
--     FK は張らず文字列として保持する (session 越え identity 連続性なし方針と整合)。
--     index は当面張らない (audit query 必要性が出たら別 migration で追加)。
--
-- 変更内容:
--   - session_identity テーブル新規作成
--   - idx_session_identity_role / idx_session_identity_handle / idx_session_identity_ended 追加
--   - decisions / discussion_logs / discussion_topics / activities / materials に
--     caller_session_id TEXT (NULL 許容) を追加

CREATE TABLE session_identity (
  session_id TEXT PRIMARY KEY,
  role TEXT NOT NULL,
  handle TEXT,
  topic_id INTEGER REFERENCES discussion_topics(id) ON DELETE SET NULL,
  parent_session_id TEXT,
  spawned_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_heartbeat TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  ended_at TIMESTAMP
);

CREATE INDEX idx_session_identity_role ON session_identity(role);
CREATE INDEX idx_session_identity_handle ON session_identity(handle) WHERE handle IS NOT NULL;
CREATE INDEX idx_session_identity_ended ON session_identity(ended_at) WHERE ended_at IS NULL;

ALTER TABLE decisions          ADD COLUMN caller_session_id TEXT;
ALTER TABLE discussion_logs    ADD COLUMN caller_session_id TEXT;
ALTER TABLE discussion_topics  ADD COLUMN caller_session_id TEXT;
ALTER TABLE activities         ADD COLUMN caller_session_id TEXT;
ALTER TABLE materials          ADD COLUMN caller_session_id TEXT;
