-- Migration 0070: instance_meta テーブル追加
--
-- depends: 0069_add_asks_choices
--
-- 背景:
--   cc-memoryインスタンス間でtopic/decision/log/material/activityをexport/importする
--   機能の基盤として、インスタンス自身を識別する識別子を導入する。エンティティの
--   同一性はインスタンス識別子とローカルIDの複合キー（例: team-a:M12）で表現する。
--   環境変数でなくDBに置くのは、識別子がデータの同一性の根でありDBファイルと
--   運命を共にすべきため（envはDBを別マシンに移した瞬間に剥がれる）。
--
-- 変更内容:
--   instance_meta単一行テーブルを追加する（id=1固定のCHECK制約で複数行の挿入を防ぐ）。
--   instance_idはNOT NULLで空値を許さない。一意な形式バリデーション（DNSラベル風）は
--   サービス層で行う（他の文字数上限等と同じくDB制約ではなくサービス層バリデーションに
--   揃える）。

CREATE TABLE instance_meta (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    instance_id TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
