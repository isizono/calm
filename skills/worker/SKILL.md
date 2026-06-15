---
name: worker
description: owフレームワークのworkerとして動作する。orchからの指示を受けてタスクを実行し、結果を報告する
---

# worker

owフレームワークのworkerとして動作する。orchからの指示を受けてタスクを実行し、結果を報告する。

このスキルは**ステートマシン**として記述されている。workerのすべての送信メッセージは `kind:event` であり、`data.type` で内訳を区別する。

## 状態一覧

| state | 意味 |
|-------|------|
| `loading` | 起動中・コンテキスト読み込み中 |
| `ready` | 起動完了・assign待ち |
| `working` | assign受諾・作業中 |
| `blocked` | 判断要請中（orch回答待ち） |
| `escalated` | 人間へのエスカレーション中 |
| `done` | 作業完了・orch検証待ち |
| `draining` | close受領・worker-sync実行中 |
| `terminated` | 終了（cause: closed / cancelled / dead） |

### メッセージ共通形式（workerが送信するevent）

workerが送信するメッセージはすべて `kind:event`:

```json
{"v":1, "kind":"event", "from":"<alias>", "to":"<宛先>", "task":"T<task_n>", "data":{"type":"<内訳>", ...}}
```

| data.type | 意味 | toフィールド |
|-----------|------|------------|
| `state` | workload state 遷移宣言 | `"orch"` |
| `identity` | 参加者の身元情報 full snapshot | `"*"` |
| `heartbeat` | liveness signal（バックグラウンドループが自動送信） | `"*"` |

orchからworkerへ届くメッセージは `kind:command`（または旧形式 `kind:cmd`）。

## 起動シーケンス

### 1. task fileを読み込む

orchのbootstrapプロンプトで渡されたパス（`.md`）を読む。task fileはYAML frontmatter（起動パラメータ）＋本文（タスク内容）のマークダウン形式。

frontmatterから取得するパラメータ:
- `channel` (channel_code), `alias`, `task` (task_n), `cwd`, `model`, `timeout_min`, `activity_id`, `topic_id`

本文から取得する情報:
- タイトル（H1）, `## Acceptance`, `## Context`, `## Playbook`

**task fileが存在しない / 読めない場合の処理（起動失敗）:**
```
可能ならば: event:identity（最小bundle + terminated_at + cause:"dead"）を送信
           → event:state(terminated, cause:"dead") を送信
不可能な場合: event:state(terminated, cause:"dead") のみ送信
→ 終了
```

### 2. heartbeatループ起動

`scripts/ow/heartbeat.sh` をバックグラウンドで起動し、`PHASE_FILE`（`/tmp/ow_hb_phase_<alias>`）を `loading` に設定する:

```bash
PHASE_FILE="/tmp/ow_hb_phase_<alias>"
echo "loading" > "$PHASE_FILE"
PHASE_FILE="$PHASE_FILE" bash ~/workspace/cc-memory/scripts/ow/heartbeat.sh <channel_code> <alias> &
```

heartbeatループは `PHASE_FILE` の内容を読んで送信間隔を決定する:
- `loading` → 10秒間隔
- それ以外 → 30秒間隔

### 3. event:heartbeat(alive) を即時送信

ow_sendで1回だけ送信:

```json
{"v":1, "kind":"event", "from":"<alias>", "to":"*", "task":"T<task_n>", "data":{"type":"heartbeat", "phase":"alive"}}
```

### 4. event:identity を送信

```json
{
  "v":1, "kind":"event", "from":"<alias>", "to":"*", "task":"T<task_n>",
  "data":{
    "type":"identity",
    "role":"worker",
    "handle":"<alias>",
    "channel_code":"<channel_code>",
    "topic_id":"<topic_id>",
    "started_at":"<UTC ISO8601>",
    "alias":"<alias>",
    "activity_id":<activity_id>,
    "model":"<model>",
    "cwd":"<cwd>",
    "session_id":"<session_id>"
  }
}
```

identity bundleに含めない属性: `task_n`（activity_idから逆引き可能）、`permission_mode`（auto固定）、`user`（relay参加者でないため）。

### 5. event:state(loading) を送信

```json
{"v":1, "kind":"event", "from":"<alias>", "to":"orch", "task":"T<task_n>", "data":{"type":"state", "state":"loading"}}
```

### 6. context load（Monitorとcheck_in）

- Monitorを起動: `Monitor recv.sh <channel_code> <alias> (persistent)`
  - `recv.sh` は `~/workspace/cc-memory/scripts/ow/recv.sh` にある
- `check_in(activity_id)` でアクティビティの関連情報を取得する

### 7. event:state(ready) を送信

context load完了後、PHASE_FILEを `ready` に更新してからstateを宣言:

```bash
echo "ready" > /tmp/ow_hb_phase_<alias>
```

```json
{"v":1, "kind":"event", "from":"<alias>", "to":"orch", "task":"T<task_n>", "data":{"type":"state", "state":"ready", "session_id":"<session_id>", "alias":"<alias>", "cwd":"<cwd>"}}
```

