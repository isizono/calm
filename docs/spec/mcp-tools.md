<!-- ccm-doc-sync
watch-tags: domain:calm, domain:cc-memory
watch-direction: true
watch-migrations: false
last-synced: 2026-09-23
last-synced-migration: 0077
-->

# CALM MCPツール仕様書 v0

## 0. 読み方

このドキュメントはツール横断の約束事と、docstringに収まらない返り値の形状だけを置く。各ツールの引数・返り値・用途は `src/main.py` の `@mcp.tool` デコレータ付き関数のdocstringが正本である。

- docstringにはクライアント側で2,048字を超えると切り詰められる制約があるため、収まりきらない返り値の形状（2.3・2.18・2.32・3.2）と、複数ツールが共通で参照する約束事（「flavor共通引数」・4章）をここに置く。docstringから「詳細は本書の○節」と名指しされた節だけを残しており、節番号は参照元と対応するため変えない。
- 全ツールの一覧・引数表は `docs/spec/openapi.yaml`（`scripts/generate_openapi.py` が `mcp.list_tools()` から自動生成し、CIで乖離を検出する）と `docs/reference.md` を使う。
- ツール名・引数名・型名は外部APIとして直接参照されるためそのまま英語表記で残す。本文は常体（だ・である調）。
- CALM内部ID（D#/M#/A#/L#/T#）は本文では使わず、論理名（decision/material/activity/log/topic）で書く。

---

## 2. 各ツール詳細（docstringから参照される節のみ）

### 2.3 add_decisions

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| items | list[object] | yes | - | 最大10件。各要素は `{topic_id, decision, reason, title?, tags?, propagate_to?}` |

**items詳細**:
- `reason`: 決定の理由。任意で本文末尾に定型節（却下案:/適用条件:/適用外:/検証:/隣接確認:）を書ける。書式・各節の意味はdocs/precedent-format.mdが正本。節はすべて任意で、「該当なし」を埋める空項目・ダミー項目は書かないこと。
- `title`: 決定の要点を表す1行（35字以内）。check-in・timeline・search等の見出しに使われるため付与を推奨。省略時はdecision本文にfallback。タグに`layer:direction`を含む場合は必須（省略・空文字は当該itemが`errors`に`ITEM_ERROR`として格納され、decision自体は作成されない）。
- `tags`: 省略時はtopicのタグを継承。内容を表すタグを積極的に追加することが望ましい。namespace規約はdocs/architecture/invariants.mdの「タグnamespace」節を参照。
- `propagate_to`: `{type: "habit" | "tag_note", content: string, tag?: string}`。tagはtype="tag_note"のとき必須。type="tag_note"は教訓・注意点のみに使い、仕様・手順の全文転記には使わない。

**返り値**: `{created: [...], errors: [...], related_decisions: [...], propagation_failed?: [...]}`。
- `related_decisions`（応答トップレベル、呼び出し全体で同topic内の類似decision上位3件、各`{type, id, title, snippet}`。similarity降順、閾値未満・embeddingサーバー未起動・セッション内で提示済みのdecisionは含まれない）は既存decisionとの矛盾・重複に気づくための導線。
- タグに`layer:direction`を含む要素には`existing_direction_decisions`（同domainの有効な方向性decision全件、自身除外・非ランク）と`direction_note`（supersede/併存の判断を促す文言）も付く。
- `reason`に定型節があれば`precedent`（`{rejected_alternatives: 件数, scope: bool, verification_anchors: [文字列, ...], adjacent_check: [文字列, ...], warnings?: [文字列, ...]}`）をecho。書式ゆれ・空節・アンカー日付欠落等、または`intent:design`タグ付き要素で「隣接確認:」節が無い場合は`precedent_warnings`（文字列のリスト）も付く。いずれもsoft validationであり、decision作成自体は拒否しない。

**propagation_failed**: propagate_toの伝搬が1件以上失敗した場合のみ付く配列。各要素は `{index, decision_id, type, tag?, message}`。decision自体の作成成否には影響しない（decisionは常に成功として作成される）ため、この配列を見ないと伝搬失敗（例: tag_note伝搬先タグの文字数上限超過）に気づけない。
**関連**: `add_habit` / `update_tag(notes=...)` と連動。

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

### 2.18 check_in

| 名前 | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| activity_id | int | yes | - | アクティビティID |

**返り値**: 5つの枠（`anchor`/`control`/`context`/`catalog`/`env`）に分けて返す。中身が空の枠・キーは省く（`anchor.activity`・`control.goal`・`env.coverage`・`env.session`は常に置く）。

