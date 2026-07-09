status: 詳細設計ドラフト（レビュー用）

## 要旨

タグに退役フラグ（archived）を持たせ、tag notes の自動 push 注入から機械的に除外する。search 等の pull 経路では削除せず、archived ラベル付きでランキング降格した状態で応答に載せる（ただし降格は相対的なスコア減衰であり、非 archived アイテムより必ず下位に来ることを数式で保証するものではない。3.5 参照）。付与・解除は人・エージェントの明示操作で行い、機械判定は含めない。フラグは `tags` テーブルへの `archived_at` 追加、押し出し部の抑止は `collect_tag_notes_for_injection` 内 SQL の `AND archived_at IS NULL` に集約する。この保証の範囲は「同関数を経由する注入経路」に限られ、経由しない別の notes 読み出し経路が将来応答注入用途に転用された場合は対象外になる（3.4 参照）。check_in は内部リライトが別セッションで進行中のため本設計のスコープ外（3.6 参照）。効果計測（22.1% 削減が実際に効いているかの計測）もスコープ外（2. 参照）。

---

## 1. 背景と目的

検討会で確定した認知境界（気づく・読む・活かすは決定論では触れられない、状態は保証できる）に照らし、この設計は「タグに付いた退役の状態」を DB 上に明示し、そこからカスケードで:

- push（自動注入）経路に退役タグの notes が**確実に載らない**こと
- pull（search・check_in 等の取得系）経路では退役タグ配下のエンティティが結果集合から**消えない**こと（archived ラベル付き・下位表示）

の2点を機構として保証することを目的とする。

背景数値として、解体済み指揮層系タグ（旧 orch/dispatcher/worker 体系関連）の notes だけで、tag notes 全体注入量の 22.1% を占めている（実測済み）。これらは既に解体済みで現在の作業には直接効かないが、tag notes として毎セッション初回注入される仕組みに乗り続けている。単に notes を削除する運用は、後から「解体前はどう決めていたか」を再確認できなくする恐れがあるため、「引けば見つかる、勝手には出てこない」という中間の状態を持てる仕組みを入れる。

---

## 2. 確定済みの制約

以下は本設計で動かさない前提。

- 退役の付与単位は**タグ単位**から始める。エンティティ単位（個別 decision/log/material の退役）への拡張は現時点でスコープ外。将来必要になったときに別設計として扱う。
- 付与判断は人・エージェントの明示操作のみ。機械判定（利用頻度・古さ・supersede 連鎖等からの自動 archive 提案）は含めない。
- push 経路: tag notes 自動注入からは**完全除外**する。退役タグの notes は初回セッションでも注入しない。
- pull 経路: 削除・非表示にはしない。archived ラベル付きでランキング降格し、応答に載る形にする。
- 常駐ストア（環境側キーワード照合による in-flight 注入）は凍結中のため、そこには乗せない。既存の DB 上のタグに機能追加する形で実現する。
- 記録=クエリ添付は別マイルストーンで未着手のため、本設計はそのレスポンス添付機構には触れず、tag notes 注入と search の 2 経路のみに閉じる。
- check_in の内部リライトは別セッションで詳細設計進行中で、既存 `checkin_service.py` は作り直される前提になっている。本設計は check_in の応答フィールドには一切触れない。push 除外（`collect_tag_notes_for_injection` 経由）の効果のみが check_in にも自動的に及ぶ（3.6 参照）。
- 効果計測（archived 化によって tag notes 注入量が実際にどれだけ減ったか等の継続計測）は本設計のスコープ外とする。将来計測を追加する場合は、新規計測は injection_telemetry に統合し新規ストアを作らないという既存方針に従う。本設計自体はこの方針と矛盾するテレメトリ経路を作らない。

---

## 3. 設計（How）

### 3.1 データフロー全体像

3 経路をそれぞれ次のように扱う。

**push 経路（tag notes 自動注入）:**

現状、tag notes を自動的に応答に混ぜているのは `collect_tag_notes_for_injection`（`src/services/tag_service.py` 1069-1141行）が唯一のチョークポイントである。ここが `notes IS NOT NULL` の行のみを SELECT している SQL に、`AND archived_at IS NULL` を追加する。呼び出し元（`src/main.py` の `_maybe_inject_tag_notes` 経由の 11 経路、および `src/services/checkin_service.py` 481行）は改修不要。これにより、この 2 モジュール経由の注入は取りこぼしなく除外される（保証の範囲は 3.4 参照）。

