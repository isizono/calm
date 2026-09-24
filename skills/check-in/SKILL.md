---
name: check-in
description: アクティビティにcheck-inして関連情報を集約取得する。「/check-in」「チェックイン」「続きやる」「再開しよう」「前回の続き」「どこまでやったっけ」など、既存アクティビティの作業を再開する意図で発動する。新しい作業を始めるとき（activity-start）や、期間横断の振り返りには発動しない。
---

# check-in

指定されたアクティビティに対して `check_in` ツールを呼び出し、関連情報を調べてアクティビティの全体像と進捗を把握してください。check-in後にユーザーが「やって」と言えばすぐ作業・議論を開始できる状態にすることがゴールです。

## 手順

1. 引数で `activity_id` が指定されていればそのまま使う
2. 指定されていなければ、SessionStart hookが注入済みのアクティビティ一覧（`# アクティビティ一覧` — 「作業中（別セッション）」「優先」の階層構成で、行は `{タイトル} (#NNN)` の形式）を使う
   a. この一覧はhookが決定論的に組み立てた表示用markdownであり、hook自身が「再フォーマットや優先順の再評価をせず、必要時はそのまま提示してください」と指示している。再構成・要約し直さずそのまま提示する。ここに現れる`(#NNN)`はタイトルに併記される形式であり、hookの指示に従いそのまま表示してよい
   b. 選びたいアクティビティがこの一覧に無い（末尾の「未表示のアクティビティN件」に該当する、またはこのセッションでhookの出力を持っていない）場合は `get_activities()` でフラットに取得しフォールバック表示する。この場合はトピック名・アクティビティ名のみを提示し、IDは表示しない
   c. ユーザーが名前やキーワードで選択したら、対応するactivity_idでステップ3へ進む
3. `check_in(activity_id=...)` を呼び出す
4. `get_logs`・`get_decisions`・`search` などで関連情報を取得し、概要と進捗を把握する
5. check-in結果に含まれるタグ一覧を見て、明らかな表記揺れや重複に気づいたらユーザーにサジェストする。分析ツール（`analyze_tags`）は呼ばない
6. 把握した内容を以下の2セクション構成でユーザーに伝える

check-in結果は5つの枠（`anchor`/`control`/`context`/`catalog`/`env`）に分かれている。フィールドの正確なパス・出力の形はcheck_inツールのdocstringを正本とし、以下は扱い方の説明に留める。

## anchor.pinnedフィールドの扱い

check-in結果に `anchor.pinned` フィールドがある場合、その内容はタスクに常に意識してほしい情報としてpinされたエンティティ群である。以下の5種が含まれることがある（0件キーは省略される）:

- `anchor.pinned.decisions`: 重要な決定事項（id, title, reason付き）
- `anchor.pinned.logs`: 重要な議事録・ログ（id, title, content付き）
- `anchor.pinned.materials`: 重要な参考資材（id, title, content, source付き）
- `anchor.pinned.topics`: 関連トピック（id, title）
- `anchor.pinned.activities`: 関連アクティビティ（id, title, status）

pinned情報は進捗把握の最初に確認し、概要・進捗の説明に反映すること。

## truncatedフィールドの扱い

check-in結果に `truncated` フィールドがある場合、応答が全体予算を超えたため一部セクションが末尾やスタブへ切り詰められている。`cuts` の各要素の `section` はドット区切りの入れ子パス（例: `anchor.pinned`、`catalog.map`）で、`next`（`{tool, args}` の形の続きへのポインタ）が付いていれば、そのツールをそのまま呼べば削られた分の本文を取り直せる。`over_budget: true` のときは全部を切り詰めてもなお超過している状態だが、それ自体はエラーではないので、必要な情報が足りないと感じた箇所だけ `next` で追加取得すればよい。

## env.hintsフィールドの扱い

