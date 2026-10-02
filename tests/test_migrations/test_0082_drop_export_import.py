"""migration 0082_drop_export_import のテスト

0082適用後にinstance_meta・import_provenanceテーブルが存在しないことを確認する。
"""

import pytest
from yoyo import default_migration_table, read_migrations
from yoyo.connections import parse_uri
from yoyo.migrations import MigrationList

from src.db import MIGRATIONS_DIR, _VecSQLiteBackend, get_connection
from src.services.tag_service import _injected_tags
from test_migrations.conftest import db_before_migration, table_exists


@pytest.fixture
def migrated_db(temp_db):
    """全migration（0082含む）を適用済みのテスト用DBを提供する。"""
    yield temp_db


@pytest.fixture
def db_before_0082():
    """0081までのmigrationを適用したDBを提供する。0082の挙動を分離検証するために使う。"""
    with db_before_migration("0082") as db_path:
        _injected_tags.clear()
        yield db_path


def _apply_migration_0082(db_path: str) -> None:
    """db_pathに対してmigration 0082のみを適用する。"""
    parsed = parse_uri(f"sqlite:///{db_path}")
    backend = _VecSQLiteBackend(parsed, default_migration_table)
    all_migs = read_migrations(str(MIGRATIONS_DIR))
    only_0082 = MigrationList([m for m in all_migs if m.id.startswith("0082")])
    with backend.lock():
        backend.apply_migrations(only_0082)


class TestInstanceMetaAndImportProvenanceTablesDropped:
    """0082適用後にinstance_meta・import_provenanceテーブルが削除されていることの確認"""

    @pytest.mark.parametrize("table", ["instance_meta", "import_provenance"])
    def test_table_not_exists_after_0082(self, migrated_db, table):
        """migration 0082適用後、テーブルが存在しない"""
        conn = get_connection()
        try:
            assert not table_exists(conn, table), f"{table} テーブルが0082適用後も残っている"
        finally:
            conn.close()

    @pytest.mark.parametrize("table", ["instance_meta", "import_provenance"])
    def test_table_present_before_0082(self, db_before_0082, table):
        """0081適用時点ではテーブルが存在する（前提確認）"""
        conn = get_connection()
        try:
            assert table_exists(conn, table), f"0082適用前のDBに{table}テーブルがない"
        finally:
            conn.close()

    @pytest.mark.parametrize("table", ["instance_meta", "import_provenance"])
    def test_table_removed_after_applying_0082(self, db_before_0082, table):
        """0081までのDBに0082を適用すると、テーブルが削除される"""
        _apply_migration_0082(db_before_0082)

        conn = get_connection()
        try:
            assert not table_exists(conn, table), f"0082適用後も{table}テーブルが残っている"
        finally:
            conn.close()


class TestOtherTablesUnaffected:
    """0082でDROPされるべきでない近傍テーブルへの影響がないことの確認"""

    def test_asks_table_intact(self, migrated_db):
        """直前の関連migration(0070/0071)と同系のasksテーブルが無傷であることの確認"""
        conn = get_connection()
        try:
            assert table_exists(conn, "asks"), "asks テーブルが0082適用後に消えている"
            conn.execute(
                "INSERT INTO asks (question, fingerprint) VALUES (?, ?)",
                ("テスト問い", "fp-test-0082"),
            )
            conn.commit()
            row = conn.execute(
                "SELECT question FROM asks WHERE fingerprint='fp-test-0082'"
            ).fetchone()
            assert row is not None
        finally:
            conn.close()