**pull 経路（search）:**

`src/services/search_service.py` の `_rrf_merge` と `_apply_recency_boost` の後段に、archived フラグを持つタグしか付いていないエンティティに対して降格係数を適用する処理を追加する。結果アイテムに `archived: True` を明示し、`score_breakdown` にも `archived_factor` を残す。ソートは既存の `final_score` 降順のまま維持し、降格係数によって自然に下位に落ちる形にする。

**pull 経路（check_in・get 系）:**

check_in は現状 `activity_tags` 経由でアクティビティ自身のタグ notes を注入している（`src/services/checkin_service.py` 478-481行）。ここも push 経路と同じチョークで自動除外される。check_in はこの SQL レベルの自動除外効果のみを本設計の保証範囲とし、`archived_tags` フィールドの追加や pinned targets への `archived` フラグ付与など、check_in 応答への新規フィールド追加は行わない（理由は 3.6 参照。check_in は内部リライトが別セッションで詳細設計進行中で、既存 `checkin_service.py` は作り直される前提のため、その構造に増築する設計はしない）。get_topics/get_logs/get_decisions/get_activities/get_by_ids（いずれも check_in 内部リライトの対象外）の各応答では、既存の `tags` フィールドに含まれる archived タグに対して、応答トップレベルに `archived_tags` の集約を返す。

### 3.2 スキーマ変更

新規 migration を追加する。ファイル名: `migrations/0058_add_tag_archived.sql`（既存最新は 0057）。

```sql
-- up
ALTER TABLE tags ADD COLUMN archived_at TIMESTAMP DEFAULT NULL;
ALTER TABLE tags ADD COLUMN archived_reason TEXT DEFAULT NULL;

CREATE INDEX idx_tags_archived_at ON tags(archived_at) WHERE archived_at IS NOT NULL;

-- down は用意しない（列削除は SQLite の再構築が必要なため、他 migration の慣行に合わせて片方向のみ）
```

**選定理由（別案との比較）:**

1. `tags.archived_at` 列を追加（採用案）:
   - 単一テーブルで一貫、既存 SELECT に `AND archived_at IS NULL` を足すだけで push 除外が完了する
   - notes/description/canonical_id と並列の属性として自然
   - archived タグと非 archived タグの JOIN が不要でクエリが単純
2. 別テーブル `tag_archives(tag_id, archived_at, reason, archived_by_session)` を新設:
   - 履歴を残せる利点があるが、退役の「現在の状態」を判定するために毎回 EXISTS/JOIN が必要になり、注入経路全部にクエリ複雑化のコストが乗る
   - 履歴が本当に必要になった時点で（ユーザーが archived の再判断を実運用で行うようになった時点で）別設計として追加すれば良い
3. namespace に `archived:` を作る:
   - CHECK 制約が 0039 で撤去されているので技術的には可能だが、既存 tag_strings（`domain:foo` 等）と直交する属性を namespace に混ぜると search / RRF / notes 全経路の分岐が複雑化する
   - archived は「その `domain:foo` の状態」であって、`domain:foo` と並列の別タグではない

`archived_reason` はワンライナーの理由（100 文字未満想定、CHECK 制約は付けない）。UI 表示・監査時に「なぜ退役したか」を辿るための最小限。

### 3.3 API・インターフェース

**`update_tag` の拡張:**

現状シグネチャ（`src/main.py` 775-815行、実装は `src/services/tag_service.py` 751-1032行）:

```python
def update_tag(tag, notes=None, canonical=None, rename=None, description=None) -> dict
```

archived を追加する:

```python
def update_tag(
    tag: str,
    notes: Optional[str] = None,
    canonical: Optional[str] = None,
    rename: Optional[str] = None,
    description: Optional[str] = None,
    archived: Optional[bool] = None,
    archived_reason: Optional[str] = None,
) -> dict
```

**動作:**

