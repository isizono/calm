status: 詳細設計ドラフト（レビュー用）

要旨: sync-memory実行時に、当該セッションのtranscriptからClaudeがユーザーに投げた質問（AskUserQuestion）とユーザーからの訂正発話を機械抽出し、既存記録との類似照合を経て「既存記録で答えられたはずの質問だったか」をClaude自身が事後判定する。yesと判定した候補のみをprecedent_missとしてreport_signalに積む。抽出と照合はホットパスに一切載らず、候補ゼロならステップ全体をサイレントスキップする。「思い出す」ことは保証せず、「後追いで拾う」という状態計測に振り切ることで、決定論の射程内に留める。

## 1. 背景と目的

決定論は認知（Claudeが記録に気づく・読む・活かす）そのものには触れられない。しかし「後追いで検出する・数える・痩せさせる」までは機構で保証できる。この設計は、聞き返しという最も観測しやすい形の「既存記録の未活用」を、sync-memoryが同期処理を回し終わった直後に事後スキャンで拾い、precedent_miss台帳（signal_events）に積む機構である。

- 保証すること — セッション終了間際にsync-memoryが実行されたときに、そのセッションのtranscriptに含まれるAskUserQuestion呼び出しと訂正発話のうち除外辞書に該当しない候補の先頭N件（上限あり）について、既存記録との高類似ヒット上位を必ず判定にかける
- 保証しないこと — sync-memory未実行セッションでの検出、候補上限Nを超えた分の判定漏れ、および高類似ヒットのない候補の見落とし。sync-memory未実行セッションの検出漏れはゲート用途では保守側に倒れるため床として成立するが、precedent_missの母集団はsync-memoryを実行したセッションの見落としに偏っており、真の見落とし率そのものではなく偏った下限指標である点に留意する
- 「既存記録があれば聞き返しが不要だったか」という判定自体はClaudeの主観判断であり、機構は判定の一貫性・再現性を保証しない。機構が保証するのは候補を判定にかけるところまでであり、判定結果（yes/no）そのもの、およびその結果として積まれるprecedent_missの件数は保証の対象外である
- ここで積んだprecedent_missはops_metrics経由の集計・digest・第三者レビューSAの起点になり、記録の使われなさを状態として観測可能にする
- 本ステップの射程は「積む」までである。SessionStartでの直接露出（section宣言・優先度・予算配分）は設計しない。露出が必要になった場合は、SessionStart圧縮設計（別途進行中）側の課題として扱う

## 2. 確定済みの制約

以下は検討会で確定済みであり、本設計では動かせない。

- 3層運用の②③（check_in督促nudge・記録レスポンスへの関連記録添付）が本命の「気づかせる」経路であり、本ステップは①計測前倒しの後追い成分にあたる
- 環境側キーワード照合によるin-flight注入系は凍結（本設計の対象外）。凍結対象の再開時設計指針として「新規計測はinjection_telemetryという計測台帳に統合し、新規の常駐ストアは作らない」がある。この方針は③記録=クエリ添付の実効性計測（提示・取得ペアの機械記録）に紐づく既決であり、本設計のprecedent_miss計測は既存のreport_signal/signal_events経路（既存実装、新規ストアではない）を使うため、injection_telemetryへの統合対象外と判断する
- 抽出・照合はホットパスに載せず、sync-memoryのステップとして事後処理する
- 候補のうちClaudeが判定に回すのは上限N件（初期値は実装時決定）。抽出そのものと類似照合は機械処理で、Claudeのターン推論が要るのは判定フェーズのみ
- 抽出→照合→判定→report_signalという流れで、report_signal側のfingerprint dedupに集約を任せる。抽出側で独自のdedupストアを持たない
- sync-memoryの構成が再編される場合は本ステップも再編対象。実装前に、進行中の記録系skill強化バッチとの衝突確認を行う（現時点でmainにマージ済みの内容とは非衝突）

## 3. 設計（How）

### 3.1 sync-memory SKILL.mdへの追加位置

既存のStep構成（0〜10）に対し、以下の位置に新規Stepを追加する。ステップ番号は整数で振り直し、小数点付きの新設は行わない。

- 現行 Step 8「抜け漏れチェック」の直後、現行 Step 9「棚卸し・remember」の直前に「聞き返しの後追い検出」を挿入する
- これに伴い、現行 Step 9 → Step 10、現行 Step 10「完了報告」→ Step 11 にリナンバーする

配置根拠。

