<!-- ccm-doc-sync
watch-tags: domain:cc-memory
watch-direction: true
watch-migrations: false
last-synced: 2026-07-04
last-synced-migration: 0048
-->

# cc-memory MCPツール仕様書 v0

## 0. 読み方

このドキュメントはcc-memoryが提供するMCPツールの引数・返り値・エラー仕様を網羅的にまとめたものである。

- **v0であり、凍結を目的としない**。レビュー・議論のたたき台として位置づける。最終的な真実は `src/main.py` の `@mcp.tool` デコレータ付き関数とそのdocstringに置く。
- 並行して `docs/spec/openapi.yaml` を機械可読版として用意している。CIや外部ツールから参照する場合はyaml側を使う。
- 本書は人間向けの俯瞰用。粒度は「読者がツールを呼び出せる」レベルに留め、内部実装には踏み込まない。
- ツール名・引数名・型名は外部APIとして直接参照されるためそのまま英語表記で残す。本文は常体（だ・である調）。
- cc-memory内部ID（D#/M#/A#/L#/T#）は本文では使わず、論理名（decision/material/activity/log/topic）で書く。

---

## 1. ツール一覧

全43ツール。カテゴリ別に一覧する。

### 1.1 記録系（add系）

| ツール | 概要 |
| --- | --- |
| `add_topic` | 新しい議論トピックを追加する |
| `add_logs` | 議論ログを一括追加する（最大10件） |
| `add_decisions` | 決定事項を一括記録する（最大10件） |
| `add_activity` | アクティビティを追加する（デフォルトで check-in 同時実行） |
| `add_material` | 資材を追加する |
| `add_habit` | 振る舞い（habit）を登録する |
| `add_relation` | エンティティ間リレーションを追加する |
| `add_pin` | pin を追加する |

### 1.2 取得系（get系）

| ツール | 概要 |
| --- | --- |
| `get_topics` | トピック一覧をフィルタ付きで取得する |
| `get_logs` | 指定エンティティの議論ログを取得する |
| `get_decisions` | 指定エンティティの決定事項を取得する |
| `get_activities` | アクティビティ一覧をフィルタ付きで取得する |
| `get_material` | 資材の全文を取得する |
| `get_habits` | 登録済み振る舞い一覧を取得する |
| `get_by_ids` | search結果の詳細を type+id 指定で取得する |
| `get_map` | リレーショングラフを走査し到達可能カタログを返す |
| `get_timeline` | トピックまたはアクティビティの時系列を返す |
| `get_config` | 現在の設定値を返す |

### 1.3 更新系（update系）

| ツール | 概要 |
| --- | --- |
| `update_activity` | アクティビティのstatus/title/description/tagsを更新する |
| `update_material` | 資材のcontent/title/tags/sourceを更新する |
| `update_habit` | 振る舞いを更新する（content/active） |
| `update_tag` | タグのnotes/canonical/rename/descriptionを更新する |
| `retract` | 決定事項・ログ・資材を論理削除する（undoで復帰可能だが検索インデックスは再登録されない） |

### 1.4 検索系

| ツール | 概要 |
| --- | --- |
| `search` | 横断検索（FTS5 trigram + ベクトル ハイブリッド） |
| `search_tags` | タグをキーワード検索する |
| `analyze_tags` | タグ共起分析（PMI/クラスタ/孤児/重複候補） |

### 1.5 関係系・pin系

| ツール | 概要 |
| --- | --- |
| `add_relation` / `remove_relation` | エンティティ間リレーションの追加・削除 |
| `add_pin` / `remove_pin` | pin の追加・削除 |
| `get_map` | リレーショングラフ走査 |

### 1.6 アクティビティ操作系

| ツール | 概要 |
| --- | --- |
| `check_in` | アクティビティにcheck-inして関連情報を集約取得する |

### 1.7 ow系（orch/worker メッセージング）

