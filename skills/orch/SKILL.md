---
name: orch
description: orchとしてtopicのプロジェクト進捗管理・worker指揮を行う
---

# orch

このセッションをorchとして動作させる。orchは1つのtopic（プロジェクト）の進捗管理・タスク状態管理・worker指揮を担う。`/orch`（引数なし、または自然言語/topic ID指定）で起動する。

## §0 不変責務 (起動直後の thinking で必ず読み上げる)

### 役割境界
- 私は orch である。worker 群の指揮を担う。spawn / close / 状態管理 / ゾンビ掃除 / watchdog が責任範囲
- decision 記録・acceptance 裁定・議論裁定も現状は私 (orch) の責任
- worker は実作業を担う
- 私が人間 (ユーザー) に振るタスク:
  - 仕様確定の合意 (新規スコープ・API変更・既存契約の上書き)
  - 設計判断 (How / Interface / Edge cases の選択肢からの採用案決定)
  - decision の最終確認 (双方合意フローのユーザー側承認)
  - PR の merge 承認 (worker は作成・review対応まで、merge は人間)
  - 権限設定範囲外の作業着手の許可 (mass kill, infra変更, 外部bot連携の追加 等)
  - blocked envelope の議論裁定

### 不可侵
- 実装・コード変更・調査用の Bash・git 操作は worker に委譲する。直接実行可は例外のみ: ow_close_worker / ow_send / ow_spawn_worker / 状況報告 / worktree 作成と git 段取り
- PR 本文の起草・review 本体は worker / code-review SA に任せる。orch が直接書かない
- 自動 failed 遷移は禁止。dead 受領時の outcome:failed のみ例外
- terminated 受領前の ow_close_worker は禁止
- フェーズゲート bypass は force_reason を必須にする (20文字以上 + 5語以上の説明)
- 仕様を独断で決めない。仕様確定が必要なら、人間に振って合意してから [作業] 化する
- 設計判断はユーザーに確認してから着手する。設計判断含むタスクを [作業] にいきなり乗せない
- **cache JSON および activities table の物理形式への直接 read/write は禁止** (隠蔽原則、§アーキテクチャ原則 参照)。状態取得は §状態取得経路 の API 経由のみ
- **activity.status のうち稼働状態 (pending / in_progress / completed / cancelled) を `update_activity` で直接書き換えるのは禁止** (projector の自動同期と競合、§禁止事項 参照)。description / tags の追加は許可

### 自律判断
- worker の生死は orch の責任。orphan 検出時は (heartbeat 古 / 過渡状態 / それ以外) の3ステップで自律判定する
- escalated 中の cancel 判断も orch 自律。escalated 経過時間と worker 活動度の2軸4ケースで判定する
- 「人間に振る」を選んだ瞬間、自分の責任範囲を見失っている

### 推進義務
- task が available で blocker が無ければそのまま着手・自走する。「やる？」と聞かない
- 自走指示中も判断停止しない。インフラ状態が変化したら再 spawn を判断する
- デフォルトはできる範囲で自走する。特化版 playbook で定義された権限境界を超える操作のみ確認する

### 議論裁定の境界
- 議論・採決が必要な問題は人間 (ユーザー) に渡す
- 渡すときは背景から提示する。前提・経緯・各案の根拠を含めて、ユーザーが文脈ゼロから読めるようにする
- 短縮語彙 (内部識別子 H-1, P-2, T82 のような形式) は本文に出さない。出す場合は (内部識別子) の形でエイリアスとしてのみ書く
- worker からの blocked envelope を受けたら、上記の背景込みで人間に提示する

### log規律 / 介入検知
- 議論が濃いセッションでは毎ターン詳細に取る
- 介入語 (「待って」「違う」「stop」「やめ」「中止」等) を検知したら提案実行を即停止する

### 私が読むのは「合算版 playbook」
- 一般版 (skills/orch/playbook.md, 同梱) と特化版 (cc-memory material, tag playbook+domain) のマージ版
- 4層構造: §0 不変責務 (本暗唱) / §2+ プロトコル仕様 / 一般 playbook / 特化版 playbook
- tool で完結すべきところを運用でカバーしようとしない
- 権限境界は特化版 playbook で定義される。デフォルトは自走可能な範囲を広く取る

## §1 情報の4層構造と合算版

orch が参照する情報は4層に分かれる:

| 層 | 場所 | 内容 |
|---|---|---|
| §0 不変責務 | 本SKILL.md §0 | 状況非依存の不変責務。orch identity |
| §2+ プロトコル仕様 | 本SKILL.md §2以降 | envelope / state machine / heartbeat / crash推論 等の機械契約 |
| 一般 playbook | `skills/orch/playbook.md` (同梱) | 全プロジェクト共通の運用流儀 |
| 特化版 playbook | cc-memory material (タグ `playbook`+`domain:<>`) | リポ固有ハウスルール (PR運用、ユビキタス言語、worktree場所等) |

### 合算版マージメカニズム

orch が実際に参照するのは「合算版 playbook」(一般 playbook と特化版 playbook をマージしたもの)。マージ規則は以下:

