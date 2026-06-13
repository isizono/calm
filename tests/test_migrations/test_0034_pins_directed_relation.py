"""migration 0034_pins_directed_relation のテスト

0034適用後のスキーマと、pinned=1のmaterialがsource='activity'のpinsレコードに
移行されることを確認する。
"""
import os
import sqlite3
import tempfile

import pytest
from yoyo import default_migration_table, read_migrations
from yoyo.connections import parse_uri
from yoyo.migrations import MigrationList

from src.db import MIGRATIONS_DIR, _VecSQLiteBackend, get_connection, init_database
from src.services.tag_service import _injected_tags, ensure_tag_ids


@pytest.fixture
def migrated_db():
    """全migration（0034含む）を適用済みのテスト用DBを提供する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def db_before_0034():
    """0033までのmigrationを適用したDBを提供する。0034の挙動を分離検証するために使う。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path

        parsed = parse_uri(f"sqlite:///{db_path}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        backend.init_database()
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        pre_0034 = MigrationList([m for m in all_migs if m.id < "0034"])
        with backend.lock():
            backend.apply_migrations(pre_0034)

        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


def _apply_migration_0034(db_path: str) -> None:
    """db_pathに対してmigration 0034のみを適用する。"""
    parsed = parse_uri(f"sqlite:///{db_path}")
    backend = _VecSQLiteBackend(parsed, default_migration_table)
    all_migs = read_migrations(str(MIGRATIONS_DIR))
    only_0034 = MigrationList([m for m in all_migs if m.id.startswith("0034")])
    with backend.lock():
        backend.apply_migrations(only_0034)


class TestPinsTableSchema:
    """pinsテーブルのスキーマ確認"""

    def test_pins_table_exists(self, migrated_db):
        """0034適用後にpinsテーブルが存在する"""
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='pins'"
            ).fetchone()
            assert row is not None, "pinsテーブルが存在しない"
        finally:
            conn.close()

    def test_pins_table_columns(self, migrated_db):
        """pinsテーブルが必要なカラムを持つ"""
        conn = get_connection()
        try:
            rows = conn.execute("PRAGMA table_info(pins)").fetchall()
            column_names = {row["name"] for row in rows}
            assert "source_type" in column_names
            assert "source_id" in column_names
            assert "target_type" in column_names
            assert "target_id" in column_names
            assert "created_at" in column_names
        finally:
            conn.close()

    def test_pins_table_primary_key(self, migrated_db):
        """pinsテーブルのPKが (source_type, source_id, target_type, target_id) である"""
        conn = get_connection()
        try:
            rows = conn.execute("PRAGMA table_info(pins)").fetchall()
            pk_columns = [row["name"] for row in rows if row["pk"] > 0]
            # PKの存在と4カラムであることを確認
            assert len(pk_columns) == 4
            assert set(pk_columns) == {"source_type", "source_id", "target_type", "target_id"}
        finally:
            conn.close()


