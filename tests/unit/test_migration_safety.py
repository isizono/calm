"""migration安全化パイプラインのユニットテスト

dry-runゲート（実DB非破壊・失敗判定4条件）、migration_ledger内容ハッシュ検証、
premigrationスナップショット連動、fresh DBスキップの各挙動を検証する。

`_apply_migrations()` 全体の統合的な振る舞い（既存DB + pendingでのスナップショット
取得・dry-run失敗時の実DB無傷確認）は tests/e2e/test_migration_pipeline.py で検証する。
"""
import sqlite3
from pathlib import Path

import pytest
from yoyo import default_migration_table, read_migrations
from yoyo.connections import parse_uri

import src.db as db
from src import config


def _write_migration(dir_path: Path, filename: str, sql: str, *, destructive: str | None = None) -> Path:
    """テスト用migrationファイルを書く。depends先は実migrationsに存在する0048固定でよい
    （dry_run_migrations()はto_apply()のdepends解決を経由しないpending直渡しのため）。
    """
    header = "-- depends: 0048_session_identity\n"
    if destructive is not None:
        header += f"-- destructive: {destructive}\n"
    path = dir_path / filename
    path.write_text(f"{header}\n{sql}\n", encoding="utf-8")
    return path


def _read_pending(dir_path: Path):
    return read_migrations(str(dir_path))


def _seed_activities(db_path: str, count: int) -> None:
    conn = sqlite3.connect(db_path)
    try:
        for i in range(count):
            conn.execute(
                "INSERT INTO activities (title, description, status) VALUES (?, ?, ?)",
                (f"activity_{i}", "desc", "pending"),
            )
        conn.commit()
    finally:
        conn.close()


class TestDryRunMigrationsSuccess:
    def test_empty_pending_is_ok(self, temp_db):
        result = db.dry_run_migrations(temp_db, [])
        assert result.ok is True

    def test_valid_migration_passes_and_leaves_real_db_untouched(self, temp_db, tmp_path):
        mig_dir = tmp_path / "migs_ok"
        mig_dir.mkdir()
        _write_migration(mig_dir, "9001_probe_ok.sql", "CREATE TABLE dryrun_probe_ok (id INTEGER PRIMARY KEY);")
        pending = _read_pending(mig_dir)

        result = db.dry_run_migrations(temp_db, pending)
        assert result.ok is True

        conn = sqlite3.connect(temp_db)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='dryrun_probe_ok'"
            ).fetchone()
            assert row is None, "dry-run成功時も実DBには一切変更が加わらないはず"
        finally:
            conn.close()

    def test_tmp_copy_is_cleaned_up_after_dry_run(self, temp_db, tmp_path):
        mig_dir = tmp_path / "migs_cleanup"
        mig_dir.mkdir()
        _write_migration(mig_dir, "9001_probe_cleanup.sql", "CREATE TABLE dryrun_probe_cleanup (id INTEGER PRIMARY KEY);")
        pending = _read_pending(mig_dir)

        db.dry_run_migrations(temp_db, pending)

        tmp_dir = Path(temp_db).parent / "tmp"
        leftover = list(tmp_dir.glob("dryrun_*")) if tmp_dir.exists() else []
        assert leftover == [], f"dry-run用の一時コピーが残存している: {leftover}"


