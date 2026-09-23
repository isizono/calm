<!-- ccm-doc-sync
watch-tags: domain:calm, domain:cc-memory
watch-direction: true
watch-migrations: false
last-synced: 2026-09-23
last-synced-migration: 0077
-->

# CALM MCPツール仕様書 v0

## 0. 読み方

このドキュメントはCALMが提供するMCPツールの引数・返り値・エラー仕様を網羅的にまとめたものである。

- **v0であり、凍結を目的としない**。レビュー・議論のたたき台として位置づける。最終的な真実は `src/main.py` の `@mcp.tool` デコレータ付き関数とそのdocstringに置く。
- 並行して `docs/spec/openapi.yaml` を機械可読版として用意している。CIや外部ツールから参照する場合はyaml側を使う。
- 本書は人間向けの俯瞰用。粒度は「読者がツールを呼び出せる」レベルに留め、内部実装には踏み込まない。
- ツール名・引数名・型名は外部APIとして直接参照されるためそのまま英語表記で残す。本文は常体（だ・である調）。
- CALM内部ID（D#/M#/A#/L#/T#）は本文では使わず、論理名（decision/material/activity/log/topic）で書く。

---

## 1. ツール一覧

全59ツール。カテゴリ別に一覧する。

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
| `get_overview` | 進行状況4節（working/recently_done/awaiting_human/backlog）を1回で集計して返す（読み取り専用） |
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
| `demote_tag_notes` | tag notesの指定セクションを資材へ逐語退避し、notesを縮小する |
| `retract` | 決定事項・ログ・資材を論理削除する（undoで復帰可能、検索インデックスも再登録される） |

### 1.4 検索系

| ツール | 概要 |
| --- | --- |
| `search` | 横断検索（FTS5 trigram + ベクトル ハイブリッド） |
| `search_tags` | タグをキーワード検索する |
| `analyze_tags` | タグ共起分析（PMI/クラスタ/孤児/重複候補） |
| `detect_reask_candidates` | transcriptから聞き返し候補を抽出し上位N件のsearchまで一括実行する |

### 1.5 関係系・pin系

| ツール | 概要 |
| --- | --- |
| `add_relation` / `remove_relation` | エンティティ間リレーションの追加・削除 |
| `resolve_destabilization` | destabilizesエッジ（前提の揺らぎ）を1本解消する |
| `suggest_destabilized_candidates` | 軸変更decisionからdestabilize候補decisionを提示（read-only） |
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
| `export_material` | 資材をmd形式のファイルとしてCALM外に出力する |
| `collect_export_candidates` | 他インスタンスへのexport候補を洗い出す（read-only） |
| `set_instance_identity` | 自インスタンスの識別子を設定する（バンドル複合キー発行の基盤） |
| `export_bundle` | 確定した候補リストからバンドル（manifest.yaml + エンティティ別mdファイル）を書き出す |
| `import_bundle` | バンドルを取り込む（mode="dry_run"で衝突検知レポート、mode="apply"で実際にDBへ書き込み） |

### 1.10 シグナル系（signal_events）

CALM自身の故障・使用感不満・矛盾検出・運用計測イベントの記録先。`add_logs` / `add_decisions` とは異なり合意不要の生の観測データであり、専用テーブル（`signal_events`）に記録される。

| ツール | 概要 |
| --- | --- |
| `report_signal` | CALM自身の故障・使用感不満・矛盾検出・運用計測イベントを記録する |
| `get_signals` | 記録されたシグナルを一覧・集計する |
| `update_signal` | シグナルのトリアージ状態を遷移する |

### 1.11 asks系（判断委譲）

AIエージェントが人間の判断を待つ問いを1箇所に積み、人間が回答するだけで作業を再開できるようにする受け皿。`signal_events`と似た設計思想だが、状態遷移（open→answered→promoted/dismissed、open→withdrawn）を持つため専用テーブル（`asks`）に記録される。answer時点ではトリアージ（promote/dismiss）を行わず、次の`check_in`で配達されるまで遅延する。

| ツール | 概要 |
| --- | --- |
| `add_ask` | 答え待ちの問いを1件積む（blocksで指定したactivityを止める） |
| `get_asks` | 記録されたaskを一覧・集計する |
| `answer_ask` | 答え待ちのaskに回答する（トリアージは行わない） |
| `triage_ask` | answered状態のaskをpromote（decision化）またはdismissへ振り分ける |
| `withdraw_ask` | 答え待ちのaskを自発的に取り下げる |
| `unsubscribe_ask` | askの通知希望（notify_wanted）を明示的に外す |

### 1.12 セッション別名系（並行セッションの現在地表示）

複数のClaude Codeセッションを並行起動したとき、`ListAgents`のPeer sessions一覧に出る自動生成名（例: `workspace-a2`）だけではどのセッションが何をしているか分からない。この2ツールは「CLI表示名 → 各セッションがcheck_inしたアクティビティから自動生成した別名」の対応表を提供する（ローカルファイル読み書きのみで完結する）。

| ツール | 概要 |
| --- | --- |
| `get_sessions` | 稼働中セッションの「CLI表示名 → 別名」対応表を取得する |
| `set_session_alias` | 自セッションの別名を手動で上書きする |

### 1.13 goal系（activityの終了条件）

activityが目指す終わりを、真偽の付く条件の集合として表現する機構。全条件の終端（satisfied/waived）はサーバーが機械的に検出するが、goalを閉じるのは明示判定（`judge_goal`）だけである。goalはactivityから作る（`set_goal`が新規作成と紐づけを1操作で行う）。goalの3表（goals/goal_conditions/goal_activities）は5型（topic/activity/material/decision/log）の外側の独立したテーブルとして持つ。

| ツール | 概要 |
| --- | --- |
| `set_goal` | activityのgoal上の立場を決める（新規作成/既存goalへの紐づけ/不要印/未定義への解除） |
| `update_goal` | 条件の追加・状態変更・担い手と束縛の変更、goalの一文の修正、判定済みgoalの差し戻しを行う |
| `judge_goal` | goalの終了を明示的に判定して閉じる。紐づく未完了のactivityも同時に閉じる |
| `get_goal` | 1つのgoalの全条件（充足済み含む）とid、紐づくactivityを読む（読み取り専用） |

### 1.14 feedback系（躓きの知見を自分に配達する機構）

Claudeが同じところで躓き続けるのを防ぐため、Claude自身が知見（フィードバックエントリ）を書き残し、発話・ツール失敗・ツール実行直前の3タイミングでhookが自分に配達する機構。CALMのDB内のエントリであり、ユーザーが設定するsettings/rules/habitsとは別の層（Claudeが自由に付け消し変更してよい）。

| ツール | 概要 |
| --- | --- |
| `get_feedback_entries` | フィードバックエントリを引く。各エントリにnotes全件とread_mark（変更前に要求される印）を添える |
| `write_feedback_entry` | フィードバックエントリを作る・直す・消す（create/update/delete）。update・delete・削除済み名前への復活はread_mark必須 |
| `add_feedback_note` | フィードバックエントリに観測・経緯のノートを足す。read_mark不要、削除済みエントリにも足せる |

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

**items詳細**:
- `reason`: 決定の理由。任意で本文末尾に定型節（却下案:/適用条件:/適用外:/検証:/隣接確認:）を書ける。書式・各節の意味はdocs/precedent-format.mdが正本。節はすべて任意で、「該当なし」を埋める空項目・ダミー項目は書かないこと。
- `title`: 決定の要点を表す1行（35字以内）。check-in・timeline・search等の見出しに使われるため付与を推奨。省略時はdecision本文にfallback。タグに`layer:direction`を含む場合は必須（省略・空文字は当該itemが`errors`に`ITEM_ERROR`として格納され、decision自体は作成されない）。
- `tags`: 省略時はtopicのタグを継承。内容を表すタグを積極的に追加することが望ましい。namespace規約はdocs/architecture/invariants.mdの「タグnamespace」節を参照。
- `propagate_to`: `{type: "habit" | "tag_note", content: string, tag?: string}`。tagはtype="tag_note"のとき必須。type="tag_note"は教訓・注意点のみに使い、仕様・手順の全文転記には使わない。

**返り値**: `{created: [...], errors: [...], propagation_failed?: [...]}`。
- `created`の各要素には`related_decisions`（同topic内の類似decision上位3件、各`{id, title, distance}`。embeddingサーバー未起動時は空配列）が付く。既存decisionとの矛盾・重複に気づくための導線。
- タグに`layer:direction`を含む要素には`existing_direction_decisions`（同domainの有効な方向性decision全件、自身除外・非ランク）と`direction_note`（supersede/併存の判断を促す文言）も付く。
- `reason`に定型節があれば`precedent`（`{rejected_alternatives: 件数, scope: bool, verification_anchors: [文字列, ...], adjacent_check: [文字列, ...], warnings?: [文字列, ...]}`）をecho。書式ゆれ・空節・アンカー日付欠落等、または`intent:design`タグ付き要素で「隣接確認:」節が無い場合は`precedent_warnings`（文字列のリスト）も付く。いずれもsoft validationであり、decision作成自体は拒否しない。

