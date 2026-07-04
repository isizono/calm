"""DBスナップショット: 取得・ヘルスチェック・ローテーション・復元

種別（kind）ごとにディレクトリと保持世代数を分けて管理する。
periodicはSessionStart hookから呼び出される既存経路との互換のため、
DBと同階層の snapshots/ 直下に据え置く。他の種別は snapshots/<kind>/ に置く。
"""
import argparse
import json
import logging
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, get_args

from src.config import SNAPSHOT_MAX_COUNT

logger = logging.getLogger(__name__)

HEALTH_CHECK_TABLES = [
    "discussion_topics",
    "decisions",
    "discussion_logs",
    "activities",
    "materials",
]

SNAPSHOT_PREFIX = "discussion_"
SNAPSHOT_DB_SUFFIX = ".db"
SNAPSHOT_JSON_SUFFIX = ".json"

SnapshotKind = Literal["periodic", "premigration", "prerestore", "manual", "daily"]

KIND_QUOTAS: dict[str, int] = {
    "periodic": SNAPSHOT_MAX_COUNT,
    "premigration": 5,
    "prerestore": 2,
    "manual": 3,
    "daily": 7,
}


@dataclass
class HealthCheckResult:
    """ヘルスチェック結果"""
    is_healthy: bool = True
    warnings: list[str] = field(default_factory=list)
    current_counts: dict[str, int] = field(default_factory=dict)
    previous_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class RestoreResult:
    """restore_snapshot() の結果"""
    restored_from: str
    db_path: str
    file_copy: bool
    forced: bool
    prerestore_path: str | None
    prerestore_row_counts: dict[str, int] = field(default_factory=dict)
    restored_row_counts: dict[str, int] = field(default_factory=dict)
    compatibility_note: str | None = None


class RestoreBlockedError(RuntimeError):
    """安全装置（稼働中チェック・整合性検証・互換性警告）により復元がブロックされたことを示す。"""


# =============================================
# ディレクトリ・メタデータ
# =============================================


def snapshot_dir_for(db_path: str, kind: SnapshotKind = "periodic") -> Path:
    """kind別のスナップショット保存ディレクトリを返す。

    periodicはDBと同階層のsnapshots/直下（既存互換）、他はそのkind名のサブディレクトリ。
    """
    base = Path(db_path).parent / "snapshots"
    if kind == "periodic":
        return base
    return base / kind


def get_row_counts(db_path: str) -> dict[str, int]:
    """各テーブルのCOUNTを取得する"""
    conn = sqlite3.connect(db_path)
    try:
        counts = {}
        for table in HEALTH_CHECK_TABLES:
            try:
                cursor = conn.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
                counts[table] = cursor.fetchone()[0]
            except sqlite3.OperationalError:
                # テーブルが存在しない場合はスキップ
                counts[table] = 0
        return counts
    finally:
        conn.close()


