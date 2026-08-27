-- Migration 0071: import_provenance テーブル追加
--
-- depends: 0070_add_instance_meta
--
-- 背景:
--   cc-memoryインスタンス間でバンドルをimportする際、受け側は取り込んだ各エンティティの
--   出自（どのインスタンスの何番から来たか）を永続記録する必要がある。この1テーブルが
--   再importの冪等性判定（UNIQUE制約への照合）・上流変更検知（content_hash比較）・
--   増分importでの参照自己解決（provenance逆引き）・チェーンexportでの正準キー維持を
--   同時に担う。
--
-- 変更内容:
--   import_provenanceテーブルを追加する。
--   - PRIMARY KEY (entity_type, entity_id): ローカルエンティティ1件につき出自は1つ
--   - UNIQUE (origin_instance, entity_type, origin_id): 同一出自エンティティの重複importを
--     防ぐ（再importはUPDATEで扱う）
--   - content_hashはimport時点のプロトコル対象フィールドのみのハッシュ（export側の
--     manifest.entities[].content_hashと同じ計算方式）。次回import時にorigin側の
--     content_hashと比較し、上流で変更されたかを判定する
--   - bundle_idはどのバンドルで入ってきたかの追跡用

CREATE TABLE import_provenance (
    entity_type         TEXT NOT NULL CHECK (entity_type IN ('topic', 'activity', 'material', 'decision', 'log')),
    entity_id           INTEGER NOT NULL,
    origin_instance     TEXT NOT NULL,
    origin_id           INTEGER NOT NULL,
    content_hash        TEXT NOT NULL,
    origin_created_at   TEXT,
    bundle_id           TEXT NOT NULL,
    imported_at         TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (entity_type, entity_id),
    UNIQUE (origin_instance, entity_type, origin_id)
);
