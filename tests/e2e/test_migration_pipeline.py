"""_apply_migrations() 安全化パイプライン全体のE2Eテスト

fresh DBスキップ・既存DB+pendingでのpremigrationスナップショット連動・
dry-run失敗時の実DB無傷・ledger内容ハッシュ検証・CCM_MIGRATION_*トグルを検証する。

実migrations/ディレクトリ自体は変更・改変しない（並行実行中の他テストへの影響を避ける
ため、追加・改変を試す migration ファイルは常に tmp_path 配下のコピーディレクトリに
書き、`db.MIGRATIONS_DIR` を monkeypatch して差し替える）。
"""
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from yoyo import read_migrations

import src.db as db
from src import config


@pytest.fixture
def extended_migrations_dir(tmp_path):
    """実migrations/の全ファイルをコピーしたディレクトリ。追加・改変して使う。"""
    mig_dir = tmp_path / "migrations_extended"
    shutil.copytree(db.MIGRATIONS_DIR, mig_dir)
    return mig_dir


def _write_new_migration(mig_dir: Path, filename: str, sql: str) -> Path:
    path = mig_dir / filename
    path.write_text(f"-- depends: 0049_add_migration_ledger\n\n{sql}\n", encoding="utf-8")
    return path


class TestFreshDatabaseSkipsProtections:
    def test_fresh_database_skips_snapshot_and_dry_run(self, tmp_path, monkeypatch):
        """新規DB（適用済みmigrationゼロ）では防護をスキップして素通しする"""
        db_path = str(tmp_path / "fresh.db")
        monkeypatch.setenv("DISCUSSION_DB_PATH", db_path)

        snapshot_calls = []
        dry_run_calls = []
        monkeypatch.setattr(db, "_take_premigration_snapshot", lambda *a, **k: snapshot_calls.append(True))
        monkeypatch.setattr(
            db, "dry_run_migrations", lambda *a, **k: dry_run_calls.append(True) or db.DryRunResult(ok=True)
        )

        from src.services.tag_service import _injected_tags

        db.init_database()
        _injected_tags.clear()

        assert snapshot_calls == [], "fresh DBではpremigrationスナップショットを取得しないはず"
        assert dry_run_calls == [], "fresh DBではdry-runゲートを通さないはず"

        conn = sqlite3.connect(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM migration_ledger").fetchone()[0]
        finally:
            conn.close()
        assert count > 0, "fresh DBでも本適用後はledgerに全migrationが記録されるはず"


class TestExistingDatabaseWithPending:
    def test_snapshot_taken_and_migration_applied(self, temp_db, extended_migrations_dir, monkeypatch):
        _write_new_migration(
            extended_migrations_dir,
            "9101_probe_new.sql",
            "CREATE TABLE pipeline_probe_new (id INTEGER PRIMARY KEY);",
        )
        monkeypatch.setattr(db, "MIGRATIONS_DIR", extended_migrations_dir)

        db._apply_migrations()

        conn = sqlite3.connect(temp_db)
        try:
            table_row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='pipeline_probe_new'"
            ).fetchone()
            assert table_row is not None, "dry-run成功後は本適用され、新テーブルが実DBに存在するはず"

            ledger_row = conn.execute(
                "SELECT content_sha256 FROM migration_ledger WHERE migration_id = ?",
                ("9101_probe_new",),
            ).fetchone()
        finally:
            conn.close()

        assert ledger_row is not None, "本適用したmigrationの内容ハッシュがledgerに記録されるはず"
        expected_hash = db._content_sha256(str(extended_migrations_dir / "9101_probe_new.sql"))
        assert ledger_row[0] == expected_hash

        premigration_dir = Path(temp_db).parent / "snapshots" / "premigration"
        json_files = list(premigration_dir.glob("*.json"))
        assert len(json_files) == 1, "既存DB+pendingではpremigrationスナップショットが1件取得されるはず"
        meta = json.loads(json_files[0].read_text(encoding="utf-8"))
        assert meta["pending_migrations"] == ["9101_probe_new"]

    def test_dry_run_failure_blocks_real_apply(self, temp_db, extended_migrations_dir, monkeypatch):
        _write_new_migration(
            extended_migrations_dir,
            "9102_probe_broken.sql",
            "CREATE TABLE (,,, THIS IS NOT VALID SQL;",
        )
        monkeypatch.setattr(db, "MIGRATIONS_DIR", extended_migrations_dir)

        with pytest.raises(SystemExit):
            db._apply_migrations()

        conn = sqlite3.connect(temp_db)
        try:
            applied = conn.execute(
                "SELECT COUNT(*) FROM _yoyo_migration WHERE migration_id = '9102_probe_broken'"
            ).fetchone()[0]
            ledger = conn.execute(
                "SELECT COUNT(*) FROM migration_ledger WHERE migration_id = '9102_probe_broken'"
            ).fetchone()[0]
        finally:
            conn.close()

        assert applied == 0, "dry-run失敗時は実DBへ本適用されないはず"
        assert ledger == 0, "dry-run失敗時はledgerにも記録されないはず"

        # premigrationスナップショット自体はdry-runより前に取得済みで、復旧の足がかりとして残る
        premigration_dir = Path(temp_db).parent / "snapshots" / "premigration"
        assert len(list(premigration_dir.glob("*.json"))) == 1

    def test_ccm_migration_snapshot_disabled_skips_snapshot(self, temp_db, extended_migrations_dir, monkeypatch):
        _write_new_migration(
            extended_migrations_dir,
            "9103_probe_toggle_snapshot.sql",
            "CREATE TABLE pipeline_probe_toggle_snapshot (id INTEGER PRIMARY KEY);",
        )
        monkeypatch.setattr(db, "MIGRATIONS_DIR", extended_migrations_dir)
        monkeypatch.setattr(config, "CCM_MIGRATION_SNAPSHOT", False)

        db._apply_migrations()

        premigration_dir = Path(temp_db).parent / "snapshots" / "premigration"
        json_files = list(premigration_dir.glob("*.json")) if premigration_dir.exists() else []
        assert json_files == [], "CCM_MIGRATION_SNAPSHOT=0ではpremigrationスナップショットを取得しないはず"

        conn = sqlite3.connect(temp_db)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='pipeline_probe_toggle_snapshot'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None, "スナップショット無効化時もmigration自体は適用されるはず"

    def test_ccm_migration_dryrun_disabled_skips_gate(self, temp_db, extended_migrations_dir, monkeypatch):
        _write_new_migration(
            extended_migrations_dir,
            "9104_probe_toggle_dryrun.sql",
            "CREATE TABLE pipeline_probe_toggle_dryrun (id INTEGER PRIMARY KEY);",
        )
        monkeypatch.setattr(db, "MIGRATIONS_DIR", extended_migrations_dir)
        monkeypatch.setattr(config, "CCM_MIGRATION_DRYRUN", False)

        dry_run_calls = []
        original_dry_run = db.dry_run_migrations

        def _spy(*args, **kwargs):
            dry_run_calls.append(args)
            return original_dry_run(*args, **kwargs)

        monkeypatch.setattr(db, "dry_run_migrations", _spy)

        db._apply_migrations()

        assert dry_run_calls == [], "CCM_MIGRATION_DRYRUN=0ではdry-runゲートを呼ばないはず"

        conn = sqlite3.connect(temp_db)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='pipeline_probe_toggle_dryrun'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None


