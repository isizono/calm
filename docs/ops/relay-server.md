# relay v2 サーバー運用手順

relay v2 サーバー（`uvicorn relay.app:app`）は CALM から独立したプロセスとして常駐させる。cc-memory server 本体は relay に対して 1 identity として接続する。

## 前提

- Python 3.11+
- relay リポジトリ（`git@github.com:isizono/relay.git`）が cwd から見えていること
- `relay` パッケージが install 済み（`uv sync` 済み）

## Bearer token 配布方式

cc-memory が relay へ接続するための Bearer token は、**招待URL方式**で配布する。

1. relay 側で招待URLを1つ発行する（`python -m relay.invite new`）。
2. 人間が招待URLをコピーする。
3. cc-memory 側でその招待URLを redeem する（`python -m src.services.relay.redeem`）。redeem に成功すると `~/.cc-memory/relay/credential.json`（0600）が生成され、以後 cc-memory はこのファイルから token / base_url / identity を読む。

招待URLは `<base>/invitations/redeem#v=1&t=it_<token>` の形式で、secret（`t=`）は URL の fragment に置かれる。fragment は HTTP GET でサーバーへ送信されないため、チャットやメールの link-preview bot に URL を踏まれても招待は消費されない。招待URLは 15 分・一回限りで失効する。

環境変数 `RELAY_BEARER_TOKEN` / `RELAY_BASE_URL` / `RELAY_IDENTITY` による直接設定は残っている（下記「cc-memory 側の設定」）。これは break-glass 用の代替経路で、通常運用では招待URL方式を使う。env が設定されていると credential.json より優先される。

## 起動コマンド（開発環境・break-glass）

招待URL方式を使わずに素早く動作確認したい場合の経路。

```bash
export RELAY_AUTH_TOKENS='{"<bearer-token>": "cc-memory"}'
export RELAY_DB_PATH="$HOME/.cc-memory/relay/relay-server.db"
mkdir -p "$(dirname "$RELAY_DB_PATH")"
uv run uvicorn relay.app:app --host 127.0.0.1 --port 8770
```

- `RELAY_AUTH_TOKENS` は `{"<token>": "<identity>"}` の JSON。cc-memory server は `RELAY_BEARER_TOKEN` に `<token>` を、`RELAY_IDENTITY` に `<identity>`（省略時は `cc-memory`）を設定する。
- `RELAY_DB_PATH` は outbox / publish_log / agent_cards（および招待URL方式導入後は invitations / credentials）の永続化先。subscription / stream registry は in-memory なので server 再起動で消える（cc-memory 側の B-2 lease loop が自己修復する）。
- ポート番号は他プロセスと衝突しない値を選ぶ。上例では 8770 を使う。
- 常駐運用（launchd）では `RELAY_DB_PATH` を `~/.local/state/relay/relay.db` に統一する（下記「macOS launchd で常駐化する例」）。git / worktree の churn から独立させるため。

## cc-memory 側の設定

cc-memory server（`python -m src.main --transport http`）は以下の env を認識する。値は env → `credential.json` → 既定 の順で解決される。

| 環境変数 | 説明 | 既定値 |
|---|---|---|
| `RELAY_BASE_URL` | relay サーバーの base URL | `http://localhost:8770`（credential.json があれば redeem 時の URL の scheme+host） |
| `RELAY_BEARER_TOKEN` | Bearer token（env・credential.json とも未設定なら relay v2 は未導入扱い） | なし |
| `RELAY_IDENTITY` | subscribe 時の subscriber、stream の名前空間 | `cc-memory`（credential.json があればそこに書かれた identity） |
| `RELAY_STATE_DIR` | declaration / inbox / cursor / **credential.json** の置き場 | `~/.cc-memory/relay` |

`get_token()` が env・credential.json のいずれからも token を得られないとき、cc-memory は常駐 3 系統 thread を起動しない（log 1 行のみで縮退）。この状態でも server 本体の起動と既存機能は影響を受けない。relay を使う MCP tool（`relay_post` / `relay_publish` / `relay_subscribe` / `relay_receive`）は明示的な `config_missing` エラーを返す。**招待URL redeem 前のこの縮退は初回ブートの正常な挙動であり、誤報ではない。**

