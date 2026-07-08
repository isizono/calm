<!-- ccm-doc-sync
watch-tags: domain:cc-memory
watch-direction: true
watch-migrations: false
last-synced: 2026-07-05
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

全40ツール。カテゴリ別に一覧する。

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
| `pull_precedents` | 設計判断前に近傍topicの決定事項を網羅列挙する（判例pull） |

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
| `report_signal` | cc-memory自身の故障・使用感不満・矛盾検出・運用計測イベントを記録する |
| `get_signals` | 記録されたシグナルを一覧・集計する |
| `update_signal` | シグナルのトリアージ状態を遷移する |

### 1.11 relay系（セッション間通信 4動詞）

Claude Codeセッション間の通信・文脈配信レイヤ。relay v2 サーバー（HTTP、既定 `http://localhost:8770`）を transport とし、cc-memory server 単一 identity 名義で代理購読・代理投函する。ack・lease renew・購読解除・SSE再接続はサーバー側で自動管理され、ツール面にはこの4動詞のみを見せる。配送状況・runtime健全性の確認は診断専用の`relay_status`（1.12）を使う。

4動詞はフラットな並列ではなく、「1. 名指し送信」「2. labelペアでの配信」「3. 受信（両方に共通）」の2+1構造を持つ。真に対をなすのは publish/subscribe のみで、post は対になる購読動詞を持たない一方通行の送信、receive は post/publish どちらの経路で届いたメッセージも受け取る共通の受け口である。

| # | ツール | 役割 | 概要 |
| --- | --- | --- | --- |
| 1 | `relay_post` | 名指し送信 | 場（stream）にメッセージを投函する（未存在streamは自動作成） |
| 2 | `relay_publish` | label配信（送信側） | labels routingでメッセージを配布する（outbox経由・at-least-once） |
| 2 | `relay_subscribe` | label配信（受信側） | labelsの購読を宣言する（同一labels集合の再呼び出しは冪等） |
| 3 | `relay_receive` | 受信（共通） | 自session宛の未読メッセージをinboxからdrainする |

### 1.12 relay観測系（診断・非動詞）

4動詞（1.11）のいずれの代替でもない、読み取り専用の診断面。relayサーバーへのHTTPアクセスは行わず、ローカルDBとruntimeのin-memory状態のみで完結する。

| ツール | 概要 |
| --- | --- |
| `relay_status` | outbox行の配送状況（pending/delivered/dead）とruntime健全性（3スレッド生存・再起動回数）を確認する |

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

**返り値**: `get_logs` は `{logs: [DiscussionLog], total_count: int, truncated: bool}`、`get_decisions` は `{decisions: [Decision], total_count: int, truncated: bool}`。`total_count` は対象log/decisionの総件数（limit/start_idの影響を受けない）、`truncated` は limit/start_id で後続を打ち切ったとき true（続きのページが存在する）。
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

- `add_habit(content: string, importance_score: int = 3, status: string = "active") -> dict`: habitを登録。SessionStart時に全件注入される（セッション途中の登録は次セッション以降に有効）。importance_scoreは1(critical)/2(important)/3(default)のいずれかで、intelligently層マニフェストのソートに使う。statusは`'active'`/`'archived'`のいずれか。
- `get_habits(active: bool = true, habit_id?: int) -> dict`: 登録済みhabit一覧。既定でactive=1のみ返す。無効化済みも含む全件が欲しいときは`active=false`を渡す。SessionStartで全文注入されるのは`trigger_mode='always'`のみで、`'intelligently'`はタイトルのみのマニフェスト表示になる。`habit_id`を渡すとその1件だけを本文付きで取得でき、intelligentlyな振る舞いの詳細を引くときに使う（取得と同時に`last_recalled_at`が更新される）。
- `update_habit(habit_id: int, content?: string, active?: bool, trigger_mode?: string, description?: string, importance_score?: int, status?: string) -> dict`: active=Falseで無効化。trigger_modeは`'always'`（全文常時注入）/`'intelligently'`（マニフェストのみ表示、詳細は`get_habits(habit_id=...)`でon-demand取得）のいずれか。descriptionはintelligently層のマニフェスト表示に使う要旨（100文字以内）。importance_scoreは1(critical)/2(important)/3(default)のいずれかでマニフェストのソートに使う。statusは`'active'`/`'archived'`のいずれかで、`'archived'`はマニフェストから除外される。

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

