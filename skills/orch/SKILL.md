---
name: orch
description: orchとしてtopicのプロジェクト進捗管理・worker指揮を行う
---

# orch

このセッションをorchとして動作させる。orchは1つのtopic（プロジェクト）の進捗管理・タスクキュー管理・worker指揮を担う。`/orch`（引数なし、または自然言語/topic ID指定）で起動する。

## アーキテクチャ原則

- **1 orch = 1 topic = 1 channel**。同一channelに複数orchを立てない
- **状態管理の階層**: relay履歴=イベントログ（唯一の真実源）→ orchestratorのqueueファイル=マテリアライズドビュー（relay履歴から再構築可能）→ cc-memory=知識層（成果のみ）
- **activityのstatusはリアルタイム性を保証しない**。稼働中タスクの真実源はorchのqueue
- **依存・優先度はqueueに持たない**。orchはcc-memoryのrelation・有向pinを参照して着手順を自律判断する

## 起動フロー

1. **queue走査**: `~/.cc-memory/ow/orch/queue-t<topic_id>.md` を走査する（auto-memory管理外のディレクトリ。パス変更は `OW_QUEUE_DIR` 環境変数で可能）。既存queueファイルがあれば再開候補として提示（crash引き継ぎ）。なければSessionStart注入のアクティビティ一覧からorch対象topicを選択（check-inスキルと同様のファジーマッチ可）
2. **relay疎通確認**: `ow_status(channel_code, topic_id)` を呼ぶ。relayサーバーの自動起動・channel自動作成（idempotent）が内部で実行される。queueファイルのfrontmatterを確認し、channel_codeを記録する（初回起動時）
3. **不在中メッセージ回収**: `ow_history(since=last_seen_msg_id)` で不在中メッセージをpullし処理する（初回起動時はskip）
4. **cc-memory check-in**: `check_in(orch_activity_id)` でアクティビティに紐づく情報を取得する
5. **特化版プレイブック取得**: `search(tags=["playbook", "domain:<topic_domain>"])` で特化版プレイブックを取得する（§プレイブック参照 参照）
6. **Monitor起動**: `Monitor recv.sh <channel_code> orch (persistent)` を起動する。Monitorはスキル指示でClaude自身に起動させる。`recv.sh` は `scripts/ow/recv.sh` にあり、引数は `<channel_code> <handle>` の位置引数。1秒自動再接続付き

**起動cwd規約**: orchは作業ルート（例: `~/workspace`）で起動し、workerにはリポジトリ/worktreeのcwdを割り当てる。auto-memoryのslug依存を回避し、orch/workerのauto-memoryを構造分離する。queueのfrontmatterに `orch_cwd` を記録し、復旧は同一cwdで行う。

## 運転ループ

Monitor発火（またはユーザー入力・自発的タイミング）を起点とする:

```
on Monitor発火 or 自発的タイミング:
  msgs = ow_history(since=last_seen_msg_id)   # msg_id昇順でpull
  for m in msgs: handle(m)                     # 冪等ハンドラ。キュー状態を更新
  queueファイル書き出し → last_seen_msg_id = max(取得msg_id) に更新
  キューを見て次のアクション判断（タスク選定・spawn・verify・close・人間対応）
  人間向けダイジェスト出力（状態変化時）→ Monitor待機へ
```

**自発的タイミングでの ow_status 呼び出し**: Monitor 発火ではなくユーザー入力・直接呼び出し等の自発的タイミングで起床した場合は、`ow_history` 処理後に `ow_status(channel, topic_id)` を呼んで worker の presence 状態を確認すること。キュー状態とworker生死の乖離を早期発見するため。

**受信処理（SSEはベル、真実源は/history）**:
1. SSE（Monitor監視）は起床信号専用。届いたdata行の中身は処理に使わない
2. 起床したら `ow_history(since=last_seen_msg_id)` で未処理メッセージを全件pull
3. msg_id昇順にバッチ処理。**全ハンドラ冪等**（再処理が安全）
4. キュー更新後に `last_seen_msg_id = max(取得msg_id)`

