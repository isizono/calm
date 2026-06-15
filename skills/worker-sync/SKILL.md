---
name: worker-sync
description: owフレームワークのworkerが退場時に実行する簡易sync。ユーザー対話なしで作業経緯log・生データmaterialを記録し、decisionはorchへ提案する
---

# worker-sync

owフレームワークの **worker** が退場処理（`cmd:close` 受信時）に実行する記録スキル。
通常の `sync-memory` のworker版で、**会話相手（ユーザー）がいない**前提で設計されている。

`worker` スキルの §退場処理 から呼び出される。worker以外のセッションでは使わない（通常セッションは `sync-memory` を使う）。

## sync-memoryとの差分

| 項目 | sync-memory（通常） | worker-sync |
|------|---------------------|-------------|
| ユーザー対話（AskUserQuestion） | あり | **なし** |
| topic作成 | あり | **なし** |
| activity作成 | あり | **なし** |
| log記録 | あり | あり（実装経緯・障害・orchやり取り） |
| material記録 | あり | あり（生データ・related=担当activity） |
| decision記録 | 直接記録 | **原則しない**（decision_proposalsでorchに提案） |
| 棚卸し・remember・ふりかえり | あり | **なし** |

workerは記録ストア上をorchフローとして走るため、知識層への書き込み権限はorchに集約する。workerが勝手にtopic/activity/decisionを量産すると、orchの認知漏れとrelay履歴からの監査欠落を招く。

## 実行手順

### 前提の確認

このスキルは `worker` スキルの退場処理から、`cmd:close` 受信後・`state:closed` 送信前に呼ばれる。
task fileの `topic_id` / `activity_id` を記録の紐づけ先に使う。

### 1. log記録（必須） — add_logs

セッション中の作業経緯を **1件のログ** として記録する。`state:done` のsummaryより詳細に残す。次に来るセッション（同一workerの再spawnや、orchによる別worker割り当て）が経緯を引き継げることが目的。

- **title**: `worker <alias> T<task_n>: <タスク名>` の形式
- **content**: 以下を含める
  - 何をやったか（実装内容）
  - どういうアプローチ・設計判断を取ったか
  - 途中で迷った点・blockedやエスカレーションをした点とその結末
  - ハマったポイント・障害
  - orchとの重要なやり取り（差し戻し・追加指示等）
- **topic_id**: task fileの `topic_id`
- **tags**: `["worker-log", "domain:<topic_domain>"]` ＋ タスク内容に応じた素タグ
- **related**: 担当activity（`activity_id`）に紐づける

### 2. material記録（該当時のみ） — add_material

`state:done` の `materials[]` で報告済み以外に、保存すべき中間成果物（調査結果・分析・ドラフト・設計メモ等）があれば保存する。なければスキップする。

- 要約・整理はせず、**生データをそのまま** 保存する
- `related` で担当activity（`activity_id`）・topic（`topic_id`）に紐づける
- tags: `domain:<topic_domain>` ＋ 内容を表す素タグ

### 3. decisionの扱い — 原則 add_decisions しない

workerは決定事項を **直接記録しない**。`state:done` の `decision_proposals[]` でorchに提案済みのはずなので、ここで `add_decisions` を呼ぶ必要はない。

- done時に提案し忘れた決定事項があることに気づいた場合は、`add_decisions` ではなく、その内容を `state` メッセージ（`working` のnote等）でorchに伝える
- **例外**: エスカレーションで人間がこのworkerセッション内で **直接合意** した決定事項は、すでにエスカレーション処理時にworkerが記録済みのはずである（worker スキル §エスカレーション）。その記録漏れがあればここで `add_decisions`（タグ `escalation`・`user-decision`・`domain:<topic_domain>`）し、記録したD#を退場後のorch通知に含める

### 4. 完了

記録が完了したら `worker` スキルの退場処理に戻り、`state:closed` を送信する。

## 禁止事項

- ユーザーへの確認・質問（AskUserQuestion）をしない
- topic・activityを新規作成しない
- decisionを直接記録しない（エスカレーション例外を除く）
- 棚卸し・remember・ふりかえりをしない
