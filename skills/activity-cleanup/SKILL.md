---
name: activity-cleanup
description: アクティビティ(active/shelved/snoozed)を棚卸しし、実態確認のうえでcompleted化・shelved化・description訂正・重複統合・裁定待ちのいずれかに処遇する。「/activity-cleanup」「アクティビティ棚卸しして」「activity棚卸し」「アクティビティの整理して」などユーザーが明示的に呼び出したときに発動する。DO NOT TRIGGER: sync-memory Step 10aの自動棚卸し(セッション終了時の軽い自己完結処理)、decision/topic等を含む全関連情報の統合・anchor整備(recompose-context)、単一アクティビティを完了にせず中断する操作(activity-pause)、タグの共起分析・整理(tag-cleanup)には発動しない。
---

# activity-cleanup

アクティビティ(active/shelved/snoozed)を対象に、記載されている状態を鵜呑みにせず機械的に実態確認したうえで処遇を判定し、反映する棚卸しskill。人間が在席している状態での手動実行を前提とする。

## 対象

走査対象は以下の3グループ全件:

- active(pending + in_progress)
- shelved
- snoozed

`orch_managed`カラムによる絞り込みは行わない。orch_managed=1のactivityも棚卸しの対象に含める。orch_managedカラム自体を別作業で削除する方向であり、新規実装では参照しない。

**`get_activities`呼び出し時の注意:**

- `limit`引数は明示指定が必須。デフォルトは5件のため、明示しないまま呼ぶと全件取得に失敗する(件数が少ないことに気づかないまま棚卸しが不完全に終わる)
- `status="active"`はpending + in_progressのみを返すエイリアスで、shelved/snoozedは含まれない。shelved・snoozedをそれぞれ対象に含めるには`get_activities(status="shelved", limit=N)`・`get_activities(status="snoozed", limit=N)`を別途明示的に呼ぶ必要がある(`status="active"`では返らない)