- `archived=True`: `archived_at = CURRENT_TIMESTAMP` を設定。既に archived の場合は `archived_at` を書き換えず（冪等）、`{"tag": ..., "archived": True, "updated": False}` を返す。
- `archived=False`: `archived_at = NULL`, `archived_reason = NULL` に戻す。既に非 archived の場合は同様に `updated: False`。
- `archived_reason` は `archived=True` と併用のときのみ有効。単独指定はバリデーションエラー。`archived=False` のときは必ず NULL に戻す（残しておくと復帰後も過去の理由が残って混乱するため）。
- 相互排他制約: `notes` / `canonical` / `rename` / `description` / `archived` は 1 呼び出しで 1 種類のみ指定可能（既存の相互排他ルールに `archived` を追加）。ただし `archived_reason` は `archived=True` に付随するオプション扱いで、相互排他カウントには含めない。
- この相互排他は「同一呼び出し内で同時に指定できるパラメータの組み合わせ」を制限するものであり、対象タグの現在の archived 状態を制限するものではない。archived 中のタグでも `update_tag(tag=X, notes=...)`（`archived` は指定しない）は 1 コールで完結し、`archived_at` には影響しない。「解除しないと notes を更新できない」わけではない。3-コール運用が必要になるのは `archived_reason` だけを後から書き換えたい場合に限られる（`archived_reason` 単独指定は不可のため。5. Edge cases 参照）。

**返り値:**

```python
# 成功時
{"tag": "domain:orch-legacy", "archived": True, "archived_at": "2026-07-09T...", "archived_reason": "...", "updated": True}
{"tag": "domain:orch-legacy", "archived": False, "updated": True}
# 冪等（既に同状態）
{"tag": "...", "archived": True, "updated": False}
# エラー
{"error": {"code": "CONFLICTING_PARAMS", "message": "..."}}
{"error": {"code": "ORPHAN_ARCHIVED_REASON", "message": "archived_reason requires archived=True"}}
```

**canonical との整合:**

archived タグをエイリアス先（canonical）に指定するのは禁止（エイリアス先が退役していると意味を成さない）。逆に archived タグを canonical 参照持ちに設定するのも許容しない（退役中のタグは新しいエイリアス関係を作らせない）。バリデーションエラー: `code=ARCHIVED_CANONICAL_INVALID`。既存で canonical 関係を持っているタグを archived にしようとした場合は、その関係を明示的に確認する形にする（現時点では単純にエラーとし、ユーザーが先に canonical を解除してから archived を付ける運用）。

このバリデーションは `update_tag` 実装内（アプリケーション層）でのみ行い、SQL の CHECK 制約やトリガーでは強制しない。既存の canonical 関連バリデーション（エイリアス連鎖禁止・notes 付きタグのエイリアス化禁止）も同様にアプリケーション層のみで強制されており、この設計はその既存パターンを踏襲する。そのため migration の手動適用や直接 SQL 操作で `archived_at` と `canonical_id` を矛盾した状態に設定することは技術的には可能で、契約は「API 経由の操作に限る」という条件付きである。

### 3.4 push 除外（tag notes 注入）

`collect_tag_notes_for_injection`（`src/services/tag_service.py` 1069-1141行）の SELECT を変更する。

現状（1128-1131行）:

```python
rows = conn.execute(
    f"SELECT namespace, name, notes FROM tags WHERE ({placeholders}) AND notes IS NOT NULL",
    params
).fetchall()
```

変更後:

```python
rows = conn.execute(
    f"SELECT namespace, name, notes FROM tags "
    f"WHERE ({placeholders}) AND notes IS NOT NULL AND archived_at IS NULL",
    params
).fetchall()
```

**なぜここか（構造的取りこぼし防止）:**

scout 報告で `collect_tag_notes_for_injection` の呼び出し元を確認した結果、tag notes 自動注入の呼び出しは以下の 2 モジュールのみに集約されている。

- `src/main.py` の `_maybe_inject_tag_notes`（156-178行）から呼ばれる 11 経路（add_topic/add_logs/add_decisions/get_topics/get_logs/get_decisions/pull_precedents/search/get_by_ids/add_activity/get_activities）
- `src/services/checkin_service.py` 481行の check_in

`hint_service._get_topic_domain_tag_notes` は別実装（`src/services/hint_service.py` 390,408行）だが、hint skip 判定用途で応答混入経路ではない。`decision_service._append_tag_notes_with_conn`（`src/services/decision_service.py` 16行 import、`src/services/tag_service.py` 1144行実装）は decision の伝搬機能で、tag の notes に decision 内容を追記する**書き込み**側であって、notes を読んで応答に注入する側ではない。archived タグに対してもこの追記自体は妨げない（archived は「自動で読み出して見せない」状態であって「書き込みを禁止する」状態ではないため）。追記後も `collect_tag_notes_for_injection` の SELECT は archived_at で除外するので、追記された内容が push 経路に漏れることはない。

