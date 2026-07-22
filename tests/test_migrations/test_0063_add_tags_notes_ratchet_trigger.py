"""migration 0063_add_tags_notes_ratchet_trigger のテスト

tag notes（tags.notes）はSessionStart系の遭遇時注入(collect_tag_notes_for_injection)で
全文表示されるため、1タグあたりのnotesが際限なく伸びるのを防ぐ必要がある。本migration
は4000字を超える「増加」INSERT/UPDATEのみを拒否するラチェットをDBトリガーで課す
（縮む更新は4000字超過中でも常に許可する）。
"""
import os
import sqlite3
import tempfile

import pytest
from yoyo import default_migration_table, read_migrations
from yoyo.connections import parse_uri
from yoyo.migrations import MigrationList

from src.db import MIGRATIONS_DIR, _VecSQLiteBackend, get_connection, init_database


@pytest.fixture
def migrated_db():
    """全migration（0063含む）を適用済みのテスト用DBを提供する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def db_before_0063():
    """0062までのmigrationを適用したDBを提供する。0063の挙動を分離検証するために使う。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path

        parsed = parse_uri(f"sqlite:///{db_path}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        backend.init_database()
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        pre_0063 = MigrationList([m for m in all_migs if m.id < "0063"])
        with backend.lock():
            backend.apply_migrations(pre_0063)

        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


def _apply_migration_0063(db_path: str) -> None:
    """db_pathに対してmigration 0063のみを適用する。"""
    parsed = parse_uri(f"sqlite:///{db_path}")
    backend = _VecSQLiteBackend(parsed, default_migration_table)
    all_migs = read_migrations(str(MIGRATIONS_DIR))
    only_0063 = MigrationList([m for m in all_migs if m.id.startswith("0063")])
    with backend.lock():
        backend.apply_migrations(only_0063)


class TestTriggerExistence:
    """トリガー2本の適用前後の有無の確認"""

    def test_triggers_absent_before_migration(self, db_before_0063):
        conn = get_connection()
        try:
            names = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                ).fetchall()
            }
            assert "trg_tags_notes_ratchet_ceiling_ins" not in names
            assert "trg_tags_notes_ratchet_ceiling_upd" not in names
        finally:
            conn.close()

    def test_triggers_present_after_migration(self, migrated_db):
        conn = get_connection()
        try:
            names = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                ).fetchall()
            }
            assert "trg_tags_notes_ratchet_ceiling_ins" in names
            assert "trg_tags_notes_ratchet_ceiling_upd" in names
        finally:
            conn.close()


class TestRatchetCeilingOnInsert:
    """INSERTに対するラチェット天井の確認"""

    def test_insert_exceeding_ceiling_is_rejected(self, db_before_0063):
        """notesが4000字を超えるINSERTは拒否される"""
        _apply_migration_0063(db_before_0063)

        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO tags (namespace, name, notes) VALUES (?, ?, ?)",
                    ("domain", "over-ceiling-insert", "x" * 4001),
                )
        finally:
            conn.rollback()
            conn.close()

    def test_insert_within_ceiling_is_accepted(self, db_before_0063):
        """notesがちょうど4000字のINSERTは許可される（境界値）"""
        _apply_migration_0063(db_before_0063)

        conn = get_connection()
        try:
            tag_id = conn.execute(
                "INSERT INTO tags (namespace, name, notes) VALUES (?, ?, ?)",
                ("domain", "within-ceiling-insert", "x" * 4000),
            ).lastrowid
            conn.commit()
            row = conn.execute(
                "SELECT LENGTH(notes) AS len FROM tags WHERE id = ?", (tag_id,)
            ).fetchone()
            assert row["len"] == 4000
        finally:
            conn.close()

    def test_insert_without_notes_ignores_ceiling(self, db_before_0063):
        """notesがNULLのINSERTは天井検査の対象外"""
        _apply_migration_0063(db_before_0063)

        conn = get_connection()
        try:
            tag_id = conn.execute(
                "INSERT INTO tags (namespace, name) VALUES (?, ?)",
                ("domain", "no-notes-insert"),
            ).lastrowid
            conn.commit()
            row = conn.execute(
                "SELECT notes FROM tags WHERE id = ?", (tag_id,)
            ).fetchone()
            assert row["notes"] is None
        finally:
            conn.close()