- 抜け漏れチェックまでに、当該セッションで生じたtopic/activity/decision/log/materialは記録が確定している。既存記録との照合対象に「今このセッションで足した記録」も含めた完全な状態でクエリできる
- 棚卸し・remember より前に置くことで、precedent_missとして残した観測をrememberの判定ネタとしても扱える（例: 訂正で得た教訓をhabits化する契機として使える）
- 完了報告より前に置くことで、報告のうち「聞き返し検出」節を条件付きで表示できる

### 3.2 追加ステップの記述文面（草案）

以下をSKILL.mdに追加する。ステップ番号は挿入位置に合わせる。

> ### 9. 聞き返しの後追い検出
>
> セッション中にClaudeがユーザーへ聞き返した質問と、ユーザーからの訂正発話を機械抽出し、既存記録との類似照合を経て「既存記録で答えられたはずだったか」を判定する。yesと判定した候補は `report_signal(kind="precedent_miss")` で計測台帳に積む。
>
> **前提スキップ:** 候補が0件のときはこのステップ全体をサイレントスキップする。ユーザーへ「該当なし」と報告する必要はない。
>
> **手順:**
>
> 1. Claudeが現在のセッションのtranscript pathを解決し、`scripts/detect_reask_candidates.py --transcript <path> --out <tmp.jsonl>` を実行する。出力は候補jsonl（各行に `kind` / `turn` / `text` / `context_snippet` / `excluded_reason?` を持つ）。transcript path解決手段は実装時に確定する（本ステップ末尾の未決事項参照）。解決できない場合はこのステップをスキップし、完了報告に一行残す
> 2. `excluded_reason` が付いた候補は対象外として除外する
> 3. 残候補のうち先頭N件（上限あり。Nを超える候補は判定対象外とし、超過した旨を完了報告に一行残す）について、`text` をクエリに `search(keyword=..., limit=10)` を呼び、既存記録top-3を得る（閾値は本ステップ末尾の実装ノート参照）
> 4. `search` レスポンスの `degraded=true` のときは判定を保守側に倒す（該当候補についてはprecedent_miss記録を行わない。skipした事実はStep 11の完了報告に一行残す）
> 5. `search` 上位のうち `score` が閾値以上のものを「高類似ヒット」と扱い、Claudeが「この既存記録があれば、この聞き返しはそもそも不要だったか」を判定する。この判定はClaudeの主観判断であり、同じ候補・同じ既存記録でも判定がぶれうる
> 6. yes判定のみ `report_signal(kind="precedent_miss", summary=..., detail=..., refs=[高類似ヒットの各id], context={"missed_ids": [...]})` を呼ぶ。summaryのフォーマットは§3.5参照（dedup fingerprintの安定性のため決定論的な文字列に固定する）
> 7. 判定ログ（候補text・ヒットid・判定結果）は本ステップ内では保存しない（signal_events側のcontextに集約される）
>
> **記録で答えられない性質の候補は除外する:**
>
> - Claudeの意見・選好を求める質問（「A案とB案どっちがいい?」等）
> - ユーザー自身の選好・状況を尋ねる質問（「今日どこまでやりたい?」等）
> - セッション外の環境事実（OS状態・CI状態・PR状態など、記録に載せる対象ではないもの）
>
> **実装ノート（初期値、実装時に確定）:**
>
> - 判定に回す候補上限N: 目安5〜10件
> - 「高類似ヒット」の閾値: search score 0.4以上
> - 除外辞書の初期セット: 上記3種＋Claude意見要求パターン

### 3.3 機械抽出スクリプトの仕様

新規ファイル: `scripts/detect_reask_candidates.py`。

CLI。

```
usage: uv run python scripts/detect_reask_candidates.py \
  --transcript <path>  # 必須。JSONL形式のClaude Code transcript
  [--out <path>]       # 省略時 stdout
  [--dict <path>]      # 訂正発話辞書 (JSON)。省略時は組み込み既定
  [--max <N>]          # 抽出上限。既定 50
```

入力: transcript path（JSONL、hooks/hook_transcript.py と同じ構造前提）。ファイル読み取りは行ストリーム（`for line in f`）で行い、巨大transcriptでも定数メモリで走査する。

turnの定義: transcript内エントリの通し番号（0始まり）。assistant/userの別を問わず1エントリごとにインクリメントする。

出力: 候補jsonl。1行1候補。

