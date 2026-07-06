# relay v2 サーバー運用手順

relay v2 サーバー（`uvicorn relay.app:app`）は cc-memory から独立したプロセスとして常駐させる。cc-memory server 本体は relay に対して 1 identity として接続する。

## 前提

- Python 3.11+
- relay リポジトリ（`git@github.com:isizono/relay.git`）が cwd から見えていること
- `relay` パッケージが install 済み（`uv sync` 済み）

## 起動コマンド（開発環境）

```bash
export RELAY_AUTH_TOKENS='{"<bearer-token>": "cc-memory"}'
export RELAY_DB_PATH="$HOME/.cc-memory/relay/relay-server.db"
mkdir -p "$(dirname "$RELAY_DB_PATH")"
uv run uvicorn relay.app:app --host 127.0.0.1 --port 8770
```

- `RELAY_AUTH_TOKENS` は `{"<token>": "<identity>"}` の JSON。cc-memory server は `RELAY_BEARER_TOKEN` に `<token>` を、`RELAY_IDENTITY` に `<identity>`（省略時は `cc-memory`）を設定する。
- `RELAY_DB_PATH` は outbox / publish_log / agent_cards の永続化先。subscription / stream registry は in-memory なので server 再起動で消える（cc-memory 側の B-2 lease loop が自己修復する）。
- ポート番号は relay v1（既存 `python -m src.relay.server` の 8765）と衝突しない値を選ぶ。上例では 8770 を使う。

## cc-memory 側の設定

cc-memory server（`python -m src.main --transport http`）は以下の env を認識する:

| 環境変数 | 説明 | 既定値 |
|---|---|---|
| `RELAY_BASE_URL` | relay サーバーの base URL | `http://localhost:8770` |
| `RELAY_BEARER_TOKEN` | Bearer token（未設定なら relay v2 は未導入扱い） | なし |
| `RELAY_IDENTITY` | subscribe 時の subscriber、stream の名前空間 | `cc-memory` |
| `RELAY_STATE_DIR` | declaration / inbox / cursor の置き場 | `~/.cc-memory/relay` |

`RELAY_BEARER_TOKEN` が未設定のとき cc-memory は常駐 3 系統 thread を起動しない（log 1 行のみで縮退）。この状態でも server 本体の起動と既存機能は影響を受けない。relay を使う MCP tool（`relay_post` / `relay_publish` / `relay_subscribe` / `relay_receive`）は明示的な `config_missing` エラーを返す。

## macOS launchd で常駐化する例

`~/Library/LaunchAgents/com.isizono.relay-v2.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.isizono.relay-v2</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/YOU/.local/bin/relay-v2.sh</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>RELAY_AUTH_TOKENS</key>
        <string>{"REPLACE_ME": "cc-memory"}</string>
        <key>RELAY_DB_PATH</key>
        <string>/Users/YOU/.cc-memory/relay/relay-server.db</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/YOU/.cc-memory/relay/relay-server.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/YOU/.cc-memory/relay/relay-server.log</string>
</dict>
</plist>
```

`~/.local/bin/relay-v2.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
cd /Users/YOU/workspace/relay
exec uv run uvicorn relay.app:app --host 127.0.0.1 --port 8770
```

登録・起動:

```bash
chmod +x ~/.local/bin/relay-v2.sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.isizono.relay-v2.plist
launchctl kickstart -k gui/$(id -u)/com.isizono.relay-v2
```

## セッション側 watcher

`scripts/relay/watch_inbox.sh <session_id>` で session 単位の inbox JSONL を `tail -F` する。business logic を含まない tail wrapper なので、Monitor ツール等の外部 watcher と組み合わせて使う。

```bash
scripts/relay/watch_inbox.sh sess-abc123
```

inbox path は `RELAY_STATE_DIR`（未設定なら `~/.cc-memory/relay`）配下の `inbox/session-<safe_session_id>.jsonl`。cc-memory server 本体と env を合わせて使うこと。

`<session_id>` に渡す値は `relay_publish` / `relay_subscribe` / `relay_receive` の返り値の `identity` フィールドから取得できる。この値は launcher.py（Claude Code CLI と cc-memory server を繋ぐ stdio ブリッジ）が発行する bridge identity であり、cc-memory server の再起動をまたいで不変（Claude Code セッション自体を再起動しない限り変わらない）。

### launcher の bridge identity・生存管理 env

| 環境変数 | 説明 | 既定値 |
|---|---|---|
| `CC_MEMORY_LAUNCHER_HEARTBEAT_SEC` | launcher.py が `/session/register` を再送する間隔（秒） | `60` |
| `CC_MEMORY_SESSION_LIVENESS_TIMEOUT_SEC` | SessionManager が heartbeat 途絶から liveness TTL 失効までの猶予（秒）。`0` で無効化 | `300` |

## トラブルシューティング

- **cc-memory 起動時のログに「RELAY_BEARER_TOKEN が未設定のため RelayRuntime を起動しません」と出る**: cc-memory を起動する launchd / shell に `RELAY_BEARER_TOKEN` を注入する。`launchctl setenv RELAY_BEARER_TOKEN <token>` は再起動で消えるため、`~/Library/LaunchAgents/com.isizono.cc-memory-remote.plist` の `EnvironmentVariables` に書く。
- **relay 起動後も cc-memory が SSE 接続に失敗する**: `RELAY_BASE_URL` の port が relay server と一致しているか確認する。cc-memory 側の既定は 8770、relay v1 の既定は 8765。
- **declaration file が増え続ける**: cc-memory server の B-2 lease loop が「lease_expires_at の最大値が 24 時間以上前」の declaration file を定期的に削除する（起動時 1 回 + 1 時間毎）。それでも増える場合は該当 session が生存していて renew が回っている可能性がある（生存判定は `SessionManager.session_ids`。SIGKILL 等で launcher が異常終了した場合も `CC_MEMORY_SESSION_LIVENESS_TIMEOUT_SEC` 経過後に自動で対象から外れる）。
- **relay v1（既存 `python -m src.relay.server`）と併走させたい**: v1 と v2 で port が異なれば同時起動可能。cc-memory server は `RELAY_BASE_URL` だけを見るため、v2 未導入の状態では `RELAY_BEARER_TOKEN` を未設定にしておけば v1 のみの環境として動く。