class TestDryRunMigrationsFailure:
    def test_syntax_error_migration_fails_with_migration_id(self, temp_db, tmp_path):
        mig_dir = tmp_path / "migs_syntax_error"
        mig_dir.mkdir()
        _write_migration(mig_dir, "9002_probe_syntax_error.sql", "CREATE TABLE (,,, THIS IS NOT VALID SQL;")
        pending = _read_pending(mig_dir)

        result = db.dry_run_migrations(temp_db, pending)
        assert result.ok is False
        assert result.failed_migration_id == pending[0].id
        assert result.error

    def test_fk_violation_migration_fails(self, temp_db, tmp_path):
        mig_dir = tmp_path / "migs_fk"
        mig_dir.mkdir()
        _write_migration(
            mig_dir,
            "9003_probe_fk_violation.sql",
            "INSERT INTO activity_dependencies (dependent_id, dependency_id) "
            "VALUES (999999, 999998);",
        )
        pending = _read_pending(mig_dir)

        result = db.dry_run_migrations(temp_db, pending)
        assert result.ok is False
        assert "foreign_key_check" in result.error

    def test_undeclared_destructive_regression_fails(self, temp_db, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "SNAPSHOT_ANOMALY_THRESHOLD", 2)
        _seed_activities(temp_db, 3)

        mig_dir = tmp_path / "migs_destructive_undeclared"
        mig_dir.mkdir()
        _write_migration(mig_dir, "9004_probe_destructive_undeclared.sql", "DELETE FROM activities;")
        pending = _read_pending(mig_dir)

        result = db.dry_run_migrations(temp_db, pending)
        assert result.ok is False
        assert "activities" in result.row_count_regressions

        conn = sqlite3.connect(temp_db)
        try:
            count = conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
        finally:
            conn.close()
        assert count == 3, "dry-run失敗時は実DBのactivitiesが無傷であるべき"

    def test_declared_destructive_regression_passes(self, temp_db, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "SNAPSHOT_ANOMALY_THRESHOLD", 2)
        _seed_activities(temp_db, 3)

        mig_dir = tmp_path / "migs_destructive_declared"
        mig_dir.mkdir()
        _write_migration(
            mig_dir,
            "9005_probe_destructive_declared.sql",
            "DELETE FROM activities;",
            destructive="テスト用の意図的なデータ削除",
        )
        pending = _read_pending(mig_dir)

        result = db.dry_run_migrations(temp_db, pending)
        assert result.ok is True
        assert "activities" in result.row_count_regressions

    def test_regression_below_threshold_does_not_require_declaration(self, temp_db, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "SNAPSHOT_ANOMALY_THRESHOLD", 100)
        _seed_activities(temp_db, 3)

        mig_dir = tmp_path / "migs_small_delete"
        mig_dir.mkdir()
        _write_migration(mig_dir, "9006_probe_small_delete.sql", "DELETE FROM activities;")
        pending = _read_pending(mig_dir)

        result = db.dry_run_migrations(temp_db, pending)
        assert result.ok is True, "閾値未満の行数減少は宣言が無くても失敗にならない"


class TestVerifyMigrationLedger:
    def test_no_mismatch_when_unchanged(self, temp_db, tmp_path):
        mig_path = tmp_path / "9007_probe_ledger.sql"
        mig_path.write_text("-- depends: 0048_session_identity\n\nCREATE TABLE ledger_probe_a (id INTEGER);\n", encoding="utf-8")
        content_hash = db._content_sha256(str(mig_path))

        conn = sqlite3.connect(temp_db)
        try:
            conn.execute(
                "INSERT INTO migration_ledger (migration_id, content_sha256) VALUES (?, ?)",
                ("9007_probe_ledger", content_hash),
            )
            conn.commit()

            class _FakeMigration:
                def __init__(self, id_, path_):
                    self.id = id_
                    self.path = path_

            mismatches = db.verify_migration_ledger(conn, [_FakeMigration("9007_probe_ledger", str(mig_path))])
            assert mismatches == []
        finally:
            conn.close()

    def test_mismatch_detected_after_file_edit(self, temp_db, tmp_path):
        mig_path = tmp_path / "9008_probe_ledger_edit.sql"
        mig_path.write_text("-- depends: 0048_session_identity\n\nCREATE TABLE ledger_probe_b (id INTEGER);\n", encoding="utf-8")
        original_hash = db._content_sha256(str(mig_path))

        conn = sqlite3.connect(temp_db)
        try:
            conn.execute(
                "INSERT INTO migration_ledger (migration_id, content_sha256) VALUES (?, ?)",
                ("9008_probe_ledger_edit", original_hash),
            )
            conn.commit()

            # ファイルを事後編集（改変シミュレーション）
            mig_path.write_text("-- depends: 0048_session_identity\n\nCREATE TABLE ledger_probe_b_edited (id INTEGER);\n", encoding="utf-8")

            class _FakeMigration:
                def __init__(self, id_, path_):
                    self.id = id_
                    self.path = path_

            mismatches = db.verify_migration_ledger(
                conn, [_FakeMigration("9008_probe_ledger_edit", str(mig_path))]
            )
            assert len(mismatches) == 1
            assert mismatches[0]["migration_id"] == "9008_probe_ledger_edit"
            assert mismatches[0]["recorded"] == original_hash
            assert mismatches[0]["current"] != original_hash
        finally:
            conn.close()

    def test_missing_file_is_not_a_mismatch(self, temp_db, tmp_path):
        """ledgerに記録があってもファイルが現存しないIDは検証対象外（対象外≠不一致）"""
        conn = sqlite3.connect(temp_db)
        try:
            conn.execute(
                "INSERT INTO migration_ledger (migration_id, content_sha256) VALUES (?, ?)",
                ("9009_probe_gone", "a" * 64),
            )
            conn.commit()

            class _FakeMigration:
                def __init__(self, id_, path_):
                    self.id = id_
                    self.path = path_

            missing_path = str(tmp_path / "does_not_exist.sql")
            mismatches = db.verify_migration_ledger(conn, [_FakeMigration("9009_probe_gone", missing_path)])
            assert mismatches == []
        finally:
            conn.close()