```json
{"kind": "ask", "turn": 12, "text": "sync-memoryのステップはどの位置に入れる？", "options": ["Step 8の直後", "Step 9の直後"], "context_snippet": "...直前のassistantテキスト末尾200字..."}
{"kind": "user_correction", "turn": 23, "text": "これ前に決めなかったっけ?", "context_snippet": "...当該user発話の全文..."}
{"kind": "ask", "turn": 8, "text": "A/Bどっちがいい？", "excluded_reason": "opinion_request"}
```

抽出対象。

- (a) AskUserQuestion呼び出し: transcript内の `assistant` エントリの `tool_use` ブロックで、`name` に `AskUserQuestion` を含むものを検出し、`input` から `question` と（あれば）`options` を取り出す
- (b) ユーザー訂正発話: `user` かつ `isMeta=false` かつ、contentがlist形式の場合は `tool_result` タイプのブロックを1つも含まないエントリのテキスト（`hook_transcript.is_user_message` と同判定ロジック）に対し、訂正辞書に定義されたパターンをマッチする

訂正辞書の初期値: `skills/audit/SKILL.md` の T-B1〜T-B3 の発話例（「これ前に決めなかったっけ?」「また同じ話してる」「またこの話か」「過去の情報・自分の知っている情報と矛盾してない?」「グルグル回ってない?」「ちゃんと過去の議論を踏まえてる?」）を初期セットとして流用する。辞書は `--dict` で差し替え可能とし、正規表現ベースで管理する。

除外判定。抽出時点で以下を `excluded_reason` として付ける（後段のClaude判定を軽くする）。

- `opinion_request`: 質問文が `どっち` / `どう思う` / `〜がいい` / `〜する?` 系の意見要求パターンに合致
- `user_preference_request`: 質問文が `どこまで` / `いつ` / `どれくらい` / `やりたい` 等のユーザー状況を尋ねるパターンに合致
- `environment_fact`: 質問文が `CI` / `PR` / `worktree` / `mac` 等、セッション外の環境状態を問うパターン

除外基準の細部は初期実装で保守側（除外を弱め）に倒し、実際のprecedent_miss記録の false positive を観察してから拡張する。

新規スクリプトとする理由。既存 `scripts/precedent_scan.py` はdecisionsテーブルのprecedent定型節（docs/precedent-format.md）の規約準拠状況を計測するread-onlyスクリプトであり、対象データ（decisionsテーブル）も処理内容（節パースと集計）も本スクリプトの対象（transcript JSONLからのAskUserQuestion呼び出し・訂正発話の抽出）と異なる。拡張ではなく新規スクリプトとするのが妥当。

DB非依存。スクリプトはsqlite/embeddingサーバー等の外部リソースに触らない。純粋にtranscriptを読んで候補jsonlを吐くだけ。理由: (i) sync-memory実行中のセッションはMCPサーバー経由で照合できる状態にあり、DB直アクセスを二重に持つと接続方針の分岐が増える、(ii) 日常的にセッション末に走るため、DB接続の副作用（WALリカバリ等）を避けたい。

### 3.4 照合フェーズの責務分担

「照合」はサーバー経由でClaudeが `search` ツールを呼ぶ。抽出スクリプトはDBに触らない。

理由。

- search はFTS5・ベクトル・タグLIKEを統合したRRFスコアを返す既存経路であり、scoreの意味・上限・degradedフラグの規約が確立している
- embeddingサーバー未起動時は `degraded=true` かつ FTS5結果のみで返る仕様が固まっているため、フォールバック判断を独自に組む必要がない
- スクリプト側でDBに直アクセスすると、readable-idやcitation flavorといった上位規約への追従が二重メンテになる

Claudeが呼ぶ具体形。

```
search(keyword=候補text, limit=10, flavor="internal")
```

候補のcontextから話題が絞れる場合は `tags` や `entity_type` を追加してもよい。ただし基本は素のkeyword呼び出しで十分（本ステップは網を張るのが目的で、絞り込みすぎるとprecedent_missの検出漏れが増える）。

### 3.5 判定フェーズと report_signal 呼び出し

Claudeが判定するのは以下の1点のみ。

> この既存記録が事前に思い出せていたら、この聞き返し（または訂正）は不要だったか？

yesのときのみ report_signal を呼ぶ。呼び出し形。

```
report_signal(
  kind="precedent_miss",
  summary="missed: <最上位ヒットの既存記録type>#<id>",
  detail="<候補textの1行要約>（判定: <既存記録タイトル>があれば聞き返し不要だった）",
  refs=[{"type": "<既存記録のtype>", "id": <既存記録のid>}, ...],
  context={"missed_ids": [{"type": ..., "id": ...}, ...], "trigger": "reask_detection", "turn": <候補のturn>},
)
```

