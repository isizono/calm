# 外縁ドキュメント同期規約

## 0. 読み方

外縁ドキュメント = リポジトリに置かれ、DB の外で「地図」を担う文書（`docs/spec/db-schema.md` / `docs/spec/mcp-tools.md` / `docs/architecture/components.md` 等）。これらは一次情報（`migrations/` / `src/main.py`）を人間が読みやすく写し取ったものであり、一次情報が変わっても自動更新されないため陳腐化する。本書はその陳腐化を検出する仕組みと、検出後の更新責務を定める。

サーバー本体（cc-memory MCP server）はリポジトリ側ドキュメントを読まない。cc-memory サーバーは任意ユーザーの汎用サーバーであり、特定リポジトリのファイル配置を知る立場にないためである。本規約が定める検査は本リポジトリ専用の `scripts/` 配下のローカルスクリプトと CI の 1 ステップで完結し、サーバー本体には一切手を入れない。

---

## 1. 同期マーカー

対象ドキュメントの先頭に HTML コメントでマーカーを置く。

```html
<!-- ccm-doc-sync
watch-tags: domain:cc-memory
watch-direction: true
watch-migrations: true
last-synced: 2026-07-04
last-synced-migration: 0048
-->
```

| フィールド | 意味 |
|---|---|
| `watch-tags` | カンマ区切りのタグ一覧（OR条件）。このタグ群を持つ decision が `last-synced` より後に増えたら stale |
| `watch-direction` | `true` のとき、`layer:direction` decision の追加・supersede（`decision_supersedes.created_at` で判定）を監視対象に含める |
| `watch-migrations` | `true` のとき、`migrations/` の最大ファイル番号が `last-synced-migration` を超えたら stale |
| `last-synced` | 直近の同期日（`YYYY-MM-DD`）。`watch-tags` / `watch-direction` の起点日時として使う |
| `last-synced-migration` | 直近同期時点の最新 migration 番号（4桁ゼロ埋め文字列） |

ドキュメント更新者は、内容を同期させたら `last-synced` / `last-synced-migration` を書き換える。マーカーが無いドキュメントは checker / lint のいずれからもスキップされる（対象外として扱われる）。

---

## 2. checker: `scripts/check_doc_freshness.py`

```
uv run python scripts/check_doc_freshness.py [--json] [--docs-root docs/] [--db <path>] [files...]
```

`docs/` 配下（+ 明示指定ファイル）の ccm-doc-sync マーカーを走査し、ローカル DB（decisions / decision_supersedes / decision_tags / topic_tags 継承）と `migrations/` のファイル名を突き合わせて、stale なドキュメントと理由を出力する。stale が 1 件でもあれば exit 1。

- **実行タイミング**: recompose / orch 起動時の運用チェックの一部として手動・skill 起動で回す
- **CI には載せない**: CI からユーザーのローカル DB は見えない。DB 参照を必要としない部分（migration 番号比較のみ）は次節の lint が別途 CI で担う

---

## 3. lint: `scripts/lint_doc_cochange.py`

```
uv run python scripts/lint_doc_cochange.py --base <ref> --head <ref>
```

git diff だけで判定できる規約を CI（`.github/workflows/test.yml`）で強制する:

1. `migrations/*.sql` に差分がある PR は `docs/spec/db-schema.md` にも差分があること。例外はコミットメッセージまたは PR 本文に `[no-schema-shape-change]` を含める（index 追加のみ等、スキーマ形状が変わらない変更）
2. `src/main.py` の `@mcp.tool()` デコレータ付き関数のシグネチャ・増減に差分がある PR は `docs/spec/mcp-tools.md` にも差分があること。例外マーカーは `[no-tool-surface-change]`

判定不能（`ast.parse` 失敗等）は警告のみで pass する。doc lint で開発を止めないためで、締め領域の防壁（マージ可否の最終ゲート）は別コンポーネントの管轄であり、この lint は地図メンテの補助輪という位置づけである。

PR 本文をチェック対象に含めるには環境変数 `CCM_PR_BODY` に本文を渡す（`.github/workflows/test.yml` では `${{ github.event.pull_request.body }}` を渡している）。

---

## 4. 更新責務の規約

- **方向性 decision の追加・supersede を行ったセッションは、`watch-direction: true` の文書の更新を同時に起案する**（agent が draft し、direction 記述部分は人間の GO を経る）
- **それ以外の watch-tags 起因の stale は checker 検出時にまとめて更新してよい**（agent 自走可）
- **`lint_doc_cochange.py` が fail したら、当該 PR 内でドキュメントを同時更新する**か、形状が変わらない変更であれば該当する例外マーカーをコミットメッセージまたは PR 本文に明記する

---

## 5. 対象ドキュメントの初期リスト

| ドキュメント | 内容 | 陳腐化トリガー |
|---|---|---|
| `docs/spec/db-schema.md` | スキーマ写し | `migrations/` への変更 |
| `docs/spec/mcp-tools.md` | ツール IF | `src/main.py` のツール定義変更 |
| `docs/architecture/components.md` | 構成地図 | サービス追加・依存変化 |

3 文書とも本規約導入時点でマーカーを敷設済み。`docs/spec/db-schema.md` は当時判明していた陳腐化（`decisions.topic_id` / `discussion_logs.topic_id` の直接 FK 記載が migration 0047 で既に削除済みだったこと、および 0040〜0048 の未反映）を修正したうえでマーカーを敷設した。他の 2 文書はマーカー敷設時点の内容をそのまま起点とし、以降の drift を checker / lint で捕捉する。

invariant 一覧文書（不変条件の一覧）は本規約策定時点でまだ存在しない。新設され次第、`watch-direction: true` を含むマーカーを追加する対象として本リストに加えること。