引数なし。返り値: `{heartbeat_timeout, in_progress_limit, pending_limit, recency_decay_rate, sync_disable_retrospective, sync_policy, snapshot_interval_hours, snapshot_max_count, snapshot_anomaly_threshold, precedent_budget_chars, budget_defaults, read_tool_limits}`。スキルが環境変数ベースの設定を参照するときに使う。`budget_defaults` は `budget_service` が把握する予算関連の既定値一覧（`precedent_budget_chars` / `recency_decay_rate` / `recency_decay_floor`。いずれもsrc.config由来）。

### 2.26 roll_dice

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| sides | int | no | 10 | サイコロ面数 |

**返り値**: `{result: int}`。

### 2.29 report_signal

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

### 2.30 get_signals

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| status | string \| null | no | "new" | `new`/`triaged`/`promoted`/`dismissed`。nullで全status横断 |
| kind | string \| null | no | null | フィルタ対象のkind。nullで全kind横断 |
| limit | int | no | 20 | 最大100 |
| offset | int | no | 0 | ページネーション |
| include_stats | bool | no | false | trueでkind×statusのクロス集計と直近30日サマリを付与 |

**返り値**: `{signals: [...], total_count: int, stats?: {by_kind_status, last_30d}}`。

### 2.31 update_signal

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| signal_id | int | yes | - | 対象シグナルID |
| status | string | yes | - | 遷移先status（`new`/`triaged`/`promoted`/`dismissed`） |
| promoted_type | string | no | null | 昇格先エンティティ種別（`topic`/`activity`/`decision`/`log`/`material`） |
| promoted_id | int | no | null | 昇格先エンティティID。promoted_typeと同時に指定する |

**返り値**: `{signal: {...}}`（更新後の行）。
**動作**: リンクを張るだけで昇格実体は作らない（実体の作成は既存のadd系ツールで行う）。

### 2.32 pull_precedents

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| context | string | yes | - | これから決めようとしている論点の記述（自由記述、2文字以上）。routingのクエリ兼telemetry用（topic_ids指定時も必須） |
| topic_ids | list[int] | no | null | 対象topicを明示指定してroutingをスキップする（embeddingサーバー停止時でも動作する） |
| k | int | no | 3 | routingで採用するtopic数の上限（1〜5にclamp） |
| budget_chars | int | no | null | 本文展開の文字数予算。省略時はconfig既定値（`get_config()`の`precedent_budget_chars`で確認可） |
| include_materials | bool | no | true | decision/topicに紐づくmaterialカタログを同時展開する（30件で打ち切り、超過時`materials_truncated=true`） |

**返り値**: `{guarantee, routing, topics, budget, truncated, materials_truncated}`。`guarantee`は`enumerated`（routing成立・全件列挙完了）/ `routing_miss`（近傍topicなし）/ `routing_unavailable`（embeddingサーバー停止）のいずれか。`routing.mode`は`vector`（embedding routingで解決）/ `explicit`（topic_ids指定でrouting skip）/ `unavailable`（embeddingサーバー停止でrouting不能）。`routing.candidates`は各`{topic_id_raw, title, distance, selected}`（topic_ids指定時はdistanceなし。存在しないtopic_idを指定した場合は`{topic_id_raw, error: "not_found"}`）。`topics[].decisions`各要素は`detail="full"`（本文展開）または`detail="index"`（id/title等のみ、`get_by_ids`で本文追補可）。
**動作**: `search`がランクtop-Nの確率的発見であるのに対し、本ツールは選ばれたtopicの非retract decisionを全件（最低でも索引粒度で）応答に含めることを保証する。read-only（statusを更新する副作用なし）。
**関連**: 設計・裁定の前に近傍topicの判例を網羅確認したい場面で`get_decisions`/`check_in`のChoose節から参照される。

> relay 4動詞（2.38〜2.41）は post（名指し送信）/ publish・subscribe（labelペア）/ receive（受信、共通）の2+1構造を持つ。並びの意図は §1.11 を参照。

### 2.38 relay_post

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| stream_name | string | yes | - | stream名。`:` と `/` は使用不可。実体のstream_idはserver名義で `<identity>:<stream_name>` に修飾される |
| body | string | yes | - | メッセージ本文（非空） |
| ttl | int | no | null | メッセージ保持秒数（60〜86400）。省略時はstreamの既定値 |