**構造的制約**: orchがツール実行中はMonitor新着を処理できない（単一ターン制約）。報告はrelay履歴に永続するため失われない。

**人間向け責務**: 状態変化時のタスクボード短報に加え、タスクがavailableになったら「やる？」と**着手提案まで**行う。プロジェクトごとの権限設定の範囲内なら提案を待たず自走してよい。

## 通信プロトコル

envelopeは `kind` が `command`（targeted）または `event`（broadcast）の2種。内訳は `data.type` で区別する（設計書v3 §4.1）。

### envelope 共通形式

```json
{
  "v": 1,
  "kind": "command" | "event",
  "from": "<handle>",
  "to": "<handle>" | "*",
  "data": { "type": "<内訳種別>", ... }
}
```

- `v` は body 内 envelope のスキーマバージョン（relay 物理スキーマとは別レイヤ、設計書v3 §7.4）
- `from` は messages.handle と一致しなければreducerが drop する
- `kind=command` は `to` 必須、`kind=event` は `to` 省略または `"*"` で broadcast

### orch→worker: `kind=command`

```json
{"v":1, "kind":"command", "from":"orch", "to":"w-<alias>", "task":"T<n>",
 "data":{"type":"assign|close|cancel|answer|ping", ...}}
```

| data.type | data 内容 | 補足 |
|---|---|---|
| `assign` | `{title, activity_id, topic_id, cwd, model（必須）, acceptance（必須）, context, playbook, timeout_min}` | worker は `event:state(working)` で応答 |
| `close` | `{reason}` | worker は退場処理（draining→terminated/cause:closed）で応答 |
| `cancel` | `{reason}` | worker は退場処理（draining→terminated/cause:cancelled）で応答 |
| `answer` | `{answer}` または `{escalate: true}` | blocked への応答 |
| `ping` | `{nonce}` | worker は現在の `event:state` を返す |

### worker→orch / worker→broadcast: `kind=event`

workerが送る全メッセージは `kind:event`。内訳は `data.type` で:

```json
{"v":1, "kind":"event", "from":"w-<alias>", "to":"orch|*", "task":"T<n>",
 "data":{"type":"state|identity|heartbeat", ...}}
```

| data.type | to | 意味 | data 内容 |
|---|---|---|---|
| `state` | `orch` | workload state 遷移宣言 | `{type:"state", state:..., ...payload}` |
| `identity` | `*` | 身元情報 full snapshot | identity bundle（§identity 参照） |
| `heartbeat` | `*` | liveness signal（バックグラウンドループから送信） | `{type:"heartbeat", phase, nonce?}` |

### workload state（設計書v3 §5.2 と整合）

```
       spawn
         │
         ▼
      loading ───────┐
         │           │ (load 失敗)
         ▼           ▼
       ready      terminated (cause: dead)
         │
         ▼
      working ──┬──▶ blocked ─▶ escalated ─▶ working
         │      │
         │      └──▶ working (continue)
         │
   ┌─────┴─────┐
   ▼           ▼
draining ──▶ terminated
            (cause: closed/cancelled/crashed/crashed-during-drain)
```

| state | data 必須 payload | 補足 |
|---|---|---|
| `loading` | なし | spawn直後、context load中。長時間でもheartbeatが続けばcrash扱いしない |
| `ready` | `{session_id, alias, cwd}` | assign待機 |
| `working` | `{phase, note}` | assignへの最初のworkingは `in_reply_to` で assign msg_id を指す |
| `blocked` | `{question, options, context_refs}` | orch回答待ち |
| `escalated` | `{report_md}` | watchdog対象外 |
| `draining` | なし | command:close/cancel 受領後の worker-sync 実行中。長時間でもheartbeatが続けばcrash扱いしない |
| `terminated` | `{cause}` | cause ∈ {closed, cancelled, dead}。crashed/crashed-during-drain は orch reducer の推論のみで、history には書かれない |