**propagation_failed**: propagate_toの伝搬が1件以上失敗した場合のみ付く配列。各要素は `{index, decision_id, type, tag?, message}`。decision自体の作成成否には影響しない（decisionは常に成功として作成される）ため、この配列を見ないと伝搬失敗（例: tag_note伝搬先タグの文字数上限超過）に気づけない。
**関連**: `add_habit` / `update_tag(notes=...)` と連動。

### 2.4 get_topics

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| tags | list[string] | no | null | AND条件。未指定は全件 |
| limit | int | no | 10 | 取得件数上限 |
| offset | int | no | 0 | ページネーション |
| since | string | no | null | ISO日付（以降） |
| until | string | no | null | ISO日付（以前） |

**返り値**: `{topics: [Topic], total_count: int, tag_notes?: [TagNote], archived_tags: [{tag, archived_reason}]}`。`archived_tags` は応答に含まれるtopicのタグのうちarchivedなものの集約で、該当なしでも常に空配列で付く。

### 2.5 get_logs / get_decisions

両者とも同じ引数構造を持つ。

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| entity_type | string | yes | - | `"topic"` または `"activity"` |
| entity_id | int | yes | - | 対象エンティティID |
| start_id | int | no | null | ページネーション用 |
| limit | int | no | 30 | 最大30件 |
| include_retracted | bool | no | false | trueで取り消し済みも含む |

**返り値**: `get_logs` は `{logs: [DiscussionLog], total_count: int, truncated: bool, archived_tags: [{tag, archived_reason}]}`、`get_decisions` は `{decisions: [Decision], total_count: int, truncated: bool, archived_tags: [{tag, archived_reason}]}`。`total_count` は対象log/decisionの総件数（limit/start_idの影響を受けない）、`truncated` は limit/start_id で後続を打ち切ったとき true（続きのページが存在する）。`archived_tags` は応答に含まれるlog/decisionのタグのうちarchivedなものの集約で、該当なしでも常に空配列で付く。
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

**返り値**: `{results: [SearchHit], archived_tags: [{tag, archived_reason}]}`。scoreは0〜1正規化（1.0=全ソース1位、片方ヒットは最大0.5）。0.4以上=高関連、0.15〜0.4=中、0.15未満=低の目安。snippetでなく全文が必要な場合は結果のtype+idを`get_by_ids`に渡す。各結果アイテムには`archived`（bool）・`archived_tags`（配列）・`score_breakdown.archived_factor`も付く（全タグがarchivedのアイテムのみ`archived: true`になりfinal_scoreが下位表示側に減衰する。除外はしない）。トップレベルの`archived_tags`は応答内の全アイテムのタグのうちarchivedなものの集約で、該当なしでも常に空配列で付く。
**実装**: FTS5 trigram + ベクトル検索のRRF統合。

### 2.6b detect_reask_candidates

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| transcript_path | string | yes | - | transcript JSONLのパス |
| max_candidates | int | no | 50 | 抽出段階の上限件数 |
| search_top_n | int | no | 8 | search実行対象とする候補の上限件数（excluded_reason付きを除いた先頭N件） |
| search_limit | int | no | 10 | 候補1件あたりのsearch呼び出しのlimit |
| score_threshold | float | no | 0.4 | `candidates[].top_hits` に残す最小final_score |

**返り値**: `{candidates: [{kind, turn, text, context_snippet, options?, degraded, top_hits: [{type, id, score, title}], search_error?}, ...], total_extracted, excluded_count, searched_count, truncated_count, degraded, score_threshold}`。`search_error`は候補に対するsearch呼び出しがエラーを返した場合のみ付与される（`{"code", "message"}`）。excluded_reason付き候補・search_top_nを超えた候補は`candidates`に含まれない。transcript_pathが存在しない場合は`{"error": {"code": "TRANSCRIPT_NOT_FOUND", ...}}`。
**用途**: `skills/sync-memory/SKILL.md` ステップ9（聞き返しの後追い検出）の候補抽出＋照合searchを1回の呼び出しに集約する。既存記録があれば聞き返しが不要だったかの主観判定と`report_signal`呼び出しは呼び出し側が行う。

### 2.7 get_by_ids

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| items | list[{type, id}] | yes | - | 最大20件 |

**返り値**: `{results: [{type, id, data}, ...], archived_tags: [{tag, archived_reason}]}`。2段階リード（searchで概要→get_by_idsで全文）の後半に位置する。materialは`data`に`content`/`source`が含まれ、追加で`get_material`を呼ぶ必要はない。`archived_tags`は応答に含まれる全アイテムのタグのうちarchivedなものの集約で、該当なしでも常に空配列で付く。

### 2.8 search_tags

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| query | string | yes | - | タグ名部分一致 + ベクトル検索 |
| namespace | string | no | null | `"domain"` / `"intent"` / `""` (素タグ) |
| include_notes | bool | no | false | trueでnotesも返す |
| limit | int | no | 20 | 取得件数上限 |

**返り値**: `{tags: [{tag, namespace, score, notes?, archived, archived_reason}]}`。`archived`はbool、`archived_reason`はarchived時のみ非null。

### 2.9 update_tag

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| tag | string | yes | - | 対象タグ |
| notes | string | no | null | 教訓・運用ルール（全文置換） |
| canonical | string | no | null | エイリアス先。`""` で解除 |
| rename | string | no | null | 新しいタグ名 |
| description | string | no | null | 短い説明文（最大100文字） |
| archived | bool | no | null | trueで退役、falseで解除 |
| archived_reason | string | no | null | 退役理由（最大100文字）。archived=trueと同時指定のときのみ有効 |

**制約**: notes/canonical/rename/description/archived は相互排他。少なくとも1つ指定。canonical連鎖（エイリアスのエイリアス）は禁止。notes付きタグはエイリアス化不可。archivedなタグをcanonical先に指定する・archivedなタグ自身をcanonical化することはできない。他タグのcanonical先になっているタグはarchived化できない。archived_reasonの単独指定（archived未指定またはfalseとの同時指定）はエラー。既にarchivedなタグへarchived=trueを再適用しても冪等（archived_atもarchived_reasonも更新されない）。archived=falseに戻すとarchived_reasonも自動的にnullへ戻る。

### 2.9b demote_tag_notes

tag notesの指定セクションを資材へ逐語退避し、notesを縮小するツールである。notesは全文置換APIしか持たないため、退避する見出し単位で本文を切り出し、退避先資材への書き込みとnotesの縮小を1トランザクションで行う。

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| tag | string | yes | - | 対象タグ |
| sections | list[string] | yes | - | 退避する見出しテキストの配列。`## ` の有無は問わない（前後空白と先頭 `## ` を正規化して照合） |
| mode | string | no | "pointer" | `pointer`は退避後にnotes末尾へ1行ポインタを残す。`drop`はポインタも残さない |
| archive_material_id | int | no | null | 既存の退避先資材へ追記する。省略時は新規作成 |
| archive_tags | list[string] | no | null | 退避先資材のタグ。省略時は `[tag, "tag-notes-archive"]` |
| reason | string | no | null | 退避理由の1行。退避先資材の冒頭に入る |

**notesの3層分解**: notesは preamble（最初の `## ` 見出し行より前の本文、退避対象にできない）/ sections（`## ` 見出し単位のブロック列）/ trailer（末尾から連続する空行または `#audited-...` 等のハッシュタグのみの行、常にnotesに残る）の3層として扱う。

**制約**: `sections` に存在しない見出しを指定するとSECTION_NOT_FOUND、同一見出しが複数あるタグではAMBIGUOUS_SECTIONで拒否する。`archive_material_id` にretract済みまたは存在しないIDを指定するとVALIDATION_ERROR。退避後のnotesの書き込みが文字数上限（4000字）を超えて拒否された場合、退避先資材の作成・追記を含む変更全体がロールバックされる。全セクションを退避してnotesが空白のみになる場合、notesは空文字列になる。

**返り値**: `{tag, material_id, material_title, material_created, demoted_sections, pointers_added, notes_length: {before, after, ceiling, over_budget}, citations_converted}`。`notes_length.over_budget` は縮小後もなお天井を超えているかを示す（ラチェット則により、超えている間は縮む更新以外のあらゆる追記が拒否され続ける）。`citations_converted` は退避先資材の本文中で生ID参照が `{{cite:...}}` へ自動変換された件数。

**tag notes 記述規約**: notesに全文で置いてよいのは行動を変える取扱注意（教訓・落とし穴、環境知識）のみで、仕様スナップショット・運用手順・歴史記録は1行ポインタ、状態・進行ジャーナルは0行（書くこと自体を禁止）とする。規約全文はツールdocstring（`demote_tag_notes`）が正典。

