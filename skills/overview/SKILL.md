---
name: overview
description: 進行状況の窓。get_overviewを1回呼び、今動いているもの/最近終わったもの/人間の裁定待ち/残りの内訳を4節で一望表示する。「/overview」「今何が進んでる」「全体状況見せて」「進捗どう」「今の状況教えて」などで発動。期間ベースの振り返りはdigest、単一アクティビティの再開・進捗把握はcheck-inの担当なのでこちらでは発動しない。
---

# overview

「今何が進んでいて、次に何をすべきか」をユーザーに一望で見せる読み取り専用skill。判定ロジック（何日以内なら動いている扱いか、in_progressの宣言と実態の乖離、トリアージ未了の扱い、domainタグのcanonical解決）はすべて`get_overview`ツール側に集約されており、本skillの手順は表示の整形だけを行う。

## 手順

1. `get_overview()` を1回だけ呼ぶ。ユーザーが期間や件数を指定していれば`days`/`limit`に渡す。他のツール（`get_activities`/`get_asks`等）は呼ばない。
2. 返ってきた4節を **`working` → `recently_done` → `awaiting_human` → `backlog` の固定順**で見出しにして出す。順序を入れ替えたり節を統合したりしない。
3. `working`/`recently_done`/`awaiting_human`は`items`を1行1件で列挙する。`working`は`is_live`が trueのものに印を付け、`days_since_touch`と`open_ask_count > 0`を添える。`awaiting_human`は`days_open`と`blocks`のタイトルを添える。`items`が空の節は見出しごと省略する。
4. `backlog`は`total_count`/`stale_in_progress_count`/`by_status`/`by_domain`/`no_domain_count`をそのまま提示する。個別のアクティビティは列挙しない。
5. いずれかの節で`count < total_count`なら「上位N件のみ表示（全M件）」と明記する。記録系ツール（`add_*`/`update_*`/`retract`等）は一切呼ばない。

## 注意

- 読み取り専用skill。`add_*`/`update_*`/`retract`等の記録・更新系ツールは一切呼ばない
- ユーザーへの提示はタイトルベースで行い、内部ID（`id_raw`など）は表示しない
- 「今週これだけ完了した」のように`recently_done`の件数を完了実績として語らない（`updated_at`近似のため、完了後の編集で再浮上しうる）

## 関連skillとの境界

- 期間を指定して複数アクティビティ・決定事項・成果物を横断的に振り返るのはdigestの担当。overviewは期間指定を持たず「今の状態」のスナップショットに徹する
- 単一アクティビティへの着手・再開はcheck-inの担当。overviewは個別アクティビティへの深掘りをしない
- `working`/`recently_done`/`awaiting_human`/`backlog`の判定基準（鮮度窓・heartbeat・トリアージ扱い・domain canonical解決）は`get_overview`ツール（`src/services/overview_service.py`）が持つ。本skillに集計ロジックを書き足さない
