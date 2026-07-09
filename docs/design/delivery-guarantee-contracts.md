status: 詳細設計ドラフト（レビュー用）

要旨: 「載せる集合」と「載せた集合」の差分を必ず機械的に明示する配送契約を、cc-memory の主要な文脈注入サーフェス（check_in と SessionStart）に導入する。契約は complete / selected_with_remainder / truncated_with_count の3種に統一し、切り詰め時のみサーバー側で自動付与される `_budget` エンベロープと、機械算出された欠損件数の文言を通じて「黙った切り詰め」を構造的に禁止する。予算超過は棚卸しの一次シグナルとして signal_events に集約し、既存の hint 経路経由でユーザーへ痩身作業を促す。初期対象は「activity 配下の pin 全件 × check_in」と「進行中 activity 全件 × SessionStart」の2面。前者は check_in 内部リライトの要件仕様として、後者は隣接する SessionStart 圧縮コンポジタの実装として着地する。


## 1. 背景と目的

決定論は「気づく・読む・活かす」といった認知には触れられないが、「DB に何が入っているか」「レスポンスに何が載るか」「カウンタがいくつか」「行動が発生したか」といった状態は機構で保証できる。したがって「思い出させる」ことは保証しない。保証するのは「確実に載せる」「確実に数える」「載らなかったものを確実に明示する」の3点である。

現状は要素ごとに縮退の作法が食い違っている。`pull_precedents` は本文予算縮退を `budget` オブジェクトと `truncated: bool` で明示する（`src/services/precedent_pull_service.py:418`）。check_in は decisions/materials/logs について `"N/M"` 形式の `coverage` を返すが（`src/services/checkin_service.py:528`）、pinned targets はこの coverage の外側に置かれ、pin 側には分母表現が一切ない。SessionStart hook の 4 階層ダッシュボード（`hooks/session_start_hook.py:117`）は「進行中 activity 全件」を集める前提だが、`_TIER2_MAX_ITEMS = 5` の切り詰めと 4 階層それぞれの選抜規則が積み重なるため、集合全体との差分は表示上どこにも出ない。「表に出ていないだけ」の切り詰めが構造的に許容されている状態である。

この設計は上記の非対称を「配送契約」という共通概念で正す。契約は「サーフェス × 宣言された集合（分母） × guarantee 種別 × 欠損の表示形式 × 超過時挙動」の5要素を持ち、サーフェスごとに1行の契約表として明文化する。契約に反する応答（黙った切り詰め）が起きたら、それはテストで検出できる不具合として扱う。


## 2. 確定済みの制約

- 新規の常駐ストアを作らない。in-flight 注入系のキーワード照合ストアは凍結。計測は既存の signal_events（もしくは既存 telemetry 系テーブル）に統合する
- check_in 全体の予算は 10,000 文字を上限とする。切り詰めが発生したときのみ `_budget` エンベロープを付与する
- budget_service は allocate_decision_budget / count_entities_for_topics / BUDGET_DEFAULTS を公開しており（`src/services/budget_service.py`）、check_in 内部リライトはここに配送契約サポートを追加する
- 初期対象は2面: pin × check_in、進行中 activity × SessionStart。対応待ち signal 等の追加は初期2面の運用を見てから
- 対象集合の追加や選抜規則の変更は「集合＝分母」を変えない。表示規則は分母に影響してはならない
- SessionStart 側は隣接する採用済み設計（未マージの予算化ブランチと、その後段のコンポジタ）が実装を担う。本設計は仕様の擦り合わせのみ行う
- 現行 checkin_service への逐次増築はしない。pin 全件保証は check_in 内部リライト（別途詳細設計進行中）への要件仕様として書く


## 3. 設計（How）

### 3.1 配送契約の3種類

各サーフェスの各集合は、以下いずれか1つの guarantee 種別を宣言する。