### 2.10 analyze_tags

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| domain | string | no | null | domainフィルタ |
| include_domain_tags | bool | no | false | trueでdomain:タグも分析対象 |
| focus_tag | string | no | null | 特定タグにフォーカス |
| min_usage | int | no | 2 | 孤児判定閾値 |
| top_n | int | no | 20 | co_occurrences の返却件数 |

**返り値**: `{co_occurrences, clusters, orphans, suspected_duplicates, notes_over_budget}`。`orphans`の各要素には`archived`（bool）と`archived_reason`（archived時のみ非null）が付く。`notes_over_budget`はnotesの文字数が推奨上限（tag notesのラチェット天井と同じ値）を超えているタグの一覧で、各要素は`{tag, length, ceiling, archived, archived_reason}`（length降順）。`domain`/`include_domain_tags`/`min_usage`等の分析スコープに関わらず、notesを持つ全タグを対象に走査する（他3セクションとは独立の全件監査）。

### 2.11 add_activity

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| title | string | yes | - | タイトル |
| description | string | yes | - | 詳細説明 |
| tags | list[string] | yes | - | 1個以上。`domain:` と `intent:` 必須 |
| related | list[RelatedRef] | no | null | 関連エンティティ |
| pins | list[PinRef] | no | null | `[{"type": "tag"\|"activity"\|"topic"\|"decision"\|"log"\|"material", "ref": int\|string}]`。作成されたactivity自身をsourceにpinを張る。refはadd_pinのtarget_refと同じ形式（tagのみnamespace:name文字列可） |
| check_in | bool | no | true | 作成後にcheck_inを実行するか |

**返り値**: 作成されたアクティビティ情報。check_in=Trueの場合は `check_in_result` を含む。
**pinsのエラー**: いずれか1件でも解決に失敗すると、activity作成自体（activity_tags・relationsを含む）を巻き戻す。部分成功はしない。

### 2.12 get_activities

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| tags | list[string] | no | null | AND条件 |
| status | string | no | "active" | `active`/`pending`/`in_progress`/`completed`/`snoozed`/`shelved` |
| limit | int | no | 5 | 取得件数上限 |
| since | string | no | null | ISO日付（以降） |
| until | string | no | null | ISO日付（以前） |

**返り値**: `{activities: [Activity], total_count: int, archived_tags: [{tag, archived_reason}]}`。statusの`active`は pending+in_progress のエイリアス（snoozed/shelvedは含まない）。`archived_tags`は応答に含まれるアクティビティのタグのうちarchivedなものの集約で、該当なしでも常に空配列で付く。
**副作用**: 呼び出し時、updated_atがSNOOZE_DURATION_DAYS（デフォルト3日）を超過したsnoozedアクティビティをpendingへ一括自動復活させる。

### 2.12b get_overview

「今何が進んでいて、次に何をすべきか」を4節（working / recently_done / awaiting_human / backlog）に集計して1回で返す。読み取り専用で、DBへの書き込みを一切行わない（`get_activities`が持つsnoozed自動復活も起こさない）。

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| days | int | no | 7 | working節の鮮度窓・recently_done節の遡り窓（日数、1以上） |
| limit | int | no | 20 | 各節が返す要素数の上限（節ごとに独立に適用、1以上。100超は100に丸める） |

**返り値**: `{generated_at, params, working, recently_done, awaiting_human, backlog}`

- `generated_at`: 集計時刻（UTC、`YYYY-MM-DD HH:MM:SS`）
- `params`: 実際に集計へ使われた実効値。`limit`は100に丸めた後の値、`heartbeat_timeout_minutes`は`HEARTBEAT_TIMEOUT_MINUTES`の実効値（引数化しない）
- `working`: 今動いているもの。`{items: [{id_raw, title, status, domains, last_touch_at, is_live, days_since_touch, open_ask_count}], count, total_count}`。`is_live`はheartbeatがタイムアウト以内かの真偽値
- `recently_done`: 最近終わったもの。`{items: [{id_raw, title, status, domains, updated_at, days_ago}], count, total_count}`
- `awaiting_human`: 人間の裁定待ち（statusが`open`のask）。`{items: [{id_raw, question, kind, choices, occurrence_count, first_seen_at, days_open, domains, blocks}], count, total_count, triage_pending_count, triage_pending_items}`。`kind="meta"`のaskは`items`・`triage_pending_items`いずれも`limit`を超えて他のaskが多数存在していても必ず含み、両配列内で非メタaskより先頭に並ぶ。`triage_pending_count`は回答済み未トリアージ（`status='answered' AND triage IS NULL`）の件数、`triage_pending_items`はそのaskを`items`と同じ形状（`{id_raw, question, kind, choices, occurrence_count, first_seen_at, days_open, domains, blocks}`）で列挙したもの（件数のみだった従来の`triage_pending_count`はそのまま残す）
- `backlog`: それ以外の残り。`{total_count, stale_in_progress_count, by_status, by_domain, no_domain_count}`。個別アクティビティは返さない

**副作用**: なし。

**caveat**: `activities`に完了時刻カラムは存在しないため、`recently_done`の完了日時は`updated_at`で近似する。完了済みアクティビティのタグ等を後から編集すると`updated_at`がbumpされ再浮上するため、この節の件数を「直近の完了数」として扱わないこと。`days`日より古い`completed`は`recently_done`にも`backlog`にも現れない（`backlog`の対象statusはpending/in_progress/snoozed/shelvedのみで完了系を含まないため）。

### 2.13 update_activity

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| activity_id | int | yes | - | 対象ID |
| status | string | no | null | 上記5値のいずれか |
| title | string | no | null | 新しいタイトル |
| description | string | no | null | 新しい説明 |
| tags | list[string] | no | null | 全置換。1個以上 |
| closed_by | string | no | null | activityを閉じた意思の主体（`"user"`\|`"claude"`\|`"external"`）。status="completed"と同時のときだけ受け付ける |
| closed_reason | string | no | null | 閉じた理由（自由文）。status="completed"と同時のときだけ受け付ける |

**副作用**: snoozed状態のアクティビティにstatusを指定せず他フィールドのみ更新すると、自動的にstatus="pending"へ復活する。

**closed_by/closed_reason**: completedでないactivityをcompletedにする呼び出しでだけ`closed_at`・`closed_by`・`closed_reason`を書く（既にcompletedのactivityにstatus="completed"を渡しても書き換えない）。`closed_by`引数を省略し、紐づくgoalが判定済みなら`"goal_judge"`がサーバー側で書かれ、`closed_reason`も省略時は`goals.judge_note`が使われる。それ以外で省略時は`closed_by`はNULL（不明）になる。`"goal_judge"`自体は引数としては受け付けない（VALIDATION_ERROR）。

**goal_hint**: status="completed"の呼び出しでは、紐づくgoalが未判定（closed=0）なら応答に`goal_hint`（`{goal_id_raw, handle, label, next, open_activities_left, open_questions?, warning?}`）を添える。判定は拒否しない。紐づく未完了のactivityが残っていない（`open_activities_left`=0）ときは`warning`が載る。組み立てで例外が出ても完了自体は失われず、`goal_hint`に`{"error": {"code": "DATABASE_ERROR", ...}}`が入る。

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
| content | string | no | null | mode次第で全体置換/先頭追記/末尾追記 |
| title | string | no | null | 新しいタイトル |
| tags | list[string] | no | null | 全置換 |
| source | string | no | null | 新しい出自 |
| mode | string | no | "overwrite" | `overwrite`/`prepend`/`append`。contentの結合動作。`overwrite`=上書き（既定）、`prepend`=新content+区切り+既存content、`append`=既存content+区切り+新content。区切りは`\n\n`。既存contentが空文字列ならoverwrite相当。contentを指定しない場合（None）はmodeは無視される |

**制約**: content/title/tags/sourceの少なくとも1つは指定する。

### 2.16 get_material

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| material_id | int | yes | - | 資材のID |
| include_retracted | bool | no | false | trueで取り消し済みの資材も取得できる |

**返り値**: 資材の全文（material_id, title, content, source, tags, created_at, retracted_at?）。`retracted_at`は`include_retracted=true`で取り消し済みの資材を取得した場合のみ付く。flavor共通引数（後述）に対応する。

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

**返り値**: `{coverage, activity, goal, related_topics, related_activities, pinned, tag_notes, materials, recent_decisions, latest_log, logs, catalog, summary, session}`。セッション内でcheck_inを初めて呼んだときのみ`flow_guide`（コンテキスト取得の手がかり）も含まれる。

