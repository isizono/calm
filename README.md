# claude-code-memory

Claude Codeのセッション間で、議論の文脈・決定事項・作業状況を永続化するプラグインです。

## 何が解決されるのか

Claude Codeはセッションごとに記憶がリセットされます。短いタスクなら問題ありませんが、長期プロジェクトでは「前に何を決めたか」「なぜその設計にしたか」「どこまで作業が進んでいるか」がセッションをまたぐと失われます。

claude-code-memoryは、こうした文脈をSQLiteデータベースに保存し、新しいセッションでAIが自動的に過去の記録を参照できるようにします。同じ説明を繰り返す必要がなくなり、議論の積み重ねがそのまま次のセッションに引き継がれます。

## 主な機能

- **トピック管理** — 議論の主題ごとに情報を整理します
- **決定事項の記録** — 合意した内容を理由とともに保存します
- **議論ログ** — 議論の経緯や検討過程を保存します
- **アクティビティ管理** — 作業タスクの進捗をステータスで追跡します
- **資材管理** — セッション中に生成された分析結果・ドラフト等をタグ付き独立エンティティとして永続化します
- **リレーション** — トピック・アクティビティ間の関連をグラフ構造で管理します
- **タグシステム** — トピック・決定・ログ・アクティビティを横断的にタグで分類します。タグにnotesを付けて作業開始時にAIへ自動注入できます
- **振る舞い（habits）** — check-in時にAIへ毎回注入される運用ルールを管理します
- **ハイブリッド検索** — キーワード検索（FTS5）とベクトル検索を組み合わせて関連情報を見つけます
- **自動同期** — セッション終了時にstop hookで自動起動し、会話内容をcc-memoryに同期します

## インストール

### 前提条件

- [uv](https://docs.astral.sh/uv/) がインストールされていること
- Claude Code v2.0.12以上
- Python 3.12+（SQLite拡張ロード対応ビルドが必要）
  - pyenvのデフォルトビルドは `--enable-loadable-sqlite-extensions` が無効のため非対応
  - Homebrew Python (`brew install python@3.12`) を推奨

### インストール手順

```bash
# マーケットプレイスを追加
claude plugin marketplace add isizono/cc-memory

# プラグインをインストール
claude plugin install claude-code-memory
```

インストール後、Claude Code内で以下を実行すると使い方の案内が表示されます。

```
/guide
```

## MCPツール

| カテゴリ | ツール | 説明 |
|---------|--------|------|
| トピック | `add_topic`, `get_topics` | 議論トピックの作成・取得 |
| 議論ログ | `add_log`, `get_logs` | 議論の経緯や検討過程の記録・取得 |
| 決定事項 | `add_decision`, `get_decisions` | 合意内容の記録・取得 |
| アクティビティ | `add_activity`, `get_activities`, `update_activity` | 作業タスクの作成・取得・状態更新 |
| check-in | `check_in` | アクティビティにcheck-inし、tag notes・資材・関連decisionsを集約取得 |
| 資材 | `add_material`, `get_material` | セッション中の成果物をタグ付き独立エンティティとして保存・取得 |
| リレーション | `add_relation`, `remove_relation`, `get_map` | エンティティ間の関連の追加・削除・グラフ探索 |
| 振る舞い | `add_habit`, `get_habits`, `update_habit` | check-in時に注入される運用ルールの管理 |
| タグ | `search_tags`, `update_tag`, `analyze_tags` | タグの検索・タグ情報の更新・タグ共起分析 |
| 検索 | `search`, `get_by_ids` | キーワード横断検索・詳細情報の取得 |

## スキル

| スキル | 説明 |
|--------|------|
| `/guide` | cc-memoryの使い方をAIが説明します |
| `/sync-memory` | セッション終了前にtranscriptを解析し、トピック・決定事項・アクティビティを一括で記録・更新します |
| `/check-in` | アクティビティにcheck-inして関連情報を集約取得します |
| `/tag-notes` | タグのnotesを確認・更新します |
| `/tag-cleanup` | タグの共起分析を実行し、整理提案をユーザーに提示します |
| `/scribe` | cc-memoryの記録からドキュメントを生成します |
| `/postmortem` | completedアクティビティを振り返り、教訓を永続化します |

## 設定

`.mcp.json`の`env`フィールドで以下の環境変数を設定すると、デフォルト値をオーバーライドできます。未設定の項目はデフォルト値で動作するため、ゼロコンフィグで使用可能です。

| 環境変数名 | デフォルト | 説明 |
|-----------|-----------|------|
| `CCM_DB_PATH` | `~/.claude/.claude-code-memory/discussion.db` | データベースファイルのパス |
| `CCM_HEARTBEAT_TIMEOUT` | `20` | ホットアクティビティ判定の閾値（分） |
| `CCM_IN_PROGRESS_LIMIT` | `3` | アクティブコンテキストのin_progress表示件数 |
| `CCM_PENDING_LIMIT` | `2` | アクティブコンテキストのpending表示件数 |
| `CCM_RECENCY_DECAY_RATE` | `0.0014` | 検索の時間減衰率 |
| `CCM_SYNC_DISABLE_RETROSPECTIVE` | `false` | `/sync-memory`のふりかえりセクションを非表示にする |

<details>
<summary>リモートサーバー（claude.aiから接続）</summary>

claude.ai（Web版）からcc-memoryに接続するためのリモートサーバー構成。Cloudflare TunnelでHTTPS公開し、GitHub OAuthで認証する。

### 1. cloudflaredのインストール

```bash
brew install cloudflared
```

### 2. GitHub OAuth App作成

1. [GitHub → Settings → Developer settings → OAuth Apps → New OAuth App](https://github.com/settings/applications/new)
2. 以下を設定:
   - **Application name**: `cc-memory`（任意）
   - **Homepage URL**: CF Tunnelの公開URL（例: `https://cc-memory.example.com`）
   - **Authorization callback URL**: `<公開URL>/auth/callback`
3. Client IDとClient Secretを控える

### 3. 環境変数の設定

```bash
export GITHUB_CLIENT_ID="your-client-id"
export GITHUB_CLIENT_SECRET="your-client-secret"
export CC_MEMORY_BASE_URL="https://cc-memory.example.com"
export CC_MEMORY_ALLOWED_USERS="your-github-username"  # カンマ区切りで複数指定可
# export CC_MEMORY_REMOTE_PORT="8001"  # デフォルト: 8001
```

`CC_MEMORY_ALLOWED_USERS`に含まれないGitHubユーザーはOAuth認証後にアクセスが拒否される。

### 4. Cloudflare Tunnelのセットアップ

```bash
# 初回のみ: Cloudflareにログイン（ブラウザが開く）
cloudflared login

# トンネル作成
cloudflared tunnel create cc-memory
cloudflared tunnel route dns cc-memory cc-memory.example.com

# config.ymlに以下を追加
# tunnel: <tunnel-id>
# credentials-file: ~/.cloudflared/<tunnel-id>.json
# ingress:
#   - hostname: cc-memory.example.com
#     service: http://localhost:8001
#   - service: http_status:404
```

### 5. 起動

```bash
# リモートサーバー起動
uv run python -m src.remote

# 別ターミナルでCF Tunnel起動
cloudflared tunnel run cc-memory
```

### 6. claude.aiから接続

claude.ai → Settings → Integrations → Add Integration からリモートサーバーのURLを追加する。

</details>

## ライセンス

MIT
