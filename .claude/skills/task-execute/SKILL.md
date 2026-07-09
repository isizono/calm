---
name: task-execute
description: plan.mdに基づいて実装を実行する。plan.mdの未着手サブプランを自律選択し、worktree作成→実装SA→レビューSA→ユーザー報告→PRの流れで進める。plan.mdが存在しない場合はtask-planを先に実行するよう案内する。
---

# Task Execute

plan.mdに基づいて実装を実行するスキル。
worktree作成からPR作成までが責務。計画はtask-planが担う。

## トリガー条件

以下のすべてを満たす場合に使用する：
- plan.mdが既に存在する
- ユーザーが実装実行を依頼した
- plan.mdに未着手（🔲）のサブプランがある

以下では使用しない：
- plan.mdが存在しない → task-plan を先に実行するよう案内する
- 仕様が未確定 → discussion / design スキルを使う
- 調査・検索・質問応答 → 本体が直接対応

## ワークフロー

### Step 1: サブプラン選択

plan.mdのPR分割テーブルを読み、依存関係を満たす未着手（🔲）のサブプランを自律的に選択する。

選択基準：
1. 依存先がすべて✅完了であること
2. 複数候補がある場合はテーブル上の順番が早いものを優先
3. 選択したサブプランの状態を🔄着手中に更新する

**単一PRの場合**: plan.md自体がサブプラン相当なので、このステップはスキップ。

### Step 2: 判例確認（pull_precedents）

実装着手前に `pull_precedents` MCPツールを実行し、応答JSONを保存する。過去の関連判例（decision）を
把握しないまま設計・実装判断を進めることを防ぐための必須ステップである。

1. `pull_precedents` を `context`（このサブプランが解決しようとしている論点の記述。plan.mdの
   「作業背景」やTODOの要約でよい）で呼ぶ
2. 応答JSONを `iterations/{nn}-pull.json` に保存する（Step 7でGO判定パッケージの
   `pull.presented` / `pull.guarantee` に機械転記する）
3. `guarantee` が `routing_miss` / `routing_unavailable` の場合は判例保証が成立していない
   （前例なし、または保証不成立）として扱い、`pre_go`寄りの判断に倒す。`enumerated` の場合は
   列挙された判例を実装方針の参考にする

**注記**: `pull_precedents` ツールが未稼働の環境（判例pull機構の実装がまだmainに反映されて
いない場合）はこのステップを省略してよい。省略した場合、Step 7で生成するGO判定パッケージの
`pull.presented` は `unavailable` のままになる。

### Step 3: worktree作成

メインエージェントが以下を実行する（SAには委譲しない）：

1. `origin/main`（またはplan.mdで指定されたbase branch）を最新にfetch
2. `.trees/` 配下にgit worktreeを作成する
3. worktreeの絶対パスを以降のステップで使用する

```bash
git fetch origin main
git worktree add .trees/{branch-name} -b {branch-name} origin/{base-branch}
```

**重要: worktreeの準備が完了するまで、いかなるファイル操作も行わないこと。**

### Step 4: 実装SA起動

`task-implementer` サブエージェントを **バックグラウンド（`run_in_background: true`）** で起動する。
モデルは `model: sonnet` を明示する（実装は設計・plan.mdに沿った記述作業が主体で、sonnetで十分なため）。

**実装SAへのプロンプトテンプレート：**

```
あなたは実装サブエージェントです。以下のタスクを実装してください。

## タスク定義
plan.md（またはサブプラン）を読んでください: {絶対パス}

## 重要な参照
- 設計書: {絶対パス}（最初に必ず読むこと）
- 既存パターンの参考: {絶対パス}

## 実装ルール
1. 設計書の仕様に忠実に実装すること
2. 既存コードのパターン（エラーハンドリング、命名規則等）を踏襲すること
3. plan.mdの実装順序に従って進めること
4. 各TODOの完了時にiterations/{nn}-impl.mdに進捗を記録すること
5. CLAUDE.mdの規約に従うこと
6. plan.mdに記載のテスト実行方法に従ってテストを実行すること
7. コミットは作成しないこと（コミットはメインエージェントが行う）

## 完了時の出力
iterations/{nn}-impl.md に以下を記録:
- 完了したTODO一覧
- 変更したファイル一覧（絶対パス）
- 実装上の判断事項（設計書に明記されていなかった部分の解釈）
- テスト実行結果

## 作業ディレクトリ
{worktreeの絶対パス}

## 重要
作業ディレクトリは既にworktreeとして準備済みです。このディレクトリ内で作業してください。
新たにworktreeを作成したり、ブランチを切り替えたりしないでください。
```

渡すもの：
- plan.md（またはサブプラン）の絶対パス
- 作業ディレクトリの絶対パス
- イテレーション番号（01〜）
- 設計書・参考実装の絶対パス

### Step 5: レビューSA起動