`goal`は`activity`の直後にあり、そのactivityのgoal機構上の現在状態と次の一手を1件返す（goal機構自体は2.50〜2.53参照）。未定義（`label="undefined"`）・不要印（`label="not_needed"`）・goal付き（`label="active"|"judge_ready"|"closed"`）のいずれかで、goal付きなら`next`（今やるべきこと1件）を含む。組み立てで例外が出ても他のキーは失われず、`goal`キーに`{"error": {"code": "DATABASE_ERROR", ...}}`が入る。flavor指定時はremaining/terminal内の束縛先表示とopen_questionsのtitleだけが展開され、goalの文（statement・条件文・note等）は展開されない。
このactivityを`add_ask`のblocksでblockしているaskが1件以上あるときのみ`asks: {awaiting_answer, awaiting_triage}`が追加される（無ければキー自体が無い）。`awaiting_answer`はstatus='open'のask一覧（各`{id_raw, question, last_seen_at}`）、`awaiting_triage`はstatus='answered'かつ未トリアージのask一覧（各`{id_raw, question, answer_body, last_seen_at}`）。activities.statusがcompleted以外のときのみ配達され、promoted/dismissed/withdrawn済みのaskは配達されない。`awaiting_triage`が1件以上あるときは`hints`にも「answered状態のaskが未トリアージです。triage_askでpromote/dismissへ振り分けてください。」という文言が1件追加される。この`asks`関連のhintsは、答え待ちである事実をhintではなく状態情報として扱う。activityが紐づくdomain:タグのnotesが推奨文字数の上限を超えている場合、`hints`に整理を促す文言（`notes_over_budget`）も1件追加される。他のimmediate hintと異なり恒久抑制マーカーは効かず、超過が解消するまで発火し続ける（`demote_tag_notes`でnotesを資材へ退避して縮めることを想定した設計）。
`session`は呼び出し元のClaude Code CLIプロセスを解決できた場合`{"name": str, "alias": str, "alias_collision": bool}`、解決できない場合（非CLIクライアント、launcher登録が間に合っていない起動直後等）は`{"registered": false, "reason": "cli_unresolved"}`。このセッション別名レジストリ更新はベストエフォートであり、失敗してもcheck_in本体は成功応答を返す。`alias_collision`がtrueのときは`hints`にも衝突を知らせる文言が追加される。詳細は2.42bを参照。
**副作用**: statusがin_progress以外なら自動的にin_progressに更新。
**呼び出し基準**: 既存アクティビティに関連する作業を始めるとき。summaryフィールドはそのまま出力することが推奨される。

### 2.19 add_relation / remove_relation

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| source_type | string | yes | - | `topic`/`activity`/`material`/`decision`/`log` |
| source_id | int | yes | - | 起点ID |
| targets | list[RelatedRef] | yes | - | ターゲット |
| relation_type | string | no | "related" | `related`/`depends_on`/`supersedes`/`destabilizes`/`belongs_to` |

**制約**: `depends_on` はactivity同士のみ、`supersedes`/`destabilizes` はdecision同士のみ有効。
**親帰属の自動書き込み**: 子（activity/material/decision/log）→topicの関連付けは、`relation_type` が `related`（デフォルト）または明示的な `belongs_to` のときに限り `belongs_to` として書き込まれる。`depends_on`/`supersedes`/`destabilizes` を指定するとtargetがtopicのためバリデーションエラーになり何も書き込まれない。この帰属はget_decisions/get_timeline/check_inのトピック帰属集計やget_by_idsのtopic_id解決の基盤になっており、`remove_relation` で `related`/`belongs_to` を指定すると帰属関係ごと削除される。
**`destabilizes`**: sourceがtargetの前提を揺るがし再検証が必要になったとマークする。`supersedes`と違いpin transferは発生させず、targetの結論そのものは維持される。循環禁止は`supersedes`と合算判定する（循環時は`CIRCULAR_DESTABILIZES`）。`remove_relation`では削除できない（`INVALID_RELATION_TYPE`を返す。履歴として残す設計のため、解消は下記`resolve_destabilization`を使う）。
**返り値**: `{added: int}` または `{removed: int}`。重複は冪等。

### 2.20 get_map

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| entity_type | string | yes | - | 起点の種別 |
| entity_id | int | yes | - | 起点ID |
| min_depth | int | no | 0 | 0=起点自身を含む |
| max_depth | int | no | 2 | 上限10 |

**返り値**: `{entities: [{type, id, title, tags, depth}], total_count: int}`。decision/logノードは経由ノードとして使うが、返却カタログにはtopic/activity/materialのみ含まれる。

### 2.20b collect_export_candidates

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| roots | list[{type, id}] | no | [] | 起点（複数可）。tag_rootsのみでシードする場合は省略可 |
| max_depth | int | no | 2 | rootsからの走査深度上限（上限10）。tag_rootsのシードには適用されない |
| include_types | list[string] | no | 5型全部 | 返却する型の表示フィルタ。走査・closure_warnings判定には影響しない |
| tag_roots | list[string] | no | null | 指定タグ文字列を持つ全エンティティを深度0固定でシード集合に合流させる |
| include_snippets | bool | no | true | falseで各candidateからsnippetキーを省く |
| limit | int | no | null | 返却candidates件数の上限 |
| offset | int | no | 0 | 返却開始位置 |

**返り値**: 成功時 `{candidates: [{type, id_raw, title, snippet, tags, depth, size_chars, parent_topic_title, retracted?, superseded?, status?}], closure_warnings: [{kind, from_title, target_title, target: {type, id_raw}}], total_count: int, truncated: bool}`。`retracted`はdecision/log/materialのみ、`superseded`はdecisionのみ、`status`はactivityのみ付く。`tag_roots`指定時のみ`co_tags: [{tag, overlap, share}]`が追加される。失敗時 `{error: {code: "VALIDATION_ERROR" | "INVALID_ENTITY_TYPE" | "INVALID_PARAMETER" | "DATABASE_ERROR", message}}`。
**get_mapとの違い**: get_mapはnavigation用途でdecision/logを経由ノードとしてのみ扱いカタログに含めないが、本ツールはexport判断のため5型全部をカタログ本体に含める。走査自体は共有のrelation走査ロジックを使うが、ツールとしては独立している。
**動作**: rootsからの走査結果とtag_rootsのシード結果（tag_rootsは深度0固定、グラフ拡張はしない）を合流し、型別の付加情報を付けて返す。`closure_warnings`は選択集合外を指すsupersede関係・本文中citation（`{{cite:X#NNN}}`）を検出する（供に情報提供のみで、自動的な集合拡張は行わない）。read-only（DBへの書き込みは一切行わない）。

### 2.20c set_instance_identity

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| instance_id | string | yes | - | DNSラベル風（`^[a-z][a-z0-9-]{2,31}$`、英小文字始まり・英小文字数字ハイフンのみ・3〜32字） |
| force | bool | no | false | trueで既存の設定を上書きする |

**返り値**: 成功時 `{instance_id, created_at}`。失敗時 `{error: {code: "VALIDATION_ERROR" | "ALREADY_EXISTS" | "DATABASE_ERROR", message}}`。
**動作**: バンドルの複合キー（`<instance_id>:<型コード><ローカルID>`、例: `team-a:M12`）発行の基盤となるインスタンス識別子を設定する。一度設定したら`force`無しでは変更不可（複合キーは出生インスタンスの識別子を基準に発行され続けるため、変更は既発行キーの意味を壊す破壊的操作）。完全自由命名で衝突保険のランダムsuffix自動付与はしない。

### 2.20d export_bundle

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| items | list[{type, ids}] | yes | - | 確定選択（`collect_export_candidates`の出力から絞り込んだ最終リスト） |
| bundle_name | string | no | null | バンドルディレクトリ名。省略時は`<instance_id>-<日時>-<起点slug>` |
| include_supersede_targets | bool | no | false | trueで選択decisionのsupersede先実体も同梱する |
| selection | dict | no | null | `collect_export_candidates`への入力をverbatimで記録する任意dict。manifest.yamlにそのまま書き込まれる |

**返り値**: 成功時 `{path, bundle_id, counts: {type: n}, auto_included: [{type, id_raw, reason}], unresolved_refs: [{key, type, title, domain_tags, referenced_by}], masked_literals: int, warnings: [{kind, from_title, target: {type, id_raw}}]}`。失敗時 `{error: {code: "VALIDATION_ERROR" | "INSTANCE_ID_NOT_SET" | "NOT_FOUND" | "IO_ERROR" | "DATABASE_ERROR", message}}`。
**動作**: `~/cc-memory-export/bundles/<bundle-name>/`配下（パスガードで配下外を拒否）にmanifest.yaml + エンティティ別mdファイルを書き出す。選択されたdecision/logの親topicは機械規則で自動同梱される（activityには適用しない）。本文中の内部参照は3段パイプライン（生リテラル正規化 → 複合キー化 → 残存リテラルの最終スイープ）で変換し、選択集合外を指す参照は`unresolved_refs`に集約される。read-only（DBへの書き込みは一切行わない。ファイル書き込みのみ）。

