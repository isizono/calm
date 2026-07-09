status: 詳細設計ドラフト（レビュー用）

要旨:
記録系ツール（add_logs / add_decisions / add_material）が返す関連既存記録 top3（3層運用の記録=クエリ添付。以下「添付」）について、「提示された記録が同セッションで実際に読まれたか」を機械記録する追随カウンタを設計する。新規台帳 `injection_telemetry` を追加し、添付が返された瞬間に present 行を1件ずつ書き込む。取得側は既存の `search_telemetry.results_json` と `fetch_telemetry.items_json` を再利用し、`get_material` のみ現状 fetch_telemetry に載っていないため追加で計装する。追随率は post-hoc の SQL 集計で算出し、専用ツールは持たない。書き込み経路は既存 telemetry と同じ非同期 daemon thread + 失敗握りつぶし規約に従い、応答レイテンシと成功確率を損なわない。

---

# 1. 背景と目的

3層運用の第3層（記録=クエリ添付）は、記録系ツール（add_logs / add_decisions / add_material）のレスポンスに「関連する既存記録 top3」を付ける。既存 add_decisions は topic 内の類似 decision top3 を `related_decisions` として既に返している（`src/services/decision_service.py:228-245`）が、これは「返した後で実際に読まれているか」を計測していない。add_logs / add_material 側にはそもそも top3 添付が未実装で、後続マイルストーンで載る。

「読まれた」を機構で保証することは決定論の対象外だが、「同セッションで get_by_ids / get_material / search のいずれかで実際に引かれたか」までは機械計測できる。この計測を追随カウンタと呼び、次の目的で使う:

- 添付機能の実効性計測（閾値・件数・形式見直しの根拠）
- 凍結された環境側キーワード照合による in-flight 注入系を将来解禁する際の実害データ
- 関連記録の relation 固定（別設計）における「よく一緒に読まれるペア」の候補選定材料

## 1.1 計測の限界

本カウンタの対象は「記録系ツールのレスポンスに添付される関連既存記録 top3」に限定される。SessionStart hook 等が行う宣言的な提示（pin・進行中 activity 一覧・tag notes 等）は対象に含まない。これらは非 MCP コンテキストで実行され `_current_session_id()` による相関が取れないこと、および「クエリに対する上位3件」という第3層添付特有の性質を持たないことが理由である。この限定により、本カウンタが示す追随率は cc-memory 全体の情報提示の実効性ではなく、第3層添付という一経路の実効性である。

`fetched_at > presented_at` という判定は相関であり因果を証明しない。添付とは無関係な文脈で同一 id が偶然 search/fetch された場合も追随として計上されうる。判定はターン数や時間窓で絞らない方針のため、この偽陽性の混入はカウンタの構造的な性質として残る。in-flight 注入系の解禁判断は、本カウンタの数値単独ではなく precedent_miss 等の他の signal データと合わせて行う前提であり、本カウンタの数値のみを解禁の直接根拠にしない。

# 2. 確定済みの制約

設計で動かせない点:

- 3層運用の第3層（記録=クエリ添付）は後続マイルストーンで実装される。追随カウンタは第3層と同一 PR で入る。カウンタ単独で先行実装しない（数える対象が第3層の提示イベントそのもののため）
- 新規常駐ストアは作らない。新規計測はすべて `injection_telemetry` に統合する
- 環境側キーワード照合による in-flight 注入系は凍結。追随カウンタも、環境側で prompt を書き換える方向には拡張しない
- 予算関連の再計算・budget_service の拡張は本設計の管轄外
- SessionStart 予算化ブランチと check_in 内部リライトは本設計と独立に進む。追随カウンタは check_in の内部リライト成果物に依存しない
- 「取得された」の判定は同セッション内に限定する。ターン数の窓では絞らない

# 3. 設計

## 3.1 データフロー概要

計測対象は 2 種類のイベントに分解される:

- present: 添付を返した瞬間。誰が（どの新規エンティティが）誰を（どの既存エンティティを）どのツール応答に載せたか、を記録する
- fetch: 同セッション内で提示済みの記録が引かれた瞬間。既存 telemetry（search_telemetry.results_json / fetch_telemetry.items_json）で既に大部分カバーされているため、追加計装は最小限に留める