class TestPinsCheckConstraints:
    """pinsテーブルのCHECK制約"""

    def test_source_type_check_constraint_rejects_invalid(self, migrated_db):
        """source_typeに 'invalid_type' を入れると IntegrityError でINSERTが拒否される"""
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO pins (source_type, source_id, target_type, target_id) VALUES (?, ?, ?, ?)",
                    ("invalid_type", 1, "material", 1),
                )
                conn.commit()
        finally:
            conn.rollback()
            conn.close()

    def test_target_type_check_constraint_rejects_invalid(self, migrated_db):
        """target_typeに 'invalid_type' を入れると IntegrityError でINSERTが拒否される"""
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO pins (source_type, source_id, target_type, target_id) VALUES (?, ?, ?, ?)",
                    ("activity", 1, "invalid_type", 1),
                )
                conn.commit()
        finally:
            conn.rollback()
            conn.close()

    def test_all_valid_source_types_accepted(self, migrated_db):
        """source_typeに6種（tag/activity/topic/decision/log/material）を入れると6行すべてが実際にpinsに挿入される"""
        valid_types = ["tag", "activity", "topic", "decision", "log", "material"]
        conn = get_connection()
        try:
            for i, entity_type in enumerate(valid_types, start=1):
                conn.execute(
                    "INSERT INTO pins (source_type, source_id, target_type, target_id) VALUES (?, ?, ?, ?)",
                    (entity_type, i, "material", i + 100),
                )
            conn.commit()
            inserted = conn.execute(
                "SELECT source_type FROM pins WHERE target_type='material' AND target_id BETWEEN 101 AND 106"
            ).fetchall()
            assert {row["source_type"] for row in inserted} == set(valid_types)
        finally:
            conn.close()

    def test_all_valid_target_types_accepted(self, migrated_db):
        """target_typeに6種（tag/activity/topic/decision/log/material）を入れると6行すべてが実際にpinsに挿入される"""
        valid_types = ["tag", "activity", "topic", "decision", "log", "material"]
        conn = get_connection()
        try:
            for i, entity_type in enumerate(valid_types, start=1):
                conn.execute(
                    "INSERT INTO pins (source_type, source_id, target_type, target_id) VALUES (?, ?, ?, ?)",
                    ("activity", i, entity_type, i + 200),
                )
            conn.commit()
            inserted = conn.execute(
                "SELECT target_type FROM pins WHERE source_type='activity' AND source_id BETWEEN 1 AND 6"
            ).fetchall()
            assert {row["target_type"] for row in inserted} == set(valid_types)
        finally:
            conn.close()


