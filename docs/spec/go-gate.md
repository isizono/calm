# 境界ゲート判定(go-gate)運用マニュアル

対象: `scripts/gate_check.py` / `scripts/gate_check.sh`。

設計案の変更が「事前にコードを読んで確認すべき類」か「事後に拒否権を行使できれば足りる類」かを、
`git diff` から機械的に判定するための検出器の仕様と運用手順をまとめる。

確定した事実は断定形(〜である)、推測・仮説は「〜と考えられる」「〜が望ましい」で書き分ける。

## 現状の実装範囲

現時点でリポジトリに存在するのは検出器本体(`scripts/gate_check.py`、`scripts/gate_check.sh`)、
GO判定パッケージツール(`scripts/go_package.py`)、CI ワークフロー(`.github/workflows/gate.yml`)、
このドキュメントである。以下は未実装である:

- plan.md / task-plan・task-execute skill への組み込み(分類予測欄、パッケージ生成手順)
- 判例 pull 機構(`pull_precedents` ツール)本体。`go_package.py new --pull-json` は
  その応答 JSON を受け取る入力口のみを用意している

検出器は手元で `uv run python3 scripts/gate_check.py --base <ref> --head <ref>` として、
CI では PR ごとに `.github/workflows/gate.yml` から自動で呼び出せる状態にある。
GO判定パッケージツール(`scripts/go_package.py`、`shadow-report` サブコマンド含む)も
手元で単体で呼び出せる。shadow 校正期の運用(下記)は skill 組み込みが揃ってから開始する。

## 3値分類

| 分類 | 意味 |
|---|---|
| `pre_go` | 事前にコードを読んで確認すべき変更。締め領域(ブラスト半径が大きい・revertが困難)に触れている、または判定不能 |
| `gray` | 機械的にどちらとも判定し切れない変更。人間の追加判断(グレー解決)が必要 |
| `post_veto_candidate` | 事後の拒否権行使で足りる変更候補。ブラスト半径が小さく revert が容易 |

`post_veto_candidate` は「デフォルトで進めてよい」ことを機械的に**保証する**分類ではなく、
事後拒否権運用の**候補**であることに注意する。実際にデフォルト進行の対象にするかどうかは
ダイヤル運用(体制側の裁定)に従う。shadow 校正期間中は `post_veto_candidate` であっても
運用上の判断には一切使わない(後述)。

各分類には理由コード(`reason`)が付与される。

| reason | 分類 | 意味 |
|---|---|---|
| `detector_error` | pre_go | git コマンド失敗・diffパース例外・非UTF-8デコード失敗など、判定不能 |
| `self_protection` | pre_go | 検出器自身のパス(下記)への接触 |
| `axis_a_hit` | pre_go | ブラスト半径検出器(migration接触・DDL・公開IF・破壊系・バイナリ)がヒット |
| `policy_pending` | gray | 依存ライブラリ変更(`pyproject.toml` / `uv.lock`)への接触。裁定待ちにつき強制グレー |
| `axis_b_unmet` | gray | ブラスト半径はクリアだが revert 容易性の条件(サイズ・テスト・機械rollback)を満たさない |
| `axis_b_met` | post_veto_candidate | ブラスト半径・revert容易性の両方をクリア |

## 判定規則とフェイルセーフ

評価順序は次の通りで、上から短絡する(`classify()` の実装がそのまま規則である)。

1. **判定不能は事前go**(`detector_error`): git 呼び出し失敗・diffパース例外・非UTF-8デコード
   失敗は握り潰さず `errors` に積み、`pre_go` を返す。検出器はどの経路でも必ず verdict を返し、
   内部バグによる例外だけが非0終了になる
2. **検出器自己保護**(`self_protection`): 以下のパスへの接触は他の結果によらず `pre_go` 固定にする

   ```
   scripts/gate_check.py
   scripts/gate_check.sh
   scripts/go_package.py
   .github/workflows/gate.yml
   tests/unit/test_gate_check.py
   tests/unit/test_go_package.py
   ```

   判定器を触る変更が判定そのものを迂回できないための invariant である
3. **軸Aヒットは事前go**(`axis_a_hit`): migration接触・DDL・公開IF変更・破壊系操作・バイナリ変更の
   いずれかが `counted` ステータスで検出された場合