- **complete**: 分母の全件を本文つきでレスポンスに載せる。超過はあり得ない（分母が予算の中に必ず収まる想定の集合）。載らなかった件数・本文を落とした件数は常に 0。テストは「集合サイズ ≦ 実測サイズ」を検証する
- **selected_with_remainder**: 分母のうち選抜規則で選んだ上位 N 件を載せ、残りを件数で明示する。「表示 N 件」＋「未表示 M 件」の2つを機械算出する。テストは `total == 表示件数 + 未表示件数` を検証する
- **truncated_with_count**: 対象の「存在」（id・タイトル等の索引情報）は全件載せるが、本文は予算超過時に配分順で落とし、落ちた件数を明示する。落ちた側は id とタイトルのみのインデックスとして残る（本文だけを落とす。索引情報自体は落とさない）。テストは `total == full件数 + index_only件数` を検証する

complete と truncated_with_count はどちらも「対象の存在は全件保証する」点で共通するが、complete は本文も含めて欠損 0 を約束するのに対し、truncated_with_count は本文のみが欠損しうる。ある集合が「存在は全件返すが本文は落ちうる」性質を持つなら、その集合の guarantee 種別は complete ではなく truncated_with_count を選ぶ。

3種の使い分け:
- 集合サイズが予算に対して十分小さいことが分かっているものは complete
- 予め上限件数を切って表示側の負担を抑えたいが、「切ったこと」を利用者に気付かせる必要があるものは selected_with_remainder
- 本文が可変長で予算超過が起こりうるものは truncated_with_count

### 3.2 配送契約表 v1

現状（before）と本設計後（after）を1つの表に併記する。after 行が本設計で明文化される契約である。

| surface | 集合（分母） | guarantee 種別 | 欠損の表示形式 | 超過時挙動 |
| --- | --- | --- | --- | --- |
| check_in / recent_decisions（before） | 関連 topic 群の非 retract decision 全件 | truncated_with_count 相当 | `"N/M"` 文字列（coverage.decisions） | サイレント切り詰め（上限 15、`DECISIONS_FULL_LIMIT`） |
| check_in / recent_decisions（after） | 同上 | truncated_with_count | `coverage.decisions = "N/M"` を維持し、切り詰め時のみ `_budget.omitted.decisions = M - N` を追加 | 予算超過を signal_events に記録し（kind は3.7節参照）、hint 経由で棚卸しを促す |
| check_in / materials（before） | activity と material の直接 relation 全件 | selected_with_remainder 相当 | `coverage.materials = "N/M"` のみ | 現状は選抜上限を持たない（全件返し） |
| check_in / materials（after） | 同上 | complete（初期）／将来的に selected_with_remainder | `coverage.materials = "N/M"`。complete 契約下では常に N == M | 全体予算 10,000 字を超えたときのみ selected_with_remainder に降格し `_budget` を出す |
| check_in / logs（before） | 関連 topic 群の非 retract log 全件 | selected_with_remainder 相当 | `coverage.logs = "1/M"`（latest_log があれば 1、他はカタログ） | サイレント（latest_log 以外は本文なしのカタログ） |
| check_in / logs（after） | 同上 | selected_with_remainder | 同上 + カタログ件数を `_budget.omitted.logs_content = M - 1` として切り詰め時のみ明示 | 全体予算超過時も latest_log は必ず1件残す |
| check_in / pinned（before） | 該当 activity（と自身の tag）に紐づく pin 全件 | 未宣言 | なし（coverage に含まれない、`checkin_service.py:518` コメントに「pins 注入 targets は含めない」と明記） | サイレント（0件キー省略で分母不可視） |
| check_in / pinned（after） | 同上 | truncated_with_count | `coverage.pinned = "N/M"`。分母は5種（decisions/logs/materials/topics/activities）合計。N は常に M と一致（存在は全件返す）。本文を落として index_only に降格した件数は `_budget.omitted.pinned = {type: 件数}` として機械算出 | 予算超過を signal_events に記録し、pin 棚卸し hint を発火 |
| SessionStart / activities（before） | 進行中 activity（in_progress + pending、pinned + tag ホット群経由で収集） | 未宣言 | なし（`_TIER2_MAX_ITEMS = 5` などの選抜は全て表示規則。分母不可視。加えて updated_at 30日超・非pinned の activity は現行実装で全階層から意図的に除外される） | サイレント |
| SessionStart / activities（after） | 進行中 activity 全件（in_progress + pending、`orch_managed=1` を除く） | selected_with_remainder | セクション末尾に「未表示 N 件」句を機械算出で追加。現行の 30日超・非pinned 除外分（隣接実装の階層3・4統計行はこの脱落分を数える項目を持たない）も未表示 N に含める必要があり、これは本設計から隣接実装への追加要件とする（3.5節参照） | 分母 - 表示数を signal_events に記録 |
| pull_precedents / topics.decisions（変更なし） | 選択 topic の非 retract decision 全件 | truncated_with_count | `budget = {limit, used, full, index_only}` + `truncated: bool` | 既に契約表準拠。呼称のみ揃える |