呼び出し元を 1 箇所ずつ改修すると、将来別の場所で `collect_tag_notes_for_injection` を呼び足したときに取りこぼしが起きる。SQL の SELECT 側で機械的に除外することで、呼び出し元が増えても構造的に破れない。

**保証の範囲（この設計が防げる取りこぼしとそうでないもの）:**

この保証が及ぶのは「`collect_tag_notes_for_injection` を経由する注入経路」に限られる。`tags.notes` を読む SELECT 自体は、上記の 2 モジュール以外にも存在する。`src/services/hint_service.py` の `_get_hints_for_tag`（227行付近）と `_get_topic_domain_tag_notes`（408-421行）が該当するが、いずれも notes 内容を抑制マーカー判定（`_is_marker_active`）に使うだけで、notes 本文を応答に混入させる用途ではない。そのため現時点でこれらは push 除外の対象外としてよい。ただし、これらの経路や将来新設される経路が notes 本文を応答表示に転用するようになった場合、この設計の保証範囲外になる。この不変条件（notes を応答に注入する経路は `collect_tag_notes_for_injection` に一本化する）を機械的に強制する仕組み（lint・CI 上の grep 検査等）は用意しない。6. Verification に、既知の notes-SELECT 箇所を列挙してこの前提を定期確認するテスト観点を追加する。

**セッション別注入済みマーカーとの相互作用:**

`_injected_tags` セッション別 set は「同一セッションで一度処理を試みた通常タグは 2 回目以降クエリしない」ためのマーカーである。`collect_tag_notes_for_injection`（`src/services/tag_service.py` 1107-1117行）の実装では、このマーカーへの登録は SELECT の実行より**前**に行われる。つまり、あるタグがそのセッションで初めて `tag_strings` に渡された時点で、そのタグに notes があるか・archived かに関わらず `_injected_tags` に登録される。

このため archived タグについては次の挙動になる:

- あるセッションで一度も参照されていない archived タグを解除した場合、その後そのタグが初めて参照されたときに通常どおり notes が注入される（新規タグと同じ扱い）。
- 一方、あるセッション中に archived な状態で一度でも参照されたタグは、その時点で `_injected_tags` に登録済みになる。同じセッション内で後から解除しても、次回参照時は「登録済み」として SELECT 自体がスキップされるため、notes は注入されない。解除後の notes 注入が保証されるのは**次のセッション**からであり、「同じセッションで解除後の初回に必ず届く」わけではない。

この非対称性はセッション別マーカーの既存仕様（一度触れたタグは同セッション内で再クエリしない）から来ており、archived 固有の追加処理ではない。運用上の影響は小さいと考えられる（同一セッション内で archived 解除操作をした直後に notes 内容を確認したい場合は `search_tags(include_notes=True)` 等の pull 経路で直接確認できる）が、Edge cases・Verification の記述はこの実際の挙動に合わせて修正する。

### 3.5 pull 側の下位表示（search）

`src/services/search_service.py` に `_apply_archived_demotion` を追加し、`_apply_recency_boost` の直後に呼ぶ。

**降格ロジック:**

```python
ARCHIVED_DEMOTION_FACTOR = 0.3  # src/config.py に定数として追加
```

- 各結果アイテムのタグ集合を確認し、少なくとも1つの非 archived タグを持つ場合は「素タグに現役のものが残っている」とみなして降格しない
- 全てのタグが archived の場合、または（現実的に見て）タグが 1 つでその 1 つが archived の場合に限り、`final_score = final_score * ARCHIVED_DEMOTION_FACTOR` を適用
- 適用したアイテムには `archived: True` フラグと、`archived_tags: ["domain:orch-legacy", ...]` を明示
- `score_breakdown.archived_factor` に適用した係数（0.3 か 1.0）を残す
- 再ソートは `_apply_recency_boost` 内で完結済みだが、降格後は改めて `final_score` 降順で再ソート。`_apply_recency_boost` 内に統合せず別関数に分けているのは、recency 補正（時間経過による自然減衰）と archived 降格（人が明示的に付けた状態による減衰）が性質の異なる補正であるため、責務を分けて個別にテストしやすくする狙い。再ソートが 2 回走ることによるコストは、search の結果件数が小さい（上限 limit 件程度）ため無視できる範囲と考える。

**なぜ「全タグが archived」条件か:**

