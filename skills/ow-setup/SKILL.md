---
name: ow-setup
description: owフレームワーク（orch/dispatcher/worker）を実行できる状態にする自動セットアップ。「/ow-setup」「owセットアップ」「ow使えるようにして」「orch起動できない」「worker起動失敗」「RELAY_UNAVAILABLE直したい」などで発動する。既にorch/dispatcher/worker skillが稼働中のセッションでは発動しない。
---

# ow-setup

owフレームワーク（orch/dispatcher/worker）を実行できる状態にするための、Claude自身が行う自動セットアップスキル。

初回導入・環境破損時の再確認・旧設定の移行のいずれのケースでも発動する。ユーザーが手順書を読んで手動セットアップする形式ではなく、Claudeが環境をスキャンして、修復可能なものは直接直し、承認が要るものは対話で確認する。

## スコープ

本スキルは **local relay (port 8765) と local MCP server (port 52837)** のみを扱う。remote server（Cloudflare Tunnel 経由の `mcp.<domain>` / launchd 常駐）は扱わない。

## 前提知識

セットアップ判定のため、以下の現行仕様を踏まえる。

- relayサーバーは cc-memory リポ内 `src/relay/server.py` に **vendoring 済み**。外部clone不要
- relayは MCP tool（`ow_status` など）の初回呼び出しで `ensure_relay_server()` が自動起動する。launchd常駐は不要
- orch/dispatcher/worker/worker-sync skill は cc-memoryプラグイン同梱
- worker skill は `~/workspace/cc-memory/scripts/ow/heartbeat.sh` `recv.sh` を **絶対パス参照** する
- `OW_TERMINAL` の default は `tmux`。`manual` にしたい場合のみ明示設定する
- 過去仕様（`CCM_OW=1` / `OW_TASK_FILE` / `OW_QUEUE_DIR` / `OW_ORCH_CWD` / iterm2アダプタ）はすべて廃止

## 発動判定

以下いずれかを満たすなら本スキルを実行しない（別 skill に譲る）:

- `env` に `OW_ROLE` が set されている（自セッションが worker として起動している）
- 現セッションで既に orch / dispatcher / worker skill が稼働中である
- `ow_status(channel=<現用channel>)` の presence に自分の handle がある

## 実行フロー

### Step 1: 環境スキャン（読み取りのみ）

以下を Bash で確認し、各項目の状態を収集する。この段階では何も変更しない。テーブル内の `\|` は GFM エスケープなので、実行時は `|` に読み替える。

チェック項目:

| # | チェック | 判定コマンド | 期待 |
|---|---|---|---|
| 1 | cc-memoryリポ配置 | `test -f ~/workspace/cc-memory/scripts/ow/heartbeat.sh` | 存在 |
| 2 | Claude Code バージョン | `claude --version` | v2.0.12+ |
| 3 | Python 3.12+ | `python3 --version` | 3.12以上 |
| 4 | SQLite拡張ロード対応 | `python3 -c "import sqlite3; sqlite3.connect(':memory:').enable_load_extension(True)"` | エラーなし |
| 5 | uv | `command -v uv` | 存在 |
| 6 | tmux | `command -v tmux` | 存在 |
| 7 | curl | `command -v curl` | 存在 |
| 8 | lsof（推奨） | `command -v lsof` | 存在（無くても relay 起動は可、port占有復旧不可） |
| 9 | pgrep | `command -v pgrep` | 存在 |
| 10 | mkfifo / mktemp | `command -v mkfifo && command -v mktemp` | 両方存在 |
| 11 | `~/.tmux.conf` 推奨設定 | `test -f ~/.tmux.conf && grep -q 'allow-passthrough' ~/.tmux.conf && grep -q 'extended-keys' ~/.tmux.conf` | 両方hit |
| 12 | `~/.cc-memory/ow` 書き込み権限 | `mkdir -p ~/.cc-memory/ow && test -w ~/.cc-memory/ow` | OK |
| 13 | プラグインキャッシュ配置 | `test -d ~/.claude/plugins/cache/claude-code-memory-marketplace` | 存在 |
| 14 | `ow_*` MCP tool 可用性 | 本セッションで `ow_status` が可視か | 利用可能 |
| 15 | セッション env の廃止envの残存 | `env \| grep -E 'CCM_OW\|OW_TASK_FILE\|OW_QUEUE_DIR\|OW_ORCH_CWD'` | 該当なし |
| 16 | `.mcp.json` の廃止env残存 | `jq -r '.mcpServers // {} \| to_entries[] \| select(.value.env) \| .value.env \| keys[]' .mcp.json ~/.claude/settings.json 2>/dev/null` | 廃止envなし |