present と fetch は追随判定のためにセッション ID で JOIN される。JOIN は書き込み時点では行わず、後続の SQL 集計に一元化する（write path を単純に保つため、および分析要件が変わっても書き直しなしで済むため）。

## 3.2 具体例

「タグ運用の統一についてどう思う」というユーザー入力に対して、次のフローを想定する:

1. エージェントが議論に至り、既存の decision (仮に id 3200)を根拠に新規 decision を作成する
2. add_decisions で新規 decision 3210 を書く。第3層添付として、同 topic 内類似 decision 3195 と 3182 の 2 件が返る
3. サーバーは即座に `injection_telemetry` に 2 行書く: (session=S, source=decision:3210, attached=decision:3195, rank=1), (…, attached=decision:3182, rank=2)
4. エージェントは添付を見て「3195 の方針が今回のケースと合いそう」と判断し、`get_by_ids([{type:"decision", id:3195}])` を呼ぶ
5. 既存 `fetch_telemetry` に 1 行入る: (session=S, tool="get_by_ids", items=[{"type":"decision","id":3195}])
6. 後日の分析クエリが present と fetch を session_id で JOIN し、「S における 3195 は presented_at 後に body 経路で fetch された」= 追随ありと判定する。3182 は presented のみで fetch なし = 追随なし

同じシーケンスで search 経由の場合、`search_telemetry.results_json` の要素の中に (decision, 3195) が出現していれば追随ありと判定する（本文取得と区別された種別で計上する）。

## 3.3 スキーマ変更

migration 番号は実装着手時点の連番の次値を採番する（本設計の起草時点での最大は `0057_drop_capability_gating.sql` だが、他の変更が先に入れば繰り上がるため、番号自体の確定は本設計の管轄外とする）。ファイル名は既存命名規約に合わせて `NNNN_add_injection_telemetry.sql` とする（以下、本文中では `NNNN` と表記する）。

新規テーブル定義:

```sql
CREATE TABLE injection_telemetry (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    caller_session_id  TEXT,
    trigger_tool       TEXT NOT NULL,        -- 'add_logs' | 'add_decisions' | 'add_material'
    source_type        TEXT NOT NULL,        -- 'decision' | 'log' | 'material'
    source_id          INTEGER NOT NULL,     -- 新規作成された側のID
    attached_type      TEXT NOT NULL,        -- 'topic'|'activity'|'material'|'decision'|'log'
    attached_id        INTEGER NOT NULL,
    rank               INTEGER NOT NULL,     -- 1〜3
    similarity         REAL,                 -- distance/score等（NULL可）
    diagnostics_json   TEXT,                 -- 将来の retriever 内訳等（NULL可）
    timestamp          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    hint_shown_at      TIMESTAMP              -- relation-ratchet.md が追加提案する制御列。NULL可。本設計の write path / analysis SQL では参照しない
);

CREATE INDEX idx_injection_telemetry_session_ts
    ON injection_telemetry(caller_session_id, timestamp);

CREATE INDEX idx_injection_telemetry_attached
    ON injection_telemetry(attached_type, attached_id);
```

`hint_shown_at` について: この列は本設計（追随カウンタ）自身の計測結果ではなく、`docs/design/relation-ratchet.md` が「取得まで至った候補にヒントを一度だけ添える」ための制御状態として利用する列である。新規常駐ストアを作らない方針の範囲で、relation-ratchet 側が本テーブルに間借りする形の追加を提案しており、本設計側はこれを受け入れる（テーブルの所有権は本設計にあるため、CREATE TABLE 定義への同梱も本設計の migration の管轄とする）。ただし本設計の `_record_injection_telemetry_async`・`_TELEMETRY_WRITABLE_COLUMNS` allowlist（3.4節）・3.7節の効果測定 SQL のいずれもこの列を書かず・読まない。書込は relation-ratchet.md 側が get_by_ids / get_material の同期経路で行う UPDATE であり、本設計の非同期 present 書込とは別のコードパスである。両設計の実装順序が前後する場合の migration の割り当て方は relation-ratchet.md 7節を参照。

意図と設計判断:

