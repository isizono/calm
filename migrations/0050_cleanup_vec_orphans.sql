-- Migration 050: vec_index孤児行の掃除
--
-- depends: 0049_add_tag_junction_indexes
--
-- 背景:
--   vec_indexはsearch_indexのidと同値のrowidで運用される仮想テーブルだが、
--   retract経路以外のエンティティ削除（ON DELETE CASCADE等）ではvec_index側の
--   対応行が掃除されない。対応するsearch_index行を持たない孤児行はベクトル検索の
--   グローバルKNN候補スロットを浪費し、search_indexとのJOIN段で黙って消える。
--
-- 変更内容:
--   - search_indexに対応行を持たないvec_index行を削除する

DELETE FROM vec_index
WHERE rowid NOT IN (SELECT id FROM search_index);