チェック #15 と #16 は「見る場所が違うので両方必要」: #15 は現在のシェル環境、#16 は MCP server プロセスに注入される env（`.mcp.json` の `env:` フィールド）。廃止 env はどちらに残っていても再確認する。

### Step 2: 結果分類

各チェックを3カテゴリに振り分ける。

- **OK**: 期待通り
- **警告**（動作はするが推奨と異なる）: `~/.tmux.conf` 推奨設定なし / 廃止envが残存 / lsof不在 など
- **要修復**（動作しない）: 依存コマンド不在（tmux/python3/uv/curl/mkfifo/mktemp/pgrep）/ Python 3.12未満 / cc-memoryリポ未配置 / 書き込み権限なし / プラグインキャッシュ不在

### Step 3: 修復（対話しながら実行）

修復対象を性質別に扱う。

**軽量副作用（自動修復可、承認不要）**

- `~/.cc-memory/ow` の `mkdir -p`

**承認要（変更前に diff 提示）**

- `.mcp.json` 編集: `jq` で該当エントリのみ差し替える。cc-memory以外の `mcpServers` エントリを触らない。全書き換え diff は提示しない
- `~/.tmux.conf` 追記: 既存の設定行を `grep -c` で確認し、重複なら追記しない。`set -g allow-passthrough off` などの明示反対設定があれば上書き判断をユーザーに委ねる
- cc-memoryリポの配置修正: `readlink ~/workspace/cc-memory` で既存 symlink を確認する。分岐:
  - 存在しない → `ln -s <実リポの絶対パス> ~/workspace/cc-memory`
  - 既に別リポ（実ディレクトリ）が置かれている → ユーザーに移動 or リネームを提案してから symlink
  - 壊れた symlink がある → `ln -sfn <実パス> ~/workspace/cc-memory` で張り替え
- プラグインキャッシュ再生成: `rm -rf ~/.claude/plugins/cache/claude-code-memory-marketplace/` + セッション再起動案内。実行前にユーザー承認

**提案のみ（Claudeは実行しない）**

- `brew install tmux` `brew install python@3.12` `brew install uv` などの外部インストール。コマンドを表示してユーザー自身に実行してもらう

修復のたびに Step 1 の該当チェックを再実行して結果を確定する。外部インストール待ちなど修復不能なブロッカーが残ったら、その時点でユーザーに報告して以降のステップをスキップする。

### Step 4: 動作確認（疎通テスト）

依存が満たされたら、疎通確認を行う。

1. `ow_status(channel="ow-setup-probe")` を呼ぶ
   - 成功なら relay起動・`ensure_channel`・sentinel起動まで動作 → 疎通OK
   - `RELAY_UNAVAILABLE` エラーなら port 8765 の占有プロセスを確認し（`lsof -i :8765 -sTCP:LISTEN`）Step 3 に戻る
2. `ow_send` でテスト envelope を1件送る。最小 payload の例:

   ```
   ow_send(
     channel="ow-setup-probe",
     handle="setup-probe",
     body={
       "v":1, "kind":"event", "from":"setup-probe", "to":"*",
       "data":{"type":"identity","role":"user","alias":"setup-probe"}
     }
   )
   ```

   送信できれば relay `/send` が動作
3. `ow_history(channel="ow-setup-probe", since=0)` で送った envelope が読めることを確認

疎通テストの副作用として `ow-setup-probe` channel 用の sentinel プロセスが1本残る（`pgrep -f "sentinel.py.*ow-setup-probe"` で確認可能）。実運用への影響はないので放置してよい。気になる場合はユーザーが `pkill -f "sentinel.py.*ow-setup-probe"` で終了できる。

### Step 5: 完了報告

以下を Markdown で報告する。

- Step 1 全チェック結果一覧（OK / 警告 / 要修復）
- 修復した項目、しなかった項目（承認拒否・外部インストール待ち）
- 疎通テスト結果、および残った sentinel プロセスの有無
- 次のステップ案内: 「`/orch`（引数なし、または自然言語 / topic ID 指定）で orch を起動できる」