- `caller_session_id` は NULL 許容。既存 fetch_telemetry / search_telemetry と規約を揃える（`migrations/0054_extend_search_telemetry.sql:41`）。MCP コンテキスト外の直接呼出（テスト等）は NULL で残り、集計側で除外される
- FK は張らない。既存 telemetry テーブル群と同じ方針（生データ台帳としての性質を優先）
- UNIQUE 制約は張らない。同一 session で同じ (attached_type, attached_id) が複数回提示されることは正常挙動（複数の記録操作で同じ関連が推薦される）。集計側で GROUP BY MIN(timestamp) して縮約する
- `trigger_tool` を持つのは、後日「add_decisions 由来の添付だけ追随率が高い」のような ツール別分析を行うため
- `similarity` は 0〜1 の範囲に正規化し、大きい値ほど類似度が高いことを意味する値として値域・方向を統一する。decision の `related_decisions[i].distance` のような「小さいほど類似」の指標は、書込前に `1 - normalized_distance` 等で変換してから格納する。add_logs / add_material 側で別種の類似度指標を使う場合も同じ値域・方向へ変換する。値域・方向を呼び出し元ごとに委ねると、蓄積後に trigger_tool 別の類似度分布を比較する分析が遡って正規化できなくなるため、書き手側の責務として最初から固定する。具体的な変換式（decision 以外の指標をどう 0〜1 へ落とすか）は未決事項とする
- 既存 telemetry の書込方針（daemon thread、失敗握りつぶし、軽量コネクション経由）に載せる。これにより present 書込が呼出元 add_* のレスポンスタイムと成功確率に影響しない

## 3.4 Present 書込 API

`src/services/search_service.py` の telemetry helper 群と同居させる。既存 helper（`_record_search_telemetry_async`, `_record_fetch_telemetry_async`）と同型で、以下を追加する:

```python
def _record_injection_telemetry_async(
    trigger_tool: str,
    source_type: str,
    source_id: int,
    attachments: list[dict],
    caller_session_id: Optional[str] = None,
) -> list[threading.Thread]:
    """記録系ツールの top3 添付を injection_telemetry へ非同期書込する。

    attachments: [{"type": str, "id": int, "rank": int,
                   "similarity": Optional[float],
                   "diagnostics": Optional[dict]}, ...]
    """
```

writable columns allowlist（`_TELEMETRY_WRITABLE_COLUMNS`）に `injection_telemetry` を追加する:

```python
"injection_telemetry": frozenset({
    "caller_session_id", "trigger_tool",
    "source_type", "source_id",
    "attached_type", "attached_id",
    "rank", "similarity", "diagnostics_json",
}),
```

呼び出しタイミングは `add_logs` / `add_decisions` / `add_material` それぞれのラッパー層。第3層添付が完成した時点で `created` 各要素に対して attachments を組み立て、この helper を呼ぶ。

add_decisions が現状持つ `related_decisions`（topic 内類似 decision top3 を distance 付きで返す）は、第3層添付の契約（manifest 形式・予算600字・閾値未満は無添付・セッション内同一記録1回）を満たしていない。第3層添付の実装は `related_decisions` の単純な流用では済まず、これらの契約を満たす形に作り直す必要がある。この作り直し自体は第3層添付の詳細設計（本設計の管轄外、7章参照）の対象であり、本設計は「作り直された添付リストを受け取って `injection_telemetry` に書く」helper のみを提供する。

セッション内同一記録1回の契約は、`injection_telemetry` 側の重複排除ではなく、第3層添付の選定ロジック（add_* 側）が担う。具体的には、添付候補を確定する前に当該セッションの既提示集合（`caller_session_id` で絞った `attached_type, attached_id` の集合）を `injection_telemetry` から同期的に問い合わせ、候補から除外してから top3 を確定する想定になる。この同期照会は記録系ツールのレスポンスタイムに乗る（present 自体の書込は非同期のままだが、除外判定の読み取りは同期）。新規常駐ストアを作らない制約とは矛盾しない（`injection_telemetry` 自体への SELECT であり、別ストアの追加ではない）。除外判定をどの粒度（トピック単位／セッション全体／trigger_tool 別）で行うかは第3層添付の詳細設計側の判断とする。