4. **依存変更は強制グレー**(`policy_pending`): `pyproject.toml` / `uv.lock` への接触は軸Bの結果に
   かかわらず `gray` にする
5. **軸B充足で事後拒否権候補**(`axis_b_met`)、**未充足でグレー**(`axis_b_unmet`)

### 改竄耐性

PR のワークツリーには変更済みの検出器が含まれ得るため、正規の判定は常に **origin/main 版の
検出器**で行う。ローカルは `scripts/gate_check.sh`(origin/main から取り出して実行する)を使う。
`scripts/gate_check.py` を直接叩いた結果は、ブランチ側の改変を反映した参考値でしかない。

CI(`.github/workflows/gate.yml`)も同じ原則で動く。PR の base ブランチから
`git show origin/<base>:scripts/gate_check.py` で検出器を取り出して実行するため、PR 側で
`scripts/gate_check.py` 自体を改変(判定ロジックの無力化など)しても CI の判定には反映されない。
PR がその改変自体を含む場合は、diff 上の `scripts/gate_check.py` 接触が自己保護パスに
ヒットして `pre_go`(`self_protection`)になる。

## CI組み込み(`.github/workflows/gate.yml`)

- トリガー: PR の `opened` / `synchronize` / `reopened`
- 判定は base ブランチ版の検出器で行う(上記の改竄耐性)。base ブランチにまだ検出器が
  存在しない(導入初期)場合は `git show` が失敗し、`pre_go`(`detector_error`)のフォールバック
  verdict でジョブを正常終了させる
- ジョブは **non-blocking** である。verdict の分類がどうであれジョブ自体は成功する。ゲートは
  merge を機械的に阻止する装置ではなく、人間の注意を routing する装置であり、その方針は
  shadow 期間中もshadow期後(本番化後)も変わらない。拒否権の行使主体は常に人間である
- verdict は job summary(`--render` の markdown)と `verdict.json`(artifact)の両方に残る。
  PR へのコメント投稿は行わない(bot ノイズ回避)
- 検出器自体が例外で落ちた場合はジョブ失敗として可視化される(検出器のバグシグナル)

## 軸A: ブラスト半径検出器

| 検出器 | 対象 |
|---|---|
| `migration_touch` | `migrations/` 配下への接触(追加・変更・削除・rename すべて) |
| `ddl_in_code` | コード内の DDL(`CREATE`/`ALTER`/`DROP` + `TABLE`/`INDEX`/`TRIGGER`/`VIEW`。動詞と対象の間に `UNIQUE`/`TEMP`/`TEMPORARY`/`VIRTUAL` 等の修飾語を挟む形も拾う。書き込み `PRAGMA`)。追加行・削除行の両方を走査する |
| `public_if` | 公開IF(`src/main.py`、`src/remote.py`、`src/http_config.py`、`src/services/visibility_middleware.py`、`docs/spec/openapi.yaml`、`hooks/hooks.json`、`marketplace.json`)への接触 |
| `data_destructive` | 追加行の破壊的SQL(`DELETE FROM`/`UPDATE ... SET`/`TRUNCATE`)・破壊的ファイル操作(`shutil.rmtree`/`os.remove`等) |
| `binary_change` | numstat が `-`(バイナリ)を返す変更 |
| `dependency_change` | `pyproject.toml` / `uv.lock` への接触。軸Aヒットではなく強制グレー(`policy_pending`) |

除外規則:

- `tests/` 配下の DDL・破壊的操作は `downgraded_tests` として記録するが、分類のゲーティングには
  数えない(テストは ephemeral DB に対して走り、本番DBへの到達経路が無いため)
- `*.md` と `docs/` 配下はコンテンツ走査の対象外(SQL実行面を持たないため)。ただし
  `docs/spec/openapi.yaml` は `public_if` のパス一致対象であり、これとは独立している
- コメント内の DDL・破壊系キーワード(例: `# ALTER TABLE ...` というコメント追加)は誤検出を
  許容する。誤検出は安全側(pre_go)に倒れるため、正規表現でコメント判定を積むより単純さを
  優先している

