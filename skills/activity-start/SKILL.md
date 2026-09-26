---
name: activity-start
description: 【必須】新しいアクティビティを開始する。「/as」「/activity-start」「新しい作業始める」「アクティビティ作って」「これやる」など、新規アクティビティの作成・開始の意図で発動する。このスキルを経由せずにadd_activityを直接呼んではいけない（orchの子の起票はcalm:orch skillの手順に従う例外とする）。
---

# activity-start

新しいアクティビティを作成してcheck-inする。

## 手順

1. ユーザーの入力と会話の文脈から、以下3つを判定する:
   - **タイトル**（何をやるか）
   - **intent**（議論/実装/調査/デバッグ/設計/...）
   - **domain**（どのプロジェクト）
2. 3つのうち推定できないものがあれば、**1ターンだけ**聞いてよい。それ以上は聞かない
3. **重複チェック**: `add_activity` を呼ぶ前に、タイトルのキーワードで `search` を実行し、同じ目的のactivity・topicが既にないか確認する（`entity_type` は指定せず横断検索し、結果の `type` が `activity` / `topic` の項目を見る）。過去セッションや並行セッションが同じテーマを扱っている可能性があるため、再発明・二重管理を防ぐための必須ステップ
   - 同じ目的のactivityが見つかった場合は新規作成を提案せず、**そのactivityへのcheck-in（再開）** を提案する
   - 承認された場合、`check_in` ツールを直接呼ばず [check-in](../check-in/SKILL.md) skillに委譲する（anchor.pinned/env.hintsフィールドの扱いや出力フォーマットをスキップしないため）
   - 見つからなかった場合、またはユーザーが新規作成を選んだ場合のみ次のステップに進む
4. **related候補の特定**: 手順3の検索結果を流用し、関連するtopic/activity/decisionを能動的に洗い出す。会話の文脈から得られる関連先も合わせて候補にする
5. **intent:implementガードの先回り**: intentが `implement` と判定された場合、`add_activity` は `related` に `type: "decision"` のエントリを最低1件含めないと `IMPLEMENT_WORKFLOW_GUARD` エラーで弾かれる（`add_activity` ツールdocstring参照）
   - 手順3〜4で見つかった関連decisionのうち、合意済みのものをrelateする
   - 合意済みdecisionが見つからない場合は、「いきなり実装に入る理由」をユーザーに確認し、その回答を `add_decisions` でdecisionとして記録してから、そのdecisionをrelateする
6. 作成内容をユーザーに提示して確認を取る
   - タイトル、intent、domain、tagsの案を見せる
   - **明らかに迷いようがない場合のみ**確認をスキップしてよい（例: `/as READMEのインストール手順を更新` で対象リポジトリが明確な場合）
7. `add_activity` でアクティビティを作成する（check_in=True）
   - `title`: intentに応じたプレフィックスを付ける（`[議論]`, `[作業]`, `[調査]`, `[設計]` 等）
   - `tags`: `domain:` タグ（必須）+ `intent:` タグ（必須）+ 内容を表す素タグ
   - `description`: ユーザーの入力から得られた情報をできるだけ記載。構造の例: 背景（なぜやるか）/ スコープ（何をやるか）/ やらないこと（対象外）
   - `related`: 手順4・5で特定した関連エンティティを紐づける
8. **終了条件の3択**（手順7で作成した`activity_id`に対して行う）

   orchのアクティビティ（`orch`タグを付けるもの）はこの3択の対象外。calm:orch skillの「なる」節でgoalまで作る。

   既存の未判定goalに続く作業の場合は、3択に入る前にここで紐づけて次のステップへ進む。手順3〜4で特定した関連activityのうち続きとなるものについて`get_goal(activity_id=<関連activityのid>)`を呼び、返ってきた`label`が`closed`でないこと(未判定であること)を確認したうえで`goal_id_raw`を控える。控えた値を使って`set_goal(activity_id=<手順7で作成したactivity_id>, goal={"goal_id": <goal_id_raw>})`で紐づける。

   - 会話でユーザーが真偽の付く終了条件を言っている、または言われていなくても会話の意図から推せる → `set_goal(activity_id, {"new": {"handle": ..., "statement": ..., "conditions": [...]}})`でそのまま書く。ユーザーの追認は待たず、書いたことの報告もしない。条件には、Claudeが自力で到達できるもの（`actor: "claude"`）を最低1本含める。到達の判定を人間に委ねる条件（`actor: "human"`）も、担い手がhumanになることを理由に確認を挟まず同様に書く
   - 終わり方の候補が複数あって1つに絞れない、またはこの活動には終わりがあるはずだが何なのか推せない → チャットでユーザーに聞き、定まれば同様に`set_goal`で書く。聞いても定まらなければ何も書かない
   - 常駐運用・同じセッションで閉じる小作業など、終わりの無い活動 → `set_goal(activity_id, {"waiver": "<理由>"})`
   - 雛形で埋めない。上のどれにも当たらない（まだ終わり方を考える判断材料が無いなど）場合は何も書かない。未定義のまま進めるのは正当な状態である
9. check-in結果をユーザーに伝える

## intent判定ガイドライン

ユーザーの意図からintentを判定する。**discussをデフォルトにしない**。語句の一致ではなく、ユーザーが何をしたいかの意図で判断する。

| intent | ユーザーの意図 | 例 |
|---|---|---|
| `investigate` | 情報を集めたい・理解したい | `/as 検索精度の現状を調べる` → 即作成 |
| `debug` | 問題の原因を突き止めたい | `/as searchが空配列を返すバグ` → 即作成 |
| `implement` | 具体的に作る・変える | `/as searchにrecency boost追加` → 即作成 |
| `design` | How/Interface/Edge casesを決めたい | `/as 取り消しプロトコルのDB設計` → 即作成 |
| `discuss` | 方針・要件の曖昧さを解消したい | `/as LLMの評価方法を考えたい` → 確認して作成 |
| `document` | ドキュメントを書く・更新する | `/as README更新` → 即作成 |
| `review` | コードの差分を確認する | `/as PR#300レビュー` → 即作成 |

intentに幅がある場合や不明瞭な場合は、ステップ2・6で確認する。
