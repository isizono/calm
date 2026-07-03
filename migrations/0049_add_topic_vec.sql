-- Migration 0049: topic_vec 仮想テーブル追加（topic 専用ベクトル索引）
--
-- depends: 0048_session_identity
--
-- 背景:
--   topic の近傍検索は既存の全 entity 共用 vec_index を使うと、グローバル KNN で
--   k 件取得してから source_type='topic' で絞り込む post-filter 方式になる。
--   corpus が増えると topic が top-k から脱落し、絞り込み後に空集合になりうる
--   構造的弱点を持つ。tag_vec（migration 0009）と同じ「型専用の vec0 テーブル」
--   方式を topic にも適用し、KNN の母集団を最初から topic のみに限定する。
--
--   distance_metric=cosine を明示するのは、vec0 の既定が L2 であり、
--   埋め込みベクトルが非正規化（ノルムが一定でない）のため L2 だとノルムが
--   距離に混入してしまうため。cosine はノルムに依存せず、add_topic が
--   既に生成しているベクトルをそのまま使い回せる。
--
-- スキーマ:
--   rowid = discussion_topics.id（vec_index が search_index.id を rowid にするのと異なり、
--   topic_vec は topic 専用のため topic_id を直接 rowid にする）。
--
-- 注意: 仮想テーブルのため外部キー制約が使えない。topic を削除する経路を
--   将来追加する場合は、同じトランザクション内で topic_vec の対応行も
--   削除すること（0005 の vec_index と同じ規約）。
CREATE VIRTUAL TABLE IF NOT EXISTS topic_vec USING vec0(
  embedding float[384] distance_metric=cosine
);
