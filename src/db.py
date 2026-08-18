"""データベース接続と初期化を管理するモジュール"""
import hashlib
import sqlite3
import os
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import sqlite_vec
from yoyo import read_migrations
from yoyo import default_migration_table
from yoyo.backends import SQLiteBackend
from yoyo.connections import parse_uri

from src.env_compat import env_get

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


def get_db_path() -> str:
    """データベースファイルのパスを取得する"""
    from src.config import DB_PATH

    # config.pyのDB_PATH（モジュールインポート時に解決済み）を優先
    if DB_PATH:
        return DB_PATH

    # 実行時の環境変数もチェック（テスト互換: テスト中に動的設定されるケース）
    db_path = env_get("CALM_DB_PATH") or os.environ.get("DISCUSSION_DB_PATH")
    if db_path:
        return db_path

    # デフォルトは ~/.claude/.claude-code-memory/discussion.db
    home = Path.home()
    db_dir = home / ".claude" / ".claude-code-memory"
    db_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    return str(db_dir / "discussion.db")


def verify_sqlite_vec() -> None:
    """sqlite-vec拡張が利用可能か起動時にチェックする。

    失敗時はわかりやすいエラーメッセージを出力してSystemExitを送出する。
    サーバー起動前（init_database前）に呼ぶこと。
    """
    conn = sqlite3.connect(":memory:")
    try:
        # Step 1: enable_load_extensionの有無チェック（パターンA: pyenv問題）
        if not hasattr(conn, "enable_load_extension"):
            logger.error(
                "sqlite-vec startup check failed: "
                "sqlite3.Connection.enable_load_extension() is not available.\n"
                "Your Python was built without --enable-loadable-sqlite-extensions.\n"
                "Fix: use Homebrew Python or rebuild with the flag.\n"
                "  brew install python@3.12\n"
                "  UV_PYTHON=/opt/homebrew/opt/python@3.12/bin/python3.12 uv sync"
            )
            raise SystemExit(1)

        try:
            conn.enable_load_extension(True)
        except AttributeError:
            logger.error(
                "sqlite-vec startup check failed: "
                "enable_load_extension() exists but is not callable.\n"
                "Your Python was built without --enable-loadable-sqlite-extensions.\n"
                "Fix: use Homebrew Python or rebuild with the flag.\n"
                "  brew install python@3.12\n"
                "  UV_PYTHON=/opt/homebrew/opt/python@3.12/bin/python3.12 uv sync"
            )
            raise SystemExit(1)

        # Step 2: sqlite_vec.load()の成否チェック（パターンB: ネイティブ拡張非互換）
        try:
            sqlite_vec.load(conn)
        except Exception as e:
            logger.error(
                "sqlite-vec startup check failed: "
                f"native extension could not be loaded: {e}\n"
                "The sqlite-vec binary is incompatible with your environment.\n"
                "Fix: reinstall sqlite-vec or use a compatible Python build.\n"
                "  UV_PYTHON=/opt/homebrew/opt/python@3.12/bin/python3.12 uv sync"
            )
            raise SystemExit(1)
        finally:
            conn.enable_load_extension(False)
    finally:
        conn.close()

    logger.info("sqlite-vec startup check passed")


def get_connection(load_vec: bool = True) -> sqlite3.Connection:
    """データベース接続を取得する

    Args:
        load_vec: sqlite-vecネイティブ拡張をロードするか。デフォルトTrue。
            ベクトル検索を使わない呼び出し元（例: delta_middlewareの純relational
            クエリ）はFalseを指定することで、拡張ロード（enable_load_extension +
            sqlite_vec.load）のコストを毎回の接続オープンから省ける。
    """
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row  # 辞書ライクなアクセスを可能にする
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys = ON")  # 外部キー制約を有効化
    if load_vec:
        try:
            _load_sqlite_vec(conn)
        except Exception:
            logger.warning("sqlite-vec could not be loaded. Vector search will be unavailable.")
    return conn


def _load_sqlite_vec(conn: sqlite3.Connection) -> None:
    """sqlite-vec拡張をコネクションにロードする"""
    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)


class _VecSQLiteBackend(SQLiteBackend):
    """sqlite-vec拡張をロードするSQLiteBackend

    yoyoの内部API（SQLiteBackend, parse_uri, default_migration_table）に依存。
    pyproject.tomlでyoyo-migrationsのメジャーバージョンをピン留めすること。
    """

    def connect(self, dburi) -> sqlite3.Connection:
        conn = super().connect(dburi)
        try:
            _load_sqlite_vec(conn)
        except Exception:
            logger.warning("sqlite-vec could not be loaded. Vector search will be unavailable.")
        return conn