`_budget` エンベロープは check_in と将来の集約系ツールが使う新しい共通形式で、`budget`（アンダースコアなし）は `pull_precedents` の既存フィールド名を保持する。呼称を分けるのは、`pull_precedents` の `budget` が「本文予算 vs 使用量」のメーターであり、`_budget` は「切り詰め発生時のみ生えるエンベロープ」で意味が異なるため。

### 3.3 `_budget` エンベロープの仕様

check_in および将来の集約系ツールのレスポンス最上位に、切り詰めが1件でも発生したときのみ挿入される。切り詰めが無い場合はキー自体が存在しない（コンテキスト消費ゼロ）。

```
{
  "_budget": {
    "limit_chars": 10000,
    "used_chars": 9812,
    "surface": "check_in",
    "omitted": {
      "decisions": 3,
      "pinned": {"materials": 2, "logs": 1},
      "logs_content": 4
    },
    "reason": "budget_exceeded"
  }
}
```

- `limit_chars` / `used_chars` は byte ではなく文字数（既存 budget_service の慣習に合わせる）
- `omitted` の値はすべて機械算出でなければならない。手書きの概算・「多数」等の非数値はここに入らない
- `omitted` のキーは配送契約表の集合名に一致する。`pinned` のみ target_type 別内訳を持つ（欠損対象が5種混在するため、ユーザーが痩身対象を選ぶ手掛かりが必要）
- `reason` の初期値は `"budget_exceeded"`。将来 selected_with_remainder の上限件数超過なども入りうる（`"selection_capped"` 等、初期値は1種のみ）
- `_budget` のアンダースコア接頭辞は「サーバー側が自動付与する制御エンベロープ」であることを示す

### 3.4 check_in 側 pin 全件保証の要件仕様

現行 checkin_service.py には配送契約の実装を足さない。以下は check_in 内部リライトが満たすべき要件である。

- `_get_pinned_targets` に相当する取得ロジックは、pin 全件を1回のクエリで取得する（現行と同じ「activity 自身の tag と activity 自身」を source としたユニオン）。0件時は空 dict、そうでなければ target_type 別の dict を返す
- coverage に `pinned = "N/M"` を追加する。ここで M は取得された distinct 件数（=分母）。N は常に M と一致させる（pin の「存在」は index_only 込みで全件返す。truncated_with_count 契約は本文の欠損のみを許容し、存在自体の欠損は許容しない）
- 予算配分は「decision 本文 → material 本文 → log 本文 → pinned 本文」の優先順で行う。pin は content 依存が薄い（title のみでも意味が通る）ため、本文予算の末尾に配置される。それでも予算超過時は本文を落として index_only（id + title のみ）に降格する
- pin の存在が全件載ったことと、本文の full/index_only 内訳は2つの invariant で機械検証する: `len(result["pinned"]) == count_pinned(activity_id)`（存在の complete 性）、および `full件数 + index_only件数 == count_pinned(activity_id)`（truncated_with_count の total invariant）。どちらもテストで固定する
- 切り詰めが発生したら、上記 `_budget.omitted.pinned` に target_type 別内訳を書く。同時に signal_events に記録する（kind の選定は3.7節参照）。summary は活動単位で固定される文言（超過件数などの可変値を含まない）とし、可変値は `context` 側に持たせる（理由は3.7節）
- 現行実装のコメント「pins注入targetsは含めない」（`src/services/checkin_service.py:518`）は撤去対象。pin 側にも分母を持たせるのが本設計の要件である

