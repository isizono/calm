---
name: restart
description: CALMのプラグインを最新にし、ローカルMCPサーバーを強制再起動する。CALMのPRがマージされた後や、環境変数などの設定を変えた後に、Claudeが自分で実行してよい。embeddingサーバーも停止され、新しいサーバーの起動直後に自動で立ち上がる。
---

# restart

CALMのローカルMCPサーバー(52837)を強制的に再起動する。

PRマージ後の後片付け全体（worktree削除やgit pull等）はリポジトリのCLAUDE.mdの手順に従う。本スキルはサーバーの再起動だけを行う。

launcherの通常起動は「生きていれば何もしない」ensure動作のため、プラグインをアップデートした後もコード変更が反映されないことがある。このスキルは既存プロセスを明示的に終了させてから新規プロセスを起動する。

embeddingサーバー(52836)はMCPサーバーの再起動より前に必ず停止する。新しいMCPサーバーが起動直後に自動でembeddingサーバーを立ち上げて温めるため、embedding側のコード変更も反映される。

## 再起動の前

1. プラグインを最新にする: `claude plugin update calm@calm-marketplace` を実行する。設定を変えただけでコードの更新が無いときは省いてよい
2. 急がないなら、更新だけして再起動は打たなくてよい。サーバーの陳腐化 watchdog が、処理中の呼び出しが0件になるのを待って自動で再起動する（確認間隔は `CALM_STALENESS_CHECK_INTERVAL_SEC`、既定1時間）
3. 更新後の版のディレクトリを調べる。セッションの `${CLAUDE_PLUGIN_ROOT}` は更新前の版を指したままのことがあるため、次で取り直す:

```
PLUGIN_DIR=$(jq -r '.plugins["calm@calm-marketplace"][0].installPath' ~/.claude/plugins/installed_plugins.json)
```

4. 古い窓口から打たない: `uv run --directory "$PLUGIN_DIR" python "$PLUGIN_DIR/scripts/restart_server.py" --status` の `mcp_server.started_at` を確かめ、直近の再起動より後の時刻であれば、別の窓口が既に打った後なので打ち直さない

新しい版のディレクトリは venv が未作成のことがある。`--no-sync` を付けると、スクリプト冒頭の `psutil` の import で `No module named 'psutil'` となり落ちるため、ここでは付けない（`uv run` が先に依存を同期する）。

## 実行

再起動が必要になったら、確認は取らずに以下をBashツールで実行する。

```
uv run --directory "$PLUGIN_DIR" python "$PLUGIN_DIR/scripts/restart_server.py"
```

外から終了させるため、watchdog の「処理中の呼び出しが0件になるまで待つ」ガードを通らない。停止の直前に処理中だった書き込みは、失敗に見えても DB には書かれていることがある。再起動の前後で失敗に見えた書き込みは、リトライする前に検索で記録があるかを確かめる（確かめずに再送すると重複する）。

## 直後

既存のセッション（bgを含む）は通常、再接続の操作なしでつながり直すため、`/mcp` の再接続や bg の stop→respawn は要らない。呼び出しが固まったセッションがあったときだけ、次のとおり対処する（固まったかどうかの見分け方は末尾の節）。

- 窓口（対話セッション）: そのセッションで `/mcp` からreconnectする。直らなければそのセッションを再起動する
- bg: `claude stop <id>` で止め、`claude respawn <id>` で会話を引き継いだまま起こし直す（依頼文の再送は不要）。respawnで短い参照(id)が変わることがあるので、`SendMessage` は新しい参照へ「`get_config` を呼んで疎通を確認して」と伝える。`claude stop` がauto modeのclassifierに止められたら、迂回せずユーザーに `! claude stop <id>` を頼む
- 起こし直したbgがorchの子なら、そのorchの窓口へ `SendMessage` で「bgを起こし直した」と伝える（窓口の説明に書かれたbg一覧を直すため）

## 結果の報告

スクリプトはJSON形式で結果を標準出力に返す。

- `uv_sync.ok` が `false`: 依存関係の同期に失敗している。`detail` を伝えつつ、`mcp_server` の再起動自体は実行済みなのでその結果と合わせて報告する
- `mcp_server.ok` が `true`: 再起動成功。`old_pids`（旧プロセス）と`new_pids`（新プロセス）をユーザーに簡潔に伝える
- `mcp_server.ok` が `false`: 再起動失敗。`detail` の内容をそのままユーザーに伝え、`--status` での手動確認を促す。プラグイン更新直後の初回実行はvenv再構築が重く、稀にこのタイムアウトが起きることがある。その場合は再実行を促す
- `embedding_server.stopped_pids` は停止したembeddingサーバーのPID。空配列なら元々停止していただけなので触れなくてよい
- `caches` は削除したパスの記録。特に問題なければ触れなくてよい
- `orphaned_processes.stopped` が空配列でない場合: 削除済みのプラグインディレクトリから動き続けていたプロセス（embeddingサーバー・MCPサーバー本体等）を検出して停止したことを伝える（`pid`・`cwd`で何を止めたか分かる）。launcherプロセス（各セッションのブリッジ）は対象外で、この一覧に出てくることはない。`orphaned_processes.kept` は、同種のプロセスのうち起動元ディレクトリがまだ存在していた（＝孤児ではない）ものの記録で、特に問題なければ触れなくてよい
- `plugin_cache_prune` はプラグインキャッシュ旧バージョンの掃除結果（`removed`/`skipped`）。削除件数があれば一言触れる、空配列なら触れなくてよい

## 再起動の後

CALMのツールが通るのを確かめてから、退避してあった分を流し込む。

- CALMに書けなかった間の記録を退避ファイルに書き出していた場合は、再起動の後に自分の分だけを名指しして流し込む。持ち主が生きていない分（respawnで消えたbg、閉じた窓口の分）は、中身を確かめてから名指しで流す。ディレクトリ直下を一括で流すような操作は行わない
- bgを起こし直した場合は、空席のorchの説明に書かれている、生きているbgの一覧（あれば）を、実際に起こし直した内容に直す
- 反映待ちは `--status` の `mcp_server.started_at` とマージした時刻を突き合わせて確かめ、済んだものは消す

## 固まった接続の見分け方

自動でつながり直さなかった場合の例外として、再起動の前からいた接続が古いサーバーを掴んだままのことがある（対処は「直後」の節）。エラー文言がauto modeのclassifierの拒否のように見えても、実際は古いサーバーへの接続が固まっているだけのことがある。`--status` の `mcp_server.started_at` が今回の再起動時刻より前なら、その接続は古いサーバーを掴んだままだと分かる。
