# Signal機能仕様: report_signal / get_signals / update_signal

## 0. 読み方

本ドキュメントは、cc-memory 自身への故障報告・使用感不満・矛盾検出・運用計測イベントを扱う Signal 機能（`report_signal` / `get_signals` / `update_signal` の3ツール）の仕様を、`src/main.py` の各ツール docstring から抽出してまとめたものである。ツール定義が正であり、本ドキュメントはその写しである。

## 1. report_signal — 記録

cc-memory 自身への故障報告・使用感不満・矛盾検出・運用計測イベントの統一入口。

### 1.1 kind（7種類、いずれか必須）

- `machine_error`: ツールエラー・hook 失敗・サーバー異常を観察した
- `friction`: cc-memory の使い勝手への不満・違和感（ユーザー発話由来を含む）
- `contradiction`: 既存記録（decision/material/log）と矛盾する結論を出した/検出した。
  - `refs` に矛盾の両側の id を必ず含めること
  - `summary` は「`<新しい結論の要旨>` ↔ `<矛盾する既存記録の title>`」形式
  - `detail` にはどちらの検証アンカー（コミット・日付・検証手段）が強いかの観察を書く
  - `context.resolution` に `existing_correct` / `new_correct` / `unresolved` を書く
- `precedent_miss` / `precedent_misapplied`: 判例参照の見落とし・誤類推の事後発覚。`context` に `missed_ids` / `cited_id` 等の規約キーを書く
- `boundary_case` / `rollback`: 運用上の案件記録。`summary` に PR 番号等の案件識別子を含める（dedup の集約単位を案件ごとに分けるため）

### 1.2 引数

| 引数 | 必須 | 内容 |
|---|---|---|
| `kind` | 必須 | 上記7種のいずれか |
| `summary` | 必須 | 1行要約（空文字不可） |
| `detail` | 任意 | traceback・引数ダイジェスト・自由記述 |
| `refs` | 任意 | `[{"type": "decision", "id": 123}, ...]` 形式の参照リスト |
| `context` | 任意 | kind ごとの構造化ペイロード |

### 1.3 返り値

- 成功時: `{"id": int, "deduped": bool, "occurrence_count": int}`
- 失敗時: `{"error": {"code": "VALIDATION_ERROR", "message": ...}}`

## 2. dedup（occurrence_count）

同一内容の再報告は自動で集約される。集約されると `occurrence_count` が加算され、`deduped: true` が返る。集約単位は kind によって異なり、`boundary_case` / `rollback` は `summary` に含めた案件識別子（PR番号等）で案件ごとに分けられる。

## 3. get_signals — 一覧・集計

`report_signal` で記録されたシグナルを一覧・集計する。

### 3.1 引数

| 引数 | デフォルト | 内容 |
|---|---|---|
| `status` | `"new"` | フィルタ対象の status（`"new"` \| `"triaged"` \| `"promoted"` \| `"dismissed"`）。null 指定で全 status 横断 |
| `kind` | null | フィルタ対象の kind。null 指定で全 kind 横断 |
| `limit` | 20 | 取得件数上限（最大100件） |
| `offset` | 0 | 取得開始位置（ページネーション用） |
| `include_stats` | false | true のとき kind×status のクロス集計と直近30日サマリを付与 |

### 3.2 返り値

- 成功時: `{"signals": [...], "total_count": int, "stats": {...}(include_stats時のみ)}`
- 失敗時: `{"error": {"code": ..., "message": ...}}`

各 signal の id は他の get 系ツールと同様 `id_raw` として返る（`id` キー自体は含まない）。`refs` 内の各要素の `id`・`promoted_id`・`context` 内にネストした参照（`missed_ids` 等）も同じ変換で対応する `{id_key}_raw` に退避される。`session_id`/`fingerprint` は記録側の内部相関・dedup専用フィールドのため含まない。

## 4. update_signal — トリアージ状態遷移

シグナルのトリアージ状態を遷移する（orch/親セッション専用）。

### 4.1 status遷移

`"new"` → `"triaged"` → `"promoted"` / `"dismissed"`

### 4.2 引数

| 引数 | 必須 | 内容 |
|---|---|---|
| `signal_id` | 必須 | 対象シグナルID |
| `status` | 必須 | 遷移先status（`"new"` \| `"triaged"` \| `"promoted"` \| `"dismissed"`） |
| `promoted_type` | 任意 | 昇格先エンティティ種別（`"topic"` \| `"activity"` \| `"decision"` \| `"log"` \| `"material"`）。省略時は既存の紐付けを変更しない |
| `promoted_id` | 任意 | 昇格先エンティティID。`promoted_type` と同時に指定する |

`promoted_type`/`promoted_id` は既存エンティティ（topic/activity/decision/log/material）への参照であり、両方指定時のみ実在チェックの上でリンクする。実体の作成は行わない（昇格実体は既存の add 系ツールで別途作成する）。

### 4.3 返り値

- 成功時: `{"signal": {...}}`（更新後の行。id は `id_raw` として返り、`session_id`/`fingerprint` は含まない。`refs`/`promoted_id`/`context` 内参照の `id_raw` 化も含め、`get_signals` と同じ整形）
- 失敗時: `{"error": {"code": ..., "message": ...}}`
