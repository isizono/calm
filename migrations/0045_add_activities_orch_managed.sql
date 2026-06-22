-- Migration 0045: activitiesテーブルに orch_managed BOOLEAN カラムを追加
--
-- depends: 0044_sanitize_log_table
--
-- 背景:
--   orch-managed (orchが管理するアクティビティであることを表す属性) は
--   これまで素タグ "orch-managed" を付けることで表現していたが、
--   タグ運用を構造的属性 (カラム) に昇格させる。
--
--   タグ運用のデメリット:
--     - "活動が orch 管轄かどうか" という静的属性をタグの存在/不在で表すと
--       読み手側で毎回 JOIN が必要、メモリ上での判定もタグ展開を経由する
--     - 「タグの意味」をコード側に注入する形になり、データモデルに表れない
--
--   構造的属性に昇格することで、JOIN 不要のカラム参照で
--   一次判定 (Stop hook / SessionStart hook / hint 抑制) が完結する。
--
-- 変更内容:
--   - activities.orch_managed (BOOLEAN NOT NULL DEFAULT FALSE) を追加。
--     SQLite は BOOLEAN を INTEGER affinity で扱う (0/1)。
--   - 既存の素タグ "orch-managed" を持つ activity に対し orch_managed=1 を反映するデータ移行を含む。
--     タグ自体 (tags 行および activity_tags 行) は本 migration では削除しない (後続で扱う)。

ALTER TABLE activities ADD COLUMN orch_managed BOOLEAN NOT NULL DEFAULT 0;

UPDATE activities
SET orch_managed = 1
WHERE id IN (
    SELECT at.activity_id
    FROM activity_tags at
    JOIN tags t ON t.id = at.tag_id
    WHERE t.namespace = '' AND t.name = 'orch-managed'
);