## チェック項目リファレンス

### 依存コマンドと用途

| コマンド | 使われる場所 | 用途 |
|---|---|---|
| tmux | `scripts/ow/adapters/tmux.sh`、`ow_spawn_worker` | worker起動先ターミナル |
| python3 (3.12+) | `src/relay/server.py`、`scripts/ow/sentinel.py` | relay + stagnation detector |
| uv | `.mcp.json`、sentinel spawn | パッケージランナー |
| curl | `recv.sh`、`heartbeat.sh` | relay HTTP呼び出し |
| lsof（推奨） | `ow_service._clear_relay_port` | port 8765 占有時の kill+restart |
| pgrep | sentinel重複起動判定 | プロセス発見 |
| mkfifo / mktemp | `recv.sh` | SSE受信バッファ |

### 環境変数（現行）

| 変数 | 意味 | default |
|---|---|---|
| `OW_TERMINAL` | worker起動ターミナル | `tmux`（未指定時） |
| `RELAY_URL` | relay HTTP endpoint | `http://127.0.0.1:8765` |
| `OW_MCP_URL` | worker heartbeat が MCP `/health` を叩く URL | `http://127.0.0.1:52837` |

### 廃止された環境変数（残っていたら削除案内）

- `CCM_OW` — 廃止。ow_* toolは常時有効
- `OW_TASK_FILE` — 廃止。spawn-bundle envelope に置換
- `OW_QUEUE_DIR` — 廃止。queue.md 運用が廃止
- `OW_ORCH_CWD` — 廃止。crash復旧経路が変わった
- `OW_TERMINAL=iterm2` — 廃止。tmuxアダプタのみ

## トラブルシューティング

### RELAY_UNAVAILABLE エラー

症状: `ow_*` tool 呼び出しで `RELAY_UNAVAILABLE` が返る。

背景: `ensure_relay_server()` が relay を kickstart するが、port 8765 に別プロセスが張り付いている場合、`lsof` が使える環境なら自動 kill+restart する。`lsof` がない場合は kill できずに失敗する。

確認と対処:

```bash
lsof -i :8765 -sTCP:LISTEN   # 占有プロセスの特定（lsof がある場合）
```

- `lsof` がない: `brew install lsof` などで導入するか、占有プロセスを手動で終了
- port が空いているのに失敗する: `~/.cc-memory/ow/` の書き込み権限を確認

### `ow_*` ツールが表示されない

症状: セッション内で `ow_status` 等の MCP tool が見えない。

対処:

1. cc-memoryプラグインがインストールされていることを確認
2. プラグインキャッシュを再生成: `rm -rf ~/.claude/plugins/cache/claude-code-memory-marketplace/`
3. Claude Code セッションを再起動

### tmux 内で worker が起動しない

症状: `OW_TERMINAL=tmux` で `ow_spawn_worker` を呼んだが新windowにworkerが立ち上がらない。

確認: `~/.tmux.conf` に以下があるか。

```
set -g allow-passthrough on
set -g extended-keys on
```

対処: 追記して `tmux source-file ~/.tmux.conf` でリロード、または tmux を再起動する。

### cc-memoryリポが `~/workspace/cc-memory` にない

症状: worker起動時に `heartbeat.sh: No such file or directory`。

背景: `skills/worker/SKILL.md` が `~/workspace/cc-memory/scripts/ow/heartbeat.sh` を絶対パス参照している。

対処: 実リポパスから symlink を張る。

```bash
mkdir -p ~/workspace
ln -sfn <実リポの絶対パス> ~/workspace/cc-memory
readlink ~/workspace/cc-memory   # 貼れたか確認
```

## 注意

- `.mcp.json` の書き換えは `jq` で該当キーのみ差し替える。cc-memory以外の `mcpServers` エントリを壊さない
- `~/.tmux.conf` に既存設定があれば、追記行の位置と重複を確認してから差分適用する
- 疎通テストで残る sentinel プロセス（`ow-setup-probe` channel 用）は無害。気になる場合のみ `pkill` で終了
- ow の状態モデルは進化しており、将来 `ow_status` 返却フォーマット等の仕様変更があれば本スキルの Step 4 判定基準を再確認する
- 既に orch/dispatcher/worker として稼働中のセッションでは本スキルは発動しない。「発動判定」章の条件を先に確認する