予算600字での切り詰めは、実際にレスポンスへ返却された添付のみを `attachments` として渡す（trim 後の集合）。予算超過で返却されなかった候補は「提示されていない」ため present 行を書かない。この線引きにより `presented` は常に「クライアントが実際に受け取った添付」と一致する。

書込中に例外が上がった場合の扱いは既存 telemetry と同じ: logger.warning に出して呼出元のレスポンスを壊さない。呼出元は返り値の Thread リストを await せず、そのまま応答を返す。

## 3.5 Fetch 側の追加計装

現状の把握（scout 実機報告 + コード確認）:

- get_by_ids: `fetch_telemetry` に記録済み（`src/services/search_service.py:2287`）
- search: `search_telemetry.results_json` に返却ページを記録済み（`src/services/search_service.py:1865`）
- get_material: telemetry 記録なし。追随判定に載らない

get_material の追加計装は `src/main.py` の `get_material` ラッパー内で行う。material 単体取得を fetch_telemetry の 1 行として書く:

```python
# main.py:1074 付近、material_service.get_material 呼び出しの前後で
result = material_service.get_material(material_id, include_retracted=include_retracted)
if "error" not in result:
    _record_fetch_telemetry_async(
        "get_material",
        [{"type": "material", "id": material_id}],
        caller_session_id=_current_session_id(),
    )
```

`_record_fetch_telemetry_async` は既存関数のためシグネチャ変更不要。tool 名を `"get_material"` として書くことで、集計側で `get_by_ids` と区別できる。

get_decisions / get_logs は topic 起点集約の性格が第3層添付の意図（単発の関連参照）と異なるため、初期スコープでは fetch 扱いに含めない。この線引きは未決事項として明記する。

## 3.6 セッション識別

第3層添付とその追随計測は同じセッション内で完結する短時間の観測なので、cc-memory サーバー再起動をまたぐ安定性は不要。ephemeral な `_current_session_id()`（`src/main.py:289` の `get_context().session_id`）を使う。この選択の理由:

- 既存 search_telemetry.caller_session_id と fetch_telemetry.caller_session_id はいずれも ephemeral（同上経路）。追随判定の JOIN 相手が ephemeral 側なので、present 側も ephemeral で揃わないと JOIN 不能
- launcher 側の bridge identity（`src/services/relay/identity.py:65`）は relay 系ツールの永続宛先解決用で、目的が違う
- SessionStart hook 等の非 MCP コンテキスト経路は追随カウンタの計装対象外（記録系ツールを MCP 外から呼ぶユースケースは想定しない）

既知の限界（subagent 経由の計測漏れ）: cc-memory の運用では、親セッションで add_decisions を呼び、その添付を見た別プロセスの subagent（builder/scout 等）が get_by_ids で本文取得する、というワークフローが日常的に発生する。subagent は親セッションとは別の MCP 接続を張るため `_current_session_id()`（ephemeral）は親と一致せず、この経路の追随は現状の JOIN では検出できない（偽陰性になる）。bridge identity は launcher プロセスの祖先 pid チェーンで解決される識別子であり、subagent が launcher 経由で起動されれば親セッションと共有されうるため理論上はこの穴を塞げる可能性があるが、JOIN 相手の search_telemetry / fetch_telemetry の caller_session_id が ephemeral のままである限り、present 側だけ bridge identity に切り替えても JOIN は成立しない。両テーブル群の識別子方式を統一する改修は既存 telemetry 全体に影響するため本設計の管轄外とする。したがって本カウンタが計測する「追随」は「同一 MCP 接続内での追随」に限定され、subagent 経由の追随は構造的に計上されない。追随率はこの意味で実際の追随行動の下限値として読む。

識別が取れないケース:

- MCP コンテキスト外の直接呼出（ユニットテスト等）: `_current_session_id()` が None を返す → NULL で記録。集計側で `caller_session_id IS NOT NULL` フィルタで除外
- リモート接続で HTTP コンテキストが取れない構成: 実質同じ扱い（None → NULL）
- 書込自体をスキップする選択肢もあるが、書き手ロジックが単純になる方を優先し「NULL で残す」を採る。集計側の除外条件を統一できる副次効果もある