### outbox dispatcher の retry 既定値

`relay_publish` の配達は outbox 経由の非同期 retry（Full Jitter backoff）で行われる。以下の環境変数で retry 挙動を調整できる。

| 環境変数 | 説明 | 既定値 |
| --- | --- | --- |
| `RELAY_OUTBOX_RETRY_BACKOFF_BASE_MS` | retry バックオフの基準値（ミリ秒） | `1000` |
| `RELAY_OUTBOX_RETRY_BACKOFF_CAP_S` | retry バックオフの上限（秒） | `300` |
| `RELAY_OUTBOX_TRANSIENT_RETRY_DEADLINE_S` | TransientError を諦めるまでの合計時間（秒） | `86400`（24 時間） |

これらは relay_sdk パッケージが提供する env 解決ヘルパーであり、cc-memory 組み込みの dispatcher（`RelayRuntime`）にも、スタンドアロン CLI（`python -m relay_sdk.outbox`）にも同じ環境変数名・同じ既定値で効く。cc-memory 組み込み側は独自の既定値を持たない（relay server の手動再起動断絶は既定の 24 時間デッドラインで十分に生き延びられるため）。

## 招待URL発行・redeem 手順（推奨経路）

### 初回セットアップ

1. **relay 側で招待URLを発行する**（relay リポジトリの cwd で）:

   ```bash
   python -m relay.invite new --identity cc-memory
   ```

   標準出力に招待URLが1行出る（例: `http://127.0.0.1:8770/invitations/redeem#v=1&t=it_...`）。DB の場所は `--db` 明示 → env `RELAY_DB_PATH` → 既定の canonical 絶対パス `~/.local/state/relay/relay.db` の順で解決される。対話 shell に `RELAY_DB_PATH` を export する必要はない（launchd が起動時に設定する env は対話 shell には伝播しないため、CLI 側は cwd 相対パスへフォールバックせずこの canonical パスを既定にしている）。

2. **招待URLをコピーする。**

3. **cc-memory 側で redeem する**（cc-memory リポジトリの cwd で）:

   ```bash
   pbpaste | python -m src.services.relay.redeem
   ```

   `echo '<URL>' | python -m src.services.relay.redeem` は使わない。`echo` は builtin でもコマンドライン全体が `~/.zsh_history` に平文で残り、招待URL（＝capability）がシェル履歴に永続してしまう。`pbpaste` でクリップボードから直接渡す。

   成功すると `~/.cc-memory/relay/credential.json`（0600、親 dir 0700）が生成され、identity と expires_at が標準出力に表示される。

4. **cc-memory の relay クライアントを再起動して credential を拾わせる。** relay に接続するのは **ローカル http プロセス（port 52837、`src.launcher` が起動する `src.main --transport http`）ただ1個**である。

   ```bash
   lsof -ti tcp:52837 -sTCP:LISTEN | xargs kill; uv run python -m src.launcher &
   ```

   **remote プロセス（port 8001, `com.isizono.cc-memory-remote`）は relay クライアントを一切起動しないため、再起動しても relay 接続には無関係。** relay を有効化する手段として remote 再起動を代用しないこと（サイレントに繋がらないまま気づかない事故になる）。

5. 疎通確認: cc-memory の relay tool（`relay_post` 等）が `config_missing` を返さないこと、relay ログに `invite_redeemed` が出ていること。

### 失効・再発行

- **失効**: relay 側で

  ```bash
  python -m relay.invite revoke --identity cc-memory
  ```

  ただし revoke は DB に `revoked_at` をセットするだけで、稼働中 relay の in-memory 認証テーブルは変えない。**revoke 単独では失効しない。** 反映には relay 再起動（`launchctl kickstart -k gui/$(id -u)/com.isizono.relay-v2`）まで含めて1手順である。

- **再発行A（credential.json の紛失・破損からの復旧、漏洩なし）**: 新 credential を追加発行するだけでよい。**旧 credential は無効化されない**（既定無期限のため自然失効もしない）。定期入替（rotation）や「旧鍵を殺す」目的には使えない — その場合は必ず再発行Bを使う。relay 再起動は不要。

  ```bash
  # relay 側
  python -m relay.invite new --identity cc-memory
  ```
  ```bash
  # cc-memory 側（招待URLをコピー後）
  pbpaste | python -m src.services.relay.redeem
  lsof -ti tcp:52837 -sTCP:LISTEN | xargs kill; uv run python -m src.launcher &
  ```