| ツール | 概要 |
| --- | --- |
| `ow_send` | ow channelにメッセージを送信する |
| `ow_history` | ow channel履歴を取得する |
| `ow_spawn_worker` | workerセッションを起動する |
| `ow_close_worker` | workerセッションをクローズする |
| `ow_spawn_dispatcher` | dispatcherセッションを起動する（既存があればcascade kill後にspawn） |
| `ow_close_dispatcher` | dispatcherセッションをkillし、紐づくworker poolもcascade killする |
| `ow_status` | queueサマリ + presence の合成ビューを返す |
| `ow_recover` | orch crash後の queue × relay × presence 整合チェック・自動修正 |

### 1.8 その他

| ツール | 概要 |
| --- | --- |
| `roll_dice` | ダイスを振る（デフォルト1d10） |

### 1.9 エクスポート系

| ツール | 概要 |
| --- | --- |
| `export_material` | 資材をmd形式のファイルとしてcc-memory外に出力する |

### 1.10 シグナル系（signal_events）

cc-memory自身の故障・使用感不満・矛盾検出・運用計測イベントの記録先。`add_logs` / `add_decisions` とは異なり合意不要の生の観測データであり、専用テーブル（`signal_events`）に記録される。

| ツール | 概要 |
| --- | --- |
| `report_signal` | cc-memory自身の故障・使用感不満・矛盾検出・運用計測イベントを記録する（orch/dispatcher/workerいずれからも呼べる） |
| `get_signals` | 記録されたシグナルを一覧・集計する |
| `update_signal` | シグナルのトリアージ状態を遷移する（orch専用） |

---

## 2. 各ツール詳細

ツールごとに「引数表 / 返り値 / エラー / 関連スキル・前提」を整理する。

### 2.1 add_topic

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| title | string | yes | - | トピックのタイトル |
| description | string | yes | - | トピックの説明 |
| tags | list[string] | yes | - | タグ配列。1個以上。`domain:` タグ必須 |
| related | list[RelatedRef] | no | null | `[{"type": "topic"|"activity", "ids": [int, ...]}]` |

**返り値**: `{topic: Topic, similar_topics: [{topic_id, title, distance}, ...]}`。レスポンスのtag_notesに該当タグのnotesが注入される場合がある。
**エラー**: `CONSTRAINT_VIOLATION`、`DATABASE_ERROR`、入力検証エラー（タグ未指定等）。
**関連**: similar_topics は重複トピック防止のヒント。

### 2.2 add_logs

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| items | list[object] | yes | - | 最大10件。各要素は `{topic_id, content, title?, tags?}` |

**返り値**: `{created: [...], errors: [{index, error}, ...]}`。
**エラー**: 個別アイテム単位でerrorsに格納される。最大件数超過は全体エラー。
**関連**: 決定に至る経緯のスナップショット。`retract` で論理削除可能。

### 2.3 add_decisions

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| items | list[object] | yes | - | 最大10件。各要素は `{topic_id, decision, reason, title?, tags?, propagate_to?}` |

**返り値**: `{created: [...], errors: [...], hints?: [string]}`。created の各要素には `related_decisions`（同topic内の類似decision上位3件）が付く。hintsはharness_serviceからの推奨行動。
**propagate_to**: `{type: "habit" | "tag_note", content: string, tag?: string}`。tagはtype="tag_note"のとき必須。
**関連**: `add_habit` / `update_tag(notes=...)` と連動。

### 2.4 get_topics

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| tags | list[string] | no | null | AND条件。未指定は全件 |
| limit | int | no | 10 | 取得件数上限 |
| offset | int | no | 0 | ページネーション |
| since | string | no | null | ISO日付（以降） |
| until | string | no | null | ISO日付（以前） |

**返り値**: `{topics: [Topic], total_count: int, tag_notes?: [TagNote]}`。

### 2.5 get_logs / get_decisions

両者とも同じ引数構造を持つ。

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| entity_type | string | yes | - | `"topic"` または `"activity"` |
| entity_id | int | yes | - | 対象エンティティID |
| start_id | int | no | null | ページネーション用 |
| limit | int | no | 30 | 最大30件 |
| include_retracted | bool | no | false | trueで取り消し済みも含む |