### 2.20e import_bundle

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| bundle_path | string | yes | - | `export_bundle`が書き出したバンドルディレクトリのパス（`manifest.yaml`を直下に持つ）。パスガードでDEFAULT_EXPORT_DIR配下外を拒否 |
| mode | string | no | "dry_run" | "dry_run"（DB無変更で衝突検知レポート）または"apply"（実際にDBへ書き込む） |
| resolutions | dict | no | null | mode="apply"向けの裁定結果。`{tag_renames: {incoming_tag: local_tag}, on_upstream_change: {entity_type: "overwrite"\|"skip"}, entity_overrides: {composite_key: "skip"\|{action: "skip"\|"import"}}}`。dry_runでは無視される |
| skip_duplicate_check | bool | no | false | trueでネイティブ重複疑い検知（embedding類似検索）をスキップする（dry_runのみ関係） |

**dry_run 返り値**: 成功時 `{format_version_ok: bool, bundle_id, source_instance, summary: {type: {new, unchanged, updatable, upstream_changed_skip, self_origin}}, upstream_changed: [{key, type, title, local_entity_id}], tag_report: {merge, create, archived_hit, alias_hit}, duplicates_suspected: [{key, title, similar: [{type, id_raw, title, score}]}], dangling_refs: {count, sample}, degraded: bool, load_errors}`。
**dry_run 動作**: バンドルを読み、DBへの書き込みを一切行わずに衝突検知レポートを返す。再import判定は`import_provenance`逆引き（origin一致+hash一致は`unchanged`、hash不一致はtopic/activity/materialなら`updatable`、decision/logなら既定skipの`upstream_changed_skip`）で行う。参照解決（belongs_to/related/supersedes/depends_on・本文中の拡張cite）はバンドル内→provenance逆引き→自インスタンス出生→解決不能、の優先順で試み、解決不能分は`dangling_refs`に集計する。タグは4区分（merge/create/archived_hit/alias_hit）でレポートし、domainタグまたはnotesを持つエントリは`review_required=true`になる。重複疑い検知はstatus="new"のエンティティのみ対象で、embeddingサーバー未起動時は`degraded=true`になるがクラッシュしない。

**apply 返り値**: 成功時 `{format_version_ok: bool, bundle_id, source_instance, created: {type: n}, updated: {type: n}, skipped: {type: n}, skip_reasons: {status: n}, created_edges: int, dropped_edges: int, unresolved_body_refs: int, warnings, load_errors}`。失敗時は共通で `{error: {code: "VALIDATION_ERROR" | "NOT_FOUND" | "INSTANCE_ID_NOT_SET" | "DATABASE_ERROR", message}}`。
**apply 動作**: dry_runと同じ分類ロジックを土台に、resolutionsを反映して実際にDBへ書き込む（topic→activity/material→decision/log→relations/supersedes/depends_on→本文citation書き換えの順に適用し、全体を1トランザクションで実行、失敗時は部分書き込みを残さない）。参照解決は4段の優先順（バンドル内→provenance逆引き→自インスタンス出生→解決不能）で行い、解決できたエッジ・citationはローカルIDへ張り直す。解決不能な本文中citationは「{title}」(未取り込みの外部記録)に置換し、解決不能なfrontmatterエッジは張らずに`dropped_edges`へ計上する。新規エンティティのcreated_atはimport実行時刻を採用する（originのcreated_atは`import_provenance.origin_created_at`に保持）。タグは新規作成分にincoming notesを設定し、既存の非archived非alias平タグには差分行のみ追記する。activityは明示選択されたもののみが対象。新規作成時はstatusをバンドルの値のまま採用するが（自動でshelvedへ変換しない）、既存を上書き更新する場合はローカルのstatus/retracted_atを保持し変更しない。タグ紐付けは`INSERT OR IGNORE`による追加のみで、送信元でタグが外れても既存の紐付けは自動削除されない。FTS同期はDBトリガー任せ、embedding/vec同期はcommit後にベストエフォートで行う。

### 2.21 add_habit / get_habits / update_habit

- `add_habit(content: string, importance_score: int = 3, status: string = "active") -> dict`: habitを登録。新規habitは`trigger_mode='intelligently'`（マニフェスト表示のみ）で作成され、`~/.claude/rules`配下の自動生成ファイル経由で常時配信されるのは`'always'`のみ（セッション途中の登録は次セッション起動から反映）。常時配信層への昇格は`update_habit(trigger_mode='always')`で行い、後述のゲートを通過する必要がある。importance_scoreは1(critical)/2(important)/3(default)のいずれかで、intelligently層マニフェストのソートに使う。statusは`'active'`/`'archived'`のいずれか。
- `get_habits(active: bool = true, habit_id?: int) -> dict`: 登録済みhabit一覧。既定でactive=1のみ返す。無効化済みも含む全件が欲しいときは`active=false`を渡す。`~/.claude/rules`配下の自動生成ファイルで全文配信されるのは`trigger_mode='always'`のみで、`'intelligently'`はタイトルのみのマニフェスト表示になる。`habit_id`を渡すとその1件だけを本文付きで取得でき、intelligentlyな振る舞いの詳細を引くときに使う（取得と同時に`last_recalled_at`が更新される）。
- `update_habit(habit_id: int, content?: string, active?: bool, trigger_mode?: string, description?: string, importance_score?: int, status?: string) -> dict`: active=Falseで無効化。trigger_modeは`'always'`（`~/.claude/rules`配下の自動生成ファイルで全文常時配信）/`'intelligently'`（マニフェストのみ表示、詳細は`get_habits(habit_id=...)`でon-demand取得）のいずれか。`'intelligently'`から`'always'`への昇格には、contentが100字未満であること、かつ昇格後のalwaysプール合計文字数が昇格前の合計以下または定員（`CALM_ALWAYS_POOL_CAPACITY`、既定1,500字）以下のいずれかを満たすことを要求するゲートがある（違反時はVALIDATION_ERROR）。降格・無効化は無条件で許可される。descriptionはintelligently層のマニフェスト表示に使う要旨（100文字以内）。importance_scoreは1(critical)/2(important)/3(default)のいずれかでマニフェストのソートに使う。statusは`'active'`/`'archived'`のいずれかで、`'archived'`はマニフェストから除外される。

### 2.22 add_pin / remove_pin

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| source_type | string | yes | - | `tag`/`activity`/`topic`/`decision`/`log`/`material` |
| source_ref | int \| string | yes | - | ID整数、tag種別のみ文字列可（"domain:calm"） |
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

**動作**: 論理削除。検索・取得でデフォルト除外される（include_retracted=Trueで含められる）。retract時はsearch_index/FTS/vecインデックスからも物理削除される。undo（un-retract）時はretracted_atをNULLに戻すと同時に、search_index/FTSへも再登録し直され、再び検索でヒットするようになる（vecインデックスはcommit後にベストエフォートで再登録）。

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

引数なし。返り値: `{heartbeat_timeout, in_progress_limit, pending_limit, recency_decay_rate, sync_disable_retrospective, sync_policy, snapshot_interval_hours, snapshot_max_count, snapshot_anomaly_threshold, precedent_budget_chars, budget_defaults, read_tool_limits}`。スキルが環境変数ベースの設定を参照するときに使う。`budget_defaults` は `budget_service` が把握する予算関連の既定値一覧（`precedent_budget_chars` / `recency_decay_rate` / `recency_decay_floor` / `recency_decay_floor_decision_live` / `precedent_response_chars_max`。いずれもsrc.config由来）。

### 2.26 roll_dice

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| sides | int | no | 10 | サイコロ面数 |

**返り値**: `{result: int}`。

### 2.29 report_signal

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| kind | string | yes | - | `machine_error` / `friction` / `contradiction` / `precedent_miss` / `precedent_misapplied` / `boundary_case` / `rollback` / `goal_rollback` の8種のいずれか。`goal_rollback`は`update_goal`の`reopen_reason`（goal判定の差し戻し）が書く専用のkindで、手で報告するものではない |
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

**返り値**: `{guarantee, routing, topics, budget, truncated, materials_truncated}`。`guarantee`は`enumerated`（routing成立・全件列挙完了）/ `routing_miss`（近傍topicなし）/ `routing_unavailable`（embeddingサーバー停止）のいずれか。`routing.mode`は`vector`（embedding routingで解決）/ `explicit`（topic_ids指定でrouting skip）/ `unavailable`（embeddingサーバー停止でrouting不能）。`routing.candidates`は各`{topic_id_raw, title, distance, selected}`（topic_ids指定時はdistanceなし。存在しないtopic_idを指定した場合は`{topic_id_raw, error: "not_found"}`）。`topics[].decisions`各要素は`detail="full"`（本文展開）または`detail="index"`（id/title等のみ、`get_by_ids`で本文追補可）。`detail="full"`のdecisionには`archived_tags`（{tag, archived_reason}の配列、該当なしでも空配列で常に付く）が付く。`detail="index"`のdecisionはtags自体を持たないためarchived_tagsも付かない。`budget`は本文予算（`budget_chars`）の配分結果（`limit/used/full/index_only`）に加え、レスポンス全体の実測文字数が実サイズ上限（既定32000字、`CALM_PRECEDENT_RESPONSE_CHARS_MAX`）を超えた場合の追加降格結果を`response_chars`（`{limit, measured, demoted}`）として持つ。full itemは配分順の逆順で`detail="index"`へ`demoted`件数分降格され、それでも超過するときは`topics[].materials`が`{type, id_raw, title}`のみへ縮退し`materials_truncated=true`になる。`response_chars`は`guarantee=enumerated`かつ対象decisionが1件以上のときのみ付与され、`routing_miss`/`routing_unavailable`時や対象topicのdecisionが0件のときは`budget`に`response_chars`キー自体が無い（この場合の状態は`guarantee`が既に開示している）。
**動作**: `search`がランクtop-Nの確率的発見であるのに対し、本ツールは選ばれたtopicの非retract decisionを全件（最低でも索引粒度で）応答に含めることを保証する。read-only（statusを更新する副作用なし）。
**関連**: 設計・裁定の前に近傍topicの判例を網羅確認したい場面で`get_decisions`/`check_in`のChoose節から参照される。

