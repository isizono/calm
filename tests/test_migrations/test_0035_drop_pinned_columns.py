"""migration 0035_drop_pinned_columns のテスト

0035適用後に discussion_logs / decisions / materials の pinned列が存在しないことを確認する。
SQLite 3.35+ で ALTER TABLE ... DROP COLUMN が使用可能なことを前提とする。
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
    """全migration（0035含む）を適用済みのテスト用DBを提供する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def db_before_0035():
    """0034までのmigrationを適用したDBを提供する。0035の挙動を分離検証するために使う。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path

        parsed = parse_uri(f"sqlite:///{db_path}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        backend.init_database()
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        pre_0035 = MigrationList([m for m in all_migs if m.id < "0035"])
        with backend.lock():
            backend.apply_migrations(pre_0035)

        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def migrated_db_up_to_0035():
    """0035までのmigrationを適用したDBを提供する。

    後続 migration（0046/0047 等）で decisions.topic_id / discussion_logs.topic_id が
    物理削除されるため、0035 直後の「topic_id NOT NULL FK が残っている」状態を
    検証するテストはここを使う。
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path

        parsed = parse_uri(f"sqlite:///{db_path}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        backend.init_database()
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        up_to_0035 = MigrationList([m for m in all_migs if m.id < "0036"])
        with backend.lock():
            backend.apply_migrations(up_to_0035)

        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


def _apply_migration_0035(db_path: str) -> None:
    """db_pathに対してmigration 0035のみを適用する。"""
    parsed = parse_uri(f"sqlite:///{db_path}")
    backend = _VecSQLiteBackend(parsed, default_migration_table)
    all_migs = read_migrations(str(MIGRATIONS_DIR))
    only_0035 = MigrationList([m for m in all_migs if m.id.startswith("0035")])
    with backend.lock():
        backend.apply_migrations(only_0035)


def _get_column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    """指定テーブルのカラム名セットを返す。"""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


class TestSQLiteVersion:
    """DROP COLUMN サポートの前提確認"""

    def test_sqlite_supports_drop_column(self, migrated_db):
        """実行環境のSQLiteが3.35以上でDROP COLUMNをサポートしている"""
        conn = get_connection()
        try:
            version_row = conn.execute("SELECT sqlite_version()").fetchone()
            version_str = version_row[0]
            major, minor, *_ = [int(x) for x in version_str.split(".")]
            assert (major, minor) >= (3, 35), (
                f"SQLite {version_str} は DROP COLUMN をサポートしていない（3.35+ が必要）"
            )
        finally:
            conn.close()


class TestPinnedColumnsDropped:
    """0035適用後にpinned列が削除されていることの確認"""

    def test_discussion_logs_has_no_pinned_column_after_0035(self, migrated_db):
        """migration 0035適用後、discussion_logsテーブルにpinned列が存在しない"""
        conn = get_connection()
        try:
            column_names = _get_column_names(conn, "discussion_logs")
            assert "pinned" not in column_names, (
                "discussion_logs.pinned が0035適用後も残っている"
            )
        finally:
            conn.close()

    def test_decisions_has_no_pinned_column_after_0035(self, migrated_db):
        """migration 0035適用後、decisionsテーブルにpinned列が存在しない"""
        conn = get_connection()
        try:
            column_names = _get_column_names(conn, "decisions")
            assert "pinned" not in column_names, (
                "decisions.pinned が0035適用後も残っている"
            )
        finally:
            conn.close()

    def test_materials_has_no_pinned_column_after_0035(self, migrated_db):
        """migration 0035適用後、materialsテーブルにpinned列が存在しない"""
        conn = get_connection()
        try:
            column_names = _get_column_names(conn, "materials")
            assert "pinned" not in column_names, (
                "materials.pinned が0035適用後も残っている"
            )
        finally:
            conn.close()

    def test_pinned_columns_present_before_0035(self, db_before_0035):
        """0034適用時点では discussion_logs / decisions / materials に pinned列が存在する（前提確認）"""
        conn = get_connection()
        try:
            assert "pinned" in _get_column_names(conn, "discussion_logs"), (
                "0035適用前のdiscussion_logsにpinned列がない"
            )
            assert "pinned" in _get_column_names(conn, "decisions"), (
                "0035適用前のdecisionsにpinned列がない"
            )
            assert "pinned" in _get_column_names(conn, "materials"), (
                "0035適用前のmaterialsにpinned列がない"
            )
        finally:
            conn.close()

    def test_pinned_columns_removed_after_applying_0035(self, db_before_0035):
        """0034までのDBに0035を適用すると、3テーブルのpinned列がすべて削除される"""
        _apply_migration_0035(db_before_0035)

        conn = get_connection()
        try:
            assert "pinned" not in _get_column_names(conn, "discussion_logs"), (
                "0035適用後もdiscussion_logs.pinned が残っている"
            )
            assert "pinned" not in _get_column_names(conn, "decisions"), (
                "0035適用後もdecisions.pinned が残っている"
            )
            assert "pinned" not in _get_column_names(conn, "materials"), (
                "0035適用後もmaterials.pinned が残っている"
            )
        finally:
            conn.close()


class TestOtherColumnsUnaffected:
    """0035でDROPされるべきでないカラムへの影響がないことの確認"""

    def test_discussion_logs_other_columns_intact(self, migrated_db_up_to_0035):
        """0035適用後、discussion_logsのid/title/content/topic_id/retracted_atカラムが保持される"""
        conn = get_connection()
        try:
            column_names = _get_column_names(conn, "discussion_logs")
            for col in ["id", "title", "content", "topic_id", "retracted_at"]:
                assert col in column_names, (
                    f"discussion_logs.{col} が0035適用後に消えている"
                )
        finally:
            conn.close()

    def test_decisions_other_columns_intact(self, migrated_db_up_to_0035):
        """0035適用後、decisionsのid/decision/reason/topic_id/retracted_atカラムが保持される"""
        conn = get_connection()
        try:
            column_names = _get_column_names(conn, "decisions")
            for col in ["id", "decision", "reason", "topic_id", "retracted_at"]:
                assert col in column_names, (
                    f"decisions.{col} が0035適用後に消えている"
                )
        finally:
            conn.close()

    def test_materials_other_columns_intact(self, migrated_db):
        """0035適用後、materialsのid/title/content/sourceカラムが保持される"""
        conn = get_connection()
        try:
            column_names = _get_column_names(conn, "materials")
            for col in ["id", "title", "content", "source"]:
                assert col in column_names, (
                    f"materials.{col} が0035適用後に消えている"
                )
        finally:
            conn.close()

    def test_pins_table_still_exists_after_0035(self, migrated_db):
        """0035適用後もpinsテーブルが存在する（0034で作成されたもの）"""
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='pins'"
            ).fetchone()
            assert row is not None, "pinsテーブルが0035適用後に消えている"
        finally:
            conn.close()