- **再発行B（credential 漏洩時、失効が必須）**: 漏洩した bearer を確実に無効化する。**relay 再起動を省くと漏洩 bearer が認証テーブルに残り、認証を通し続ける**（revoke は `revoked_at` 列を立てるだけで、稼働中プロセスの照合ロジックはその列を見ない）。

  ```bash
  # 1. relay 側: revoke
  python -m relay.invite revoke --identity cc-memory

  # 2. relay 再起動（漏洩 token を in-memory 認証テーブルから除去。省略厳禁）
  launchctl kickstart -k gui/$(id -u)/com.isizono.relay-v2

  # 3. relay 側: 新規招待発行
  python -m relay.invite new --identity cc-memory
  ```
  ```bash
  # 4. cc-memory 側（招待URLをコピー後）
  pbpaste | python -m src.services.relay.redeem
  lsof -ti tcp:52837 -sTCP:LISTEN | xargs kill; uv run python -m src.launcher &
  ```

## macOS launchd で常駐化する例（秘密ゼロ plist）

credential は relay の DB 側と cc-memory の credential.json 側にのみ存在させ、plist / 起動スクリプトには一切 secret を書かない。

`~/.local/bin/relay-v2.sh`:

```sh
#!/bin/sh
# relay-v2.sh — 秘密を一切持たない。credential は DB 側に置く。
# umask 077: SQLite が open のたびに再生成する -wal/-shm を含め、新規生成ファイル
# 全てを 0600 相当に強制する（一回きりの chmod では再起動ごとに 0644 の窓が開く）。
umask 077
cd "$HOME/workspace/relay"
install -m 700 -d "$HOME/.local/state/relay"
export RELAY_DB_PATH="$HOME/.local/state/relay/relay.db"
export RELAY_SERVER_LOG_PATH="$HOME/.local/state/relay/relay-server.jsonl"
exec uv run uvicorn relay.app:app --host 127.0.0.1 --port 8770
```

```bash
chmod +x ~/.local/bin/relay-v2.sh
```

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
        <string>/bin/sh</string>
        <string>/Users/YOU/.local/bin/relay-v2.sh</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/Users/YOU/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/YOU/.local/state/relay/relay-v2.out.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/YOU/.local/state/relay/relay-v2.err.log</string>
</dict>
</plist>
```

- `RELAY_AUTH_TOKENS` は plist に置かない（動的 credential は DB から起動時に読み込まれる）。
- `EnvironmentVariables` の `PATH` に `uv` の場所とツールチェーンを明示すること。launchd の最小 PATH には `uv` が含まれず、無いと `uv run` が not found で起動失敗する。
- bind は `127.0.0.1`（ネットワーク非露出。最重要かつ無償の一次制御）。

登録・起動:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.isizono.relay-v2.plist
launchctl kickstart -k gui/$(id -u)/com.isizono.relay-v2
```

起動直後に `ls -l ~/.local/state/relay/` で `relay.db` 系ファイルが 0600 になっていることを確認する（`umask 077` が効いていれば追加操作は不要。0644 が見えたら `chmod 600 ~/.local/state/relay/relay.db*` で是正する）。

### 既存プロセスからの切り替え（cutover）時の注意

- 新旧の relay は同一 port を bind するため同時起動できない。切り替え前に旧プロセスの listener を確実に停止し、port を解放してから launchd を bootstrap すること。`kill <PID>` を `uv run` ラッパの PID に対して行っても、実 listener が子プロセスとして port を保持し続けることがある。listener は次のように特定して落とす:

  ```bash
  lsof -ti tcp:8770 -sTCP:LISTEN | xargs kill
  lsof -ti tcp:8770 -sTCP:LISTEN   # 空になったことを確認してから次へ進む
  ```

  port が空かないまま `launchctl bootstrap` すると bind 失敗（`Address already in use`）→ `KeepAlive=true` で crash-loop する。

