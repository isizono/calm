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

1. **queue走査**: `<auto-memory>/orch/queue-t<topic_id>.md` を走査する。既存queueファイルがあれば再開候補として提示（crash引き継ぎ）。なければSessionStart注入のアクティビティ一覧からorch対象topicを選択（check-inスキルと同様のファジーマッチ可）
2. **relay疎通確認**: `ow_status()` を呼ぶ（ow_*ツール内蔵のensure-server処理が自動実行）。channelがなければ作成し、channel_codeをqueueのfrontmatterに記録する
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

**受信処理（SSEはベル、真実源は/history）**:
1. SSE（Monitor監視）は起床信号専用。届いたdata行の中身は処理に使わない
2. 起床したら `ow_history(since=last_seen_msg_id)` で未処理メッセージを全件pull
3. msg_id昇順にバッチ処理。**全ハンドラ冪等**（再処理が安全）
4. キュー更新後に `last_seen_msg_id = max(取得msg_id)`

**構造的制約**: orchがツール実行中はMonitor新着を処理できない（単一ターン制約）。報告はrelay履歴に永続するため失われない。

**人間向け責務**: 状態変化時のタスクボード短報に加え、タスクがavailableになったら「やる？」と**着手提案まで**行う。プロジェクトごとの権限設定の範囲内なら提案を待たず自走してよい。

## 通信プロトコル

### orch→worker: `cmd`

```json
{
  "v": 1,
  "kind": "cmd",
  "from": "orch",
  "to": "w-<alias>",
  "task": "T<n>",
  "verb": "assign | answer | cancel | close | ping",
  "data": {}
}
```

**verb別data**:
- `assign`: `{title, activity_id, topic_id, cwd, model（必須）, permission_mode, acceptance（必須）, context, playbook, timeout_min}` → needs_reply=true
- `answer`: `{answer}` または `{escalate: true}`
- `cancel`, `close`, `ping`: needs_reply=true

### worker→orch: `state`

```json
{
  "v": 1,
  "kind": "state",
  "from": "w-<alias>",
  "to": "orch",
  "task": "T<n>",
  "state": "<状態>",
  "data": {}
}
```

| state | 意味 | data | 補足 |
|---|---|---|---|
| `ready` | 起動完了 | `{session_id, alias, cwd}` | |
| `working` | 受諾・作業中 | `{phase, note}` | assignへの最初のworkingはin_reply_to必須 |
| `blocked` | 判断要請 | `{question, options, context_refs}` | needs_reply=true |
| `escalated` | エスカレーション文脈出力済み | `{report_md}` | watchdog対象外 |
| `done` | 完了（sync済み） | `{summary, evidence, synced, materials[], decision_proposals[], cancelled?}` | needs_reply=true。cancelへの応答時はin_reply_to必須。cancelled（boolean）: cmd:cancelへの応答doneの場合にtrue、自発doneではfalseまたは省略 |
| `closed` | クローズ受諾 | `{}` | |
| `dead` | 起動失敗等の自己申告 | `{message}` | |
| `fallback` | 人間対話モードへ移行宣言 | `{reason}` | |

**スレッド規約**: `in_reply_to` はrelay標準。存在しない親は400になるため送信前にmsg_id確定必須。`to` はbody内規約でサーバーはルーティングしない（宛先フィルタは受信側のローカル実装）。

## タスクキュー

### ファイルパスとMEMORY.md

- パス: `<auto-memory>/orch/queue-t<topic_id>.md`
- MEMORY.mdに1行ポインタを追記し「orchセッション専用」と明記する
- task fileは `<auto-memory>/orch/tasks/T<n>.json`

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
- model: sonnet / permission: acceptEdits
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

**「完了」の定義**: `done` の `evidence` が `acceptance` を満たすとorchが判定 ∧ `synced: true`。無条件信頼しない。

**cancelと自発doneの交差**:
- `done` の `in_reply_to` が `cancel` を指していなければ自発done
- orchは自発doneを優先してacceptance照合し、cancelを無効化する（完成タスクを捨てる理由がない）

**done検証NG時の振る舞い**:
- acceptance不充足 → `cmd:answer` に理由を付けてworkerに差し戻す
- 差し戻しを受けてもworkerが改善できない場合 → `failed` としてキューを更新し人間に通知する

**クローズハンドシェイク**:
1. orchが「done検証OK ∧ synced:true ∧ escalated/stalled非該当」を確認
2. `cmd:close` を送信
3. `state:closed` 受信後に `ow_close_worker(term_ref)` でセッションをクローズ
4. `state:closed` が来なければ閉じずに人間に通知する

