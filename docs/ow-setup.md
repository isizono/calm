# ow（orch/worker）フレームワーク セットアップガイド

owは、cc-memory上でオーケストレーター（orch）と複数のワーカー（worker）を協調させて、タスクを並行処理するフレームワークである。このガイドでは、新規ユーザーが /orch を起動できる状態になるまでの手順を説明する。

## 前提条件

以下がインストール・設定済みであること。

- **Claude Code** v2.0.12 以上
- **cc-memory プラグイン** がインストール済み（`claude plugin list` で確認）
- **relayサーバー** のソースコードが配置済み（後述）

## 1. relayサーバーの準備

owはセッション間通信のために軽量HTTPサーバー（relay）を使用する。relayのソースコードを取得する。

```bash
# デフォルトの配置先
git clone <relay-repo-url> ~/workspace/powwow
```

配置先を変更する場合は環境変数 `RELAY_DIR` で指定する（後述）。

relayサーバーはcc-memoryが `/orch` 起動時に自動で立ち上げる。手動で起動する場合は以下を実行する。

```bash
cd ~/workspace/powwow
python server.py
```

デフォルトのリッスンポートは **8765**（`http://127.0.0.1:8765`）。

## 2. 環境変数の設定

cc-memory の `.mcp.json` に以下の環境変数を追加する。

### 必須

| 変数 | 値 | 説明 |
|------|-----|------|
| `CCM_OW` | `1` | ow機能（`ow_*` MCPツール）を有効化する |

### 任意（デフォルト値で動作する）

| 変数 | デフォルト | 説明 |
|------|-----------|------|
| `OW_TERMINAL` | `manual` | workerを起動するターミナル種別。`iterm2` / `tmux` / `manual` のいずれか |
| `RELAY_DIR` | `~/workspace/powwow` | relayサーバーのソースコードディレクトリ |
| `RELAY_URL` | `http://127.0.0.1:8765` | relayサーバーのURL |
| `OW_QUEUE_DIR` | （自動） | queueファイルの保存先。未設定時は `~/.cc-memory-ow/orch` |

### 設定例（`.mcp.json`）

```json
{
  "cc-memory": {
    "command": "uv",
    "args": ["run", "--directory", "${CLAUDE_PLUGIN_ROOT}", "python", "-m", "src.launcher"],
    "env": {
      "CCM_OW": "1",
      "OW_TERMINAL": "iterm2"
    }
  }
}
```

### OW_TERMINAL の選び方

| 値 | 動作 |
|----|------|
| `iterm2` | orchがworkerを起動するとき、iTerm2に新しいタブを自動作成する（macOSのみ） |
| `tmux` | 新しいtmuxペインを作成する |
| `manual` | 起動コマンドを表示するだけ。ユーザーが手動でコマンドを実行する |

`iterm2` を使う場合はcc-memoryがiTerm2を操作する権限が必要。macOSの「システム設定 > プライバシーとセキュリティ > オートメーション」でClaude Codeに iTerm2 の操作を許可すること。

## 3. workerスキルの確認

workerスキルはcc-memoryプラグインに同梱されており、プラグインインストール後は自動で利用可能になる。

確認方法：Claude Codeセッション内で `/worker` と入力してスキルが表示されることを確認する。

表示されない場合は、ユーザーレベルのスキルディレクトリに手動配置する。

```bash
# ユーザーレベルに手動配置
mkdir -p ~/.claude/skills/worker
cp <cc-memory-repo>/skills/worker/SKILL.md ~/.claude/skills/worker/SKILL.md
```

## 4. /orch の起動と基本的な使い方

### 起動

cc-memoryとowが有効なClaude Codeセッションを開き、以下のスキルを実行する。

```
/orch
```

`/orch` は以下を自動で実行する。

1. relayサーバーの起動確認（未起動なら `RELAY_DIR/server.py` を自動起動）
2. channelの作成または復元
3. queueファイル（`~/.cc-memory-ow/orch/queue-t<topic_id>.md`）の初期化

### 基本的な使い方

`/orch` 起動後、タスクキューに登録されたタスクをworkerに割り当てる。

**workerの起動（OW_TERMINAL=manualの場合）:**

orchが `ow_spawn_worker` を呼ぶと、起動コマンドが表示される。そのコマンドを別のClaude Codeセッションで実行する。

```bash
# orchが表示するコマンドの例
claude --task "/worker task: ~/.cc-memory-ow/orch/tasks/T1.json"
```

**OW_TERMINAL=iterm2 の場合:**

自動で新しいタブが開き、workerが起動する。

### タスクキューの管理

queueファイル（`~/.cc-memory-ow/orch/queue-t<topic_id>.md`）を直接編集してタスクを追加・変更できる。orchが次のサイクルでpickupする。

## 5. トラブルシューティング

### relayサーバーに接続できない

**症状:** `ow_*` ツール呼び出し時に `RELAY_UNAVAILABLE` エラーが出る。

**確認事項:**
```bash
# relayが動いているか確認
curl http://127.0.0.1:8765/presence?channel=__health__

# 手動起動
cd ~/workspace/powwow && python server.py
```

`RELAY_DIR` が正しいパスを指しているか確認すること。

### ow_* ツールが表示されない

**症状:** `ow_send` などのツールがClaude Codeセッションに表示されない。

**確認事項:**
- `.mcp.json` に `"CCM_OW": "1"` が設定されているか確認する
- Claude Codeセッションを再起動する（環境変数変更後は再起動が必要）

### workerスキルが見つからない

**症状:** `/worker` を入力してもスキルが表示されない。

**対処:** 「3. workerスキルの確認」の手動配置手順を実行する。

### iTerm2でworkerが自動起動しない

**症状:** `OW_TERMINAL=iterm2` を設定したがworkerが起動しない。

**確認事項:**
- macOSの「システム設定 > プライバシーとセキュリティ > オートメーション」でClaude CodeにiTerm2操作権限を付与しているか確認する
- iTerm2が起動していることを確認する

### cmd:assign がworkerに届かない

**症状:** worker側でreadyを送信したがorchからassignが来ない。

**対処:** workerスキルの起動ステップ6の通り、ready送信直後に `ow_history` で履歴をpullする。SSE接続完了前にassignが届く場合があるため。

### queueファイルが見つからない / 消える

**症状:** orchがqueueファイルを読み込めないエラーが出る。

**確認事項:**
- `OW_QUEUE_DIR` または デフォルトの `~/.cc-memory-ow/orch/` を確認する
- `auto-memory`管理ディレクトリ（`~/.claude/projects/.../memory/`）には配置しないこと。Claude Codeのauto-memoryが書き換えるため。
