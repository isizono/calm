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
| `loading` | 起動中・spawn-bundle pull / context load 中 |
| `working` | 作業中 (loading → working 直行、ready 状態は廃止) |
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
| `state` | workload state 遷移宣言 | `"dispatcher"` (過渡期エイリアスとして `"orch"` も relay 側で吸収) |
| `identity` | 参加者の身元情報 full snapshot | `"*"` |
| `heartbeat` | liveness signal（バックグラウンドループが自動送信） | `"*"` |

dispatcher (旧 orch handle) からworkerへ届くメッセージは `kind:command`。新 channel では handle prefix `d-*` (dispatcher) を使い、既存 channel では `orch` handle が dispatcher エイリアスとして当面維持される。

## 起動シーケンス

worker は ow_spawn_worker (`OW_ROLE=worker` 経由) で起動される。起動 prompt は `/goal <goal_text>。workerスキルに従って check_in からぜんぶやって。` の形式で、claude CLI ネイティブの `/goal` 自走モードでゴール完了 (`event:state(done)`) まで動き続ける。

### 1. env 読み込み

以下の環境変数から worker bootstrap 識別子を取得する:

- `OW_CHANNEL`: 自分が参加する channel code
- `OW_ALIAS`: 自分の handle (alias)
- `OW_TASK_N`: タスク番号 (Tn)

これらが未設定 / 空のいずれかなら起動失敗扱い。relay 接続できないため envelope 送信もできず、exit code 非ゼロで即終了する。

### 2. heartbeat ループ起動

`scripts/ow/heartbeat.sh` をバックグラウンドで起動し、`PHASE_FILE`（`/tmp/ow_hb_phase_<alias>`）を `loading` に設定する:

```bash
PHASE_FILE="/tmp/ow_hb_phase_$OW_ALIAS"
echo "loading" > "$PHASE_FILE"
PHASE_FILE="$PHASE_FILE" bash ~/workspace/cc-memory/scripts/ow/heartbeat.sh "$OW_CHANNEL" "$OW_ALIAS" &
```

heartbeat ループは `PHASE_FILE` の内容を読んで送信間隔を決定する:
- `loading` → 10秒間隔
- それ以外 (`working` / `draining` 等) → 30秒間隔

### 3. event:heartbeat(alive) を即時送信

ow_send で1回だけ送信:

```json
{"v":1, "kind":"event", "from":"<alias>", "to":"*", "task":"T<task_n>", "data":{"type":"heartbeat", "phase":"alive"}}
```

### 4. event:identity を送信（最小フィールド）

bundle 受信前のため bundle 由来の `task_title` / `activity_id` / `topic_id` は null で送る。Step 9 で bundle 受信後にフル identity を再送する。

```json
{
  "v":1, "kind":"event", "from":"<alias>", "to":"*", "task":"T<task_n>",
  "data":{
    "type":"identity",
    "role":"worker",
    "handle":"<alias>",
    "channel_code":"<channel_code>",
    "topic_id": null,
    "started_at":"<UTC ISO8601>",
    "alias":"<alias>",
    "activity_id": null,
    "model":"<model>",
    "cwd":"<cwd>",
    "session_id":"<session_id>"
  }
}
```

identity から **除外する属性**: `task_n`（activity_id から逆引き可能）、`user`（relay 非参加者）。`term_ref` は SessionStart hook が env (`TMUX_PANE`) から `~/.cc-memory/ow/term_refs/<session_id>.json` にキャッシュし、`ow_send` が identity event 送信時に session_id ベースで自動補完する（手動で payload に乗せる必要なし）。

### 5. event:state(loading) を送信

```json
{"v":1, "kind":"event", "from":"<alias>", "to":"dispatcher", "task":"T<task_n>", "data":{"type":"state", "state":"loading"}}
```

### 6. Monitor 起動

- `Monitor recv.sh <channel_code> <alias> (persistent)`
- `recv.sh` は `~/workspace/cc-memory/scripts/ow/recv.sh` にある

### 7. spawn-bundle pull

`ow_history(channel=OW_CHANNEL, since=0)` で channel 全 messages を pull する。pull 結果から以下の条件をすべて満たす envelope を filter する:

- `body.to == OW_ALIAS` または `body.to == "*"`
- `body.task == "T<OW_TASK_N>"`
- `body.data.type == "spawn-bundle"`

複数該当する場合は msg_id 最大の 1 件を採用する。bundle data から以下を取得する:

- `task_title`: タスクのタイトル
- `acceptance`: 完了条件
- `context`: タスクコンテキスト (思考 worker の場合、末尾に `ultrathink` マーカーセクションが含まれる)
- `playbook`: プレイブック抜粋
- `activity_id`: アクティビティID
- `topic_id`: トピックID
- `effort`: 思考 worker effort (None または `high`/`xhigh`/`max`/`ultrathink`)
- `goal_text`: `/goal` 起動 prompt に埋められた短文 (claude が起動時点で自走モードのトリガーに使用済み、本文 context として参照可)

