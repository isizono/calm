status: 詳細設計ドラフト（レビュー用）

# 確認済み発見のリンク化（relation ratchet）詳細設計

記録時に添付する類似候補のうち、同セッション内で実際に取得（get_by_ids / get_material）まで進んだ候補に対して、取得レスポンスに一行ヒントを添える。ヒントは「関連していれば add_relation で繋げ、decision の場合は矛盾なら report_signal」という短い誘導文で、機構は同一セッション内かつ session_id が取得できる呼出に限り、候補の漏れない検出と提示タイミング・一回性・既存 relation の回避を保証する。関連有無の判定と add_relation の実行は作業中の Claude が担当し、機構側は自動リンクを行わない。

## 1. 背景と目的

「確認済み発見」の記録がある種の孤島として増え続け、後段が拾えないという課題認識は 3 層運用（前倒し計測 / check_in 督促 / 記録=クエリ添付）の議論で確定した。1 層目と 2 層目は「気づけるかどうか」を残る認知タスクとして扱うため機構では保証できない。3 層目のうち「記録直後に類似候補を提示する」までは決定論で保証できるが、提示された候補と新規記録の間に relation を張るかどうかは「読み比べて関連度を判定する」認知タスクを必ず含む。この認知タスクを機構側で自動化すると「同じ tag が付いていた」等の表層一致だけで無関係な relation が量産され、後段の get_map が汚染される。

本設計は次の 3 点を機構として保証する。

- 記録直後に提示された類似候補が、その後同セッション内で読まれた（get_by_ids / get_material が返した）事実の検出漏れをゼロにする
- 読まれた瞬間のレスポンスに、Claude が判定・実行するのに必要十分な短いヒントを一度だけ添える
- 既に relation が存在する / 対象が retracted された / 別セッションで読まれた等、ヒントを出す意味のない状況では抑制する

「Claude がヒントを読む・関連度を正しく判定する・実際に add_relation を呼ぶ」までは決定論では保証できない。ここは判定タスクとして残り、変換率（提示 → add_relation 呼出）を計測台帳で観測する対象になる。

## 2. 確定済みの制約

以下は既決事項として本設計で動かさない。

- 候補の絞りは「同セッション内で実際に取得されたヒットのみ」に限る。提示されたが読まれなかった候補にヒントは出さない
- 自動リンクは行わない。add_relation の呼出主体は必ず作業中の Claude
- 環境側キーワード照合による in-flight 注入系は凍結。新規常駐ストアは作らない。凍結対象は、相転移分類器・radar.db 等の第3ストア・Stop hook の block 強制継続・接触点リバースインデックスといった、環境側の自由テキストへ投機的にキーワード照合をかけて発火を判定する機構を指す。本設計のヒントはこれとは技術的に別物である。発火条件は「Claude が明示的に呼んだ get_by_ids / get_material の対象 ID が、直前に提示された候補 ID と一致するか」という決定論的な突合のみで、環境側テキストへの照合は一切行わない。この get_by_ids / get_material 応答へのヒント添付という具体的な仕組み自体、凍結の翌日に別途合意済みであり、凍結の再検討ゲートを待つ対象には含まれない
- 記録トリガー（source_type）は add_logs / add_decisions / add_material の3種（log / decision / material）に限る。add_topic / add_activity / add_habit 経由の記録は対象外。これは本設計が独自に絞ったものではなく、土台となる3層目マニフェスト自体の適用範囲がこの3トリガーに限定されているため、その範囲をそのまま継承している
- 新規の状態管理（`hint_shown_at`）は injection_telemetry（attachment-follow-through-counter.md が新設する計測台帳）に統合する方針。本設計は独自のテーブルを新設しない（3.2節）
- 出口はエンティティ種別で異なる。log / material 候補は relation の 1 出口、decision 候補は relation と contradiction 報告の 2 出口
- 矛盾出口が選ばれた場合、relation は張らない（supersede 判断は別プロセスの担当）
- add_relation 側の既存 API を変更しない前提で成立させる（変更する場合は最小拡張の提案に留める）
- check_in 全体予算 10,000 字を導入する方針は決定済み（実装はこれから）。超過時は選抜 + `truncated` 表示 + 継続ポインタで縮退する枠組みが別途あるが、本設計のヒント文言は候補 1 件あたり数十字レベルで、budget 節約対象の枠外として扱う

