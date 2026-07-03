# cc-memory Invariants 一覧 v1

## 読み方

本ドキュメントは cc-memory の DB・実行系が常に満たすべき不変条件（invariant）を一覧化したものである。凍結を目的とせず、把握・デバッグ用の作業用ドキュメントとして扱う。一次情報はコードであり、本ドキュメントと食い違った場合はコードが正である。cc-memory 内部 ID（D#/M#/A#/L#/T#）は本文に出さず論理名で書く。

フォーマット: `| ID | 不変条件 | 強制箇所 | 検証手段 |`。各項目に「破れると何が起きるか」を 1 文添える。

各検証手段は、このドキュメントを書く際に実際に空 DB（全 migration 適用済み・レコード 0 件）に対して実行し、想定通りの結果（0 行）になることを確認済みである。

**更新規約**: invariant を新設・変更・廃止する PR は本ドキュメントの更新を含める（機械強制はしない。目視でのレビュー規約とする）。

---

## 一覧

### INV-1: 適用済み migration ファイルの内容は適用後に変更されない

- **強制箇所**: 現状なし。yoyo-migrations の `get_migration_hash()` が計算するのは migration_id（拡張子なしファイル名）の sha256 であり、ファイル内容のハッシュではないため、内容改変を検知する機構が無い（`.venv/lib/python3.12/site-packages/yoyo/migrations.py`）。内容ハッシュを記録・検証する仕組みは設計止まりで本書執筆時点では未実装
- **検証手段**（手動）:
  ```bash
  git diff origin/main -- migrations/<file>
  ```
  想定される差分が無ければ正常。
- **破れると**: 適用済みの DB と新規に全 migration を適用した DB とでスキーマが分岐する。既存ユーザーの DB は古いスキーマのまま、新規 DB だけが変更後の定義を得るという静的な skew が発生し、原因不明のカラム欠落・型不一致を招く

### INV-2: migration は追記オンリー（origin/main 上のファイルは編集・削除しない）

- **強制箇所**: 現状なし。CI での改変検知は設計止まりで本書執筆時点では未実装
- **検証手段**（手動）:
  ```bash
  git diff origin/main --stat -- migrations/
  ```
  既存ファイルの rename・削除が含まれていないか確認する。
- **破れると**: 他ブランチ・他開発者側の適用履歴（`_yoyo_migration`）と物理ファイルが食い違い、migration の再適用に失敗する、または意図しない再実行が起きる

### INV-3: 全エンティティ行は search_index に 1:1 で存在する

- **強制箇所**: `search_index(source_type, source_id)` の `UNIQUE` 制約（`migrations/0002_add_fts5_search.sql`）+ 各テーブルの INSERT/UPDATE/DELETE トリガー
- **検証手段**:
  ```sql
  SELECT source_type, source_id, COUNT(*) AS c
  FROM search_index GROUP BY source_type, source_id HAVING c > 1;
  ```
  0 行が正常。
- **破れると**: `search_index_fts` / `vec_index` の rowid 対応が破綻し、検索結果が重複・欠落する

### INV-4: search_index_fts の rowid は search_index.id と一致する（contentless、rebuild 不可）

- **強制箇所**: トリガー + 初期投入（`src/db.py` `_migrate_fts5_search_index`）
- **検証手段**:
  ```sql
  SELECT COUNT(*) AS orphan_fts
  FROM search_index_fts WHERE rowid NOT IN (SELECT id FROM search_index);
  ```
  0 が正常。
- **破れると**: contentless FTS5 テーブルは `rebuild` コマンドが使えないため、一度ずれると全文検索が結果落ち・誤結果を返し続ける。復旧にはテーブル再構築（退避コピー）が必要

### INV-5: vec_index の rowid は search_index.id と一致し、孤児を作らない

- **強制箇所**: `embedding_service`（`src/services/embedding_service.py`）。仮想テーブルのため FK 制約は使えず、アプリ層の責務
- **検証手段**（sqlite-vec 拡張のロードが必要。`docs/operations/debugging.md` §3 参照）:
  ```python
  import sqlite3, sqlite_vec
  conn = sqlite3.connect(db_path)
  conn.enable_load_extension(True); sqlite_vec.load(conn); conn.enable_load_extension(False)
  conn.execute(
      "SELECT count(*) FROM vec_index WHERE rowid NOT IN (SELECT id FROM search_index)"
  ).fetchone()
  ```
  0 が正常。
- **破れると**: ベクトル検索が削除済みエンティティを返す、またはベクトルを持たないエンティティが検索から漏れる

### INV-6: relations.belongs_to は子（decision/log/material/activity）→ topic の向きにのみ張れる

- **強制箇所**: `relation_service` の正規化制約 + 各 add 系サービス（`migrations/0046_relations_belongs_to_unify.sql` / `0047_drop_decisions_logs_topic_id.sql` で統一）
- **検証手段**:
  ```sql
  SELECT * FROM relations WHERE relation_type = 'belongs_to' AND target_type != 'topic';
  ```
  0 行が正常。作成時に 1 本張られるが、複数 topic への多重帰属は禁止されていない（「ちょうど 1 つ」ではない）。
- **破れると**: 親子ナビゲーション（recompose 等）が誤った方向に辿り、無関係な topic に帰属しているように見える

### INV-7: decision_supersedes は DAG（自己参照・循環なし）