**snoozedを対象に含める理由:** ここでいう「含める」は走査・集計対象の母集団の話であり、「寝かせる」という新しい処遇の話ではない。処遇語彙は[手順3](#3-処遇判定)の6つに限定し、寝かせる処遇は既存の`shelved`+再開条件で表現する(`snoozed`を処遇として新たに使うことはしない)。既存のsnoozed状態のactivityも実態確認・処遇判定の対象に含める、という走査範囲だけの話として扱う。

**snoozed抽出の副作用:** `get_activities`は呼び出し時、statusの指定に関わらず、updated_atから3日(SNOOZE_DURATION_DAYS)を超過したsnoozedアクティビティを自動的にpendingへ復活させてから検索する(lazy evaluation、既存仕様)。つまり棚卸しの実行自体が一部のsnoozedをpending化する副作用を持つ。これは実態確認の前提として扱い、復活後にpendingとして出てきても驚かない。

## 発動契機

実行は手動のみ。ユーザーが「アクティビティ棚卸しして」「/activity-cleanup」などで明示的に呼び出したときに走る。人間が在席していることが前提の作業であり、session_end等のhookから無人で自律実行する運用は行わない。自律度ルールの🔴を「その場で確認」で処理できるのも、この手動・在席前提があるため。

## 手順

### 1. 対象抽出

active・shelved・snoozedそれぞれについて `get_activities(status=..., limit=N)` を呼ぶ。Nは想定件数を確実に上回る値を明示指定する(デフォルトの5件のまま呼ばない)。

**取りこぼしの機械確認:** `limit`の見積もりが外れていないかは目視ではなく機械的に確認する。`get_activities`のレスポンスは該当ステータスの全件数を表す`total_count`を含むため、各ステータスについて次を必ず突き合わせる。

- 返ってきた`activities`の件数が`total_count`と一致するか確認する
- 一致しない場合(`activities`件数 < `total_count`)は取りこぼしが発生している。`limit`を`total_count`以上に上げて同じstatusで再取得し、再度突き合わせる
- この確認をactive・shelved・snoozedそれぞれについて独立に行う(あるステータスで一致していても他のステータスで取りこぼれている場合がある)

snoozedを抽出する呼び出し自体が期限切れ分をpendingへ自動復活させる(前述)。復活後の状態を前提に以降の手順を進める。

### 2. 実態確認

各activityについて、記載されている状態を鵜呑みにせず機械的に裏取りする。件数が多い場合は[手順4](#4-反映)と同じ考え方で進め方を分ける。

- **件数が少ない場合**: 対象を順に1件ずつ確認する
- **件数が多い場合**: 全件を均等な優先度で潰そうとせず、まず`gh pr view`が要る(PRリンクを持つ)activityと、そうでないactivity(shelvedの再開条件確認・description訂正のみで済むもの)に仕分ける。前者から着手し、外部コマンド実行を伴う確認をWorkflowで並列実行してよい(反映と同様、規模に応じて手動・並列どちらでもよい)。後者は情報がactivity自身に閉じているため後回しにしてよい

確認する内容:

- **PRマージ状態**: descriptionに「マージ待ち」等の文言があっても、それだけで判断しない。`gh pr view <PR番号> --json state,mergeStateStatus` で機械的に確認してから判断する
- **mainコードでの実装有無**: PRの有無に関わらず、該当機能がmainに実装済みかを直接確認する
- **shelvedの再開条件充足**: descriptionに書かれた再開条件が現時点で充足しているかを確認する

**shelvedの実態確認には`check_in`を使わない。** check-inすると、statusがin_progress以外の場合(shelved・snoozedを含む)は自動的にin_progressに遷移してしまう既存仕様があるため、実態確認だけのつもりが状態を書き換えてしまう。

```
# NG: check_inすると自動的にin_progressに遷移してしまう
check_in(activity_id=xxx)

# OK(shelved): 状態を書き換えずに確認
get_activities(tags=[...], status="shelved", limit=N)
get_by_ids(items=[{"type": "activity", "id": xxx}])
```

snoozedの実態確認も同様に`check_in`を避けるが、`get_activities`自体が「読み取り専用」とは言い切れない点に注意する。[対象](#対象)節で述べたとおり、`get_activities`の呼び出しは(statusの指定に関わらず)期限切れsnoozedをpendingへ自動復活させるUPDATEを常に先に実行してから検索する。そのためsnoozedの実態確認は「復活を引き起こす可能性のある確認手段」であって、副作用ゼロの読み取り専用ではない。この副作用はactivityのstatus(shelved/snoozed)を書き換えるものではなく、あくまでlazy evaluationによる期限切れ分の自動遷移なので、`check_in`が引き起こす「実態確認のつもりでin_progressへ書き換わる」問題とは性質が異なる。加えて、`get_activities(status="snoozed")`は復活UPDATEを実行してから`status`で絞り込むため、期限切れだったactivityはこの呼び出し結果には現れず(pending側に回る)。active・shelved・snoozedの3クエリを全て回す限り母集団からの取りこぼしは起きないが、「snoozedクエリの結果に出てこない=対象から消えた」ではない点を押さえておく。

一方、descriptionの更新(再開条件の訂正・誤字修正など)は`update_activity`で行ってよい。shelved中にstatus以外のフィールド(title/description/tags)を更新しても自動復活は起きない(自動復活が起きるのはsnoozedにstatusを指定せず更新した場合のみで、shelvedには適用されない既存仕様)。

### 3. 処遇判定

実態確認の結果を踏まえ、以下6つの語彙のいずれかに処遇を決める。新しい状態カラムや新しい処遇語彙を追加しない。

| 語彙 | 内容 |
|---|---|
| completed | 実態が完了していることを機械確認できたもの。`update_activity(status="completed")` |
| shelved + 再開条件 | 上流待ちなど、いま進める意味がなく再開条件が明確に書けるもの。`update_activity(status="shelved", description=<再開条件を明記>)` |
| description訂正 | 状態は現状のままで正しいが、description・タイトルが実態と食い違っている(誤字含む)もの。`update_activity(description=...)` などで訂正のみ行う |
| relation+completedで統合 | 同じ目的の重複activityが複数あるもの。情報量が多い方を残し、`add_relation(relation_type="related")`で残す側と紐づけたうえで、重複側を`update_activity(status="completed")` |
| askで裁定待ち | 実態確認・ユーザーへのその場確認を経てもなお、その場では処遇を決め切れないもの(第三者の判断待ち・将来の外部イベント待ちなど)。`add_ask`で正式に裁定待ちとして記録する |
| 進行・着手待ちは現状維持 | in_progress/pendingのまま、実態上も特に処遇変更が要らないもの。何もしない |

「askで裁定待ち」は[自律度ルール](#自律度ルール)の🔴とは別物である。🔴はエージェント自身が処遇を分類しきれない場合に使う確認の仕組みであり、その場でユーザーに確認して解消する。確認した結果、ユーザー本人もいまその場では判断できないと分かった場合にのみ、この処遇語彙としての「askで裁定待ち」を使う。

### 4. 反映

処遇判定の結果を既存の`update_activity`(・重複統合時は`add_relation`)で反映する。新規の一括処遇反映ツールは作らない。

件数が少なければ手動で順に呼ぶ。件数が多い場合はWorkflowで並列実行してよい(規模に応じてどちらでも良い、既存のrecompose-context skillと同様の運用)。

### 5. 完了時クールダウン

棚卸しが完了したら、`activity-management`タグのnotesに期限付きマーカーを書き、7日間は催促を止める。手順は以下の順で行う(sync-memory Step 10bのtag notes手順と同じ形)。

1. **既存notesの読み出し**: `activity-management`はnamespaceが空文字の素タグである。`search_tags(query="activity-management", namespace="", include_notes=True)`で候補を取得し、返却された`name`が`"activity-management"`と完全一致するタグのnotes全文を取得する。search_tagsはタグ名のLIKE一致とベクトル検索のハイブリッドで意味的に近いだけの別タグも返るため、上位ヒットの有無ではなく`name`の完全一致で対象タグを特定する。**完全一致するタグが0件だった場合は、本手順(完了時クールダウン)をここで中止し、ユーザーにその旨を報告する。** `update_tag`は既存notesとの差し替えを無条件の全文置換で行うため、既存notesを読み出せないまま本節の4(書き込み)に進むとマーカー1行だけがnotes全文として書き込まれ、既存notesを消してしまう。0件時に空文字や仮のnotesで進めてはならない
2. **既存マーカーの確認**: 取得したnotes内に`#activity-cleanup-skipped-until:YYYY-MM-DD`が既に存在し、かつその日付が今回書こうとしている日付より未来の場合は、ユーザーが意図的に設定した長期抑制とみなして**上書き・短縮しない**(何もせず本手順を終える)。既存マーカーが無い、または今回書く日付以前(過去・当日)であれば3へ進む
3. **マーカーの算出**: 以下の書式で、他の内容を消さないよう既存notesを保持したうえで追記・更新する

```
#activity-cleanup-skipped-until:YYYY-MM-DD
```

   **YYYY-MM-DDの計算に注意**: 判定側(CALM側のhint_service相当)は`date.today() <= YYYY-MM-DD`を抑制有効の条件とするため、実行日を含めてN日間抑制したい場合は`実行日 + (N-1)日`を指定する。7日間の抑制であれば`実行日 + 6日`が正しい(`実行日 + 7日`を書くと実行日を含めて8日間の抑制になり、意図より1日長くなる)
4. **書き込み**: `update_tag`で全文置換する(既存notesに3で算出したマーカーを追記・更新した全文を渡す)

マーカー文字列自体はCALM側の実装(hint_service相当)の定数と一致させる必要があり、値はコード側の定義が正となる。

## 自律度ルール

recompose-context skillの自律度ルールをそのまま流用し、対象語彙をactivity-cleanupの処遇語彙に翻訳したもの。

| ゾーン | activity-cleanupでの処遇 |
|---|---|
| 🟢 自律 | description訂正(誤字・状態食い違いの修正) / PRマージ機械確認が取れたcompleted化 / 進行・着手待ちのまま現状維持 |
| 🟡 確証あれば自律 | shelved化(再開条件が明確に書ける場合) / relation+completedでの統合(重複が明白な場合) |
| 🔴 その場で確認 | 上記のいずれにも確証を持って分類できないもの |

**怪しき発火条件(全ゾーン共通で🔴に格上げ):**

- 矛盾で新旧不明(例: descriptionと実コードの状態が食い違い、どちらが正か判断できない)
- [議論中]相当の未解決分岐がactivity自身やその関連decisionに残っている
- descriptionやlogに懸念が明記されている
- 推測でしか判定不可(PRの実在確認ができない、gitログが読めない等)

**🔴は`ask`(離席中裁定用の機構)に流さない。** 棚卸しskillは人間が手動で呼び出す=セッション内に人間が在席している前提の作業であり、recompose-context原典の🔴も「その場で確認 / バッファに溜めて一括提示」という在席前提の確認機構である。離席中判断用のask機構に流すのは設計として不整合。

実行中に🔴に該当するケースが出たら、確認をバッファに溜めて最後に一括提示・ジャッジする(recompose-context原典と同じハイブリッド方式)。重大な矛盾はその場ですぐ確認してよい。

## 注意

- **sync-memory Step 10aとの関係**: sync-memory Step 10aは、セッション終了時に自己完結で動く軽い自動棚卸し(重複・7日放置・フェーズ移行済みの検出とcompleted/snoozed化)であり、本skillとは独立に今後も動作し続けてよい。両者の判定基準が食い違った場合はactivity-cleanup(本skill)を正本とする。ただしsync-memory Step 10aは`get_activities(status="active", orch_managed=False)`で取得したactivityのみを対象にするため、判定を突き合わせられる範囲もactiveかつorch_managed=0のものに限られる(shelved・snoozed、orch_managed=1のactivityはStep 10aの対象外)
- **PR状態の鵜呑み防止**: description本文の「マージ済み」「マージ待ち」等の文言は鮮度が保証されない。completed判定には必ず`gh pr view --json state,mergeStateStatus`等の機械確認を伴わせる
- **内部IDを報告に出さない**: ユーザーへの報告や記録では、activityのタイトル・内容の要約で言及し、内部ID単体では言及しない
- 判断に迷い🔴に該当するものは、消さない・completedにしない側に倒したうえでバッファに溜め、最後に一括確認する