**返り値**: `{stream_id: string, publish_id: int, matched_members: int}`。
**動作**: 投函先streamが未存在（404）なら自動作成し、自identityを`read_write` memberに設定して1回だけ再投函する。作成の同時競合（409）も1回の再投函で解消する。自server名義のstreamのみ扱う。relayへの呼び出し自体は同期だが、成功応答の`matched_members`は投函時点の購読者数を示すのみで、実配達は relay 側の非同期配信を経由する（配達完了そのものは保証しない）。
**エラー処理**: `RELAY_BEARER_TOKEN`未設定は設定方法を含む明示エラー（`config_missing`）。認証エラー（401）・close済みstream（410）はそのまま明示エラーとして返す（silent fallbackしない）。rate limit（429）は専用コード`rate_limited`で返し、`retry_after`（秒、`Retry-After`ヘッダ未提供時は`null`）を構造化フィールドで付与する。呼び出し側はこの秒数だけ待ってからリトライすること。
**関連**: 投函した内容はcc-memory本体（search/get_timeline/pull_precedents等）には自動反映されない。後から参照できる形で残したい場合は受信後にadd_logs/add_material等で明示的に保存すること。

### 2.39 relay_publish

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| labels | list[string] | yes | - | 配送先マッチング用labels（1個以上、1個あたり200字以内）。routing系（`handle:`/`room:`/`task:`）とtag namespace（`domain:`/`intent:`等）を併用可。これらのみでも有効。未知prefixは不透明labelとして受理。`role:`（廃止済みnamespace）とcc-memoryの予約namespace（`entity:`/`event:`/`topic:`/`activity:`/`decision:`/`log:`/`material:`/`tag:`/`habit:`。entity更新のrelay publishが使うnamespaceで、実在チェックなしの不透明文字列にしかならないため予約済み）はエラー |
| body | string | yes | - | メッセージ本文（非空） |
| title | string | no | null | 一覧表示用の見出し（200字以内） |

**返り値**: `{outbox_id: int, labels: list[string], handle: string, identity: string}`。
**動作**: 送信者の`handle:` labelを自動付与し、`relay_outbox`テーブルへINSERTして完結する（transactional outbox）。relayへの配達はserver内の常駐配達ループが非同期に行い、保証はat-least-once。labelsが空のpublishは宛先が決まらないため拒否する。`identity`は呼び出し元セッションの識別子（cc-memory server再起動をまたいで安定）。
**エラー処理**: `RELAY_BEARER_TOKEN`未設定・session_id未解決・labels/body不正はいずれも明示エラー。
**関連**: 配布した内容はcc-memory本体（search/get_timeline/pull_precedents等）には自動反映されない。後から参照できる形で残したい場合は受信後にadd_logs/add_material等で明示的に保存すること。

### 2.40 relay_subscribe

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| labels | list[string] | yes | - | 購読条件labels（publish側labelsをすべて含む発話が届く）。空配列なら自handle宛のみの購読。`role:`はエラー。cc-memoryの予約namespace（`entity:`/`event:`/`topic:`/`activity:`/`decision:`/`log:`/`material:`/`tag:`/`habit:`）はrelay_publishと異なりここでは許可（entity更新のrelay publishを購読するために必要。例: `["activity:1183", "event:updated"]`） |

**返り値**: `{subscription_id: string, labels: list[string], lease_expires_at: string, handle: string, reused: bool, identity: string}`。
**動作**: 自sessionの`handle:` labelを自動付与し、subscription declaration file（`~/.cc-memory/relay/subscriptions/session-<session_id>.json`）とrelayの購読登録を同期する。同一labels集合の再呼び出しは冪等で、leaseが有効なら既存購読を返し（`reused: true`）、失効・不明なら新規購読してdeclaration fileのidを差し替える。lease更新・再購読・購読解除はserver側常駐処理が自動管理する。新規購読（`reused: false`）が成立すると、server内の常駐SSE受信スレッドへ即座に反映指示を送る。反映は次にSSEフレーム（実メッセージだけでなくkeepaliveのコメントフレーム到達でも判定される）が届いた時点で完了し、既定設定では上限概ね60秒に収まる。この間に届いたメッセージはrelay側のsubscription outboxに保持されるため取りこぼされない。`identity`は呼び出し元セッションの識別子（cc-memory server再起動をまたいで安定。`scripts/relay/watch_inbox.sh`等に渡す値として使える）。
**エラー処理**: `RELAY_BEARER_TOKEN`未設定・session_id未解決は明示エラー。relayエラー時はdeclaration fileを更新しない。rate limit（429）は専用コード`rate_limited`で返し、`retry_after`（秒、`Retry-After`ヘッダ未提供時は`null`）を構造化フィールドで付与する。呼び出し側はこの秒数だけ待ってからリトライすること。
**関連**: 購読宣言（`relay_subscribe`）と受信（`relay_receive`）は分離しており、実際のメッセージ受信は`relay_receive`側が担う。