### 2.42b get_sessions / set_session_alias

Claude Codeセッション間の「CLI表示名（例: `workspace-a2`）→人間可読な別名」対応表。`ListAgents`のPeer sessions一覧をユーザーに提示する前に、生の自動生成名を別名へ変換するために使う。ローカルファイル `~/.cc-memory/session_aliases.json` の読み書きのみで完結する。

別名は各セッションが`check_in`したアクティビティタイトルから自動生成される（先頭の`[議論]`/`[作業]`等の区分プレフィックスは残し、24文字を超える場合は省略記号「…」で切り詰める）。他セッションの別名と衝突した場合は`-2`, `-3`…のサフィックスが自動で付く。手動で付けた別名（`set_session_alias`）は同じアクティビティへの再check_inでは保持されるが、別のアクティビティへcheck_inし直すと自動生成の別名に戻る。

**get_sessions**: 引数なし。
**返り値**: `{"sessions": [{"name": str, "alias": str, "alias_source": "derived" | "manual", "activity_id": int | null, "activity_title": str | null, "activity_status": str | null, "cwd": str | null, "is_self": bool, "updated_at": str}, ...], "count": int}`。`updated_at`降順。呼び出し元自身の行は`is_self: true`。CLIプロセスが消滅したセッションの行は自動的に除外される。

**set_session_alias**

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| alias | string | yes | - | 1〜24文字。前後の空白は除去される。改行・制御文字は不可 |

**返り値**: 成功時 `{"name": str, "alias": str, "requested_alias": str, "collided": bool}`。`collided`がtrueのとき`alias`は衝突回避で接尾辞（-2, -3…）が付いた値になっている。失敗時 `{"error": {"code": "VALIDATION_ERROR" | "SESSION_UNRESOLVED" | "NOT_REGISTERED", "message": str}}`。`SESSION_UNRESOLVED`は呼び出し元のClaude Code CLIプロセスを解決できなかったとき、`NOT_REGISTERED`は未check_in（先にcheck_inが必要）のとき。

**関連**: `check_in`のレスポンス`session`フィールド（2.18参照）で、check_in自身のセッションについても同じ別名が確認できる。

### 2.43 add_ask

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| question | string | yes | - | 問い本文（空不可、500字以内） |
| blocks | list[int] | yes | - | この問いが答え待ちで止めているactivityのid一覧（1件以上必須）。全て存在するactivityであること。全てcompleted状態のときはエラー |
| tags | list[string] | yes | - | タグ配列（1個以上必須）。`domain:`タグを最低1つ含むこと。素タグは任意。`tag_service.resolve_tags`（完全一致・KNN統合）で解決する |
| kind | string | no | "ask" | `"ask"`（通常ask）または`"meta"`（メタask） |
| context | string | no | null | 背景（8000字以内） |
| choices | list[string] \| null | no | null | 選択肢テンプレート（最大3件、1件100字以内）。AskUserQuestion風の選択式UIをダッシュボード等で組み立てるための添え物。回答（`answer_ask`）は引き続き自由文字列のまま |
| notify | bool | no | true | 通知希望（`notify_wanted`）。trueなら`answer_ask`/`triage_ask`(dismiss)完了時に返り値の`notify_path`へ完了通知が追記される。falseにするか後から`unsubscribe_ask`で外すと通知が来なくなる（pullでの確認には影響しない） |

**返り値**: `{id: int, deduped: bool, occurrence_count: int, notify_path: string, similar_precedents: [...], similar_asks: [...]}`。`similar_precedents`/`similar_asks`はそれぞれ近傍のdecision/ask最大3件（embeddingサーバー未起動時は空配列）。`notify_path`はこの時点では存在しない場合がある（`answer_ask`/`triage_ask`側が初めて書き込む瞬間に生成されるため）。
**動作**: question/contextの構成は`ask-compose` skillを必ず経由すること。同じ問い（正規化後questionのfingerprint一致）が答え待ち（open）で既にあれば新規行を作らず`occurrence_count`を+1し、blocks/要求元セッションはUNIONで追記、context/最終出現時刻は今回の値で上書きする。answered/promoted/dismissed/withdrawnの同一問いは別のライフとして新規行になる（訂正は新規postで行い、supersedes等のリンクは張らない）。dedup時（同一fingerprintのopen ask再post）は今回渡したtags/kind/choices/notifyを無視し、初回投入時の値を保持する。レスポンスのsimilar_asks（裁定内容込み）を読み、同型の問いが繰り返され裁定が一貫していると判断した場合は、`ask-distill` skillでメタaskの起票を検討する。
**エラー処理**: question空・500字超、context 8000字超、blocks空・存在しないactivity id含む・全てcompleted状態、同一fingerprintの直近withdrawから5分未満の再post、kindが"ask"/"meta"以外、choicesが0件または4件以上・要素が空文字列・101字以上はいずれも`VALIDATION_ERROR`。tagsが空・namespace不正等は`TAGS_REQUIRED`/`INVALID_TAG_NAMESPACE`/`INVALID_TAG_NAME`、`domain:`タグを含まない場合は`VALIDATION_ERROR`。

### 2.44 get_asks

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| status | string \| null | no | "open" | `open`/`answered`/`promoted`/`dismissed`/`withdrawn`。nullで全status横断。triage_pending_only指定時は無視される |
| blocking_activity_id | int | no | null | 指定時はそのactivityをblockしているaskだけに絞る |
| triage_pending_only | bool | no | false | trueでstatus='answered'かつ未トリアージのみに絞る |
| tags | list[string] \| null | no | null | 指定時はAND条件でフィルタ、未指定時は全件 |
| kind | string \| null | no | null | `"ask"`/`"meta"`。nullでフィルタなし |
| ids | list[int] \| null | no | null | 指定時はこのask idの集合だけに絞る（他のフィルタとAND条件）。空配列はids条件なし扱い。statusは既定"open"のままなので、状態を問わず引き当てたい場合はstatus=nullも併せて指定する |
| limit | int | no | 20 | 最大100 |
| offset | int | no | 0 | ページネーション |
| include_stats | bool | no | false | trueでstatus別クロス集計と直近30日サマリを付与 |

**返り値**: `{asks: [...], total_count: int, stats?: {by_status, last_30d}}`。各askにblocks（`[{id_raw, title, status}]`）、requesters（要求元session_idの文字列リスト）、tags（タグ文字列のリスト）が合流される。タグnotesは返さない。`choices`はadd_ask時に指定していればstring配列、未指定ならnull。`notify_wanted`（0または1）は通知希望の有無（`add_ask`の`notify`引数、または`unsubscribe_ask`での解除状態）を示す。

### 2.45 answer_ask

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| ask_id | int | yes | - | 対象ask ID |
| answer_body | string | yes | - | 回答本文（空不可、8000字以内） |

**返り値**: `{id: int, status: "answered", triage_pending: true, blocked_activities: [int, ...], next_step: string}`。
**動作**: トリアージ（promote/dismiss）はここでは行わない。次のcheck_inでの配達か`get_asks(triage_pending_only=true)`で拾われるまで遅延する。対象がopen状態でない場合は`VALIDATION_ERROR`（1問1答、再回答は拒否）。対象askの`notify_wanted`がtrueなら、状態更新に加えて`notify_path`（`add_ask`の返り値参照）へ完了通知を1行追記する。書き込み失敗はログのみで、状態更新の成否には影響しない。

### 2.46 triage_ask

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| ask_id | int | yes | - | 対象ask ID |
| action | string | yes | - | `promote` または `dismiss` |
| decision | string | action=promoteのとき必須 | null | 生成するdecisionの内容 |
| reason | string | action=promoteのとき必須 | null | 生成するdecisionの理由 |
| title | string | no | null | decisionの見出し（35字以内） |
| tags | list[string] | no | null | decisionに付けるタグ |
| topic_id | int | action=promoteのとき必須 | null | 生成するdecisionを紐付けるトピックID |
| dismiss_reason | string | action=dismissのとき必須 | null | 見送り理由 |

