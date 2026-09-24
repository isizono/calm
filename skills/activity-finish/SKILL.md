---
name: activity-finish
description: 【必須】アクティビティを完了にする。「/af」「/activity-finish」「アクティビティ終わり」「この作業完了」「クローズして」など、現在のアクティビティを終了・完了させる意図で発動する。このスキルを経由せずにupdate_activity(status="completed")を直接呼んではいけない。
---

# activity-finish

現在のアクティビティを完了にする。

## 手順

1. **対象の特定**: 「現在のアクティビティ」を以下の優先順位で特定する
   - このセッション内でcheck-in・作成したactivityを対象にする
   - 候補が複数ある場合は**1ターンだけ**どれを完了にするか確認する
   - セッション内に該当がなければ `get_activities(limit=15)` で一覧を提示し選んでもらう（`limit` を省略するとデフォルト5件になり対象が一覧から漏れうるため明示する。タイトルベースで提示し、内部ID・数値は表示しない）
2. **終了条件（goal）の確認**: 手元に今のgoalブロックが無ければ `get_goal(activity_id=...)` で読み直し、`label` を見る（`get_goal`は`check_in`と違いactivityのstatusを変えない読み取り専用なので、statusを書き換えずに確認できる）。以下の `goal_id` は`get_goal`応答の `goal_id_raw` を指す
3. `label` に応じて完了させる
   - `undefined`（goal無し）: `update_activity(status="completed", closed_by="user", closed_reason=...)`。事後にgoalを促すことはしない。ただし、ユーザーがその場で「何をもって終わったか」を条件として言ったときだけ、`set_goal(activity_id, goal={"new": {...条件はstate="satisfied"か理由付きwaivedで作成...}})` で全条件を終端にして作り、続けて `judge_goal` で判定する（`update_activity` は呼ばない。activityの完了はjudge_goal側で行われる）
   - `not_needed`（不要印）: `update_activity(status="completed", closed_by="user", closed_reason=...)`。不要印は「この activity には終了条件を置かない」という明示の印なので、事後にgoalを作る例外は適用しない
   - `active`（openの条件が残る）: 1ターンだけ確認し、ユーザーの選択に応じて次のいずれかにする
     - 残りの条件を理由付きでwaivedにしてから達成扱いにする: `update_goal(goal_id, changes=[{"op":"set","id":<条件id>,"state":"waived","note":理由}, ...])` → `judge_goal(goal_id, verdict="achieved", judged_by="human")`（waivedにするとsatisfiedが0件になる場合はこの選択肢を示さない。全条件のidが要るときは `get_goal(activity_id=...)` で確認する）
     - `judge_goal(goal_id, verdict="failed", note=理由, judged_by="human")` で閉じる
     - 同じgoalに未完了の兄弟activityが残るなら、このactivityだけ `update_activity(status="completed", closed_by="user")`（goalは未判定のまま残る）
   - `judge_ready`（判定待ち）: 手元のgoalブロック（またはget_goalの応答）の`open_questions`に未決があれば畳むか1ターン聞く。そのうえで条件のうちsatisfiedが1件以上あれば `judge_goal(goal_id, verdict="achieved")`、0件なら `judge_goal(goal_id, verdict="failed", note=理由)` か条件を足す。`judged_by` は、goalに紐づく未完了activityがこの1件だけなら `"human"`（/afの起動自体をgoal全体の完了明言とみなす）、他にも未完了activityがあれば `"session"`
   - `closed`（判定済み）: `update_activity(status="completed")` だけを呼ぶ。`closed_by` は渡さない（サーバーが `closed_by="goal_judge"` を自動で書く）
4. **完了記録の追記**: `description` の末尾に完了記録を追記する。日付に加えて、会話から自明な範囲で「何がどうなって終わったか」を一行添える（例: `\n\n## 完了\nYYYY-MM-DD ○○を実装しPRを作成して完了`）。自明でない場合はユーザーへの質問はせず日付のみ記載する
   - `update_activity(status="completed", ...)` で閉じた場合は、そのまま同じ呼び出しに `description` を含めてよい
   - `judge_goal` で閉じた場合は、閉じた後に `status` を渡さない `update_activity(activity_id, description=...)` で追記する（既に `completed` のactivityへ再度 `status="completed"` を渡しても、サーバー側のガードにより閉じ方の記録は書き換わらない仕様のため、statusは省略すれば足りる）
   - ログは残さない（軽量にサクッと終わらせる）
5. 完了したことをユーザーに一言伝える。goalを判定した場合は判定したこと（達成／未達）も一言添える
   - 閉じたアクティビティが `[議論]` または `[設計]` で、次フェーズ（`[設計]` / `[作業]`）が自然に続く内容の場合、「次のactivityを作るか」を一言だけ提案する（勝手に作成はしない）
   - 承認された場合、`add_activity` を直接呼ばず [activity-start](../activity-start/SKILL.md) skillに委譲する（重複チェック・related候補特定・`IMPLEMENT_WORKFLOW_GUARD`先回りをスキップしないため）

## 注意

- `/af`が呼ばれたこと自体が「終わった」の根拠。goal無し・不要印のactivityでは、エージェント側で「本当に終わった？」等の判定ロジックは入れない
- goal付きのactivityでも完了は拒否されない。判定を挟むのは上記の分岐に従うときだけで、goalが未判定のまま `update_activity(completed)` を呼んでも通る（応答に付く `goal_hint` は知らせであり、拒否ではない）
- sync-memory的な記録の棚卸しはしない。あくまで軽量な完了操作