- `context.missed_ids` の形式は既存 report_signal ガイド（refsと同構造でtype+idの配列）に準じる
- `context.trigger` に `reask_detection` を入れることで、後続の集計で本ステップ由来のprecedent_missを識別可能にする
- summaryはfingerprint計算（`sha256(kind|source|正規化summary)`、正規化は前後空白除去・連続空白畳み込み・小文字化のみで意味的な揺れは吸収しない）にそのまま使われる。Claudeが自由記述する要約文言をsummaryに含めると、同じ既存記録を別セッションで踏み直したときに文言が実行ごとに揺れ、fingerprintが一致せずdedupが発火しない
- そのためsummaryは決定論的な文字列（`missed: <既存記録のtype>#<id>`）に固定する。高類似ヒットが複数件ある場合はrefsのうち最もscoreが高いものを代表として使う。自由記述の要約はdetailフィールドに逃がす
- dedupは signal_events 側の fingerprint (kind|source|正規化summary の sha256 先頭16hex) に任せる。本ステップは独自の去重を行わないが、fingerprintが安定するようsummaryのフォーマットを決定論的に保つ責務を負う

### 3.6 軽量性の担保

- 抽出はO(transcriptサイズ)の行ストリーム。ホットパスに載らない。行あたりのJSONパースコストは既存の `hook_transcript.py` と同オーダー
- 照合の `search` 呼び出し回数は 候補判定上限N（初期値5〜10）で固定される
- 候補ゼロ時はスクリプトが空jsonlを返すだけで、Claudeのターン内でsearchも判定も走らない（Step 9全体スキップ）
- 判定にターン推論を使うのはyes/noの2値であり、textの単位も短い（質問文＋既存記録top-3のタイトル）。既存 Step 9b（記憶すべき知見の判定）と同オーダーの推論負荷

Claudeが消費する追加トークンの見積もりの立て方。

- 候補判定上限をN、search結果top-3のスニペットを各200文字上限とすると、判定1件あたりの入力は候補text（〜200字）＋既存記録top-3×(タイトル+スニペット, 各250字)＝〜1000字/件
- Nを5〜10に置くと 5000〜10000字/セッション。これは既存 Step 9b の記憶判定と同オーダー
- 実装後、ops_metricsで「本ステップ由来のsearch呼び出し回数」を計測可能にすると軽量性の検証が事後にできる（将来拡張）

### 3.7 完了報告（Step 11）への追記

リナンバー後のStep 11「完了報告」フォーマット末尾に以下を条件付きで追加する。

```
### 聞き返し検出
- (precedent_miss として記録した件数と、各候補の要約)
- (degraded で判定を保守側に倒した件数)
- ※ 誤検出は report_signal 側で dismiss できます
```

該当なしのときはセクション自体を省略する（既存の「該当なしのセクションは省略する」規約に沿う）。

## 4. 変更ファイル一覧

- `skills/sync-memory/SKILL.md` — Step 9として「聞き返しの後追い検出」節を追加。現行 Step 9「棚卸し・remember」→ Step 10、現行 Step 10「完了報告」→ Step 11 にリナンバー。リナンバー後のStep 11「完了報告」フォーマット末尾に、§3.7で示す聞き返し検出セクションを追記
- `scripts/detect_reask_candidates.py`（新規） — transcript JSONL を読み、AskUserQuestion呼び出しと訂正発話を抽出して候補jsonlを吐くCLIスクリプト。DB非依存
- `scripts/detect_reask_candidates_dict.json`（新規、任意） — 訂正発話パターンと除外パターンの初期辞書。CLIの `--dict` 既定値。組み込み既定を上書きしたい場合の差し替え点として用意（初期実装ではPython内に組み込みで持ち、外部ファイル化はfollow-upでも可）
- `tests/unit/test_detect_reask_candidates.py`（新規） — fixture transcriptによるスクリプト単体テスト。既存 `tests/unit/test_precedent_scan.py` 等と同様、`tests/unit/` 直下にフラット配置する（`tests/unit/scripts/` サブディレクトリは存在しない）
- `docs/spec/mcp-tools.md`（既存、任意） — report_signal の precedent_miss 節に `trigger: "reask_detection"` の慣習を追記（既存 context 規約キーの補足として）。docsを触るかは実装時判断