**返り値**: promote時 `{id: int, status: "promoted", promoted_decision_id: int}`、dismiss時 `{id: int, status: "dismissed"}`。promote時、対象askが`kind="meta"`のときのみ`next_step: str`が追加で含まれる。
**動作**: promoteはdecision/reason/title/tags/topic_idをそのまま`add_decisions`に渡してdecisionを生成し、promoted_decision_idとして紐付ける。いずれもこのaskが止めていたactivityのblockを解除する（ask_blocksを削除）。`kind="meta"`のpromoteは、`rule-placement` skillに従いhabits/tag-notes/pin/判例decision/rules等への配置を先に済ませてから呼ぶ。dismissかつ対象askの`notify_wanted`がtrueなら、`answer_ask`と同じ`notify_path`へ完了通知を1行追記する（promoteでは書かない。`answer_ask`時点で既に一度通知済みのため）。
**エラー処理**: 対象がanswered かつ未トリアージでない場合、action不正、promote時のdecision/reason/topic_id欠落、dismiss時のdismiss_reason欠落はいずれも`VALIDATION_ERROR`（topic_id欠落は`add_decisions`側の必須バリデーションに起因する）。promote処理中にdecision生成が失敗した場合はask側の状態変更もロールバックされ`answered`のまま残る。

### 2.47 withdraw_ask

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| ask_id | int | yes | - | 対象ask ID |
| reason | string | yes | - | 取り下げ理由（空不可） |

**返り値**: `{id: int, status: "withdrawn"}`。
**動作**: 答え待ち（open）のaskを人間の回答を待たずに取り消す。取り下げ後はask_blocksを削除するが、要求元セッションの記録（ask_requesters）は参照ログとして残す。同一fingerprintの再postは、誤操作保護のため取り下げから5分間拒否される（session条件は課さない）。
**エラー処理**: 対象がopen状態でない場合は`VALIDATION_ERROR`。

### 2.47b unsubscribe_ask

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| ask_id | int | yes | - | 対象ask ID |

**返り値**: `{id: int, notify_wanted: false}`。
**動作**: 対象askの`notify_wanted`をfalseにする。statusは問わずいつでも呼べる（既に回答済み・却下済みのaskに対しても呼べる）。「サブスクを外した＝完全に見えなくなる」ではなく、以後`answer_ask`/`triage_ask`(dismiss)が実行されても`notify_path`への書き込みが行われなくなるだけで、pull（`check_in`/`get_asks`）では引き続き通常通り見える。
**既知の制約**: 同一の問いが複数セッションから`add_ask`された（要求元セッションが2件以上、`ask_requesters`が複数行）askには対応していない。`notify_wanted`は`asks`テーブルの単一列（ask単位）であり要求元セッション単位ではないため、あるセッションが外すと、まだ通知を必要としている他のセッションの通知希望も巻き添えで止めてしまう。この版では安全側に倒し、要求元セッションが2件以上のaskに対する呼び出しは状態を一切変更せず`VALIDATION_ERROR`で拒否する。要求元が1件以下（`session_id`未指定でadd_askされたaskを含む）のときは通常通り動作する。
**エラー処理**: 対象askが存在しない場合、または要求元セッションが2件以上の場合は`VALIDATION_ERROR`。

### 2.48 resolve_destabilization

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| source_decision_id | int | yes | - | destabilizesエッジのsource（軸変更decision） |
| target_decision_id | int | yes | - | destabilizesエッジのtarget（影響を受けたdecision） |
| resolution | string | yes | - | `reaffirmed`/`revised`/`retracted` |
| revised_to_decision_id | int | resolution=revisedのとき必須 | null | 改訂後の新decision ID |
| note | string | no | "" | 自由記述 |

**返り値**: `{resolved: bool, already_resolved: bool}`。
**動作**: `decision_destabilization_resolutions`にエッジ単位で1行記録し解消する。エッジ自体（`decision_supersedes`のdestabilizes行）は削除しない（履歴保存）。`resolution="retracted"`のときのみtargetを実際にretractする（`decisions.retracted_at`更新）。`reaffirmed`/`revised`ではtargetのretract状態は変化しない。
**冪等性**: 既に解消済みの同一エッジに対して再度呼んでも、2件目のINSERTや副作用（retract呼び出し等）は発生させず`already_resolved: true`を返す。
**エラー処理**: `resolution`が3値以外、または`resolution="revised"`で`revised_to_decision_id`が未指定の場合は`VALIDATION_ERROR`。

### 2.49 suggest_destabilized_candidates

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| source_decision_id | int | yes | - | 軸変更decisionのID |
| k | int | no | 20 | 返す候補数の上限 |
| include_already_resolved | bool | no | false | resolve済み候補も含めるか |

**返り値**: `{candidates: [{decision_id, title, score, match_reason, already_destabilized, already_resolved}], mode: "vector" | "tag_only"}`。
**動作**: read-only。候補は「(a) sourceとtag集合が重なるnon-retract decision」と「(b) sourceが属するtopicのembedding近傍topicに属するnon-retract decision」の和集合で、tag_jaccard・embedding類似度（近傍topic routingのdistanceを正規化）・同一topicボーナス（same_topic_bonus）を合成したスコア降順で返す。embeddingサーバー停止時は例外にせず、embedding近傍チャネル(b)のみを無効化してタグ一致チャネル(a)の候補を`mode: "tag_only"`で返し続ける（縮退してもゼロ件にはしない）。`decision_supersedes`（kind='destabilizes'）を参照して`already_destabilized`、`decision_destabilization_resolutions`を参照して`already_resolved`を付与し、`include_already_resolved=false`（既定）ではresolve済み候補を除外する。実際にdestabilizesエッジを張るかどうかは呼び出し側の判断で、別途`add_relation(relation_type="destabilizes")`を呼ぶ。

### 2.50 set_goal

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| activity_id | int | yes | - | 対象activity |
| goal | dict \| null | yes | - | 4形式のいずれか。`{"new": {handle, statement, conditions}}`（新規作成して紐づけ）／`{"goal_id": int}`（既存の未判定goalに紐づけ）／`{"waiver": str}`（不要印）／`null`（未定義に戻す）。conditionsの各要素は`{statement, actor: "claude"\|"human"\|"external", bound: {type: "activity"\|"decision"\|"ask", id}\|null, state: "open"\|"satisfied"\|"waived"(既定open), note}`。waivedはnote必須 |
| replace | bool | no | false | 既に別内容の紐づけ・不要印がある活動に上書きするときtrue |

**返り値**: 成功時 `{goal: <goalブロック>}`。
**情報応答**: `{info: "ACTIVITY_GOAL_EXISTS", current: {...}}`（既に別内容の行があり`replace=false`）、`{info: "GOAL_CLOSED", goal: {...}}`（判定済みgoalへの紐づけ・判定済みgoalからの解除）。
**エラー**: `VALIDATION_ERROR`（形の違反）、`NOT_FOUND`（activity・紐づけ先・束縛先が無い）、`HANDLE_TAKEN`（handleの重複）、`GOAL_WOULD_ORPHAN`（未判定goalの最後のactivityを外す）、`DATABASE_ERROR`。
**動作**: 同じ内容への再送は何もせずに成功する（set_goal(new)の再送を含む）。goals/goal_conditions/goal_activitiesのINSERT/UPDATE/DELETEのみを行い、activityのstatusには触れない。書き込みはBEGIN IMMEDIATEの1トランザクション。

### 2.51 update_goal

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| goal_id | int | yes | - | 対象goal |
| changes | list[dict] | no | null | 前から順に適用するop列。`{"op": "add", ...条件の形...}`（追加）／`{"op": "set", "id", "state", "note"}`（状態を書く。waivedはnote必須）／`{"op": "edit", "id", "actor"?, "bound"?}`（担い手・束縛を変える） |
| statement | string | no | null | goalの一文の修正（未判定のgoalにだけ許す） |
| reopen_reason | string | no | null | 判定済みgoalを差し戻す理由 |

**返り値**: 成功時 `{goal: {...}, applied: int, reopened?: {verdict, judged_by, judged_at, judge_note}}`（`reopened`は差し戻し時のみ）。
**情報応答**: `{info: "GOAL_CLOSED", goal: {...}}`（判定済みgoalにreopen_reasonなしで書き込もうとした）、`{info: "GOAL_ALREADY_OPEN", goal: {...}}`（未判定goalにreopen_reasonを渡した）。
**エラー**: `VALIDATION_ERROR`（他goalの条件id、同じ条件に同じopを2回、waivedにnote無し等）、`NOT_FOUND`、`DATABASE_ERROR`。全体を1トランザクションにし、1件でもエラーなら何も書かない。
**動作**: `reopen_reason`を渡すと、changes/statementより先に`goals.closed=0`への書き戻し、closed_by='goal_judge'のactivityのpendingへの復帰、`signal_events`への1行（kind='goal_rollback'）の記録を同じトランザクションで行う。

