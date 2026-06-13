-- Migration 036: materialsテーブルにupdated_atカラムを追加
--
-- depends: 0035_drop_pinned_columns
--
-- 背景:
--   check-in時のrecomposeナッジhint判定で、pinされたmaterialの最終更新時刻を
--   基準時刻Tとして使う。materialsテーブルにはupdated_atが無く、created_atしか
--   ないため、recompose（update_material）による更新時刻を追跡できない。
--
-- 変更内容:
--   - materials.updated_at を追加（NULL許容）。
--     SQLiteのALTER TABLE ... ADD COLUMN は非定数DEFAULT（datetime('now')等）を
--     許容しないため、DEFAULTを付けずNULL許容で追加する。
--   - 既存行は created_at で初期化する（バックフィル）。
--   - 以降のINSERT/UPDATEは material_service 側で updated_at をセットする。

ALTER TABLE materials ADD COLUMN updated_at TIMESTAMP;

UPDATE materials SET updated_at = created_at;
