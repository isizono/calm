-- Migration 0062: habits always層プールのDBトリガーラチェット天井
--
-- depends: 0061_add_tag_archived
--
-- 背景:
--   アプリ層(habit_service._check_always_promotion_gate_with_conn)は定員1500字の
--   ラチェットゲートを持つが、これは update_habit 経由の更新にのみ効く。将来の
--   コードパス追加・手動SQL・マイグレーションのバグがこのゲートを経由せず habits に
--   直接書き込んだ場合に備え、DBトリガーによる独立した上限（2000字、アプリ層の
--   1500字より緩いハード天井）を設ける。増加のみを拒否するラチェット
--   （縮む変更・無効化は天井超過中でも常に許可）。
--
-- 変更内容:
--   - trigger_mode='always' AND active=1 なhabitの content 合計文字数が
--     2000字を超え、かつ超過後の合計が超過前の合計より増加する INSERT/UPDATE を
--     RAISE(ABORT) で拒否する2トリガーを追加する
--
-- 注意（将来のmigration作者向け）:
--   habits テーブルを DROP+RENAME で再構築する migration（0059/0060 と同様の手法）を
--   将来書く場合、SQLiteの仕様上トリガーはテーブルと一緒に消える。0039が
--   trg_pins_cascade_delete_tag を再作成したのと同じ要領で、本トリガー2本の
--   再作成を同一migrationに含めること。

CREATE TRIGGER trg_habits_always_pool_ratchet_ceiling_ins
BEFORE INSERT ON habits
FOR EACH ROW
WHEN NEW.active = 1 AND NEW.trigger_mode = 'always'
BEGIN
    SELECT RAISE(ABORT, 'always pool ratchet ceiling (2000 chars) exceeded')
    WHERE (
        (SELECT COALESCE(SUM(LENGTH(content)), 0) FROM habits
         WHERE active = 1 AND trigger_mode = 'always')
        + LENGTH(NEW.content)
    ) > 2000;
END;

CREATE TRIGGER trg_habits_always_pool_ratchet_ceiling_upd
BEFORE UPDATE OF content, active, trigger_mode ON habits
FOR EACH ROW
WHEN NEW.active = 1 AND NEW.trigger_mode = 'always'
BEGIN
    SELECT RAISE(ABORT, 'always pool ratchet ceiling (2000 chars) exceeded and increasing')
    WHERE (
        (SELECT COALESCE(SUM(LENGTH(content)), 0) FROM habits
         WHERE active = 1 AND trigger_mode = 'always')
        - (CASE WHEN OLD.active = 1 AND OLD.trigger_mode = 'always'
                THEN LENGTH(OLD.content) ELSE 0 END)
        + LENGTH(NEW.content)
    ) > 2000
    AND (
        (SELECT COALESCE(SUM(LENGTH(content)), 0) FROM habits
         WHERE active = 1 AND trigger_mode = 'always')
        - (CASE WHEN OLD.active = 1 AND OLD.trigger_mode = 'always'
                THEN LENGTH(OLD.content) ELSE 0 END)
        + LENGTH(NEW.content)
    ) > (SELECT COALESCE(SUM(LENGTH(content)), 0) FROM habits
         WHERE active = 1 AND trigger_mode = 'always');
END;
