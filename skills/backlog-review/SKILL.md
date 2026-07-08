---
name: backlog-review
description: 【必須】improvement-backlogタグ付きlog/materialの未処理累積が閾値に達した状況で発動する。SessionStartまたはcheck-inのhintで「要望会タイミングです（未処理N件）。/backlog-review で着手」を受け取りユーザーが着手を承認した状況、または「/backlog-review」「要望会やろう」「バックログレビューして」等ユーザーが直接要望した状況で発動する。このスキルを経由せずにimprovement-backlogタグ付き項目の一括判定（採用/却下/保留の分類・タグ付け）を行ってはいけない。TRIGGER: hint受領後にユーザーが着手を承認した／ユーザーが要望会・backlog-review着手を明示した、のいずれか。DO NOT TRIGGER: improvement-backlogタグ付き項目1件だけをその場で個別対応する場合／discomfort-protocolタグのみでまだimprovement-backlogに昇格していない違和感メモを扱う場合／緊急バグ調査中／設計上の大論点を議論している最中。
---

# backlog-review

improvement-backlogタグが付いた未処理のlog/materialをまとめてレビューし、採用（設計activity化）/却下（decision記録）/保留（タグ付け）に振り分ける「要望会」を実行する。

## 発動契機

- **hint経由（主経路）**: SessionStartの表示、または`check_in`の`hints`フィールドに「要望会タイミングです（未処理N件）。/backlog-review で着手」が出た後、ユーザーが着手を承認した
- **ユーザー明示**: 「/backlog-review」「要望会やろう」等の直接呼び出し

いずれの経路でも、対象件数と概要を提示してユーザーの承認を取ってから本体フローに入る（自律発動 ≠ 全自動）。

## 手順

### 1. 未処理バックログの取得

- `search(tags=["improvement-backlog"], entity_type="log", limit=50)` と `search(tags=["improvement-backlog"], entity_type="material", limit=50)` をそれぞれ呼ぶ
- `search`の`tags`はAND条件のみでNOT条件がないため、`improvement-backlog-triaged`または`improvement-backlog-deferred`が付いているものは、返ってきた各アイテムの`tags`配列を見てクライアント側で除外する
- 除外後に残った件数が今回のレビュー対象件数

### 2. Edge case判定

- **0件**: 「未処理バックログはありません」と伝えて終了する
- **各searchの`total_count`が50件超**: 全件を一度に扱わず、「直近作成分」「特定タグ」等のサブセット選択肢を提示し、対象を絞ってから3へ進む
- 上記以外はそのまま3へ

### 3. SA事前トリアージ

対象が6件以上の場合、Agentツール（`subagent_type: "scout"`, `mode: "auto"`, `run_in_background: true`）でSAに分類提案を委譲する。5件以下ならこのステップを省き自分で分類案を作ってよい。

SAへの依頼に含めるもの:
- 各アイテムのtitle/snippet/tags全文
- 分類観点: 採用候補（具体的な設計/実装activityを起票する価値がある）／却下候補（対応不要と判断できる理由がある）／保留候補（今は判断材料が足りない）
- 各アイテムに1行の分類提案+理由を返すよう指示

SAの提案は「たたき台」であり、最終判断はユーザーのバッチ承認で行う。scoutの成果物は裏取り前提の生データとして扱い、そのまま実行に流さない。

### 4. 5件バッチでユーザー承認

5件ずつAskUserQuestionで提示する。1バッチの選択肢:
- A: SA提案通り承認
- B: 一部修正して承認（どのアイテムをどう変えるか自由記述）
- C: このバッチは保留にして次へ

**承認が得られたバッチから即座に実行する**（全バッチの承認を待たない）。これにより、ユーザーが途中で中断しても、それまでの承認済みバッチは処理済みとして残る。

### 5. 実行（承認された分類ごと）

- **採用**: `add_activity`でintent:designの`[設計]`activityを新規作成する。`related`に起点log/materialを紐づける。**作成成功を確認してから**起点log/materialに`improvement-backlog-triaged`タグを追加する（既存タグを保持したまま追記、全置換で消さないこと）
- **却下**: `add_decisions`で却下decisionを記録する。`topic_id`は起点log/materialが既存で属するtopicがあればそれを使う（`get_by_ids`や`get_map`で確認）。属していなければ`search(keyword="improvement-backlog 要望会", entity_type="topic")`で該当topicを検索して使う。reasonに却下理由を明記する。**記録成功を確認してから**起点log/materialに`improvement-backlog-triaged`タグを追加する
- **保留**: 起点log/materialのタグに`improvement-backlog-deferred`を追加する（`improvement-backlog-triaged`は付けない）

**順序を厳守する**: 成果物（activity/decision）の作成が成功してから起点のタグを更新する。作成に失敗した場合はタグを変更せず、そのアイテムは未処理のまま次回に持ち越す（この順序が失敗時の一貫性を保証するため、別途ロールバック処理は不要）。

### 6. 完了報告

`add_material`で今回の要望会サマリを保存する。内容: 採用N件・却下M件・保留L件の内訳（各アイテムのタイトルと振り分け先）、次回しきい値到達までの未処理残数見込み。`related`で対象topicや今回作成したactivity/decisionと紐づける。

ユーザーへの報告は「採用N件（新規activity一覧）・却下M件・保留L件、要望会完了」の簡潔な要約に留める。

## discomfort-protocolとの境界

- `improvement-backlog`タグが付いていない（discomfort-protocolタグのみの）log/materialは本スキルの対象外。まだ「育っていない」違和感の段階
- discomfort-protocol → improvement-backlogへの昇格判定は別フロー。本スキルは既にimprovement-backlogが付いたものだけを扱う

## 関連skillとの境界

- 単発1件のimprovement-backlog対応（その場で判断がつく軽微な要望）は本スキルを介さず直接処理してよい。本スキルは「まとめて」の継続運用が対象
- 記録の同期・棚卸し全般はsync-memoryの担当。本スキルはimprovement-backlogタグに限定したレビューに専念する

## 注意

- ユーザー承認なしにactivity/decisionを大量生成しない。5件バッチ承認を必ず経由する
- 完了報告のmaterial保存はレビュー完了ごとに1件（複数回に分けて保存しない）
