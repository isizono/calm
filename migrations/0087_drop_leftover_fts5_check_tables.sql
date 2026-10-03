-- Migration 0087: 起動時FTS5可否チェックが残した一時テーブルの削除
--
-- depends: 0081_vec_cosine_rebuild
--
-- destructive: FTS5可否チェック用に作られたFTS5仮想テーブル_fts5_checkを削除する。
--   FTS5仮想テーブルのDROPは、紐づく影のテーブル5つ（_fts5_check_config/_content/
--   _data/_docsize/_idx）も連鎖して削除する。いずれもチェック専用のテーブルで、
--   他のテーブル・コードから参照されていない。
--
-- 背景:
--   _check_fts5_availableはCREATE VIRTUAL TABLEの直後にDROP TABLEで後始末する実装
--   だったが、DROPがsqlite3.OperationalErrorで失敗した場合に例外を握り潰すだけで、
--   CREATEで自動生成された_fts5_checkと影のテーブルが本体DBに残り続けていた
--   （この後始末漏れ自体は別途修正済み）。本migrationは既に残ってしまった
--   環境の後始末を行う。IF EXISTSのため、残っていない環境では何もしない。

DROP TABLE IF EXISTS _fts5_check;