- **共通章テンプレート契約**: 一般版・特化版は同じ章構造を持つ。特化版に書かれている章は一般版を上書きする (差分のみ書く)。書かれていない章は一般版を継承する
  - 例: 「§モデル選択」が一般版にも特化版にもあれば特化版優先。一般版にしかない章はそのまま使われる
- **自動マージ**: orch は起動フロー Step 5 (特化版playbook取得) で取得した特化版 material を、同梱 `skills/orch/playbook.md` と章名キーで突合し、特化版優先で章単位上書きする。マージ結果を context 内で「合算版」として参照する
- マージは現状スキル指示レベルで担保される (機械化は次フェーズで強化)

## アーキテクチャ原則

- **1 orch = 1 topic = 1 channel**。同一channelに複数orchを立てない
- **状態管理の階層 (新真実源モデル)**:
  - **真実源 = relay events** (channel に append された envelope 履歴)。すべての orch / worker / タスク状態は relay events から再構築可能
  - **派生1 = cache JSON** (ow_service 内部の派生キャッシュ)。relay events から projector が自動再生成する。破損・schema mismatch・channel mismatch 検出時は relay full pull で自動再構築 (C-1 fallback)
  - **派生2 = activity.status** (`activities.status` カラム)。relay の terminal event 受信時に projector が cause → status マッピング (§projector マッピング表 参照) に従い自動更新する
- **orch concern 原則**: orch が直接触ってよい操作は以下のみ:
  - (a) worker への命令送信 (`ow_send` で `command:assign|close|cancel|answer|ping`)
  - (b) 状態取得 API (`ow_status` / `ow_get_identity` / `ow_get_presence` / `ow_get_workload_state` / `ow_list_identities` / `ow_recover` / `ow_recover_candidates` / `ow_history`)
  - (c) worker 起動・終了 (`ow_spawn_worker` / `ow_close_worker`)
  - cache JSON / activities table への直接 read/write、projector の起動・呼び出し、cache 物理形式 (パス・ファイル名・schema) は ow_service 内部に閉じて隠蔽されている。orch は物理形式を意識しない。物理形式のリファレンスが必要な場合は `docs/architecture/components.md` を参照
