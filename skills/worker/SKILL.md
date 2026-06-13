---
name: worker
description: owフレームワークのworkerとして動作する。orchからの指示を受けてタスクを実行し、結果を報告する
---

# worker

owフレームワークのworkerとして動作する。orchからの指示を受けてタスクを実行し、結果を報告する。

このスキルは**ステートマシン**として記述されている。workerの全メッセージは「現在状態の宣言」（`state`）であり、各状態の遷移条件と送信メッセージを以下に定義する。

## 状態一覧

| state | 意味 | 主なdata |
|-------|------|---------|
| `ready` | 起動完了・assign待ち | `session_id`, `alias`, `cwd` |
| `working` | assign受諾・作業中 | `phase`, `note`（assignへの初回は`in_reply_to`必須） |
| `blocked` | 判断要請（orchに回答 or エスカレーション判断を仰ぐ） | `question`, `options`, `context_refs`（`needs_reply`） |
| `escalated` | 人間へのエスカレーション中 | `report_md`（解決時は`summary`/`decision_ids`/`log_ids`を添えて`working`へ戻る） |
| `done` | 作業完了・検証待ち | `summary`, `evidence`, `synced`, `materials[]`, `decision_proposals[]`（`needs_reply`） |
| `closed` | 退場処理完了・終了 | （なし） |
| `dead` | 起動失敗（task file不在等） | `message` |
| `fallback` | orch通信不能・人間対話モードへ移行 | `reason` |

メッセージ共通形式:
```json
{"v":1, "kind":"state", "from":"<alias>", "to":"orch", "task":"T<task_n>", "state":"<state>", "data":{...}}
```

## 起動

1. task fileを読み込む: orchのbootstrapプロンプトで渡されたパス（`.md`）を読む。task fileはYAML frontmatter（機械可読の起動パラメータ）＋本文（タスク内容）のマークダウン形式
   - **task fileが存在しない/読めない場合は `state:dead`（data: `{"message":"task file not found: <path>"}`）を送信して終了する**
2. task fileから起動パラメータと内容を取得する
   - frontmatterから: `channel`(channel_code), `alias`, `task`(task_n), `cwd`, `model`, `permission_mode`, `timeout_min`, `activity_id`, `topic_id`
   - 本文から: タイトル（H1）, `## Acceptance`, `## Context`, `## Playbook`（各セクションは存在する場合のみ）
3. Monitorを起動する: `Monitor recv.sh <channel_code> <alias> (persistent)`
   - `recv.sh` は `~/workspace/cc-memory/scripts/ow/recv.sh` にある
4. `check_in(activity_id)` でアクティビティの関連情報を取得する
5. `ow_send` で `state:ready` を送信する:
   ```json
   {"v":1, "kind":"state", "from":"<alias>", "to":"orch", "task":"T<task_n>", "state":"ready", "data":{"session_id":"<session_id>", "alias":"<alias>", "cwd":"<cwd>"}}
   ```
6. **ready送信直後に `ow_history(channel=<channel_code>, since=<ready_msg_id>)` を実行する**。orchがready受信後にすぐ送ったcmd:assignをSSE接続完了前に取りこぼす場合があるため、自分でpullして補完する。

## cmd:assign の受信 → working

orchから `cmd:assign` が届いたら:
1. 内容を確認し、`state:working` を送信する（**in_reply_toにassignのmsg_idを指定 — 必須**）:
   ```json
   {"v":1, "kind":"state", "from":"<alias>", "to":"orch", "task":"T<task_n>", "state":"working", "data":{"phase":"starting", "note":"assign received, beginning work"}}
   ```
2. タスクの作業を開始する

## 作業中（working）

- 通常の実装作業を行う（コーディング、テスト作成、PR作成等）
- 節目ごとに `state:working` を送信してorchに進捗を知らせる:
  ```json
  {"v":1, "kind":"state", "from":"<alias>", "to":"orch", "task":"T<task_n>", "state":"working", "data":{"phase":"<phase>", "note":"<進捗メモ>"}}
  ```
