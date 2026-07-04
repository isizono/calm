-- Migration 0053: vec_index孤児行の掃除
--
-- depends: 0052_add_tag_junction_indexes
--
-- 背景:
--   vec_indexはsearch_indexのidと同値のrowidで運用される仮想テーブルだが、
--   retract経路以外のエンティティ削除（ON DELETE CASCADE等）ではvec_index側の
--   対応行が掃除されない。対応するsearch_index行を持たない孤児行はベクトル検索の
--   グローバルKNN候補スロットを浪費し、search_indexとのJOIN段で黙って消える。
--
-- 変更内容:
--   - search_indexに対応行を持たないvec_index行を削除する
--
-- 適用範囲（既知の制約）:
--   これは適用時点で存在する孤児行を整合する一回限りの掃除であり、発生源には手を入れない。
--   トピック削除等でdecisions/discussion_logsがCASCADE削除されるとtrg_search_*_deleteが
--   発火するが、これらはsearch_index/search_index_ftsのみ削除しvec_indexには触れないため、
--   同経路を通れば孤児行は再び蓄積し得る。vec_indexはvec0仮想テーブルでFK制約を持てず、
--   トリガーからのvec_index削除は全接続でsqlite-vec拡張のロードを必須化する（現状は拡張が
--   ロードできない環境でも削除系操作を許容する設計）ため、この掃除には含めていない。

DELETE FROM vec_index
WHERE rowid NOT IN (SELECT id FROM search_index);