エンティティは複数タグを持つ。「archived タグが 1 つでも付いていたら降格」にすると、`domain:cc-memory` と `domain:orch-legacy` の両方を持つ decision まで降格されてしまう（前者は現役）。「全部 archived」条件なら「本当に退役システムに閉じた記録」だけが降格対象になる。

**係数 0.3 の根拠:**

RRF 正規化スコアは 0.0-1.0 のレンジで、recency_floor が 0.15。3 か月経過相当（recency ≈ 0.34）と同程度の下位帯に落とす。完全に下位に沈めず、明示的にクエリすればトップ N に残る位置、という狙いで置く初期値。

この係数は `final_score` に対する**相対的な**掛け算であり、「archived アイテムは非 archived アイテムより必ず下位に来る」という絶対順位を数式で保証するものではない。クエリマッチ強度や recency が高い archived アイテムが、マッチ強度の低い非 archived アイテムより上位に来ることはありうる。「クエリすればトップ N に残る」という設計意図と、絶対順位保証がないことの両方を踏まえた上で、係数の妥当性は実データでのサンプル走査で確認する（8. 未決事項）。

**結果アイテムの形（変更後）:**

```python
{
    "type": "decision",
    "id": 123,
    "title": "...",
    "score": 0.24,
    "final_score": 0.24,
    "score_breakdown": {"fts": ..., "vec": ..., "tag": ..., "rrf_normalized": 0.8, "recency_factor": 1.0, "archived_factor": 0.3},
    "snippet": "...",
    "tags": ["domain:orch-legacy"],
    "archived": True,
    "archived_tags": ["domain:orch-legacy"]
}
```

### 3.6 check_in・get 系での表示

**check_in は本設計のスコープ外とする。** check_in の内部（`checkin_service.py`）は別セッションで内部リライトの詳細設計が進行中で、既存 `checkin_service.py` は作り直される前提になっている。既存コードの具体的な構造（`_get_pinned_targets`、478-481行等）にフックする増築設計は、リライトで前提が崩れるリスクが高い。

本設計が check_in に対して保証するのは、push 除外の SQL フィルタ（3.4）が `checkin_service.py` 481行の `collect_tag_notes_for_injection` 呼び出しにも自動的に効くという点のみである。これは checkin_service.py の内部構造に依存せず、リライト後も `collect_tag_notes_for_injection` を経由する限り効果が続く。check_in 応答への `archived_tags` フィールド追加・pinned targets への `archived` フラグ付与といった表示層の拡張は、内部リライト側の詳細設計に委ねる（リライト側は既決の check_in 全体予算（10,000 字）を踏まえた設計を行っているため、この設計側で先に予算を消費するフィールドを追加すると、その前提を壊す恐れがある）。

get_topics/get_logs/get_decisions/get_activities/get_by_ids は check_in 内部リライトの対象外の独立した関数であるため、これらは本設計の対象に含める。既存の `tags` フィールドに含まれる archived タグ文字列から、応答トップレベルに `archived_tags` の集約を返す。

**エージェント視点の出力イメージ（get_decisions の例）:**

```
get_decisions 応答（抜粋）
{
  "decisions": [...],
  "archived_tags": [
    {"tag": "domain:orch-legacy", "archived_reason": "旧 orch 体系解体（合意記録参照）"}
  ]
}
```

エージェントに対しては「この応答には domain:orch-legacy 配下（退役システム）のタグが含まれる」という事実が明示される。積極的に引き上げる意思決定はエージェント側で行う。

**タグ自体を操作・照会する経路（search_tags/analyze_tags）:** `search_tags`（`include_notes=True` 時）は archived タグの notes もそのまま返す。これは push（自動注入）ではなく明示クエリによる pull であり、2. の「pull 経路は削除・非表示にしない」という前提と整合する。ただし、そのタグが archived であることが分かるよう、`search_tags` と `analyze_tags` の応答に tag 単位の `archived` / `archived_reason` を追加する。

**対象外とする経路（pin/relation 操作）:** `add_pin` / `remove_pin` / `add_relation` / `remove_relation` はタグ一覧を主目的とした応答を返さないため、archived 表示の追加対象には含めない。

### 3.7 初回付与の運用手順

機械判定はしないため、手順は運用ガイド + 対象タグリスト作成の 2 段構えとする。