**done の扱い**: 設計書v3 §5.2 の workload state machine に `done` は含まれない。ただし worker は acceptance 完了申告として `event:state(done)` を送る運用（worker SKILL.md §完了→done）。orchはこれを **workload state ではなく orchestration 層の完了申告イベント** として解釈し、acceptance 照合 → `command:close` 送信に進む。doneは workload遷移ではないため、heartbeat 途絶判定の対象外（worker は close 受領前まで working として heartbeat を継続）。

**done の payload**: `{summary, evidence, synced, materials[], decision_proposals[], cancelled?}`。`cancelled`（boolean）は `command:cancel` への応答 doneの場合に true、自発 done では false または省略。

**fallback state は v3 で正式削除**（設計書v3 §5.2.1）。

### identity（event:identity）

worker 起動時と terminated 直前に append される身元情報。orchは `ow_get_identity(channel, handle)` で最新 bundle を取得する（§identity 参照経路）。

```json
{"v":1, "kind":"event", "from":"w-<alias>", "to":"*", "task":"T<n>",
 "data":{
   "type":"identity",
   "role":"worker",
   "handle":"w-<alias>",
   "channel_code":"...",
   "topic_id":"...",
   "started_at":"<UTC ISO8601>",
   "alias":"<alias>",
   "activity_id":<id>,
   "model":"...",
   "cwd":"...",
   "session_id":"...",
   "terminated_at":"<UTC ISO8601>",   // terminated 直前の再 append でのみ set
   "cause":"closed|cancelled|dead"     // terminated 直前の再 append でのみ set
 }}
```

identity から **削除された属性**: `task_n`（activity_id から逆引き可能）、`permission_mode`（auto 固定）、`user`（relay 非参加者）。設計書v3 §6.3.1 参照。

### heartbeat（event:heartbeat）

worker のバックグラウンドループ（scripts/ow/heartbeat.sh）が定期送信する liveness signal:

```json
{"v":1, "kind":"event", "from":"w-<alias>", "to":"*", "task":"T<n>",
 "data":{"type":"heartbeat", "phase":"alive|loading|ready|working|draining"}}
```

- 周期: loading=10秒、それ以外=30秒（設計書v3 §5.4.1）
- `phase` は workload state を写像する補助情報

**スレッド規約**: `in_reply_to` はrelay物理カラムとして残置されているが、ow 応用層では原則使わない（設計書v3 §4.4）。例外として assign への最初の `event:state(working)` には `in_reply_to=<assign msg_id>` を付けることがある（worker SKILL.md 規約に従う）。`to` はbody内規約でサーバーはルーティングしない（宛先フィルタは受信側のローカル実装）。

### 旧 cmd/state envelope の後方互換放棄

旧 `kind:cmd` / `kind:state` レコードは v3 reducer・orch では解釈しない。新規送受信は `kind:command` / `kind:event` のみとする。relay 物理スキーマ（messages テーブル）は凍結されており、過去レコードはそのまま残置される。

## タスクキュー

### ファイルパス

- パス: `~/.cc-memory/ow/orch/queue-t<topic_id>.md`（auto-memory管理外）
- MEMORY.mdに1行ポインタを追記し「orchセッション専用」と明記する
- task fileは `~/.cc-memory/ow/orch/tasks/t<topic_id>-T<n>-<title-slug>.md`（マークダウン形式: YAML frontmatter＋本文。title slugでファイル名から内容が分かり、topic prefixでtopic間の名前衝突を防ぐ。topic_id未指定時は `T<n>-<title-slug>.md`）
- パスは `OW_QUEUE_DIR` 環境変数で変更可能（変更する場合もauto-memory管理外のディレクトリを指定すること）

### frontmatterフォーマット