**失敗時挙動:**

- **relay 接続失敗** (HTTP error / curl timeout): 1 秒待ち + 最大 3 回 retry
- 3 回連続失敗 → `event:state(terminated, cause:"dead", note:"relay unreachable at bundle pull")` を送信して終了
- **bundle 不在** (pull 成功だが該当 envelope なし): retry しない。spawn 設計上、bundle 送信「後」に worker 起動が固定されているため、bundle 不在 = spawn 側バグ。`event:state(terminated, cause:"dead", note:"spawn-bundle not found in channel")` で即終了

### 8. check_in

bundle から取得した activity_id で `check_in(activity_id)` を実行し、アクティビティの関連情報を取得する。activity_id が null の場合は check_in をスキップする (任意作業 worker の例外運用)。

### 9. event:identity 再送（フルフィールド）

bundle 由来フィールドを含めて identity を再 append する。reducer は最新を採用するため、Step 4 の最小 identity は上書きされる。

```json
{
  "v":1, "kind":"event", "from":"<alias>", "to":"*", "task":"T<task_n>",
  "data":{
    "type":"identity",
    "role":"worker",
    "handle":"<alias>",
    "channel_code":"<channel_code>",
    "topic_id":"<topic_id>",
    "started_at":"<起動時と同じ値>",
    "alias":"<alias>",
    "activity_id":<activity_id>,
    "model":"<model>",
    "cwd":"<cwd>",
    "session_id":"<session_id>",
    "task_title":"<bundle data.task_title>"
  }
}
```

### 10. event:state(working, phase="briefing") を送信

