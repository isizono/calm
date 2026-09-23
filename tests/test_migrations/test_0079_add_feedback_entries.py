"""migration 0079_add_feedback_entries のテスト

0079適用後にfeedback_entries / feedback_notes / feedback_holds / feedback_turn_marks /
feedback_bootstrap_seen / feedback_switchが期待通り存在し、CHECK制約・FK制約・
追記専用トリガー・feedback_switchの初期行が機能することを、feedback_serviceを
経由せず生SQLで検証する。
"""
import os
import sqlite3
import tempfile

import pytest

from src.db import get_connection, init_database
from src.services.tag_service import _injected_tags
from test_migrations.conftest import db_before_migration, get_column_names, index_names, table_exists

_TABLES = (
    "feedback_entries",
    "feedback_notes",
    "feedback_holds",
    "feedback_turn_marks",
    "feedback_bootstrap_seen",
    "feedback_switch",
)


@pytest.fixture
def migrated_db():
    """全migration（0079含む）を適用済みのテスト用DBを提供する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def db_before_0079():
    """0078までのmigrationを適用したDBを提供する。0079の挙動を分離検証するために使う。"""
    with db_before_migration("0079") as db_path:
        _injected_tags.clear()
        yield db_path


def _insert_entry(conn: sqlite3.Connection, **overrides) -> int:
    fields = {
        "name": "test-entry",
        "body": "テスト本文",
        "strength": "notify",
        "timing": "utterance",
        "condition_json": '{"tool": null, "all": []}',
    }
    fields.update(overrides)
    columns = ", ".join(fields.keys())
    placeholders = ", ".join("?" for _ in fields)
    cur = conn.execute(
        f"INSERT INTO feedback_entries ({columns}) VALUES ({placeholders})",
        tuple(fields.values()),
    )
    return cur.lastrowid


def _insert_note(conn: sqlite3.Connection, entry_id: int, **overrides) -> int:
    fields = {"entry_id": entry_id, "kind": "note", "body": "ノート本文"}
    fields.update(overrides)
    columns = ", ".join(fields.keys())
    placeholders = ", ".join("?" for _ in fields)
    cur = conn.execute(
        f"INSERT INTO feedback_notes ({columns}) VALUES ({placeholders})",
        tuple(fields.values()),
    )
    return cur.lastrowid


class TestTablesCreated:
    def test_feedback_tables_do_not_exist_before_0079(self, db_before_0079):
        conn = get_connection()
        try:
            for table in _TABLES:
                assert not table_exists(conn, table), table
        finally:
            conn.close()

    def test_feedback_tables_exist_after_0079(self, migrated_db):
        conn = get_connection()
        try:
            for table in _TABLES:
                assert table_exists(conn, table), table
        finally:
            conn.close()

    def test_expected_columns(self, migrated_db):
        conn = get_connection()
        try:
            entries_cols = get_column_names(conn, "feedback_entries")
            notes_cols = get_column_names(conn, "feedback_notes")
            holds_cols = get_column_names(conn, "feedback_holds")
            turn_marks_cols = get_column_names(conn, "feedback_turn_marks")
            bootstrap_cols = get_column_names(conn, "feedback_bootstrap_seen")
            meta_cols = get_column_names(conn, "feedback_switch")
        finally:
            conn.close()
        assert {
            "id", "name", "body", "ref", "strength", "timing", "condition_json",
            "delivered_count", "overridden_count", "deleted_at", "created_at", "updated_at",
        } <= entries_cols
        assert {"id", "entry_id", "kind", "body", "created_at"} <= notes_cols
        assert {"session_id", "entry_id", "fingerprint", "created_at"} <= holds_cols
        assert {"session_id", "prompt_id", "entry_id", "created_at"} <= turn_marks_cols
        assert {"session_id", "created_at"} <= bootstrap_cols
        assert {"id", "mode", "updated_at"} <= meta_cols

    def test_expected_indexes(self, migrated_db):
        conn = get_connection()
        try:
            names = index_names(conn, "idx_feedback%")
        finally:
            conn.close()
        assert "idx_feedback_notes_entry" in names

    def test_feedback_switch_initial_row(self, migrated_db):
        conn = get_connection()
        try:
            row = conn.execute("SELECT id, mode FROM feedback_switch").fetchall()
        finally:
            conn.close()
        assert len(row) == 1
        assert row[0]["id"] == 1
        assert row[0]["mode"] == "on"

    def test_feedback_entries_has_no_seed_rows(self, migrated_db):
        """本マイグレーションは対象エントリの初期データを含まない（マージ後に別途登録する運用）。"""
        conn = get_connection()
        try:
            count = conn.execute("SELECT COUNT(*) AS n FROM feedback_entries").fetchone()["n"]
        finally:
            conn.close()
        assert count == 0


class TestFeedbackEntriesCheckConstraints:
    def test_name_uppercase_rejected(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_entry(conn, name="Bad-Name")
        finally:
            conn.rollback()
            conn.close()

    def test_name_with_whitespace_rejected(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_entry(conn, name="bad name")
        finally:
            conn.rollback()
            conn.close()

    def test_name_empty_rejected(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_entry(conn, name="")
        finally:
            conn.rollback()
            conn.close()

    def test_name_duplicate_rejected(self, migrated_db):
        conn = get_connection()
        try:
            _insert_entry(conn, name="dup-entry")
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                _insert_entry(conn, name="dup-entry")
        finally:
            conn.rollback()
            conn.close()

    def test_body_whitespace_only_rejected(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_entry(conn, body="   ")
        finally:
            conn.rollback()
            conn.close()

    def test_body_over_100_chars_rejected(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_entry(conn, body="a" * 101)
        finally:
            conn.rollback()
            conn.close()

    def test_body_exactly_100_chars_accepted(self, migrated_db):
        conn = get_connection()
        try:
            entry_id = _insert_entry(conn, name="max-body", body="a" * 100)
            conn.commit()
            row = conn.execute(
                "SELECT LENGTH(body) AS n FROM feedback_entries WHERE id = ?", (entry_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row["n"] == 100

    def test_ref_over_500_chars_rejected(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_entry(conn, ref="a" * 501)
        finally:
            conn.rollback()
            conn.close()

    def test_ref_null_accepted(self, migrated_db):
        conn = get_connection()
        try:
            entry_id = _insert_entry(conn, name="no-ref")
            conn.commit()
            row = conn.execute(
                "SELECT ref FROM feedback_entries WHERE id = ?", (entry_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row["ref"] is None

    def test_strength_out_of_range_rejected(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_entry(conn, strength="warn", timing="utterance")
        finally:
            conn.rollback()
            conn.close()

    def test_timing_out_of_range_rejected(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_entry(conn, timing="post_tool")
        finally:
            conn.rollback()
            conn.close()

    def test_block_strength_with_utterance_timing_rejected(self, migrated_db):
        """strength='block'はtiming='pre_tool'以外を持てない（双方向対応のCHECK）。"""
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_entry(conn, strength="block", timing="utterance")
        finally:
            conn.rollback()
            conn.close()

    def test_pre_tool_timing_with_notify_strength_rejected(self, migrated_db):
        """timing='pre_tool'はstrength='block'以外を持てない（双方向対応のCHECK）。"""
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_entry(conn, strength="notify", timing="pre_tool")
        finally:
            conn.rollback()
            conn.close()

    def test_block_strength_with_pre_tool_timing_accepted(self, migrated_db):
        conn = get_connection()
        try:
            entry_id = _insert_entry(
                conn, name="block-entry", strength="block", timing="pre_tool"
            )
            conn.commit()
            row = conn.execute(
                "SELECT strength, timing FROM feedback_entries WHERE id = ?", (entry_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row["strength"] == "block"
        assert row["timing"] == "pre_tool"

    def test_defaults_on_insert(self, migrated_db):
        conn = get_connection()
        try:
            entry_id = _insert_entry(conn, name="defaults-entry")
            conn.commit()
            row = conn.execute(
                "SELECT delivered_count, overridden_count, deleted_at FROM feedback_entries WHERE id = ?",
                (entry_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row["delivered_count"] == 0
        assert row["overridden_count"] == 0
        assert row["deleted_at"] is None


class TestFeedbackNotesCheckConstraintsAndTrigger:
    def test_kind_out_of_range_rejected(self, migrated_db):
        conn = get_connection()
        try:
            entry_id = _insert_entry(conn)
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                _insert_note(conn, entry_id, kind="warning")
        finally:
            conn.rollback()
            conn.close()

    def test_body_whitespace_only_rejected(self, migrated_db):
        conn = get_connection()
        try:
            entry_id = _insert_entry(conn)
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                _insert_note(conn, entry_id, body="   ")
        finally:
            conn.rollback()
            conn.close()

    def test_body_over_500_chars_rejected(self, migrated_db):
        conn = get_connection()
        try:
            entry_id = _insert_entry(conn)
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                _insert_note(conn, entry_id, body="a" * 501)
        finally:
            conn.rollback()
            conn.close()

    def test_entry_id_must_reference_existing_entry(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_note(conn, entry_id=999999)
        finally:
            conn.rollback()
            conn.close()

    def test_update_rejected_by_trigger(self, migrated_db):
        conn = get_connection()
        try:
            entry_id = _insert_entry(conn)
            note_id = _insert_note(conn, entry_id)
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE feedback_notes SET body = ? WHERE id = ?", ("changed", note_id)
                )
        finally:
            conn.rollback()
            conn.close()

    def test_delete_rejected_by_trigger(self, migrated_db):
        conn = get_connection()
        try:
            entry_id = _insert_entry(conn)
            note_id = _insert_note(conn, entry_id)
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM feedback_notes WHERE id = ?", (note_id,))
        finally:
            conn.rollback()
            conn.close()


class TestFeedbackHoldsAndTurnMarksAndBootstrapSeen:
    def test_holds_primary_key_is_session_and_entry(self, migrated_db):
        conn = get_connection()
        try:
            entry_id = _insert_entry(conn)
            conn.commit()
            conn.execute(
                "INSERT INTO feedback_holds (session_id, entry_id, fingerprint) VALUES (?, ?, ?)",
                ("sess-1", entry_id, "fp-a"),
            )
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO feedback_holds (session_id, entry_id, fingerprint) VALUES (?, ?, ?)",
                    ("sess-1", entry_id, "fp-b"),
                )
        finally:
            conn.rollback()
            conn.close()

    def test_holds_entry_id_must_reference_existing_entry(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO feedback_holds (session_id, entry_id, fingerprint) VALUES (?, ?, ?)",
                    ("sess-1", 999999, "fp-a"),
                )
        finally:
            conn.rollback()
            conn.close()

    def test_turn_marks_primary_key_is_session_prompt_and_entry(self, migrated_db):
        conn = get_connection()
        try:
            entry_id = _insert_entry(conn)
            conn.commit()
            conn.execute(
                "INSERT INTO feedback_turn_marks (session_id, prompt_id, entry_id) VALUES (?, ?, ?)",
                ("sess-1", "prompt-1", entry_id),
            )
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO feedback_turn_marks (session_id, prompt_id, entry_id) VALUES (?, ?, ?)",
                    ("sess-1", "prompt-1", entry_id),
                )
        finally:
            conn.rollback()
            conn.close()

    def test_turn_marks_prompt_id_defaults_to_empty_string(self, migrated_db):
        conn = get_connection()
        try:
            entry_id = _insert_entry(conn)
            conn.commit()
            conn.execute(
                "INSERT INTO feedback_turn_marks (session_id, entry_id) VALUES (?, ?)",
                ("sess-1", entry_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT prompt_id FROM feedback_turn_marks WHERE session_id = ? AND entry_id = ?",
                ("sess-1", entry_id),
            ).fetchone()
        finally:
            conn.close()
        assert row["prompt_id"] == ""

    def test_bootstrap_seen_primary_key_is_session_id(self, migrated_db):
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO feedback_bootstrap_seen (session_id) VALUES (?)", ("sess-1",)
            )
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO feedback_bootstrap_seen (session_id) VALUES (?)", ("sess-1",)
                )
        finally:
            conn.rollback()
            conn.close()


class TestFeedbackSwitchCheckConstraints:
    def test_id_other_than_1_rejected(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO feedback_switch (id, mode) VALUES (2, 'on')")
        finally:
            conn.rollback()
            conn.close()

    def test_mode_out_of_range_rejected(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE feedback_switch SET mode = 'observe' WHERE id = 1"
                )
        finally:
            conn.rollback()
            conn.close()

    def test_mode_can_be_toggled_off(self, migrated_db):
        conn = get_connection()
        try:
            conn.execute("UPDATE feedback_switch SET mode = 'off' WHERE id = 1")
            conn.commit()
            row = conn.execute("SELECT mode FROM feedback_switch WHERE id = 1").fetchone()
        finally:
            conn.close()
        assert row["mode"] == "off"
