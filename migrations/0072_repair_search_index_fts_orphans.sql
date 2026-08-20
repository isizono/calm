-- Migration 0072: search_index_ftsの孤立行・rowid衝突を全量リビルドで修復する
--
-- depends: 0071_add_import_provenance
--
-- 背景:
--   trg_search_decisions_update / trg_search_logs_update / trg_search_materials_update
--   (AFTER UPDATE、対象カラムを問わず無条件発火)は内部で
--   `(SELECT id FROM search_index WHERE source_type=? AND source_id=OLD.id)`により
--   search_index.idを引き当てる実装になっている。retract済み行（search_indexの対応行
--   が物理削除済み）に対してUPDATEが走ると、このサブクエリがNULLを返し、
--   `INSERT INTO search_index_fts (rowid, ...) VALUES (NULL, ...)`でFTS5がrowidを
--   自動採番する。この自動採番idはsearch_index.idのAUTOINCREMENTシーケンス
--   (sqlite_sequence)とは独立に進むため、後から追加される別エンティティの
--   search_index.idと衝突しうる。衝突すると、取り消し済みエンティティの本文で
--   検索したはずが無関係な別エンティティがヒットする。
--   `retract(entity_type, ids, undo=True)`のUPDATE（retracted_atをNULLに戻す）が
--   この経路を最も踏みやすく、un-retract操作のたびにこの孤立行が発生しうる状態だった。
--
--   本 migration と対になるアプリケーション側の修正（search_index再登録を
--   UPDATEより前に行う）は再発防止のみを行い、適用前に既に発生した孤立行・
--   rowid衝突は修復しない。本 migration は既存データを一括修復する。
--
-- 変更内容:
--   search_index_fts (contentless FTS5、'rebuild'コマンド不可) を'delete-all'で
--   全消去した上で、search_index結合各ソーステーブルの現在値から全件を再投入する。
--   search_indexに対応行を持たない孤立行は再投入されないため消える。
--   同じrowidに複数エンティティの内容が混在していた行も、正しい1エンティティの
--   内容のみで上書きされる。

INSERT INTO search_index_fts(search_index_fts) VALUES('delete-all');

INSERT INTO search_index_fts (rowid, title, body)
SELECT
  si.id,
  CASE si.source_type
    WHEN 'decision' THEN d.decision
    WHEN 'log' THEN l.title
    WHEN 'material' THEN m.title
    WHEN 'topic' THEN t.title
    WHEN 'activity' THEN a.title
  END,
  CASE si.source_type
    WHEN 'decision' THEN d.reason
    WHEN 'log' THEN l.content
    WHEN 'material' THEN m.content
    WHEN 'topic' THEN t.description
    WHEN 'activity' THEN a.description
  END
FROM search_index si
LEFT JOIN decisions d ON si.source_type = 'decision' AND si.source_id = d.id
LEFT JOIN discussion_logs l ON si.source_type = 'log' AND si.source_id = l.id
LEFT JOIN materials m ON si.source_type = 'material' AND si.source_id = m.id
LEFT JOIN discussion_topics t ON si.source_type = 'topic' AND si.source_id = t.id
LEFT JOIN activities a ON si.source_type = 'activity' AND si.source_id = a.id;
