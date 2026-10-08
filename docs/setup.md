# セットアップ

[← README](../README.md)

## インストールすると何が起きるか

CALMは`$HOME`配下にいくつかのファイルを生成し、hookを全イベントに登録します。

**書き込み先**

| 種別 | パス | 内容 |
|------|------|------|
| データベース | `~/.claude/.claude-code-memory/discussion.db` | トピック・決定事項・ログ・アクティビティ等 |
| スナップショット | `~/.claude/.claude-code-memory/snapshots/` | 既定12時間毎・最大5世代の定期バックアップ |
| 振る舞い（habits）投影 | `~/.claude/rules/cc-memory-habits.md` | habits DBから自動生成。**手編集は次回同期で上書きされて失われます** |
| hook発火ログ・状態ファイル | `~/.cc-memory/`, `~/.cache/cc-memory/` | 内部IDブロック時のログ、embeddingサーバーのログ、セッション別名対応表、ask通知ファイル等 |

**hooksの登録内容とコスト**

SessionStart(3スクリプト)・Stop・UserPromptSubmit・MessageDisplayに加え、PreToolUse・PostToolUseは`matcher: "*"`で登録されており、**ツール呼び出し1回ごとに`uv run python`で新規プロセスが起動します**。初回の`uv run`は依存関係の解決・インストールが走るため重く、2回目以降はvenvがキャッシュされるため起動コストは小さくなります。PreToolUseの内部IDリーク防止hookは、cwd上方の`pyproject.toml`の`[project].name`がCALM自身のプロジェクト名と一致する場合のみ実際にブロック判定を行うため、通常のユーザープロジェクトでは判定自体は常にno-opで通過します（プロセス起動自体は発生します）。

**無効化スイッチ**

振る舞い投影は環境変数`CALM_HABITS_RULES_EXPORT=0`で止められます（既存の投影済みファイルはプレースホルダで上書きされたまま更新されなくなります）。他の環境変数は[設定](#設定)を参照してください。

**アンインストール後の残置物**

`claude plugin uninstall`はプラグイン本体を削除しますが、上記の`$HOME`配下への書き込み（DB・スナップショット・rules投影ファイル・ログ）と、埋め込みモデルのダウンロードキャッシュ（`~/.cache/huggingface`、詳しくは[初回起動が重い理由](troubleshooting.md#初回起動が重い理由)を参照）は自動削除されません。不要になった場合は手動で削除してください。

## 設定

`~/.claude/settings.json`の`env`フィールドで以下の環境変数を設定すると、デフォルト値をオーバーライドできます（`/calm:setup`から設定変更を選ぶと、一覧の表示と書き込みをAIが手伝います）。設定を変えたら、MCPサーバーの再起動が必要です。`/calm:restart`（プラグイン更新と再起動を行う）はClaudeが自分で実行しますが、ユーザーが打ってもかまいません。未設定の項目はデフォルト値で動作するため、ゼロコンフィグで使用可能です。ここに載せているのは利用者が調整する機会が多いものの抜粋です。挙動の内部調整用に他にも環境変数がありますが、必要になったら`/man`でAIに聞いてください。

| 環境変数名 | デフォルト | 説明 |
|-----------|-----------|------|
| `CALM_DB_PATH` | `~/.claude/.claude-code-memory/discussion.db` | データベースファイルのパス |
| `CALM_HEARTBEAT_TIMEOUT` | `20` | ホットアクティビティ判定の閾値（分） |
| `CALM_GOAL_RECHECK_HOURS` | `6` | goalの担い手human/external条件で要確認フラグを立てるまでの経過時間（時間） |
| `CALM_TIER2_MAX_AGE_DAYS` | `7` | SessionStart一覧の階層2にin_progressアクティビティを載せるupdated_at上限（日） |
| `CALM_TIER2_MAX_ITEMS` | `5` | SessionStart一覧の『優先』に出す件数の上限。hookが読むため`~/.claude/settings.json`の`env`で設定する。増やすときは`CALM_INJECTION_BUDGET_ACTIVITIES`も上げる（各セクションの予算の合計が`CALM_TOTAL_INJECTION_BUDGET_CHARS`を超えるとcomposeがValueErrorを出す。既定の合計は10500字で、総予算12000字との差は1500字） |
| `CALM_PIN_SURFACE_DECAY_DAYS` | `60` | pinnedアクティビティが階層2表示を維持できるupdated_at上限（日） |
| `CALM_RECENCY_DECAY_RATE` | `0.0119` | 検索の時間減衰率 |
| `CALM_PRECEDENT_BUDGET_CHARS` | `24000` | `pull_precedents`が本文展開（decision＋reason）に使う文字数予算 |
| `CALM_SYNC_DISABLE_RETROSPECTIVE` | `false` | `/sync-memory`のふりかえりセクションを非表示にする |
| `CALM_SNAPSHOT_INTERVAL` | `12` | スナップショット取得間隔（時間） |
| `CALM_SNAPSHOT_MAX_COUNT` | `5` | スナップショット最大保持数 |
| `CALM_SNAPSHOT_ANOMALY_THRESHOLD` | `100` | 行数減少の異常検知閾値（件） |
| `CALM_SEARCH_HEALTH_WINDOW_DAYS` | `7` | 検索縮退・クエリ拡張停止検知の集計対象ウィンドウ（日） |
| `CALM_SEARCH_HEALTH_MAX_SAMPLE` | `100` | 同集計で見る最大件数（timestamp降順） |
| `CALM_SEARCH_HEALTH_MIN_SAMPLE` | `20` | 同集計の判定に必要な最小サンプル数（未満なら常に健全扱い） |
| `CALM_SEARCH_HEALTH_DEGRADED_RATIO` | `0.2` | 検索の縮退率がこの値以上なら異常とみなす閾値 |
| `CALM_SEARCH_HEALTH_QE_FIRE_FLOOR` | `0.0` | クエリ拡張の発火率がこの値以下なら異常とみなす閾値 |
| `CALM_PROJECTION_MANIFEST_MAX_ITEMS` | `30` | intelligently habitsマニフェストの掲載件数上限 |
| `CALM_PROJECT_ROOT` | 自動解決（`CLAUDE_PLUGIN_ROOT` → `git rev-parse --git-common-dir`） | `embedding_server`を起動するプロジェクトルート。優先順位は 明示設定 → プラグイン実行時は`CLAUDE_PLUGIN_ROOT`の値から自動設定 → `embedding_server`自身の`git rev-parse --git-common-dir`解決 → いずれも失敗した場合はRuntimeError。加えて`/calm:restart`（強制再起動）実行時は、上記のいずれでも未設定であれば`restart_service`自身も同じgit-common-dir解決（gitリポジトリでなければ実行時のプロジェクトルート）で先回りして設定する。通常は自動解決されるため設定不要だが、いずれの自動解決にも失敗する環境（gitリポジトリ外かつ`CLAUDE_PLUGIN_ROOT`も未設定）では明示設定が必要 |

環境変数は `CALM_` 接頭辞に統一されている。旧名（`CCM_` / `CC_MEMORY_`）も当面はフォールバックとして読まれるが、新名が設定されていればそちらが優先される。