**返り値**: `get_logs` は `{logs: [DiscussionLog]}`、`get_decisions` は `{decisions: [Decision], total_count: int, truncated: bool}`。`total_count` は対象decisionの総件数（limit/start_idの影響を受けない）、`truncated` は limit/start_id で後続を打ち切ったとき true（続きのページが存在する）。
**特殊挙動**: entity_type="activity" の場合、related topics経由で集約される。

### 2.6 search

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| keyword | string \| list[string] | yes | - | 2文字以上。配列でAND。完全一致検索(FTS5)は3文字以上のみ発動、2文字はベクトル検索のみ |
| tags | list[string] | no | null | AND条件 |
| entity_type | string | no | null | `topic`/`decision`/`activity`/`log`/`material` |
| limit | int | no | 10 | 最大50 |
| offset | int | no | 0 | ページネーション |
| keyword_mode | string | no | "and" | `"and"` または `"or"` |
| include_details | bool | no | false | 上位10件にdetails自動添付 |
| domain | string | no | null | `tags=["domain:{domain}"]` にマージ |
| date_after | string | no | null | YYYY-MM-DD ほか |
| date_before | string | no | null | 同上 |
| include_retracted | bool | no | false | 取り消し済み含む |

**返り値**: `{results: [SearchHit]}`。scoreは0〜1正規化（1.0=全ソース1位、片方ヒットは最大0.5）。0.4以上=高関連、0.15〜0.4=中、0.15未満=低の目安。snippetでなく全文が必要な場合は結果のtype+idを`get_by_ids`に渡す。
**実装**: FTS5 trigram + ベクトル検索のRRF統合。

### 2.7 get_by_ids

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| items | list[{type, id}] | yes | - | 最大20件 |

**返り値**: `{results: [{type, id, data}, ...]}`。2段階リード（searchで概要→get_by_idsで全文）の後半に位置する。materialは`data`に`content`/`source`が含まれ、追加で`get_material`を呼ぶ必要はない。

### 2.8 search_tags

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| query | string | yes | - | タグ名部分一致 + ベクトル検索 |
| namespace | string | no | null | `"domain"` / `"intent"` / `""` (素タグ) |
| include_notes | bool | no | false | trueでnotesも返す |
| limit | int | no | 20 | 取得件数上限 |

**返り値**: `{tags: [{tag, namespace, score, notes?}]}`。

### 2.9 update_tag

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| tag | string | yes | - | 対象タグ |
| notes | string | no | null | 教訓・運用ルール（全文置換） |
| canonical | string | no | null | エイリアス先。`""` で解除 |
| rename | string | no | null | 新しいタグ名 |
| description | string | no | null | 短い説明文（最大100文字） |

**制約**: notes/canonical/rename/description は相互排他。少なくとも1つ指定。canonical連鎖（エイリアスのエイリアス）は禁止。notes付きタグはエイリアス化不可。

### 2.10 analyze_tags

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| domain | string | no | null | domainフィルタ |
| include_domain_tags | bool | no | false | trueでdomain:タグも分析対象 |
| focus_tag | string | no | null | 特定タグにフォーカス |
| min_usage | int | no | 2 | 孤児判定閾値 |
| top_n | int | no | 20 | co_occurrences の返却件数 |

**返り値**: `{co_occurrences, clusters, orphans, suspected_duplicates}`。

### 2.11 add_activity

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| title | string | yes | - | タイトル |
| description | string | yes | - | 詳細説明 |
| tags | list[string] | yes | - | 1個以上。`domain:` と `intent:` 必須 |
| related | list[RelatedRef] | no | null | 関連エンティティ |
| check_in | bool | no | true | 作成後にcheck_inを実行するか |

**返り値**: 作成されたアクティビティ情報。check_in=Trueの場合は `check_in_result` を含む。

### 2.12 get_activities

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| tags | list[string] | no | null | AND条件 |
| status | string | no | "active" | `active`/`pending`/`in_progress`/`completed`/`snoozed`/`shelved` |
| limit | int | no | 5 | 取得件数上限 |
| since | string | no | null | ISO日付（以降） |
| until | string | no | null | ISO日付（以前） |