def _apply_migrations() -> None:
    """yoyoマイグレーションを安全化パイプラインで適用する。

    新規DB（適用済みmigrationゼロ）は防護をスキップして素通しする。
    既存DBは (1) migration_ledgerの内容ハッシュ検証 → (2) premigrationスナップショット
    → (3) 実DBコピーへのdry-run適用 → (4) dry-run成功時のみ本適用、の順で進む。
    dry-runが失敗した場合、実DBには一切変更を加えずに停止する。
    """
    db_path = get_db_path()
    parsed = parse_uri(f"sqlite:///{db_path}")
    backend = _VecSQLiteBackend(parsed, default_migration_table)
    backend.init_database()
    migrations = read_migrations(str(MIGRATIONS_DIR))

    with backend.lock():
        pending = backend.to_apply(migrations)

        if _is_fresh_database(backend):
            _apply_pending_and_record(backend, pending)
            return

        ledger_existed_before = _migration_ledger_table_exists(backend.connection)
        if ledger_existed_before:
            mismatches = verify_migration_ledger(backend.connection, migrations)
            if mismatches:
                _handle_hash_mismatch(mismatches)
            # 本適用（yoyo側コミット）とledger記録が別コミットのため、その間で
            # プロセスが落ちると「適用済みだがledger未記録」のmigrationが残る。
            # 毎起動でこの欠落を補填する（INSERT OR IGNOREで既存エントリは不変）。
            _backfill_migration_ledger(backend.connection, backend, migrations)

        if not pending:
            return

        from src.config import CALM_MIGRATION_DRYRUN, CALM_MIGRATION_SNAPSHOT

        snapshot_path: str | None = None
        if CALM_MIGRATION_SNAPSHOT:
            snapshot_path = _take_premigration_snapshot(db_path, pending)

        if CALM_MIGRATION_DRYRUN:
            _run_dry_run_gate(db_path, pending, snapshot_path)

        _apply_pending_and_record(backend, pending, guide_snapshot_path=snapshot_path)

        if not ledger_existed_before and _migration_ledger_table_exists(backend.connection):
            _backfill_migration_ledger(backend.connection, backend, migrations)


def _is_fresh_database(backend: "_VecSQLiteBackend") -> bool:
    """適用済みmigrationが1件も無ければ新規DBとみなす（守るべきデータが無い）。"""
    return len(backend.get_applied_migration_hashes()) == 0


def _apply_sequentially(backend: "_VecSQLiteBackend", migrations) -> tuple[str | None, Exception | None]:
    """migrationsを1件ずつ適用する。

    yoyoのapply_migrations()を使わず手動でループするのは、例外発生時にどのmigration
    で失敗したかをmigration_idとして特定するため。成功時は(None, None)を返す。
    """
    for m in migrations:
        try:
            backend.apply_one(m)
        except Exception as e:
            return m.id, e
    backend.run_post_apply(migrations)
    return None, None


def _apply_pending_and_record(
    backend: "_VecSQLiteBackend", pending, *, guide_snapshot_path: str | None = None
) -> None:
    """pendingを本適用し、migration_ledgerが存在すれば内容ハッシュを記録する。

    本適用の失敗はdry-runゲート成功後であれば環境要因（ディスク・ロック等）に限られる
    と考えられるが、その場合もpremigrationスナップショットからの復旧手順を案内する。
    """
    if not pending:
        return
    failed_id, error = _apply_sequentially(backend, pending)
    if error is not None:
        message = (
            "migration の本適用に失敗しました。\n"
            f"  failed_migration_id={failed_id}\n"
            f"  error={error}\n"
        )
        if guide_snapshot_path:
            message += (
                f"  復元手順: uv run python scripts/snapshot.py restore {guide_snapshot_path}\n"
            )
        logger.error(message)
        raise SystemExit(1) from error
    if _migration_ledger_table_exists(backend.connection):
        _record_content_hashes(backend.connection, pending)


# =============================================
# migration_ledger（内容ハッシュ検証）
# =============================================


def _migration_ledger_table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='migration_ledger'"
    ).fetchone()
    return row is not None


