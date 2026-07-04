# cc-memory デバッグ手順書 v1

## 0. 読み方

本ドキュメントは cc-memory の運用中に発生する故障の切り分け手順をまとめたものである。一次情報はコードであり、本ドキュメントと食い違った場合はコードが正である。cc-memory 内部 ID（D#/M#/A#/L#/T#）は本文に出さず論理名で書く。

「どこで詰まったら何を見るか」を先に引けることを優先し、各コマンドは実行して結果を確認済みのものだけを載せる。未実装の機能（例: migration の dry-run・ハッシュ検証、バックアップのワンコマンド強化版）は「未実装」と明記し、代わりに現状で取れる手段を書く。

---

## 1. プロセス地図と生存確認

cc-memory は 4 つの独立プロセスで構成される。

| プロセス | 役割 | 起動主体 | 確認コマンド |
|---|---|---|---|
| launcher（stdio ブリッジ） | Claude Code から stdio で接続を受け、HTTP サーバーに中継する（`src/launcher.py`） | Claude Code が MCP サーバー起動時に `uv run python -m src.launcher` を実行。セッションごとに 1 プロセス | `ps aux \| grep "src.launcher"`（セッション数だけ存在するのが正常） |
| HTTP サーバー（MCP 本体） | 全ツール・全 DB アクセスの実体。ポート 52837（`src/http_config.py`） | launcher が未起動を検知すると自動でデーモン起動（`_start_http_server`, `src/launcher.py`）。多重起動はロックファイルで防止（`src/services/lock_file.py`、INV-11） | `curl -s http://localhost:52837/health`（`{"status":"ok","pid":...,"uptime_sec":...}` が返る）。ロックファイルは `~/.cc-memory/server.lock` |
| embedding server | ベクトル検索用の embedding 生成。ポート 52836（`src/services/embedding_service.py:19`） | embedding が最初に必要になったタイミングで embedding_service が detached プロセスとして起動（遅延起動） | `curl -s http://localhost:52836/health` |
| relay（ow 用） | orch/worker 間のメッセージ中継。ポート 8765（`src/relay/server.py:32`、env `RELAY_PORT` で変更可） | ow_service が呼び出し時に自己修復ゲートとして起動・再起動（`/health` の `protocol_version` 不一致で kill→再起動） | `curl -s http://127.0.0.1:8765/health` |

補足:

- HTTP サーバーの cwd はプロジェクトルートに固定される（`_ensure_project_root_cwd`, `src/main.py`）。worktree から起動しても worktree 削除後にパスが消える事故は起きない
- 4 プロセスのうち cc-memory 本体の障害調査で最初に見るべきは HTTP サーバー。ここが落ちていると全ツールが失敗する

---

## 2. ログの所在

現状、**HTTP サーバー内部の未処理例外は永続化されていない**。launcher がサーバーを `stdout=DEVNULL, stderr=DEVNULL` で起動するため（`src/launcher.py` `_start_http_server`）、起動時 migration 失敗などサーバープロセスがクラッシュする類の障害は標準出力に何を書いても消える。HTTP サーバーのログファイルへの永続化は本書執筆時点で未実装（embedding server は別途永続ログを持つ。下表参照）。

「どの故障がどこに痕跡を残すか」の対応表:

| 故障の種類 | 痕跡が残る場所 |
|---|---|
| HTTP サーバー起動時の例外（migration 失敗等） | **どこにも残らない**（上記の理由）。launcher 側のログ（`_ensure_server_running` の `logger.warning`）に「30秒以内に起動できなかった」旨が出るのみで、原因は分からない。原因調査は §5 の手動再現に頼る |
| MCP tool 呼び出し中の未捕捉例外 | (1) 呼び出し元に MCP エラーレスポンスとして返る、(2) `signal_events` テーブルに `kind="machine_error"` で自動記録される（`SignalCaptureMiddleware`, `src/services/signal_middleware.py`）。role guard による正常な拒否（`CapabilityError`）は記録対象外 |
| hooks（SessionStart 等）内の例外 | 各 hook の top-level try/except で stderr に print されるのみ（例: `hooks/session_start_hook.py:483-485`）。stderr は Claude Code 側にしか出ず、集約されない。hook からの signal 自動捕捉は本書執筆時点で未実装 |
| embedding server の起動・モデルロード・shutdown・内部エラー | `~/.cache/cc-memory/embedding-server.log`（`RotatingFileHandler` で永続化。デフォルト 5MB × 3 世代ローテーション、`_setup_logging`, `src/services/embedding_server.py`）。プロセス境界は `=== PID ... started at ... ===` ヘッダー行で識別する。起動失敗・モデルロード失敗の原因はここに残る |
| 検索呼び出し（`search`）の挙動 | `search_telemetry` テーブル（非同期記録。query / parameters / result_count、`migrations/0041_add_search_telemetry.sql`） |
| citation sanitize 系の変換イベント | `citation_event_log` テーブル（逐次行型、`migrations/0046_sanitize_log_to_citation_event_log.sql`） |
| 故障報告・使用感不満・矛盾検出の統一入口 | `signal_events` テーブル。`get_signals` ツールで一覧・集計取得（§7） |

