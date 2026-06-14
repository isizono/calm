# orch 一般プレイブック

owフレームワークのorch用一般プレイブック。トピック特化版プレイブック（cc-memory material、タグ `playbook`+domain）がある場合は**特化版を優先**し、本書は特化版がカバーしていない項目のみ適用する。

## モデル選択目安

assignの `model` は必須。タスクの性質に応じて以下の目安で選択する。

| タスクの性質 | 推奨モデル |
|---|---|
| 機械的作業（フォーマット変換、ファイル整理、定型テスト実行等） | haiku / sonnet |
| 通常実装（コーディング、テスト作成、バグ修正等） | sonnet / opus |
| 設計・複雑推論（アーキテクチャ設計、トレードオフ分析、根本原因調査等） | opus 以上 |

cc-memoryプラグイン付きのworkerはコンテキスト消費が大きいため、原則 **1Mコンテキスト版**を使う（D#2449）。CLIの `--model` 引数: `sonnet[1m]`、`claude-opus-4-7` 等。

**opus 4.8は使用禁止**（D#2476）。`opus` `opus[1m]` `opus-4-7` `opus-4-7[1m]` は無効なモデルIDまたは4.8に解決される。必ずフルID `claude-opus-4-7` を使う。

workerのSA（サブエージェント）にも同ルールを適用する。

## タイムアウト既定値

- `timeout_min` のデフォルト値: **60分**
- assignに明示的に指定しない場合は60分を適用する
- タスクの性質（大規模実装・長時間調査等）に応じてorchが上書き可能

## worker同時稼働数上限

- **最大3インスタンス**（暴走防止ハードリミット兼用）
- 実行中（in_progress/assigned/spawning/awaiting_verify）のworker数が3に達している場合、新規spawnは待機キューに留める
- escalatedはカウントに含める（セッションが存続しているため）
- stalledはカウントに含める（閉じていないため）

## チャンネル混在禁止

- **1 channel = 1 topic**。同一channelにorch/workerを複数topicで混在させない
- `orch` / `w-` はロールID予約プレフィックス。v1ではow用channelにリモート参加者（GitHubユーザー）を混在させない
- 誤って混在した場合: リモート参加者にJSONがそのまま流れるため即座にorchestratorが気づく。新channelを作成してqueueを更新する

## エスカレーション基準

workerがエスカレーション（`state:blocked` → `cmd:answer {escalate: true}`）を要請する典型的な場面:

| 類型 | 例 |
|---|---|
| 要件の曖昧さ | acceptanceの解釈が複数あり、どれが正しいか判断できない |
| 破壊的操作の確認 | 本番データの変更、リポジトリへのforce push等 |
| 予算・権限外の操作 | acceptance範囲を大幅に超える実装が必要になった |
| 外部サービス・API変更 | 依存ライブラリの破壊的変更や外部APIの仕様変更を発見した |
| 設計上の矛盾 | 実装中に設計書の矛盾・誤りを発見し、一人では判断できない |
| セキュリティ懸念 | 脆弱性の可能性があり、修正方針に人間の判断が必要 |

エスカレーションの際はフォーマット（質問/推奨と理由/選択肢/文脈要約/関連ID）で出力する。

orchが自力で判断できる場合は `cmd:answer {answer}` で直接回答し、エスカレーションに進めない。escalate指示は慎重に使う。

## 報告頻度

| タイミング | 内容 |
|---|---|
| 状態変化時（即時） | タスクボード短報（status変化、blocked/escalated発生、done検証結果等） |
| worker spawn時 | worker起動確認（alias、task名、model、cwd） |
| done検証完了時 | acceptance照合結果 + decision_proposalsの採否 |
| crash復旧完了時 | 整合チェック結果と復帰状況のサマリー |

定期的なポーリング報告は不要。Monitor発火ベースのイベントドリブン報告を原則とする。