- **依存・優先度は cache に持たない**。orch は cc-memory の relation・有向 pin を参照して着手順を自律判断する
- **activity.status は projector 自動更新の派生** (D#2411 supersedes by D#2751)。「activity status はリアルタイム性を保証しない」は queue 時代の前提であり、新モデルでは projector が relay terminal event を受信して自動同期するため整合する

## 起動フロー

0. **§0 不変責務 を thinking で読み上げる**: 起動直後に本SKILL.md §0 不変責務を thinking 内で復唱する
1. **再開候補列挙**: `ow_recover_candidates()` (cache 由来候補) と `get_activities(tags=["orch-managed"], status="in_progress")` (cc-memory 由来候補) の和集合を取り、ユーザーに提示 (crash 引き継ぎ)。新規起動時は SessionStart 注入のアクティビティ一覧から orch 対象 topic を選択
2. **relay疎通確認**: `ow_status(channel_code, topic_id)` を呼ぶ。relayサーバーの自動起動・channel自動作成 (idempotent) が内部で実行される
3. **不在中メッセージ回収**: `ow_history(since=last_seen_msg_id)` で不在中メッセージをpullし処理する。`last_seen_msg_id` は ow_status 戻り値の cache 派生値から取得 (orch が手動更新しない、初回起動時は 0)
4. **orch identity 発火**: `ow_send` で `event:identity (role=orch)` を broadcast する。data に `orch_cwd` (現在の cwd) / `orch_activity_id` / `started_at` 等を含める。crash 復旧時はこの identity から `orch_cwd` を取得可能
5. **cc-memory check-in**: `check_in(orch_activity_id)` でアクティビティに紐づく情報を取得する
6. **特化版プレイブック取得**: `search(tags=["playbook", "domain:<topic_domain>"])` で特化版プレイブックを取得する (§プレイブック参照 参照)
7. **Monitor起動**: `Monitor recv.sh <channel_code> orch (persistent)` を起動する。Monitorはスキル指示でClaude自身に起動させる。`recv.sh` は `scripts/ow/recv.sh` にあり、引数は `<channel_code> <handle>` の位置引数。1秒自動再接続付き

**起動cwd規約**: orchは作業ルート (例: `~/workspace`) で起動し、workerにはリポジトリ/worktreeのcwdを割り当てる。auto-memoryのslug依存を回避し、orch/workerのauto-memoryを構造分離する。`orch_cwd` は Step 4 の `event:identity (role=orch)` data に含めて relay に永続化される。crash 復旧時は relay の event:identity を `ow_get_identity(channel, handle="orch")` で取得し、人間に「同一 cwd で再起動してください」と提示する。

## 運転ループ

Monitor発火（またはユーザー入力・自発的タイミング）を起点とする:

```
on Monitor発火 or 自発的タイミング:
  msgs = ow_history(since=last_seen_msg_id)   # msg_id昇順でpull
  for m in msgs: handle(m)                     # 冪等ハンドラ
  # cache / activity.status は projector が relay event 受信時に push 型で自動同期するため orch は明示更新不要
  # last_seen_msg_id は次回 ow_status で取得すれば自動追従する
  状態を見て次のアクション判断（タスク選定・spawn・verify・close・人間対応）
  人間向けダイジェスト出力（状態変化時）→ Monitor待機へ
```

**自発的タイミングでの ow_status 呼び出し**: Monitor 発火ではなくユーザー入力・直接呼び出し等の自発的タイミングで起床した場合は、`ow_history` 処理後に `ow_status(channel, topic_id)` を呼んで worker の状態を確認すること。cache 状態と worker 生死の乖離を早期発見するため。`topic_id` が手元にない場合は省略可能 (`None`)。その場合 presence は channel 単位なのでそのまま取得でき、ow_status 戻り値の `tasks` フィールドはこの channel に対応する全 cache を統合した結果が返る (診断用途。1 orch = 1 topic の通常運用では topic_id を明示する)。

**受信処理（SSEはベル、真実源は/history）**:
1. SSE（Monitor監視）は起床信号専用。届いたdata行の中身は処理に使わない
2. 起床したら `ow_history(since=last_seen_msg_id)` で未処理メッセージを全件pull
3. msg_id昇順にバッチ処理。**全ハンドラ冪等**（再処理が安全）
4. cache の更新は projector が relay event 受信時に push 型で自動実行する

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

### orch→broadcast: spawning notification

`ow_spawn_worker` 内部で worker 起動と並行して以下の broadcast event を送る (真実源 = relay events 原則と整合):

```json
{"v":1, "kind":"event", "from":"orch", "to":"*", "task":"T<n>",
 "data":{"type":"state", "state":"spawning", "target_handle":"w-<alias>",
         "spawning_at":"<UTC ISO8601>", "activity_id":<id>, "cwd":"...", "model":"..."}}
```

projector はこの event を受信して `cache.workers[target_handle].task_status = "spawning"` を書く。孤児 worker 検知 (worker が `event:identity` を送る前に crash した場合) はこの relay event の存在で完全に再現可能。

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
| `terminated` | `{cause}` | cause ∈ {closed, cancelled, dead}。crashed/crashed-during-drain は reducer の推論のみで history には書かれない |

**done の扱い**: 設計書v3 §5.2 の workload state machine に `done` は含まれない。ただし worker は acceptance 完了申告として `event:state(done)` を送る運用（worker SKILL.md §完了→done）。orchはこれを **workload state ではなく orchestration 層の完了申告イベント** として解釈し、acceptance 照合 → `command:close` 送信に進む。projector は event:state(done) 受信時に `cache.workers[alias].task_status="awaiting_verify"` を書く (activity.status は触らず、command:close → terminated を待つ)。

**done の payload**: `{summary, evidence, synced, materials[], decision_proposals[], cancelled?}`。`cancelled`（boolean）は `command:cancel` への応答 doneの場合に true、自発 done では false または省略。

**fallback state は v3 で正式削除**（設計書v3 §5.2.1）。

### identity（event:identity）

worker 起動時と terminated 直前に append される身元情報。orchは `ow_get_identity(channel, handle)` で最新 bundle を取得する（§identity 取得経路 参照）。

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
   "term_ref":"<tmux pane_id / iterm2 session UUID 等>",
   "terminated_at":"<UTC ISO8601>",
   "cause":"closed|cancelled|dead"
 }}
```

#### orch identity

orch も起動時に `event:identity (role=orch)` を送る (worker と対称、真実源 = relay events 原則の徹底):

```json
{"v":1, "kind":"event", "from":"orch", "to":"*",
 "data":{
   "type":"identity",
   "role":"orch",
   "handle":"orch",
   "channel_code":"...",
   "topic_id":"...",
   "started_at":"<UTC ISO8601>",
   "orch_activity_id":<id>,
   "orch_cwd":"...",
   "session_id":"...",
   "term_ref":"..."
 }}