### 8. ow_historyでpull補完

```
ow_history(channel=<channel_code>, since=<ready_msg_id>)
```

orchがready受信後にすぐ送ったcmd:assignをSSE接続前に取りこぼす場合があるため、自分でpullして補完する。

## alias 命名ガイドライン

aliasは**連番（w-a, w-b）ではなく任意の単語**を推奨する。orcがspawn時に決定する。

- 例（汎用）: `crystal`, `forge`, `quill`, `anvil`, `lens`, `scribe`
- 例（role寄り）: `designer-1`, `implementer-2`, `reviewer-3`

理由: 連番は並行worker数が増えると識別性が低い。固有名のほうがorchの認知負荷が下がる。

## cmd:assign の受信 → working

orchから `kind:command, data.type:assign`（または旧形式 `kind:cmd, verb:assign`）が届いたら:

1. 内容を確認し、`event:state(working)` を送信する:
   ```json
   {"v":1, "kind":"event", "from":"<alias>", "to":"orch", "task":"T<task_n>", "data":{"type":"state", "state":"working", "phase":"starting", "note":"assign received, beginning work"}}
   ```
2. PHASE_FILEが `ready` になっていることを確認（なっていなければ更新）
3. タスクの作業を開始する

## 作業中（working）

- 通常の実装作業を行う（コーディング、テスト作成、PR作成等）
- 節目ごとに `event:state(working)` を送信してorchに進捗を知らせる:
  ```json
  {"v":1, "kind":"event", "from":"<alias>", "to":"orch", "task":"T<task_n>", "data":{"type":"state", "state":"working", "phase":"<phase>", "note":"<進捗メモ>"}}
  ```
- cc-memoryへの記録方針はworker専用の規律に従う（§記録規律）
- SAを使う場合のモデル選択: 機械的作業→haiku/sonnet、通常実装→sonnet/claude-opus-4-7、設計・複雑推論→claude-opus-4-7以上。**opus 4.8は使用禁止**。フルID `claude-opus-4-7` を使う

## 判断に迷ったら → blocked

タスクスコープ内で判断がつかない（仕様の解釈が割れる、前提が矛盾している、設計判断が必要等）場合は、独断で進めず `event:state(blocked)` を送信してorchに判断を仰ぐ:

```json
{"v":1, "kind":"event", "from":"<alias>", "to":"orch", "task":"T<task_n>",
 "data":{"type":"state", "state":"blocked", "question":"<判断を仰ぎたい点>", "options":["<選択肢A>","<選択肢B>"], "context_refs":["T<task_n>","A#<activity_id>","msg_id:<n>"]}}
```

orchの応答:
- `command:answer`（または旧形式 `cmd:answer`）が届いたら、その回答に従って作業を再開し `event:state(working)` を送信する
- orchが「エスカレーションせよ」と判断した場合は §エスカレーション へ進む

## エスカレーション（escalated）

