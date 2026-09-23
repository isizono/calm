-- Migration 0081: vec_index / tag_vec を distance_metric=cosine で再構築
--
-- depends: 0080_drop_activities_orch_managed
--
-- destructive: vec_index / tag_vec を一時テーブル退避経由でDROP TABLE + CREATE TABLE
--   再作成する。両テーブルの全行(rowid, embedding)を退避後に完全コピーし直すため、
--   データ損失は無い(embeddingベクトル値そのものは変換不要。cosineはノルムに依存
--   しないため既存BLOBをそのままコピーできる)。
--
-- 背景:
--   embeddingモデル(cl-nagoya/ruri-v3-70m)の想定距離はコサインだが、vec_index /
--   tag_vec は vec0 の既定である L2 のまま運用されてきた。格納embeddingは非正規化
--   (ノルムが一定でない)ため、L2距離にはノルムの大小が混入し、モデルの想定順序と
--   ずれる。加えて QE_DISTANCE_THRESHOLD (search_service.py) / MERGE_THRESHOLD
--   (tag_service.py) はコサイン距離のスケール(0〜2程度)を前提に書かれた値で、
--   L2の実距離スケール(非正規化ベクトル同士で30〜40程度)には遠く届かず、
--   クエリ拡張・タグ統合が構造的に発火しない状態になっている。
--
--   topic_vec(migration 0050)・ask_vec(migration 0062)は同じ非正規化embeddingを
--   distance_metric=cosineで格納しており、このリポジトリでは既にcosine運用の実績が
--   ある。vec_index / tag_vec だけが取り残されたL2のままだった。
--
-- 再構築手順(一時テーブル退避方式。ALTER TABLE ... RENAME TOは使わない):
--   sqlite-vec 0.1.6実機検証で、vec0仮想テーブルへのRENAMEは文としては成功するが
--   shadow tables(*_rowids / *_chunks 等)が旧名のまま残り、以後の全クエリが
--   `no such table` 系エラーで失敗するサイレント破壊を起こすことを確認済み。
--   migration自体は例外を出さずに成功扱いになるため、事後の検証(下記テスト)なしには
--   気づけない。(1)一時名でcosineテーブルをCREATE (2)既存テーブルから
--   INSERT SELECTで退避 (3)既存テーブルをDROP (4)本来名でcosineテーブルを再CREATE
--   (5)一時テーブルからINSERT SELECT (6)一時テーブルをDROP、の6手順を両テーブルに
--   適用する。

-- vec_index: 既存データを保持したまま distance_metric=cosine へ再構築
CREATE VIRTUAL TABLE vec_index_mig_tmp USING vec0(
  embedding float[384] distance_metric=cosine
);
INSERT INTO vec_index_mig_tmp(rowid, embedding)
  SELECT rowid, embedding FROM vec_index;
DROP TABLE vec_index;
CREATE VIRTUAL TABLE vec_index USING vec0(
  embedding float[384] distance_metric=cosine
);
INSERT INTO vec_index(rowid, embedding)
  SELECT rowid, embedding FROM vec_index_mig_tmp;
DROP TABLE vec_index_mig_tmp;

-- tag_vec: 同型再構築(実DBでは0行のためコピーは空振りで成立する)
CREATE VIRTUAL TABLE tag_vec_mig_tmp USING vec0(
  embedding float[384] distance_metric=cosine
);
INSERT INTO tag_vec_mig_tmp(rowid, embedding)
  SELECT rowid, embedding FROM tag_vec;
DROP TABLE tag_vec;
CREATE VIRTUAL TABLE tag_vec USING vec0(
  embedding float[384] distance_metric=cosine
);
INSERT INTO tag_vec(rowid, embedding)
  SELECT rowid, embedding FROM tag_vec_mig_tmp;
DROP TABLE tag_vec_mig_tmp;