def _content_sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_migration_ledger(conn: sqlite3.Connection, migrations) -> list[dict]:
    """migration_ledgerの内容ハッシュと現ファイルを照合し、不一致のエントリを返す。

    ファイルが現存しない適用済みID（migrations一覧から見つからないledger行）は対象外とする。
    """
    path_by_id = {m.id: m.path for m in migrations}
    mismatches = []
    for row in conn.execute("SELECT migration_id, content_sha256 FROM migration_ledger"):
        migration_id, recorded_hash = row[0], row[1]
        path = path_by_id.get(migration_id)
        if path is None or not os.path.exists(path):
            continue
        current_hash = _content_sha256(path)
        if current_hash != recorded_hash:
            mismatches.append(
                {"migration_id": migration_id, "recorded": recorded_hash, "current": current_hash}
            )
    return mismatches


def _handle_hash_mismatch(mismatches: list[dict]) -> None:
    from src.config import CALM_MIGRATION_HASH_ENFORCE

    lines = [
        "migration_ledger 内容ハッシュ不一致を検出しました"
        "（想定原因: worktree混在 / 手編集 / ブランチ差し替え）。"
    ]
    for m in mismatches:
        lines.append(f"  - {m['migration_id']}: recorded={m['recorded']} current={m['current']}")
    lines.append(
        "対処: `git checkout -- <file>` でファイルを復元するか、"
        "意図的な変更であれば `uv run python scripts/migrate.py re-mark <id>` で承認し直してください。"
    )
    message = "\n".join(lines)

    if CALM_MIGRATION_HASH_ENFORCE == "warn":
        logger.warning(message)
        return

    logger.error(message)
    raise SystemExit(1)


def _record_content_hashes(conn: sqlite3.Connection, migrations) -> None:
    """指定migrationsの内容ハッシュをledgerにupsertする。"""
    for m in migrations:
        content_hash = _content_sha256(m.path)
        conn.execute(
            """
            INSERT INTO migration_ledger (migration_id, content_sha256, applied_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(migration_id) DO UPDATE SET
                content_sha256 = excluded.content_sha256,
                applied_at = excluded.applied_at
            """,
            (m.id, content_hash),
        )
    conn.commit()


def _backfill_migration_ledger(conn: sqlite3.Connection, backend: "_VecSQLiteBackend", migrations) -> None:
    """ledger未登録の既存適用済みmigrationを、現存ファイルの内容ハッシュで埋める。

    「現在のファイルが適用当時のものである」ことを仮定する。ledger導入時の一括登録と、
    本適用とledger記録の間でのクラッシュで生じた欠落の補填（毎起動のreconcile）の両方で使う。
    既存エントリは上書きしない（INSERT OR IGNORE）。
    ファイルが現存しない適用済みIDは対象外（警告ログのみ、ledger未登録のまま）。
    """
    applied_hashes = set(backend.get_applied_migration_hashes())
    applied_ids = {m.id for m in migrations if m.hash in applied_hashes}
    existing = {row[0] for row in conn.execute("SELECT migration_id FROM migration_ledger")}
    to_backfill = applied_ids - existing
    if not to_backfill:
        return

    path_by_id = {m.id: m.path for m in migrations}
    for migration_id in sorted(to_backfill):
        path = path_by_id.get(migration_id)
        if path is None or not os.path.exists(path):
            logger.warning(
                f"migration_ledger backfill: 適用済みmigration '{migration_id}' のファイルが"
                "現存しないためスキップします"
            )
            continue
        content_hash = _content_sha256(path)
        conn.execute(
            "INSERT OR IGNORE INTO migration_ledger (migration_id, content_sha256, applied_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP)",
            (migration_id, content_hash),
        )
    conn.commit()


# =============================================
# premigrationスナップショット
# =============================================


def _take_premigration_snapshot(db_path: str, pending) -> str:
    """migration前のpremigrationスナップショットを取得し、パスを返す。

    取得失敗（ディスク満杯等）は「バックアップ無しでmigrationを進める」より復旧不能
    リスクが大きいため、migrationを中断する。
    """
    from src.services.backup_service import take_snapshot

    try:
        path = take_snapshot(
            db_path,
            kind="premigration",
            extra_metadata={"pending_migrations": [m.id for m in pending]},
        )
        return str(path)
    except Exception as e:
        logger.error(f"premigration スナップショット取得に失敗したため migration を中断します: {e}")
        raise SystemExit(1) from e


# =============================================
# dry-runゲート
# =============================================


@dataclass
class DryRunResult:
    """dry_run_migrations()の結果。"""
    ok: bool
    error: str | None = None
    failed_migration_id: str | None = None
    row_count_regressions: dict[str, tuple[int, int]] = field(default_factory=dict)