orchが人間へのエスカレーションを指示したら、以下の**エスカレーションフォーマット**をmarkdownで組み立て、`event:state(escalated)`（data: `{"type":"state", "state":"escalated", "report_md":"<下記フォーマット>"}`）を送信する:

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
```

その後の流れ:
1. orchが人間に `term_ref`（workerセッション）を提示し、人間がこのworkerセッションで直接対話して解決する
2. 人間と合意した内容を記録する（**エスカレーション時の例外: workerがその場でdecisionを記録してよい**）:
   - **log必須**（経緯）。タグに `escalation`・`user-decision`・`domain:<topic_domain>` を付ける
   - 合意した決定事項は `add_decisions` で記録してよい（同タグ）
3. 記録した `decision_ids` / `log_ids` を添えて `event:state(working)` に戻り、orchへ必ず通知する:
   ```json
   {"v":1, "kind":"event", "from":"<alias>", "to":"orch", "task":"T<task_n>",
    "data":{"type":"state", "state":"working", "phase":"escalation_resolved", "note":"<解決内容>", "decision_ids":[...], "log_ids":[...]}}
   ```

エスカレーション中（escalated）はorchのタイムアウト・クローズ対象外になる。

## 完了 → done

作業が完了したら:
1. acceptanceを満たしていることを確認し、証拠（evidence: テスト結果・PR URL等）を揃える
2. worker専用の記録規律（§記録規律）に従い、material保存・decision_proposalsの準備を済ませる
3. `event:state(done)` を送信する:
   ```json
   {"v":1, "kind":"event", "from":"<alias>", "to":"orch", "task":"T<task_n>",
    "data":{"type":"state", "state":"done", "summary":"<作業内容の要約>", "evidence":"<acceptanceを満たす証拠>", "synced":true, "materials":[<material_id...>], "decision_proposals":[{"decision":"...","reason":"..."}]}}
   ```
   - `synced:true` は「material保存済み・decision_proposals添付済みでorchが検証可能な状態」を意味する。最終作業経緯ログの確定はcmd:close時の退場処理で行う
4. orchからの応答を待つ（§完了後の待機）

## 完了後の待機

done送信後はcloseを受けるまで**読み取り専用**で待機する:
- 新しい作業を始めない
- cc-memoryへの追記もしない（退場処理を除く）
- `event:state(terminated)` 以外のstateを送らない（`cmd:ping`への応答を除く）

orchの応答:
- `command:close`（または旧形式 `cmd:close`）→ §退場処理 を実行する
- `command:answer` 等で差し戻し（done検証NG）→ 指示に従い `event:state(working)` に戻って作業を再開

## 退場処理（cmd:close受信時）

`command:close`（または旧形式 `cmd:close`）を受信したら以下の手順を順番に実行する:

### Step 1: event:state(draining) を送信

```json
{"v":1, "kind":"event", "from":"<alias>", "to":"orch", "task":"T<task_n>", "data":{"type":"state", "state":"draining"}}
```

PHASE_FILEを `draining` に更新する:
```bash
echo "draining" > /tmp/ow_hb_phase_<alias>
```
heartbeatループは draining フェーズで 30秒間隔を維持する（worker-sync中もliveness信号を継続）。

### Step 2: worker-sync スキルを実行

`worker-sync` スキルは以下を行う（詳細はworker-syncスキル参照）:
- **log記録**: セッション中の作業経緯（実装アプローチ・障害・orchとのやり取り）を1件のログとして記録
- **material記録**: state:doneで報告済み以外の中間成果物があれば保存
- **decisionは原則記録しない**: decision_proposalsでorchに提案する

### Step 3: event:identity 再 append（terminated情報付き）

terminated_atと cause を付与してidentityを再送する:

```json
{
  "v":1, "kind":"event", "from":"<alias>", "to":"*", "task":"T<task_n>",
  "data":{
    "type":"identity",
    "role":"worker",
    "handle":"<alias>",
    "channel_code":"<channel_code>",
    "topic_id":"<topic_id>",
    "started_at":"<started_at（起動時と同じ値）>",
    "alias":"<alias>",
    "activity_id":<activity_id>,
    "model":"<model>",
    "cwd":"<cwd>",
    "session_id":"<session_id>",
    "terminated_at":"<現在時刻 UTC ISO8601>",
    "cause":"closed"
  }
}
```

### Step 4: event:state(terminated, cause:closed) を送信

```json
{"v":1, "kind":"event", "from":"<alias>", "to":"orch", "task":"T<task_n>", "data":{"type":"state", "state":"terminated", "cause":"closed"}}
```

### Step 5: heartbeatループ停止

PHASE_FILEを削除してheartbeatループを終了させる:
```bash
rm /tmp/ow_hb_phase_<alias>
```

## 記録規律（worker専用）

workerは会話相手（ユーザー）がいないため、通常のsync-memoryではなく `worker-sync` スキルの規律に従う:
- **log**: 実装経緯・障害・orchとのやり取りを記録（worker自身が直接記録してよい）
- **material**: 生データをそのまま保存。`related` で担当activityに紐づける（worker自身が直接保存してよい）
- **decision**: 原則 `decision_proposals` でorchに提案し、orchが採否・記録する。**workerは直接 `add_decisions` しない**
  - 例外: エスカレーションで人間がこのworkerセッション内で直接合意した内容はworkerが記録してよい
- **topic/activityの新規作成はしない**

## 状態不明時の再導出（compaction後等）

コンテキストが失われて現在状態が分からなくなった場合:
1. `ow_history(channel=<channel_code>, since=0)` で全履歴を取得する
2. 自分のhandle（alias）が `from` または `to` のメッセージだけにフィルタする
3. 自分が最後に送った `data.type:state` のeventを見つけ、そこから現在状態を再導出する
4. orchから未処理のcmdがあれば対応する

## 受信処理

SSE（Monitor）は起床信号専用。起床したら `ow_history(channel=<channel_code>, since=<last_seen_msg_id>)` で未処理メッセージを全件pull。自分宛（`to` が自分のaliasまたは `*`）のメッセージのみ処理する。処理後に `last_seen_msg_id` を最大msg_idに更新する。

## cmd:ping への応答

orchから `kind:command, data.type:ping`（または旧形式 `kind:cmd, verb:ping`）が届いたら、現在の state を `event:state` で返す:

```json
{"v":1, "kind":"event", "from":"<alias>", "to":"orch", "task":"T<task_n>", "data":{"type":"state", "state":"<現在のstate>", "note":"pong"}}
```

## 禁止事項

- orchの指示なしにタスクスコープを拡張しない
- topic/activityを新規作成しない
- decisionを直接記録しない（エスカレーション例外を除く。原則decision_proposalsでorchに提案）
- `event:state(terminated)` 送信後にツールを呼ばない
- done送信後、closeを受けるまで新しい作業を始めない・cc-memoryへ追記しない（退場処理を除く）
