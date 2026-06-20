-- Migration 0042: citations テーブル追加
--
-- depends: 0040_add_heartbeat_session_id 0041_add_search_telemetry
--
-- 背景:
--   本文中の `{{cite:X#NNN}}` テンプレ参照を構造化テーブルに保存する。
--   X は M/D/L/A/T の 5 種で、それぞれ material/decision/log/activity/topic に対応する。
--   読み出し時に flavor 引数で展開形式 (raw/internal/readable) を切り替える前提の参照基盤。
--
-- スキーマ:
--   id          : 主キー
--   owner_type  : 参照を含む本文を持つエンティティの種別
--   owner_id    : 同上 ID
--   target_type : 参照先エンティティの種別
--   target_id   : 同上 ID
--   occurrence  : owner 本文中の出現順 (1 始まり連番、文字オフセットは保持しない)
--   created_at  : INSERT 時刻
--
-- ライフサイクル:
--   - owner 削除: 該当 owner の citations 行はトリガーで cascade 削除
--   - target retract / 物理削除: citations 行は残置 (監査トレース要件)
--     展開時に dangling 判定を動的に行い `[deleted X#NNN]` / `[retracted X#NNN]` 表現にする

CREATE TABLE citations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_type TEXT NOT NULL CHECK(owner_type IN ('material', 'decision', 'log', 'activity', 'topic')),
    owner_id INTEGER NOT NULL,
    target_type TEXT NOT NULL CHECK(target_type IN ('material', 'decision', 'log', 'activity', 'topic')),
    target_id INTEGER NOT NULL,
    occurrence INTEGER NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(owner_type, owner_id, occurrence)
);

CREATE INDEX idx_citations_target ON citations(target_type, target_id);
CREATE INDEX idx_citations_owner ON citations(owner_type, owner_id);

-- owner 側 cascade delete トリガー (5 種)
CREATE TRIGGER trg_citations_cascade_delete_material
AFTER DELETE ON materials
FOR EACH ROW
BEGIN
    DELETE FROM citations WHERE owner_type = 'material' AND owner_id = OLD.id;
END;

CREATE TRIGGER trg_citations_cascade_delete_decision
AFTER DELETE ON decisions
FOR EACH ROW
BEGIN
    DELETE FROM citations WHERE owner_type = 'decision' AND owner_id = OLD.id;
END;

CREATE TRIGGER trg_citations_cascade_delete_log
AFTER DELETE ON discussion_logs
FOR EACH ROW
BEGIN
    DELETE FROM citations WHERE owner_type = 'log' AND owner_id = OLD.id;
END;

CREATE TRIGGER trg_citations_cascade_delete_activity
AFTER DELETE ON activities
FOR EACH ROW
BEGIN
    DELETE FROM citations WHERE owner_type = 'activity' AND owner_id = OLD.id;
END;

CREATE TRIGGER trg_citations_cascade_delete_topic
AFTER DELETE ON discussion_topics
FOR EACH ROW
BEGIN
    DELETE FROM citations WHERE owner_type = 'topic' AND owner_id = OLD.id;
END;