**実行主体とタイミング:** 定期実行・自動実行の仕組みは持たない。ユーザーが「このタグ群を退役させたい」と判断した任意の通常セッションで、ユーザーがエージェントに依頼し、エージェントが以下の手順を代行する形を基本とする（ユーザー自身が直接 `update_tag` を呼ぶことも妨げない）。設計マージ直後に自動で走る作業ではなく、ユーザー起点で始まる ad hoc な操作である。

1. 退役対象システムを人が特定する（既存の decision 記録・合意履歴から）
2. 対象タグの一覧を SQL で抽出（例: `SELECT namespace, name FROM tags WHERE (namespace='domain' AND name IN (...)) OR name LIKE 'orch-%'`）し、目視確認
3. `update_tag(tag='domain:orch-legacy', archived=True, archived_reason='<合意記録の要約>')` を各タグに対して呼ぶ
4. 合意記録側（decision）に「このタグ群を archived にする合意」を残し、archived_reason にその合意の要約を短く書く

エージェント側から実行する場合も同じ流れ。「勝手に archived にしていいか」の判断はエージェント側では行わず、ユーザーとの合意経由に限定する運用にする（この運用制約はコード側で強制しない。人間の判断に委ねる）。

---

## 4. 変更ファイル一覧

- `migrations/0058_add_tag_archived.sql`: 新規。`tags.archived_at` と `tags.archived_reason` 列追加、`idx_tags_archived_at` 部分インデックス追加
- `src/services/tag_service.py`:
  - `update_tag` に `archived` / `archived_reason` 引数追加、相互排他バリデーション更新、archived 更新分岐実装、冪等判定、canonical との整合バリデーション
  - `collect_tag_notes_for_injection` の SELECT に `AND archived_at IS NULL` を追加
  - archived 状態を取得するヘルパー `get_archived_tags_by_entity(conn, entity_type, entity_id) -> list[dict]` を追加（get 系から利用）
- `src/main.py`:
  - `update_tag` MCP tool 定義に `archived` / `archived_reason` 引数追加、docstring 更新
  - `search` / `get_topics` / `get_logs` / `get_decisions` / `get_activities` / `get_by_ids` の応答に `archived_tags` トップレベル集約を追加するヘルパー呼び出しを追加
  - `search_tags` / `analyze_tags` の応答に tag 単位の `archived` / `archived_reason` を追加
- `src/services/search_service.py`:
  - `_apply_archived_demotion` を新設、`_rrf_merge` + `_apply_recency_boost` の直後に呼ぶ
  - 結果アイテムに `archived` / `archived_tags` / `score_breakdown.archived_factor` を付与
- `src/services/checkin_service.py`: 変更なし。`collect_tag_notes_for_injection` 経由の push 除外が自動的に効く（3.6 参照。archived 表示層の拡張は内部リライト側の詳細設計に委ねる）
- `src/config.py`: `ARCHIVED_DEMOTION_FACTOR = 0.3` を追加
- `tests/unit/services/test_tag_service.py`: `update_tag(archived=True/False)` の冪等・相互排他・canonical 整合、`collect_tag_notes_for_injection` の archived 除外
- `tests/unit/services/test_search_service.py`: archived 全タグエンティティの降格、部分 archived の非降格、応答フィールドの存在
- `tests/unit/services/test_checkin_service.py`: 新規テストは追加しない。既存の `collect_tag_notes_for_injection` 経由の除外は `test_tag_service.py` 側でカバーする
- `tests/integration/test_archived_flow.py`: 新規。archived → 注入されない → 解除 → 注入復活のフルサイクル

---

## 5. Edge cases