## 3.7 追随率の算出（分析用 SQL）

専用ツールは作らない。分析は SQL を直接叩く。以下は追随率の代表的な集計クエリ（analysis テンプレート）。

```sql
WITH presented AS (
    SELECT
        caller_session_id,
        attached_type,
        attached_id,
        trigger_tool,
        MIN(timestamp) AS presented_at
    FROM injection_telemetry
    WHERE caller_session_id IS NOT NULL
      AND timestamp >= :since
    GROUP BY caller_session_id, attached_type, attached_id, trigger_tool
),
body_fetched AS (
    SELECT
        ft.caller_session_id,
        json_extract(item.value, '$.type') AS type,
        CAST(json_extract(item.value, '$.id') AS INTEGER) AS id,
        MIN(ft.timestamp) AS fetched_at,
        ft.tool AS fetch_tool
    FROM fetch_telemetry ft, json_each(ft.items_json) item
    WHERE ft.caller_session_id IS NOT NULL
      AND ft.timestamp >= :since
    GROUP BY ft.caller_session_id, type, id, fetch_tool
),
search_hit AS (
    SELECT
        st.caller_session_id,
        json_extract(res.value, '$.type') AS type,
        CAST(json_extract(res.value, '$.id') AS INTEGER) AS id,
        MIN(st.timestamp) AS hit_at
    FROM search_telemetry st, json_each(st.results_json) res
    WHERE st.caller_session_id IS NOT NULL
      AND st.results_json IS NOT NULL
      AND st.timestamp >= :since
    GROUP BY st.caller_session_id, type, id
)
SELECT
    p.trigger_tool,
    COUNT(*) AS presented_total,
    SUM(CASE
            WHEN bf.fetched_at IS NOT NULL AND bf.fetched_at > p.presented_at
            THEN 1 ELSE 0 END) AS body_followthrough,
    SUM(CASE
            WHEN sh.hit_at IS NOT NULL AND sh.hit_at > p.presented_at
            THEN 1 ELSE 0 END) AS search_followthrough,
    SUM(CASE
            WHEN (bf.fetched_at IS NOT NULL AND bf.fetched_at > p.presented_at)
              OR (sh.hit_at   IS NOT NULL AND sh.hit_at   > p.presented_at)
            THEN 1 ELSE 0 END) AS any_followthrough
FROM presented p
LEFT JOIN body_fetched bf
    ON bf.caller_session_id = p.caller_session_id
   AND bf.type = p.attached_type
   AND bf.id   = p.attached_id
LEFT JOIN search_hit sh
    ON sh.caller_session_id = p.caller_session_id
   AND sh.type = p.attached_type
   AND sh.id   = p.attached_id
GROUP BY p.trigger_tool;
```

意図:

- `presented_at` は「同セッション内で最初に提示された時刻」。以降の fetch/search hit のみを追随として計上する（順序逆転を明示的に除外）
- body_fetched と search_hit を分けて計上することで、「本文取得された」と「search 結果一覧に出現した」を分析側で区別できる。決定事項が「3ツール列挙、両方数える、種別を残す」なので、この形で保存する
- `:since` は analysis 側の任意の起点。初期の運用としては直近30日を提案する
- 個別ツール別（add_logs / add_decisions / add_material）の追随率を返せる

# 4. 変更ファイル一覧

- migrations/NNNN_add_injection_telemetry.sql: 新規テーブルとインデックス（`hint_shown_at` 列を含む。同列は relation-ratchet.md が定義・利用する制御列で、本設計の write path / analysis SQL では使用しない。relation-ratchet.md の実装が本設計より後にずれ込む場合は、当該列とそのための追加 INDEX を relation-ratchet.md 側の ALTER TABLE migration に切り出してよい。詳細は同設計7節）
- src/services/search_service.py: `_TELEMETRY_WRITABLE_COLUMNS` に `injection_telemetry` 追加、`_record_injection_telemetry_async` 追加
- src/main.py: `get_material` ラッパー内で `_record_fetch_telemetry_async("get_material", ...)` 呼び出し追加
- src/main.py: `add_logs` / `add_decisions` / `add_material` の第3層添付組立直後に `_record_injection_telemetry_async` 呼び出し追加（第3層実装 PR で同時挿入）
- tests/unit/services/test_injection_telemetry.py: 新規（present 書込・NULL session 挙動・writable columns バリデーション）
- tests/integration/test_follow_through_counter.py: 新規（present + fetch/search の JOIN 集計が期待通り動く end-to-end）