依存する未マージ／未着手の作業も本設計の前提として固定する。

- 記録時 manifest 添付（3 層目）は未着手。本設計はその実装ができてから積む
- 「追随カウンタ」の提示 → 取得ペア記録も未着手。本設計はこの記録を検出源として使う
- SessionStart 予算化ブランチと check_in 内部リライトは別プロセスで進行中。本設計は両者のいずれにも新規カラム／新規レスポンスキーを要求しない

## 3. 設計（How）

### 3.1 データフロー

登場するテーブルは 2 つ。

- `injection_telemetry`（既存流用）: attachment-follow-through-counter.md（3.3節）が定義する追随カウンタ用の台帳をそのまま再利用する。`caller_session_id` / `trigger_tool` / `source_type` / `source_id` / `attached_type` / `attached_id` / `rank` / `similarity` / `diagnostics_json` / `timestamp` の各列に加え、本設計固有の状態管理として `hint_shown_at` 列の追加を提案する（3.2節）
- `relations`（既存）: 同 PK 制約で冪等 INSERT

流れは 3 段。

1. 記録: Claude が add_logs / add_decisions / add_material を呼ぶ
2. 提示: 3 層目 manifest 実装（未着手・本設計の外）がレスポンスに類似候補 top3 を添付し、その 1 件ごとに `injection_telemetry` へ 1 行 INSERT する（attachment-follow-through-counter.md 3.4節の `_record_injection_telemetry_async`）。`timestamp` は `CURRENT_TIMESTAMP` で自動的に埋まり、これが本設計における「提示時刻」を兼ねる。`hint_shown_at` は書込時点では NULL のまま
3. 取得とヒント: 作業中の Claude が候補の中身を読むため get_by_ids / get_material を呼ぶ。この呼出自体が「取得」イベントであり、別列（`fetched_at` 等）で取得時刻を独立に持たない（呼出内側でヒント判定の同期処理が走ること自体が取得の記録を兼ねる）。呼出内側で、対象 (attached_type, attached_id) が `caller_session_id` 一致かつ `hint_shown_at IS NULL` の行にあれば、抑制条件（3.4節）を評価する。抑制に該当しなければ対応する entity のレスポンス dict へ `relation_hint` キーを埋め込む。抑制に該当する場合も含め、該当行の `hint_shown_at` を `now()` で UPDATE する

Claude はヒントを読み、関連していれば add_relation を呼び、decision で既決と衝突していれば report_signal(kind=contradiction) を呼ぶ。機構はここに介入しない。

追随カウンタ自体の「取得」計測（body_followthrough / search_followthrough）は attachment-follow-through-counter.md 3.7節が別途 `fetch_telemetry` / `search_telemetry` との JOIN で SQL 集計する。本設計はこの集計の仕組みと重複する列を持たず、ヒント判定に必要な状態（`hint_shown_at`）のみを追加する。

### 3.2 injection_telemetry のスキーマ（再利用）

本設計は新規テーブルを作らない。`injection_telemetry` は attachment-follow-through-counter.md（3.3節）が定義するテーブルをそのまま再利用する。`docs/design/README.md` の依存関係節が示す通り、relation固定（本設計）は記録=クエリ添付＋追随カウンタの実装後に載る関係にあるため、テーブルの所有権は追随カウンタ側に一本化し、本設計はそこに乗る。