- `anchor`: `{activity, pinned}`
- `control`: `{goal, asks, neighbor_asks, recent_settled_asks, decision_candidates, unlearned_corrections, dependencies}`
- `context`: `{topics, activities, decisions, latest_log, materials}`
- `catalog`: `{logs, map}`
- `env`: `{tag_notes, hints, coverage, session, flow_guide}`。セッション内でcheck_inを初めて呼んだときのみ`flow_guide`（コンテキスト取得の手がかり）も含まれる

`control.goal`は、そのactivityのgoal機構上の現在状態と次の一手を1件返す（goal機構自体は `get_goal` 等のdocstring参照）。未定義（`label="undefined"`）・不要印（`label="not_needed"`）・goal付き（`label="active"|"judge_ready"|"closed"`）のいずれかで、goal付きなら`next`（今やるべきこと1件）を含む。組み立てで例外が出ても他のキーは失われず、`goal`キーに`{"error": {"code": "DATABASE_ERROR", ...}}`が入る。flavor指定時はremaining/terminal内の束縛先表示とopen_questionsのtitleだけが展開され、goalの文（statement・条件文・note等）は展開されない。
activity束縛の条件が1件以上あるgoalには`children`（内訳を1行にした文字列、例「子4: 達成1・進行中1・失敗未処理1・停止1」）が付く。手を打つべき子（失敗して未処理・止まっている）があれば`attention`（各`{condition_id_raw, title, mark, hint}`、markは`失敗`\|`止まっている`、最大3件）も付き、超過分は`attention_more`（`"他 N 件"`）に畳む。「止まっている」は、束縛先activityのgoalが未判定で、子を止めているopen askがある・heartbeatが`HEARTBEAT_TIMEOUT_MINUTES`（既定20分）を超えて途切れている・判定せずに完了している、のいずれかに当たること。`remaining`/`other_activities`/`terminal`はgoalブロックが目安の800字を超えると件数表示に畳まれるが、`children`・`attention`はこの畳み込みの対象外で、どれだけ子が多くても畳まれない。
`anchor.pinned.decisions`の各要素は、未resolveなdestabilizesエッジを持つ場合のみdestabilizationが付く。
このactivityを`add_ask`のblocksでblockしているaskが1件以上あるときのみ`control.asks: {awaiting_answer, awaiting_triage}`が追加される（無ければキー自体が無い）。`awaiting_answer`はstatus='open'のask一覧（各`{id_raw, question, last_seen_at}`）、`awaiting_triage`はstatus='answered'かつ未トリアージのask一覧（各`{id_raw, question, answer_body, last_seen_at}`）。activities.statusがcompleted以外のときのみ配達され、promoted/dismissed/withdrawn済みのaskは配達されない。合わせて新しい順に最大5件、超過分は`more`（件数）と`next`（`get_asks`へのポインタ）に畳む。`awaiting_triage`の存在自体が「triage_askで振り分けるべき」という状態情報であり、`env.hints`にはこの旨のテキストを重複させない。activityが紐づくdomain:タグのnotesが推奨文字数の上限を超えている場合、`env.hints`に整理を促す文言（`notes_over_budget`）が1件追加される。他のimmediate hintと異なり恒久抑制マーカーは効かず、超過が解消するまで発火し続ける（`demote_tag_notes`でnotesを資材へ退避して縮めることを想定した設計）。
`control.neighbor_asks`は、このactivityと隣の作業（goalの親子関係にある作業、`depends_on`でつながる作業。向きは問わない）を止めている、未決（open）または回答済み未トリアージのaskのうち、このactivity自身は止めていないもの。`{items: [{id_raw, question, status, activity}], more?, next?}`で、`activity`はどの作業のaskかを示す作業の題。回答本文は載せない。`control.recent_settled_asks`は、このactivityと隣の作業を止めていたaskのうちトリアージから7日以内のもの。`{items: [{id_raw, question, activity, outcome, detail}], more?, next?}`で、`outcome`は`promoted`|`dismissed`、`detail`はpromoteなら昇格先decisionの見出し、dismissなら却下理由。どちらも新しい順に最大3件、超過分は`more`（件数）と`next`（超過したaskを止めている作業ごとの`get_asks(blocking_activity_id=<その作業>, status=null)`へのポインタ。最大3件）に畳み、該当が無ければキー自体を省く。`control`は10,000字の予算に数えない枠なので、予算の切り詰め対象は変わらない。
`control.decision_candidates`は、記録役が決定事項の候補として退避したmaterial（素タグ`recorder-decision-candidate`）のうち、閉じていないものを新しい順に出す。対象は、このactivityに直接つながる候補と、このactivityの関連topicに属する候補。閉じているとは、retractされている、またはdecisionとの関係（`add_relation`）を持つこと。`{items: [{id_raw, title}], guide, more?}`で、`items`は最大3件（titleは60字で切る）、超過分は`more`（残りの件数）に畳む（ポインタは付けない。閉じると残りが次のcheck_inで出る）。`guide`は閉じ方の案内で、本文に明示的な承認があれば`add_decisions`で決定事項にして候補と`add_relation`で結ぶ（同じ決定事項が既にあればそれと結ぶ）、合意でなかったなら`retract`、曖昧ならユーザーに確かめる、という内容。該当が無ければキー自体を省く。`control`の3,000字の天井の中に収まるよう件数とtitleの長さを絞っている。
`control.unlearned_corrections`は、記録役が人の訂正として積んだmaterial（素タグ`unlearned-correction`）のうち、未解消のものを古い順に出す。対象はdecision_candidatesと同じく、このactivityに直接つながる件と関連topicに属する件。解消とは、届け先の記録（素タグ`lesson-delivery`。置き場と発火の契機を書く）と、届いたことの観測の記録（素タグ`lesson-observed`）の両方が、logかmaterialとしてその件と`add_relation`で結ばれていること（取り消された記録は数えない）。`{items: [{id_raw, title, delivered, observed}], guide, more?}`で、`items`は最大3件（titleは60字で切る）、超過分は`more`に畳む。`delivered`・`observed`はそれぞれの記録が既に結ばれているか。skill・rulesのように届いたことを観測できない置き場に書いた件は、届け先の記録に素タグ`lesson-unobservable`も付けると一覧から外れる（解消とは数えず、`scripts/corrections.py metrics`で「観測なし」として別に数える）。記録役が同じ型として既存の件と結んだ件のうち、同じ型の他の誤りを洗った結果（素タグ`same-type-checked`）がまだ結ばれていないものは`same_type_pending: [{id_raw}]`（最大3件）に出る。観測は書き手でない`scripts/corrections.py observe`が付ける（DBの複製に新しいセッションとしてcheck_inし教訓の文が全文で出るか、またはフィードバックエントリが届け先の記録より後に配達されたかを見る）。該当が無ければキー自体を省く。
`env.session`は呼び出し元のClaude Code CLIプロセスを解決できた場合`{"name": str, "alias": str, "alias_collision": bool}`、解決できない場合（非CLIクライアント、launcher登録が間に合っていない起動直後等）は`{"registered": false, "reason": "cli_unresolved"}`。このセッション別名レジストリ更新はベストエフォートであり、失敗してもcheck_in本体は成功応答を返す。`alias_collision`がtrueの場合にユーザーへ伝えるかどうかは呼び出し側（check-inスキル等）の責務であり、`env.hints`には重複させない。詳細は2.42bを参照。
応答全体が10,000字を超えるときは`truncated`キーが付く（`{budget, before, after, over_budget, cuts: [{section, kept, cut, next?}, ...]}`）。`section`はドット区切りの入れ子パス（例: `anchor.pinned`、`catalog.map`）。`catalog.map`/`catalog.logs`/`context.materials`/`context.activities`/`context.decisions`/`context.latest_log`/`anchor.pinned`の順に切り詰められる。`control`（goal/asks/dependencies）と`env.tag_notes`はこの10,000字には数えず、それぞれ3,000字・6,000字の天井を別に持つ（超過時は`truncated.control_over`/`tag_notes_over`が立つ）。
**副作用**: statusがin_progress以外なら自動的にin_progressに更新。
**呼び出し基準**: 既存アクティビティに関連する作業を始めるとき。

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