- 旧構成が環境変数 `RELAY_AUTH_TOKENS` の静的表（例: `claude-main` identity）で認証していた場合、新構成（`RELAY_AUTH_TOKENS` 未設定・新 DB は空）へ切り替えると、その identity で接続中の全クライアントが無警告で 401 になる。切り替え前に静的表利用の全 identity を棚卸しし、各々について「招待URLで DB credential として再発行する」「break-glass として plist に温存する」「明示的に破棄する」のいずれかを決めること。

### federation identity/JWE 機能のロールアウト順序

relay が federation 配達 payload に `publisher_identity`（`sub@handle` 形式）を刻印する変更と、cc-memory 側がそれを見て federation 由来メッセージを `is_federation_origin` / `trust_notice` でマークする変更は、別リポジトリの別々のデプロイ操作である。cc-memory 側のマーキングは `publisher_identity` フィールドの有無だけで federation 由来かどうかを判定するため、relay 側の刻印がまだ反映されていない relay に接続した状態で cc-memory 側だけ先に更新すると、該当フィールド自体が届かず federation 由来メッセージが無警告で local 扱いのまま通過する（fail-open）。

デプロイは必ず relay → cc-memory の順で行うこと。逆順にすると、両者が揃うまでの間マーキングが機能しない窓ができる。

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

- **cc-memory 起動時のログに token 未設定の縮退が出る（招待URL redeem 前）**: 初回ブートで credential.json がまだ無い状態のこの縮退は正常な挙動。`python -m relay.invite new` → `python -m src.services.relay.redeem` で credential を取得すれば解消する。
- **redeem に成功した（`credential.json` は存在する）のに relay tool が `config_missing` を返し続ける**: credential.json の更新は cc-memory の再起動を挟まないと反映されない（`get_token()` は呼び出しごとに読むが、`RelayRuntime` の起動判定はプロセス起動時に一度だけ評価されるため）。`lsof -ti tcp:52837 -sTCP:LISTEN | xargs kill; uv run python -m src.launcher &` でローカル http プロセスを再起動する。
- **credential.json はあるのに relay が 401 を返し続ける**: revoke 済みの bearer を掴んだままの状態（再発行Bの手順を最後まで完遂していない、または cc-memory の再起動を忘れている）。`RelayRuntime` の再接続ループは指数バックオフ（上限30秒）で無限に再試行し続けるため、プロセスは落ちないが noisy な縮退が続く。再発行A（redeem し直し）を行い、必ず cc-memory（ローカル http 52837）を再起動する。
- **credential.json と runtime の参照先がずれて `config_missing` になる**: redeem CLI の書込先と runtime の読取先はどちらも `RELAY_STATE_DIR`（既定 `~/.cc-memory/relay`）に一致している前提。どちらか一方だけで `RELAY_STATE_DIR` を override すると発症が分かりにくいズレが生じる。既定のまま揃えることを推奨する。
- **`python -m relay.invite new` で発行した招待を redeem すると常に 404 になる**: invite CLI と稼働中 relay server が別々の DB ファイルを見ている可能性が高い。invite CLI の DB 解決は `--db` → env `RELAY_DB_PATH` → canonical 絶対パス `~/.local/state/relay/relay.db` の順で、cwd 相対パスにはフォールバックしない。稼働中 relay の `RELAY_DB_PATH`（launchd plist または `relay-v2.sh` の設定）と一致していることを確認する。
- **relay 起動後も cc-memory が SSE 接続に失敗する**: `RELAY_BASE_URL`（または credential.json の `base_url`）の port が relay server と一致しているか確認する。cc-memory 側の既定は 8770。
- **declaration file が増え続ける**: cc-memory server の B-2 lease loop が「lease_expires_at の最大値が 24 時間以上前」の declaration file を定期的に削除する（起動時 1 回 + 1 時間毎）。それでも増える場合は該当 session が生存していて renew が回っている可能性がある（生存判定は `SessionManager.session_ids`。SIGKILL 等で launcher が異常終了した場合も `CC_MEMORY_SESSION_LIVENESS_TIMEOUT_SEC` 経過後に自動で対象から外れる）。