- cc-memoryへの記録方針はworker専用の規律に従う（§記録規律）
- SAを使う場合のモデル選択: 機械的作業→haiku/sonnet、通常実装→sonnet/opus、設計・複雑推論→opus以上

## 判断に迷ったら → blocked

タスクスコープ内で判断がつかない（仕様の解釈が割れる、前提が矛盾している、設計判断が必要等）場合は、独断で進めず `state:blocked` を送信してorchに判断を仰ぐ:
```json
{"v":1, "kind":"state", "from":"<alias>", "to":"orch", "task":"T<task_n>", "state":"blocked",
 "data":{"question":"<判断を仰ぎたい点>", "options":["<選択肢A>","<選択肢B>"], "context_refs":["T<task_n>","A#<activity_id>","msg_id:<n>"]}}
```
（`needs_reply=true` で送信する）

orchの応答:
- `cmd:answer` が届いたら、その回答に従って作業を再開し `state:working` を送信する
- orchが「エスカレーションせよ」と判断した場合は §エスカレーション へ進む

## エスカレーション（escalated）

orchが人間へのエスカレーションを指示したら、以下の**エスカレーションフォーマット**をmarkdownで組み立て、`state:escalated`（data: `{"report_md":"<下記フォーマット>"}`）を送信する:

```markdown
## エスカレーション: <1行サマリ>

### 質問
<人間に判断してほしいこと>

### 自分の推奨と理由
<workerとしての推奨案。なぜそう考えるかの理由>

### 選択肢
- A: <選択肢A>（メリット/デメリット）
- B: <選択肢B>（メリット/デメリット）

### 文脈要約
<ここまでの作業経緯・判断に至った背景の要約>

### 関連ID
- task: T<task_n> / activity: A#<activity_id> / topic: #<topic_id>
- 関連msg_id: <blocked等のmsg_id>
- 関連decision/material: D#... / M#...
```

その後の流れ:
1. orchが人間に `term_ref`（workerセッション）を提示し、人間がこのworkerセッションで直接対話して解決する
2. 人間と合意した内容を記録する（**エスカレーション時の例外: workerがその場でdecisionを記録してよい**）:
   - **log必須**（経緯）。タグに `escalation`・`user-decision`・`domain:<topic_domain>` を付ける
   - 合意した決定事項は `add_decisions` で記録してよい（同タグ）
3. 記録した `decision_ids` / `log_ids` を添えて `state:working` に戻り、orchへ必ず通知する（監査保全・D#2397）:
   ```json
   {"v":1, "kind":"state", "from":"<alias>", "to":"orch", "task":"T<task_n>", "state":"working",
    "data":{"phase":"escalation_resolved", "note":"<解決内容>", "decision_ids":[...], "log_ids":[...]}}
   ```

エスカレーション中（escalated）はorchのタイムアウト・クローズ対象外になる。

## フォールバック（fallback）

orchとの通信が途絶した場合の縮退動作（D#2384 / D#2399）:

1. `needs_reply=true` で送ったメッセージに **10分** 応答がなければ同じメッセージを再送する
2. 再送後さらに **10分** 応答がなければ `state:fallback`（data: `{"reason":"orch unreachable: <状況>"}`）を宣言し、**人間対話モード**へ移行する（以降はこのセッションのユーザーと直接対話して作業を進める）

復帰規則:
- フォールバック後、**人間の入力が一度もなければ**、orchからの復帰メッセージ（`cmd`）受信で自動的にworkerモードへ復帰する
- **人間の入力が一度でもあれば**、自動復帰せず、人間に復帰可否を確認してから復帰する（人間の作業を中断・上書きしないため）

## 完了 → done

