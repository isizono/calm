"""migration 0074_drop_relay_outbox のテスト

0074適用後にrelay_outboxテーブルが存在しないことを確認する。
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
    """全migration（0074含む）を適用済みのテスト用DBを提供する。"""
    yield temp_db


@pytest.fixture
def db_before_0074():
    """0073までのmigrationを適用したDBを提供する。0074の挙動を分離検証するために使う。"""
    with db_before_migration("0074") as db_path:
        _injected_tags.clear()
        yield db_path


def _apply_migration_0074(db_path: str) -> None:
    """db_pathに対してmigration 0074のみを適用する。"""
    parsed = parse_uri(f"sqlite:///{db_path}")
    backend = _VecSQLiteBackend(parsed, default_migration_table)
    all_migs = read_migrations(str(MIGRATIONS_DIR))
    only_0074 = MigrationList([m for m in all_migs if m.id.startswith("0074")])
    with backend.lock():
        backend.apply_migrations(only_0074)


class TestRelayOutboxTableDropped:
    """0074適用後にrelay_outboxテーブルが削除されていることの確認"""

    def test_relay_outbox_table_not_exists_after_0074(self, migrated_db):
        """migration 0074適用後、relay_outboxテーブルが存在しない"""
        conn = get_connection()
        try:
            assert not table_exists(conn, "relay_outbox"), (
                "relay_outbox テーブルが0074適用後も残っている"
            )
        finally:
            conn.close()

    def test_relay_outbox_table_present_before_0074(self, db_before_0074):
        """0073適用時点ではrelay_outboxテーブルが存在する（前提確認）"""
        conn = get_connection()
        try:
            assert table_exists(conn, "relay_outbox"), (
                "0074適用前のDBにrelay_outboxテーブルがない"
            )
        finally:
            conn.close()

    def test_relay_outbox_table_removed_after_applying_0074(self, db_before_0074):
        """0073までのDBに0074を適用すると、relay_outboxテーブルが削除される"""
        _apply_migration_0074(db_before_0074)

        conn = get_connection()
        try:
            assert not table_exists(conn, "relay_outbox"), (
                "0074適用後もrelay_outboxテーブルが残っている"
            )
        finally:
            conn.close()


class TestOtherTablesUnaffected:
    """0074でDROPされるべきでない近傍テーブルへの影響がないことの確認"""

    def test_asks_table_intact(self, migrated_db):
        """直前のmigration(0073)が触るasksテーブルが無傷であることの確認"""
        conn = get_connection()
        try:
            assert table_exists(conn, "asks"), "asks テーブルが0074適用後に消えている"
            conn.execute(
                "INSERT INTO asks (question, fingerprint) VALUES (?, ?)",
                ("テスト問い", "fp-test-0074"),
            )
            conn.commit()
            row = conn.execute(
                "SELECT question FROM asks WHERE fingerprint='fp-test-0074'"
            ).fetchone()
            assert row is not None
        finally:
            conn.close()
