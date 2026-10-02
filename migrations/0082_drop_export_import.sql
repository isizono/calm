-- Migration 0082: instance_meta / import_provenance テーブル削除
--
-- depends: 0081_vec_cosine_rebuild
--
-- destructive: インスタンス間export/import機能自体の撤去に伴う削除で、代替スキーマへの
--   移行ではない。本番では両テーブルとも0行（export_bundle/import_bundleが一度も
--   呼ばれていない）であることを確認済みのため、既存行に対する移行処理は不要。
--
-- 背景:
--   instance_meta（0070で新設）・import_provenance（0071で新設）は、calmインスタンス間で
--   topic/decision/log/material/activityをexport/importするバンドル機能の基盤
--   テーブルだった。export_bundle/import_bundle/collect_export_candidates/
--   set_instance_identityの4ツールが全履歴で一度も呼ばれておらず、単一インスタンス
--   運用が実態であるため、ツール本体ごと撤去する。
--
-- 変更内容:
--   - instance_meta テーブルを DROP
--   - import_provenance テーブルを DROP

DROP TABLE instance_meta;
DROP TABLE import_provenance;
