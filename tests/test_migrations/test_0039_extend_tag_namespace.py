"""migration 0039_extend_tag_namespace のテスト

tags テーブルの namespace CHECK 制約を完全削除し、任意の namespace 文字列を
受け付けるよう変更する migration。

検証項目:
- 0039適用後、任意の namespace ('ow' / 'outcome' / 'bogus' / 既存 'domain' 等)
  がそのまま INSERT 可能になる
- 既存タグデータ（id, namespace, name, notes, canonical_id）が完全保持される
- 既存 junction 行（activity_tags 等）が消えずに新 tags への参照が維持される
- 0038適用時点（CHECK 制約あり）では新規 namespace は拒否される（前提確認）
"""
import os
import sqlite3
import tempfile

import pytest
from yoyo import default_migration_table, read_migrations
from yoyo.connections import parse_uri
from yoyo.migrations import MigrationList

from src.db import MIGRATIONS_DIR, _VecSQLiteBackend, get_connection, init_database
from src.services.tag_service import _injected_tags


@pytest.fixture
def migrated_db():
    """全migration適用後（0039含む）"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def db_before_0039():
    """0038までのmigrationを適用したDB（0039の分離検証用）"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        parsed = parse_uri(f"sqlite:///{db_path}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        backend.init_database()
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        pre_0039 = MigrationList([m for m in all_migs if m.id < "0039"])
        with backend.lock():
            backend.apply_migrations(pre_0039)
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


def _apply_migration_0039(db_path: str) -> None:
    parsed = parse_uri(f"sqlite:///{db_path}")
    backend = _VecSQLiteBackend(parsed, default_migration_table)
    all_migs = read_migrations(str(MIGRATIONS_DIR))
    only_0039 = MigrationList(
        [m for m in all_migs if m.id.startswith("0039_extend_tag_namespace")]
    )
    with backend.lock():
        backend.apply_migrations(only_0039)


class TestNamespaceUnrestricted:
    def test_ow_namespace_accepted_after_0039(self, migrated_db):
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO tags (namespace, name) VALUES ('ow', 'managed')"
            )
            row = conn.execute(
                "SELECT namespace, name FROM tags WHERE namespace = 'ow'"
            ).fetchone()
            assert row["namespace"] == "ow"
            assert row["name"] == "managed"
        finally:
            conn.close()

    def test_outcome_namespace_accepted_after_0039(self, migrated_db):
        conn = get_connection()
        try:
            conn.executemany(
                "INSERT INTO tags (namespace, name) VALUES (?, ?)",
                [("outcome", "cancelled"), ("outcome", "failed")],
            )
            rows = conn.execute(
                "SELECT name FROM tags WHERE namespace = 'outcome' ORDER BY name"
            ).fetchall()
            assert [r["name"] for r in rows] == ["cancelled", "failed"]
        finally:
            conn.close()

    def test_legacy_namespaces_still_accepted(self, migrated_db):
        conn = get_connection()
        try:
            conn.executemany(
                "INSERT INTO tags (namespace, name) VALUES (?, ?)",
                [
                    ("", "plain-tag"),
                    ("domain", "cc-memory-ext"),
                    ("intent", "discuss-ext"),
                ],
            )
            for ns, name in [("", "plain-tag"), ("domain", "cc-memory-ext"),
                              ("intent", "discuss-ext")]:
                row = conn.execute(
                    "SELECT id FROM tags WHERE namespace = ? AND name = ?",
                    (ns, name),
                ).fetchone()
                assert row is not None
        finally:
            conn.close()

    def test_arbitrary_namespace_accepted(self, migrated_db):
        """任意の namespace 文字列が CHECK 違反なく INSERT できる。

        namespace の妥当性は Python 層でバリデーションするポリシーに移行したため、
        DB スキーマレベルでは any TEXT を受け付ける。
        """
        conn = get_connection()
        try:
            for ns in ["bogus", "future-ns", "x", "0", "a-b-c"]:
                conn.execute(
                    "INSERT INTO tags (namespace, name) VALUES (?, ?)",
                    (ns, "test-name"),
                )
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM tags WHERE name = 'test-name'"
            ).fetchone()["c"]
            assert count == 5
        finally:
            conn.close()

    def test_ow_namespace_rejected_before_0039(self, db_before_0039):
        """0038時点では 'ow' namespace は CHECK 違反で拒否される（前提確認）"""
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO tags (namespace, name) VALUES ('ow', 'managed')"
                )
        finally:
            conn.close()


