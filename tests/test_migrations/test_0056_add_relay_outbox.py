"""migration 0056_add_relay_outbox のテスト

0056 適用後に relay_outbox テーブルと pending 部分インデックスが作成され、
その形状が vendored relay_sdk の DDL（単一の真実源）と一致することを確認する。
"""
import os
import sqlite3
import tempfile

import pytest

from src.db import get_connection, init_database
from src.relay_sdk.outbox import create_outbox_table, publish
from src.services.tag_service import _injected_tags
from test_migrations.conftest import index_names, table_exists


@pytest.fixture
def migrated_db():
    """全 migration（0056 含む）を適用済みのテスト用 DB を提供する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


def _column_shapes(conn, table: str) -> list[tuple]:
    """PRAGMA table_info の (name, type, notnull, dflt_value, pk) を返す。"""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [
        (row["name"], row["type"], row["notnull"], row["dflt_value"], row["pk"])
        for row in rows
    ]


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

    def test_schema_matches_sdk_ddl(self, migrated_db):
        """migration の形状が SDK の create_outbox_table と完全一致する。"""
        reference = sqlite3.connect(":memory:")
        reference.row_factory = sqlite3.Row
        create_outbox_table(reference)

        conn = get_connection()
        try:
            assert _column_shapes(conn, "relay_outbox") == _column_shapes(
                reference, "relay_outbox"
            )
        finally:
            conn.close()
            reference.close()

    def test_sdk_publish_inserts_into_migrated_table(self, migrated_db):
        """SDK の publish() が migration 済みテーブルへそのまま INSERT できる。"""
        conn = get_connection()
        try:
            outbox_id = publish(
                conn,
                ref_type="message",
                ref_id="hello",
                labels=["handle:session-abc"],
                title="test",
            )
            conn.commit()
            row = conn.execute(
                "SELECT ref_type, ref_id, idempotency_key, processed_at"
                " FROM relay_outbox WHERE id = ?",
                (outbox_id,),
            ).fetchone()
            assert row["ref_type"] == "message"
            assert row["ref_id"] == "hello"
            assert row["idempotency_key"] == str(outbox_id)
            assert row["processed_at"] is None
        finally:
            conn.close()