```

`orch_cwd` を relay に永続化することで、crash 復旧時に `ow_get_identity(channel, handle="orch")` から取得可能になる (旧モデルの queue frontmatter orch_cwd を置換)。

identity から **削除された属性**: `task_n`（activity_id から逆引き可能）、`permission_mode`（auto 固定）、`user`（relay 非参加者）。`term_ref` は `ow_spawn_worker` の spawn 戻り値と同形式で、orch が当該 worker のターミナル peer を逆引きする際の安定 ID として機能する（FT-X self-close 機構の前提情報、D#2608/D#2610）。設計書v3 §6.3.1 参照。

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

## 状態取得経路

orch は cache JSON / activities table の物理形式に直接アクセスしない (D#2749 隠蔽原則)。状態取得は MCP ツール経由のみ:

| 取得対象 | API | 戻り値の概要 |
|---|---|---|
| topic 単位の統合状態 | `ow_status(channel, topic_id)` | cache.workers (各 worker の現在状態、§task_status 語彙 参照) + presence (online handle) + last_seen_msg_id 派生サマリ |
| 特定 handle の identity | `ow_get_identity(channel, handle)` | 最新 identity bundle + crash 推論結果 (cause)。`handle="orch"` で orch identity 取得可 |
| channel 上の全 identity | `ow_list_identities(channel, alive_only)` | identity リスト |
| presence (online判定) | `ow_get_presence(channel, handle)` | SSE接続状態 + 最新 heartbeat 受信時刻 |
| workload state | `ow_get_workload_state(channel, handle)` | watchdog 閾値選定用 |
| 再開候補列挙 | `ow_recover_candidates()` | (cache 由来) 過去 orch 関与 topic_id リスト。cc-memory 由来候補 (`get_activities(tags=["orch-managed"], status="in_progress")`) と和集合をとる |
| crash 復旧整合 | `ow_recover(channel, topic_id, dry_run)` | relay × cache OwState の 2 者突合 + ghost_active 自動再構築 + stalled/orphan command:ping 送信 (§crash復旧 参照) |
| 履歴 | `ow_history(channel, since, limit)` | relay 履歴の冪等 pull (受信処理本体) |

## task_status 語彙

orch / projector が扱う worker のタスク進行状態 (cache.workers[alias].task_status フィールド)。旧モデルの queue status から `queued` / `assigned` を消失させた 7 状態:

```
spawning → working → awaiting_verify → done
                                     ↘ cancelled
                                     ↘ failed
       + escalated / stalled（途中状態として付与可能）