- **archived タグを新規エンティティに付与しようとしたとき:** 許容する。エンティティ側は「退役システムに関する記録」として作られる可能性が普通にある（例: 過去記録の整理、archived になった経緯の記録自体）。tag notes 注入と search 降格は archived 状態がその瞬間の SELECT で判定されるため、付与後も一貫して機械除外・降格が効く。ワーニングも出さない。
- **archived タグ配下の decision が pull_precedents / 判例検索に出るとき:** precedent_pull は topic routing 経由でタグ横断的に decision を集めるため（`src/services/precedent_pull_service.py` の設計）、archived タグの decision も browse 保証で応答に載る。pull_precedents は search と異なりスコアベースの降格ロジックを持たない（topic routing による集合取得であり RRF ランキングを経由しない）ため、archived による順位への影響はない。各 decision item に `archived_tags` を付与し、archived 配下だと明示するラベル付けのみを行う。呼び出し側（エージェント）が「参考にはするが現行方針としては採用しない」を判断できる材料として置く。retract 済み decision は既に除外されているのに合わせ、archived は「除外ではなくラベル付き」で載せる。応答をトップレベルでも集約するか item 単位に閉じるかは未決事項（8. 参照）。
- **archived と非 archived タグの併存エンティティ:** search では降格対象外（3.5 に記述）。tag notes 注入は archived タグの分だけ SELECT で落ちるため、非 archived タグの notes は通常通り注入される。get 系の `archived_tags` フィールドには archived 側だけが列挙される。
- **archived タグの解除:** `update_tag(archived=False)` で `archived_at = NULL`, `archived_reason = NULL`。notes 注入は `_injected_tags` セッション別マーカーの状態に依存する（3.4 参照）。そのタグが解除前の同セッション内で一度も参照されていなければ、解除後の初回参照で notes が注入される。既に同セッション内で参照済み（マーカー登録済み）の場合は、そのセッション内では notes は注入されず、次のセッションから注入される。この非対称性は既存のセッション別マーカー仕様に起因し、archived 固有の追加処理は行わない。
- **同一タグへの archived の連続適用:** 冪等。既に archived の状態で `archived=True` が呼ばれても `archived_at` は更新せず `updated: False` を返す。`archived_reason` の後追い更新は現状不可（`update_tag` は 1 プロパティずつ）。理由変更が必要な場合は一度 `archived=False` に戻して `archived=True` で理由付きで再設定する運用（実運用でそうそう起きないと考えられるため専用 API は用意しない）。
- **canonical で紐付いたエイリアス:** canonical 先が archived になった場合、既存の canonical 紐付けは維持されるが、新規の紐付け操作（`update_tag(canonical='archived-tag')`）は拒否する。エイリアス経由での参照は「過去に紐付けた事実」として残す。エイリアス元は archived にしても意味が薄いが技術的には可能（挙動は「エイリアス元は元々 canonical 側に転送されて実質使われない」ので archived の効果はほぼない）。
- **rename との相互作用:** archived タグを rename しても archived 状態は tag_id ベースで維持される（既存 rename は tag_id を保持したまま namespace/name のみ更新するため、`archived_at` 列は自動的にそのまま残る）。特別な処理は不要。
- **タグの物理削除トリガー:** `migrations/0039_extend_tag_namespace.sql` 56-62行の `trg_pins_cascade_delete_tag` があるが、tags 行が消えれば `archived_at` 列も一緒に消える。削除経路は既存挙動に任せる（archived と delete は別概念で、削除は完全消去、archived は下位表示）。

---

## 6. Verification

この設計が保証する振る舞いを、以下の観点で確認できる。

- **push 除外の全経路カバー:**
  - archived タグに notes を持たせた状態で `add_topic` / `add_logs` / `add_decisions` / `get_*` / `search` / `pull_precedents` / `add_activity` / `check_in` を呼び、応答 dict に `tag_notes` が含まれない（または archived 分が含まれない）ことを確認
  - `collect_tag_notes_for_injection` の SELECT 実行前に `_injected_tags` へ登録される実装（3.4 参照）を踏まえ、「archived タグを一度も参照していない新規セッションでは archived タグは `_injected_tags` に登録されない」ことを確認（登録は SELECT 対象になった時点、かつ notes の有無や archived かどうかに関わらず行われるため、参照済みかどうかで場合分けする）
- **push 除外の SQL 単体:**
  - `collect_tag_notes_for_injection` に archived / 非 archived 両方が入った tag_strings を渡し、archived 分だけ返らないことをユニットテスト
- **notes-SELECT 経路の既知一覧確認:**
  - `grep -rn "SELECT.*notes.*FROM tags\|FROM tags.*notes"` で `tags.notes` を読む SELECT 箇所を列挙し、応答注入用途のものが `collect_tag_notes_for_injection` 以外に増えていないことを確認（`hint_service.py` の抑制マーカー判定用途 2 箇所は許可リストとして明示）
- **pull 側降格:**
  - 全タグ archived の decision と、archived タグ + 現役タグを持つ decision を用意し、search 応答で前者だけが `archived: True` + `archived_factor: 0.3` になり、後者は係数 1.0 のまま
  - final_score が降格係数を掛けた値で再ソートされていること
- **check_in への自動除外の波及:**
  - activity に archived タグ（notes 付き）を付けて check_in を呼び、応答の `tag_notes` から archived 分が消えていること（`archived_tags` 等の新規フィールドは本設計では追加しないため確認対象外。3.6 参照）