**返り値**: `{activities: [Activity], total_count: int}`。statusの`active`は pending+in_progress のエイリアス（snoozed/shelvedは含まない）。
**副作用**: 呼び出し時、updated_atがSNOOZE_DURATION_DAYS（デフォルト3日）を超過したsnoozedアクティビティをpendingへ一括自動復活させる。

### 2.13 update_activity

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| activity_id | int | yes | - | 対象ID |
| status | string | no | null | 上記5値のいずれか |
| title | string | no | null | 新しいタイトル |
| description | string | no | null | 新しい説明 |
| tags | list[string] | no | null | 全置換。1個以上 |

**副作用**: snoozed状態のアクティビティにstatusを指定せず他フィールドのみ更新すると、自動的にstatus="pending"へ復活する。

### 2.14 add_material

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| title | string | yes | - | タイトル |
| content | string | yes | - | 本文（マークダウン推奨）。先頭1-2文は要約として書く |
| tags | list[string] | yes | - | 1個以上 |
| source | string | yes | - | データ出自（ユーザー発言/公式ドキュメント/コード調査 等） |
| related | list[RelatedRef] | no | null | 関連エンティティ |

### 2.15 update_material

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| material_id | int | yes | - | 対象ID |
| content | string | no | null | 全体置換 |
| title | string | no | null | 新しいタイトル |
| tags | list[string] | no | null | 全置換 |
| source | string | no | null | 新しい出自 |

**制約**: 最低1つは指定する。contentは部分更新やappendではなく全体置換。

### 2.16 get_material

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| material_id | int | yes | - | 資材のID |

**返り値**: 資材の全文。

### 2.17 export_material

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| material_id | int | yes | - | 資材のID |
| dest_path | string | no | null | 出力先パス。省略/既存ディレクトリ/ファイルパスで振り分ける。`~/cc-memory-export` 配下でなければならない |

**返り値**: 成功時 `{path, overwritten, material_id, title}`。失敗時 `{error: {code: "NOT_FOUND" | "VALIDATION_ERROR" | "IO_ERROR" | "DATABASE_ERROR", message}}`。
**動作**: 資材を YAML frontmatter + h1 + content 形式の md ファイルとして出力する。frontmatter に資材IDを保持し往復同期の鍵とする。書き込み先は `~/cc-memory-export` 配下に限定（配下外・シンボリックリンク経由の脱出は VALIDATION_ERROR で拒否）。上書き確認はせず既存ファイルは無警告で上書きする（`overwritten` で通知）。

### 2.18 check_in

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| activity_id | int | yes | - | アクティビティID |

**返り値**: `{coverage, activity, related_topics, related_activities, pinned, tag_notes, materials, recent_decisions, latest_log, logs, catalog, summary}`。
**副作用**: statusがin_progress以外なら自動的にin_progressに更新。
**呼び出し基準**: 既存アクティビティに関連する作業を始めるとき。summaryフィールドはそのまま出力することが推奨される。

### 2.19 add_relation / remove_relation

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| source_type | string | yes | - | `topic`/`activity`/`material`/`decision`/`log` |
| source_id | int | yes | - | 起点ID |
| targets | list[RelatedRef] | yes | - | ターゲット |
| relation_type | string | no | "related" | `related`/`depends_on`/`supersedes`/`belongs_to` |

**制約**: `depends_on` はactivity同士のみ、`supersedes` はdecision同士のみ有効。
**親帰属の自動書き込み**: 子（activity/material/decision/log）→topicの関連付けは、`relation_type` が `related`（デフォルト）または明示的な `belongs_to` のときに限り `belongs_to` として書き込まれる。`depends_on`/`supersedes` を指定するとtargetがtopicのためバリデーションエラーになり何も書き込まれない。この帰属はget_decisions/get_timeline/check_inのトピック帰属集計やget_by_idsのtopic_id解決の基盤になっており、`remove_relation` で `related`/`belongs_to` を指定すると帰属関係ごと削除される。
**返り値**: `{added: int}` または `{removed: int}`。重複は冪等。