```markdown
---
topic_id: 454
orch_activity_id: 798
channel_code: AbCdEfGh
orch_cwd: /Users/babajunichi/workspace
last_seen_msg_id: 0
---

## T1 | タスク名 | status
- worker: w-a / term_ref: iterm2:UUID / session: <uuid>
- activity: 801
- model: claude-opus-4-7
- cwd: ~/workspace/cc-memory/.trees/feature-xxx
- assigned: HH:MM / last_recv: HH:MM
- acceptance: {acceptance条件}
- note: {最新状態のメモ}
```

### status遷移

```
queued → spawning → assigned → in_progress → awaiting_verify → done
                                                              ↘ cancelled
                                                              ↘ failed
                  + escalated / stalled（途中状態として付与可能）
```

- `spawning` はspawn実行前にwrite-ahead（孤児worker対策）
- `done`: orchがacceptance照合・synced確認後に設定
- `cancelled`: orchがキャンセル処理後に設定
- `failed`: 人間判断待ちのまま設定
- `stalled`: watchdog検出後、人間通知済みで設定

## activityとの対応

- orchが作成する[作業]activityには**専用タグ `orch-managed` を必須付与**する。個人フロー（SessionStartの一覧注入・スコアリング・nudge判定）から除外される
- **タスク終端とactivity statusのマッピング**:
  - `done` → activity `completed`
  - `cancelled` → activity `completed` + description先頭に[cancelled]経緯を追記
  - `failed` → activity `in_progress` のまま経緯を追記し人間判断待ち
- activityのstatusはリアルタイム性を保証しない記録。稼働中の真実源はqueue

## タスク完了の定義と検証

**「完了」の定義**: `event:state(done)` の `evidence` が `acceptance` を満たすとorchが判定 ∧ `synced: true`。無条件信頼しない。

**done は workload state ではない**: 設計書v3 §5.2 の workload state machine に done は含まれず、worker→orchの完了申告として `event:{type:state, state:done}` envelope形式で扱う（orchestration層の信号）。orchはdoneを受信したらacceptance照合し、`command:close`送信 → workerは draining → terminated(cause:closed) へ進む。

**cancelと自発doneの交差**:
- `event:state(done)` の `in_reply_to` が `command:cancel` を指していなければ自発done
- orchは自発doneを優先してacceptance照合し、cancelを無効化する（完成タスクを捨てる理由がない）

**done検証NG時の振る舞い**:
- acceptance不充足 → `command:answer` に理由を付けてworkerに差し戻す
- 差し戻しを受けてもworkerが改善できない場合 → `failed` としてキューを更新し人間に通知する

**クローズハンドシェイク**:
1. orchが「done検証OK ∧ synced:true ∧ escalated/stalled非該当」を確認
2. `command:close` を送信
3. workerは `event:state(draining)` → worker-sync → `event:identity` 再append → `event:state(terminated, cause:closed)` を送信
4. orchは `event:state(terminated, cause:closed)` 受信後に `ow_close_worker(term_ref)` でセッションをクローズ
5. terminated が来なければ閉じずに人間に通知する

## watchdog

**監視基準**: 「そのworkerからの最後の `event:heartbeat` 受信時刻」からの経過時間（設計書v3 §5.4.2）。workload state の所要時間や `last_recv` 全般の経過では判定しない（長時間 loading や draining でも heartbeat が続けば crash 扱いしない）。

**heartbeat 周期 × 3 の閾値**:

| 現在の workload state | heartbeat 周期 | タイムアウト閾値（周期×3） |
|---|---|---|
| `loading` | 10秒 | **30秒** |
| `ready` / `working` / `blocked` / `draining` | 30秒 | **90秒** |
| `escalated` | 監視対象外 | — |

orchは `ow_get_workload_state(channel, handle)` で現在のstateを参照し、対応する閾値を選んで判定する。閾値はreducer実装のチューニング余地として残るが、orch側の初期実装は上記固定値を使う。

