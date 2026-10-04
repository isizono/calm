# リモートサーバー（claude.aiから接続）

[← README](../README.md)

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
export CALM_BASE_URL="https://cc-memory.example.com"
export CALM_ALLOWED_USERS="your-github-username"  # カンマ区切りで複数指定可
# export CALM_REMOTE_PORT="8001"  # デフォルト: 8001
```

`CALM_ALLOWED_USERS`に含まれないGitHubユーザーはOAuth認証後にアクセスが拒否される。

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