### 2.20 get_map

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| entity_type | string | yes | - | 起点の種別 |
| entity_id | int | yes | - | 起点ID |
| min_depth | int | no | 0 | 0=起点自身を含む |
| max_depth | int | no | 2 | 上限10 |

**返り値**: `{entities: [{type, id, title, tags, depth}], total_count: int}`。decision/logノードは経由ノードとして使うが、返却カタログにはtopic/activity/materialのみ含まれる。

### 2.21 add_habit / get_habits / update_habit

- `add_habit(content: string) -> dict`: habitを登録。SessionStart時に全件注入される（セッション途中の登録は次セッション以降に有効）。
- `get_habits() -> dict`: 登録済みhabit一覧。
- `update_habit(habit_id: int, content?: string, active?: bool) -> dict`: active=Falseで無効化。

### 2.22 add_pin / remove_pin

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| source_type | string | yes | - | `tag`/`activity`/`topic`/`decision`/`log`/`material` |
| source_ref | int \| string | yes | - | ID整数、tag種別のみ文字列可（"domain:cc-memory"） |
| target_type | string | yes | - | 同上 |
| target_ref | int \| string | yes | - | 同上 |

**制約**: 自己参照（source==target）は拒否。重複追加は冪等。
**エラー**: source/targetが存在しないとき `NOT_FOUND`。
**返り値**: 追加時は `{source_type, source_id, target_type, target_id}`、削除時は `{removed: int}`。

### 2.23 retract

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| entity_type | string | yes | - | `"decision"` / `"log"` / `"material"` |
| ids | list[int] | yes | - | 対象IDリスト |
| undo | bool | no | false | trueで取り消しを戻す（un-retract） |

**動作**: 論理削除。検索・取得でデフォルト除外される（include_retracted=Trueで含められる）。retract時はsearch_index/FTS/vecインデックスからも物理削除される。
**undoの不可逆性**: undo（un-retract）はretracted_atをNULLに戻すだけで、検索インデックスへの再登録は行わない。un-retract後に再び検索でヒットさせたい場合はadd_decisions/add_logs/add_materialで新規に追加し直す必要がある。

### 2.24 get_timeline

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| topic_id | int | no | null | activity_idと排他 |
| activity_id | int | no | null | topic_idと排他 |
| entity_types | list[string] | no | null | `decision`/`log`/`material` のサブセット |
| before | string | no | null | ページネーション用カーソル（ISO 8601） |
| limit | int | no | 50 | 最大100 |
| order | string | no | "desc" | `"desc"` または `"asc"` |

### 2.25 get_config

引数なし。返り値: `{heartbeat_timeout, in_progress_limit, pending_limit, recency_decay_rate, sync_disable_retrospective, sync_policy, snapshot_interval_hours, snapshot_max_count, snapshot_anomaly_threshold}`。スキルが環境変数ベースの設定を参照するときに使う。

### 2.26 roll_dice

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| sides | int | no | 10 | サイコロ面数 |

**返り値**: `{result: int}`。

### 2.27 ow_send

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| channel | string | yes | - | channelコード |
| handle | string | yes | - | 送信者handle |
| body | object | yes | - | ow固有JSON。`{v:1, kind:"command"|"event", ...}` |
| needs_reply | bool | no | false | 返信を期待するか |
| in_reply_to | int | no | null | 返信先のmsg_id |

**返り値**: `{msg_id: int}`。
**エラー処理**: 4xxは即失敗、5xx/接続断のみ3回指数バックオフ。

### 2.28 ow_history

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| channel | string | yes | - | channelコード |
| since | int | no | 0 | このmsg_idより大きいものを返す |
| limit | int | no | 100 | 最大取得件数 |

**返り値**: `{messages: [{msg_id, handle, body, ...}]}`。SSEは起床信号専用で、実体取得はこちらで行う。

