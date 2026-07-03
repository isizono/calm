"""scripts/migrate.py CLIエントリポイントの疎通テスト（status / dry-run / mark / re-mark）。

subprocess経由で実際に `uv run` 相当（sys.executable）で起動し、終了コードと標準出力を検証する。
"""
import os
import subprocess
import sys
import sqlite3
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _run_cli(db_path: str, *cli_args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DISCUSSION_DB_PATH": db_path}
    return subprocess.run(
        [sys.executable, "scripts/migrate.py", *cli_args],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        env=env,
    )


class TestStatusCommand:
    def test_status_lists_all_migrations_as_applied(self, temp_db):
        result = _run_cli(temp_db, "status")
        assert result.returncode == 0, result.stderr
        assert "0001_initial_schema" in result.stdout
        assert "0049_add_migration_ledger" in result.stdout
        # temp_dbはinit_database()でfresh DBパスを通っており、全migrationが
        # applied=yes / ledger=ok になっているはず
        for line in result.stdout.splitlines():
            if line.startswith("0049_add_migration_ledger"):
                assert "yes" in line
                assert "ok" in line


class TestDryRunCommand:
    def test_dry_run_with_no_pending(self, temp_db):
        result = _run_cli(temp_db, "dry-run")
        assert result.returncode == 0, result.stderr
        assert "pending migrationはありません" in result.stdout


class TestMarkCommand:
    def test_mark_rejects_already_applied_migration(self, temp_db):
        result = _run_cli(temp_db, "mark", "0001_initial_schema")
        assert result.returncode == 1
        assert "既にapplied済み" in result.stderr

    def test_mark_unknown_migration_id(self, temp_db):
        result = _run_cli(temp_db, "mark", "9999_does_not_exist")
        assert result.returncode == 1
        assert "見つかりません" in result.stderr


class TestReMarkCommand:
    def test_re_mark_updates_ledger_hash(self, temp_db):
        conn = sqlite3.connect(temp_db)
        try:
            conn.execute(
                "UPDATE migration_ledger SET content_sha256 = 'stale' WHERE migration_id = '0001_initial_schema'"
            )
            conn.commit()
        finally:
            conn.close()

        result = _run_cli(temp_db, "re-mark", "0001_initial_schema")
        assert result.returncode == 0, result.stderr
        assert "更新しました" in result.stdout

        conn = sqlite3.connect(temp_db)
        try:
            row = conn.execute(
                "SELECT content_sha256 FROM migration_ledger WHERE migration_id = '0001_initial_schema'"
            ).fetchone()
        finally:
            conn.close()
        assert row[0] != "stale"

    def test_re_mark_unknown_migration_id(self, temp_db):
        result = _run_cli(temp_db, "re-mark", "9999_does_not_exist")
        assert result.returncode == 1
        assert "見つかりません" in result.stderr
