"""migration 0056_add_relay_outbox のテスト

0056 適用後に relay_outbox テーブルと pending 部分インデックスが作成されることを
確認する。

relay_outbox は後続の migration（relay統合機能の撤去に伴う 0074）で削除される
ため、本テストは「0056 まで適用した時点」の DB で検証する（最新までの全
migration を適用した DB では 0074 によりこのテーブルは既に存在しない）。
"""
import pytest

from src.db import get_connection
from src.services.tag_service import _injected_tags
from test_migrations.conftest import db_before_migration, index_names, table_exists


@pytest.fixture
def migrated_db():
    """0056 まで（0056 含む）を適用したテスト用 DB を提供する。

    relay_outbox は 0074 で削除されるため、最新までの全 migration を
    適用した DB では検証できない。
    """
    with db_before_migration("0057") as db_path:
        _injected_tags.clear()
        yield db_path


class TestRelayOutboxMigration:
    def test_relay_outbox_table_exists(self, migrated_db):
        conn = get_connection()
        try:
            assert table_exists(conn, "relay_outbox")
        finally:
            conn.close()

    def test_pending_partial_index_exists(self, migrated_db):
        conn = get_connection()
        try:
            assert "idx_relay_outbox_pending" in index_names(
                conn, "idx_relay_outbox_%"
            )
        finally:
            conn.close()