### 2.29 ow_spawn_worker

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| alias | string | yes | - | workerのhandle（例: "w-a"） |
| channel | string | yes | - | channelコード |
| cwd | string | yes | - | workerの作業ディレクトリ |
| model | string | yes | - | `claude-opus-4-7` のみ許可 |
| task_title | string | no | "" | タスクタイトル |
| acceptance | string | no | "" | 完了条件 |
| context | string | no | "" | タスクコンテキスト |
| playbook | string | no | "" | プレイブック抜粋 |
| timeout_min | int | no | 60 | タイムアウト（分） |
| activity_id | int | no | null | 対応するアクティビティID |
| topic_id | string | no | null | 対応するトピックID |
| task_n | int | no | 1 | タスク番号 |
| tmux_target_pane | string | no | null | tmux分割表示用の基準pane ID |
| effort | string | no | null | `high`/`xhigh`/`max`/`ultrathink` |

**制約**: modelは `claude-opus-4-7` 固定。sonnet/haiku/opus-4-8 はバリデーションで拒否される。
**返り値**: 通常時 `{term_ref, task_file, spawning: "ok", alias}`。manualフォールバック時 `{command, manual: True, task_file, alias}`。

### 2.30 ow_close_worker

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| term_ref | string | yes | - | 安定ID（tmux pane ID 等） |

**返り値**: `{closed: True, term_ref}` または `{manual: True, message}`。

### 2.31 ow_spawn_dispatcher

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| channel | string | yes | - | channelコード（handleに `d-` prefixで組み込まれる） |
| cwd | string | yes | - | dispatcherセッションの作業ディレクトリ |
| model | string | yes | - | `claude-opus-4-7` のみ許可 |
| tmux_target_pane | string | no | null | tmux分割表示用の基準pane ID |

**制約**: modelは `claude-opus-4-7` 固定。sonnet/haiku/opus-4-8 はバリデーションで拒否される。channelに既存dispatcherがあればcascade kill（既存dispatcher + 紐づくworker pool全員）してから新規spawnする。health check や idempotent reject は行わない。
**返り値**: 成功時 `{term_ref, bundle_msg_id, spawning: "ok", alias}`。失敗時 `{error: {code, message, ...}}`。

### 2.32 ow_close_dispatcher

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| channel | string | yes | - | channelコード |

**動作**: dispatcher（handle=`d-{channel}`）をkillし、紐づくworker poolもcascade killする。dispatcherが存在しない場合はエラーを返す（no-op successは採らない）。graceful shutdownは試みず即process kill。
**返り値**: 成功時 `{closed: True, channel, dispatcher_handle, killed_workers, failed_workers}`。失敗時 `{error: {code, message}, killed_workers, ...}`。

### 2.33 ow_status

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| channel | string | yes | - | channelコード |
| topic_id | string | no | null | queueファイル特定用 |

**返り値**: `{tasks, presence, frontmatter, summary}`。queueの論理状態とrelayのpresence（物理接続）を統合した単一ビュー。

### 2.34 ow_recover

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| channel | string | yes | - | channelコード |
| topic_id | string | yes | - | queue-t<topic_id>.md 特定用 |
| dry_run | bool | no | false | trueなら検出のみ |

**返り値**: `{detected: {ghost_active, pending_spawn, stalled_done, orphans}, applied, warnings, presence, reconstructed_max_msg_id, dry_run}`。orch crash後の queue × relay × presence 整合チェック・自動修正に用いる。

### 2.35 report_signal

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| kind | string | yes | - | `machine_error` / `friction` / `contradiction` / `precedent_miss` / `precedent_misapplied` / `boundary_case` / `rollback` の7種のいずれか |
| summary | string | yes | - | 1行要約（空文字不可） |
| detail | string | no | null | traceback・引数ダイジェスト・自由記述 |
| refs | list[{"type", "id"}] | no | null | 参照リスト。`contradiction` では矛盾の両側のidを必須とする |
| context | object | no | null | kindごとの構造化ペイロード（例: `contradiction` は `resolution`、`precedent_miss` は `missed_ids`） |

