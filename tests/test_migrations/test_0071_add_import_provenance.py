"""migration 0071_add_import_provenance のテスト

0071適用後にimport_provenanceテーブルが期待通り存在し、PRIMARY KEY
(entity_type, entity_id)・UNIQUE (origin_instance, entity_type, origin_id)・
entity_type CHECK制約が機能することを、サービス層を経由せず生SQLで検証する。
"""
import os
import sqlite3
import tempfile

import pytest

from src.db import get_connection, init_database
from src.services.tag_service import _injected_tags
from test_migrations.conftest import db_before_migration, get_column_names, table_exists


@pytest.fixture
def migrated_db():
    """全migration(0071含む)を適用済みのテスト用DBを提供する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def db_before_0071():
    """0070までのmigrationを適用したDBを提供する。0071の挙動を分離検証するために使う。"""
    with db_before_migration("0071") as db_path:
        _injected_tags.clear()
        yield db_path


_BASE_ROW = {
    "entity_type": "material",
    "entity_id": 1,
    "origin_instance": "team-a",
    "origin_id": 12,
    "content_hash": "deadbeef",
    "bundle_id": "team-a-20260101000000",
}


def _insert_base_row(conn: sqlite3.Connection, **overrides) -> None:
    row = {**_BASE_ROW, **overrides}
    conn.execute(
        "INSERT INTO import_provenance "
        "(entity_type, entity_id, origin_instance, origin_id, content_hash, bundle_id) "
        "VALUES (:entity_type, :entity_id, :origin_instance, :origin_id, :content_hash, :bundle_id)",
        row,
    )


class TestImportProvenanceTable:
    def test_table_does_not_exist_before_0071(self, db_before_0071):
        conn = get_connection()
        try:
            exists = table_exists(conn, "import_provenance")
        finally:
            conn.close()
        assert exists is False

    def test_table_exists_after_0071(self, migrated_db):
        conn = get_connection()
        try:
            exists = table_exists(conn, "import_provenance")
            columns = get_column_names(conn, "import_provenance")
        finally:
            conn.close()
        assert exists is True
        assert columns == {
            "entity_type",
            "entity_id",
            "origin_instance",
            "origin_id",
            "content_hash",
            "origin_created_at",
            "bundle_id",
            "imported_at",
        }

    def test_insert_succeeds_and_imported_at_defaults(self, migrated_db):
        conn = get_connection()
        try:
            _insert_base_row(conn)
            conn.commit()
            row = conn.execute(
                "SELECT entity_type, entity_id, origin_instance, origin_id, content_hash, "
                "bundle_id, imported_at FROM import_provenance WHERE entity_type = 'material' AND entity_id = 1"
            ).fetchone()
        finally:
            conn.close()
        assert row["origin_instance"] == "team-a"
        assert row["origin_id"] == 12
        assert row["content_hash"] == "deadbeef"
        assert row["bundle_id"] == "team-a-20260101000000"
        assert row["imported_at"] is not None

    def test_primary_key_rejects_duplicate_entity(self, migrated_db):
        """同一(entity_type, entity_id)への2回目のINSERTはPRIMARY KEY違反で拒否される。"""
        conn = get_connection()
        try:
            _insert_base_row(conn)
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                _insert_base_row(conn, origin_id=99)
        finally:
            conn.rollback()
            conn.close()

    def test_unique_constraint_rejects_duplicate_origin(self, migrated_db):
        """同一(origin_instance, entity_type, origin_id)への2回目のINSERTはUNIQUE違反で拒否される
        (再importの冪等性判定に使う制約)。"""
        conn = get_connection()
        try:
            _insert_base_row(conn)
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                _insert_base_row(conn, entity_id=2)
        finally:
            conn.rollback()
            conn.close()

    def test_entity_type_check_constraint_rejects_invalid_type(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_base_row(conn, entity_type="bogus")
        finally:
            conn.rollback()
            conn.close()

    def test_required_columns_not_null_enforced(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO import_provenance "
                    "(entity_type, entity_id, origin_instance, origin_id, bundle_id) "
                    "VALUES ('material', 1, 'team-a', 12, 'bundle-1')"
                )
        finally:
            conn.rollback()
            conn.close()
