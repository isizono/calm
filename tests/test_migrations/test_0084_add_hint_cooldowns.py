"""migration 0084_add_hint_cooldowns のテスト

0084適用後にhint_cooldownsが期待通り存在し、PRIMARY KEY制約・FK制約・
ON DELETE CASCADEが機能することを、hint_serviceを経由せず生SQLで検証する。
"""
import sqlite3

import pytest

from src.db import get_connection
from src.services.tag_service import _injected_tags
from test_migrations.conftest import db_before_migration, get_column_names, table_exists


@pytest.fixture
def migrated_db(temp_db):
    """全migration（0084含む）を適用済みのテスト用DBを提供する。"""
    yield temp_db


@pytest.fixture
def db_before_0084():
    """0083までのmigrationを適用したDBを提供する。0084の挙動を分離検証するために使う。"""
    with db_before_migration("0084") as db_path:
        _injected_tags.clear()
        yield db_path


def _insert_tag(conn: sqlite3.Connection, name: str) -> int:
    cur = conn.execute(
        "INSERT INTO tags (namespace, name) VALUES ('domain', ?)", (name,)
    )
    return cur.lastrowid


class TestTableCreated:
    def test_hint_cooldowns_does_not_exist_before_0084(self, db_before_0084):
        conn = get_connection()
        try:
            assert not table_exists(conn, "hint_cooldowns")
        finally:
            conn.close()

    def test_hint_cooldowns_exists_after_0084(self, migrated_db):
        conn = get_connection()
        try:
            assert table_exists(conn, "hint_cooldowns")
        finally:
            conn.close()

    def test_expected_columns(self, migrated_db):
        conn = get_connection()
        try:
            cols = get_column_names(conn, "hint_cooldowns")
        finally:
            conn.close()
        assert {"tag_id", "marker", "until_date", "updated_at"} <= cols


class TestConstraints:
    def test_primary_key_is_tag_id_and_marker(self, migrated_db):
        conn = get_connection()
        try:
            tag_id = _insert_tag(conn, "t1")
            conn.commit()
            conn.execute(
                "INSERT INTO hint_cooldowns (tag_id, marker, until_date) VALUES (?, ?, ?)",
                (tag_id, "#marker-a", "2026-01-01"),
            )
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO hint_cooldowns (tag_id, marker, until_date) VALUES (?, ?, ?)",
                    (tag_id, "#marker-a", "2026-02-02"),
                )
        finally:
            conn.rollback()
            conn.close()

    def test_different_marker_same_tag_is_allowed(self, migrated_db):
        conn = get_connection()
        try:
            tag_id = _insert_tag(conn, "t2")
            conn.commit()
            conn.execute(
                "INSERT INTO hint_cooldowns (tag_id, marker, until_date) VALUES (?, ?, ?)",
                (tag_id, "#marker-a", "2026-01-01"),
            )
            conn.execute(
                "INSERT INTO hint_cooldowns (tag_id, marker, until_date) VALUES (?, ?, ?)",
                (tag_id, "#marker-b", "2026-01-01"),
            )
            conn.commit()
            rows = conn.execute(
                "SELECT marker FROM hint_cooldowns WHERE tag_id = ? ORDER BY marker", (tag_id,)
            ).fetchall()
        finally:
            conn.close()
        assert [r["marker"] for r in rows] == ["#marker-a", "#marker-b"]

    def test_tag_id_must_reference_existing_tag(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO hint_cooldowns (tag_id, marker, until_date) VALUES (?, ?, ?)",
                    (999_999, "#marker-a", "2026-01-01"),
                )
        finally:
            conn.rollback()
            conn.close()

    def test_until_date_not_null(self, migrated_db):
        conn = get_connection()
        try:
            tag_id = _insert_tag(conn, "t3")
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO hint_cooldowns (tag_id, marker, until_date) VALUES (?, ?, ?)",
                    (tag_id, "#marker-a", None),
                )
        finally:
            conn.rollback()
            conn.close()

    def test_deleting_tag_cascades_to_hint_cooldowns(self, migrated_db):
        conn = get_connection()
        try:
            tag_id = _insert_tag(conn, "t4")
            conn.commit()
            conn.execute(
                "INSERT INTO hint_cooldowns (tag_id, marker, until_date) VALUES (?, ?, ?)",
                (tag_id, "#marker-a", "2026-01-01"),
            )
            conn.commit()
            conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
            conn.commit()
            remaining = conn.execute(
                "SELECT COUNT(*) AS n FROM hint_cooldowns WHERE tag_id = ?", (tag_id,)
            ).fetchone()["n"]
        finally:
            conn.close()
        assert remaining == 0

    def test_upsert_replaces_until_date(self, migrated_db):
        """hint_service._apply_cooldown_markerが使うON CONFLICT DO UPDATEの
        UPSERTが実際に成功し、until_dateが置き換わることを確認する"""
        conn = get_connection()
        try:
            tag_id = _insert_tag(conn, "t5")
            conn.commit()
            conn.execute(
                "INSERT INTO hint_cooldowns (tag_id, marker, until_date) VALUES (?, ?, ?)",
                (tag_id, "#marker-a", "2026-01-01"),
            )
            conn.commit()
            conn.execute(
                """
                INSERT INTO hint_cooldowns (tag_id, marker, until_date)
                VALUES (?, ?, ?)
                ON CONFLICT(tag_id, marker) DO UPDATE SET until_date = excluded.until_date
                """,
                (tag_id, "#marker-a", "2026-01-02"),
            )
            conn.commit()
            rows = conn.execute(
                "SELECT until_date FROM hint_cooldowns WHERE tag_id = ? AND marker = ?",
                (tag_id, "#marker-a"),
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        assert rows[0]["until_date"] == "2026-01-02"