class TestPinsMigrationData:
    """既存pinned=1 materialの移行確認"""

    def _setup_pinned_material_with_activity_relation(self, conn, material_title="テスト資材"):
        """pinned=1のmaterialをactivityとのrelationつきで作成する。

        0034の移行クエリは relations テーブル経由で紐づきを引くため、
        activityとmaterialをrelationsに登録した上でmaterial.pinned=1にする。
        """
        # activityを作成
        tag_ids = ensure_tag_ids(conn, [("domain", "test")])
        cursor = conn.execute(
            "INSERT INTO activities (title, description, status) VALUES (?, ?, ?)",
            ("テストアクティビティ", "説明", "completed"),
        )
        activity_id = cursor.lastrowid
        for tag_id in tag_ids:
            conn.execute(
                "INSERT OR IGNORE INTO activity_tags (activity_id, tag_id) VALUES (?, ?)",
                (activity_id, tag_id),
            )

        # materialを作成（pinned=1）
        cursor = conn.execute(
            "INSERT INTO materials (title, content, pinned) VALUES (?, ?, 1)",
            (material_title, "テスト内容"),
        )
        material_id = cursor.lastrowid
        for tag_id in tag_ids:
            conn.execute(
                "INSERT OR IGNORE INTO material_tags (material_id, tag_id) VALUES (?, ?)",
                (material_id, tag_id),
            )

        # activity → material の relation を追加（0033以降の形式）
        # activity < material のアルファベット順制約（relations.CHECK）に注意:
        # 'activity' < 'material' は True なので source=activity, target=material で格納
        conn.execute(
            "INSERT OR IGNORE INTO relations (source_type, source_id, target_type, target_id, relation_type) VALUES (?, ?, ?, ?, ?)",
            ("activity", activity_id, "material", material_id, "related"),
        )

        conn.commit()
        return activity_id, material_id

    def test_pinned_column_still_exists_after_0034_before_0035(self, db_before_0034):
        """0033までのDBに0034を適用するとmaterialsテーブルにpinned列が存在する（0035適用前の状態確認）"""
        _apply_migration_0034(db_before_0034)

        conn = get_connection()
        try:
            rows = conn.execute("PRAGMA table_info(materials)").fetchall()
            column_names = {row["name"] for row in rows}
            assert "pinned" in column_names, "0034適用後（0035適用前）にpinned列がmaterialsから削除されている"
        finally:
            conn.close()

    def test_pinned_material_with_relation_migrated_to_pins_on_0034_apply(self, db_before_0034):
        """0033まで適用したDBにpinned=1 material + activity relationをseedし、0034を適用するとpinsに('activity', activity_id, 'material', material_id)レコードが1件作られる"""
        conn = get_connection()
        try:
            activity_id, material_id = self._setup_pinned_material_with_activity_relation(
                conn, material_title="移行対象資材"
            )
        finally:
            conn.close()

        _apply_migration_0034(db_before_0034)

        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT source_type, source_id, target_type, target_id FROM pins WHERE target_type='material' AND target_id=?",
                (material_id,),
            ).fetchall()
            assert len(rows) == 1, "移行後のpinsレコードは1件であるべき"
            row = rows[0]
            assert row["source_type"] == "activity"
            assert row["source_id"] == activity_id
            assert row["target_type"] == "material"
            assert row["target_id"] == material_id
        finally:
            conn.close()

    def test_pinned_material_without_relation_skipped_during_0034_migration(self, db_before_0034):
        """0033まで適用したDBにpinned=1 materialのみ（relations欠落）をseedし、0034を適用してもpinsに該当materialのレコードは作られない"""
        conn = get_connection()
        try:
            cursor = conn.execute(
                "INSERT INTO materials (title, content, pinned) VALUES (?, ?, 1)",
                ("孤立した資材", "content"),
            )
            material_id = cursor.lastrowid
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0034(db_before_0034)

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM pins WHERE target_type='material' AND target_id=?",
                (material_id,),
            ).fetchone()
            assert row is None, "activityとのrelationが無いpinned=1 materialは移行スコープ外"
        finally:
            conn.close()

    def test_unpinned_material_not_migrated_on_0034_apply(self, db_before_0034):
        """0033まで適用したDBにpinned=0 material + activity relationをseedし、0034を適用してもpinsには移行されない（移行条件はpinned=1のみ）"""
        conn = get_connection()
        try:
            tag_ids = ensure_tag_ids(conn, [("domain", "test")])
            cursor = conn.execute(
                "INSERT INTO activities (title, description, status) VALUES (?, ?, ?)",
                ("テストアクティビティ", "説明", "completed"),
            )
            activity_id = cursor.lastrowid
            for tag_id in tag_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO activity_tags (activity_id, tag_id) VALUES (?, ?)",
                    (activity_id, tag_id),
                )

            cursor = conn.execute(
                "INSERT INTO materials (title, content, pinned) VALUES (?, ?, 0)",
                ("ピンなし資材", "content"),
            )
            material_id = cursor.lastrowid
            for tag_id in tag_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO material_tags (material_id, tag_id) VALUES (?, ?)",
                    (material_id, tag_id),
                )

            conn.execute(
                "INSERT OR IGNORE INTO relations (source_type, source_id, target_type, target_id, relation_type) VALUES (?, ?, ?, ?, ?)",
                ("activity", activity_id, "material", material_id, "related"),
            )
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0034(db_before_0034)

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM pins WHERE target_type='material' AND target_id=?",
                (material_id,),
            ).fetchone()
            assert row is None, "pinned=0 materialは0034の移行対象外"
        finally:
            conn.close()


class TestPinsInsertOrIgnore:
    """pinsテーブルの重複挿入挙動"""

    def test_insert_or_ignore_duplicate(self, migrated_db):
        """同一の (source_type, source_id, target_type, target_id) を重複挿入してもエラーにならない"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO pins (source_type, source_id, target_type, target_id) VALUES (?, ?, ?, ?)",
                ("activity", 1, "material", 1),
            )
            conn.commit()

            # 同じキーを再度 INSERT OR IGNORE
            conn.execute(
                "INSERT OR IGNORE INTO pins (source_type, source_id, target_type, target_id) VALUES (?, ?, ?, ?)",
                ("activity", 1, "material", 1),
            )
            conn.commit()

            count = conn.execute(
                "SELECT COUNT(*) FROM pins WHERE source_type='activity' AND source_id=1 AND target_type='material' AND target_id=1"
            ).fetchone()[0]
            assert count == 1
        finally:
            conn.close()