class TestHandleHashMismatch:
    def test_default_raises_system_exit(self, monkeypatch):
        monkeypatch.setattr(config, "CCM_MIGRATION_HASH_ENFORCE", "error")
        with pytest.raises(SystemExit):
            db._handle_hash_mismatch([{"migration_id": "x", "recorded": "a", "current": "b"}])

    def test_warn_mode_does_not_raise(self, monkeypatch):
        monkeypatch.setattr(config, "CCM_MIGRATION_HASH_ENFORCE", "warn")
        db._handle_hash_mismatch([{"migration_id": "x", "recorded": "a", "current": "b"}])  # raises無しでreturn


class TestBackfillMigrationLedger:
    def test_backfills_only_missing_ids(self, temp_db):
        """fresh DB (temp_db) は全migrationが既にledger記録済みのため、backfillは何も追加しない"""
        conn = sqlite3.connect(temp_db)
        try:
            before = {row[0] for row in conn.execute("SELECT migration_id FROM migration_ledger")}
        finally:
            conn.close()

        parsed = parse_uri(f"sqlite:///{temp_db}")
        backend = db._VecSQLiteBackend(parsed, default_migration_table)
        backend.init_database()
        migrations = read_migrations(str(db.MIGRATIONS_DIR))

        conn = sqlite3.connect(temp_db)
        try:
            db._backfill_migration_ledger(conn, backend, migrations)
            after = {row[0] for row in conn.execute("SELECT migration_id FROM migration_ledger")}
        finally:
            conn.close()
            backend.connection.close()

        assert after == before, "既に全件記録済みのfresh DBではbackfillは何も変更しないはず"

    def test_backfills_ledger_deleted_rows(self, temp_db):
        """ledgerから行を削除した状態でbackfillすると、現存ファイルの内容ハッシュで復元される"""
        conn = sqlite3.connect(temp_db)
        try:
            conn.execute("DELETE FROM migration_ledger WHERE migration_id = '0001_initial_schema'")
            conn.commit()
        finally:
            conn.close()

        parsed = parse_uri(f"sqlite:///{temp_db}")
        backend = db._VecSQLiteBackend(parsed, default_migration_table)
        backend.init_database()
        migrations = read_migrations(str(db.MIGRATIONS_DIR))

        conn = sqlite3.connect(temp_db)
        try:
            db._backfill_migration_ledger(conn, backend, migrations)
            row = conn.execute(
                "SELECT content_sha256 FROM migration_ledger WHERE migration_id = '0001_initial_schema'"
            ).fetchone()
        finally:
            conn.close()
            backend.connection.close()

        assert row is not None
        expected_path = next(m.path for m in migrations if m.id == "0001_initial_schema")
        assert row[0] == db._content_sha256(expected_path)
