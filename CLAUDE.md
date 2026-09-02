## 開発フロー

- 実装前に関連トピックのget_decisionsを取得し、ユーザーに仕様確認を取ってから着手する
- cc-memoryプラグインがある場合、コードベース調査の前にまず既存記録で文脈を取得すること

## コミット規約

Conventional Commits形式（scopeなし）。typeは英語、subjectは日本語。

- `feat:` / `fix:` / `docs:` / `refactor:` / `test:` / `chore:`
- 例: `feat: searchにrecency boost追加`
- bodyは変更理由が自明でない場合のみ

## ブランチ戦略

- main直push禁止（pre-pushフックで防止）
- mainの作業ディレクトリ（プロジェクトルート）でコード変更を行わないこと。ファイル編集は必ずworktree内で行う
- ブランチ作業は必ずgit worktreeで行うこと（作業ディレクトリで直接checkoutしない）
- worktreeは`.trees/`配下に作成する
- ブランチは必ずorigin/mainの最新から切る
- 命名: `feature/<要約>`, `fix/<要約>`, `docs/<要約>`（英語ケバブケース）

## PRマージ後の反映手順

cc-memoryはローカルディレクトリをmarketplaceとして登録しており、mainブランチからプラグインキャッシュが生成される。PRマージ後は以下を実行する:

1. **メインディレクトリが main ブランチをホールドしていることを確認**: プロジェクトルートで `git branch --show-current` が `main` を返すこと。別ブランチに居る・別 worktree が main を握っている場合は以下で復旧してから手順 2 へ:
   - 未コミット変更があれば `git stash push -u -m "wip"` で退避
   - 別 worktree が main を握っていれば `git worktree remove <path>` で開放（対象 worktree に未コミット変更が残っているとコマンドが失敗するため、先に stash / commit してから実行する）
   - その上で `git checkout main`
   - 理由: 手順 7 のサーバー起動は cwd 配下のコードで動くため、メインディレクトリが main 以外だとプラグインキャッシュ（main 由来）とサーバー本体（別ブランチ由来）のミスマッチで動作不整合が起きる
2. `git pull origin main`
3. マージ済みworktreeを削除: `git worktree remove .trees/<name>`（対象 worktree に未コミット変更が残っているとコマンドが失敗するため、先に stash / commit してから実行する）
4. ローカルブランチを削除: `git branch -D <branch>`
5. プラグインキャッシュを削除: `rm -rf ~/.claude/plugins/cache/claude-code-memory-marketplace/`
6. `__pycache__` を削除: `find . -type d -name __pycache__ -exec rm -rf {} +`
7. 既存のhttpサーバーを停止・再起動:

   ```sh
   lsof -ti tcp:52837 -sTCP:LISTEN | xargs kill
   nohup .venv/bin/python -m src.main --transport http > /tmp/calm_http_server.log 2>&1 &
   ```

   - `-sTCP:LISTEN` を付けないと :52837 に接続中のブリッジプロセスまで巻き添えで kill され、生存セッションの再接続競争を誘発する。多数のセッションが同時接続している状態でkillすると、再接続待ちのブリッジ全部が同時に起動リトライを行い、起動が失敗することがある
   - 起動するのはブリッジ（`src.launcher`）ではなくサーバー本体（`src.main --transport http`）。launcherはstdio MCPブリッジで、stdinが`/dev/null`になる非対話シェル（エージェントのツール実行・スクリプト等）から起動すると即EOFでサイレント終了し、HTTPサーバーが再起動されないままになる。各セッションのブリッジは既存の再接続機構で新サーバーへ自動的に繋ぎ直すため、launcher自体の再起動は不要
   - 起動後、`lsof -i tcp:52837 -sTCP:LISTEN` でLISTENしていることを確認する（LISTENが無ければ `/tmp/calm_http_server.log` を確認する）
8. embeddingサーバーを停止: `lsof -ti tcp:52836 -sTCP:LISTEN | xargs kill`（再起動は不要。次回encode時に`embedding_service`がlazy spawnする。`-sTCP:LISTEN`を付けないと接続中クライアントを巻き添えにする）
9. 生存している全Claude Codeセッションで `/mcp` からreconnectを実行する（個別でOK、全セッション同時に落とす必要なし）。reconnectで復旧しない場合はそのセッションを再起動する