---

## 3. 共通エンティティ型

### 3.2 Decision
- `decision_id: int`
- `topic_id: int`
- `title: string`、`decision: string`、`reason: string`
- `tags: list[string]`
- `related_decisions: [{id, title, distance}]`（add_decisions返り値のみ）
- `retracted_at: string | null`
- `destabilization: {destabilized_by: [source_id, ...], unresolved_count: int, latest_source: source_id | null, sources: [{decision_id, title, created_at, kind_reason}, ...]}`
  （`get_decisions`/`get_by_ids`/`check_in`のanchor.pinned.decisions/`pull_precedents`の読み出し応答のみに付く算出フィールド。
  未resolveなdestabilizesエッジ（`add_relation(relation_type="destabilizes")`で登録、`resolve_destabilization`で解消）を
  1本以上持つ場合のみ付与され、無ければキー自体が無い。`destabilized_by`と`sources`は`created_at`昇順、
  `latest_source`は最新のsource decisionのid。`is_superseded`/`supersede_chain`（結論の置き換え）とは独立に併記され、両方成立しうる）

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

- 本書（Markdown、人間向け）の更新は手動である。機械可読版の `docs/spec/openapi.yaml` は `scripts/generate_openapi.py` が `mcp.list_tools()` から自動生成し、CIで乖離を検出する（`.github/workflows/test.yml` の `doc-gen-drift` ジョブ）。