`src/main.py` が diff に含まれる場合のみ、公開IFの差分を AST 解析で抽出する(`public_if_delta`)。
これは判定材料の**充実**(人間が読む「何が変わったか」の要約)にのみ使い、分類そのものには
影響しない。parse に失敗した場合は `errors` に積むが、`public_if` のパス一致による `pre_go` 判定
自体はそれとは独立に成立する。

## 軸B: revert容易性

軸Aが全クリア(counted findingsが無い)の場合のみ評価する。3条件のANDで `met` を決める。

- **サイズ**: 本体コード行(`tests/`・`docs/`・`*.md`・`uv.lock` を除外した追加+削除行数)が
  `MAX_LINES`(既定400)以下、かつファイル数(`uv.lock` のみ除外)が `MAX_FILES`(既定15)以下
- **テスト差分**: 変更ファイルに `tests/` 配下が1つ以上含まれる。全変更ファイルが `.md` または
  `docs/` 配下のときは `waived_docs_only` として充足扱いにする
- **機械rollback**: migration非接触・DDL非検出・破壊系非検出・バイナリ変更なしのとき成立する

閾値(`MAX_LINES` / `MAX_FILES`)は初期提案値であり、shadow 期の実測データで校正する前提を置く。

## verdict JSON

`scripts/gate_check.py --base <ref> --head <ref> --format json` の出力例:

```json
{
  "schema_version": 1,
  "detector_sha256": "...",
  "detector_source": "main",
  "repo": "cc-memory",
  "base_ref": "origin/main",
  "merge_base": "<sha>",
  "head": "<sha>",
  "classification": "pre_go",
  "reason": "axis_a_hit",
  "axis_a": { "hit": true, "findings": [ { "detector": "migration_touch", "path": "migrations/0049_x.sql", "lineno": null, "evidence": "status=A", "status": "counted" } ] },
  "axis_b": { "lines_changed": 214, "files_changed": 6, "size_ok": true, "has_tests": true, "mechanical_rollback": true, "met": true },
  "public_if_delta": { "tools_added": [], "tools_removed": [], "params_changed": [], "docstring_changed": [] },
  "ignored_paths": ["uv.lock"],
  "errors": []
}
```

同一 diff 入力に対して出力はバイト同一になる(findings は `(detector, path, lineno)` でソート、
JSON は `sort_keys=True` で出力する)。`--render <verdict.json>` で `axis_a`/`axis_b` を人間可読な
markdown に変換できる。

## CLI

```
uv run python3 scripts/gate_check.py --base origin/main --head HEAD \
    [--repo <path>] [--format json|markdown|both] [--out <path>] \
    [--detector-source main|worktree]

uv run python3 scripts/gate_check.py --render <verdict.json> [--out <path>]

scripts/gate_check.sh --base origin/main --head HEAD   # 正規経路。origin/main 版検出器を使う
```

exit code は verdict を返せた場合は常に0。内部例外(検出器自身のバグ)のみ非0になる。

## shadow 校正期の運用(パッケージツール導入後に開始)

以下は設計時点の運用方針であり、`go_package.py` が揃うまでは実施できない。揃った時点で
この節を実運用の起点とする。

### 何を影判定するか

- 対象は cc-memory リポジトリの main 向け全 PR(例外なし)
- shadow 期間中、検出器の分類は**運用に一切使わない**。全案件を従来通り人間判断で進める。
  検出器は記録だけを積む

### 何と突き合わせるか

- ground truth はユーザー自身が下す分類判断(「コード読みが必要な類か / パッケージだけで
  判断できる類か」を1問だけ聞く)
- 突き合わせ(`divergence`)は下表から機械的に導出する。手で分類しない

| machine | human | divergence | 意味 |
|---|---|---|---|
| post_veto_candidate | post_veto_candidate | none | 一致 |
| pre_go | pre_go | none | 一致 |
| post_veto_candidate | pre_go | false_negative | 締め領域の見逃し。最優先で検出器改修 |
| pre_go | post_veto_candidate | false_positive | 過剰保守。安全側。頻度のみ監視 |
| gray | いずれか | gray_case | 検出器の失敗ではない。グレー解決手続きの校正データ |

### 乖離への対処優先順位

1. `false_negative`: 締め領域の見逃し。検出器の改修対象として最優先。該当ケースを再現する
   回帰テストを必ず伴う