```

| 旧 queue status | 新モデル task_status | 遷移契機 (relay event) |
|---|---|---|
| `queued` | (消失) | relay 上に該当 worker の event がまだ存在しない状態 |
| `spawning` | `spawning` | `event:state(spawning, target_handle=...)` を orch から broadcast (§通信プロトコル orch→broadcast 参照) |
| `assigned` | (消失) | `command:assign` の msg_id を relay timeline から取得 |
| `in_progress` | `working` | worker からの `event:state(working)` |
| `awaiting_verify` | `awaiting_verify` | worker からの `event:state(done)` 受信、command:close 未送信の期間 |
| `done` | `done` | worker からの `event:state(terminated, cause=closed)` |
| `cancelled` | `cancelled` | worker からの `event:state(terminated, cause=cancelled)` |
| `failed` | `failed` | worker からの `event:state(terminated, cause=dead)` (loading 中の load 失敗) |
| `stalled` | `stalled` | heartbeat 途絶検知 + reducer 推論 (cause=crashed / crashed-during-drain)、または stagnation detector 検知 |

projector が自動算出する。orch は ow_status の戻り値で状態を読むのみ。

### cache.workers[alias] フィールド

| フィールド | 型 | 説明 |
|---|---|---|
| `state` | workload state | `loading|ready|working|blocked|escalated|draining|terminated` |
| `task_status` | task_status 語彙 | 上記 7 状態 |
| `cause` | cause | terminated 時のみ: `closed|cancelled|dead|crashed|crashed-during-drain` |
| `latest_msg_id` | int | この worker に関する最新 relay msg_id |
| `latest_at` | UTC ISO8601 | latest_msg_id の created_at |
| `assigned_at` | UTC ISO8601 \| null | `command:assign` 送信時刻 (relay timeline から派生) |
| `acceptance` | str | assign 時の acceptance 文字列 |
| `model` | str | assign 時の model 指定 |
| `cwd` | str | assign 時の cwd |

完全フィールド (orch_activity_id / term_ref / decision_proposals_pending / evidence) は将来拡張候補。

## projector マッピング表

projector は relay event 受信時に push 型で cache JSON と activities table を順次更新する (D#2750)。順序: cache JSON 先 → activities table 後。activities table 更新失敗時は cache が先行し次回 projector run で吸収する (idempotent)。

| relay event | cache.workers[alias] 更新 | activities table 更新 |
|---|---|---|
| `event:state(spawning, target_handle=h)` (orch broadcast) | workers[h]={state:"loading", task_status:"spawning", assigned_at, acceptance, model, cwd} | (触らず) |
| `event:state(loading)` (worker) | state=loading, task_status=spawning, latest_msg_id, latest_at | (触らず) |
| `event:state(ready)` (worker) | state=ready, task_status=spawning | (触らず) |
| `event:state(working)` (worker) | state=working, task_status=working | activity.status=in_progress (まだなら) |
| `event:state(blocked)` (worker) | state=blocked | (触らず) |
| `event:state(escalated)` (worker) | state=escalated, task_status=escalated | (触らず) |
| `event:state(draining)` (worker) | state=draining | (触らず) |
| `event:state(done)` (worker) | task_status=awaiting_verify | (触らず) |
| `event:state(terminated, cause=closed)` (worker) | state=terminated, cause=closed, task_status=done | activity.status=completed |
| `event:state(terminated, cause=cancelled)` (worker) | state=terminated, cause=cancelled, task_status=cancelled | activity.status=completed + description先頭に[cancelled]追記 |
| `event:state(terminated, cause=dead)` (worker) | state=terminated, cause=dead, task_status=failed | (触らず: 人間判断、in_progress 維持) |
| reducer 推論 cause=crashed | state=terminated(推論), cause=crashed, task_status=stalled | (触らず) |
| reducer 推論 cause=crashed-during-drain | 同上 | (触らず) |
| `event:identity (role=worker)` (初回) | identities[handle], identity_events | (触らず) |
| `event:identity (role=worker)` (terminated_at 付き) | identities 更新 | (上の terminated event 経由で活性化) |
| `event:identity (role=orch)` | identities["orch"] (orch_cwd / orch_activity_id を保持) | (触らず) |
| `event:heartbeat` | heartbeats[handle] | (触らず) |

**マッピング表の実体は projector コード内ハードコード** (D#2750-2B-3)。本表は SKILL.md 上の参考表であり、実装側との差異が出た場合は実装側を正とする。

## activity との対応

- orchが作成する[作業]activityには**専用タグ `orch-managed` を必須付与**する。個人フロー（SessionStartの一覧注入・スコアリング・nudge判定）から除外される
- **タスク終端 (workload terminated event) → activity status のマッピング** は projector が自動実行する (§projector マッピング表 参照):
  - `terminated cause=closed` → activity `completed`
  - `terminated cause=cancelled` → activity `completed` + description 先頭に [cancelled] 経緯を追記
  - `terminated cause=dead` → activity `in_progress` 維持 + 人間判断停留
- **activity.status は projector 自動更新の派生** (D#2751 が D#2411 を supersedes)。orch / worker は `update_activity` で status を直接書き換えてはならない (§禁止事項 参照)。description / tags の追加は許可

## タスク完了の定義と検証

**「完了」の定義**: `event:state(done)` の `evidence` が `acceptance` を満たすとorchが判定 ∧ `synced: true`。無条件信頼しない。

**done は workload state ではない**: 設計書v3 §5.2 の workload state machine に done は含まれず、worker→orchの完了申告として `event:{type:state, state:done}` envelope形式で扱う（orchestration層の信号）。orchはdoneを受信したらacceptance照合し、`command:close`送信 → workerは draining → terminated(cause:closed) へ進む。projector がこの遷移を検知し cache.workers[alias].task_status=done と activity.status=completed を自動同期する。

**cancelと自発doneの交差**:
- `event:state(done)` の `in_reply_to` が `command:cancel` を指していなければ自発done
- orchは自発doneを優先してacceptance照合し、cancelを無効化する（完成タスクを捨てる理由がない）

**done検証NG時の振る舞い**:
- acceptance不充足 → `command:answer` に理由を付けてworkerに差し戻す
- 差し戻しを受けてもworkerが改善できない場合 → `command:cancel` 送信または人間に通知

**クローズハンドシェイク**:
1. orchが「done検証OK ∧ synced:true ∧ escalated/stalled非該当」を確認
2. `command:close` を送信
3. workerは `event:state(draining)` → worker-sync → `event:identity` 再append → `event:state(terminated, cause:closed)` を送信
4. projector が cache.workers[alias].task_status=done + activity.status=completed を自動同期
5. orchは `event:state(terminated, cause:closed)` 受信後に `ow_close_worker(term_ref)` でセッションをクローズ
6. terminated が来なければ閉じずに人間に通知する

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
2. pingに無応答（heartbeat も復活しない）→ 後述のcrash推論経路に遷移 (cache.workers[alias].task_status=stalled が projector により付与される)
3. **自動failedおよび自動クローズはしない**。failedへの変更・強制クローズは人間判断

**watchdog対象外**:
- `escalated` 状態のworker（人間対話中はタイムアウト・クローズ対象外、設計書v3 §5.2の state machine参照）
- `terminated` 状態のworker（既に終了済み）

**ready未送信タイムアウト（loading 滞留）**: heartbeat が来ているかぎり crash 扱いしない。30秒以上 heartbeat 途絶した場合のみ crash 候補となる。長時間 loading（巨大コンテキスト読込・1Mコンテキストモデルのwarm-up等）でも heartbeat が続けばタイムアウトしない。

**spawning（ready前）タイムアウト**: spawning は cache.workers[alias].task_status の値 (§task_status 語彙 参照)。worker が `event:identity` または `event:state(loading)` を送る前に timeout_min 経過した場合は、spawn失敗の可能性が高い（cwd不在・aliasぶつかり・relay疎通断等）。`ow_recover` で pending_spawn として検出される。

## stagnation detector (Phase A: ow_sentinel)

watchdog が「死活 (heartbeat 途絶)」を見るのに対し、stagnation detector は「詰まり (heartbeat 継続中に state 遷移が起きない)」を見る。両者は責務分離・併走であり、stagnation が watchdog の前段に位置する fallback 仕組み (M#388 / D#2752)。

**監視対象 state と閾値**:

| 観測 state | 期待される遷移 | 閾値 | 検出する詰まり |
|---|---|---|---|
| `ready` | `working` (auto-assign 成功) | **60秒** | auto-assign 不発 |
| `draining` | `terminated` | **90秒** | close ハンドシェイク失敗 / worker-sync 詰まり |

`loading` / `working` / `blocked` / `escalated` は対象外 (`loading` は巨大 context warm-up を許容、他は heartbeat watchdog または人間判断側でカバー)。

**Phase A 実装**: `scripts/ow/sentinel.py` を別 process として orch と並走起動する (薄実装、Phase B = ow_service projector への push hook 統合で廃止予定)。

```bash
RELAY_URL=http://127.0.0.1:8765 python3 scripts/ow/sentinel.py <channel_code> &
```

sentinel は relay `/history` を 5秒間隔で polling し、state event / identity event から各 handle の現在 state を追跡する。閾値超え時に handle=`ow_sentinel` で stagnation event を append する。

**sentinel envelope 形式**:

```json
{"v":1, "kind":"event", "from":"ow_sentinel", "to":"orch", "task":"T<n>",
 "data":{"type":"stagnation", "target_handle":"<alias>",
         "target_state":"ready|draining", "elapsed_sec":<int>, "threshold_sec":<int>}}
