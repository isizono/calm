-- Migration 0046: relations.belongs_to 統一化 — 全エンティティの親帰属を relations 経由で表現
--
-- depends: 0045_add_activities_orch_managed
--
-- 背景:
--   decision/log の親 topic は `decisions.topic_id NOT NULL FK` / `discussion_logs.topic_id NOT NULL FK`
--   で直接保持する一方、material/activity の親 topic は `relations` テーブルに
--   `relation_type='related'` として書き込む非対称な状態だった。
--
--   全エンティティの親帰属を `relations.relation_type='belongs_to'` に統一することで、
--   親帰属の表現を 1 経路にまとめる。search / 依存解決 / recompose の経路も `belongs_to`
--   ベースで動かす。
--
-- 変更内容:
--   1. relations テーブル再作成: relation_type CHECK 制約を ('related','belongs_to') に緩和
--   2. relations の partial index 2 本を追加 (belongs_to クエリ hot path 用)
--   3. relations_view 再作成: relation_type を直接返す (旧版は 'related' リテラルだった)
--   4. 既存 material/activity → topic の 'related' 行を全件 'belongs_to' に変換
--   5. decisions/discussion_logs.topic_id を relations.belongs_to に複製
--   6. decisions / discussion_logs テーブル再作成:
--      - topic_id を NULLABLE 化 (FK 制約も削除)
--      - 親帰属は relations.belongs_to を正としつつ、当面は topic_id 列も残置 (0047 で物理削除)
--      - 関連トリガー (search_index 3 本 + CASCADE 3 本) を再作成
--
-- 設計メモ:
--   - relations の PK は (source_type, source_id, target_type, target_id) のまま据置。
--     relation_type は PK に含めない (同一ペアで related/belongs_to が同居しない)
--   - 既存 'related' 行は UPDATE で 'belongs_to' に書き換える (新規 INSERT で重複しない)
--   - dual-write は行わない: 書き込みパスは migration 適用と同時にコード側で belongs_to 一本に切替
--   - subject_id 同期トリガーは migration 0010 で既に撤去済みのため変更不要

-- legacy_alter_table=ON: ALTER TABLE RENAME 時に既存トリガー本体内のテーブル参照を
-- 自動更新しない (デフォルト挙動だと relations を rename した瞬間 CASCADE トリガー
-- 5 本の本体が renamed 名を指すように書き換えられ、後段の DROP TABLE で参照崩壊する)
PRAGMA legacy_alter_table = ON;

-- テーブル再作成中の FK 違反を commit 時まで遅延 (decisions / discussion_logs を
-- 参照する decision_supersedes 等の FK 整合性を中間状態で守る)
PRAGMA defer_foreign_keys = ON;

-- ============================================
-- Step 1: relations テーブル再作成 (CHECK 緩和) + partial index 2 本
-- ============================================

ALTER TABLE relations RENAME TO relations_old_0046;

CREATE TABLE relations (
    source_type TEXT NOT NULL CHECK(source_type IN ('topic', 'activity', 'material', 'decision', 'log')),
    source_id INTEGER NOT NULL,
    target_type TEXT NOT NULL CHECK(target_type IN ('topic', 'activity', 'material', 'decision', 'log')),
    target_id INTEGER NOT NULL,
    -- 'related' は対称関係、'belongs_to' は子→親 (decision/log/material/activity → topic) を表す
    relation_type TEXT NOT NULL DEFAULT 'related' CHECK(relation_type IN ('related', 'belongs_to')),
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (source_type, source_id, target_type, target_id),
    CHECK (source_type < target_type OR (source_type = target_type AND source_id < target_id))
);

INSERT INTO relations (source_type, source_id, target_type, target_id, relation_type, created_at)
SELECT source_type, source_id, target_type, target_id, relation_type, created_at FROM relations_old_0046;

DROP TABLE relations_old_0046;