def _copy_db_file(src_path: str, dest_path: Path) -> None:
    source = sqlite3.connect(src_path)
    try:
        dest = sqlite3.connect(str(dest_path))
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()


def _cleanup_db_file(db_path: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)


def _pragma_integrity_check(db_path) -> tuple[bool, str]:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        result = row[0] if row else "unknown"
        return result == "ok", result
    finally:
        conn.close()


def _pragma_foreign_key_check(db_path) -> list:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        conn.close()


def dry_run_migrations(db_path: str, pending) -> DryRunResult:
    """実DBのコピーに対してpendingを適用し、成功判定する。実DBには一切触れない。

    成功判定: (i) 全pendingが例外なく適用完了, (ii) integrity_checkがok,
    (iii) foreign_key_checkが空, (iv) HEALTH_CHECK_TABLESの行数減少が
    SNAPSHOT_ANOMALY_THRESHOLD以上のテーブルがあり、かつpending中に破壊的変更が
    未宣言（`-- destructive:`ヘッダ無し）のmigrationが含まれる場合は失敗扱いにする
    （意図しないデータ破壊と宣言済みのデータ整理を区別する）。
    """
    from src.config import SNAPSHOT_ANOMALY_THRESHOLD
    from src.services.backup_service import HEALTH_CHECK_TABLES, get_row_counts
    from scripts.migration_lint import lint_files

    if not pending:
        return DryRunResult(ok=True)

    tmp_dir = Path(db_path).parent / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp_db_path = tmp_dir / f"dryrun_{uuid.uuid4().hex}.db"

    try:
        before_counts = get_row_counts(db_path)
        _copy_db_file(db_path, tmp_db_path)

        parsed = parse_uri(f"sqlite:///{tmp_db_path}")
        tmp_backend = _VecSQLiteBackend(parsed, default_migration_table)
        tmp_backend.init_database()
        try:
            # コピー元の実DBが安全化パイプライン自身のbackend.lock()保持中にコピーされるため、
            # yoyo_lockの行がそのままコピーに複製される。コピーはこのプロセス専有の使い捨て
            # ファイルで他プロセスと競合し得ないため、advisory lockを取らずに直接適用する
            # （取ろうとすると自分自身がコピーしたロック行と衝突しLockTimeoutになる）。
            failed_id, error = _apply_sequentially(tmp_backend, pending)
        finally:
            tmp_backend.connection.close()

        if error is not None:
            return DryRunResult(ok=False, error=str(error), failed_migration_id=failed_id)

        integrity_ok, integrity_result = _pragma_integrity_check(tmp_db_path)
        if not integrity_ok:
            return DryRunResult(ok=False, error=f"PRAGMA integrity_check failed: {integrity_result}")

        fk_violations = _pragma_foreign_key_check(tmp_db_path)
        if fk_violations:
            return DryRunResult(
                ok=False, error=f"PRAGMA foreign_key_check: {len(fk_violations)} 件の違反を検出"
            )

        after_counts = get_row_counts(str(tmp_db_path))
        regressions = {
            table: (before_counts.get(table, 0), after_counts.get(table, 0))
            for table in HEALTH_CHECK_TABLES
            if before_counts.get(table, 0) - after_counts.get(table, 0) >= SNAPSHOT_ANOMALY_THRESHOLD
        }

        if regressions:
            lint_results = lint_files([Path(m.path) for m in pending])
            undeclared_destructive = any(
                lr.is_destructive and not lr.destructive_declared for lr in lint_results
            )
            if undeclared_destructive:
                return DryRunResult(
                    ok=False,
                    error=f"未宣言の破壊的変更による行数減少を検出: {regressions}",
                    row_count_regressions=regressions,
                )

        return DryRunResult(ok=True, row_count_regressions=regressions)
    finally:
        _cleanup_db_file(tmp_db_path)


def _run_dry_run_gate(db_path: str, pending, snapshot_path: str | None) -> None:
    result = dry_run_migrations(db_path, pending)
    if result.ok:
        return

    message = (
        "migration dry-run に失敗しました。実DBは変更されていません。\n"
        f"  failed_migration_id={result.failed_migration_id}\n"
        f"  error={result.error}\n"
    )
    if snapshot_path:
        message += f"  premigration snapshot: {snapshot_path}\n"
    message += (
        "対処: migration ファイルを修正するか "
        "'uv run python scripts/migrate.py dry-run' で再確認してください。"
    )
    logger.error(message)
    raise SystemExit(1)