**タイムアウト処理（heartbeat 途絶検知）**:
1. heartbeat 途絶（loading: 30秒 / ready以降: 90秒）→ `command:ping` を送信
2. pingに無応答（heartbeat も復活しない）→ 後述のcrash推論経路に遷移
3. **自動failedおよび自動クローズはしない**。failedへの変更・強制クローズは人間判断

**watchdog対象外**:
- `escalated` 状態のworker（人間対話中はタイムアウト・クローズ対象外、設計書v3 §5.2の state machine参照）
- `terminated` 状態のworker（既に終了済み）

**ready未送信タイムアウト（loading 滞留）**: heartbeat が来ているかぎり crash 扱いしない。30秒以上 heartbeat 途絶した場合のみ crash 候補となる。長時間 loading（巨大コンテキスト読込・1Mコンテキストモデルのwarm-up等）でも heartbeat が続けばタイムアウトしない。

**spawning（ready前）タイムアウト**: spawning は orch 側 queue の状態。worker が `event:identity` または `event:state(loading)` を送る前に timeout_min 経過した場合は、spawn失敗の可能性が高い（cwd不在・aliasぶつかり・relay疎通断等）。`ow_recover` で pending_spawn として検出される。

## モデル選択

assignの `model` は必須。一般プレイブック `skills/orch/playbook.md` のモデル選択目安表に従って選択する。

## エスカレーション

1. workerが `event:state(blocked)` を送信する
2. orchは**特化版→一般版プレイブックと過去エスカレーションログ**（`search(tags=["escalation","domain:..."])`）を参照してから判断する
3. 回答可能なら `command:answer {answer}` を送信する
4. 判断不能なら `command:answer {escalate: true}` → workerはエスカレーションフォーマット（質問/推奨と理由/選択肢/文脈要約/関連ID）で自セッションに出力し `event:state(escalated)` を送信する
5. orchは人間に概要とworkerの場所（term_ref）を提示する
6. 人間がworkerセッションで直接解決する
7. workerがその場で記録（log必須・合意decisionも。タグ: `escalation`+`user-decision`+domain）
8. worker → `event:state(working) {summary, decision_ids, log_ids}` で再開通知 → orchが特化版プレイブックを更新する（§プレイブック参照）

エスカレーション中（`event:state(escalated)`）はwatchdog対象外（§watchdog参照）。

## プレイブック参照

| | 一般版 | トピック特化版 |
|---|---|---|
| 内容 | モデル選択目安、タイムアウト・worker同時数既定値、エスカレーション基準、報告頻度等 | topicで蓄積した対応知識 |
| 保存場所 | `skills/orch/playbook.md`（同梱・静的） | cc-memory material（タグ `playbook`+domain、related=topic） |
| 更新 | プラグイン更新 | orchが新material+supersedes relationで版管理 |
| 参照優先 | 特化版がない項目のみ | **特化版優先** |

orchは起動時に特化版最新を取得し、assignの `playbook` フィールドで関連抜粋をworkerに渡す。

## identity 取得経路

orchはworkerの身元情報（alias、activity_id、model、cwd、session_id、terminated_at、cause等）を取得する際は、必ず `ow_get_identity(channel, handle)` 経由で取得する（設計書v3 §6 / §8.3）。`ow_history` を自前パースして identity bundle を組み立てる経路は使わない。

理由:
- reducer が event:identity の最新エントリを返す責務を持つため、orch側で二重実装しない
- crash 推論（`cause: "crashed (inferred)"` / `"crashed-during-drain (inferred)"`）は reducer がメモリ上で付与する（DB不変、設計書v3 §9.2）。orch側で直接 history を見ると推論結果が得られない

ID別の取得関数:

