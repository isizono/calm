-- Migration 039: tags テーブルの namespace CHECK 制約を完全削除
--
-- depends: 0038_pins_target_index_and_cascade
--
-- 背景:
--   現行 tags テーブルは namespace に CHECK(namespace IN ('', 'domain', 'intent'))
--   の enum 制約があり、新しい namespace（`ow:` / `outcome:` / 将来追加予定の他）を
--   保存するたびに migration で enum を拡張する必要があった。
--
--   将来 namespace を追加するたびに migration を切るのはレールが重いため、CHECK 制約
--   そのものをテーブルから取り去り、tags.namespace は任意の TEXT を受け付ける形に変更
--   する。namespace の妥当性は Python 層（tag_service.validate_and_parse_tags 等）で
--   バリデーションするポリシーに切り替える。
--
--   SQLite では CHECK 制約を ALTER で削除できないためテーブル再構築を行う。
--
-- 変更内容:
--   - tags テーブル再構築（namespace カラムから CHECK 制約を取り除く）
--   - 既存タグデータ・canonical_id・notes を全保持
--   - junction tables（activity_tags / topic_tags / log_tags / decision_tags /
--     material_tags）の REFERENCES は rename 後の tags にそのまま引き継がれる

-- ============================================
-- Step 1: 新 tags テーブル作成（CHECK なし）
-- ============================================
CREATE TABLE tags_new (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  namespace TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL,
  notes TEXT,
  description TEXT DEFAULT NULL
    CHECK(description IS NULL OR LENGTH(description) <= 100),
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  canonical_id INTEGER REFERENCES tags(id),
  UNIQUE(namespace, name)
);

-- ============================================
-- Step 2: 既存データ移行（id 不変）
-- ============================================
INSERT INTO tags_new (id, namespace, name, notes, description, created_at, canonical_id)
SELECT id, namespace, name, notes, description, created_at, canonical_id
FROM tags;

-- ============================================
-- Step 3: 旧テーブル削除 + リネーム
-- ============================================
DROP TABLE tags;
ALTER TABLE tags_new RENAME TO tags;

-- ============================================
-- Step 4: tags 用トリガーを再作成
-- ============================================
-- DROP TABLE tags でトリガー (0038 で作成された trg_pins_cascade_delete_tag) が
-- 消えるため再作成する。pins の CASCADE 削除を維持する。
CREATE TRIGGER trg_pins_cascade_delete_tag
AFTER DELETE ON tags
FOR EACH ROW
BEGIN
    DELETE FROM pins WHERE (source_type = 'tag' AND source_id = OLD.id)
                       OR (target_type = 'tag' AND target_id = OLD.id);
END;