### 3.5 SessionStart 側の擦り合わせ（隣接設計への要件）

隣接する未マージの予算化ブランチが導入する注入コンポジタは、各 section 宣言に guarantee 種別を持たせる。本設計はそのコンポジタが以下の4点を満たしていることをレビュー時に確認する。

- activities section の分母は「進行中 activity 全件」（`orch_managed=1` を除く）。現行の 4 階層構造は表示規則であって分母を変えない
- 「未表示 N 件」句は compose() が機械算出する。手書き概算は契約違反として reject する
- 現行実装は updated_at 30日超・非pinned の activity を全階層から除外する仕様を持つ（回帰テストで固定済み）。同ブランチの階層3・4統計行はこの除外分を数える項目を持たないため、本設計はこの脱落分も「未表示 N 件」に合算する集計項目を追加することを要件とする
- 予算超過イベントは compose() 内で signal_events に記録される。kind・summary・fingerprint の作り方は3.7節の規約（可変値を summary に含めない等）に合わせる

擦り合わせが崩れると本設計の SessionStart 行が空手形になるため、コンポジタ側のテストで上記4点を固定する（項目のみ本設計に列挙し、実装は隣接側に委譲）。

### 3.6 欠損明示の文言仕様

- 数値は必ず機械算出する。手書き・概算は禁止する
- 数値の粒度: 集合単位の件数（例: `omitted.decisions = 3`）。バイト数や字数の欠損量は原則出さない（`_budget.used_chars` と `limit_chars` の差分で再構成可能なため）
- テキスト表示に落とすときの雛形（3.5節の compose() が SessionStart の markdown セクション末尾に機械出力する固定フォーマット。agent が手で埋めるプロースではない）:
  - 「進行中 activity: 表示 5 件 / 未表示 12 件」
  - 「pin: 表示 8 件 / 未表示 2 件（material 2 件を本文なしで表示）」
- 「多数」「多め」「一部」等の非数値表現は使わない。表示件数が 0 の場合も「0 件」と数値で書く

### 3.7 超過 → 棚卸しトリガー

「新規ストアを作らない」制約下で、超過イベントの記録先を既存の signal_events に集約する。