---

## 3. DB 直接調査レシピ

DB パス: `~/.claude/.claude-code-memory/discussion.db`（env `CCM_DB_PATH` / `DISCUSSION_DB_PATH` で上書き可能。`src/db.py` `get_db_path`）。

```bash
sqlite3 ~/.claude/.claude-code-memory/discussion.db
```

主要テーブル: `discussion_topics` / `decisions` / `discussion_logs` / `activities` / `materials` / `tags` / `relations` / `decision_supersedes` / `signal_events`。

**vec_index は sqlite3 CLI から素では読めない**（重要な落とし穴）。`vec_index` は sqlite-vec 拡張が提供する仮想テーブルであり、拡張をロードした接続でないと `no such module: vec0` になる。加えて macOS 標準の `/usr/bin/sqlite3`（Apple ビルド）は loadable extension 自体が無効化されており、`.load` コマンドが `unknown command or invalid arguments: "load"` で失敗する（本書執筆時に実機確認済み）。読む場合は次のいずれかを使う:

```python
# 確実に動く方法（uv run 経由でプロジェクトの venv を使う）
import sqlite3, sqlite_vec
conn = sqlite3.connect(db_path)
conn.enable_load_extension(True)
sqlite_vec.load(conn)
conn.enable_load_extension(False)
conn.execute("SELECT COUNT(*) FROM vec_index").fetchone()
```

```bash
# 拡張ロードに対応した sqlite3 が別途あるならこちらでも可（例: brew install sqlite）
EXT=$(uv run python -c "import sqlite_vec; print(sqlite_vec.loadable_path())")
/opt/homebrew/opt/sqlite/bin/sqlite3 discussion.db ".load $EXT" "SELECT COUNT(*) FROM vec_index;"
```

その他の読み方の勘所:

- **retract**: `retracted_at` が NULL でない行は通常の検索・列挙から外れる（INV-8）。撤回済みも含めて見るには `WHERE retracted_at IS NOT NULL`（decisions / discussion_logs / materials のみこのカラムを持つ。topics / activities には無い）
- **supersede**: `decision_supersedes(source_id, target_id)` は `source_id` が新しい decision、`target_id` が古い decision（source が target を supersede する）。chain を辿るには BFS が必要（`src/services/supersede_service.py`）。1 ホップだけなら直接 SELECT で足りる
- **親帰属（belongs_to）**: `relations` テーブルの `relation_type='belongs_to'` 行。向きは常に子（decision/log/material/activity）→ topic（INV-6）。1 つの子が複数 topic に帰属することは禁止されていない
- **readable id とテーブル id の対応**: get 系ツールのレスポンスは内部整数 `id` を `id_raw` に退避し `id` キー自体を削除する（`apply_readable_id_inplace`, `src/services/readable_id.py`）。DB 上の主キーはそのまま各テーブルの `id` 列であり、AI 向け表示用の `(#123)` 形式の文字列とは別物（そちらは `format_readable_id` が SessionStart hook 表示専用に使う）

---

## 4. スナップショットと復元

現状の実装は `scripts/snapshot.py` のみ（バックアップ機構の kind 分離・世代管理拡張・CLI 強化は別途進行中で本書執筆時点では未マージ）。