class TestDataIntegrity:
    """0035適用後のデータ操作確認"""

    def test_insert_discussion_log_without_pinned(self, migrated_db_up_to_0035):
        """0035適用後、discussion_logsにpinned列なしでINSERTできる"""
        conn = get_connection()
        try:
            # topicを先に作成
            conn.execute(
                "INSERT INTO discussion_topics (title, description) VALUES (?, ?)",
                ("テストトピック", "説明"),
            )
            topic_id = conn.execute(
                "SELECT id FROM discussion_topics WHERE title='テストトピック'"
            ).fetchone()["id"]

            conn.execute(
                "INSERT INTO discussion_logs (topic_id, title, content) VALUES (?, ?, ?)",
                (topic_id, "テストログ", "内容"),
            )
            conn.commit()

            row = conn.execute(
                "SELECT title FROM discussion_logs WHERE title='テストログ'"
            ).fetchone()
            assert row is not None
        finally:
            conn.close()

    def test_insert_decision_without_pinned(self, migrated_db_up_to_0035):
        """0035適用後、decisionsにpinned列なしでINSERTできる"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO discussion_topics (title, description) VALUES (?, ?)",
                ("テストトピック2", "説明"),
            )
            topic_id = conn.execute(
                "SELECT id FROM discussion_topics WHERE title='テストトピック2'"
            ).fetchone()["id"]

            conn.execute(
                "INSERT INTO decisions (topic_id, decision, reason) VALUES (?, ?, ?)",
                (topic_id, "テスト決定", "テスト根拠"),
            )
            conn.commit()

            row = conn.execute(
                "SELECT decision FROM decisions WHERE decision='テスト決定'"
            ).fetchone()
            assert row is not None
        finally:
            conn.close()

    def test_insert_material_without_pinned(self, migrated_db):
        """0035適用後、materialsにpinned列なしでINSERTできる"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO materials (title, content) VALUES (?, ?)",
                ("テスト資材", "内容"),
            )
            conn.commit()

            row = conn.execute(
                "SELECT title FROM materials WHERE title='テスト資材'"
            ).fetchone()
            assert row is not None
        finally:
            conn.close()