- kind は既存の `"friction"` を再利用しない。中盤文脈配送の3層運用における `friction` は「自己申告による文脈飢餓」を表す kind としてすでに運用開始が決まっており、本設計の「予算超過による機械算出の欠損」と意味が異なる。同一 kind に自己申告と機械検出が同居すると fingerprint dedup と occurrence_count の集計が意味を跨いで混ざるため、本設計専用の新 kind `budget_overflow` を `KNOWN_KINDS`（`src/services/signal_service.py`）に追加する。この追加は Python 定数への追記のみで migration は不要
- `source = "tool:check_in"` / `"hook:session_start"` 等、既存の source 命名規則に合わせる
- `summary` は活動・サーフェス・集合の組で固定される文言のテンプレ埋めとし、超過件数など連続超過のたびに変わりうる可変値を含めない: `"{surface} budget overflow in {set_name} (activity {activity_id})"`。可変値（omitted 件数等）は `summary` ではなく `context` にのみ持たせる。`_compute_fingerprint` は `summary` を正規化してハッシュ化するため、可変値を summary に混ぜると超過のたびに fingerprint が変わり dedup が効かなくなる。固定文言にすることで初めて「同一活動×同一サーフェス×同一集合の連続超過は1行にまとまり `occurrence_count` が積み上がる」が成立する。activity_id を summary に含めるのは、複数 activity の超過が1行に集約されて活動別集計ができなくなることを防ぐため
- `context` に `{"surface": ..., "set_name": ..., "activity_id": ..., "limit_chars": ..., "used_chars": ..., "omitted_count": ...}` を JSON で入れる。これで後段のトリアージが機械可読になる
- signal_events への記録が失敗した場合（DB エラー等）は、`_budget` エンベロープを含む check_in 本体レスポンスには影響させない。既存の `capture_signal_safe`（例外を握りつぶし stderr のみに出す捕捉専用経路）を使い、記録失敗はレスポンス全体の失敗にしない

棚卸しへの導線は既存 hint 経路を再利用する。

- `hint_service` に新 kind `contract_overflow` を追加する（`HintType` の Literal 定義、`src/services/hint_service.py:46-53` 周辺に追加）。集合名ごとに hint kind を分けると decisions/materials/logs/activities へ対象を広げるたびに kind が増殖するため、汎用 kind 1つ + payload の `set_name` で表現する。生成条件は「該当 activity について直近30日間の `budget_overflow` × `set_name = "pinned"` の occurrence_count 合計が閾値 K 以上」。初期値は K=3 とし、運用開始後の signal 発生頻度を見て調整する（閾値の最終確定は未決事項として8節に置くが、K が定まらない限り hint 自体が発火しないため、運用開始前に暫定値での着手を必須とする）
- `delivery_hint = "immediate"` にして check_in 応答の `hints` に載せる。既存の recompose_bootstrap と同経路（`src/services/checkin_service.py:551` 付近の `_get_recompose_hints` 呼び出し）
- 棚卸し作業自体（pin の削除等）は本設計の範囲外とする。hint はユーザーまたはエージェントに気づきを渡すところまでを担い、実行は既存の `remove_pin` 等の既存ツールを呼ぶ形を想定する。hint を無視し続けた場合の escalation は本設計では持たない
- SessionStart 側の hint は隣接設計側で足す（本設計では要件のみ）

新規計測の記録先については、確定済みの中盤文脈配送3層運用の中で「新規計測は injection_telemetry に統合する」という方針が別途決まっている。ただしその方針が対象とするのは3層運用③（記録＝クエリ添付の提示・取得追随カウンタ）であり、本設計が扱う「予算超過による欠損」の計測が同じ統合対象に含まれるかどうかは一次資料から明確に判断できない。この論点は8節の未決事項に置く。

### 3.8 契約の検証可能性

「黙った切り詰めが起きていない」ことは以下の invariant で機械検証する。テストは integration 層で書く（DB 触るため）。

- **check_in**: `sum(coverage 各集合の分子) + sum(_budget.omitted 各集合) == sum(coverage 各集合の分母)`。`_budget` が無い場合は分子と分母が全集合で一致していることを検証
- **check_in / pinned**: pin 全件を返すクエリを別途走らせ、レスポンスの `pinned` 各リスト長（存在の complete 性）が pin 総数（retract 除外後の有効件数、5節参照）に一致することを検証。加えて `pinned` 内の full 件数 + `_budget.omitted.pinned` の総和が同じ pin 総数に一致すること（truncated_with_count の total invariant）を検証
- **SessionStart / activities**: hookSpecificOutput.additionalContext の markdown を parse し、各階層の表示件数合計 + 「未表示 N 件」の N が、進行中 activity 全件（`orch_managed=1` 除外、pending 含む）の総数と一致することを検証。「未表示 N 件」には updated_at 30日超・非pinned で全階層から脱落した分も含まれていることを別途確認する
- **signal 側**: 意図的に予算超過を起こす fixture を用意し、`budget_overflow` 行が期待の source/set_name/context で1件生えることを検証