- **強制箇所**: `supersede_service` の BFS 前提。DB レベルでは `CHECK (source_id != target_id)`（`migrations/0033_relation_expansion.sql`）による直接自己参照の禁止のみで、2 ホップ以上の循環を防ぐ制約は無い
- **検証手段**:
  ```sql
  WITH RECURSIVE path(start_id, cur_id, depth) AS (
    SELECT source_id, target_id, 1 FROM decision_supersedes
    UNION ALL
    SELECT p.start_id, ds.target_id, p.depth + 1
    FROM path p JOIN decision_supersedes ds ON ds.source_id = p.cur_id
    WHERE p.depth < 50
  )
  SELECT DISTINCT start_id FROM path WHERE cur_id = start_id;
  ```
  0 行が正常（decision_supersedes の連鎖が 50 階層を超える場合は `depth < 50` の上限を広げて再実行する）。
- **破れると**: `supersede_service` の BFS は visited set で無限ループはしない（探索自体は止まる）が、chain 表示・展開ロジックが誤った到達集合を返し、supersede 履歴の表示が事実と異なる

### INV-8: retracted_at が立った行は検索・列挙のデフォルト経路に出ない

- **強制箇所**: `retract_service` + 各読み出しサービスのフィルタ（`retracted_at IS NULL`）。このカラムを持つのは decisions / discussion_logs / materials のみ（topics / activities には無い）
- **検証手段**:
  ```bash
  grep -Ln "retracted_at IS NULL" src/services/decision_service.py src/services/discussion_log_service.py src/services/material_service.py
  ```
  （出力が無ければ 3 ファイルとも該当クエリを持つ = 正常。動的には decision を 1 件 retract した上で `get_decisions` の通常経路に出ないことを確認する。）
- **破れると**: 撤回したはずの decision / log / material が検索・一覧に再出現し、撤回の意味が失われる

### INV-9: タグ namespace は tag_service.VALID_NAMESPACES の集合と一致する

- **強制箇所**: `tag_service.py` の `VALID_NAMESPACES` 定数（Python 層検証。DB の CHECK 制約は `migrations/0039_extend_tag_namespace.sql` で撤廃済み）
- **検証手段**:
  ```sql
  SELECT DISTINCT namespace FROM tags
  WHERE namespace NOT IN ('', 'domain', 'intent', 'glossary');
  ```
  0 行が正常。**注意**: `VALID_NAMESPACES` の集合は将来の namespace 追加（例: `layer`）で変わりうる。このクエリのリテラル集合は `tag_service.py` の現在値を都度確認して合わせること
- **破れると**: 未知 namespace のタグがフィルタ・表示ロジックから漏れる、または誤分類される

### INV-10: tags.canonical_id の連鎖は深さ 1（エイリアスのエイリアスを作らない）

- **強制箇所**: `tag_service`
- **検証手段**:
  ```sql
  SELECT t.id FROM tags t
  JOIN tags c ON t.canonical_id = c.id
  WHERE c.canonical_id IS NOT NULL;
  ```
  0 行が正常。
- **破れると**: canonical 解決を 1 段しか辿らない箇所（`resolve_tag_ids` 等）でエイリアスが完全に解決しきれず、タグの実体特定に失敗する

### INV-11: HTTP サーバーは同時に 1 プロセス

- **強制箇所**: ロックファイル（`~/.cc-memory/server.lock`、`src/services/lock_file.py`）+ ポートバインド（`src/main.py`）
- **検証手段**:
  ```bash
  lsof -i :52837
  cat ~/.cc-memory/server.lock
  ```
  `lsof` の PID とロックファイルの `pid` が一致し、プロセスが 1 つのみであれば正常。
- **破れると**: 複数プロセスが同一 DB に書き込む。WAL 自体の整合性は保たれるが、embedding 生成の二重実行等、ビジネスロジック上の重複処理が起きうる

### INV-12: 全接続で PRAGMA foreign_keys=ON / WAL / busy_timeout=5000

- **強制箇所**: `src/db.py` `get_connection`
- **検証手段**: アプリが実際に開く接続に対して:
  ```sql
  PRAGMA foreign_keys;
  PRAGMA journal_mode;
  PRAGMA busy_timeout;
  ```
  それぞれ `1` / `wal` / `5000` を期待する。**注意**: `sqlite3` CLI で DB ファイルを直接開いた接続はこれらの PRAGMA を引き継がない（接続ごとに独立設定のため）。CLI 上での確認はアプリ接続の代わりにならない
- **破れると**: `busy_timeout` 未設定だと並行書き込みで `SQLITE_BUSY` が即座に発生する。`foreign_keys` 未設定だと `ON DELETE CASCADE` が効かず、孤児レコードが残る

### INV-13: snapshots の .db と .json はペアで増減する

- **強制箇所**: `scripts/snapshot.py` のローテーション処理（`_rotate_snapshots`）
- **検証手段**:
  ```bash
  cd ~/.claude/.claude-code-memory/snapshots
  comm -3 <(ls *.db | sed 's/\.db$//' | sort) <(ls *.json | sed 's/\.json$//' | sort)
  ```
  出力が無ければ正常。
- **破れると**: 復元候補一覧（`.json` 基準で運用者が目視選択する）と実データ（`.db`）が食い違う。存在しないファイルを復元しようとして失敗する、またはメタデータの無い `.db` が一覧から漏れて選択されなくなる