2. `gray_case`: 検出器の欠陥ではないが、`axis_b_unmet` の内訳(サイズ超過かテスト欠如か)を
   集計し閾値の校正データにする
3. `false_positive`: 安全側。頻度が高い検出器は精緻化候補として記録するに留め、shadow期間中の
   緩和は原則行わない(緩和は false_negative リスクとの交換になるため)

### 昇格条件(件数基準)

以下の AND を満たしたときのみ、ユーザーの明示判断として本番化する(自動昇格はしない)。

1. 直近 N 件の連続案件で `false_negative` がゼロ(N は shadow開始時にユーザーが決める)
2. 連続無事故のカウントは `detector_sha256` が変わるたびにリセットする
3. shadow期に発生した全乖離に `divergence_reason` が記入済みで、`false_negative` は全件が
   検出器改修+回帰テスト追加済み
4. 連続無事故ウィンドウ内の全 merge済みPRにパッケージが存在する(欠落ゼロ)

### 降格条件

昇格後、merge済み `post_veto_candidate` 案件について `false_negative` が1件でも事後判明したら
(抜き取り監査での発覚を含む)、即座に shadow へ戻す。降格・原因分析・検出器改修・再昇格の
記録を残す。

## 既知の検出ギャップ・過剰検出

shadow 期の観察対象として記録しておく。`false_negative` として実測されたら検出器を拡張し、
実測されなければ現状の単純さを維持する。過剰検出(`false_positive`)の緩和は昇格前の校正課題
として扱う。

- サービス層の返り値 dict の形状変更(レスポンス形状の変更だが `PUBLIC_IF_PATHS` 外で起きうる)
- skill markdown 内の破壊的指示文(markdown除外規則の盲点。LLMへの指示文として
  「DELETE FROM を実行せよ」と書ける)
- 実行時に組み立てられる動的SQL(文字列連結でDDL/破壊文が完成するケース)
- サービス層CRUD由来の `data_destructive` 過剰ヒット: `UPDATE ... SET` / `DELETE FROM` は
  通常のCRUD実装として多数存在するため、永続化を触る通常の機能追加PRが高確率で `pre_go`
  固定になる構造的 false positive がある。shadow期に CRUD由来ヒット率を計測し、パラメタライズド
  WHERE付きUPDATE/DELETEの扱い(降格または文脈限定)を昇格前の校正課題とする
- `public_if` の広すぎるヒット: `src/main.py` はツール定義とヘルパーが同居しており、任意行の
  接触で hit する。ツール表面 vs ヘルパーのみ、の内訳を AST差分で分類集計し、同じく昇格前の
  校正課題とする

## GO判定パッケージツール(go_package.py)