複数集合を同時に切り詰める場合の合算検証も入れる: 予算をわざと絞って decisions と pinned を同時に溢れさせ、`_budget.omitted` に両方のキーが立ち、両方の invariant を満たすことを1テストで確認する。


## 4. 変更ファイル一覧

- `src/services/checkin_service.py`（別途進行中の内部リライトが生成する成果物ファイル） — 内部リライトが本設計の要件（pin 全件保証・`_budget` エンベロープ・signal_events への記録）を実装する。現行ファイルへの逐次パッチとしては変更しない
- `src/services/budget_service.py` — `_budget` エンベロープを構築するヘルパー `build_budget_envelope(surface, limit_chars, used_chars, omitted: dict, reason: str) -> dict` を追加する。既存 API との整合を保つため、既存の allocate/count 関数は触らない
- `src/services/signal_service.py` — `KNOWN_KINDS` に新 kind `budget_overflow` を追加する（Python 定数への追記のみ）。`record_signal` 本体のロジック変更は不要
- `src/services/hint_service.py` — `HintType` に新 kind `contract_overflow` を追加。閾値定数（初期値 K=3、8節参照）を追加。判定関数を書く
- `hooks/session_start_hook.py` — 隣接設計側で置き換わるため本設計では触らない
- `src/main.py` — check_in ツールの docstring に `_budget` エンベロープと `coverage.pinned` の追加を反映する
- `docs/spec/mcp-tools.md` / `docs/spec-v0.md` — check_in と SessionStart の配送契約を上記契約表 v1 の形で追記する
- `tests/integration/test_checkin_service.py` — 3.8 節の invariant テストを追加する
- `tests/unit/test_budget_service.py` — 新規または既存に `build_budget_envelope` のテストを追加する
- `tests/unit/test_hint_service.py` — `contract_overflow` hint の生成条件テストを追加する

migration 新設は不要。理由は個別に確認済み: hint は DB テーブルを持たない動的計算のみのサービスであり `kind` に相当する制約は Python 側の `HintType` Literal で管理される。signal_events の `kind` も DB 制約ではなく `KNOWN_KINDS`（Python 定数）で検証される設計であり、`context` 列はフリーフォーム JSON で schema 制約を持たない。したがってどちらの追加も migration を要しない。config への追加は閾値定数のみ。


## 5. Edge cases