以上、SKILL.md と 新規スクリプト＋テストの3ファイルが最小変更セット。

## 5. Edge cases

- transcript が巨大（数十MB規模） — 抽出スクリプトは行ストリームで処理する。全行メモリ展開はしない。抽出上限（`--max`）で候補件数自体も抑える
- 1セッションで複数activityにまたがる — 抽出には activity 単位の区切りは持ち込まない。候補ごとに `turn` を持たせ、周辺assistant文脈を `context_snippet` に載せることで、Claude判定時に文脈が復元できる
- 質問がそもそも記録で答えられない性質（意見・選好・環境事実） — §3.3 の除外辞書で `excluded_reason` を付与し、判定対象外にする。除外辞書は保守側に倒し、迷ったら候補として残す（false positive は report_signal 側で dismiss できる）
- 既に report_signal に積まれている候補の再出現 — signal_events 側のfingerprintで自動集約（`_compute_fingerprint` は kind+source+正規化summary の sha256 先頭16hex）。同一 summary なら status='new' 行の occurrence_count が +1 されるだけで新規行は増えない
- embeddingサーバー未起動 — `search` レスポンスの `degraded=true` を見て、当該候補は判定スキップ。skipした事実は完了報告に件数のみ残す（precedent_miss 記録は行わない）。理由: FTS5のみの照合で意味的に強い既存記録を取り逃すと、precedent_miss の誤検出（実際は既存記録がある候補を「見つからなかった」と誤判定）が発生する
- AskUserQuestion 以外の聞き返し（テキスト末尾の疑問文だけで質問しているケース） — 初期実装ではAskUserQuestionツール呼び出しに限定する。テキスト末尾の疑問文検出はfalse positive が多く、除外辞書の整備が追いつくまで対象外とする
- ユーザー訂正発話の false positive（「またこの話か」を単なる相槌で使うケース） — 初期辞書は audit の T-B 系語彙のみに絞り、辞書拡張は observed data を見て段階的に行う
- sync-memory 実行を跨がないままセッションが終わった — 検出対象外。この漏れは3層運用の②check_in督促nudgeと③記録レスポンス添付でカバーされる想定であり、本ステップの守備範囲外
- 同一セッション内での同じ候補が複数回抽出される — スクリプト側で `(kind, text)` の同値排除は行う。textは正規化（連続空白畳み込み・小文字化）してから比較。同一文言が別文脈で意図的に再度発生するケース（過剰dedup）と、表記ゆれで別候補扱いになるケース（過小dedup）はどちらも起こりうる。正規化ルールの精緻化は初期実装の対象外とし、observed dataを見て調整する

## 6. Verification

この設計が保証する振る舞い。

- **B1 抽出の正確性:** AskUserQuestion呼び出しと訂正発話が漏れなく候補jsonlに現れる
- **B2 除外理由の付与:** 意見要求・選好要求・環境事実の3系統について、除外辞書のパターンにヒットした候補には `excluded_reason` が付与され、判定対象外になる。除外辞書のカバレッジ自体（意見要求を実際に意見要求として正しく分類できているか）は機構で保証されない。辞書の精度はobserved dataを見て評価する非機構事項
- **B3 スキップの正しさ:** 候補ゼロの transcript を渡した場合、Step 9 全体がサイレントスキップされ、signal_events に何も積まれない
- **B4 集約の正しさ:** 同一 summary を2回report_signalしたとき、signal_events は新規行を作らず既存 status='new' 行の occurrence_count が +1 される
- **B5 保守側フォールバック:** embeddingサーバー未起動時、`search` の `degraded=true` を検出した候補はprecedent_missとして記録しない

確かめ方（テスト観点）。

- スクリプト単体（tests/unit/test_detect_reask_candidates.py）
  - fixture 1: AskUserQuestion 3件のみを含むtranscript → 候補3件、いずれも `kind="ask"`
  - fixture 2: 訂正発話3件のみを含むtranscript → 候補3件、いずれも `kind="user_correction"`
  - fixture 3: 意見要求質問1件＋選好要求質問1件＋通常質問1件 → 前2件に `excluded_reason` 付与
  - fixture 4: 空transcript → 出力空
  - fixture 5: 巨大transcript（10MB程度、行数多） → 定数メモリで完了する
- フルフロー（手動または integration test）
  - 実transcriptを用意し、SKILL.mdの手順通りにClaudeが実行 → signal_events に precedent_miss 行が意図通り作られる（refsに既存記録idが入っている）
  - 同transcriptで再度sync-memoryを回す → signal_events は新規行を作らず occurrence_count が +1 される