1設計案 = 1パッケージ。markdown文書で、先頭に機械可読ブロック(` ```go-package ` フェンス内
YAML)、続いて人間が読む3区分本文(1-a 分類判定材料 / 1-b 地図メンテ材料 / 1-c 品質証跡、
計10小見出し)を置く。保存先は cc-memory material(素タグ `go-package` + `domain:cc-memory`
必須)であり、PR本文には載せない(判例idを含む文書であり、PR本文の記述規則と衝突するため)。

### サブコマンド

```
uv run python3 scripts/go_package.py template
    テンプレートmarkdownをstdoutへ出す。機械可読ブロック・3区分本文とも空欄の
    プレースホルダのみで、`lint --allow-placeholder` を通る状態にある

uv run python3 scripts/go_package.py new --activity <id> [--base origin/main] [--head HEAD] \
    [--predicted pre_go|gray|post_veto_candidate] [--pull-json <file>] [--out <path>] \
    [--repo <path>]
    scripts/gate_check.sh(正規呼び出し経路)を実行し、機械判定欄(gate.machine /
    gate.effective / detector_sha256 / verdict_sha256)とブラスト半径・revert容易性の
    2小見出しを実データで充填した雛形を生成する。--pull-json に pull_precedents 応答
    (JSON、design-pull-core.md 3-3-1 のスキーマ)を渡すと pull.presented / pull.guarantee
    を機械転記する(手書きしない)。人間記述欄(判例引用・判例が無かった論点・1-b・1-c・
    shadow)はプレースホルダのまま

uv run python3 scripts/go_package.py lint <file.md> [--mode shadow|live] [--allow-placeholder]
    L1〜L8 を検証する(下記)。--allow-placeholder は L2(セクション非空)・L6(shadowブロック
    必須)のみを緩和し、未記入のドラフト状態を許容する。exit code: エラーあり=1 / 警告のみ
    または問題なし=0

uv run python3 scripts/go_package.py extract <file.md>
    機械可読ブロックをJSONでstdoutへ出す(観測装置の取り込み用)

uv run python3 scripts/go_package.py shadow-report [--db <path>] [--prs-file <json>]
    素タグ go-package の material 群(`src.db.get_connection()` 経由、read-only)から
    機械可読ブロックを抽出し、divergenceの件数・detector_sha256別の連続false_negative
    ゼロ数を集計する。--prs-file に merge済みPR番号一覧のJSON配列を渡すと、パッケージの
    `prs` フィールドと突合してパッケージ欠落PRを検出する(7-4の昇格条件のカバレッジ判定に使う)
```

`shadow-report` のみ `src.db` に依存する(遅延import)。他のサブコマンドは標準ライブラリ +
pyyaml のみで動く。

### lintルール

| ID | 内容 | エラー/警告 |
|---|---|---|
| L1 | 機械可読ブロックが存在しYAMLとしてparseでき、`schema_version` が既知 | エラー |
| L2 | 1-a×4・1-b×4・1-c×2 の10小見出しが存在し非空(`--allow-placeholder` で非空チェックのみ緩和) | エラー |
| L3 | `precedents` の `stance` が列挙値内、判例引用テーブルの行数と件数が一致 | エラー |
| L4 | `novel_points` キーが存在(空リスト可、キー欠落は不可) | エラー |
| L5 | `strictness(gate.effective) >= strictness(gate.machine)`(pre_go=2 > gray=1 > post_veto_candidate=0)。厳格化方向(effectiveの方が強い)は `gate.escalated_by` 必須。唯一の緩和例外は `machine: gray` から `gray_resolution.resolved_to: post_veto_candidate` かつ `basis` が非空かつ各基底判例が `precedents` にstance付きで存在する場合のみ | エラー |
| L6 | `--mode shadow` のとき `shadow` ブロックが必須(`--allow-placeholder` で必須チェックのみ緩和)。存在する場合は `shadow.human` が妥当な値で、`shadow.divergence` が下表の対応表から正しく導出されている | エラー |
| L7 | `gate.predicted` と `gate.machine` が乖離している | 警告のみ |
| L8 | `--mode live` のとき `pull.presented: unavailable` はエラー(pull_precedents 稼働後の実行漏れ検知) | エラー |

divergence対応表(3-7、`expected_divergence()` が単一ソース):

| machine | human | divergence |
|---|---|---|
| post_veto_candidate | post_veto_candidate | none |
| pre_go | pre_go | none |
| post_veto_candidate | pre_go | false_negative |
| pre_go | post_veto_candidate | false_positive |
| gray | いずれか | gray_case |

### 運用フロー(想定)

1. 設計着手時に `pull_precedents` を実行し、応答JSONを保存する(pull稼働後。稼働前は省略可)
2. PR作成時に `go_package.py new --activity <id> --pull-json <保存した応答>` でパッケージ雛形を生成する
3. 人間が1-a判例引用・判例が無かった論点・1-b・1-cを記入する
4. shadow期は `shadow.human` / `shadow.divergence` を追記する
5. `go_package.py lint <file> --mode shadow` で検証し、通ったら material として保存しユーザーへ提示する
6. PRの機械可読ブロック `prs` フィールドへ、open後のPR番号を追記する

## 他コンポーネントとの共有物

- パターン定数(`DDL_PATTERNS` / `DESTRUCTIVE_SQL_PATTERNS` / `DESTRUCTIVE_FS_PATTERNS`)と
  サイズ計数関数(`count_diff_size()`)・閾値定数(`MAX_LINES` / `MAX_FILES`)は
  `scripts/gate_check.py` を単一ソースとする。他コンポーネント(PRサイズlint、migration安全
  装置)はこれを import して使い、二重定義しない
