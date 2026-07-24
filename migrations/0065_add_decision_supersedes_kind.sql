-- Migration 0065: decision_supersedesにkind列追加 + destabilizes解消追跡テーブル新設
--
-- depends: 0062_add_asks
--
-- 背景:
--   軸変更decisionが複数decisionの前提を揺るがす関係（destabilizes）を、既存の
--   decision_supersedes（結論の置き換え = replaces）と同じテーブルに kind 列で
--   区別して表現する。新エンティティは追加しない。

PRAGMA legacy_alter_table = ON;
PRAGMA defer_foreign_keys = ON;

-- ============================================
-- Step 1: decision_supersedes 再作成（kind列追加）
-- ============================================

ALTER TABLE decision_supersedes RENAME TO decision_supersedes_old_0065;

CREATE TABLE decision_supersedes (
    source_id INTEGER NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
    target_id INTEGER NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'replaces'
        CHECK (kind IN ('replaces', 'destabilizes')),
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (source_id, target_id, kind),
    CHECK (source_id != target_id)
);

INSERT INTO decision_supersedes (source_id, target_id, kind, created_at)
SELECT source_id, target_id, 'replaces', created_at FROM decision_supersedes_old_0065;

DROP TABLE decision_supersedes_old_0065;

CREATE INDEX idx_decision_supersedes_target ON decision_supersedes(target_id, kind);

-- ============================================
-- Step 2: 解消追跡テーブル新設
-- ============================================

CREATE TABLE decision_destabilization_resolutions (
    source_id INTEGER NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
    target_id INTEGER NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
    resolution TEXT NOT NULL
        CHECK (resolution IN ('reaffirmed', 'revised', 'retracted')),
    revised_to_decision_id INTEGER NULL REFERENCES decisions(id) ON DELETE SET NULL,
    note TEXT NOT NULL DEFAULT '',
    resolved_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (source_id, target_id)
);

CREATE INDEX idx_destab_resolutions_target ON decision_destabilization_resolutions(target_id);

-- ============================================
-- Step 3: relations_view 再作成（decision_supersedes由来の行のみ kind で出し分け）
-- ============================================

DROP VIEW relations_view;

CREATE VIEW relations_view AS
  SELECT source_type, source_id, target_type, target_id, relation_type, created_at
  FROM relations
  UNION ALL
  SELECT target_type, target_id, source_type, source_id, relation_type, created_at
  FROM relations
  UNION ALL
  SELECT 'activity' AS source_type, dependent_id AS source_id,
         'activity' AS target_type, dependency_id AS target_id,
         'depends_on' AS relation_type, created_at
  FROM activity_dependencies
  UNION ALL
  SELECT 'decision' AS source_type, source_id,
         'decision' AS target_type, target_id,
         CASE kind
              WHEN 'replaces' THEN 'supersedes'
              WHEN 'destabilizes' THEN 'destabilizes'
         END AS relation_type,
         created_at
  FROM decision_supersedes;