**返り値**: 成功時 `{id: int, deduped: bool, occurrence_count: int}`、失敗時 `{error: {code: "VALIDATION_ERROR", message: ...}}`。
**動作**: 同一 `fingerprint`（kind+source+正規化summaryのハッシュ）を持つ未トリアージ行が既にあれば新規行を作らず `occurrence_count` を加算する（dedup）。
**関連**: MCPツール例外の middleware 捕捉やhooksのtop-level捕捉からも自動的に呼ばれる（`source` がそれぞれ `tool:*` / `hook:*` になる）。

### 2.36 get_signals

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| status | string \| null | no | "new" | `new`/`triaged`/`promoted`/`dismissed`。nullで全status横断 |
| kind | string \| null | no | null | フィルタ対象のkind。nullで全kind横断 |
| limit | int | no | 20 | 最大100 |
| offset | int | no | 0 | ページネーション |
| include_stats | bool | no | false | trueでkind×statusのクロス集計と直近30日サマリを付与 |

**返り値**: `{signals: [...], total_count: int, stats?: {by_kind_status, last_30d}}`。

### 2.37 update_signal

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| signal_id | int | yes | - | 対象シグナルID |
| status | string | yes | - | 遷移先status（`new`/`triaged`/`promoted`/`dismissed`） |
| promoted_type | string | no | null | 昇格先エンティティ種別（`topic`/`activity`/`decision`/`log`/`material`） |
| promoted_id | int | no | null | 昇格先エンティティID。promoted_typeと同時に指定する |

**返り値**: `{signal: {...}}`（更新後の行）。
**動作**: リンクを張るだけで昇格実体は作らない（実体の作成は既存のadd系ツールで行う）。orch専用（capability_matrixでdispatcher/workerは拒否）。

---

## 3. 共通エンティティ型

cc-memoryが扱うエンティティの内部表現。詳細スキーマは `docs/spec/db-schema.md`（並行作成中）を参照する。本書では論理構造のみ示す。

### 3.1 Topic
- `topic_id: int`
- `title: string`
- `description: string`
- `tags: list[string]`
- `created_at: string`、`updated_at: string`

### 3.2 Decision
- `decision_id: int`
- `topic_id: int`
- `title: string`、`decision: string`、`reason: string`
- `tags: list[string]`
- `related_decisions: [{id, title, distance}]`（add_decisions返り値のみ）
- `retracted_at: string | null`

### 3.3 DiscussionLog
- `log_id: int`
- `topic_id: int`
- `title: string`、`content: string`
- `tags: list[string]`
- `retracted_at: string | null`

### 3.4 Activity
- `activity_id: int`
- `title: string`、`description: string`
- `status: "pending" | "in_progress" | "completed" | "snoozed" | "shelved"`
- `tags: list[string]`

### 3.5 Material
- `material_id: int`
- `title: string`、`content: string`、`source: string`
- `tags: list[string]`

### 3.6 Pin
- `source_type, source_id, target_type, target_id` の4タプル
- source/target種別は `tag | activity | topic | decision | log | material`

### 3.7 Relation
- `source_type, source_id, target_type, target_id, relation_type`
- relation_type: `related | depends_on | supersedes`
- `depends_on` はactivity同士、`supersedes` はdecision同士に限定

### 3.8 Tag
- 文字列としては `namespace:name` または素タグ
- namespace: `domain` / `intent` / 空
- 補助フィールド: `notes`（教訓）、`canonical`（エイリアス先）、`description`（短い説明）

---

## 4. ガード・前提

### 4.1 OW_ROLE による制限
`src/main.py` の `@mcp.tool` 関数群に **OW_ROLE=worker を理由とした直接的なツール拒否ロジックは確認できなかった**（v0時点）。worker側で何らかのツール利用を制限する場合は、ハーネス層（task_file・instructions注入）または運用ルールで間接的に行うものと推測される。要追加調査。

### 4.2 orch-managed 運用
ow系ツール（特に `ow_spawn_worker` / `ow_close_worker` / `ow_status` / `ow_recover`）はorchロールでの利用を想定している。worker側からも `ow_send` / `ow_history` は呼べる。`report_signal` / `get_signals` はcapability_matrixでorch/dispatcher/workerいずれにも開放されている（観測データの記録に合意形成が不要なため）。`update_signal` のみトリアージ操作としてorch専用。