- CLI は `restore` サブコマンドのみ実装されている。`take` / `list` / `verify` に相当する関数（`take_snapshot` / `health_check` 等）はモジュール内には存在するが、CLI からは呼べない。定期取得は SessionStart hook からの直接呼び出しのみ
- スナップショット一覧の見方: `ls ~/.claude/.claude-code-memory/snapshots/`。`.db` と `.json` が常にペアで存在する（INV-13）。`.json` に `created_at` / `db_size_bytes` / `row_counts`（topics/decisions/logs/activities/materials の行数）が入っている。復元先を選ぶ際はこの `row_counts` を見る
- 取得間隔はデフォルト 12 時間、保持世代は最大 5（`SNAPSHOT_INTERVAL_HOURS` / `SNAPSHOT_MAX_COUNT`, `src/config.py`）。保持ウィンドウは概算 2.5 日
- 復元手順:
  1. 全ての Claude Code セッションを閉じる（DB への書き込みを止めるため。現状 `restore_snapshot` はサーバー稼働中でも実行できてしまう安全装置は無い）
  2. `python scripts/snapshot.py restore <snapshot_db_path>`（内部で `sqlite3.backup()` を使い、スナップショット→現行 DB に書き戻す。**この操作は不可逆**で、復元前の現状態を退避する仕組みは無い）
  3. Claude Code を再起動する
- SessionStart 異常警告（「テーブルの行数が前回スナップショットより 100 件以上減少」）が出た場合のフロー: (1) 即座にユーザーへ報告する、(2) 直近のスナップショットが正常な状態か `.json` の `row_counts` で確認する、(3) 復元が必要と判断したら上記手順を実施する

---

## 5. migration トラブルシュート

現状、migration は yoyo-migrations が無条件に適用する（`src/db.py` `_apply_migrations`）。dry-run ゲート・premigration スナップショット連動・適用済みファイルの内容ハッシュ検証は設計止まりで本書執筆時点では未実装。次の事実は実際に故障を注入して確認済み:

- **1 migration ファイル = 1 トランザクション**。ファイル内の SQL がエラーになると yoyo はそのファイルだけをロールバックし、`_yoyo_migration` にも記録されない。実 DB は無傷のまま残る
- ただし複数ファイルをまとめて適用する場合、失敗ファイルより**前に**適用されたファイルは commit 済みのまま残る（全体を包む 1 トランザクションではない）。「途中まで進んで止まった」状態は普通に起こりうる
- 失敗時の例外は `sqlite3.OperationalError` 等がそのまま外に投げられる。§2 の通りサーバー起動時に発生すると **DEVNULL 送りで消える**ため、原因を見るには次のように直接フォアグラウンドで起動し直す:
  ```bash
  uv run python -m src.main --transport http
  ```
  これで例外がターミナルに出る。（先に HTTP サーバーが稼働中ならロックファイル取得で失敗するので、既存プロセスを `lsof -ti :52837 | xargs kill` で止めてから実行する）
- **適用済み migration ファイルの内容改変は yoyo からは検知されない**。`get_migration_hash()` が計算するのは migration_id（拡張子なしファイル名）の sha256 であり、ファイル内容のハッシュではない（`.venv/lib/python3.12/site-packages/yoyo/migrations.py`）。改変の有無を確認したい場合は手動で `git diff origin/main -- migrations/<file>` を使う（INV-1 参照）
- `migrations/` には番号重複が 4 組ある（0005 / 0015 / 0039 / 0046）。yoyo は ID がフルファイル名で `-- depends:` により順序が明示されるため動作上は問題ない

---

## 6. 典型故障モード表