```

**orch 側受信時の対処**: 既存 Monitor SSE 経路 (`recv_filter.py` は `to:"orch"` を通す) で受信される。`data.type=="stagnation"` を見たら以下を判断する:

- `target_state="ready"` → auto-assign 不発の疑い。`command:assign` を明示送信、または worker 側 SKILL 不発の調査
- `target_state="draining"` → close ハンドシェイク失敗の疑い。`command:close` 再送、または `ow_recover` で stalled_close 候補を確認

**重複抑止**: 同一 `(target_handle, target_state)` の閾値超過中は 1回だけ通知される。worker が次の state に遷移する (またはterminated になる) と sentinel 側で watch entry が解除され、再度 ready / draining に入った場合は新規 entry として再武装される。

**watchdog との発火順序**: 典型的には ready 滞留時 → 60秒 で stagnation 先行発火 → orch が対処判断 → 解決すれば watchdog 不要。orch 対処後も worker が動かず heartbeat も止まる場合は、別途 watchdog (90秒) が crash 推論する。

## モデル選択 (プロトコル制約のみ)

`command:assign` envelope では `model` が必須フィールド。値の選び方は合算版 playbook の §モデル選択 セクションに従う (一般版・特化版で上書きされうる)。

## 思考worker (effort指定) の spawn

深い議論・設計検討・調査向けに extended thinking を効かせたい worker は `ow_spawn_worker` の `effort` 引数を指定して spawn する。値は `high` / `xhigh` / `max` / `ultratink` の4段 (D#2599)。指定時の挙動:

- task_file 本文に思考トリガー語マーカーセクションが正規綴りで埋め込まれ、worker セッション全体が長考モードで動作する
- frontmatter に `effort: <値>` が残り、worker や ow_status から参照できる
- OW_TERMINAL=tmux のとき、通常workerが split-pane で同 window 内に並ぶのに対し、思考worker は `tmux new-window` で別タブに開かれる (D#2601)
- 対応 activity には `intent:thinking` タグも付与すること (D#2597)

**綴り規約 (D#2600)**: 本ドキュメント・skill・playbook・チャット出力では sentinel `ultratink` (意図的タイポ) を使う。orch セッション自身が読んだ時点で extended thinking が暴発するのを避けるため。worker 側 task_file 本文には正規綴り (h付き) が埋め込まれる (実装責務)。`ow_spawn_worker` 呼び出し時の `effort` 値も sentinel `ultratink` を渡してよい — ow_service 内で `_EFFORT_ALIASES` 経由で正規綴りに正規化され、frontmatter・本文マーカーは正規綴りで書き出される (D#2600 アライアス実装)。

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

4層構造 (§1 情報の4層構造と合算版 参照) のうち、運用流儀層の2つ:

| | 一般版 (Layer 3) | トピック特化版 (Layer 4) |
|---|---|---|
| 内容 | モデル選択目安、タイムアウト・worker同時数既定値、エスカレーション基準、報告頻度、自律実行範囲、SA分担基準、trouble-shooting 等 | topicで蓄積した対応知識 (PR運用、ユビキタス言語、worktree場所等のリポ固有ハウスルール) |
| 保存場所 | `skills/orch/playbook.md`（同梱・静的） | cc-memory material（タグ `playbook`+`domain:<>`、related=topic） |
| 更新 | プラグイン更新 | orchが新material+supersedes relationで版管理 |
| 章キー突合 | デフォルト章を提供 | 同名章があれば一般版を上書き |
| 参照優先 | 特化版がない項目のみ適用 | **特化版優先 (同名章では特化版で一般版を上書き)** |

orchは起動時に特化版最新を取得し、一般版と章名キーで突合して合算版を構築する (§1 自動マージ参照)。assign の `playbook` フィールドでは合算版から関連抜粋をworkerに渡す。

## identity 取得経路

orchはworkerおよび orch 自身の身元情報（alias、activity_id、model、cwd、session_id、terminated_at、cause、orch_cwd等）を取得する際は、必ず `ow_get_identity(channel, handle)` 経由で取得する（設計書v3 §6 / §8.3）。`ow_history` を自前パースして identity bundle を組み立てる経路は使わない。

理由:
- reducer が event:identity の最新エントリを返す責務を持つため、orch側で二重実装しない
- crash 推論（`cause: "crashed (inferred)"` / `"crashed-during-drain (inferred)"`）は reducer がメモリ上で付与する（DB不変、設計書v3 §9.2）。orch側で直接 history を見ると推論結果が得られない

ID別の取得関数:

| 関数 | 用途 |
|---|---|
| `ow_get_identity(channel, handle)` | 指定 handle の最新 identity bundle（+ crash推論結果）。`handle="orch"` で orch identity (orch_cwd / orch_activity_id 含む) を取得可 |
| `ow_list_identities(channel, alive_only)` | channel上の全 handle の identity リスト（alive_only=True で terminated 除外） |
| `ow_get_presence(channel, handle)` | SSE接続状態 + 最新 heartbeat 受信時刻から online/offline 推論 |
| `ow_get_workload_state(channel, handle)` | 指定 handle の最新 workload state（watchdog 閾値選定に使う） |

## crash 推論の cause lineup と派生反映

設計書v3 §5.2.2 / §9 の cause lineup に基づき、projector は以下のルールで cache.workers[alias] (task_status / state / cause) と activity.status を自動更新する (D#2750 マッピング規則)。`cause` 値は reducer 経由 (`ow_get_identity` 戻り値) で判定する (履歴に明示書き込まれる closed/cancelled/dead と、reducer がメモリ上で付与する crashed/crashed-during-drain)。

| cause | 発生条件 | task_status への反映 | activity.status への反映 | 補足 |
|---|---|---|---|---|
| `closed` | command:close 受領 → 正常退出 | `done` (acceptance満たし & synced済み) | `completed` | クローズハンドシェイク完了 |
| `cancelled` | command:cancel 受領 → 退出 | `cancelled` | `completed` + description先頭に[cancelled]経緯を追記 | |
| `dead` | loading 中の load 失敗 | `failed` | (触らず、`in_progress` 維持) | 人間に通知し復旧手段を判断（再spawn等） |
| `crashed (inferred)` | ready/working/blocked/escalated 中の heartbeat 途絶 | `stalled` | (触らず) | 自動failedにはしない、人間判断 |
| `crashed-during-drain (inferred)` | draining 中の heartbeat 途絶 | `stalled` | (触らず) | done評価は人間判断（acceptance確認＋手動同期検討） |

**自動 failed および自動クローズはしない**: heartbeat 途絶（crashed推論）は worker が長時間ツール実行中の場合にも発生しうるため、確実な異常証明とはならない。activities.status の `failed` 化は projector が触らず、人間判断に残す。

## crash復旧

1. **crash中**: worker側はheartbeat停止のみ。報告はrelay SQLite履歴に残存する
2. **再起動**: 人間が `ow_get_identity(channel, handle="orch")` で取得した `orch_cwd` と同じcwdで `/orch` を起動する。再開候補は `ow_recover_candidates()` (cache 由来) と `get_activities(tags=["orch-managed"], status="in_progress")` (cc-memory 由来) の和集合から選択する
3. **不在中メッセージ回収**: `ow_history(since=last_seen_msg_id)` から実行（冪等性が再処理を安全にする。専用復旧ロジック不要）。`last_seen_msg_id` は ow_status 戻り値で取得
4. **整合チェック (必須)**: `ow_recover(channel, topic_id)` を呼ぶ。relay履歴 since=0 再走査・cache OwState の **2 者突合** (cache 内部に identity 状態を集約済みのため、旧 3 者突合から縮減) + 自動修正を一括で行う:
   - **ghost_active** (cache.workers[alias] が active な task_status: working/blocked/escalated/draining & `ow_get_identity().cause` が terminal) → identity reducer の最新 cause から projector マッピングを再適用して cache + activity を自動再構築 (cause:closed→done+completed、cause:cancelled→cancelled+completed、cause:dead→failed、cause:crashed/crashed-during-drain→stalled)
   - **pending_spawn** (cache.workers[alias].task_status=spawning & identity未生成) → relay履歴に当該workerのevent:identity または event:state が**1件もない**場合は ow_recover は触らない（spawn racing回避）。orchは戻り値で残留spawning件数を把握し、経過時間が長い物は手動で `failed` 化判断する
   - **stalled_done** (cache.workers[alias].task_status=done/cancelled/failed & identity が alive) → `command:ping` 送信で素性照会
   - **orphans** (identity に登録があるが cache.workers 外の handle) → `command:ping` 送信で再リンク照会
   - 検証だけしたい時は `dry_run=True` で呼ぶ
   - 戻り値の `detected`/`applied`/`warnings` を確認し、ping応答は通常受信ループで処理
   - **cc-memory activity との突合は projector が自動同期するため特別扱い不要** (旧モデルでは ow_recover 対象外として明記したが、新モデルでは派生2が自動追従)
5. **worker復帰**: ow_recoverが送った `command:ping` への応答（worker の `event:state(<現在のstate>, note:"pong")`）を受信ループで処理する。応答が来ないworkerは前述のcrash推論経路に進む

**spawn前バリデーション**: `ow_spawn_worker` は内部で relay疎通・channel存在・cwd存在・alias重複の4点を自動チェックする。失敗時は `{"error": {"code": "SPAWN_PRECONDITION_FAILED", "warnings": [...]}}` が返るので、warningsを確認して原因を解消してから再spawnする。

**tmux分割表示**: `OW_TERMINAL=tmux` の環境では、orchが自身の `os.environ['TMUX_PANE']` を読んで `ow_spawn_worker(..., tmux_target_pane=<TMUX_PANE>)` に渡すと、orchペインと同じwindow内にworker paneが分割表示される（最初は右に30%水平、以降は右ペインを垂直分割で積む）。未指定時は従来の `ow-workers` 別sessionに新windowで起動する。MCPサーバープロセスのenvは起動時にフリーズするためサーバー側で参照できない、必ずクライアント側で読んで渡すこと。

## 複数orchの運用

- インスタンスキー = `topic_id`。channel・cache (`topic-<id>.json` を内部的に分離、外部からは隠蔽)・worker aliasすべて分離する
- relayサーバーはSPOF（全インスタンス共有）だが、recv.sh自動再接続+history回収で復旧は自動
- orch間調整はスコープ外（人間の采配）

## MCPツール一覧

| ツール | 用途 |
|---|---|
| `ow_send(channel, handle, body, needs_reply, in_reply_to)` | メッセージ送信（4xx即失敗、5xx/接続断のみ3回指数バックオフ）。bodyは `kind=command`/`event` envelope |
| `ow_history(channel, since, limit)` | 履歴pull（受信処理の本体・保険経路。SSE push本体添付が主軸、設計書v3 §3.3） |
| `ow_spawn_worker(alias, channel, cwd, model, task_title, acceptance, context, playbook, timeout_min, activity_id, topic_id, task_n, tmux_target_pane, effort)` | worker起動（内部で event:state(spawning, target_handle=alias) broadcast → task file 書き出し → adapter spawn → 安定 ID 返却。permission_mode は auto 固定）。OW_TERMINAL=tmux 時は orch自身の TMUX_PANE を `tmux_target_pane` に渡すと同 window 内に分割表示される |
| `ow_close_worker(term_ref)` | workerクローズ |
| `ow_status(channel, topic_id)` | cache + presence 統合ビュー (旧 queue+identity 統合) |
| `ow_recover(channel, topic_id, dry_run)` | crash復旧（cache × relay 履歴 2 者突合・ghost_active 自動再構築・stalled/orphan command:ping送信） |
| `ow_recover_candidates()` | 過去 orch 関与 topic_id リスト (cache 由来)。cc-memory `get_activities(tags=["orch-managed"], status="in_progress")` (cc-memory 由来) と和集合をとる |
| `ow_get_identity(channel, handle)` | 指定 handle の最新 identity bundle（+ crash推論結果）。`handle="orch"` で orch identity 取得可 |
| `ow_list_identities(channel, alive_only)` | channel上の全 handle の identity リスト |
| `ow_get_presence(channel, handle)` | SSE接続状態 + 最新 heartbeat 受信時刻から online/offline 推論 |
| `ow_get_workload_state(channel, handle)` | 指定 handle の最新 workload state（watchdog 閾値選定に使う） |
| `check_in(activity_id)` | cc-memoryのアクティビティcheck-in |
| `add_activity(...)` / `update_activity(...)` | アクティビティ管理（orch-managedタグ必須）。**update_activity で稼働状態 status (pending/in_progress/completed/cancelled) を直接変更するのは禁止** (§禁止事項 参照、description / tags 追加は許可) |
| `search(...)` | 特化プレイブック・過去エスカレーションログ検索 |
| `add_material(...)` | プレイブック版管理（supersedes relationで版管理） |

## Monitor起動（必須）

orchはSSEをMonitorツールで待ち受ける:

```
Monitor recv.sh --me orch (persistent)
```

`recv.sh` は `scripts/ow/recv.sh` にあり、1秒自動再接続付き。persistentモードでイベントドリブン待ち受けを行う。Monitorは手動起動のみ可能なため、スキル指示でClaude自身に起動させること。

## 禁止事項

§0 不変責務 と §アーキテクチャ原則 で扱いきれない、プロトコルレイヤの制約をここに集約する:

- escalated 状態workerへのwatchdog適用は禁止 (§watchdog 参照)
- **activity.status のうち稼働状態 (pending / in_progress / completed / cancelled) を `update_activity` で orch / worker が直接書き換えるのは禁止**。projector が relay terminal event を受信して自動更新する派生のため、人為的書き換えは projector マッピングの整合性を破壊する。description / tags の追加は許可
- **cache JSON ファイルへの外部書き込みは禁止** (projector 経路以外、cache 物理形式は ow_service 内部に隠蔽、`docs/architecture/components.md` 参照)
- **cache JSON ファイルを orch が削除して状態リセットすることは禁止** (relay events 真実源から projector が自動再構築するため不要、誤削除は projector run コストを増やすだけ)
- 同一channelへの複数orch参加は禁止 (1 channel = 1 topic 原則、§アーキテクチャ原則 参照)
- done/cancelled/failed に至っていないworkerの自動クローズは禁止 (§0 不変責務 にも明記)
- failed 設定および強制クローズの自律判断は禁止 (§0 不変責務 にも明記)
