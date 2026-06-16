# orch 一般プレイブック

owフレームワークのorch用一般プレイブック。トピック特化版プレイブック（cc-memory material、タグ `playbook`+domain）がある場合は**特化版を優先**し、本書は特化版がカバーしていない項目のみ適用する。

## モデル選択

assignの `model` は必須。**claude-opus-4-7 のみ許可**（タスク性質による使い分けはしない）。

- 全タスク共通: `claude-opus-4-7`
- sonnet（`sonnet` / `claude-sonnet-4-6` / `[1m]` 付き等）はバリデーションで拒否される（credit消費が大きいため）
- haiku もバリデーションで拒否される
- opus 4.8（`opus-4-8` / `claude-opus-4-8`）は恒久禁止
- `opus` `opus-4-7` 等のエイリアスは `claude-opus-4-7` に正規化される

workerのSA（サブエージェント）にも同ルールを適用する。

## 思考worker (effort指定) の使い分け

`ow_spawn_worker(effort=...)` を指定すると思考worker (深い議論・設計検討・調査向け) として起動する。値: `high` / `xhigh` / `max` / `ultratink` (sentinel; ow_service内で正規綴りに alias される。D#2599, D#2600)。

| effort | 用途 |
|---|---|
| `high` | 通常より深く考えてほしい議論・設計検討 |
| `xhigh` | 多角的な比較検討・トレードオフ整理 |
| `max` | 大規模な設計判断・複雑な仕様確定 |
| `ultratink` (sentinel) | 最深長考。仕様の根本検討・抜本的設計レビュー等 |

挙動 (effort 指定時):
- task_file 本文に思考トリガー語マーカーが正規綴りで埋め込まれ、worker セッション全体が長考モードで動作する
- frontmatter に `effort: <値>` が残る
- OW_TERMINAL=tmux のとき、通常worker (split-pane) ではなく `tmux new-window` で別タブに開かれる (D#2601)
- 対応 activity には `intent:thinking` タグも付与する (D#2597)

orch 側ドキュメント・チャット出力では sentinel `ultratink` (意図的タイポ) で参照する。orch セッション自身に正規綴りが入ると extended thinking が暴発するため。`ow_spawn_worker` の `effort` 引数にも sentinel をそのまま渡してよい (ow_service が正規化する)。

## SAの活用

orchは調査・分析・検証のためにAgent/TaskツールによるSA（サブエージェント）を積極的に活用してよい。worker spawnとは独立した手段として、短期の情報収集や品質チェックをSAに委譲できる。

### orchがSAを使う典型的な場面

- **情報収集・調査**: 既存コード・設計記録の調査、関連ファイルの特定
- **done検証**: PR URL・CI結果の確認、acceptance照合の補助
- **品質チェック**: worker成果のコードレビュー、テスト結果サマリー

### SAのモデル選択

**SAも `claude-opus-4-7` 一択**。用途による使い分けはしない（sonnet/haiku は禁止、opus 4.8 も禁止）。

Agent ツールの `model` パラメータは `"opus"` enum を渡すと環境側で `claude-opus-4-7` 系に解決される。ただしフルID `claude-opus-4-7` を明示するのが確実。

### SAへの指示の書き方

- スコープと完了条件を明示する（何を調べて何を返すか）
- 調査系には `subagent_type: "Explore"` を活用できる
- バックグラウンドで走らせる場合は `run_in_background: true`（完了通知はharnessから届く）

## タイムアウト既定値

### worker assign の timeout_min

- `timeout_min` のデフォルト値: **60分**
- assignに明示的に指定しない場合は60分を適用する
- タスクの性質（大規模実装・長時間調査等）に応じてorchが上書き可能

### heartbeat 途絶 watchdog（設計書v3 §5.4.2）

orchが worker の生死を判定する基準は **heartbeat 途絶**。workload state の所要時間（`timeout_min`）とは別軸で監視する。`timeout_min` 超過は workload 上の予期外長期化、heartbeat 途絶は liveness 側のcrash候補（reducer 推論）として分けて扱う。

| 現在の workload state | heartbeat 周期 | watchdog 閾値（周期×3） |
|---|---|---|
| `loading` | 10秒 | **30秒** |
| `ready` / `working` / `blocked` / `draining` | 30秒 | **90秒** |
| `escalated` | 監視対象外 | — |
| `terminated` | 監視対象外（既に終了済み） | — |

途絶検知時のorchの行動: `command:ping` 送信 → 無応答かつ heartbeat 復活なし → reducer の `cause` を参照して queue を更新（cause lineup は orch SKILL.md §crash推論 参照）。

**自動 failed / 自動クローズはしない**。failed への変更・強制クローズは人間判断（heartbeat 途絶は worker が長時間ツール実行中の場合にも発生しうるため、確実な異常証明にならない）。

### watchdog 対象外の state

- `escalated`: 人間対話中はタイムアウト・クローズ対象外
- `terminated`: 既に終了済み

## worker同時稼働数上限

- **最大5インスタンス**（暴走防止ハードリミット兼用）
- 実行中（in_progress/assigned/spawning/awaiting_verify）のworker数が5に達している場合、新規spawnは待機キューに留める
- escalatedはカウントに含める（セッションが存続しているため）
- stalledはカウントに含める（閉じていないため）

## チャンネル混在禁止

- **1 channel = 1 topic**。同一channelにorch/workerを複数topicで混在させない
- `orch` / `w-` はロールID予約プレフィックス。v1ではow用channelにリモート参加者（GitHubユーザー）を混在させない
- 誤って混在した場合: リモート参加者にJSONがそのまま流れるため即座にorchestratorが気づく。新channelを作成してqueueを更新する

## エスカレーション基準

workerがエスカレーション（`event:state(blocked)` → `command:answer {escalate: true}`）を要請する典型的な場面:

| 類型 | 例 |
|---|---|
| 要件の曖昧さ | acceptanceの解釈が複数あり、どれが正しいか判断できない |
| 破壊的操作の確認 | 本番データの変更、リポジトリへのforce push等 |
| 予算・権限外の操作 | acceptance範囲を大幅に超える実装が必要になった |
| 外部サービス・API変更 | 依存ライブラリの破壊的変更や外部APIの仕様変更を発見した |
| 設計上の矛盾 | 実装中に設計書の矛盾・誤りを発見し、一人では判断できない |
| セキュリティ懸念 | 脆弱性の可能性があり、修正方針に人間の判断が必要 |

エスカレーションの際はフォーマット（質問/推奨と理由/選択肢/文脈要約/関連ID）で出力する。

orchが自力で判断できる場合は `command:answer {answer}` で直接回答し、エスカレーションに進めない。escalate指示は慎重に使う。

## 報告頻度

| タイミング | 内容 |
|---|---|
| 状態変化時（即時） | タスクボード短報（status変化、blocked/escalated発生、done検証結果等） |
| worker spawn時 | worker起動確認（alias、task名、model、cwd） |
| done検証完了時 | acceptance照合結果 + decision_proposalsの採否 |
| crash復旧完了時 | 整合チェック結果と復帰状況のサマリー |

定期的なポーリング報告は不要。Monitor発火ベースのイベントドリブン報告を原則とする。