### 2.41 relay_receive

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| limit | int | no | 50 | 最大取得件数（1以上）。200を超える値は200に切り詰める |
| peek | bool | no | false | trueのとき既読化せず内容だけ返す（cursor前進なし） |

**返り値**: `{messages: list[object], count: int, has_more: bool, identity: string}`。`has_more`はtrueのときlimitに収まらない未読が残っている（同じ呼び出しを繰り返すかlimitを上げて追加取得できる）。
**動作**: 自sessionのinbox（`~/.cc-memory/relay/inbox/session-<session_id>.jsonl`）をcursor位置から読み出す。既定（peek=false）はconsume（読んだら既読=cursor前進、末尾まで読み切ったらtruncate）。peek=trueはcursor・inbox fileを一切変更せず読むだけで、同じ範囲を何度でも読み直せる。実際に既読化するには同じ呼び出しをpeek=false（既定）で呼び直す。推奨パターン: (1) `peek=true`で内容確認 (2) add_logs/add_material等で保存 (3) 同じ呼び出しを`peek=false`で呼び直し既読化し、その返り値のmessagesも必ず確認する（手順1・3の間に新着があれば手順3の返り値に含まれるため）。inbox不在（未購読・未配達）は空リストの正常応答（エラーにしない）。relayへのHTTPアクセスは発生しない（ローカル完結）。受信内容はcc-memory本体に自動記録されない。重要な内容は受信側がadd_logs/add_material等で明示的に保存すること。`identity`は呼び出し元セッションの識別子（cc-memory server再起動をまたいで安定）。
**配達契約**: at-least-once。同一メッセージが重複して届くことがあるため、受信側は冪等に扱うこと。
**関連**: `relay_subscribe`で宣言したlabelsにマッチした配達のみをdrainする（受信対象の決定は`relay_subscribe`側で行う）。

### 2.42 relay_status

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| outbox_id | int | no | null | relay_publishの返り値のoutbox_id。指定するとその行の配送状況を返す。省略時は`outbox`キーの値がnullになる（キー自体は常に存在する） |

**返り値**: `{outbox: {outbox_id, status, labels, title, created_at, processed_at, dead_at, retry_count, last_error} | null, runtime: {configured, running, threads: {<thread名>: {alive, restart_count, last_restart_at, last_error}}}}`。
**動作**: outbox行の配送状況はrelay_outboxテーブルのローカルSELECTのみで判定する（`processed_at`セット済み=delivered、`dead_at`セット済み=dead、いずれも無ければpending）。message本文（`ref_id`）は返さない（同一プロセス内の他sessionが発行した行にも越境してアクセスできてしまうため、意図的に除外）。runtimeセクションは常に返る。`running: false`はこのプロセスでrelay v2常駐処理が起動していないことを示す（エラーではない）。relayサーバー本体へのHTTPアクセスは一切発生しない。
**エラー処理**: outbox_idが正の整数でない場合は`validation`。指定したIDの行が存在しない場合（存在しないID、またはdead化から一定期間経過後にDLQ物理削除済み。保持日数は`relay_sdk`側の設定値）は`not_found`。

### 2.42 relay_status

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| outbox_id | int | no | null | relay_publishの返り値のoutbox_id。指定するとその行の配送状況を返す。省略時はoutboxセクションを返り値から省く |