class TestHashMismatchOnStartup:
    def test_default_blocks_startup(self, temp_db, extended_migrations_dir, monkeypatch):
        monkeypatch.setattr(db, "MIGRATIONS_DIR", extended_migrations_dir)

        target_path = extended_migrations_dir / "0001_initial_schema.sql"
        target_path.write_text(target_path.read_text(encoding="utf-8") + "\n-- tampered (test)\n", encoding="utf-8")

        with pytest.raises(SystemExit):
            db._apply_migrations()

    def test_warn_mode_continues_without_raising(self, temp_db, extended_migrations_dir, monkeypatch):
        monkeypatch.setattr(db, "MIGRATIONS_DIR", extended_migrations_dir)
        monkeypatch.setattr(config, "CCM_MIGRATION_HASH_ENFORCE", "warn")

        target_path = extended_migrations_dir / "0001_initial_schema.sql"
        target_path.write_text(target_path.read_text(encoding="utf-8") + "\n-- tampered (test)\n", encoding="utf-8")

        db._apply_migrations()  # 例外を投げずに完走するはず

    def test_re_mark_style_update_clears_mismatch(self, temp_db, extended_migrations_dir, monkeypatch):
        """migrate.py re-mark相当の操作（ledgerを現ファイルで更新）後は不一致が解消する"""
        monkeypatch.setattr(db, "MIGRATIONS_DIR", extended_migrations_dir)

        target_path = extended_migrations_dir / "0001_initial_schema.sql"
        target_path.write_text(target_path.read_text(encoding="utf-8") + "\n-- tampered (test)\n", encoding="utf-8")

        new_hash = db._content_sha256(str(target_path))
        conn = sqlite3.connect(temp_db)
        try:
            conn.execute(
                "UPDATE migration_ledger SET content_sha256 = ? WHERE migration_id = ?",
                (new_hash, "0001_initial_schema"),
            )
            conn.commit()
        finally:
            conn.close()

        db._apply_migrations()  # 例外を投げずに完走するはず


class TestVerifyMigrationLedgerIntegration:
    def test_no_mismatch_on_unmodified_existing_db(self, temp_db, extended_migrations_dir, monkeypatch):
        """既存DBにpendingが無く、ファイルも無改変なら何も起きずに完走する"""
        monkeypatch.setattr(db, "MIGRATIONS_DIR", extended_migrations_dir)
        db._apply_migrations()  # 例外なし

        migrations = read_migrations(str(extended_migrations_dir))
        conn = sqlite3.connect(temp_db)
        try:
            mismatches = db.verify_migration_ledger(conn, migrations)
        finally:
            conn.close()
        assert mismatches == []
