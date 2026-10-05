# トラブルシューティング

[← README](../README.md)

## 動作確認

### 初回起動が重い理由

前提条件は[前述](../README.md#前提条件)のuv・Claude Codeバージョン・Python 3.12の3点です。embeddingサーバーの起動先（`CALM_PROJECT_ROOT`）はMCPサーバー起動時にClaude Codeが渡す`CLAUDE_PLUGIN_ROOT`から自動設定されるため、gitリポジトリでの利用は前提になりません（詳細は[設定](setup.md#設定)の`CALM_PROJECT_ROOT`項を参照）。

初回起動が重いのは主に2つの理由によります。

1. 初回の`uv run`で依存関係（sentence-transformers等）を解決・インストールする
2. 初回のembedding呼び出し時に、埋め込みモデル`cl-nagoya/ruri-v3-70m`（本体約270MB、トークナイザー等を含め合計約290MB）をHugging Face Hubから取得し`~/.cache/huggingface`にキャッシュする

いずれもネットワーク接続が必要で、2回目以降はキャッシュ済みのため高速です。embeddingサーバーはMCPサーバー起動と同時には立ち上がらず、検索やcheck-in等で最初にembeddingが必要になったタイミングで遅延起動します。

### 正常に動いているかの確認

1. Claude Codeで`/mcp`を実行し、calmサーバーがconnected状態であることを確認する
2. `/man`を実行し、使い方の案内が返ってくることを確認する（MCPツール呼び出しの疎通確認を兼ねる）
3. `~/.claude/rules/cc-memory-habits.md`が生成されていることを確認する（SessionStart hookの動作確認）
4. embeddingサーバーの疎通: 検索やcheck-in等を一度実行した後、`curl http://localhost:52836/health`が`{"status": "ok"}`を返すか確認する（一度もembeddingを呼んでいない場合は未起動なので接続不可が正常）

## よくある詰まり

| 症状 | 対処 |
|------|------|
| プラグイン更新後もコードの変更や新しいツールが反映されない | `/restart`でMCPサーバーを強制再起動する |
| 決定事項・アクティビティ等の記録が急に減った・消えたように見える | `/db-recovery`でスナップショットからの復旧を検討する |
| 検索が過去の記録を拾わない／精度が低い | embeddingサーバーが未起動か古い可能性がある。`/restart`に`--restart-embedding`を付けて明示的に再起動する（`search`応答の`degraded: true`はベクトル検索が利用不可だったことを示す） |
| MCPツールが使えない・CALMサーバーに接続できない | `/mcp`から再接続する。直らなければ`/restart` |
| 記録ナッジやSessionStartの一部セクションが理由もなく出なくなった | hookがfail-openで例外を握っている可能性がある。[hookが黙って失敗したときに気づく](#hookが黙って失敗したときに気づく)を参照 |

## hookが黙って失敗したときに気づく

embeddingサーバー起動失敗・検索の`degraded`については[よくある詰まり](#よくある詰まり)を参照してください。ここではその表に無い、hookのfail-open沈黙について書きます。

CALMのhookはfail-open設計であり、1つのhookが例外を投げてもClaude Codeの他の操作（tool呼び出し・セッション開始等）を止めない。この設計自体は意図的だが、失敗は既定では標準エラー出力にしか残らず、記録ナッジやSessionStart注入の一部が黙って消えても「何も起きていない」ように見える。

以下のhookは、hook本体のコードが実行された後に起きた例外を`signal_events`テーブルへ`kind: machine_error`として記録する。`get_signals`ツールで確認できるほか、1件でもあればSessionStart注入の「未トリアージのシグナル」行にも件数が現れる。

- SessionStart注入の各セクション（アクティビティ一覧・habits・signals等）が個別に失敗した場合（`hook:section:<セクション名>`）
- SessionStart hook本体が失敗した場合（`hook:session_start`）
- Stop hookの記録ナッジ（`logs_sparse`判定）が失敗した場合（`hook:stop:logs_sparse`）
- Stop hook本体が失敗した場合（`hook:stop`）
- UserPromptSubmit hookが失敗した場合（`hook:user_prompt_submit`）
- PreToolUseの内部IDリークブロックhookが失敗した場合（`hook:preblock`）
- PreToolUseのbg起動拒否hookが失敗した場合（`hook:deny_nested_bg`）

venvの破損や依存パッケージの欠落でhookがimport時点で落ちた場合は、この記録自体が動かず標準エラー出力のみに残る（記録機構自体がDB層のimportに依存するため）。表示専用hook・transcript sanitize系hookも現状この記録の対象外。頻発する場合は`get_signals`で`source`（`hook:section:<セクション名>`等）を確認し、原因を調査する。
