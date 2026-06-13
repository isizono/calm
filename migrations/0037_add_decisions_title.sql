-- Migration 037: decisionsテーブルにtitleカラムを追加
--
-- depends: 0036_add_materials_updated_at
--
-- 背景:
--   decisionは現状titleを持たず、表示時はdecision本文をそのまま見出しに使っている。
--   要点を1行で表すtitleを持たせ、表示はtitle優先・decision本文fallbackにする。
--   既存行はNULLのままとし、COALESCE(title, decision)で現行挙動を維持する。
--
-- 変更内容:
--   - decisions.title を追加（NULL許容）。既存行はNULLのまま。
--   - search_index投入トリガー（insert/update）を貼り直し、表示用titleが
--     COALESCE(NEW.title, NEW.decision) になるようにする。
--     FTSインデックス（search_index_fts: マッチ用）はNEW.decision/NEW.reasonの
--     ままで変更しないため、全文検索の挙動は不変。
--     既存行のtitleはNULL → COALESCE で decision本文と一致するため、
--     search_indexの既存行に対するバックフィルは不要。

ALTER TABLE decisions ADD COLUMN title TEXT;

-- search_index投入トリガーを title優先のdisplay title に貼り直す
-- （FTSのtitle/bodyは従来通り decision本文/reason のまま）
DROP TRIGGER IF EXISTS trg_search_decisions_insert;
CREATE TRIGGER IF NOT EXISTS trg_search_decisions_insert
AFTER INSERT ON decisions
BEGIN
  INSERT INTO search_index (source_type, source_id, title, created_at)
  VALUES ('decision', NEW.id, COALESCE(NEW.title, NEW.decision), NEW.created_at);
  INSERT INTO search_index_fts (rowid, title, body)
  VALUES (last_insert_rowid(), NEW.decision, NEW.reason);
END;

DROP TRIGGER IF EXISTS trg_search_decisions_update;
CREATE TRIGGER IF NOT EXISTS trg_search_decisions_update
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
