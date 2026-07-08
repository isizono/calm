---
name: check-in
description: アクティビティにcheck-inして関連情報を集約取得する。「/check-in」「チェックイン」「続きやる」「再開しよう」「前回の続き」「どこまでやったっけ」など、既存アクティビティの作業を再開する意図で発動する。新しい作業を始めるとき（activity-start）や、期間横断の振り返りには発動しない。
---

# check-in

指定されたアクティビティに対して `check_in` ツールを呼び出し、関連情報を調べてアクティビティの全体像と進捗を把握してください。check-in後にユーザーが「やって」と言えばすぐ作業・議論を開始できる状態にすることがゴールです。

## 手順

1. 引数で `activity_id` が指定されていればそのまま使う
2. 指定されていなければ、SessionStart hookで注入済みのトピック別アクティビティ一覧を提示する:
   a. SessionStart hookの出力にトピック別グルーピングが含まれていればそのまま使う
   b. グルーピングがなければ `get_activities(orch_managed=False)` でフラットに取得しフォールバック表示する
   c. IDは一切表示しない（トピックID・アクティビティIDともに非表示）。トピック名とアクティビティ名のみ
   d. ユーザーが名前やキーワードで選択したら、対応するactivity_idでステップ3へ進む
3. `check_in(activity_id=...)` を呼び出す
4. `get_logs`・`get_decisions`・`search` などで関連情報を取得し、概要と進捗を把握する
5. check-in結果に含まれるタグ一覧を見て、明らかな表記揺れや重複に気づいたらユーザーにサジェストする。分析ツール（`analyze_tags`）は呼ばない
6. 把握した内容を以下の2セクション構成でユーザーに伝える

## pinnedフィールドの扱い

check-in結果に `pinned` フィールドがある場合、その内容はタスクに常に意識してほしい情報としてpinされたエンティティ群である。以下の5種が含まれることがある（0件キーは省略される）:

- `pinned.decisions`: 重要な決定事項（id, title, reason付き）
- `pinned.logs`: 重要な議事録・ログ（id, title, content付き）
- `pinned.materials`: 重要な参考資材（id, title, content, source付き）
- `pinned.topics`: 関連トピック（id, title）
- `pinned.activities`: 関連アクティビティ（id, title, status）

pinned情報は進捗把握の最初に確認し、概要・進捗の説明に反映すること。

## hintsフィールドの扱い

check-in結果に `hints` フィールド（文字列リスト）がある場合、それはタグに蓄積したdecisionの整理（recompose-context skill）をおすすめしたい状況を示すナッジである。出力の最後に、各hintの内容を「〜をおすすめします」程度の一言として添えるに留めること。check-inの目的（現在のアクティビティの進捗把握）を差し置いてrecomposeに着手してはならない。

## 出力フォーマット

```
check-in: {activity.title}

## 概要
{タスクの背景・目的・やることがユーザーに伝わる程度にまとめる。activity.descriptionと関連情報をもとに構成する}

## 進捗
intent: {タグから抽出した intent 値、なければ省略}
{logs・decisions・materialsなどから読み取れる、実際にどこまで進んでいるか・何が残っているかの要約}
```