def init_database() -> None:
    """データベースを初期化する（マイグレーション適用と初期データ投入）"""
    _apply_migrations()

    conn = get_connection()
    try:
        # 初期データの投入（subjects廃止後はタグベースで初期トピックを作成）
        # discussion_topicsにはtitleのUNIQUE制約がないため、存在確認してから挿入
        cursor = conn.execute(
            "SELECT id FROM discussion_topics WHERE title = 'first_topic'"
        )
        if cursor.fetchone() is None:
            conn.execute(
                """
                INSERT INTO discussion_topics (title, description)
                VALUES ('first_topic', 'これはサンプルのトピックです。トピックは「この会話を一言で表すと？」に答えられる粒度が目安です。例：「[議論] ユーザー認証に使う外部サービスの選定」「[設計] エラーAPIのレスポンス形式」「[作業] 商品詳細→カート画面遷移時のクラッシュ」など。新しい話題が出てきたら、新しいトピックを作成してください。')
                """
            )
            topic_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            # 初期トピックにdomain:defaultタグを付与
            from src.services.tag_service import ensure_tag_ids, link_tags
            tag_ids = ensure_tag_ids(conn, [("domain", "default")])
            link_tags(conn, "topic_tags", "topic_id", topic_id, tag_ids)

        # FTS5初期マイグレーション
        _migrate_fts5_search_index(conn)

        conn.commit()
    finally:
        conn.close()


def _check_fts5_available(conn: sqlite3.Connection) -> bool:
    """FTS5が利用可能か確認する"""
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_check USING fts5(x)")
        conn.execute("DROP TABLE IF EXISTS _fts5_check")
        return True
    except sqlite3.OperationalError:
        return False


def _migrate_fts5_search_index(conn: sqlite3.Connection) -> None:
    """FTS5検索インデックスの初期データマイグレーション（contentless方式）"""
    if not _check_fts5_available(conn):
        logger.warning("FTS5 is not available. Skipping search index migration.")
        return

    # search_indexが空の場合のみ実行
    cursor = conn.execute("SELECT COUNT(*) FROM search_index")
    if cursor.fetchone()[0] > 0:
        return  # 既にデータがある場合はスキップ

    # topics
    conn.execute("""
        INSERT OR IGNORE INTO search_index (source_type, source_id, title)
        SELECT 'topic', id, title
        FROM discussion_topics
    """)

    # decisions（topic_idは常にNOT NULL）
    conn.execute("""
        INSERT OR IGNORE INTO search_index (source_type, source_id, title)
        SELECT 'decision', d.id, d.decision
        FROM decisions d
    """)

    # activities
    conn.execute("""
        INSERT OR IGNORE INTO search_index (source_type, source_id, title)
        SELECT 'activity', id, title
        FROM activities
    """)

    # materials
    conn.execute("""
        INSERT OR IGNORE INTO search_index (source_type, source_id, title)
        SELECT 'material', id, title
        FROM materials
    """)

    # FTS5インデックスにデータを投入（contentless方式ではrebuildが使えない）
    conn.execute("""
        INSERT INTO search_index_fts (rowid, title, body)
        SELECT si.id, si.title,
          COALESCE(
            CASE si.source_type
              WHEN 'topic' THEN (SELECT description FROM discussion_topics WHERE id = si.source_id)
              WHEN 'decision' THEN (SELECT reason FROM decisions WHERE id = si.source_id)
              WHEN 'activity' THEN (SELECT description FROM activities WHERE id = si.source_id)
              WHEN 'material' THEN (SELECT content FROM materials WHERE id = si.source_id)
            END,
            ''
          )
        FROM search_index si
    """)


def execute_query(query: str, params: tuple = ()) -> list[sqlite3.Row]:
    """SELECT クエリを実行して結果を返す"""
    conn = get_connection()
    try:
        cursor = conn.execute(query, params)
        return cursor.fetchall()
    except sqlite3.Error as e:
        raise sqlite3.Error(f"クエリ実行エラー: {e}") from e
    finally:
        conn.close()


def execute_insert(query: str, params: tuple = ()) -> int:
    """INSERT クエリを実行して新しいIDを返す"""
    conn = get_connection()
    try:
        cursor = conn.execute(query, params)
        conn.commit()
        return cursor.lastrowid
    except sqlite3.IntegrityError:
        conn.rollback()
        raise
    except sqlite3.Error as e:
        conn.rollback()
        raise sqlite3.Error(f"INSERT実行エラー: {e}") from e
    finally:
        conn.close()


def row_to_dict(row: sqlite3.Row) -> dict:
    """sqlite3.Row を辞書に変換する"""
    return dict(row)