再掲（定義本体と設計判断の根拠は attachment-follow-through-counter.md 3.3節を参照）:

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
    hint_shown_at      TIMESTAMP              -- 本設計が追加提案する列。NULL可
);
```

対応関係（本設計の旧ドラフトとの読替表。実装・レビュー時の混同防止のため明記する）:

| 旧ドラフトでの呼称 | 採用するカラム |
|---|---|
| `session_id`（NOT NULL） | `caller_session_id`（NULL 許容） |
| `candidate_type` / `candidate_id` | `attached_type` / `attached_id` |
| `presented_at` | `timestamp`（INSERT 時に `CURRENT_TIMESTAMP` で自動確定するため、別列を持たない） |
| `fetched_at` | 廃止。「取得」は get_by_ids / get_material の呼出そのものであり、その呼出内の同期処理が発火した事実こそが取得の記録であるため、別列での永続化を持たない。追随カウンタとしての取得計測（body_followthrough 等）は attachment-follow-through-counter.md 3.7節の `fetch_telemetry` / `search_telemetry` JOIN に委ねる（3.7節で詳述） |
| `hint_shown_at` | 変更なし。ただし attachment-follow-through-counter.md 側のテーブルへの追加列として位置づけを明確化する（下記） |
| `UNIQUE (session_id, source_type, source_id, candidate_type, candidate_id)` | 廃止。ベーステーブルに UNIQUE 制約はない。同一候補が複数 source から提示されるのは正常挙動という attachment-follow-through-counter.md 3.3節の設計判断を本設計も継承する。UNIQUE がないため同一セッション内の同一 (attached_type, attached_id) に複数行が存在しうる点は 3.3節の SELECT/UPDATE 設計に反映した |
| `CHECK(source_type IN (...))` / `CHECK(candidate_type IN (...))` | 廃止。ベーステーブルは FK・CHECK を張らない方針（「生データ台帳としての性質を優先」、attachment-follow-through-counter.md 3.3節）。値の妥当性は書き手側（3層目 manifest 実装）の責務とする |

**`caller_session_id` が NULL 許容になったことへの対応**: ベーステーブルは NOT NULL 制約を持たないため、MCP コンテキスト外の直接呼出等で `caller_session_id` が NULL のまま present 行が書かれるケースがありうる（attachment-follow-through-counter.md 3.6節）。本設計の `enrich_with_relation_hints` は呼出側の `caller_session_id` が None のときに no-op する契約（5節・6節）であり、SQL 上も `caller_session_id = :session_id` の比較は NULL 行を拾わないため、NULL 行が誤ってヒント対象になることはない。

**`hint_shown_at` 列の追加提案**: この列は追随カウンタ自身の計測結果（起きた事実の記録）ではなく、ヒントを再提示しないための制御状態であり、本設計が計測台帳に間借りする形で追加を提案するものである。attachment-follow-through-counter.md 側の migration・write path・writable columns allowlist・3.7節の効果測定 SQL のいずれもこの列を参照しない（意図的に無視する）。追加の位置づけは attachment-follow-through-counter.md 3.3節側にも追記済み。

書込経路は 2 つに分かれる。present 行の INSERT（`caller_session_id` 〜 `timestamp` の各列）は attachment-follow-through-counter.md が定義する非同期 daemon thread 経路（`_record_injection_telemetry_async`）で行われ、本設計はここに関与しない。一方 `hint_shown_at` の UPDATE は get_by_ids / get_material 呼出の同期経路（3.3節）で行う、別のコードパスである。`_TELEMETRY_WRITABLE_COLUMNS` allowlist は present 行の INSERT 対象列を制御するものなので、`hint_shown_at` をこの allowlist に加える必要はない。

追加するインデックス（ベーステーブルの既存 2 インデックスに加えて本設計が提案するもの）:

```sql
CREATE INDEX idx_injection_telemetry_hint_lookup
    ON injection_telemetry (caller_session_id, attached_type, attached_id)
    WHERE hint_shown_at IS NULL;
