#!/usr/bin/env python3
"""migration運用CLI: status / dry-run / mark / re-mark

`uv run python scripts/migrate.py <command>` として実行する。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from yoyo import default_migration_table, read_migrations  # noqa: E402
from yoyo.connections import parse_uri  # noqa: E402

from src.db import (  # noqa: E402
    MIGRATIONS_DIR,
    _VecSQLiteBackend,
    _content_sha256,
    _migration_ledger_table_exists,
    dry_run_migrations,
    get_db_path,
)


def _backend_for(db_path: str) -> "_VecSQLiteBackend":
    parsed = parse_uri(f"sqlite:///{db_path}")
    backend = _VecSQLiteBackend(parsed, default_migration_table)
    backend.init_database()
    return backend


def cmd_status(_args: argparse.Namespace) -> int:
    """applied / pending / ledger照合結果の一覧表を表示する。"""
    db_path = get_db_path()
    backend = _backend_for(db_path)
    migrations = read_migrations(str(MIGRATIONS_DIR))
    applied_hashes = set(backend.get_applied_migration_hashes())

    ledger_hashes: dict[str, str] = {}
    if _migration_ledger_table_exists(backend.connection):
        rows = backend.connection.execute(
            "SELECT migration_id, content_sha256 FROM migration_ledger"
        ).fetchall()
        ledger_hashes = {row[0]: row[1] for row in rows}

    print(f"{'migration_id':<55} {'applied':<8} {'ledger':<10}")
    for m in migrations:
        applied = m.hash in applied_hashes
        if not applied:
            ledger_state = "-"
        elif m.id not in ledger_hashes:
            ledger_state = "missing"
        elif ledger_hashes[m.id] != _content_sha256(m.path):
            ledger_state = "mismatch"
        else:
            ledger_state = "ok"
        print(f"{m.id:<55} {'yes' if applied else 'no':<8} {ledger_state:<10}")
    return 0


def cmd_dry_run(_args: argparse.Namespace) -> int:
    """dry-runゲートを単独実行する（本適用しない）。"""
    db_path = get_db_path()
    backend = _backend_for(db_path)
    migrations = read_migrations(str(MIGRATIONS_DIR))
    with backend.lock():
        pending = backend.to_apply(migrations)

    if not pending:
        print("pending migrationはありません")
        return 0

    print(f"{len(pending)}件のpending migrationをdry-run適用します: {', '.join(m.id for m in pending)}")
    result = dry_run_migrations(db_path, pending)
    if result.ok:
        print("dry-run OK: 実DBへ安全に適用可能です")
        if result.row_count_regressions:
            print(f"  宣言済みの行数減少あり: {result.row_count_regressions}")
        return 0

    print(f"dry-run FAILED: failed_migration_id={result.failed_migration_id}")
    print(f"  error={result.error}")
    return 1


def cmd_mark(args: argparse.Namespace) -> int:
    """migrationを手動適用済み扱いにする（正規ハッシュでの登録をコマンド化する）。"""
    db_path = get_db_path()
    backend = _backend_for(db_path)
    migrations = read_migrations(str(MIGRATIONS_DIR))
    target = next((m for m in migrations if m.id == args.migration_id), None)
    if target is None:
        print(f"migration '{args.migration_id}' が見つかりません", file=sys.stderr)
        return 1

    with backend.lock():
        if backend.is_applied(target):
            print(f"'{args.migration_id}' は既にapplied済みです", file=sys.stderr)
            return 1
        with backend.transaction():
            backend.mark_one(target)

        if _migration_ledger_table_exists(backend.connection):
            content_hash = _content_sha256(target.path)
            backend.connection.execute(
                "INSERT INTO migration_ledger (migration_id, content_sha256, applied_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(migration_id) DO UPDATE SET "
                "content_sha256 = excluded.content_sha256, applied_at = excluded.applied_at",
                (target.id, content_hash),
            )
            backend.connection.commit()

    print(f"'{args.migration_id}' をapplied済みとしてmarkしました")
    return 0


def cmd_re_mark(args: argparse.Namespace) -> int:
    """migration_ledgerのcontent_sha256を現ファイル内容で更新する（改変の明示承認）。"""
    db_path = get_db_path()
    backend = _backend_for(db_path)
    migrations = read_migrations(str(MIGRATIONS_DIR))
    target = next((m for m in migrations if m.id == args.migration_id), None)
    if target is None:
        print(f"migration '{args.migration_id}' が見つかりません", file=sys.stderr)
        return 1
    if not backend.is_applied(target):
        print(f"'{args.migration_id}' はまだapplied済みではありません", file=sys.stderr)
        return 1
    if not _migration_ledger_table_exists(backend.connection):
        print("migration_ledgerテーブルがまだ存在しません（サーバーを一度起動してください）", file=sys.stderr)
        return 1

    content_hash = _content_sha256(target.path)
    backend.connection.execute(
        "INSERT INTO migration_ledger (migration_id, content_sha256, applied_at) "
        "VALUES (?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(migration_id) DO UPDATE SET "
        "content_sha256 = excluded.content_sha256, applied_at = excluded.applied_at",
        (target.id, content_hash),
    )
    backend.connection.commit()
    print(f"'{args.migration_id}' のledgerハッシュを現ファイル内容で更新しました")
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="migration運用CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="applied / pending / ledger照合結果の一覧表")
    sub.add_parser("dry-run", help="dry-runゲートを単独実行する（本適用しない）")

    p_mark = sub.add_parser("mark", help="手動適用済み扱いにする")
    p_mark.add_argument("migration_id")

    p_re_mark = sub.add_parser("re-mark", help="ledgerのcontent_sha256を現ファイルで更新する")
    p_re_mark.add_argument("migration_id")

    return parser


_HANDLERS = {
    "status": cmd_status,
    "dry-run": cmd_dry_run,
    "mark": cmd_mark,
    "re-mark": cmd_re_mark,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return _HANDLERS[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