- 副作用の非存在
  - ops_metrics.py の集計値（本ステップ導入前後）で、precedent_miss 以外の kind の件数が変わらないこと

## 7. 依存関係と実装順序

先行して確認・整合させるべき既存作業。

- 記録系skill強化バッチ（mainマージ済み: SKILL.md の Step 3 / Step 4 に material と log の棲み分けが明記された内容） — 本設計はStep 8以降にステップを挿入する形なので、記録系ステップの中身には触らず非衝突
- SessionStart予算化ブランチ（未マージ） — check_in周辺の改修であり、sync-memory側の本ステップとは非衝突
- check_in内部リライト（別課題で進行中） — `skills/check-in/SKILL.md` を確認した結果、sync-memoryのStep番号（Step 9・Step 10等）を参照する記述は存在しない。check_in内部リライトはcheck_inのcurationロジック内部（`check_in`ツールが返すデータ構造）の変更であり、SKILL.mdのステップ番号非依存。よって非衝突と判断できる
- sync-memory軽量化議論（[議論中]、結論未定） — sync-memoryのStep構成自体を軽くするかどうかは未決であり、本設計はその結論を先取りしない。本ステップが「候補ゼロ時サイレントスキップ」構造を持つことは、軽量化の結論が「縮小」「現状維持」のいずれであっても本ステップ自体の実装方針を変える必要がないという意味での独立性であり、軽量化議論の結論を待たずに実装してよいという意味ではない。単独スクリプト＋SKILL.md1節の構成のため、軽量化の結論が出た時点で移設・削除いずれにも対応しやすい

実装順序。

1. `scripts/detect_reask_candidates.py` と単体テストを先に足し、抽出の正確性・除外の妥当性を独立に検証可能な状態にする
2. `skills/sync-memory/SKILL.md` に新Step節と完了報告への追記を入れる。既存Stepのリナンバーもこの変更で完了させる
3. mainマージ後、precedent_miss行がops_metrics集計に現れるようになった時点で、その集計値を確認できる機会（digest実行時・第三者レビューSA起点・ops_metrics参照時など）に、precedent_miss行の増え方とfalse positive率を観察する
4. 3の観察を行ったセッションが、observed dataに基づき除外辞書と判定閾値の初期値を実装ノートから確定値に差し替える

3層運用の他要素との時系列。

- 本ステップ（①計測前倒しの後追い成分）はsync-memoryの範囲で単独実装可能。②check_in督促nudge、③記録レスポンス添付の実装マイルストーンとは独立に着地できる
- ただし③記録レスポンス添付（M4予定）は「add_logs/add_material呼び出し時に関連既存記録top3をmanifest形式で添付」する機構であり、本ステップの§3.4照合フェーズ（候補textをkeywordにsearchを呼び上位3件を得る）と同じsearch top3ヒット構造を独立に組むことになる。③着手時に、本ステップの照合ロジックとの統合可否をその時点の一次情報で確認する工程を設ける。統合の要否・方式は現時点では判断せず先取りしない

## 8. 未決事項

実装時にコードで確定する。

- 判定に回す候補上限N（本ドキュメントでは目安5〜10と記載） — false positive率と検出漏れのバランスで決定
- 「高類似ヒット」と扱う `search` score の閾値（本ドキュメントでは目安0.4と記載） — search レスポンスの実測分布を見て確定
- 除外辞書の初期セット（意見要求・選好要求・環境事実の3系統について具体的なパターンリスト） — 初期は保守側（除外を弱め）に倒す
- 訂正発話辞書の拡張タイミング — 初期はaudit T-B系語彙に限定。observed dataを見て段階的に拡張
- `context.trigger` キー名（本ドキュメントでは `reask_detection`） — 既存 report_signal ガイドの context キー命名慣習に合わせて実装時に微調整可能
- `--dict` の外部ファイル化を初期版で行うかどうか — 組み込み既定＋fixtureテストで十分なら外部ファイルは follow-up
- 完了報告に含める件数の粒度（記録件数のみか、候補text要約も出すか） — ノイズにならない粒度を実運用で確定
- transcript pathの解決手段 — `$CLAUDE_TRANSCRIPT_PATH` のような環境変数がSKILL.md実行時のBashツール実行環境で利用可能かは未確認。利用不可の場合の代替手段（hookから得られる情報を経由する等）を実装時に確定する
