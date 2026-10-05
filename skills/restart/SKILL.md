---
name: restart
description: CALMのローカルMCPサーバーを強制再起動する。プラグイン更新後にコード変更を反映したいときに使う。embeddingサーバーも停止され、新しいサーバーの起動直後に自動で立ち上がる。
disable-model-invocation: true
---

# restart

CALMのローカルMCPサーバー(52837)を強制的に再起動する。

PRマージ後の後片付け全体（worktree削除やgit pull等）はリポジトリのCLAUDE.mdの手順に従う。本スキルはサーバーの再起動だけを行う。

launcherの通常起動は「生きていれば何もしない」ensure動作のため、プラグインをアップデートした後もコード変更が反映されないことがある。このスキルは既存プロセスを明示的に終了させてから新規プロセスを起動する。

embeddingサーバー(52836)はMCPサーバーの再起動より前に必ず停止する。新しいMCPサーバーが起動直後に自動でembeddingサーバーを立ち上げて温めるため、embedding側のコード変更も反映される。

## 再起動の前（CALMを使う）

- 古い窓口から打たない: `uv run --no-sync --directory "${CLAUDE_PLUGIN_ROOT}" python "${CLAUDE_PLUGIN_ROOT}/scripts/restart_server.py" --status` の `mcp_server.started_at` を確かめ、直近の再起動より後の時刻であれば、別の窓口が既に打った後なので打ち直さない
- `orch` タグの付いたアクティビティ(orch)を検索し、担い手欄（読み方は `orch` タグのnotesを参照。orchの説明の先頭に書かれた、今の担い手の欄）と、生きている窓口(`claude agents --json`)を控える。直後の節で、控えた窓口への知らせと `/mcp` 再接続の案内に使う

## 実行

ユーザーがこのスキルを明示的に呼び出したこと自体を実行の承認とみなし、追加確認は取らずに以下をBashツールで実行する。

```
uv run --no-sync --directory "${CLAUDE_PLUGIN_ROOT}" python "${CLAUDE_PLUGIN_ROOT}/scripts/restart_server.py"
```

## 直後（CALMを使わない）

MCPサーバーの再起動が終わった直後、自分自身もまだ `/mcp` で再接続していない可能性がある。この区間はCALMのツールを使わず、`claude` コマンドと `SendMessage` だけで後始末する。

- `claude agents --json` で生きているbg（`kind=background` かつ `pid` あり）を全部列挙する
- 各bgを `claude stop <id>` で止め、`claude respawn <id>` で会話を引き継いだまま起こし直す（依頼文の再送は不要）。respawnで短い参照(id)が変わることがあるので、`SendMessage` は新しい参照へ「`get_config` を呼んで疎通を確認して」と伝える
- `claude stop` がauto modeのclassifierに止められたら、迂回せずユーザーに `! claude stop <id>` を頼む
- 前の節で控えた、生きている窓口の名前を「`/mcp` の再接続が要る窓口」としてユーザーに示す
- 控えた担い手のうち生きている窓口（担い手欄の`sessionId`が一致し`pid`がある行。宛先はその行の今の`name`）には、`SendMessage` で「bgを起こし直した」と伝える

## 結果の報告

スクリプトはJSON形式で結果を標準出力に返す。

- `uv_sync.ok` が `false`: 依存関係の同期に失敗している。`detail` を伝えつつ、`mcp_server` の再起動自体は実行済みなのでその結果と合わせて報告する
- `mcp_server.ok` が `true`: 再起動成功。`old_pids`（旧プロセス）と`new_pids`（新プロセス）をユーザーに簡潔に伝える
- `mcp_server.ok` が `false`: 再起動失敗。`detail` の内容をそのままユーザーに伝え、`--status` での手動確認を促す。プラグイン更新直後の初回実行はvenv再構築が重く、稀にこのタイムアウトが起きることがある。その場合は再実行を促す
- `embedding_server.stopped_pids` は停止したembeddingサーバーのPID。空配列なら元々停止していただけなので触れなくてよい
- `caches` は削除したパスの記録。特に問題なければ触れなくてよい
- `orphaned_processes.stopped` が空配列でない場合: 削除済みのプラグインディレクトリから動き続けていたプロセス（embeddingサーバー・MCPサーバー本体等）を検出して停止したことを伝える（`pid`・`cwd`で何を止めたか分かる）。launcherプロセス（各セッションのブリッジ）は対象外で、この一覧に出てくることはない。`orphaned_processes.kept` は、同種のプロセスのうち起動元ディレクトリがまだ存在していた（＝孤児ではない）ものの記録で、特に問題なければ触れなくてよい
- `plugin_cache_prune` はプラグインキャッシュ旧バージョンの掃除結果（`removed`/`skipped`）。削除件数があれば一言触れる、空配列なら触れなくてよい

再起動後、他に生存しているClaude Codeセッションがあれば、それぞれで `/mcp` からreconnectが必要な場合があることを伝える。窓口ごとの具体的な案内は「直後」の節で控えた一覧に従う。

## `/mcp` の後（CALMに戻ってから）

`/mcp` で再接続できてから、退避してあった分を流し込む。

- CALMに書けなかった間の記録を退避ファイルに書き出していた場合は、`/mcp`の後に自分の分だけを名指しして流し込む。持ち主が生きていない分（respawnで消えたbg、閉じた窓口の分）は、中身を確かめてから名指しで流す。ディレクトリ直下を一括で流すような操作は行わない
- 空席のorchの説明に書かれている、生きているbgの一覧（あれば）を、実際に起こし直した内容に直す
- 反映待ちは `--status` の `mcp_server.started_at` とマージした時刻を突き合わせて確かめ、済んだものは消す

## 固まった接続の見分け方

再起動の前からいた接続は、古いサーバーを掴んだままのことがある。エラー文言がauto modeのclassifierの拒否のように見えても、実際は古いサーバーへの接続が固まっているだけのことがある。`--status` の `mcp_server.started_at` が今回の再起動時刻より前なら、その接続は古いサーバーを掴んだままだと分かる。