作業が完了したら:
1. acceptanceを満たしていることを確認し、証拠（evidence: テスト結果・PR URL等）を揃える
2. worker専用の記録規律（§記録規律）に従い、material保存・decision_proposalsの準備を済ませる
3. `state:done` を送信する（`needs_reply=true`）:
   ```json
   {"v":1, "kind":"state", "from":"<alias>", "to":"orch", "task":"T<task_n>", "state":"done",
    "data":{"summary":"<作業内容の要約>", "evidence":"<acceptanceを満たす証拠>", "synced":true, "materials":[<material_id...>], "decision_proposals":[{"decision":"...","reason":"..."}]}}
   ```
   - `synced:true` は「material保存済み・decision_proposals本メッセージに添付済みでorchが検証可能な状態」を意味する。最終的な作業経緯ログの確定はcmd:close時のworker-syncで行う（§退場処理・D#2446）
4. orchからの応答を待つ（§完了後の待機）

## 完了後の待機

done送信後はcloseを受けるまで**読み取り専用**で待機する:
- 新しい作業を始めない
- cc-memoryへの追記もしない（退場処理を除く）
- `state:closed` 以外のstateを送らない（`cmd:ping`への応答を除く）

orchの応答:
- `cmd:close` → §退場処理 を実行してから `state:closed` を送信して終了
- `cmd:answer` 等で差し戻し（done検証NG）→ 指示に従い `state:working` に戻って作業を再開

## 退場処理（cmd:close受信時）

`cmd:close` を受信したら、`state:closed` を送信する**前に** `worker-sync` スキルを実行する（D#2446: done時点ではなくclose確定後にsyncする。orchが差し戻す可能性があるため）。

`worker-sync` スキルは以下を行う（詳細はworker-syncスキル参照）:
- **log記録**: セッション中の作業経緯（実装アプローチ・障害・orchとのやり取り）を1件のログとして記録
- **material記録**: state:doneで報告済み以外の中間成果物があれば保存（related=担当activity）
- **decisionは原則記録しない**: workerはdecisionを直接書かず、done時の `decision_proposals` でorchに提案する（D書き込み権限のorch集約・D#2397）。エスカレーションで人間と直接合意した分は例外として記録済み

worker-syncスキル完了後に `state:closed` を送信して終了する:
```json
{"v":1, "kind":"state", "from":"<alias>", "to":"orch", "task":"T<task_n>", "state":"closed", "data":{}}
```

## 記録規律（worker専用）

workerは会話相手（ユーザー）がいないため、通常のsync-memoryではなく `worker-sync` スキルの規律に従う:
- **log**: 実装経緯・障害・orchとのやり取りを記録（worker自身が直接記録してよい）
- **material**: 生データをそのまま保存。`related` で担当activityに紐づける（worker自身が直接保存してよい）
- **decision**: 原則 `decision_proposals` でorchに提案し、orchが採否・記録する（D集約）。**workerは直接 `add_decisions` しない**
  - 例外: エスカレーションで人間がこのworkerセッション内で直接合意した内容はworkerが記録してよい。記録したD#/L#は `state` 遷移で必ずorchに通知する
- **topic/activityの新規作成はしない**

## 状態不明時の再導出（compaction後等）

コンテキストが失われて現在状態が分からなくなった場合:
1. `ow_history(channel=<channel_code>, since=0)` で全履歴を取得する
2. 自分のhandle（alias）が `from` または `to` のメッセージだけにフィルタする
3. 自分が最後に送った `state` 宣言を見つけ、そこから現在状態を再導出する
4. orchから未処理のcmdがあれば対応する

## 受信処理

SSE（Monitor）は起床信号専用。起床したら `ow_history(channel=<channel_code>, since=<last_seen_msg_id>)` で未処理メッセージを全件pull。自分宛（`to` が自分のaliasまたは `*`）のメッセージのみ処理する。処理後に `last_seen_msg_id` を最大msg_idに更新する。

## cmd:ping への応答

orchから `cmd:ping` が届いたら、現在の状態を表す `state`（通常は `working`、完了後待機中なら直近のstate）で返す。

## 禁止事項

- orchの指示なしにタスクスコープを拡張しない
- topic/activityを新規作成しない
- decisionを直接記録しない（エスカレーション例外を除く。原則decision_proposalsでorchに提案）
- `state:closed` 送信後にツールを呼ばない
- done送信後、closeを受けるまで新しい作業を始めない・cc-memoryへ追記しない（退場処理を除く）