### 2.52 judge_goal

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| goal_id | int | yes | - | 対象goal |
| verdict | string | yes | - | `achieved`（達成） \| `failed`（達成せず終了。不可能・不要化・取り下げを含む） |
| note | string | no | null | 判定理由。failedでは必須 |
| judged_by | string | no | "session" | `session`（セッション自身の判断） \| `human`（ユーザーが同席して完了を明言・同意した） |

**返り値**: 成功時 `{goal: <goalブロック(label=closed)>, closed_activities: [{id_raw, title}, ...]}`。
**情報応答**: `{info: "GOAL_ALREADY_CLOSED", goal: {...}}`。
**エラー**: `VALIDATION_ERROR`（failedでnote空）、`NOT_FOUND`、`GOAL_NOT_READY`（openの条件が残っている）、`GOAL_NOTHING_SATISFIED`（satisfiedが0件）、`GOAL_BINDING_BROKEN`（崩れた条件がある）、`DATABASE_ERROR`。achievedの判定はこの3種の前提検査を順に行う。
**動作**: `goals`の判定記録と、紐づく未完了activity全件のcompleted化（closed_by='goal_judge'）を同じトランザクションで行う。既にcompletedのactivityのclosed_*は書き換えない。

### 2.53 get_goal

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| goal_id | int | no | null | goalを直接指す（3引数のうちちょうど1つを指定する） |
| activity_id | int | no | null | activity経由で紐づくgoalを指す |
| handle | string | no | null | goalの短い名前で指す |

**返り値**: `{goal_id_raw, handle, statement, label, progress, claude, next, last_verdict, conditions: [...全件...], activities: [...], open_questions?, open_questions_more?}` | `{label: "undefined"|"not_needed", next?, reason?}`（activity_idを指定してgoalが無い場合）。
**エラー**: `VALIDATION_ERROR`（3引数のちょうど1つを指定していない）、`NOT_FOUND`（指したものが無い）、`DATABASE_ERROR`。
**動作**: 読み取り専用（check_inと違いactivityのstatusを変えない）。`conditions`は充足済みを含む全件を返す点がcheck_inのgoalブロックと異なる。`label`が`judge_ready`のときだけ、判定待ちの未決（未回答のask・`[議論中]`のまま未決着のdecision）を`open_questions`（最大3件）に載せ、超過分があれば件数を`open_questions_more`に載せる（update_goalの応答と同じ形）。

### 2.54 get_feedback_entries

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| name | string | no | null | 完全一致で1件に絞る |
| query | string | no | null | body/refへの部分一致検索 |
| include_deleted | bool | no | false | trueで削除済み（`deleted_at IS NOT NULL`）も含める |

**返り値**: `{ok: true, entries: [{id, name, body, ref, strength, timing, condition, delivered_count, overridden_count, deleted_at, created_at, updated_at, notes: [{kind, body, created_at}, ...], read_mark: int}, ...]}`。
**動作**: 読み取り専用。`read_mark`は`MAX(feedback_notes.id)`（ノートが無ければ0）。write_feedback_entryのupdate/delete、削除済み名前へのcreate（復活）はここで取得したread_markを要求する。

### 2.55 write_feedback_entry

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| name | string | yes | - | 英小文字・数字・ハイフンのみ |
| action | string | yes | - | `create` \| `update` \| `delete` |
| body | string | create/updateで実質必須 | null | エントリ本文（100字以内） |
| ref | string | no | null | 参照（500字以内） |
| strength | string | create/updateで実質必須 | null | `notify`（知らせる） \| `block`（止める。timing='pre_tool'必須） |
| timing | string | create/updateで実質必須 | null | `utterance`（発話時） \| `tool_fail`（ツール失敗時） \| `pre_tool`（実行直前） |
| condition | dict \| string | no | null | `{"tool": str\|null, "all": [{"field","op":"regex"\|"len_gt","value"}, ...]}`（all は0〜3要素。dictまたはJSON文字列） |
| read_mark | int | update/delete/復活で必須 | null | get_feedback_entriesで取得した最新値 |

**返り値**: 成功時 `{ok: true, entry: {...}}`（get_feedback_entriesの1件と同形）。
**エラー**: `{ok: false, error: {code, message, fix}}`。codeは`VALIDATION_ERROR`（形式違反、strength/timingの不整合、条件JSON不正、block+tool=null+all=[]等）、`NOT_FOUND`（update/deleteの対象が未作成または削除済み）、`CONFLICT`（read_markが古い）、`DUPLICATE`（createで既に使われている名前）、`DATABASE_ERROR`。
**動作**: create/updateはbody/strength/timing/conditionを全て渡す全置き換え（部分更新ではない）。削除済み名前へのcreateは復活として扱い、read_markを要求する（骨格「変更前に必ずノートが読まれる」を仕組みで保証するため）。復活時はdeleted_atをクリアして新しい内容で上書きし、notes・delivered_count・overridden_countは引き継ぐ。deleteは`deleted_at`をセットするのみ（物理削除しない、notesは残す）。書き込みはBEGIN IMMEDIATEの1トランザクション。

### 2.56 add_feedback_note

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| name | string | yes | - | 対象エントリの名前 |
| kind | string | yes | - | `stumble`（踏んだ・躓いた事実） \| `note`（それ以外の経緯） |
| body | string | yes | - | ノート本文（500字以内） |

**返り値**: 成功時 `{ok: true, note: {kind, body, created_at}, read_mark: int}`。
**エラー**: `{ok: false, error: {code, message, fix}}`。codeは`VALIDATION_ERROR`（kind不正・body空/超過）、`NOT_FOUND`（nameのエントリが存在しない）、`DATABASE_ERROR`。
**動作**: read_mark引数は取らない（いつでも書ける）。削除済みエントリにも足せる（観測記録は削除後も続けられる）。`feedback_notes`は追記専用（UPDATE/DELETEはDBトリガーで拒否）。

---

## 3. 共通エンティティ型

CALMが扱うエンティティの内部表現。詳細スキーマは `docs/spec/db-schema.md`（並行作成中）を参照する。本書では論理構造のみ示す。

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
- `destabilization: {destabilized_by: [source_id, ...], unresolved_count: int, latest_source: source_id | null, sources: [{decision_id, title, created_at, kind_reason}, ...]}`
  （`get_decisions`/`get_by_ids`/`check_in`のpinned.decisions/`pull_precedents`の読み出し応答のみに付く算出フィールド。
  未resolveなdestabilizesエッジ（`add_relation(relation_type="destabilizes")`で登録、`resolve_destabilization`で解消）を
  1本以上持つ場合のみ付与され、無ければキー自体が無い。`destabilized_by`と`sources`は`created_at`昇順、
  `latest_source`は最新のsource decisionのid。`is_superseded`/`supersede_chain`（結論の置き換え）とは独立に併記され、両方成立しうる）

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
- relation_type: `related | depends_on | supersedes | destabilizes | belongs_to`
- `depends_on` はactivity同士、`supersedes` / `destabilizes` はdecision同士に限定

### 3.8 Tag
- 文字列としては `namespace:name` または素タグ
- namespace: `domain` / `intent` / 空
- 補助フィールド: `notes`（教訓）、`canonical`（エイリアス先）、`description`（短い説明）、
  `archived`（退役状態、bool）、`archived_reason`（退役理由、archived時のみ非null）

---

## 4. ガード・前提

### 4.1 check-in 先行が前提のツール
- `check_in` の hints は `hint_service` 経由で生成される（recompose_bootstrap/recompose_delta/logs_sparse/direction_overflow/activity_cleanup/notes_over_budget等、詳細は`docs/architecture/components.md`の該当節を参照）。`check_in` を経由せず `update_activity` 等を直接呼ぶ運用では、これらのhintsによる示唆（整理・確認の推奨）を受け取れない。
- `check_in` を経由しないアクティビティへの操作（`update_activity` 等）は可能だが、その場合 tag_notes の自動注入は行われない。habitsのうち`trigger_mode='always'`のものは`~/.claude/rules`配下の自動生成ファイル経由でセッション起動時に配信されるため、check_inの有無に関係なく反映される（`'intelligently'`はタイトルのみのマニフェスト表示にとどまる）。

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
| `readable` | `<title>` 形式。IDなし | `[deleted item]` / `[retracted item]` | 人間への最終出力（CALM内部識別子を露出させたくない場合） |

選定基準: ユーザーに提示する最終出力なら`readable`、エージェントが内部処理を続けるなら`internal`、生データのままの特殊用途のみ`raw`。コードブロック内やエスケープ済み（`\{{cite:...}}`）のテンプレートはどのflavorでも展開されない。

---

## 補足

- 本書（Markdown、人間向け）の更新は手動のままである。機械可読版の `docs/spec/openapi.yaml` は `scripts/generate_openapi.py` が `mcp.list_tools()` から自動生成し、CIで乖離を検出する（`.github/workflows/test.yml` の `doc-gen-drift` ジョブ）。
- 個別ツールの呼び出し例（typical-call snippets）は別資料 `docs/architecture/sequences/` に分離する予定。