### 4.3 check-in 先行が前提のツール
- `add_decisions` の hints はharness_service経由で「整合性確認」「pin見直し」などを示唆する。直前にcheck-inしていない場合、文脈不足のためhintsを過信しない方がよい。
- `check_in` を経由しないアクティビティへの操作（`update_activity` 等）は可能だが、その場合 tag_notes の自動注入は行われない。habitsはSessionStart時に全件注入されるため、check_inの有無に関係なく反映される。

### 4.4 モデル指定の固定
`ow_spawn_worker(model=...)` は `claude-opus-4-7` のみ許可。sonnet/haiku/opus-4-8 はバリデーションで弾かれる。

### 4.5 取り消し済みエンティティの扱い
`retract` で論理削除されたdecision/logは、`search` / `get_logs` / `get_decisions` でデフォルト除外される。`include_retracted=true` で明示的に含められる。

### 4.6 上限値
- 一括追加系（`add_logs` / `add_decisions`）: 最大10件
- `get_by_ids`: 最大20件
- `get_logs` / `get_decisions`: limit最大30
- `search`: limit最大50
- `get_timeline`: limit最大100
- `get_map`: max_depth上限10
- `get_signals`: limit最大100

---

## 5. 既知の課題

5次元統合レポートT4節および周辺資料から抽出した、v0時点で残置されている設計課題を列挙する。各項目は本仕様書を凍結せず議論を続けるための論点として置く。

1. **docstring内の判断ロジック残置**: 各ツールのdocstringが「いつ呼ぶか」「いつ呼ばないか」の判断基準を含んでおり、スキルレイヤとの責務境界が曖昧。仕様（What）と運用（When/How）が混在している。
2. **entity_type が文字列フリー**: `topic` / `activity` / `material` / `decision` / `log` の5値は型としてLiteralやEnumで縛られておらず、ツール間で許容値の差（`add_relation` は全5種、`get_logs` は2種のみ等）が散在している。
3. **Read系ツール選択基準の不在**: `search` / `get_by_ids` / `get_map` / `get_timeline` / `check_in` の使い分け方針が一元化されていない。エージェントが最適なツールを選びにくい。
4. **OW_ROLE による直接的なガードの不在**: workerロール時の意図しない高権限操作（spawn_worker / recover 等）を防ぐコードレベルの仕組みが確認できなかった。運用ルールでカバーする現状は脆い。
5. **2段階リード（search → get_by_ids → get_material）の冗長性**: 〔解消済〕`get_by_ids`の`material`レスポンスに`content`/`source`を同梱したため、`search → get_by_ids` の2ステップで全文取得が完結する。`get_material`はmaterial_id単発取得用として残存。
6. **`propagate_to` の二重記録経路**: `add_decisions(propagate_to=...)` で habit / tag_note を派生生成できるが、直接 `add_habit` や `update_tag(notes=)` を呼ぶ経路と並存している。どちらを使うべきかが明確でない。
7. **`related_decisions` の embedding 依存**: embedding サーバー未起動時は空配列を返すが、それを呼び出し側が判別する手段がレスポンスにない。
8. **タグnamespaceのリテラル化**: `domain:` / `intent:` / 素タグの3区分は文字列パースに依存しており、型安全ではない。
9. **status="active" のエイリアス挙動**: pending+in_progress を返すが、snoozed/shelvedは含まない。明示しないと誤解の温床になる。
10. **`include_retracted` がツール間で揃っていない**: `search` / `get_logs` / `get_decisions` にはあるが、`get_timeline` には無い。

---

## 補足

- 本書の更新は `src/main.py` の docstring を一次情報とし、yamlを再生成する手順を別途整える必要がある（現状は手動同期）。
- 個別ツールの呼び出し例（typical-call snippets）は別資料 `docs/architecture/sequences/` に分離する予定。