check-in結果に `env.hints` フィールド（文字列リスト）がある場合、それはタグに蓄積したdecisionの整理（recompose-context skill）をおすすめしたい状況を示すナッジである。出力の最後に、各hintの内容を「〜をおすすめします」程度の一言として添えるに留めること。check-inの目的（現在のアクティビティの進捗把握）を差し置いてrecomposeに着手してはならない。

## control.asksフィールドの扱い

check-in結果に `control.asks` フィールドがある場合、対象アクティビティをblockしているaskが1件以上ある。`awaiting_answer`は回答待ち、`awaiting_triage`は回答済みだが未トリアージのask一覧である。`awaiting_triage`が1件以上あれば、`triage_ask`でpromote/dismissへ振り分けること。`more`と`next`が付いていれば、件数分はまだ未取得なので`next`のツール呼び出しで残りを取得できる。

## env.sessionフィールドの扱い

`env.session.alias_collision`が`true`のときは、セッション別名が他セッションと衝突して付け替わった旨をユーザーに伝えること。

## control.goalフィールドの扱い

check-in結果に `control.goal` フィールドがある場合、対象アクティビティの終了条件と次にやるべきこと1件（`next`）がまとまっている。`goal.error`が入っていれば組み立てに失敗しただけなので無視してよい。`label`に応じて次のとおり動く。

- `label: judge_ready`（判定待ち）: `open_questions`に未決があれば畳むかユーザーに1ターン確認する。そのうえで、`terminal`の充足が1件以上あれば`judge_goal(goal_id, verdict="achieved", note=...)`、0件なら`judge_goal(goal_id, verdict="failed", note=...)`をその場で呼ぶ。人間の判断は待たず、判定したことの報告もしない
- `label: undefined`（未定義）: `next`の文面どおり、真偽の付く終了条件が会話にある、または意図から推せるなら`set_goal`で書く。ユーザーの追認は待たず、書いたことの報告もしない。候補が複数で定まらない、またはこの活動には終わりがあるはずだが何なのか推せないときだけユーザーに聞き、定まれば同様に書く。聞いても定まらない、またはそもそも判断材料が無いときは何もしない
- `next.rule`が`13`（外部待ちの再確認。確かめる主体はClaudeなので`next.actor`は常に`claude`になる）: `next.what`が示す条件を、ghなど実際の手段で確かめる。確認できたら該当条件を`update_goal`で`satisfied`にし、確かめた事実は`add_material`か`add_logs`に残す。確認できなければ確認済みである旨だけを`note`に残してopenのまま待つ
- `next.rule`が`14`（待ち。まだ再確認の時期に達していない）: 会話や記録から満たされたと分かれば、確認や報告なしに`update_goal`で`satisfied`に書く。分からず`next.actor`が`human`のときは、その場で聞ければ聞いて決定事項にする（回答が得られればdecision-recordの通常の流れで記録される）。聞けない、または`next.actor`が`external`なら、無理に確かめようとせず`next.what`をユーザーに一言伝えて待つ
- `label: closed`（判定済み）: `last_verdict`をユーザーに伝え、残作業が無ければ`update_activity(status="completed")`で閉じ直す（`closed_by`は渡さない）
- 上記以外（`next.actor`が`claude`など）: `next.what`が示す作業にそのまま取りかかる

`label: judge_ready`時の判定分岐（`open_questions`の扱いから`judge_goal`の呼び分けまで）は、[recording](../recording/SKILL.md)の「記録の直後にgoalの条件を満たす場合」節・[decision-record](../decision-record/SKILL.md)の「決定記録の直後にgoalの条件を満たす場合」節からも同じロジックとして参照される正本である。判定条件を変更する場合はこの節を更新の起点とし、他の2箇所の記述も同じ内容に揃える。

## 出力フォーマット

```
check-in: {anchor.activity.title}

## 概要
{タスクの背景・目的・やることがユーザーに伝わる程度にまとめる。anchor.activity.descriptionと関連情報をもとに構成する}

## 進捗
intent: {タグから抽出した intent 値、なければ省略}
{logs・decisions・materialsなどから読み取れる、実際にどこまで進んでいるか・何が残っているかの要約}
```