# 5. Edge cases

以下は実装者が判断に迷わない粒度で振る舞いを固定する:

- 同一記録が同セッションで複数回提示される: present 行は複数入る。集計は GROUP BY で `MIN(timestamp)` を採る。追随率の分母は「初回提示以降の追随の有無」で数える
- 提示と取得の順序逆転（提示前に既に fetch/search 済み）: 集計 SQL の `fetched_at > presented_at` 条件で追随に含めない。実装上は既存 telemetry の tail に present 行が入るだけで、write path での順序制御は不要。ただし `timestamp` は既存 telemetry と同じ `CURRENT_TIMESTAMP`（秒精度）であり、daemon thread 書込の遅延と組み合わさると、同一秒内で present と fetch/search が発生した場合に `fetched_at > presented_at` が false になり、実際には追随していたケースが偽陰性として除外されうる。ミリ秒精度への変更は `injection_telemetry` 側だけでは JOIN 相手（search_telemetry / fetch_telemetry）が秒精度のままである限り効果が限定的で、両テーブル群の精度統一は本設計の管轄外とする。既知の限界として残す
- セッション識別子欠落: `_current_session_id()` が None のとき NULL 記録。集計側で `IS NOT NULL` フィルタして落とす
- at-least-once 由来の重複記録: 現状の telemetry 書込は daemon thread で 1 回のみ発行し再送機構を持たない（`search_service.py:1975-2007`）。重複が入っても GROUP BY で縮約される。UNIQUE 制約は張らない（write path 単純化のため）
- add_material に第3層添付が未実装のケース: 第3層 PR のスコープ次第で add_material の attachments が空になり得る。その場合は `_record_injection_telemetry_async` が空リストを受け取り、書込は 0 件で終わる（helper 側で早期 return）
- embedding サーバー未起動時: `find_similar_decisions` が空リストを返す（`search_service.py:1013-1014`）ため、add_decisions の第3層添付もゼロ件になる。この場合も present 行は 0 件で正常。追随率の分母は空になるだけで、書込エラーにはならない
- search が degraded な結果を返した場合: `search_telemetry.results_json` は通常通り書かれる。追随率の集計は degraded を区別しない（初期実装）。degraded ケースを切り分けたい場合は `diagnostics_json.degraded` を JOIN 条件に足す（分析側の裁量）
- 添付エンティティが集計時点で retracted 済み: `retracted_at` の考慮はしない。分析対象は「提示された事実」と「その後 fetch された事実」で、削除は present より後に起きた事象。フィルタしたい場合は analysis 側で LEFT JOIN materials/decisions/etc. して `retracted_at IS NULL` を付ける
- rank に 4 以上が入る可能性: 第3層の仕様上 top3 なので 1〜3。writer 側で assert しない（DB CHECK 制約も張らない）が、rank に大きな値が入ってきた場合は 分析側で `rank <= 3` フィルタで対処可能

# 6. Verification

この設計が保証する振る舞いと、その確かめ方:

保証事項:

- present の書込が記録系ツールの各添付エンティティに対して 1 回ずつ試みられる（daemon thread での非同期書込。書込失敗時は logger.warning のみで再試行はしないため、行が実際に残ることまでは保証しない。既存 telemetry と同じ握りつぶし規約を継承する結果であり、「1 行ずつ確実に書き込まれる」という強い保証ではない点に注意する）
- present 書込の失敗は記録系ツールの応答成功に影響しない（既存 telemetry の握りつぶし規約を継承）
- get_material 呼出が fetch_telemetry に `tool='get_material'` として記録される
- 同セッション内で presented_at 以降に fetch または search hit されたエンティティが SQL 集計で追随ありとカウントされる
- MCP コンテキスト外の呼出は caller_session_id=NULL で記録され、集計上は追随判定の対象外になる

テスト観点:

