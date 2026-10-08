"""migration 0092_feedback_output_timing のテスト

- 適用前: timing='output'はCHECKで拒否される
- 適用後: 'output'が許可され、既存のエントリ・ノートが内容と親子関係を保ったまま残る
- block⇔pre_tool対応のCHECKが再作成後も効く
- feedback_output_cursor / feedback_output_cooldowns が作られている
"""
import sqlite3

import pytest

from src.db import get_connection, init_database
from test_migrations.conftest import db_before_migration, table_exists

_INSERT = (
    "INSERT INTO feedback_entries (name, body, strength, timing, condition_json) "
    "VALUES (?, 'b', ?, ?, '{}')"
)


@pytest.fixture
def db_before_0092():
    with db_before_migration("0092") as db_path:
        yield db_path


def test_output_timing_rejected_before_migration(db_before_0092):
    conn = get_connection(load_vec=False)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(_INSERT, ("o", "notify", "output"))
    finally:
        conn.rollback()
        conn.close()


def test_rows_and_children_survive_and_output_is_accepted(db_before_0092):
    conn = get_connection(load_vec=False)
    try:
        conn.execute(_INSERT, ("keep", "notify", "utterance"))
        conn.execute(_INSERT, ("blk", "block", "pre_tool"))
        eid = conn.execute("SELECT id FROM feedback_entries WHERE name = 'keep'").fetchone()["id"]
        conn.execute("UPDATE feedback_entries SET delivered_count = 7 WHERE id = ?", (eid,))
        conn.execute("INSERT INTO feedback_notes (entry_id, kind, body) VALUES (?, 'note', 'n')", (eid,))
        conn.execute("INSERT INTO feedback_turn_marks (session_id, prompt_id, entry_id) VALUES ('s', 'p', ?)", (eid,))
        conn.commit()
    finally:
        conn.close()

    init_database()

    conn = get_connection(load_vec=False)
    try:
        row = conn.execute("SELECT id, delivered_count FROM feedback_entries WHERE name = 'keep'").fetchone()
        assert (row["id"], row["delivered_count"]) == (eid, 7)
        assert conn.execute("SELECT COUNT(*) AS c FROM feedback_notes WHERE entry_id = ?", (eid,)).fetchone()["c"] == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        # 子テーブルのFKが改名後のfeedback_entriesを指している（新規の子行が入る）
        conn.execute("INSERT INTO feedback_notes (entry_id, kind, body) VALUES (?, 'stumble', 'm')", (eid,))
        conn.execute(_INSERT, ("o", "notify", "output"))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(_INSERT, ("bad", "block", "output"))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(_INSERT, ("bad2", "notify", "pre_tool"))
        assert table_exists(conn, "feedback_output_cursor")
        assert table_exists(conn, "feedback_output_cooldowns")
    finally:
        conn.rollback()
        conn.close()