## watchdog

**監視基準**: 「そのworkerからの最後の受信メッセージ」からの経過時間。

**タイムアウト処理**:
1. timeout_min超過 → `cmd:ping` を送信
2. pingに無応答 → queueを `stalled` に更新 + 人間に通知
3. **自動failedおよび自動クローズはしない**（長時間ツール実行中はping応答不能のため、無応答は異常の証明にならない）
4. failedへの変更・強制クローズは人間判断

**watchdog対象外**: `escalated` 状態のworker。escalated中はタイムアウト・クローズ対象外。

**ready未送信タイムアウト**: spawning状態でtimeout_minを超過した場合もwatchdogと同一基準（ping→無応答でstalled+人間通知）。

## モデル選択

assignの `model` は必須。一般プレイブック `skills/orch/playbook.md` のモデル選択目安表に従って選択する。

## エスカレーション

1. workerが `state:blocked`（needs_reply）を送信する
2. orchは**特化版→一般版プレイブックと過去エスカレーションログ**（`search(tags=["escalation","domain:..."])`）を参照してから判断する
3. 回答可能なら `cmd:answer {answer}` を送信する
4. 判断不能なら `cmd:answer {escalate: true}` → workerはエスカレーションフォーマット（質問/推奨と理由/選択肢/文脈要約/関連ID）で自セッションに出力し `state:escalated` を送信する
5. orchは人間に概要とworkerの場所（term_ref）を提示する
6. 人間がworkerセッションで直接解決する
7. workerがその場で記録（log必須・合意decisionも。タグ: `escalation`+`user-decision`+domain）
8. worker → `state:working {summary, decision_ids, log_ids}` で再開通知 → orchが特化版プレイブックを更新する（§プレイブック参照）

## プレイブック参照

| | 一般版 | トピック特化版 |
|---|---|---|
| 内容 | モデル選択目安、タイムアウト・worker同時数既定値、エスカレーション基準、報告頻度等 | topicで蓄積した対応知識 |
| 保存場所 | `skills/orch/playbook.md`（同梱・静的） | cc-memory material（タグ `playbook`+domain、related=topic） |
| 更新 | プラグイン更新 | orchが新material+supersedes relationで版管理 |
| 参照優先 | 特化版がない項目のみ | **特化版優先** |

orchは起動時に特化版最新を取得し、assignの `playbook` フィールドで関連抜粋をworkerに渡す。

## crash復旧

1. **crash中**: workerはフォールバック規則で待機。報告はrelay SQLite履歴に残存する
2. **再起動**: 人間が`orch_cwd`と同じcwdで `/orch` を起動し、queue再開候補から選択する
3. **不在中メッセージ回収**: `ow_history(since=last_seen_msg_id)` から実行（冪等性が再処理を安全にする。専用復旧ロジック不要）
4. **整合チェック**（必須）:
   - queue各タスクのstatus × presence（worker死活）× 履歴のready/doneを突合する
   - `spawning` 残留: readyの有無で「assigned再リンク」または「queued戻し」を判別する
   - queueに対応のないready（孤児worker）: `cmd:ping` で素性確認 → 再リンクまたはclose
   - **cc-memory activityも突合**: queue終端済み（done/cancelled/failed） × activity `in_progress` 残留を検出・修正する
5. **worker復帰**: 各workerにpingを送り、フォールバック復帰規則に従って復帰させる

**フォールバック復帰規則**:
- フォールバック後に人間入力ゼロ → orchの復帰メッセージで自動復帰
- フォールバック後に人間入力が一度でもあり → 復帰可否をユーザーに確認

## 複数orchの運用

- インスタンスキー = `topic_id`。channel・queueファイル・worker aliasすべて分離する
- relayサーバーはSPOF（全インスタンス共有）だが、recv.sh自動再接続+history回収で復旧は自動
- orch間調整はスコープ外（人間の采配）

## MCPツール一覧

| ツール | 用途 |
|---|---|
| `ow_send(channel, handle, body, needs_reply, in_reply_to)` | メッセージ送信（4xx即失敗、5xx/接続断のみ3回指数バックオフ） |
| `ow_history(channel, since, limit)` | 履歴pull（受信処理の本体） |
| `ow_spawn_worker(alias, channel, cwd, model, permission, task_title, acceptance, context, playbook, timeout_min, activity_id, topic_id, task_n)` | worker起動（spawning write-ahead→task file書き出し→アダプタ起動→安定ID返却） |
| `ow_close_worker(term_ref)` | workerクローズ |
| `ow_status(channel, topic_id)` | queue+presence統合ビュー |
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