context load 完了で working 状態に直行する。ready 状態は廃止 (D#2962)。PHASE_FILE を `working` に更新してから state を宣言する:

```bash
echo "working" > /tmp/ow_hb_phase_<alias>
```

```json
{"v":1, "kind":"event", "from":"<alias>", "to":"dispatcher", "task":"T<task_n>",
 "data":{"type":"state", "state":"working", "phase":"briefing", "note":"bundle pulled, context loaded"}}
```

その後、§作業中（working） に進む。dispatcher / orch から軌道修正が必要な場合は `command:answer` で差し戻される (旧 `command:assign` 経路は廃止)。

## alias 命名ガイドライン

aliasは**連番（w-a, w-b）ではなく任意の単語**を推奨する。orcがspawn時に決定する。

- 例（汎用）: `crystal`, `forge`, `quill`, `anvil`, `lens`, `scribe`
- 例（role寄り）: `designer-1`, `implementer-2`, `reviewer-3`

理由: 連番は並行worker数が増えると識別性が低い。固有名のほうがorchの認知負荷が下がる。

## 作業中（working）

- 通常の実装作業を行う（コーディング、テスト作成、PR作成等）
- 節目ごとに `event:state(working)` を送信してorchに進捗を知らせる:
  ```json
  {"v":1, "kind":"event", "from":"<alias>", "to":"dispatcher", "task":"T<task_n>", "data":{"type":"state", "state":"working", "phase":"<phase>", "note":"<進捗メモ>"}}
  ```
- cc-memoryへの記録方針はworker専用の規律に従う（§記録規律）
- SAの活用については §SAの活用 参照

## SAの活用

workerは全部自分で調べきる必要はない。Agent/TaskツールによるSA（サブエージェント）を積極的に活用してよい。

### workerがSAを使う典型的な場面

- **コードベースの調査**: 実装前の既存コード把握、関連ファイルの特定
- **テスト・検証**: CI結果の解析、大量ログの要約
- **コードレビュー**: 実装後の品質チェック

### SAのモデル選択

**SAも `claude-opus-4-7` 一択**。用途による使い分けはしない（sonnet/haiku は禁止、opus 4.8 も禁止）。

### SAへの指示の書き方

- 明確なスコープと完了条件を渡す（何を調べてどう返すかを指定）
- 漠然と「調べて」ではなく、探す対象・返却形式・成功条件を具体的に書く
- 調査系には `subagent_type: "Explore"` を活用できる

## 判断に迷ったら → blocked

タスクスコープ内で判断がつかない（仕様の解釈が割れる、前提が矛盾している、設計判断が必要等）場合は、独断で進めず `event:state(blocked)` を送信してorchに判断を仰ぐ:

```json
{"v":1, "kind":"event", "from":"<alias>", "to":"dispatcher", "task":"T<task_n>",
 "data":{"type":"state", "state":"blocked", "question":"<判断を仰ぎたい点>", "options":["<選択肢A>","<選択肢B>"], "context_refs":["T<task_n>","A#<activity_id>","msg_id:<n>"]}}
```

orchの応答:
- `command:answer` が届いたら、その回答に従って作業を再開し `event:state(working)` を送信する
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
   {"v":1, "kind":"event", "from":"<alias>", "to":"dispatcher", "task":"T<task_n>",
    "data":{"type":"state", "state":"working", "phase":"escalation_resolved", "note":"<解決内容>", "decision_ids":[...], "log_ids":[...]}}
   ```

エスカレーション中（escalated）はorchのタイムアウト・クローズ対象外になる。

## 完了 → done

作業が完了したら:
1. acceptanceを満たしていることを確認し、証拠（evidence: テスト結果・PR URL等）を揃える
2. worker専用の記録規律（§記録規律）に従い、material保存・decision_proposalsの準備を済ませる
3. `event:state(done)` を送信する:
   ```json
   {"v":1, "kind":"event", "from":"<alias>", "to":"dispatcher", "task":"T<task_n>",
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
- `command:close` → §退場処理 を実行する
- `command:answer` 等で差し戻し（done検証NG）→ 指示に従い `event:state(working)` に戻って作業を再開

## 退場処理（cmd:close受信時）

`command:close` を受信したら以下の手順を順番に実行する:

### Step 1: event:state(draining) を送信

```json
{"v":1, "kind":"event", "from":"<alias>", "to":"dispatcher", "task":"T<task_n>", "data":{"type":"state", "state":"draining"}}
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

terminated_atと cause を付与してidentityを再送する（`term_ref` は `ow_send` が再度自動補完するため payload に乗せ直す必要なし）:

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
{"v":1, "kind":"event", "from":"<alias>", "to":"dispatcher", "task":"T<task_n>", "data":{"type":"state", "state":"terminated", "cause":"closed"}}
```

### Step 5: heartbeatループ停止

PHASE_FILEを削除してheartbeatループを終了させる:
```bash
rm /tmp/ow_hb_phase_<alias>
```

### Step 6: auto-close（worker 自身による pane/タブ kill）

`event:state(terminated, cause:closed)` または `event:state(terminated, cause:cancelled)` の relay 送信が**完了したあと**、worker は自身の pane/タブを kill して退場する（worker 完結方式）。

kill タイミングは relay POST の**完了後**で固定する。送信前に kill すると process が死んで POST が relay に届かず、orch / dispatcher が `crashed-during-drain` と誤判定するリスクがある。

`cause` 別の対応:

| cause | auto-close 対象 | 理由 |
|-------|-----------------|------|
| `closed` | ✅ 対象 | 正常終了（dispatcher の `cmd:close` 受領済み） |
| `cancelled` | ✅ 対象 | 中断終了（dispatcher の `cmd:cancel` 受領済み） |
| `dead` | ❌ 対象外 | 起動失敗。人間判断ステージに残す |
| `crashed` | ❌ 対象外 | reducer 推論のみで本 step を実行する worker は存在しない |
| `crashed-during-drain` | ❌ 対象外 | 同上 |

起動環境（環境変数）別の経路:

```bash
if [ -n "${TMUX_PANE:-}" ]; then
  # 経路 A: tmux pane で起動（通常 worker / 思考 worker tmux new-window 経路）
  tmux kill-pane -t "$TMUX_PANE"
elif [ -n "${ITERM_SESSION_ID:-}" ]; then
  # 経路 B: iTerm2 別タブ（思考 worker iTerm2 経路、暫定）
  # current tab を狙うとフォアグラウンドが別タブのときに誤って閉じうるため、
  # ITERM_SESSION_ID で対象タブを特定して閉じる。
  osascript -e "tell application \"iTerm2\" to tell current window to close (first tab whose current session's unique ID is \"${ITERM_SESSION_ID}\")" || true
else
  # 経路 C: manual / その他
  # 環境変数がいずれも未設定なら dispatcher の ow_close_worker による外部 kill にフォールバック
  :
fi
```

注意:
- 本 step が失敗（pane が消えない）した場合は `ow_close_worker` 側の SIGKILL fallback が二重防御として補完する
- 経路 B (iTerm2) は思考 worker 別タブ実装方針の確定後に再評価する（tmux 一本化なら削除可）

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

orchから `kind:command, data.type:ping` が届いたら、現在の state を `event:state` で返す:

```json
{"v":1, "kind":"event", "from":"<alias>", "to":"dispatcher", "task":"T<task_n>", "data":{"type":"state", "state":"<現在のstate>", "note":"pong"}}
```

## 禁止事項

- orchの指示なしにタスクスコープを拡張しない
- topic/activityを新規作成しない
- decisionを直接記録しない（エスカレーション例外を除く。原則decision_proposalsでorchに提案）
- `event:state(terminated)` 送信後にツールを呼ばない
- done送信後、closeを受けるまで新しい作業を始めない・cc-memoryへ追記しない（退場処理を除く）
- **AskUserQuestion 禁止**: worker は人間に直接質問しない。質問は `event:state(blocked)` envelope で orch 経由。AskUserQuestion ツールは ow_spawn_worker の settings injection で deny されているが、明示的に守ること