-- 既存の target 逆引き index を再作成
CREATE INDEX idx_relations_target ON relations(target_type, target_id);

-- partial index: belongs_to クエリの hot path 用
-- 子 → 親方向 (例: get_decisions(topic) で decision の親 topic 経由集約)
CREATE INDEX idx_relations_belongs_to_tgt
  ON relations(target_type, target_id, source_type, source_id)
  WHERE relation_type = 'belongs_to';

-- 親 → 子方向 (例: timeline で topic 配下の decision/log 集約)
CREATE INDEX idx_relations_belongs_to_src
  ON relations(source_type, source_id, target_id)
  WHERE relation_type = 'belongs_to';

-- ============================================
-- Step 2: relations_view 再作成 (relation_type を直接返す)
-- ============================================

DROP VIEW IF EXISTS relations_view;

CREATE VIEW relations_view AS
  -- relations 正方向 (related + belongs_to)
  SELECT source_type, source_id, target_type, target_id, relation_type, created_at
  FROM relations
  UNION ALL
  -- relations 逆方向 (related + belongs_to の対称展開)
  SELECT target_type, target_id, source_type, source_id, relation_type, created_at
  FROM relations
  UNION ALL
  -- depends_on (activity_dependencies)
  SELECT 'activity' AS source_type, dependent_id AS source_id,
         'activity' AS target_type, dependency_id AS target_id,
         'depends_on' AS relation_type, created_at
  FROM activity_dependencies
  UNION ALL
  -- supersedes (decision_supersedes)
  SELECT 'decision' AS source_type, source_id,
         'decision' AS target_type, target_id,
         'supersedes' AS relation_type, created_at
  FROM decision_supersedes;

-- ============================================
-- Step 3: 既存 'related' 行を 'belongs_to' に変換
-- 正規化制約により source_type < target_type なので必ず source=子, target=topic の形。
--
-- - material/activity → topic: 全件変換 (これらは元々 relations 一本管理、すべて親帰属)
-- - decision/log → topic:      FK (decisions.topic_id / discussion_logs.topic_id) と
--                              一致する行のみ変換。FK と一致しない副たる関連 ('related')
--                              は据置 ── 主たる親 1 個 + 副は related の運用ルールを保つ。
--                              (これをやらないと Step 4 の INSERT OR IGNORE が PK 衝突
--                               をスキップして主たる親が belongs_to で表現されない不整合になる)
-- ============================================

UPDATE relations
SET relation_type = 'belongs_to'
WHERE relation_type = 'related'
  AND target_type = 'topic'
  AND source_type IN ('activity', 'material');

UPDATE relations
SET relation_type = 'belongs_to'
WHERE relation_type = 'related'
  AND target_type = 'topic'
  AND source_type = 'decision'
  AND EXISTS (
    SELECT 1 FROM decisions d
    WHERE d.id = relations.source_id AND d.topic_id = relations.target_id
  );

UPDATE relations
SET relation_type = 'belongs_to'
WHERE relation_type = 'related'
  AND target_type = 'topic'
  AND source_type = 'log'
  AND EXISTS (
    SELECT 1 FROM discussion_logs l
    WHERE l.id = relations.source_id AND l.topic_id = relations.target_id
  );

-- ============================================
-- Step 4: decisions/discussion_logs.topic_id を relations.belongs_to に複製
-- 'decision' < 'topic'、'log' < 'topic' なので正規化形 (source=子) のまま挿入
-- ============================================

INSERT OR IGNORE INTO relations (source_type, source_id, target_type, target_id, relation_type)
SELECT 'decision', d.id, 'topic', d.topic_id, 'belongs_to'
FROM decisions d
WHERE d.topic_id IS NOT NULL;

INSERT OR IGNORE INTO relations (source_type, source_id, target_type, target_id, relation_type)
SELECT 'log', l.id, 'topic', l.topic_id, 'belongs_to'
FROM discussion_logs l
WHERE l.topic_id IS NOT NULL;