class TestRatchetCeilingOnUpdate:
    """UPDATEに対するラチェット天井（ラチェット則含む）の確認"""

    def test_short_notes_stretched_beyond_ceiling_is_rejected(self, db_before_0063):
        """4000字以下の既存notesを4000字超へ伸ばすUPDATEは拒否される"""
        conn = get_connection()
        try:
            tag_id = conn.execute(
                "INSERT INTO tags (namespace, name, notes) VALUES (?, ?, ?)",
                ("domain", "short-to-over", "short notes"),
            ).lastrowid
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0063(db_before_0063)

        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE tags SET notes = ? WHERE id = ?", ("x" * 4001, tag_id)
                )
        finally:
            conn.rollback()
            conn.close()

    def test_already_over_ceiling_notes_further_stretched_is_rejected(self, db_before_0063):
        """トリガー導入前から4000字超のnotesを、さらに伸ばすUPDATEは拒否される"""
        conn = get_connection()
        try:
            tag_id = conn.execute(
                "INSERT INTO tags (namespace, name, notes) VALUES (?, ?, ?)",
                ("domain", "over-further-stretch", "x" * 4500),
            ).lastrowid
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0063(db_before_0063)

        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE tags SET notes = ? WHERE id = ?", ("x" * 4600, tag_id)
                )
        finally:
            conn.rollback()
            conn.close()

    def test_shrink_while_over_ceiling_is_allowed(self, db_before_0063):
        """トリガー導入前から4000字超のnotesを、なお4000字超のまま縮める更新は許可される
        （ラチェットの核: 減少は常に許可、増加のみ拒否）"""
        conn = get_connection()
        try:
            tag_id = conn.execute(
                "INSERT INTO tags (namespace, name, notes) VALUES (?, ?, ?)",
                ("domain", "shrink-while-over", "x" * 4500),
            ).lastrowid
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0063(db_before_0063)

        conn = get_connection()
        try:
            # 4500 -> 4200: なお4000字超だが減少しているので許可される
            conn.execute(
                "UPDATE tags SET notes = ? WHERE id = ?", ("x" * 4200, tag_id)
            )
            conn.commit()
            row = conn.execute(
                "SELECT LENGTH(notes) AS len FROM tags WHERE id = ?", (tag_id,)
            ).fetchone()
            assert row["len"] == 4200
        finally:
            conn.close()

    def test_description_only_update_ignores_ceiling(self, db_before_0063):
        """descriptionのみの更新は、notesが4000字超過中でも成功する
        （UPDATE OF notesの対象外であることの確認）"""
        conn = get_connection()
        try:
            tag_id = conn.execute(
                "INSERT INTO tags (namespace, name, notes) VALUES (?, ?, ?)",
                ("domain", "description-only-update", "x" * 4500),
            ).lastrowid
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0063(db_before_0063)

        conn = get_connection()
        try:
            conn.execute(
                "UPDATE tags SET description = ? WHERE id = ?", ("短い要旨", tag_id)
            )
            conn.commit()
            row = conn.execute(
                "SELECT description FROM tags WHERE id = ?", (tag_id,)
            ).fetchone()
            assert row["description"] == "短い要旨"
        finally:
            conn.close()


class TestExistingDataUnaffectedByMigration:
    """migration適用自体が既存データを書き換えないことの確認"""

    def test_existing_tag_notes_unchanged_after_migration(self, db_before_0063):
        conn = get_connection()
        try:
            tag_id = conn.execute(
                "INSERT INTO tags (namespace, name, notes) VALUES (?, ?, ?)",
                ("domain", "unchanged-after-migration", "x" * 4500),
            ).lastrowid
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0063(db_before_0063)

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT LENGTH(notes) AS len FROM tags WHERE id = ?", (tag_id,)
            ).fetchone()
            assert row["len"] == 4500
        finally:
            conn.close()