- present 書込のユニット: 添付 N 件 → injection_telemetry に N 行、trigger_tool / source_* / attached_* / rank / caller_session_id が期待通り
- present 書込の allowlist: 未知カラムを payload に混ぜたら assert で開発時に落ちる
- 書込失敗の握りつぶし: `_telemetry_get_connection` が例外を投げるモックを差し込み、add_decisions のレスポンスが壊れないことを確認
- get_material fetch 計装: `get_material` 呼出後に fetch_telemetry に 1 行、tool が `"get_material"`、items_json が `[{"type":"material","id":<id>}]`
- 追随判定の end-to-end: 同 session で present → get_by_ids で対応 id を引き、集計 SQL が body_followthrough=1 を返す
- 順序逆転の除外: fetch → present の順で書いたら追随には計上されない
- NULL セッションの除外: caller_session_id=NULL の present と fetch は集計 SQL で数えられない

# 7. 依存関係と実装順序

前提として先行必要な変更:

- 第3層添付の詳細設計（類似度計算方法、予算600字の配分方法、閾値、セッション内同一記録1回の判定実装場所）。これらは本設計の管轄外であり未確定。add_decisions の `related_decisions` は distance 付き id/title を返すのみで第3層添付の契約（manifest 形式・予算・閾値・重複排除）を満たさないため作り直しが必要（3.4節）。add_logs / add_material は新規実装
- migration 番号は実装着手時点の最大値 + 1 を採番する（先行して増える migration があれば繰り上がる）。既存 migrations ledger 経由で通常フローに載せる

本設計が単独で確定できる部分（migration のカラム定義、writable columns allowlist、`_record_injection_telemetry_async` の helper 関数、get_material の fetch_telemetry 計装）は、第3層添付の詳細設計を待たずに先行実装してよい。一方、add_logs / add_decisions / add_material 側の present 書込呼び出し実装は、第3層添付の詳細設計が確定してからでないと attachments の組み立て方が定まらない。したがって設計の順序としては第3層添付の詳細設計が先に降りる必要があり（または両者を並行して詰める必要があり）、実装 diff としては同一 PR にまとめる想定である。

同一 PR に載る変更:

- migration の追加（`_TELEMETRY_WRITABLE_COLUMNS` と同型のテーブル定義、3.3節）
- search_service に `_record_injection_telemetry_async` と allowlist 追加
- main.py の add_logs / add_decisions / add_material への present 書込呼び出し追加
- main.py の get_material に fetch_telemetry 呼び出し追加
- ユニット / インテグレーションテスト

先行しない変更:

- カウンタ単独 PR は作らない（present の書込対象がゼロなので数える対象が発生しない）
- 分析用ダッシュボード / 専用集計ツールは本設計外

# 8. 未決事項

実装時に決める点:

- 追随率算出 SQL の期間窓（`:since`）のデフォルト値。初期提案は直近30日
- fetch 判定に含めるツールの線引き。初期は get_by_ids / get_material / search の 3 種。get_decisions / get_logs（topic 起点集約）を含めるかは第3層添付の運用実績を見てから決める
- similarity 列の具体的な変換式。値域（0〜1）と方向（大きいほど類似）は本設計で統一済み（3.3節）だが、decision 以外の add_logs / add_material 添付で使う類似度指標をこの値域・方向へどう変換するかは第3層添付の詳細設計待ち
- diagnostics_json に何を入れるか。初期は NULL 固定。将来 retriever 内訳（fts / vec / tag のどの経路で候補になったか）を分析したくなったら追加
- 追随率の運用しきい値。「N% 未満なら添付形式を見直す」のような判断ラインは、まず1ヶ月分のデータを取ってから設定する
- injection_telemetry を precedent_miss / friction 等の将来の自動検知イベントの書込先として再利用するかどうか。再利用する場合、「提示済み」と「検知された欠落」という意味の異なるイベントを同じ行形式に混ぜてよいか、event_kind 列で明示的に分離すべきか、テーブル自体を分けるべきかは未検討
- 分析 SQL を誰が・いつ実行するか。専用ツールは作らない方針のため、当面は都度手動での実行を前提とし、定期的な自動集計・通知の仕組みは本設計に含めない
