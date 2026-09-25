# CALM

CALM（Concurrent, autonomous, loosely-coupled minds）は、Claude Codeのセッション間で、議論の文脈・決定事項・作業状況を永続化するプラグインです。

## 何が解決されるのか

Claude Codeはセッションごとに記憶がリセットされます。短いタスクなら問題ありませんが、長期プロジェクトでは「前に何を決めたか」「なぜその設計にしたか」「どこまで作業が進んでいるか」がセッションをまたぐと失われます。

CALMは、こうした文脈をSQLiteデータベースに保存し、新しいセッションでAIが自動的に過去の記録を参照できるようにします。同じ説明を繰り返す必要がなくなり、議論の積み重ねがそのまま次のセッションに引き継がれます。

## 主な機能

- **トピック管理** — 議論の主題ごとに情報を整理します
- **決定事項の記録** — 合意した内容を理由とともに保存します
- **議論ログ** — 議論の経緯や検討過程を保存します
- **アクティビティ管理** — 作業タスクの進捗をステータスで追跡します
- **資材管理** — セッション中に生成された分析結果・ドラフト等をタグ付き独立エンティティとして永続化します
- **リレーション** — トピック・アクティビティ間の関連をグラフ構造で管理します
- **タグシステム** — トピック・決定・ログ・アクティビティを横断的にタグで分類します。タグにnotesを付けて作業開始時にAIへ自動注入できます
- **振る舞い（habits）** — check-in時にAIへ毎回注入される運用ルールを管理します
- **ハイブリッド検索** — キーワード検索（FTS5）とベクトル検索を組み合わせて関連情報を見つけます

## インストール

### 前提条件