-- ============================================
-- Step 5: decisions テーブル再作成 (topic_id NULLABLE 化 + FK 削除)
-- 旧テーブルに紐づく全トリガー (search_index 3 + CASCADE 3) は再作成必要
-- ============================================

-- 旧トリガー DROP (テーブル DROP で自動 DROP されるが明示)
DROP TRIGGER IF EXISTS trg_search_decisions_insert;
DROP TRIGGER IF EXISTS trg_search_decisions_update;
DROP TRIGGER IF EXISTS trg_search_decisions_delete;
DROP TRIGGER IF EXISTS trg_relations_cascade_delete_decision;
DROP TRIGGER IF EXISTS trg_pins_cascade_delete_decision;
DROP TRIGGER IF EXISTS trg_citations_cascade_delete_decision;

DROP INDEX IF EXISTS idx_decisions_topic_id;

ALTER TABLE decisions RENAME TO decisions_old_0046;

CREATE TABLE decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  -- topic_id は 0047 で物理削除予定。当面は NULL 許容で残置 (旧 INSERT パスの後方互換ではなく、
  -- migration 適用直後の中間状態で SELECT 互換性を維持するための残置)
  topic_id INTEGER,
  decision TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  retracted_at TIMESTAMP NULL,
  title TEXT
);

INSERT INTO decisions (id, topic_id, decision, reason, created_at, retracted_at, title)
SELECT id, topic_id, decision, reason, created_at, retracted_at, title FROM decisions_old_0046;

DROP TABLE decisions_old_0046;

-- search_index 同期トリガー 3 本 (topic_id 不参照、migration 0037 と同形)
CREATE TRIGGER trg_search_decisions_insert
AFTER INSERT ON decisions
BEGIN
  INSERT INTO search_index (source_type, source_id, title, created_at)
  VALUES ('decision', NEW.id, COALESCE(NEW.title, NEW.decision), NEW.created_at);
  INSERT INTO search_index_fts (rowid, title, body)
  VALUES (last_insert_rowid(), NEW.decision, NEW.reason);
END;

CREATE TRIGGER trg_search_decisions_update
AFTER UPDATE ON decisions
BEGIN
  INSERT INTO search_index_fts (search_index_fts, rowid, title, body)
  VALUES ('delete',
    (SELECT id FROM search_index WHERE source_type = 'decision' AND source_id = OLD.id),
    OLD.decision, OLD.reason);
  UPDATE search_index
  SET title = COALESCE(NEW.title, NEW.decision)
  WHERE source_type = 'decision' AND source_id = NEW.id;
  INSERT INTO search_index_fts (rowid, title, body)
  VALUES (
    (SELECT id FROM search_index WHERE source_type = 'decision' AND source_id = NEW.id),
    NEW.decision, NEW.reason);
END;

CREATE TRIGGER trg_search_decisions_delete
AFTER DELETE ON decisions
BEGIN
  INSERT INTO search_index_fts (search_index_fts, rowid, title, body)
  VALUES ('delete',
    (SELECT id FROM search_index WHERE source_type = 'decision' AND source_id = OLD.id),
    OLD.decision, OLD.reason);
  DELETE FROM search_index WHERE source_type = 'decision' AND source_id = OLD.id;
END;

-- CASCADE トリガー 3 本 (relations / pins / citations)
CREATE TRIGGER trg_relations_cascade_delete_decision
AFTER DELETE ON decisions
FOR EACH ROW
BEGIN
    DELETE FROM relations WHERE (source_type = 'decision' AND source_id = OLD.id)
                             OR (target_type = 'decision' AND target_id = OLD.id);
END;

CREATE TRIGGER trg_pins_cascade_delete_decision
AFTER DELETE ON decisions
FOR EACH ROW
BEGIN
    DELETE FROM pins WHERE (source_type = 'decision' AND source_id = OLD.id)
                       OR (target_type = 'decision' AND target_id = OLD.id);