実装SA完了後、`task-reviewer` サブエージェントを **バックグラウンド（`run_in_background: true`）** で起動する。
モデルは `model: opus` を明示する（バグ検出・仕様整合・テスト審査の判断品質を優先するため、親がsonnet運用でもレビューはopusで動かす）。

**レビューSAへのプロンプトテンプレート：**

```
あなたはコードレビューサブエージェントです。以下の実装をレビューしてください。

## レビュー対象
- 実装ログ: {iterations/{nn}-impl.md の絶対パス}
- 変更ファイル一覧: {ファイルリスト or git diffの取得方法}

## 参照
- plan.md: {絶対パス}
- 設計書: {絶対パス}

## レビュー観点
1. 設計書との整合性: 仕様通りに実装されているか
2. コード品質: 既存パターンとの一貫性、命名規則、エラーハンドリング
3. テストカバレッジ: 以下a・bの両方を満たすか確認する
   a. 一般カバレッジ（通常系）: エッジケース表に載らない通常系（happy path）・基本機能に対しても、
      具体的な期待結果をアサートするテストが存在するか。表の照合（b）だけでは通常系の
      テスト網羅は担保されないため、独立にチェックする
   b. テスト仕様カバレッジ（照合）: plan.md「## エッジケース（仕様カバレッジ）」表の
      各ケースの「あるべき振る舞い」を検証するテストが実在するか。表の振る舞いの正しさは
      plan段階の導出検証SA＋人間承認で担保済みの前提。ここは照合に徹する。
      - 「検証する」＝そのケースを突き、あるべき振る舞い（期待結果）をアサートしていること。
        ケースを呼ぶだけ・存在を確認するだけは不可（観点4と連動）
      - severity 一意化（観点4と重複する領域）: ケースに対応するテスト不在 = Critical /
        ケースは突くがアサーション無し（写し相当）= Critical / ケースは突くがアサーションが弱い = Major
      - 前提ガード: 表が「テスト不要（類型X：理由）」と明記されていれば本観点はスキップ。
        表が空かつ明記も無い場合は「plan段階の手続き不備」として Minor 指摘。
        ただし diff に分岐追加・新規公開関数・入力バリデーションが含まれるのに表が空なら、
        本来エッジケースが必要なのに列挙されていない疑いとして **Major に格上げ**。
        ※これは「どのケースが漏れたか」の網羅判定（plan責務）ではなく「そもそも列挙すべき状況なのに
        空か」の観測シグナルのフラグ立てに留める
      - プロパティ種別のケースは、値一致でなく不変条件を検証しているか
4. アサーション実効性: 実装の写し・トートロジーの足跡を検出（成果物から検知できるため重視）
   - 実装を呼んで返り値をそのまま期待値にしている（assert f(x)==f(x) 等）→ Critical
   - 「例外が出ない」「None でない」止まりで具体値を未検証 → Major
   - スナップショット/ゴールデンの生成元が実装出力なら赤、仕様由来の固定フォーマットなら可
5. エッジケース: 設計書に記載のないエッジケースへの対処
6. セキュリティ: SQLインジェクション、入力バリデーション等
7. 再利用性・簡素化: 重複コードの検出、不要な抽象化

## 出力
iterations/{nn}-review.md に、各観点の「検出事項 ＋ severity（Critical/Major/Minor）」の形式で出力してください。
PASS/FAIL 等の最終判定は下さない（メインエージェントが Step 6 で判定する）。
decision とコード/PR の間に矛盾を見つけた場合も、ここで FAIL とせず「メイン経由でユーザー確認が必要」と
記載するに留めてください。decision は絶対の正解(truth)でなく独立な期待値ソース(oracle)であり、
ズレの裁定は人間が行います。

## 作業ディレクトリ
{worktreeの絶対パス}
```

渡すもの：
- plan.md の絶対パス
- iterations/{nn}-impl.md の絶対パス
- 変更されたファイル一覧
- 設計書の絶対パス

### Step 6: レビュー結果に基づく判定

メインエージェントがレビュー結果を読み、**自律的に**判定する。

**判定基準テーブル：**

| 判定 | 条件 | アクション |
|------|------|-----------|
| **PASS** | Critical: 0, Major: 0 | → Step 7（報告）へ |
| **DIRECT_FIX** | Critical: 0-1, 修正量が少ない | → メインが直接修正 → Step 7 へ |
| **RE_DELEGATE** | Critical: 2+, または大規模修正 | → 修正SA再起動 → Step 5 に戻る |

「修正量が少ない」の目安：
- 修正対象ファイルが3個以内
- 修正行数が合計50行以内
- 新規ファイル作成を伴わない

### Step 7: コミット + GO判定パッケージ + ユーザーへ報告

`scripts/gate_check.py`（境界ゲート検出器）はコミット済みの差分を対象に動くため、
GO判定パッケージの生成にはこの時点でのコミットが要る。PRはまだ作らない。