- [uv](https://docs.astral.sh/uv/) がインストールされていること
- Claude Code v2.0.12以上
- Python 3.12+（SQLite拡張ロード対応ビルドが必要）
  - pyenvのデフォルトビルドは `--enable-loadable-sqlite-extensions` が無効のため非対応
  - Homebrew Python (`brew install python@3.12`) を推奨

### インストール手順

```bash
# マーケットプレイスを追加
claude plugin marketplace add isizono/calm

# プラグインをインストール
claude plugin install calm
```

インストール後、Claude Code内で以下を実行すると使い方の案内が表示されます。

```
/man
```

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

`claude plugin uninstall`はプラグイン本体を削除しますが、上記の`$HOME`配下への書き込み（DB・スナップショット・rules投影ファイル・ログ）と、埋め込みモデルのダウンロードキャッシュ（`~/.cache/huggingface`、後述）は自動削除されません。不要になった場合は手動で削除してください。

## 動作確認

### 初回起動が重い理由

前提条件は[前述](#前提条件)のuv・Claude Codeバージョン・Python 3.12の3点です。embeddingサーバーの起動先（`CALM_PROJECT_ROOT`）はMCPサーバー起動時にClaude Codeが渡す`CLAUDE_PLUGIN_ROOT`から自動設定されるため、gitリポジトリでの利用は前提になりません（詳細は[設定](#設定)の`CALM_PROJECT_ROOT`項を参照）。

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

## MCPツール

| カテゴリ | ツール | 説明 |
|---------|--------|------|
| トピック | `add_topic`, `get_topics` | 議論トピックの作成・新しい順の取得 |
| 議論ログ | `add_logs`, `get_logs` | 議論の経緯や検討過程の一括記録・取得 |
| 決定事項 | `add_decisions`, `get_decisions`, `pull_precedents` | 合意内容の記録・取得、設計判断前の近傍トピック判例の網羅確認 |
| アクティビティ | `add_activity`, `get_activities`, `update_activity` | 作業タスクの作成・取得・状態更新 |
| check-in | `check_in` | アクティビティにcheck-inし、tag notes・資材・関連decisionsを集約取得 |
| 資材 | `add_material`, `update_material`, `get_material`, `export_material` | セッション中の成果物をタグ付き独立エンティティとして保存・更新・取得・md出力 |
| リレーション | `add_relation`, `remove_relation`, `get_map` | エンティティ間の関連の追加・削除・グラフ探索 |
| 前提の揺らぎ管理 | `resolve_destabilization`, `suggest_destabilized_candidates` | 軸変更によりdestabilizeされたdecisionの解消・候補提示 |
| 振る舞い | `add_habit`, `get_habits`, `update_habit` | check-in時に注入される運用ルールの登録・取得・更新 |
| 終了条件（goal） | `set_goal`, `update_goal`, `judge_goal`, `get_goal` | アクティビティの終了条件の設定・条件の追加や状態変更・達成/失敗の判定・全条件と紐づくアクティビティの取得 |
| タグ | `search_tags`, `update_tag`, `analyze_tags`, `demote_tag_notes` | タグの検索、notes・エイリアス・退役状態等の更新、タグ共起分析、notesの指定セクションの資材への退避 |
| ピン | `add_pin`, `remove_pin` | エンティティ間のpin（強調的な関連付け）の追加・削除 |
| 取り消し | `retract` | 決定事項・ログ・資材の論理削除 |
| 検索・横断参照 | `search`, `get_by_ids`, `get_timeline`, `get_overview` | キーワード横断検索、詳細情報の一括取得、時系列表示、進行中/直近完了/裁定待ち/残件の内訳を一望取得 |
| セッション | `get_sessions`, `set_session_alias` | 稼働中セッションの表示名→別名の対応表取得、自セッションの別名の付け替え |
| シグナル・計測 | `report_signal`, `get_signals`, `update_signal`, `detect_reask_candidates` | cc-memory自身への故障報告・矛盾検出・聞き返し候補検出等の運用計測 |
| Ask（人間への判断委譲） | `add_ask`, `get_asks`, `answer_ask`, `triage_ask`, `withdraw_ask`, `unsubscribe_ask` | 離席中・セッション跨ぎの判断待ち問いの起票・取得・回答・振り分け・取り下げ・通知解除 |
| インスタンス間連携 | `set_instance_identity`, `collect_export_candidates`, `export_bundle`, `import_bundle` | 自インスタンス識別子の設定、export候補の洗い出し、バンドルの書き出し、他インスタンスのバンドルの取り込み |
| フィードバック | `get_feedback_entries`, `write_feedback_entry`, `add_feedback_note` | 発話・ツール失敗・実行直前に配達するフィードバックエントリの取得・作成/変更/削除・ノート追加 |
| その他 | `get_config`, `roll_dice` | 設定値の取得、ダイスロール |

## スキル

| スキル | 説明 |
|--------|------|
| `/man` | CALMの使い方をAIが説明します |
| `/overview` | 進行中・直近完了・裁定待ち・残件の内訳を一望表示します |
| `/project-setup` | 新しいプロジェクト・取り組みの知識フレームをCALMにセットアップします |
| `/coding-project-setup` | コードプロジェクト向けの知識フレームをセットアップします（project-setupから委譲） |
| `/activity-start` | 新しいアクティビティを開始します |
| `/activity-pause` | 進行中のアクティビティを完了にせず中断します |
| `/activity-finish` | アクティビティを完了にします |
| `/check-in` | アクティビティにcheck-inして関連情報を集約取得します |
| `/decision-record` | ユーザーとの合意事項をdecisionとして記録するようガイドします |
| `/recording` | 議論の経緯や成果物をログ・資材として記録するようガイドします |
| `/remember` | 「覚えて」等の依頼を受けて、情報の保存先を判定します |
| `/forget` | 現状と矛盾・陳腐化した過去の記録を撤回します |
| `/rule-placement` | 一般化ルールの配置先（habit・tag notes・CLAUDE.md等）をfull評価で判定します |
| `/tag-notes` | タグのnotesを確認・更新します |
| `/tag-cleanup` | タグの共起分析を実行し、整理提案をユーザーに提示します |
| `/sync-memory` | セッション終了前にtranscriptを解析し、トピック・決定事項・ログ・アクティビティを一括で記録・更新します |
| `/digest` | 直近の記録を期間横断で俯瞰するダイジェストを生成します |
| `/postmortem` | completedアクティビティを振り返り、教訓を永続化します |
| `/audit` | 過去の決定事項の矛盾・陳腐化を検証し、知識を正しい場所に記録し直します |
| `/recompose-context` | アクティビティ・トピック等の関連情報を統合整理し、anchor対応表を作ります。整理範囲のアクティビティのgoal・親への結びつけも整えます。`--all` でアクティビティ(active/shelved/snoozed)全域を棚卸しし、completed化・shelved化・統合などの処遇に反映します |
| `/setup-anchor` | 合意事項の検証先（anchor）を対話的に確定・更新します |
| `/scribe` | CALMの記録からドキュメントを生成します |
| `/db-recovery` | DBデータの異常減少を検知した際に、スナップショットから復旧します |
| `/restart` | CALMのローカルMCPサーバー・embeddingサーバーを再起動します |
| `/ask-compose` | `add_ask`のquestion/contextをテンプレートに沿って構成するようガイドします |
| `/ask-distill` | 繰り返し起票されている同型のaskをまとめてメタaskを起票します |
| `/ask-answer` | open askを一覧して1件ずつ提示し、回答をanswer_askで記録します |
| `/ask-watch` | askストアを継続的に監視し、同型のaskが溜まっていたらメタaskとして起票します |
| `/memory-export` | 記録を他インスタンスへ渡すexportバンドルを作成します |
| `/memory-import` | 他インスタンスのexportバンドルを衝突裁定を経て取り込みます |
| `/peer-nudge` | セッション台帳の宛先候補へSendMessageで直接話しかけるときの作法をガイドします |

## hookが黙って失敗したときに気づく

embeddingサーバー起動失敗・検索の`degraded`については[よくある詰まり](#よくある詰まり)を参照してください。ここではその表に無い、hookのfail-open沈黙について書きます。

CALMのhookはfail-open設計であり、1つのhookが例外を投げてもClaude Codeの他の操作（tool呼び出し・セッション開始等）を止めない。この設計自体は意図的だが、失敗は既定では標準エラー出力にしか残らず、記録ナッジやSessionStart注入の一部が黙って消えても「何も起きていない」ように見える。

以下のhookは、hook本体のコードが実行された後に起きた例外を`signal_events`テーブルへ`kind: machine_error`として記録する。`get_signals`ツールで確認できるほか、1件でもあればSessionStart注入の「未トリアージのシグナル」行にも件数が現れる。

- SessionStart注入の各セクション（アクティビティ一覧・habits・signals等）が個別に失敗した場合
- Stop hookの記録ナッジ（`logs_sparse`判定）が失敗した場合
- PreToolUseの内部IDリークブロックhookが失敗した場合

venvの破損や依存パッケージの欠落でhookがimport時点で落ちた場合は、この記録自体が動かず標準エラー出力のみに残る（記録機構自体がDB層のimportに依存するため）。表示専用hook・transcript sanitize系hookも現状この記録の対象外。頻発する場合は`get_signals`で`source`（`hook:section:<セクション名>`等）を確認し、原因を調査する。

## 設定

`.mcp.json`の`env`フィールドで以下の環境変数を設定すると、デフォルト値をオーバーライドできます。未設定の項目はデフォルト値で動作するため、ゼロコンフィグで使用可能です。ここに載せているのは利用者が調整する機会が多いものの抜粋です。挙動の内部調整用に他にも環境変数がありますが、必要になったら`/man`でAIに聞いてください。

| 環境変数名 | デフォルト | 説明 |
|-----------|-----------|------|
| `CALM_DB_PATH` | `~/.claude/.claude-code-memory/discussion.db` | データベースファイルのパス |
| `CALM_HEARTBEAT_TIMEOUT` | `20` | ホットアクティビティ判定の閾値（分） |
| `CALM_GOAL_RECHECK_HOURS` | `6` | goalの担い手human/external条件で要確認フラグを立てるまでの経過時間（時間） |
| `CALM_IN_PROGRESS_LIMIT` | `3` | アクティブコンテキストのin_progress表示件数 |
| `CALM_PENDING_LIMIT` | `2` | アクティブコンテキストのpending表示件数 |
| `CALM_TIER2_MAX_AGE_DAYS` | `7` | SessionStart一覧の階層2にin_progressアクティビティを載せるupdated_at上限（日） |
| `CALM_PIN_SURFACE_DECAY_DAYS` | `60` | pinnedアクティビティが階層2表示を維持できるupdated_at上限（日） |
| `CALM_RECENCY_DECAY_RATE` | `0.0119` | 検索の時間減衰率 |
| `CALM_PRECEDENT_BUDGET_CHARS` | `24000` | `pull_precedents`が本文展開（decision＋reason）に使う文字数予算 |
| `CALM_SYNC_DISABLE_RETROSPECTIVE` | `false` | `/sync-memory`のふりかえりセクションを非表示にする |
| `CALM_SNAPSHOT_INTERVAL` | `12` | スナップショット取得間隔（時間） |
| `CALM_SNAPSHOT_MAX_COUNT` | `5` | スナップショット最大保持数 |
| `CALM_SNAPSHOT_ANOMALY_THRESHOLD` | `100` | 行数減少の異常検知閾値（件） |
| `CALM_PROJECTION_MANIFEST_MAX_ITEMS` | `30` | intelligently habitsマニフェストの掲載件数上限 |
| `CALM_PROJECT_ROOT` | 自動解決（`CLAUDE_PLUGIN_ROOT` → `git rev-parse --git-common-dir`） | `embedding_server`を起動するプロジェクトルート。優先順位は 明示設定 → プラグイン実行時は`CLAUDE_PLUGIN_ROOT`の値から自動設定 → `embedding_server`自身の`git rev-parse --git-common-dir`解決 → いずれも失敗した場合はRuntimeError。加えて`/calm:restart`（強制再起動）実行時は、上記のいずれでも未設定であれば`restart_service`自身も同じgit-common-dir解決（gitリポジトリでなければ実行時のプロジェクトルート）で先回りして設定する。通常は自動解決されるため設定不要だが、いずれの自動解決にも失敗する環境（gitリポジトリ外かつ`CLAUDE_PLUGIN_ROOT`も未設定）では明示設定が必要 |

環境変数は `CALM_` 接頭辞に統一されている。旧名（`CCM_` / `CC_MEMORY_`）も当面はフォールバックとして読まれるが、新名が設定されていればそちらが優先される。

<details>
<summary>リモートサーバー（claude.aiから接続）</summary>

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

</details>

## ライセンス

MIT