| 症状 | 原因 | 確認 | 復旧 |
|---|---|---|---|
| HTTP サーバーが起動しない（`port 52837 is already in use`） | 別プロセスが既にポートを握っている、または前回プロセスが正常終了せず残っている | `lsof -i :52837` でプロセスを特定 | 生きていて不要なら `kill`。ゾンビ（ロックファイルはあるがプロセス死亡）は `lock_file.acquire` が stale 判定で自動掃除するので、通常は再起動を待てば直る |
| sqlite-vec ロード失敗（パターン A: `enable_load_extension` が無い） | Python が `--enable-loadable-sqlite-extensions` 無しでビルドされている（pyenv 由来等） | 起動ログに `sqlite-vec startup check failed` + fix 手順が出る（`src/db.py` `verify_sqlite_vec`） | Homebrew Python を使う: `UV_PYTHON=/opt/homebrew/opt/python@3.12/bin/python3.12 uv sync` |
| sqlite-vec ロード失敗（パターン B: ネイティブ拡張が非互換） | sqlite-vec バイナリが環境と非互換 | 同上の起動ログで判別 | sqlite-vec 再インストール、または上記と同じ Python 切替 |
| embedding server が応答しない | 未起動、または起動に失敗（`_resolve_project_root` が git worktree の common-dir 解決に失敗する等） | `curl -s http://localhost:52836/health` で生存確認。起動失敗・モデルロード失敗の原因は `~/.cache/cc-memory/embedding-server.log`（§2）を見る | ベクトル検索のみ劣化し、キーワード検索は生きる（graceful degradation）。プロセス自体は次回 embedding 要求時に再起動を試みる |
| DB ファイル破損 | ディスク異常・強制終了時の書き込み中断等 | `sqlite3 discussion.db "PRAGMA integrity_check;"` を直接実行して確認（現状、起動時の自動整合性チェックは無い） | `python scripts/snapshot.py restore <snapshot_db_path>`（§4） |
| migration 失敗 | migration ファイルの SQL エラー、既存データとの制約違反 | §5 の手順でフォアグラウンド起動し、例外を直接確認 | コード側（migration ファイル）を直す。実 DB は無傷（§5） |
| プラグインキャッシュとサーバーコードの不整合 | PR マージ後、プラグインキャッシュ（main ブランチ由来）と HTTP サーバープロセス（起動時点のコード）がずれる | ツールの挙動が最新 PR の変更を反映していない | `rm -rf ~/.claude/plugins/cache/claude-code-memory-marketplace/` + `__pycache__` 削除 + サーバー再起動（`CLAUDE.md` の PR マージ後手順を参照） |
| relay 不通 | relay プロセス未起動、または `PROTOCOL_VERSION` 不一致の旧プロセスが居座っている | `curl -s http://127.0.0.1:8765/health` | ow_service の自己修復ゲートが `/health` の version 不一致を検知して自動で kill→再起動する。手動介入が要るのは自己修復自体が働かないケース（ロック競合等）のみ |

---

## 7. シグナル記録の使い方

`report_signal` / `get_signals` / `update_signal` の 3 ツールで、cc-memory 自身の故障・使用感不満・矛盾検出・運用計測イベントを記録・トリアージする。decision と違い「双方の合意」を要さない生の観測データであり、状態遷移（トリアージ）を持つ。

- **kind（7 種、いずれか必須）**: `machine_error`（ツールエラー・hook 失敗・サーバー異常）/ `friction`（使い勝手への不満・違和感）/ `contradiction`（既存記録と矛盾する結論。`refs` に矛盾の両側の id を含める）/ `precedent_miss` / `precedent_misapplied`（判例参照の見落とし・誤類推）/ `boundary_case` / `rollback`（運用上の案件記録。`summary` に案件識別子を含め fingerprint を案件ごとに分ける）
- **dedup**: 同一 fingerprint（`sha256(kind|source|正規化summary)` の先頭 16 hex）の `status='new'` 行が既存なら、新規行を作らず `occurrence_count` を +1 する。トリアージ済み（`new` 以外）の同型イベント再発は新規行になる — 「直したはずの故障の再発」が新規シグナルとして再浮上する
- **運用フロー**: `report_signal` で記録 → `get_signals(status="new")` で未トリアージ一覧を確認 → 対処したら `update_signal` で `triaged` / `dismissed` に遷移、既存エンティティ（topic/activity/decision/log/material）に昇格させる場合は `promoted_type` / `promoted_id` を指定して `promoted` にする（実体の作成は各種 add 系ツールで別途行う。`update_signal` はリンクを張るだけ）
- `get_signals(include_stats=True)` で kind × status のクロス集計と直近 30 日サマリを取得できる
- middleware（`SignalCaptureMiddleware`）が MCP tool 呼び出し中の未捕捉例外を自動で `machine_error` として記録する。role guard による正常な拒否（`CapabilityError`）は対象外。記録自体の失敗はツール呼び出しを壊さない（ベストエフォート）