| 関数 | 用途 |
|---|---|
| `ow_get_identity(channel, handle)` | 指定 handle の最新 identity bundle（+ crash推論結果） |
| `ow_list_identities(channel, alive_only)` | channel上の全 handle の identity リスト（alive_only=True で terminated 除外） |
| `ow_get_presence(channel, handle)` | SSE接続状態 + 最新 heartbeat 受信時刻から online/offline 推論 |
| `ow_get_workload_state(channel, handle)` | 指定 handle の最新 workload state（watchdog 閾値選定に使う） |

## crash 推論の cause lineup と queue 反映

設計書v3 §5.2.2 / §9 の cause lineup に基づき、orchは以下のルールで queue status を更新する。`cause` 値は `ow_get_identity` の戻り値 `cause` フィールドから判定する（履歴に明示書き込まれる closed/cancelled/dead と、reducer がメモリ上で付与する crashed/crashed-during-drain）。

| cause | 発生条件 | queue status への反映 | 補足 |
|---|---|---|---|
| `closed` | command:close 受領 → 正常退出 | `done`（acceptance満たし & synced済み） | クローズハンドシェイク完了 |
| `cancelled` | command:cancel 受領 → 退出 | `cancelled` | activity description先頭に[cancelled]経緯を追記 |
| `dead` | loading 中の load 失敗 | `failed` | 人間に通知し復旧手段を判断（再spawn等） |
| `crashed (inferred)` | ready/working/blocked/escalated 中の heartbeat 途絶 | `stalled` + 人間に通知 | 自動failedにはしない |
| `crashed-during-drain (inferred)` | draining 中の heartbeat 途絶 | `stalled` + 人間に通知（worker-sync失敗の懸念） | done評価は人間判断（acceptance確認＋手動同期検討） |

**自動 failed および自動クローズはしない**: heartbeat 途絶（crashed推論）は worker が長時間ツール実行中の場合にも発生しうるため、確実な異常証明とはならない。failedへの変更・強制クローズは人間判断とする。

## crash復旧

1. **crash中**: worker側はheartbeat停止のみ。報告はrelay SQLite履歴に残存する
2. **再起動**: 人間が`orch_cwd`と同じcwdで `/orch` を起動し、queue再開候補から選択する
3. **不在中メッセージ回収**: `ow_history(since=last_seen_msg_id)` から実行（冪等性が再処理を安全にする。専用復旧ロジック不要）
4. **整合チェック**（必須）: `ow_recover(channel, topic_id)` を呼ぶ。relay履歴since=0再走査・queue・identity reducerの3者突合・自動修正までを一括で行う:
   - **ghost_active**（queue=assigned/ready/working/blocked/draining & `ow_get_identity().cause` が terminal）→ identity reducer の最新 cause から queue を自動再構築（cause:closed→done、cause:cancelled→cancelled、cause:dead→failed、cause:crashed/crashed-during-drain→stalled）
   - **pending_spawn**（queue=spawning & identity未生成）→ relay履歴に当該workerのevent:identity または event:state が**1件もない**場合は ow_recover は触らない（spawn racing回避）。orchは戻り値で残留spawning件数を把握し、経過時間が長い物は手動で `failed` 化判断する
   - **stalled_done**（queue=done/closed/cancelled/failed & identity が alive）→ `command:ping` 送信で素性照会
   - **orphans**（identity に登録があるがqueue外の handle）→ `command:ping` 送信で再リンク照会
   - 検証だけしたい時は `dry_run=True` で呼ぶ
   - 戻り値の `detected`/`applied`/`warnings` を確認し、ping応答は通常受信ループで処理
   - **cc-memory activityも突合**（ow_recover対象外）: queue終端済み（done/cancelled/failed） × activity `in_progress` 残留を別途検出・修正する
5. **worker復帰**: ow_recoverが送った `command:ping` への応答（worker の `event:state(<現在のstate>, note:"pong")`）を受信ループで処理する。応答が来ないworkerは前述のcrash推論経路に進む