- **pin が予算の大半を食う**: pin は本文予算の末尾に置かれるため、decision/material/log の本文が全部載っても pin が予算超過することはある。この場合 pin 側だけ `_budget.omitted.pinned` が立ち、他集合は omitted に出ない。この振る舞いはテストで固定する
- **分母ゼロ**: coverage は `"0/0"` と表示する。「N/A」ではなく「0/0」に統一（既存の `f"{len(x)}/{total_x}"` と同じ形）。`_budget.omitted` には該当集合キーを出さない（欠損 0 と欠損対象なしを区別しない）
- **retracted 済みの pin target**: 現行 `_get_pinned_targets` は decision/log/material について `retracted_at IS NULL` でフィルタする（`src/services/checkin_service.py:275`）。`coverage.pinned` の分母 M は「有効な pin 件数」と定義し、pins テーブル上の生の総数から retract 済み分を予め除外して数える（数式を検証テストで固定）。これにより retract は切り詰めとして扱われず signal を発火しない。M をこう定義する理由は、pin 総数（retract 込み）を分母にすると常に N < M となり、complete / truncated_with_count が保証する「欠損 0」または「本文以外は欠損しない」という invariant が恒常的に破れた状態になってしまうため
- **同一 target が複数 source から pin されている**: 現行実装は `(target_type, target_id)` で DISTINCT 化しており、M も DISTINCT 後の数を使う。テストで固定
- **target_type='tag' の pin**: 現行実装は無視する（`src/services/checkin_service.py:207`）。本設計でも分母から除外する。将来 tag pin を表示対象にするなら本設計の分母定義も追随して改訂
- **SessionStart 側の実装が2段になる間の暫定状態**: 「未表示 N 件」句の機械算出だけが先行 PR で入り、guarantee 宣言の正式レジストリ化が後段コンポジタで入る間、配送契約表 v1 の SessionStart 行は「N 件表示規則あり、契約宣言未実装」の中間状態になる。この間の一次意思決定として、暫定期間中は `_budget` 相当のエンベロープを付与しない（「未表示 N 件」句のみを先行させ、`_budget` はコンポジタ側の正式実装まで待つ）。signal 発火も同様にコンポジタ PR まで見送る。ドキュメント（`docs/spec/mcp-tools.md`）には両方の状態を注記する
- **activity に紐づく decision の topic 経由重複**: 複数 topic に belongs_to する decision は現行 `_get_decisions_from_topics` で DISTINCT 化される。分母もこの DISTINCT 後の値を使う。`count_entities_for_topics` は既に DISTINCT 済みで整合が取れている（`src/services/budget_service.py:93`）
- **check_in と別セッションの並行実行**: pin の追加・削除は別セッションが行いうる。分母 M は check_in 開始時点のスナップショットに固定する。pin テーブルへの参照から各 target の content fetch（decision/material/log）までは複数クエリにまたがるため、単一 SELECT の原子性だけでは snapshot の一貫性を担保できない。実装は `BEGIN IMMEDIATE` 等で明示的なトランザクション境界を張り、その内側で pin 取得から content fetch までを完結させることを要件とする。同一 check_in 内で M が変動しないことをテストで固定するが、これは通常経路の固定であり、真の並行書き込み下での担保はトランザクション境界の実装に依存する


## 6. Verification

配送契約が満たされていることを確認する観点。

- check_in レスポンスに `coverage` が常に存在する（error レスポンスを除く）
- `coverage` の全集合について、分母 == 分子 + `_budget.omitted` に対応する数 が成立する
- 切り詰めが1件も発生しなければ `_budget` キーが存在しない
- 切り詰めが1件でも発生すれば `_budget` キーが存在し、`omitted` のいずれかの値が正の数
- `_budget.omitted` に含まれる件数が全て機械算出（fixture で意図的に発生させた欠損数と厳密一致）
- 切り詰め発生時に signal_events に `budget_overflow` 行が期待の source/context で1件生える
- 同一活動・同一サーフェス・同一集合の切り詰めが連続発生したとき signal_events は dedup され `occurrence_count` が増える（既存 `record_signal` の仕様に依存。summary に可変値を含めないことが前提、3.7節参照）
- `contract_overflow` hint が threshold 超過時のみ生成される
- pin 側の分母は「target_type='tag' 除外・retract 除外・DISTINCT 済み」に一致
- SessionStart 側 activities section の表示件数合計 + 「未表示 N 件」 == 進行中 activity 全件（`orch_managed=1` 除外、pending 含む、30日超・非pinned 脱落分も未表示 N に合算）
- 全 verification は integration テストで自動化。手動での目視確認は補助的な位置付けにとどめる


## 7. 依存関係と実装順序

依存の向きだけを示す（時間見積もりは書かない）。

