# コントリビューションガイド

CALM への関心をありがとうございます。バグ報告・機能提案・PR を歓迎します。
参加にあたっては [行動規範](CODE_OF_CONDUCT.md) に従ってください。

プロダクトの概要・インストール・設定は [README](README.md) を参照してください。このドキュメントは開発に参加するための手順だけを扱います。

## Issue

- バグ報告・機能提案は [Issue テンプレート](https://github.com/isizono/calm/issues/new/choose) から起票してください
- 既存の issue に同じ内容がないか先に検索してください
- 使い方の疑問は、まずプラグインをインストールした Claude Code で `/man` を実行すると解決することがあります

## 開発環境

前提条件（uv、Python 3.12 の SQLite 拡張ロード対応ビルド等）は README の [前提条件](README.md#前提条件) と同じです。

```bash
git clone https://github.com/isizono/calm.git
cd calm
uv sync
```

コミット前チェックに [pre-commit](https://pre-commit.com/) を使っています（`.pre-commit-config.yaml`）。`main` への直接コミットもこのフックで防止されます。

```bash
pre-commit install
```

## ブランチと worktree

- `main` への直接 push は禁止です。変更はすべて PR で行います
- ブランチは `origin/main` の最新から切ります
- 作業は git worktree で行い、`.trees/` 配下に作成します（プロジェクトルートでブランチを checkout しない）
- ブランチ名は `feature/<要約>` / `fix/<要約>` / `docs/<要約>`（英語ケバブケース）

```bash
git fetch origin
git worktree add .trees/<name> -b feature/<要約> origin/main
```

Claude Code で開発する場合の追加の作法は [CLAUDE.md](CLAUDE.md) にまとまっています。

## コミットメッセージ

[Conventional Commits](https://www.conventionalcommits.org/ja/) 形式（scope なし）で、type は英語、subject は日本語で書きます。

- type: `feat:` / `fix:` / `docs:` / `refactor:` / `test:` / `chore:`
- 例: `feat: searchにrecency boost追加`
- body は変更理由が自明でない場合のみ書きます

## テスト

```bash
uv run pytest
```

CI（`.github/workflows/test.yml`）はテストを `unit`（`tests/unit tests/test_migrations tests/services`）と `integration-e2e`（`tests/integration tests/e2e`）に分けて並列実行しています。手元で同じ範囲を回す場合は次のようにします。

```bash
uv run pytest -n auto tests/unit tests/test_migrations tests/services
uv run pytest -n auto tests/integration tests/e2e
```

テストを追加・変更する前に [テスト規約](docs/spec/test-convention.md) を読んでください。

## ドキュメントの同時更新

CI の `lint-docs` ジョブ（`scripts/lint_doc_cochange.py`）が、コードと外縁ドキュメントの同時更新を検査します。

- `migrations/*.sql` を変更したら `docs/spec/db-schema.md` も更新する（スキーマ形状が変わらない場合は PR 本文かコミットメッセージに `[no-schema-shape-change]`）
- `src/main.py` の MCP ツールのシグネチャ・増減を変更したら `docs/spec/mcp-tools.md` も更新する（例外マーカーは `[no-tool-surface-change]`）
- README の「MCPツール」「スキル」表は、実装されたツール・`skills/*/SKILL.md` と一致させる

手元では次で確認できます。

```bash
uv run python scripts/lint_doc_cochange.py --base origin/main --head HEAD
```

このほか CI では、`docs/spec/openapi.yaml` と `docs/spec/db-schema-tables.md` の生成物ドリフト検査、migration の lint（適用済み migration の変更は禁止。新しい migration を追加してください）なども走ります。詳細は [外縁ドキュメント同期規約](docs/spec/doc-sync-convention.md) と `.github/workflows/` を参照してください。

## Pull Request

- リポジトリへの書き込み権限がない場合は、fork にブランチを push してから PR を作成してください
- PR 本文は [PR テンプレート](.github/PULL_REQUEST_TEMPLATE.md) の構成（概要・補足・テスト計画・Revert）に沿って書いてください
- 1 PR は 1 つの目的に絞ってください。大きすぎる PR には CI（PR Size Check）がコメントで知らせます
- CI がすべて通っていることを確認してからレビューを依頼してください

## ライセンス

コントリビューションは本リポジトリの [MIT License](LICENSE) の下で提供されるものとします。