1. **コミットを作成する**（CLAUDE.mdの規約に従う。pushはまだしない）
2. **GO判定パッケージの雛形を生成する**：

   ```
   uv run python scripts/go_package.py new --activity {タスクID} --base {base branch} \
       --head HEAD --predicted {plan.md/サブプランのpredicted値} \
       [--pull-json iterations/{nn}-pull.json] \
       --out iterations/{nn}-go-package.md
   ```

   Step 2で `pull_precedents` を省略した場合は `--pull-json` を付けずに実行する。
3. **人間記述欄を埋める**: 「1-a 分類判定材料」の判例引用・判例が無かった論点、
   「1-b 地図メンテ材料」「1-c 品質証跡」の各欄。判例が無ければ「なし」と明記する（空欄禁止）
4. 埋めた雛形を `uv run python scripts/go_package.py lint iterations/{nn}-go-package.md --mode shadow --allow-placeholder` に通す（`shadow`欄はまだ未記入のため `--allow-placeholder` を付ける）
5. 下記テンプレートで、GO判定パッケージの分類判定材料を含めてユーザーに報告する

**報告テンプレート：**

```
## 実装完了報告

### 変更ファイル一覧
- {ファイルパス}: {何をしたか}
- ...

### planからの逸脱
- {planと異なる判断をした箇所。なければ「なし」}

### レビュー結果
- 判定: {PASS / DIRECT_FIX}
- {レビューで改善した点があれば記載}

### 仕様サマリ
**機能**: {機能名}
**目的**: {なぜ必要か}
**方式**: {どう実現したか（1行）}

### GO判定
- machine: {classification}（{reason}）
- predicted: {plan.md/サブプランの値}
- ブラスト半径・revert容易性の要点: {1-2行}

→ この変更、コード読みが必要な類（事前go相当）？ パッケージだけで判断できる類（事後拒否権相当）？
→ PR出してOK？
```

6. ユーザーの回答を機械可読ブロックの `shadow.human` に記入し、`shadow.divergence` を
   `docs/spec/go-gate.md` の対応表（machine × human）から導出して追記する
7. `uv run python scripts/go_package.py lint iterations/{nn}-go-package.md --mode shadow`
   を（`--allow-placeholder` なしで）再度通す

**ユーザーのOKが出てから**PR作成に進む。

### Step 8: PR作成

1. PRを作成する前に `uv run python scripts/pr_size_check.py --local` を実行してサイズを確認する
   - `--local` の既定 base は `origin/main`。サブプランの base が main 以外（依存先ブランチにスタックしている）場合は `--base <依存先ブランチ>` を明示する。指定しないと依存元ブランチの差分ごと巻き込んだ過大な verdict になる
   - verdict が `oversized` の場合、PRを出す前に分割を検討する（サブプランをさらに分けるか、変更範囲を見直す）
   - verdict が `ok` / `large` ならそのまま進めてよい
2. push（`git push -u origin {branch}`）する
3. PRを作成
4. GO判定パッケージ（`iterations/{nn}-go-package.md`）の機械可読ブロック `prs` フィールドに、
   作成したPR番号を追記する
5. `add_material` でGO判定パッケージ全文を保存する（title 40字以内、素タグ `go-package` +
   `domain:cc-memory`、`related` にタスクのactivityを指定）。**GO判定パッケージはPR本文には
   載せない**（判例idを含む文書のため。PRとの対応は `prs` フィールドが持つ）
6. plan.mdの当該サブプランの状態を✅完了に更新

### Step 9: 統合マージチェック

Step 8完了後、plan.mdを確認し、残りの🔲が `final`（統合マージ）のみかをチェックする。

該当する場合：
1. ユーザーに「全サブプラン完了。統合ブランチからmainへのマージPR出していい？」と確認
2. OKが出たら統合ブランチ→mainのマージPRを作成（SA起動不要）
3. plan.mdのfinalを✅完了に更新

該当しない場合（他に🔲のサブプランが残っている、またはfinal行がない）：
- 何もしない。通常のフローで次のサブプランに進む。

## iterations/ ディレクトリ

実装ログとレビュー結果はworktree内ではなく、plan.mdと同じディレクトリに保存する。

```
~/.claude/projects/<project>/work/{task-name}/
├── plan.md
├── plan-a.md（サブプラン、あれば）
├── plan-b.md
└── iterations/
    ├── 01-pull.json（pull_precedents応答、Step 2で保存。省略時はなし）
    ├── 01-impl.md
    ├── 01-review.md
    ├── 01-go-package.md（GO判定パッケージ、Step 7で生成しmaterial化）
    ├── 02-impl.md（RE_DELEGATEの場合）
    └── 02-review.md
```

## 注意事項

- plan.mdがない場合は「task-planを先に実行して」と案内する
- worktree作成前にファイル操作を始めない
- PR作成前に必ずユーザーに報告して確認を取る
- plan.mdの状態更新を忘れない
- SAの出力はユーザーに見えない。報告テンプレートでユーザーに内容を伝える
- force pushは絶対にしない
