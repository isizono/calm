---
name: digest
description: 直近の記録を期間横断で俯瞰するダイジェストを生成する。「/digest」「最近何やったっけ」「今週のまとめ」「ここ数日の動きを見せて」「先週から何が決まった？」など、期間ベースの振り返り・俯瞰の意図で発動する。単一アクティビティの再開（check-in）、完了作業の深掘り振り返り（postmortem）、ドキュメント生成（scribe）には発動しない。
---

# digest

指定期間（デフォルト直近7日間）に動きのあったアクティビティ・決定事項・成果物をdomainごとに俯瞰するダイジェストを生成する。ユーザーに「ちゃんと記録が蓄積されている」実感を返すことがゴール。読み取り専用skillであり、記録・更新は一切行わない。

## 手順

### 1. 期間の決定

デフォルトは直近7日間（today - 7日 〜 today）。引数や会話中の期間表現があればそちらを優先する。

- 「今週」→ 今週月曜日 〜 今日
- 「ここ3日」「直近3日」→ 3日前 〜 今日
- 「先週から」→ 先週月曜日 〜 今日
- 「先月から」→ 先月1日 〜 今日
- 期間表現が無ければデフォルト7日間を使い、その旨を出力冒頭に明記する

以降の`since`はISO日付文字列（YYYY-MM-DD）として扱う。

### 2. データ取得

`get_activities`と`get_timeline`のみを使う。record系ツール（`add_*`/`update_*`/`retract`等）は呼ばない。

1. `get_activities(status="active", since={期間開始日}, limit=適宜)` で期間内に更新された進行中・保留中のアクティビティを取得する
2. `get_activities(status="completed", since={期間開始日}, limit=適宜)` で期間内に完了したアクティビティを取得する
3. 1・2で取得した各アクティビティについて `get_timeline(activity_id=activity.id, entity_types=["decision","log","material"], order="desc")` を呼び、紐づくdecision/log/materialを取得する
   - `get_timeline`に期間指定引数は無い。`order="desc"`（新しい順）で取得し、`created_at`が期間開始日より古いitemが現れた時点で以降は全て期間外なので打ち切ってよい
   - `activity_id`指定時は関連する全topicのエンティティを集約して返すため、そのアクティビティに紐づく決定・ログ・成果物を漏れなく拾える

アクティビティに紐づかない独立トピックの決定事項は本skillの収集対象外（アクティビティ起点の集約のため）。該当topicの深掘りを求められたらcheck-inや`get_timeline(topic_id=...)`へ誘導する。

### 3. グルーピング

- 各アクティビティの`tags`から`domain:`タグを抽出し、domainごとにアクティビティをまとめる
- domainタグが無いアクティビティは「未分類」として最後にまとめる
- 各domain内では、アクティビティごとに収集したdecision/log/materialを種別（decision/log/material）で整理する

### 4. 出力

以下の構成でユーザーに提示する。該当がないセクションは丸ごと省略する。

```
digest: {期間の説明}（{開始日} 〜 {終了日}）

## {domain名}
### 動いたアクティビティ
- [完了] {activity.title}
- [進行中] {activity.title}

### 決まったこと
- {decision.title}（{紐づくactivity名}）

### 継続中の論点
- {[議論中]decision.title}

### 保存された成果物
- {material.title}
```

- domainが複数ある場合はdomainごとに上記ブロックを繰り返す
- 全体の量が多い場合はdomainごとの件数・要点のみ提示し、詳細はユーザーが指名したdomain/activityだけ展開する

## 注意

- 読み取り専用skill。`add_*`/`update_*`/`retract`等の記録・更新系ツールは一切呼ばない
- ユーザーへの提示はタイトルベースで行い、内部ID（activity_id, decision idなど）は表示しない
- 量が多い場合はdomainごとの要約を優先し、詳細はユーザーが指名したものだけ展開する

## 関連skillとの境界

- 単一アクティビティに絞った再開・進捗把握はcheck-inの担当。digestは複数アクティビティを期間横断で俯瞰する
- 完了アクティビティ1件を深掘りして教訓を抽出するのはpostmortemの担当。digestは俯瞰に留まり深掘りしない
- 記録から外部共有可能なドキュメントを生成するのはscribeの担当。digestはその場での提示のみで、ドキュメント成果物は作らない