```

このインデックスは 3.3節の一括 SELECT（`caller_session_id` 一致 かつ `hint_shown_at IS NULL` の絞り込み）を高速化する目的で追加する。attachment-follow-through-counter.md 側の migration に同時に入れるか、本設計側の後続 migration で追加するかは 7 節（依存関係と実装順序）に従う。

### 3.3 ヒントの検出と埋め込み

get_by_ids / get_material の内部で以下を実行する。責務の切り出し先として `services/relation_hint_service.py` を新設し、レスポンス組み立ての最後段で 1 回だけ呼ぶ。

```python
def enrich_with_relation_hints(
    conn: sqlite3.Connection,
    caller_session_id: Optional[str],
    entries: list[dict],   # get_by_ids の場合は results 、get_material は単一 dict をリスト化
) -> None:
    """entries を in-place で書き換える。get_by_ids から渡される要素は {type, data} 形式のため、
    relation_hint は各要素の data 直下に追加する。get_material 呼出側は単一 dict をそのまま
    リスト化して渡すため、その dict 直下に追加する。caller_session_id が None なら no-op"""
```

内部処理は 4 ステップ。

1. `entries` から `(attached_type, attached_id)` を抽出（get_by_ids/get_material が返す種別・IDが injection_telemetry の `attached_type` / `attached_id` に対応する）。retracted のものは対象外
2. 一括 SELECT で該当行を取得:

   ```sql
   SELECT id, source_type, source_id, attached_type, attached_id, rank, similarity
   FROM injection_telemetry
   WHERE caller_session_id = :session_id
     AND (attached_type, attached_id) IN (...)
     AND hint_shown_at IS NULL
   ```

   `timestamp` は INSERT 時に NOT NULL で確定しているため、旧ドラフトにあった `presented_at IS NOT NULL` 相当の絞り込みは不要（該当行が存在する時点で「提示済み」が保証されている）
3. 各行について抑制フィルタを適用（3.4 参照）。ベーステーブルに UNIQUE 制約がないため、同じ (attached_type, attached_id) に対し複数 source からの複数行がヒットしうる。生き残った行・抑制された行の双方について、`caller_session_id` と `(attached_type, attached_id)` が一致する行全部の `hint_shown_at` を一括 UPDATE する（一部だけ更新すると取りこぼした行が次回呼出で再度ヒットし、二重判定になるため）
4. entries に `relation_hint` を埋め込む。同じ候補に対し複数 source から提示されていた場合は `candidate_of` を配列で並べる（レスポンス上の呼称は引き続き「candidate」を使う。3.2節の対応表の通り、これは DB 上の `attached_type` / `attached_id` を指す）

ステップ 2・3 の JOIN は source_type（最大3種）・attached_type（最大5種）それぞれ異なるテーブルにまたがるため、単一 SQL で完結させるには `UNION ALL` または `CASE` によるテーブル出し分けが必要になる。「一括 SELECT」という表現は 1 回のクエリ発行で完結させる意図を指しており、単純な単一テーブル JOIN と同等の実装難度という意味ではない。

「取得」イベントを injection_telemetry に別列で記録しないことについて: 旧ドラフトはステップ 3 の前段で `fetched_at` の UPDATE を独立に行っていたが、これは冗長である。get_by_ids / get_material の内部でこの一連の処理が実行されていること自体が「取得された」事実であり、SELECT がヒットした時点でそのセッションでの取得は成立している。取得の集計（追随率としての body_followthrough）が別途必要な場合は、attachment-follow-through-counter.md 3.7節の `fetch_telemetry` / `search_telemetry` との JOIN による既存の集計クエリを使う（3.7節）。

### 3.4 抑制フィルタ

以下いずれかに該当すればヒントを出さない。

- 対象 candidate が retracted（レスポンス dict に `retracted_at` がある）。ただし get_material のデフォルト呼出（`include_retracted=False`）は retracted な資材を `NOT_FOUND` として返し `_material_to_response` 自体を呼ばないため、この条件が実際に効くのは get_by_ids 経由（retracted 行もそのまま返す）にほぼ限られる。get_material に `include_retracted=True` を明示指定した場合は呼出側が意図して retracted を見ているため抑制が過剰という考え方もあるが、本設計では一貫性を優先し同条件で抑制する
- 対応する source_id が既に retracted。バッチ SELECT 内で source 側の retracted_at を JOIN で見て弾く
- 該当ペアに relation が既存（`relations` を `_normalize_pair` で正規化してから存在チェック。`relation_type` は問わない）
- 自己参照 `(source_type, source_id) == (attached_type, attached_id)`。3 層目実装で除外される想定だが防御的に弾く

抑制した場合も hint_shown_at を UPDATE する（同セッション内の再取得で再判定させない。「読まれたが抑制で提示スキップ」は本設計の観点では「一度提示相当」として扱い終端させる。retracted 状態が後で復活しても再提示はしない。復活時のフォローは別途 audit 経路に委ねる）。

### 3.5 レスポンスに追加するキーの形

get_by_ids は `entries[i].data` に、get_material は返却 dict の最上位に、それぞれ `relation_hint` キーを追加する。既存キーは一切変更しない。

log / material 候補（1 出口）の例。

```json
{
  "relation_hint": {
    "outlets": ["relation"],
    "candidate_of": [
      {"source_type": "log", "source_title": "get_map の再帰CTE挙動", "similarity": 0.31}
    ],
    "message": "この記録は直前に書いた記録（candidate_of に1件以上列挙）の類似候補として提示されたもの。candidate_of の各 source ごとに関連の有無を判断し、関連していれば add_relation で繋げ。無関係なら無視してよい。"
  }
}
```

decision 候補（2 出口）の例。

```json
{
  "relation_hint": {
    "outlets": ["relation", "contradiction"],
    "candidate_of": [
      {"source_type": "decision", "source_title": "check_in の予算切り詰め方針", "similarity": 0.22}
    ],
    "message": "この decision は直前に書いた decision（candidate_of に1件以上列挙）の類似候補として提示されたもの。candidate_of の各 source ごとに、判例として関連していれば add_relation、既決と衝突していれば report_signal(kind=contradiction) で報告し、その source への relation は張らないこと。"
  }
}
```

`source_title` は source 側から SELECT する。表層一致で誤リンクさせないために「直前の記録がどれか」を最小限想起可能な形で見せる。id そのものは Claude 側が既に対象記録を持っている（直前のレスポンスで返っている）ため不要。ただし相関追跡のためのデバッグ情報として `session_id` や injection_telemetry の主キーは載せない（決定表現に不要かつ肥大化する）。

`candidate_of` が複数件並ぶ場合（同じ candidate が複数の記録から類似候補として提示されていた場合）、Claude は各 source を独立に評価してよい。関連するものが 1 件なら該当 source とだけ add_relation を張り、複数関連するなら複数回 add_relation を呼んでよい。「候補に並んだ source 全部と機械的につなぐ」ことを機構は要求しない。

### 3.6 add_relation 側の変更

不要と判断する。既存 API で足りる根拠は以下。

- 冪等性は relations テーブル PK 制約（source_type, source_id, target_type, target_id）と INSERT OR IGNORE で担保済み
- Claude 側は「関連あり」と判定した後、通常の add_relation 呼出フォーマット `targets=[{"type": candidate_type, "ids": [candidate_id]}]` で呼べる
- ヒント経由での relation 追加を区別したい要求は現状ない（後述の効果測定はテーブル JOIN で解決する）

もし将来「ヒント経由 relation」であることを痕跡として残したくなった場合は、add_relation の内部で「そのペアの injection_telemetry 行があれば `relation_created_at = now()` を UPDATE」する形で最小に載せられる。本設計では入れず、テーブル拡張案として 8 節に残す。

### 3.7 効果測定

計測は SQL で後付け算出する。injection_telemetry と relations（decision 候補についてはさらに signals）の突合で以下を出せる。カラムは attachment-follow-through-counter.md 側のスキーマ（3.2節）に準拠する。

- 提示件数: `SELECT COUNT(*) FROM injection_telemetry WHERE caller_session_id IS NOT NULL`（`timestamp` は INSERT 時に必ず埋まるため `IS NOT NULL` 条件は不要。`caller_session_id IS NOT NULL` のみで対象を絞る）
- 取得件数: injection_telemetry 単体には「取得」を表す列がない（3.2節・3.3節参照）ため、attachment-follow-through-counter.md 3.7節の集計クエリ（`fetch_telemetry` / `search_telemetry` との JOIN で `body_followthrough` / `search_followthrough` を出す）をそのまま再利用する。本設計独自の取得件数クエリは持たない
- ヒント提示件数: `SELECT COUNT(*) FROM injection_telemetry WHERE hint_shown_at IS NOT NULL`（抑制により hint_shown_at のみ埋まったケースも含まれる点は 3.2節・3.4節の通り）
- relation 変換件数: 上記行のうち、`(source_type, source_id, attached_type, attached_id)` を `_normalize_pair` と同じ正規化規則で relations と突合できたもの。Python の `_normalize_pair` をそのまま SQL に持ち込めるわけではなく、SQL 側で同等の正規化式（source/target の順序を揃える CASE 式等）を別途組む必要がある
- relation 変換率: relation 変換件数 / ヒント提示件数
- decision 候補の contradiction 件数（参考指標。変換率には含めない）: ヒント提示された decision 候補のうち、同一セッション内で対象 decision に対して report_signal(kind=contradiction) が呼ばれた件数

`relation 変換率` は「関連 → relation」出口のみを分子にした指標であり、「衝突 → contradiction」出口が正しく選ばれたケースは分母に残ったまま分子に入らない。decision 候補で contradiction 出口が正しく機能しているセッションほど relation 変換率は見かけ上低く出るため、この数値だけで「機構が機能していない」と読まれないよう、contradiction 件数を並記の参考指標として運用開始時から出す。変換の定義に contradiction を含めるかどうかの最終判断は 8 節の未決事項とする。

`scripts/ops_metrics.py` の集計対象に追加する形で運用側から観測できるようにする。閾値監視までは本設計の外。

## 4. 変更ファイル一覧

- `migrations/00XX_add_injection_telemetry_hint_shown_at.sql`（または attachment-follow-through-counter.md 側の CREATE TABLE migration への同梱。7節参照）: `hint_shown_at` 列の追加（先方が未マージなら CREATE TABLE に含める、マージ済みなら `ALTER TABLE injection_telemetry ADD COLUMN hint_shown_at TIMESTAMP`）と 3.2節の追加 INDEX
- `src/services/relation_hint_service.py` 新規: 3.3 の `enrich_with_relation_hints`。抑制フィルタ・batched SELECT・batched UPDATE を持つ
- `src/services/search_service.py` 修正: `get_by_ids` の最後に `enrich_with_relation_hints(conn, caller_session_id, results)` を呼ぶ 1 行追加
- `src/services/material_service.py` 修正: `get_material` の関数シグネチャに `caller_session_id: Optional[str] = None` を追加し、返却直前（`_material_to_response` の呼出後）に単一 dict をリスト化して同関数へ渡す
- `src/main.py` 修正: `get_by_ids` は既に `caller_session_id=_current_session_id()` を渡しているため無修正で済む。一方 `get_material` の MCP エンドポイントは現状 `material_service.get_material` に session_id を渡していないため、`get_by_ids` と同様に `caller_session_id=_current_session_id()` を渡す 1 行の追加が必要
- `scripts/ops_metrics.py` 修正: 変換率レポート 1 セクション追加

`injection_telemetry` テーブル自体・present 行の INSERT（`_record_injection_telemetry_async`）・`_TELEMETRY_WRITABLE_COLUMNS` allowlist は attachment-follow-through-counter.md 側の変更ファイル一覧に既に含まれる。本設計はテーブルを新設しないため、この allowlist へのエントリ追加は行わない（`hint_shown_at` は allowlist を経由しない同期 UPDATE で書く。3.2節参照）。

## 5. Edge cases

- 別セッションで取得された場合: `caller_session_id` 一致で絞るため対象外。次に対象を提示した session の Claude はそれを認識できないが、これは意図通り（別セッションが記録済みの relation を張っていることを期待）
- ヒント後に候補が retract された場合: hint_shown_at は既に埋まっており再提示されない。Claude 側が retract を受けて別途 audit を回せば良い
- ヒント前に候補が retract された場合: 3.4 で抑制し hint_shown_at を埋める。retract が後で取り消されても再提示はしない
- 自己参照: 3.4 で防御的に弾く
- 矛盾出口が選ばれた場合: Claude は report_signal(kind=contradiction) のみ呼び、add_relation を呼ばない。機構は relation の非存在を許容する（relation 変換率の計算では「未変換」に計上されるが、これは想定内。3.7 節の contradiction 件数（参考指標）でこのケースを別途可視化する）
- 同セッション内で同じ id を複数回 get_by_ids に渡した場合: 1 回目で hint_shown_at が埋まり、2 回目以降は抑制
- get_by_ids の items にヒント対象と非対象が混在: 対象のみ relation_hint が付く。非対象は既存レスポンス通り
- 3 層目 manifest 実装がまだ稼働していない環境: injection_telemetry に present 行が入らないため `enrich_with_relation_hints` は全 no-op。既存レスポンスと差分ゼロ
- caller_session_id が None（MCP コンテキスト外の直接呼出）: `enrich_with_relation_hints` は先頭で return して no-op
- 3 層目 manifest 実装側で present 行書込時に `caller_session_id` が取得できない場合: attachment-follow-through-counter.md の契約により NULL のまま INSERT される（同設計 3.6節。ベーステーブルの `caller_session_id` が NULL 許容であるため、旧ドラフトが想定していた「INSERT 自体のスキップ」は起きない）。3.2節の通り `caller_session_id = :session_id` の比較は NULL 行を拾わないため、読取側の抑制判定は自然に no-op になる
- embedding サーバーが落ちていて manifest 側で候補が 0 件だった場合: injection_telemetry に何も入らないため本経路も no-op
- 同一 (attached_type, attached_id) に対し複数 source から提示され複数の present 行が存在する場合: 3.3節の一括 SELECT は該当行すべてを拾い、UPDATE も該当行すべての `hint_shown_at` をまとめて確定する（ベーステーブルに UNIQUE 制約がないため、行単位ではなく (caller_session_id, attached_type, attached_id) 単位で状態をまとめる必要がある。3.2節の対応表参照）

## 6. Verification

保証すべき振る舞いと確かめ方。

- 3 層目が提示 → 同セッションで get_by_ids で候補 id を引く → レスポンス dict に `relation_hint` が 1 度だけ付く。2 回目の get_by_ids では付かない（hint_shown_at 埋まり済み）
- 別セッションで同 candidate を get_by_ids しても付かない（caller_session_id 不一致）
- 既に relations に該当ペアがある状態で get_by_ids しても付かない
- 候補が retracted 状態のとき付かない
- source が retracted 状態のとき付かない
- decision 候補には `outlets: ["relation", "contradiction"]`、log / material 候補には `outlets: ["relation"]` が入る
- Claude が add_relation を呼んだ後の 2 回目取得は relations 既存判定で抑制される（1 の再確認）
- relation 変換率クエリが、`hint_shown_at IS NOT NULL AND (正規化済みペアが relations に存在する)` の件数 / hint_shown_at 件数で算出できる
- パフォーマンス: get_by_ids 1 回あたりの追加コストは、対象 candidate 数 N（N <= 20、GET_BY_IDS_MAX）に対して、候補行の一括取得・source 側 retracted 確認・relations 既存判定・source_title 取得を含む複数 JOIN 込みの SELECT 群と、生存行への一括 UPDATE。「1 SELECT + 1 UPDATE の 2 クエリ」のような単純な固定数には収まらず、JOIN 対象テーブル数（source_type 3 種 × attached_type 5 種の組合せ）に応じて条件が増える。N の上限があるため悪化しても線形的な範囲に収まる想定だが、実測はしておらず断定はしない。実装時にレイテンシを計測して確認する

テスト観点。

- `enrich_with_relation_hints` の単体テスト: caller_session_id None / 空 entries / 抑制フィルタ各条件 / 複数 source から同 candidate / decision vs log の outlets 差異
- get_by_ids 統合テスト: manifest 記録済みの状態で get_by_ids を呼び、relation_hint が付くこと・2 回目で付かないこと
- migration の up / down 確認: `hint_shown_at` 列と新設 index が期待通り張られていること、attachment-follow-through-counter.md 側の write path（present 行 INSERT）・既存 telemetry 系テーブルへの影響がないこと（本設計は既存テーブルへの列追加であり、新規 CREATE TABLE ではない点に注意）
- 既存 fetch_telemetry テストが壊れないこと（get_by_ids の shape 変更は data 配下のみで、fetch_telemetry の記録内容は不変）

## 7. 依存関係と実装順序

前提として、記録時 manifest（3 層目）・追随カウンタ（attachment-follow-through-counter.md）はいずれも本設計に先行する。`docs/design/README.md` の依存関係節が明記する通り、relation固定は「記録=クエリ添付＋追随カウンタの実装後」に着手する関係にあり、本設計はテーブル定義を新規に持たない（3.2節）。両者と本設計の関係は以下。

- 3 層目 manifest 実装（外の作業）: add_logs / add_decisions / add_material のレスポンスに類似候補 top3 を添付し、それぞれを `injection_telemetry` に INSERT する。attachment-follow-through-counter.md が定義する present 書込経路（`_record_injection_telemetry_async`）の唯一の呼出元
- 追随カウンタ（attachment-follow-through-counter.md、外の作業）: `injection_telemetry` テーブル自体・present 行の書込 API・`_TELEMETRY_WRITABLE_COLUMNS` allowlist・`fetch_telemetry` / `search_telemetry` との JOIN による追随率集計を定義する。本設計はこのテーブルを再利用する側であり、テーブルの所有権・present 行の書込・追随率そのものの集計はすべて同設計の管轄

本設計自身が持ち込むのは `hint_shown_at` 列とその読み書き（`relation_hint_service.py`）のみである。したがって本設計の migration は attachment-follow-through-counter.md の migration に対する ALTER（列追加）、または両設計が同一 PR で入るならその CREATE TABLE への同梱のいずれかになる。単独の CREATE TABLE は発生しない。

本設計内の順序。

1. attachment-follow-through-counter.md 側の migration がマージ済みであることを確認する（未マージなら 3.2節の通り `hint_shown_at` を先方の CREATE TABLE に同梱してもらうよう調整するか、本設計側で ALTER TABLE migration を追加する）
2. `hint_shown_at` 列と 3.2節の追加 INDEX の migration
3. `relation_hint_service.py` 新設と単体テスト
4. `search_service.get_by_ids` と `material_service.get_material` への呼出配線
5. `scripts/ops_metrics.py` に変換率レポート追加

3 層目 manifest 実装が完了してマージされてから 2〜4 を積むと、テーブルが空でない状態で挙動確認できる。順序を逆にする場合は、テストデータで injection_telemetry に手動 INSERT して統合テストを回す。

## 8. 未決事項

実装時に確定させる点。

- 変換率計測で「contradiction signal 作成」を relation 変換率の分子に含めるか。案としては relations JOIN と signals JOIN を UNION して両方を「変換」に含める。決めきる材料は運用開始後 1〜2 サイクル分のデータで、初期は relation のみを分子とし、contradiction 件数は 3.7 節の並記の参考指標として別出しする
- ヒント文言の最終 wording: 3.5 の draft をたたき台にし、実装時に短縮／固有名調整を行う。文言変更は挙動保証範囲外
- 「ヒント経由 relation」の痕跡カラム（3.6 末尾）: 効果測定が JOIN で十分回るなら不要。運用で JOIN コストが問題になったら add_relation 内で UPDATE を足す
- rank / similarity カラムの精度: 3 層目 manifest 実装で使う find_similar_* の返り値をそのまま入れる。float 精度は既存 vec_index に合わせて 4 桁丸め
- get_material のような単体取得系で「同ターン内 hint_shown 抑制」が過剰になるケース: 現状想定なし。observed されたら hint_shown_at の意味論を「見せた」から「見せて Claude が反応するチャンスを与えた」に読み替えて対処