**spawn前バリデーション**: `ow_spawn_worker` は内部で relay疎通・channel存在・cwd存在・alias重複の4点を自動チェックする。失敗時は `{"error": {"code": "SPAWN_PRECONDITION_FAILED", "warnings": [...]}}` が返るので、warningsを確認して原因を解消してから再spawnする。

**tmux分割表示**: `OW_TERMINAL=tmux` の環境では、orchが自身の `os.environ['TMUX_PANE']` を読んで `ow_spawn_worker(..., tmux_target_pane=<TMUX_PANE>)` に渡すと、orchペインと同じwindow内にworker paneが分割表示される（最初は右に30%水平、以降は右ペインを垂直分割で積む）。未指定時は従来の `ow-workers` 別sessionに新windowで起動する。MCPサーバープロセスのenvは起動時にフリーズするためサーバー側で参照できない、必ずクライアント側で読んで渡すこと。

## 複数orchの運用

- インスタンスキー = `topic_id`。channel・queueファイル・worker aliasすべて分離する
- relayサーバーはSPOF（全インスタンス共有）だが、recv.sh自動再接続+history回収で復旧は自動
- orch間調整はスコープ外（人間の采配）

## MCPツール一覧

| ツール | 用途 |
|---|---|
| `ow_send(channel, handle, body, needs_reply, in_reply_to)` | メッセージ送信（4xx即失敗、5xx/接続断のみ3回指数バックオフ）。bodyは `kind=command`/`event` envelope |
| `ow_history(channel, since, limit)` | 履歴pull（受信処理の本体・保険経路。SSE push本体添付が主軸、設計書v3 §3.3） |
| `ow_spawn_worker(alias, channel, cwd, model, task_title, acceptance, context, playbook, timeout_min, activity_id, topic_id, task_n, tmux_target_pane)` | worker起動（spawning write-ahead→task file書き出し→アダプタ起動→安定ID返却。permission_modeはauto固定）。OW_TERMINAL=tmux時は orch自身の TMUX_PANE を `tmux_target_pane` に渡すと同window内に分割表示される |
| `ow_close_worker(term_ref)` | workerクローズ |
| `ow_status(channel, topic_id)` | queue+identity統合ビュー |
| `ow_recover(channel, topic_id, dry_run)` | crash復旧（queue×relay履歴×identity reducer突合・ghost_active自動再構築・stalled/orphan command:ping送信） |
| `ow_get_identity(channel, handle)` | 指定 handle の最新 identity bundle（+ crash推論結果） |
| `ow_list_identities(channel, alive_only)` | channel上の全 handle の identity リスト |
| `ow_get_presence(channel, handle)` | SSE接続状態 + 最新 heartbeat 受信時刻から online/offline 推論 |
| `ow_get_workload_state(channel, handle)` | 指定 handle の最新 workload state（watchdog 閾値選定に使う） |
| `check_in(activity_id)` | cc-memoryのアクティビティcheck-in |
| `add_activity(...)` / `update_activity(...)` | アクティビティ管理（orch-managedタグ必須） |
| `search(...)` | 特化プレイブック・過去エスカレーションログ検索 |
| `add_material(...)` | プレイブック版管理（supersedes relationで版管理） |

## Monitor起動（必須）

orchはSSEをMonitorツールで待ち受ける:

```
Monitor recv.sh --me orch (persistent)
```

`recv.sh` は `scripts/ow/recv.sh` にあり、1秒自動再接続付き。persistentモードでイベントドリブン待ち受けを行う。Monitorは手動起動のみ可能なため、スキル指示でClaude自身に起動させること。

## 禁止事項

- 同一channelへの複数orch参加（混在channelは1 channel = 1 topic原則違反）
- done/cancelled/failedに至っていないworkerの自動クローズ
- failed設定および強制クローズの自律判断（人間判断が必要）
- escalated状態workerへのwatchdog適用
- cc-memory activityへのリアルタイム稼働状態の書き込み（queue=真実源）