- **`update_tag(archived=...)` の冪等・相互排他:**
  - 連続 True/False 呼び出しで `updated: False` が返る
  - `notes` と `archived` の同時指定で `CONFLICTING_PARAMS` エラー
  - `archived_reason` 単独指定で `ORPHAN_ARCHIVED_REASON` エラー
  - `archived_reason` は `archived=False` 時に自動 NULL 化
- **canonical との整合:**
  - archived タグを canonical 先に指定するとエラー（`ARCHIVED_CANONICAL_INVALID`）
- **解除後の復帰:**
  - archived タグを、そのタグを一度も参照していないセッションで解除した場合、当該セッション内の初回参照で tag notes が注入される
  - 解除前の同セッション内で既に参照済みだったタグは、そのセッションでは注入されず、次のセッションから注入されることを確認する
  - search 応答で `archived: True` フラグと `archived_factor` が消える

---

## 7. 依存関係と実装順序

以下の順で載せる。前段が後段の前提。

1. `migrations/0058_add_tag_archived.sql` 追加、DB スキーマ変更を確定
2. `tag_service.update_tag` の archived 分岐と `collect_tag_notes_for_injection` の SELECT 変更（push 除外の中核。この段階で push 経路は保証される）
3. `src/main.py` の `update_tag` MCP tool 定義更新（外部から archived を操作できる状態に）
4. `search_service` の降格ロジック追加（pull 側降格）
5. get 系（get_topics/get_logs/get_decisions/get_activities/get_by_ids/search_tags/analyze_tags）の `archived_tags`・`archived` フィールド追加（応答での明示。check_in は含まない。3.6 参照）
6. 対象タグへの初回付与（3.7 参照。設計マージ後、ユーザーが必要と判断した任意のセッションでユーザー起点により実施。定期実行・自動実行はしない）

隣接する未マージ作業との関係:

- SessionStart 予算化ブランチ（隣接セッションで進行、未マージ）: SessionStart hook 側でも tag notes を扱う経路がある場合、そちら側の変更で `collect_tag_notes_for_injection` を新規に呼ぶことがあれば同じチョークで自動除外される。呼び出し追加は既存構造の内側に閉じるため衝突しない。
- check_in の内部リライト（別セッションで詳細設計進行中、既存 `checkin_service.py` は作り直される前提）: 本設計は check_in の応答 dict には触れない（3.6 参照）。push 除外のみが `collect_tag_notes_for_injection` 経由で自動的に効き、内部リライト後の実装がこの関数を経由する限りその効果は保たれる。check_in 応答での archived 表示（`archived_tags` フィールド等）は内部リライト側の詳細設計に委ねる。

---

## 8. 未決事項

実装時に決める点。

- `ARCHIVED_DEMOTION_FACTOR` の初期値: 0.3 で置くが、実データで sample 走査を行い、下位帯が「引きたいときにトップ N に残る位置」に落ちるか確認して調整する。0.15（recency floor と同じ）まで下げるか、0.5 で緩めるかは実測次第。
- `archived_reason` の長さ制約: 100 文字未満想定だが CHECK 制約は付けない。将来的に長文が入るリスクは低いと判断（人が短くまとめる用途）。運用で発散したら CHECK を後付けする。
- `pull_precedents` の応答での archived 明示: 各 decision item に `archived_tags` を付ける方針（Edge cases に記述）だが、precedent の全体応答トップレベルにも集約するか、item ごとに閉じるかは実装時判断。集約する方が呼び出し側が扱いやすいと考えられる。
- check_in の coverage・pinned targets 等での archived 区分表示: 本設計は check_in の応答には触れない（3.6 参照）ため対象外。check_in 内部リライト側の詳細設計で扱うかどうかは、そちらの設計判断に委ねる。
- 監査観点: 「誰が archived にしたか」を残すか。現時点では `archived_reason` の中に「合意記録の要約」を書く運用で十分と考える。監査ログ的な `archived_by_session` 列を追加する必要はまだ低い。
- embedding / FTS 検索での archived 事前除外: 3.5 の降格はスコアリング後に適用するため、検索コスト自体は archived 分も含めて発生する。事前除外（クエリ時点で `archived_at IS NULL` を JOIN 条件に加える等）に倒すかどうかはコスト実測次第で、現時点では降格方式を採用する。
