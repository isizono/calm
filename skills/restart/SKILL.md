---
name: restart
description: 【必須】CALMのローカルMCPサーバー・embeddingサーバーを強制再起動する。プラグインアップデート後にコード変更を反映させたいときに使う。「/restart」「MCPサーバー再起動して」「CALMのサーバー再起動」「サーバー再起動して」などで発動。このスキルを経由せずに再起動用のkill/起動コマンドを直接組み立てて実行してはいけない。DO NOT TRIGGER: 再起動について相談・検討しているだけで実行をまだ求めていない場合、worktree削除やgit pull等PRマージ後の後片付け全体を求められた場合（それはリポジトリのCLAUDE.mdの手順に従う）。
---

# restart

CALMのローカルMCPサーバー(52837)・embeddingサーバー(52836)を強制的に再起動する。

launcherの通常起動は「生きていれば何もしない」ensure動作のため、プラグインをアップデートした後もコード変更が反映されないことがある。このスキルは既存プロセスを明示的に終了させてから新規プロセスを起動する。

## 実行

ユーザーがこのスキルを明示的に呼び出したこと自体を実行の承認とみなし、追加確認は取らずに以下をBashツールで実行する。

```
uv run --directory ${CLAUDE_PLUGIN_ROOT} python ${CLAUDE_PLUGIN_ROOT}/scripts/restart_server.py
```

## 結果の報告

スクリプトはJSON形式で結果を標準出力に返す。

- `uv_sync.ok` が `false`: 依存関係の同期に失敗している。`detail` を伝えつつ、`mcp_server` の再起動自体は実行済みなのでその結果と合わせて報告する
- `mcp_server.ok` が `true`: 再起動成功。`old_pids`（旧プロセス）と`new_pids`（新プロセス）をユーザーに簡潔に伝える
- `mcp_server.ok` が `false`: 再起動失敗。`detail` の内容をそのままユーザーに伝え、手動確認（`lsof -i tcp:52837 -sTCP:LISTEN`等）を促す。プラグイン更新直後の初回実行はvenv再構築が重く、稀にこのタイムアウトが起きることがある。その場合は再実行を促す
- `embedding_server.stopped_pids` は空配列でもよい（元々起動していなかっただけ）
- `caches` は削除したパスの記録。特に問題なければ触れなくてよい

再起動後、他に生存しているClaude Codeセッションがあれば、それぞれで `/mcp` からreconnectが必要な場合があることを伝える。