def _get_latest_json(snapshot_dir: Path) -> dict | None:
    """指定ディレクトリ直下の最新メタデータJSONを読み込む。なければNone。"""
    if not snapshot_dir.exists():
        return None

    json_files = sorted(snapshot_dir.glob(f"{SNAPSHOT_PREFIX}*{SNAPSHOT_JSON_SUFFIX}"), reverse=True)
    if not json_files:
        return None

    try:
        return json.loads(json_files[0].read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _date_part(filename: str) -> str:
    """スナップショットファイル名から日付部分（YYYYMMDD相当の先頭セグメント）を取り出す。"""
    stem = filename
    for suffix in (SNAPSHOT_DB_SUFFIX, SNAPSHOT_JSON_SUFFIX):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    stem = stem[len(SNAPSHOT_PREFIX):]
    return stem.split("_")[0]


def _get_schema_head(db_path: str) -> str | None:
    """DBの_yoyo_migrationテーブルから最終適用migration_idを取得する。

    yoyoの既定migrationテーブル名（`yoyo.default_migration_table`）は単数形の
    `_yoyo_migration` であり、`src/db.py` の`_apply_migrations()`もこの名前を使う。
    テーブルが存在しない（yoyo未適用・破損DB等）場合はNoneを返す。
    """
    try:
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.execute(
                "SELECT migration_id FROM _yoyo_migration ORDER BY applied_at_utc DESC LIMIT 1"
            )
            row = cur.fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def _current_migration_ids() -> list[str]:
    """現行コードのmigrations/配下のIDを依存関係順（yoyoの解決順）で返す。"""
    from yoyo import read_migrations

    from src.db import MIGRATIONS_DIR

    migrations = read_migrations(str(MIGRATIONS_DIR))
    return [m.id for m in migrations]


def _quick_check(snapshot_db_path: Path) -> str:
    """PRAGMA quick_checkを実行し結果文字列を返す。破損検知目的でスナップショット取得直後に呼ぶ。"""
    try:
        conn = sqlite3.connect(str(snapshot_db_path))
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
            result = row[0] if row else "unknown"
        finally:
            conn.close()
        if result != "ok":
            logger.warning(f"snapshot quick_check failed: {snapshot_db_path} -> {result}")
        return result
    except sqlite3.Error as e:
        logger.warning(f"snapshot quick_check errored: {snapshot_db_path}: {e}")
        return f"error: {e}"


# =============================================
# ヘルスチェック・間隔判定（既存挙動を維持）
# =============================================


def health_check(db_path: str, snapshot_dir: Path | None = None, threshold: int | None = None) -> HealthCheckResult:
    """最新JSONと現在の行数を比較してヘルスチェックを行う。

    threshold件以上の減少があるテーブルを異常と判定する。
    """
    from src.config import SNAPSHOT_ANOMALY_THRESHOLD

    if snapshot_dir is None:
        snapshot_dir = snapshot_dir_for(db_path, "periodic")
    if threshold is None:
        threshold = SNAPSHOT_ANOMALY_THRESHOLD

    current_counts = get_row_counts(db_path)
    result = HealthCheckResult(current_counts=current_counts)

    latest_json = _get_latest_json(snapshot_dir)
    if latest_json is None:
        # 初回起動（スナップショットなし）: 正常扱い
        return result

    prev_counts = latest_json.get("row_counts", {})
    result.previous_counts = prev_counts

    for table in HEALTH_CHECK_TABLES:
        prev = prev_counts.get(table, 0)
        current = current_counts.get(table, 0)
        diff = prev - current
        if diff >= threshold:
            result.is_healthy = False
            result.warnings.append(
                f"- {table}: {prev} → {current} (-{diff}件)"
            )

    return result


def should_take_snapshot(snapshot_dir: Path | None = None, interval_hours: int | None = None, db_path: str | None = None) -> bool:
    """最新JSONのcreated_atで間隔チェック。取得すべきならTrue。"""
    from src.config import SNAPSHOT_INTERVAL_HOURS

    if interval_hours is None:
        interval_hours = SNAPSHOT_INTERVAL_HOURS

    if snapshot_dir is None:
        if db_path is None:
            return True
        snapshot_dir = snapshot_dir_for(db_path, "periodic")

    latest_json = _get_latest_json(snapshot_dir)
    if latest_json is None:
        # スナップショットなし: 即取得
        return True

    created_at_str = latest_json.get("created_at")
    if not created_at_str:
        return True

    try:
        created_at = datetime.fromisoformat(created_at_str)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        elapsed_hours = (now - created_at).total_seconds() / 3600
        return elapsed_hours >= interval_hours
    except (ValueError, TypeError):
        return True


# =============================================
# 取得・ローテーション
# =============================================


def _unique_snapshot_paths(snapshot_dir: Path) -> tuple[Path, Path]:
    """衝突しないスナップショットの.db/.jsonパスを発行する。

    同一秒内に複数回取得される場合（restore()のprerestore退避が短時間に連続する等）に
    既存ファイルを上書きしないよう、衝突時は連番を振って別名にする。
    """
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    base_stem = f"{SNAPSHOT_PREFIX}{timestamp}"

    stem = base_stem
    db_path = snapshot_dir / f"{stem}{SNAPSHOT_DB_SUFFIX}"
    json_path = snapshot_dir / f"{stem}{SNAPSHOT_JSON_SUFFIX}"
    suffix = 1
    while db_path.exists() or json_path.exists():
        suffix += 1
        stem = f"{base_stem}_{suffix}"
        db_path = snapshot_dir / f"{stem}{SNAPSHOT_DB_SUFFIX}"
        json_path = snapshot_dir / f"{stem}{SNAPSHOT_JSON_SUFFIX}"

    return db_path, json_path


def take_snapshot(
    db_path: str,
    snapshot_dir: Path | None = None,
    max_snapshots: int | None = None,
    kind: SnapshotKind = "periodic",
    extra_metadata: dict | None = None,
) -> Path:
    """sqlite3.backup()でスナップショットを取得し、メタデータJSONを保存する。

    取得直後にPRAGMA quick_checkを実行し結果をメタデータに記録する（破損検知）。
    ローテーション: max_snapshots超過時に古いペア(.db + .json)を処理する。
    kind="periodic"の場合のみ、削除の代わりにdaily/への昇格を試みる。
    """
    if snapshot_dir is None:
        snapshot_dir = snapshot_dir_for(db_path, kind)
    if max_snapshots is None:
        max_snapshots = KIND_QUOTAS[kind]

    snapshot_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    now = datetime.now(timezone.utc)
    snapshot_db_path, snapshot_json_path = _unique_snapshot_paths(snapshot_dir)

    # sqlite3.backup()でスナップショット取得
    source = sqlite3.connect(db_path)
    try:
        dest = sqlite3.connect(str(snapshot_db_path))
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()

    quick_check_result = _quick_check(snapshot_db_path)

    # メタデータJSON保存
    row_counts = get_row_counts(db_path)
    metadata = {
        "created_at": now.isoformat(),
        "db_size_bytes": snapshot_db_path.stat().st_size,
        "row_counts": row_counts,
        "kind": kind,
        "schema_head": _get_schema_head(db_path),
        "quick_check": quick_check_result,
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    snapshot_json_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    # ローテーション
    if kind == "periodic":
        _rotate_periodic_with_promotion(db_path, snapshot_dir, max_snapshots)
    else:
        _rotate_snapshots(snapshot_dir, max_snapshots)

    return snapshot_db_path


def _rotate_snapshots(snapshot_dir: Path, max_snapshots: int) -> None:
    """max_snapshots超過時に古い.db + .jsonペアを削除する。"""
    db_files = sorted(snapshot_dir.glob(f"{SNAPSHOT_PREFIX}*{SNAPSHOT_DB_SUFFIX}"))
    while len(db_files) > max_snapshots:
        oldest_db = db_files.pop(0)
        oldest_json = oldest_db.with_suffix(SNAPSHOT_JSON_SUFFIX)
        oldest_db.unlink(missing_ok=True)
        oldest_json.unlink(missing_ok=True)


def _rotate_periodic_with_promotion(db_path: str, periodic_dir: Path, max_snapshots: int) -> None:
    """periodicのローテーション。

    最古を削除する代わりに、その日付のdaily/スナップショットが未存在なら
    削除せずdaily/へ移動（rename）する。移動先が既にあれば通常通り削除する。
    daily/自体は独立クォータ（KIND_QUOTAS["daily"]）でローテーションする。
    """
    daily_dir = snapshot_dir_for(db_path, "daily")
    daily_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    daily_dates = {
        _date_part(p.name) for p in daily_dir.glob(f"{SNAPSHOT_PREFIX}*{SNAPSHOT_DB_SUFFIX}")
    }

    db_files = sorted(periodic_dir.glob(f"{SNAPSHOT_PREFIX}*{SNAPSHOT_DB_SUFFIX}"))
    while len(db_files) > max_snapshots:
        oldest_db = db_files.pop(0)
        oldest_json = oldest_db.with_suffix(SNAPSHOT_JSON_SUFFIX)
        date_part = _date_part(oldest_db.name)

        if date_part in daily_dates:
            oldest_db.unlink(missing_ok=True)
            oldest_json.unlink(missing_ok=True)
        else:
            oldest_db.rename(daily_dir / oldest_db.name)
            if oldest_json.exists():
                oldest_json.rename(daily_dir / oldest_json.name)
            daily_dates.add(date_part)

    _rotate_snapshots(daily_dir, KIND_QUOTAS["daily"])


def list_snapshots(db_path: str) -> list[dict]:
    """全kind横断のスナップショット一覧をメタデータ込みで返す（created_at降順）。"""
    results: list[dict] = []
    for kind in get_args(SnapshotKind):
        snap_dir = snapshot_dir_for(db_path, kind)
        if not snap_dir.exists():
            continue
        for json_path in sorted(snap_dir.glob(f"{SNAPSHOT_PREFIX}*{SNAPSHOT_JSON_SUFFIX}")):
            db_file = json_path.with_suffix(SNAPSHOT_DB_SUFFIX)
            if not db_file.exists():
                continue
            try:
                metadata = json.loads(json_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            entry = {
                **metadata,
                "kind": metadata.get("kind", kind),
                "db_path": str(db_file),
                "json_path": str(json_path),
            }
            results.append(entry)

    results.sort(key=lambda e: e.get("created_at") or "", reverse=True)
    return results


# =============================================
# 検証
# =============================================


def verify_snapshot(snapshot_path: str) -> dict:
    """PRAGMA integrity_check + 行数取得でスナップショットの整合性を検証する。

    破損ファイル（途中切断コピー・不正な内容）はok=Falseで返す。
    """
    path = Path(snapshot_path)
    if not path.exists():
        return {"path": str(path), "ok": False, "error": f"ファイルが見つかりません: {snapshot_path}"}

    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as e:
        return {"path": str(path), "ok": False, "error": str(e)}

    try:
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            integrity = row[0] if row else "unknown"
        except sqlite3.DatabaseError as e:
            return {"path": str(path), "ok": False, "error": str(e), "integrity_check": None}

        ok = integrity == "ok"
        row_counts: dict[str, int] = {}
        if ok:
            for table in HEALTH_CHECK_TABLES:
                try:
                    cur = conn.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
                    row_counts[table] = cur.fetchone()[0]
                except sqlite3.OperationalError:
                    row_counts[table] = 0

        return {
            "path": str(path),
            "ok": ok,
            "integrity_check": integrity,
            "row_counts": row_counts,
            "db_size_bytes": path.stat().st_size,
        }
    finally:
        conn.close()


# =============================================
# 復元
# =============================================


@dataclass
class _CompatibilityCheck:
    note: str | None = None       # 情報提供のみ。非ブロッキング
    warning: str | None = None    # --yes必須のブロッキング警告


def _check_schema_compatibility(snapshot_schema_head: str | None) -> _CompatibilityCheck:
    """スナップショットのschema_headと現行コードのmigrations/を比較する。"""
    if snapshot_schema_head is None:
        return _CompatibilityCheck(
            note="スナップショットにschema_head記録が無く、互換性判定をスキップしました。"
        )

    try:
        current_ids = _current_migration_ids()
    except Exception as e:
        logger.warning(f"migration一覧の取得に失敗、互換性判定をスキップ: {e}")
        return _CompatibilityCheck()

    if not current_ids:
        return _CompatibilityCheck()

    if snapshot_schema_head == current_ids[-1]:
        return _CompatibilityCheck()

    if snapshot_schema_head in current_ids:
        return _CompatibilityCheck(
            note=(
                f"スナップショットのschema_head（{snapshot_schema_head}）は現行コードより古いです。"
                "復元後、次回サーバー起動時に差分migrationが自動適用されます。"
            )
        )

    return _CompatibilityCheck(
        warning=(
            f"スナップショットのschema_head（{snapshot_schema_head}）は現行コードのmigrations/に"
            "存在しません。コードが巻き戻された可能性があります。"
        )
    )


def _check_health_endpoint(timeout: float = 2.0) -> bool:
    """ローカルHTTPサーバーの/healthエンドポイント疎通確認。"""
    import urllib.error
    import urllib.request

    from src.http_config import HTTP_HOST, HTTP_PORT

    url = f"http://{HTTP_HOST}:{HTTP_PORT}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return resp.status == 200
    except Exception:
        return False


def _server_appears_running() -> tuple[bool, str]:
    """lock file生存 または /healthエンドポイント応答のいずれかでrunningと判定する。"""
    from src.services.lock_file import is_process_alive
    from src.services.lock_file import read as read_lock

    reasons = []
    running = False

    info = read_lock()
    if info is not None and is_process_alive(info["pid"]):
        running = True
        reasons.append(f"lock file: pid={info['pid']} port={info['port']}")

    if _check_health_endpoint():
        running = True
        reasons.append("/health エンドポイントが応答")

    return running, "; ".join(reasons)


def restore_snapshot(
    snapshot_path: str,
    db_path: str | None = None,
    *,
    force: bool = False,
    file_copy: bool = False,
    yes: bool = False,
) -> RestoreResult:
    """スナップショットからDBを復元する。

    db_pathが未指定の場合はCCM_DB_PATHまたはデフォルトパスから解決する。

    防護フロー:
      1. サーバー稼働チェック（lock file + /health）。稼働中はforce=Trueでのみ続行
      2. スナップショットの整合性検証（verify_snapshot）。失敗時は常に中断
      3. schema_head比較による互換性警告。巻き戻し疑いはyes=Trueでのみ続行
      4. 復元前の現行DBをkind=prerestoreで自動退避（backup API不可なら生ファイルコピー）
      5. 復元本体（既定はsqlite3.backup()、file_copy=Trueでファイル直接置換 + -wal/-shm削除）
    """
    from src.db import get_db_path

    if db_path is None:
        db_path = get_db_path()

    snapshot_file = Path(snapshot_path)
    if not snapshot_file.exists():
        raise FileNotFoundError(f"スナップショットが見つかりません: {snapshot_path}")

    # 1. サーバー稼働チェック
    running, detail = _server_appears_running()
    if running and not force:
        raise RestoreBlockedError(
            "サーバーが稼働中のため復元を中断しました"
            f"（{detail}）。"
            "先にサーバーを停止してください: lsof -ti :52837 | xargs kill "
            "（停止済みであることを確認の上で続行する場合は --force を指定）"
        )

    # 2. スナップショット検証
    verification = verify_snapshot(str(snapshot_file))
    if not verification.get("ok"):
        raise RestoreBlockedError(
            f"スナップショットの整合性検証に失敗しました: {snapshot_path} "
            f"({verification.get('error') or verification.get('integrity_check')})"
        )

    # 3. 互換性警告
    snapshot_json_path = snapshot_file.with_suffix(SNAPSHOT_JSON_SUFFIX)
    try:
        snapshot_metadata = json.loads(snapshot_json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        snapshot_metadata = {}
    compat = _check_schema_compatibility(snapshot_metadata.get("schema_head"))
    if compat.warning and not yes:
        raise RestoreBlockedError(f"{compat.warning} 承知の上で続行する場合は --yes を指定してください。")

    # 4. prerestore退避
    prerestore_path: str | None = None
    prerestore_row_counts: dict[str, int] = {}
    if Path(db_path).exists():
        try:
            prerestore_row_counts = get_row_counts(db_path)
            prerestore_db = take_snapshot(db_path, kind="prerestore")
            prerestore_path = str(prerestore_db)
        except sqlite3.Error:
            # backup APIで読めないほど破損している場合はファイルコピーで退避する
            prerestore_dir = snapshot_dir_for(db_path, "prerestore")
            prerestore_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            fallback_path, fallback_json_path = _unique_snapshot_paths(prerestore_dir)
            shutil.copy2(db_path, fallback_path)
            # メタデータJSONを書かないとlist_snapshots()に載らず、_rotate_snapshots()の
            # クォータ超過で無警告に削除されうる（破損DBからの復元直前という最も必要な退避）。
            fallback_metadata = {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "db_size_bytes": fallback_path.stat().st_size,
                "kind": "prerestore",
                "note": "実DBがsqlite3.backup()で読めないほど破損していたためファイルコピーで退避した",
            }
            fallback_json_path.write_text(
                json.dumps(fallback_metadata, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            prerestore_path = str(fallback_path)
            prerestore_row_counts = {}
            _rotate_snapshots(prerestore_dir, KIND_QUOTAS["prerestore"])

    # 5. 復元本体
    if file_copy:
        shutil.copy2(snapshot_file, db_path)
        # -journalを残すと次回オープン時にSQLiteが古いjournalで意図しないロールバックを試みうる。
        for suffix in ("-wal", "-shm", "-journal"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)
    else:
        source = sqlite3.connect(str(snapshot_file))
        try:
            dest = sqlite3.connect(db_path)
            try:
                source.backup(dest)
            finally:
                dest.close()
        finally:
            source.close()

    restored_row_counts = get_row_counts(db_path)

    return RestoreResult(
        restored_from=str(snapshot_file),
        db_path=db_path,
        file_copy=file_copy,
        forced=force and running,
        prerestore_path=prerestore_path,
        prerestore_row_counts=prerestore_row_counts,
        restored_row_counts=restored_row_counts,
        compatibility_note=compat.note,
    )


# =============================================
# CLI
# =============================================


def _cmd_list(args: argparse.Namespace) -> int:
    from src.db import get_db_path

    db_path = args.db_path or get_db_path()
    snapshots = list_snapshots(db_path)
    if not snapshots:
        print("スナップショットはありません")
        return 0

    print(f"{'kind':<12} {'created_at':<26} {'size_bytes':>12} {'quick_check':<10} path")
    for s in snapshots:
        print(
            f"{s.get('kind', ''):<12} {s.get('created_at', ''):<26} "
            f"{s.get('db_size_bytes', 0):>12} {s.get('quick_check', '-'):<10} {s.get('db_path', '')}"
        )
    return 0


def _cmd_take(args: argparse.Namespace) -> int:
    from src.db import get_db_path

    db_path = args.db_path or get_db_path()
    path = take_snapshot(db_path, kind=args.kind)
    print(f"取得完了: {path}")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    result = verify_snapshot(args.path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def _cmd_restore(args: argparse.Namespace) -> int:
    from src.db import get_db_path

    db_path = args.db_path or get_db_path()

    if args.latest:
        snapshots = list_snapshots(db_path)
        if not snapshots:
            print("スナップショットが見つかりません", file=sys.stderr)
            return 1
        snapshot_path = snapshots[0]["db_path"]
    elif args.path:
        snapshot_path = args.path
    else:
        print("復元元を指定してください（<path> または --latest）", file=sys.stderr)
        return 1

    try:
        result = restore_snapshot(
            snapshot_path,
            db_path,
            force=args.force,
            file_copy=args.file_copy,
            yes=args.yes,
        )
    except (RestoreBlockedError, FileNotFoundError) as e:
        print(f"復元エラー: {e}", file=sys.stderr)
        return 1

    print(f"復元完了: {result.restored_from} -> {result.db_path}")
    if result.compatibility_note:
        print(f"ℹ {result.compatibility_note}")
    if result.prerestore_path:
        print(f"復元前の状態を退避: {result.prerestore_path}")

    print("行数の変化 (復元前 -> 復元後):")
    tables = sorted(set(result.prerestore_row_counts) | set(result.restored_row_counts))
    for table in tables:
        before = result.prerestore_row_counts.get(table, "-")
        after = result.restored_row_counts.get(table, "-")
        print(f"  {table:<20} {before!s:>8} -> {after!s:>8}")
    return 0


def main() -> None:
    """CLI: list / take / verify / restore サブコマンド"""
    parser = argparse.ArgumentParser(description="cc-memory DBスナップショット管理CLI")
    parser.add_argument("--db-path", dest="db_path", default=None, help="対象DBのパス（省略時はCCM_DB_PATH等から解決）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="全kind横断のスナップショット一覧を表示する")

    p_take = sub.add_parser("take", help="スナップショットを取得する")
    p_take.add_argument("--kind", choices=list(get_args(SnapshotKind)), default="manual")

    p_verify = sub.add_parser("verify", help="スナップショットの整合性を検証する")
    p_verify.add_argument("path")

    p_restore = sub.add_parser("restore", help="スナップショットから復元する")
    p_restore.add_argument("path", nargs="?", default=None, help="復元元スナップショットのDBファイルパス")
    p_restore.add_argument("--latest", action="store_true", help="全kind横断で最新のスナップショットを使う")
    p_restore.add_argument("--force", action="store_true", help="サーバー稼働中チェックを無視して続行する")
    p_restore.add_argument("--file-copy", dest="file_copy", action="store_true", help="DBファイルを直接置換する（-wal/-shmを削除）")
    p_restore.add_argument("--yes", action="store_true", help="schema互換性警告を承知の上で続行する")

    args = parser.parse_args()

    handlers = {
        "list": _cmd_list,
        "take": _cmd_take,
        "verify": _cmd_verify,
        "restore": _cmd_restore,
    }
    sys.exit(handlers[args.command](args))


if __name__ == "__main__":
    main()