**返り値**: `{outbox: {outbox_id, status, labels, title, created_at, processed_at, dead_at, retry_count, last_error} | null, runtime: {configured, running, threads: {<thread名>: {alive, restart_count, last_restart_at, last_error}}}}`。
**動作**: outbox行の配送状況はrelay_outboxテーブルのローカルSELECTのみで判定する（`processed_at`セット済み=delivered、`dead_at`セット済み=dead、いずれも無ければpending）。message本文（`ref_id`）は返さない（同一プロセス内の他sessionが発行した行にも越境してアクセスできてしまうため、意図的に除外）。runtimeセクションは常に返る。`running: false`はこのプロセスでrelay v2常駐処理が起動していないことを示す（エラーではない）。relayサーバー本体へのHTTPアクセスは一切発生しない。
**エラー処理**: outbox_idが正の整数でない場合は`validation`。指定したIDの行が存在しない場合（存在しないID、またはdead化から一定期間経過後にDLQ物理削除済み。保持日数は`relay_sdk`側の設定値）は`not_found`。

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

### 4.1 check-in 先行が前提のツール
- `add_decisions` の hints はharness_service経由で「整合性確認」「pin見直し」などを示唆する。直前にcheck-inしていない場合、文脈不足のためhintsを過信しない方がよい。
- `check_in` を経由しないアクティビティへの操作（`update_activity` 等）は可能だが、その場合 tag_notes の自動注入は行われない。habitsはSessionStart時に全件注入されるため、check_inの有無に関係なく反映される。

### 4.2 取り消し済みエンティティの扱い
`retract` で論理削除されたdecision/logは、`search` / `get_logs` / `get_decisions` でデフォルト除外される。`include_retracted=true` で明示的に含められる。

### 4.3 上限値
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
4. **2段階リード（search → get_by_ids → get_material）の冗長性**: 〔解消済〕`get_by_ids`の`material`レスポンスに`content`/`source`を同梱したため、`search → get_by_ids` の2ステップで全文取得が完結する。`get_material`はmaterial_id単発取得用として残存。
5. **`propagate_to` の二重記録経路**: `add_decisions(propagate_to=...)` で habit / tag_note を派生生成できるが、直接 `add_habit` や `update_tag(notes=)` を呼ぶ経路と並存している。どちらを使うべきかが明確でない。
6. **`related_decisions` の embedding 依存**: embedding サーバー未起動時は空配列を返すが、それを呼び出し側が判別する手段がレスポンスにない。
7. **タグnamespaceのリテラル化**: `domain:` / `intent:` / 素タグの3区分は文字列パースに依存しており、型安全ではない。
8. **status="active" のエイリアス挙動**: pending+in_progress を返すが、snoozed/shelvedは含まない。明示しないと誤解の温床になる。
9. **`include_retracted` がツール間で揃っていない**: `search` / `get_logs` / `get_decisions` にはあるが、`get_timeline` には無い。

---

## flavor共通引数

`get_topics` / `get_logs` / `get_decisions` / `pull_precedents` / `search` / `get_by_ids` / `get_activities` / `get_material` / `check_in` / `get_timeline` の10ツールに共通する `flavor: "raw" | "internal" | "readable"` 引数（既定値 `internal`）。本文中の `{{cite:X#NNN}}` citationテンプレートと、削除・取り消し済みエンティティへの参照の表示形式を切り替える。正確な変換ロジックは `src/services/citation_renderer.py` のモジュールdocstringを一次情報とする。

| flavor | citationテンプレートの展開 | 削除/取り消し済み参照 | 想定用途 |
| --- | --- | --- | --- |
| `raw` | 無加工（テンプレのまま） | 無加工 | 生データが必要な特殊用途（再エクスポート等） |
| `internal`（既定） | `<title> (X#NNN)` 形式。IDを保持 | `[deleted X#NNN]` / `[retracted X#NNN]` | エージェントが結果を保持し、以降のtool呼び出しにIDで追跡させたい場合 |
| `readable` | `<title>` 形式。IDなし | `[deleted item]` / `[retracted item]` | 人間への最終出力（cc-memory内部識別子を露出させたくない場合） |

選定基準: ユーザーに提示する最終出力なら`readable`、エージェントが内部処理を続けるなら`internal`、生データのままの特殊用途のみ`raw`。コードブロック内やエスケープ済み（`\{{cite:...}}`）のテンプレートはどのflavorでも展開されない。

---

## 補足

- 本書の更新は `src/main.py` の docstring を一次情報とし、yamlを再生成する手順を別途整える必要がある（現状は手動同期）。
- 個別ツールの呼び出し例（typical-call snippets）は別資料 `docs/architecture/sequences/` に分離する予定。