END;

CREATE TRIGGER trg_citations_cascade_delete_decision
AFTER DELETE ON decisions
FOR EACH ROW
BEGIN
    DELETE FROM citations WHERE owner_type = 'decision' AND owner_id = OLD.id;
END;

-- ============================================
-- Step 6: discussion_logs テーブル再作成 (同様の手順)
-- ============================================

DROP TRIGGER IF EXISTS trg_search_logs_insert;
DROP TRIGGER IF EXISTS trg_search_logs_update;
DROP TRIGGER IF EXISTS trg_search_logs_delete;
DROP TRIGGER IF EXISTS trg_relations_cascade_delete_log;
DROP TRIGGER IF EXISTS trg_pins_cascade_delete_log;
DROP TRIGGER IF EXISTS trg_citations_cascade_delete_log;

DROP INDEX IF EXISTS idx_logs_topic_id;

ALTER TABLE discussion_logs RENAME TO discussion_logs_old_0046;

CREATE TABLE discussion_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  topic_id INTEGER,
  content TEXT NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  title TEXT NOT NULL DEFAULT '',
  retracted_at TIMESTAMP NULL
);

INSERT INTO discussion_logs (id, topic_id, content, created_at, title, retracted_at)
SELECT id, topic_id, content, created_at, title, retracted_at FROM discussion_logs_old_0046;

DROP TABLE discussion_logs_old_0046;

CREATE TRIGGER trg_search_logs_insert
AFTER INSERT ON discussion_logs
BEGIN
  INSERT INTO search_index (source_type, source_id, title, created_at)
  VALUES ('log', NEW.id, NEW.title, NEW.created_at);
  INSERT INTO search_index_fts (rowid, title, body)
  VALUES (last_insert_rowid(), NEW.title, NEW.content);
END;

CREATE TRIGGER trg_search_logs_update
AFTER UPDATE ON discussion_logs
BEGIN
  INSERT INTO search_index_fts (search_index_fts, rowid, title, body)
  VALUES ('delete',
    (SELECT id FROM search_index WHERE source_type = 'log' AND source_id = OLD.id),
    OLD.title, OLD.content);
  UPDATE search_index
  SET title = NEW.title
  WHERE source_type = 'log' AND source_id = NEW.id;
  INSERT INTO search_index_fts (rowid, title, body)
  VALUES (
    (SELECT id FROM search_index WHERE source_type = 'log' AND source_id = NEW.id),
    NEW.title, NEW.content);
END;

CREATE TRIGGER trg_search_logs_delete
AFTER DELETE ON discussion_logs
BEGIN
  INSERT INTO search_index_fts (search_index_fts, rowid, title, body)
  VALUES ('delete',
    (SELECT id FROM search_index WHERE source_type = 'log' AND source_id = OLD.id),
    OLD.title, OLD.content);
  DELETE FROM search_index WHERE source_type = 'log' AND source_id = OLD.id;
END;

CREATE TRIGGER trg_relations_cascade_delete_log
AFTER DELETE ON discussion_logs
FOR EACH ROW
BEGIN
    DELETE FROM relations WHERE (source_type = 'log' AND source_id = OLD.id)
                             OR (target_type = 'log' AND target_id = OLD.id);
END;

CREATE TRIGGER trg_pins_cascade_delete_log
AFTER DELETE ON discussion_logs
FOR EACH ROW
BEGIN
    DELETE FROM pins WHERE (source_type = 'log' AND source_id = OLD.id)
                       OR (target_type = 'log' AND target_id = OLD.id);
END;

CREATE TRIGGER trg_citations_cascade_delete_log
AFTER DELETE ON discussion_logs
FOR EACH ROW
BEGIN
    DELETE FROM citations WHERE owner_type = 'log' AND owner_id = OLD.id;
END;