1. `build_budget_envelope` を budget_service に追加する。単体テスト付き
2. `contract_overflow` hint kind を hint_service に追加する。単体テスト付き
3. check_in 内部リライトが「別途詳細設計進行中」の状態から実装フェーズへ入るタイミングで、本設計の要件（pin 全件保証・`_budget` 付与・`budget_overflow` 記録）を組み込む。integration テストが本設計の invariant を検証する
4. 隣接する SessionStart コンポジタ設計が採用確定済みのため、SessionStart 側 activities セクションの契約は隣接実装が担う。本設計はドキュメント（配送契約表 v1）の擦り合わせのみ責任を持つ
5. `docs/spec/mcp-tools.md` と `docs/spec-v0.md` への配送契約表 v1 の追記は、check_in 側と SessionStart 側の両方が本番に載った後に mainline へ落とす。実装より先にドキュメントだけ入ると仕様と実体の食い違いが発生するため、docs は最後尾

check_in 内部リライトと SessionStart コンポジタの実装順序は疎結合。ただし配送契約の呼称（`_budget` エンベロープの形式、`budget_overflow` の source 命名規則）は両者で揃える必要があるため、順序ではなく規約の共有で整合を取る。


## 8. 未決事項

実装時に決める点。

- **`contract_overflow` hint の閾値 K**: 直近30日間の該当 activity への `budget_overflow × pinned` occurrence_count 合計が K 以上で hint 発火。初期値は 3 で運用開始し、signal 発生頻度を見て調整する。K が未確定のままだと hint 自体が発火しないため、この初期値での着手は運用開始の前提条件とする
- **`_budget.reason` の初期候補セット**: 初期は `"budget_exceeded"` のみ。将来 `"selection_capped"`（selected_with_remainder の上限件数超過）を足す可能性がある
- **check_in 全体予算 10,000 字の内訳**: 集合ごとの内訳（decisions に N%、pinned に M% など）を持たせるか、単一プールから配分順で消費するか。単一プール方式は既存の allocate_decision_budget と同じ配分順（非 superseded → 新しい順）を全集合の統合順で使えるため実装がシンプル。初期は単一プール方式で開始し、集合間の偏りが顕著になった場合のみ内訳導入を再検討
- **`budget_overflow` の source 文字列**: `"tool:check_in"` / `"hook:session_start"` を採用する方向だが、既存の source 命名の粒度と合わせる必要がある。既存 source 値の分布を signal_events から確認して確定する
- **retract 済み pin の扱いの表示**: 分母から除外することは決めたが、「retract により自動整理された pin が N 件ある」ことを coverage の外側の別フィールドで通知するかは未決。過度なノイズを避けるため初期は非表示で開始
- **materials セクションの complete → selected_with_remainder 降格の閾値**: `_budget.used_chars` が limit_chars の何%に達したら降格するか。単一プール方式では降格ではなく配分順による自然な溢れになるため、明示的な閾値は不要かもしれない。単一プール方式の挙動を integration テストで観察して決める
- **`budget_overflow` 計測と injection_telemetry 統合方針の関係**: 中盤文脈配送3層運用で「新規計測は injection_telemetry に統合する」という方針が別途決まっているが、その方針が対象とする範囲（3層運用③の提示・取得追随カウンタ）に本設計の予算超過計測が含まれるかどうかは一次資料から判断できない。含まれるなら記録先を signal_events から injection_telemetry へ寄せる再設計が必要になる
- **`_budget` と `pull_precedents` の `budget` の呼称統合**: 両者は「切り詰め欠損内訳」と「本文予算の使用量」という関係にあり、`budget` オブジェクトの中に `omitted` を生やす形へ将来統合できる余地がある。初期実装では呼称を分けたまま進めるが、`pull_precedents` を契約表に正式に取り込むタイミングで再検討する
- **3層運用③（記録＝クエリ添付の manifest）との guarantee 種別対応**: ③の manifest（top3、予算600字）も切り詰め対象になりうるが、本設計は初期対象2面（pin × check_in、進行中 activity × SessionStart）に限定しており③はスコープ外。③が実装段階に入るときに、どの guarantee 種別に該当するか、`_budget` エンベロープや `budget_overflow` kind を再利用するかを別途整理する