class TestExistingDataPreserved:
    def test_pre_existing_tag_data_kept_intact(self, db_before_0039):
        """0038時点で投入した tag データが 0039 適用後も同じ id で残っている"""
        conn = get_connection()
        try:
            cur = conn.execute(
                "INSERT INTO tags (namespace, name, notes) VALUES (?, ?, ?)",
                ("domain", "test-domain", "メモ"),
            )
            tid = cur.lastrowid
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0039(db_before_0039)

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT id, namespace, name, notes FROM tags WHERE id = ?", (tid,)
            ).fetchone()
            assert row is not None
            assert row["namespace"] == "domain"
            assert row["name"] == "test-domain"
            assert row["notes"] == "メモ"
        finally:
            conn.close()

    def test_canonical_id_relation_preserved(self, db_before_0039):
        """canonical_id によるエイリアス関係が 0039 適用後も保持される"""
        conn = get_connection()
        try:
            cur = conn.execute(
                "INSERT INTO tags (namespace, name) VALUES (?, ?)",
                ("domain", "canonical-tag"),
            )
            canonical_id = cur.lastrowid
            cur = conn.execute(
                "INSERT INTO tags (namespace, name, canonical_id) VALUES (?, ?, ?)",
                ("domain", "alias-tag", canonical_id),
            )
            alias_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0039(db_before_0039)

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT canonical_id FROM tags WHERE id = ?", (alias_id,)
            ).fetchone()
            assert row["canonical_id"] == canonical_id
        finally:
            conn.close()

    def test_junction_tables_preserved_after_rebuild(self, db_before_0039):
        """既存 activity_tags 行が 0039 (tags 再構築) 後も維持される"""
        conn = get_connection()
        try:
            cur = conn.execute(
                "INSERT INTO tags (namespace, name) VALUES (?, ?)",
                ("domain", "preserved-junction-tag"),
            )
            tag_id = cur.lastrowid
            cur = conn.execute(
                "INSERT INTO activities (title, description, status) VALUES (?, ?, ?)",
                ("act", "", "pending"),
            )
            aid = cur.lastrowid
            conn.execute(
                "INSERT INTO activity_tags (activity_id, tag_id) VALUES (?, ?)",
                (aid, tag_id),
            )
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0039(db_before_0039)

        conn = get_connection()
        try:
            row = conn.execute(
                """
                SELECT a.id, t.name FROM activity_tags at
                JOIN activities a ON a.id = at.activity_id
                JOIN tags t ON t.id = at.tag_id
                WHERE a.id = ?
                """,
                (aid,),
            ).fetchone()
            assert row is not None, (
                "tags 再構築後に activity_tags 経由のJOINで行が見つからない"
                "（junction の REFERENCES が切れている可能性）"
            )
            assert row["name"] == "preserved-junction-tag"
        finally:
            conn.close()


class TestSchemaShapeRetained:
    def test_tags_columns_retained(self, migrated_db):
        conn = get_connection()
        try:
            rows = conn.execute("PRAGMA table_info(tags)").fetchall()
            cols = {r["name"] for r in rows}
            for col in ["id", "namespace", "name", "notes", "description",
                         "created_at", "canonical_id"]:
                assert col in cols, f"tags.{col} が 0039 適用後に欠落"
        finally:
            conn.close()

    def test_unique_namespace_name_constraint_retained(self, migrated_db):
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO tags (namespace, name) VALUES ('ow', 'dup-test')"
            )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO tags (namespace, name) VALUES ('ow', 'dup-test')"
                )
        finally:
            conn.close()
