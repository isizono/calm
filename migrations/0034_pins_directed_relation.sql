-- Migration 034: pins有向関係拡張 — pinsテーブル新設 + 既存pinned=1 material移行
--
-- depends: 0033_relation_expansion
--
-- 背景:
--   従来のpin機構は material/decision/log の pinned カラムで管理していたが、
--   「source→target」の有向関係として表現できるよう pinsテーブルに移行する。
--   source（tag/activity等）から target（任意エンティティ）へのpinを格納する。
--
-- NOTE: pinned列のDROPは0035で行う（checkin_service改修と同一PRに同梱するため）

CREATE TABLE pins (
    source_type TEXT NOT NULL CHECK(source_type IN ('tag','activity','topic','decision','log','material')),
    source_id   INTEGER NOT NULL,
    target_type TEXT NOT NULL CHECK(target_type IN ('tag','activity','topic','decision','log','material')),
    target_id   INTEGER NOT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (source_type, source_id, target_type, target_id)
);
-- 注入クエリは source(type,id) で引くが、PKが (source_type, source_id, ...) 先頭一致のため追加indexは不要。
-- target逆引き（A#733のsupersedes引き継ぎ）が必要になった時点で idx_pins_target を追加する。

-- 既存pinned=1 material（M#145, M#152）を source='activity' で移行（D#2103）。
-- materialとactivityの紐付けは relations（0033で統合済み, source_type='activity' target_type='material'）から引く。
INSERT OR IGNORE INTO pins (source_type, source_id, target_type, target_id, created_at)
SELECT 'activity', r.source_id, 'material', m.id, m.created_at
FROM materials m
JOIN relations r
  ON r.source_type = 'activity' AND r.target_type = 'material' AND r.target_id = m.id
WHERE m.pinned = 1;
