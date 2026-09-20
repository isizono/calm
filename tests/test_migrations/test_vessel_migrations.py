"""migration 0075_add_vessel_tables のテスト

観測台帳（obs_events・lessons・lesson_entries）と3つの参照表、追記専用の
強制・種類と条件の組み合わせの検査・常時配達の禁止・lessons_fts連動を検証する。
"""
import os
import sqlite3
import tempfile

import pytest
from yoyo import default_migration_table, read_migrations
from yoyo.connections import parse_uri
from yoyo.migrations import MigrationList

from src.db import MIGRATIONS_DIR, _VecSQLiteBackend, get_connection, init_database
from test_migrations.conftest import db_before_migration, table_exists


@pytest.fixture
def migrated_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def db_before_0075():
    with db_before_migration("0075") as db_path:
        yield db_path


def _apply_migration_0075(db_path: str) -> None:
    parsed = parse_uri(f"sqlite:///{db_path}")
    backend = _VecSQLiteBackend(parsed, default_migration_table)
    all_migs = read_migrations(str(MIGRATIONS_DIR))
    only_0075 = MigrationList([m for m in all_migs if m.id.startswith("0075")])
    with backend.lock():
        backend.apply_migrations(only_0075)


def _insert_prevent_lesson(conn: sqlite3.Connection, handle: str = "test-prevent-lesson") -> int:
    cur = conn.execute(
        "INSERT INTO lessons (kind, handle, body, deliver_event, deliver_spec, step_event, step_spec) "
        "VALUES ('prevent', ?, 'body text', 'tool_call', '{}', 'tool_call', '{}')",
        (handle,),
    )
    return cur.lastrowid


def _insert_tally_lesson(conn: sqlite3.Connection, handle: str = "test-tally-lesson") -> int:
    cur = conn.execute(
        "INSERT INTO lessons (kind, handle, body) VALUES ('tally', ?, 'tally body')",
        (handle,),
    )
    return cur.lastrowid


def _insert_guide_lesson(conn: sqlite3.Connection, handle: str = "test-guide-lesson") -> int:
    cur = conn.execute(
        "INSERT INTO lessons (kind, handle, body, deliver_event, deliver_spec) "
        "VALUES ('guide', ?, 'guide body', 'prompt', '{}')",
        (handle,),
    )
    return cur.lastrowid


class TestTableExistence:
    def test_tables_absent_before_migration(self, db_before_0075):
        conn = get_connection()
        try:
            for table in ("obs_events", "lessons", "lesson_entries", "obs_kinds",
                          "lesson_kinds", "delivery_channels", "vessel_cursor", "vessel_meta"):
                assert not table_exists(conn, table), table
        finally:
            conn.close()

    def test_tables_present_after_migration(self, migrated_db):
        conn = get_connection()
        try:
            for table in ("obs_events", "lessons", "lesson_entries", "obs_kinds",
                          "lesson_kinds", "delivery_channels", "vessel_cursor", "vessel_meta",
                          "lessons_fts"):
                assert table_exists(conn, table), table
        finally:
            conn.close()


class TestReferenceTables:
    def test_obs_kinds_seed_rows(self, migrated_db):
        conn = get_connection()
        try:
            kinds = {row["kind"] for row in conn.execute("SELECT kind FROM obs_kinds").fetchall()}
        finally:
            conn.close()
        assert kinds == {
            "utterance", "speaker", "reply", "tool", "tool_overflow", "tool_fail",
            "delivered", "suppressed", "stepped", "bind", "boundary", "human_withdraw",
        }

    def test_lesson_kinds_seed_rows(self, migrated_db):
        conn = get_connection()
        try:
            rows = {
                row["kind"]: (row["delivers"], row["steps"])
                for row in conn.execute("SELECT kind, delivers, steps FROM lesson_kinds").fetchall()
            }
        finally:
            conn.close()
        assert rows == {"prevent": (1, 1), "tally": (0, 0), "guide": (1, 0)}

    def test_delivery_channels_seed_rows(self, migrated_db):
        conn = get_connection()
        try:
            channels = {row["channel"] for row in conn.execute("SELECT channel FROM delivery_channels").fetchall()}
        finally:
            conn.close()
        assert channels == {"session", "prompt", "post_tool", "tool_fail", "pull"}

    def test_vessel_meta_default_mode_is_observe(self, migrated_db):
        """停止スイッチの既定値が観測だけの状態であることを必ず守る。"""
        conn = get_connection()
        try:
            row = conn.execute("SELECT mode FROM vessel_meta WHERE id = 1").fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row["mode"] == "observe"


class TestAppendOnlyEnforcement:
    """obs_events・lessons・lesson_entries・lesson_kinds へのUPDATE/DELETEを拒否する。"""

    def test_obs_events_update_rejected(self, migrated_db):
        conn = get_connection()
        try:
            row_id = conn.execute(
                "INSERT INTO obs_events (session_id, kind) VALUES ('s1', 'boundary')"
            ).lastrowid
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("UPDATE obs_events SET session_id = 's2' WHERE id = ?", (row_id,))
        finally:
            conn.rollback()
            conn.close()

    def test_obs_events_delete_rejected(self, migrated_db):
        conn = get_connection()
        try:
            row_id = conn.execute(
                "INSERT INTO obs_events (session_id, kind) VALUES ('s1', 'boundary')"
            ).lastrowid
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM obs_events WHERE id = ?", (row_id,))
        finally:
            conn.rollback()
            conn.close()

    def test_lessons_update_rejected(self, migrated_db):
        conn = get_connection()
        try:
            lesson_id = _insert_prevent_lesson(conn)
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("UPDATE lessons SET body = 'changed' WHERE id = ?", (lesson_id,))
        finally:
            conn.rollback()
            conn.close()

    def test_lessons_delete_rejected(self, migrated_db):
        conn = get_connection()
        try:
            lesson_id = _insert_prevent_lesson(conn)
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM lessons WHERE id = ?", (lesson_id,))
        finally:
            conn.rollback()
            conn.close()

    def test_lesson_entries_update_rejected(self, migrated_db):
        conn = get_connection()
        try:
            lesson_id = _insert_prevent_lesson(conn)
            entry_id = conn.execute(
                "INSERT INTO lesson_entries (lesson_id, kind, note) VALUES (?, 'note', 'a note')",
                (lesson_id,),
            ).lastrowid
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("UPDATE lesson_entries SET note = 'changed' WHERE id = ?", (entry_id,))
        finally:
            conn.rollback()
            conn.close()

    def test_lesson_entries_delete_rejected(self, migrated_db):
        conn = get_connection()
        try:
            lesson_id = _insert_prevent_lesson(conn)
            entry_id = conn.execute(
                "INSERT INTO lesson_entries (lesson_id, kind, note) VALUES (?, 'note', 'a note')",
                (lesson_id,),
            ).lastrowid
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM lesson_entries WHERE id = ?", (entry_id,))
        finally:
            conn.rollback()
            conn.close()

    def test_lesson_kinds_update_rejected(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("UPDATE lesson_kinds SET delivers = 0 WHERE kind = 'tally'")
        finally:
            conn.rollback()
            conn.close()

    def test_lesson_kinds_delete_rejected(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM lesson_kinds WHERE kind = 'tally'")
        finally:
            conn.rollback()
            conn.close()

    def test_vessel_cursor_and_vessel_meta_updates_are_allowed(self, migrated_db):
        """vessel_cursor・vessel_metaは更新を許す（追記専用の対象外）。"""
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO vessel_cursor (session_id, byte_offset) VALUES ('s1', 0)"
            )
            conn.execute(
                "UPDATE vessel_cursor SET byte_offset = 100 WHERE session_id = 's1'"
            )
            conn.execute("UPDATE vessel_meta SET mode = 'on' WHERE id = 1")
            conn.commit()
            row = conn.execute(
                "SELECT byte_offset FROM vessel_cursor WHERE session_id = 's1'"
            ).fetchone()
            assert row["byte_offset"] == 100
            mode_row = conn.execute("SELECT mode FROM vessel_meta WHERE id = 1").fetchone()
            assert mode_row["mode"] == "on"
        finally:
            conn.close()


class TestLessonsKindShapeTrigger:
    def test_kind_mismatch_rejected(self, migrated_db):
        """tallyは条件を持てない: deliver_eventを与えるとkind_mismatchで拒否される。"""
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError, match="vessel:kind_mismatch"):
                conn.execute(
                    "INSERT INTO lessons (kind, handle, body, deliver_event, deliver_spec) "
                    "VALUES ('tally', 'bad-tally', 'body', 'tool_call', '{}')"
                )
        finally:
            conn.rollback()
            conn.close()

    def test_prevent_shape_accepted(self, migrated_db):
        conn = get_connection()
        try:
            lesson_id = _insert_prevent_lesson(conn)
            conn.commit()
            assert lesson_id is not None
        finally:
            conn.close()

    def test_tally_shape_accepted(self, migrated_db):
        conn = get_connection()
        try:
            lesson_id = _insert_tally_lesson(conn)
            conn.commit()
            assert lesson_id is not None
        finally:
            conn.close()

    def test_guide_with_session_channel_rejected(self, migrated_db):
        """踏み跡を持たない種類（guide）に常時配達を与えるとno_session_channelで拒否される。"""
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError, match="vessel:no_session_channel"):
                conn.execute(
                    "INSERT INTO lessons (kind, handle, body, deliver_event, deliver_spec) "
                    "VALUES ('guide', 'bad-guide-session', 'body', 'session', '{}')"
                )
        finally:
            conn.rollback()
            conn.close()

    def test_guide_with_non_session_channel_accepted(self, migrated_db):
        conn = get_connection()
        try:
            lesson_id = _insert_guide_lesson(conn)
            conn.commit()
            assert lesson_id is not None
        finally:
            conn.close()


class TestLessonEntriesConditionsTrigger:
    def test_conditions_on_tally_rejected(self, migrated_db):
        """配達しない種類（tally）へのconditions追記はdeliversが0なので拒否される。"""
        conn = get_connection()
        try:
            lesson_id = _insert_tally_lesson(conn)
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError, match="vessel:kind_mismatch"):
                conn.execute(
                    "INSERT INTO lesson_entries (lesson_id, kind, deliver_event, deliver_spec) "
                    "VALUES (?, 'conditions', 'tool_call', '{}')",
                    (lesson_id,),
                )
        finally:
            conn.rollback()
            conn.close()

    def test_conditions_shape_mismatch_rejected(self, migrated_db):
        """prevent（steps=1）へのconditions追記でstep_eventを欠くとkind_mismatch。"""
        conn = get_connection()
        try:
            lesson_id = _insert_prevent_lesson(conn)
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError, match="vessel:kind_mismatch"):
                conn.execute(
                    "INSERT INTO lesson_entries (lesson_id, kind, deliver_event, deliver_spec) "
                    "VALUES (?, 'conditions', 'tool_call', '{}')",
                    (lesson_id,),
                )
        finally:
            conn.rollback()
            conn.close()

    def test_conditions_matching_shape_accepted(self, migrated_db):
        conn = get_connection()
        try:
            lesson_id = _insert_prevent_lesson(conn)
            conn.commit()
            entry_id = conn.execute(
                "INSERT INTO lesson_entries (lesson_id, kind, deliver_event, deliver_spec, step_event, step_spec) "
                "VALUES (?, 'conditions', 'tool_fail', '{}', 'tool_fail', '{}')",
                (lesson_id,),
            ).lastrowid
            conn.commit()
            assert entry_id is not None
        finally:
            conn.close()

    def test_conditions_no_session_channel_on_guide_rejected(self, migrated_db):
        """初期値（guide）へのconditions追記でdeliver_event='session'を与えると拒否される。

        record_lesson からは seed_only が先に返るため、この経路が実際に使われる
        のは初期値への追記のときだけである。
        """
        conn = get_connection()
        try:
            lesson_id = _insert_guide_lesson(conn)
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError, match="vessel:no_session_channel"):
                conn.execute(
                    "INSERT INTO lesson_entries (lesson_id, kind, deliver_event, deliver_spec) "
                    "VALUES (?, 'conditions', 'session', '{}')",
                    (lesson_id,),
                )
        finally:
            conn.rollback()
            conn.close()


class TestUniqueSpeakerIndex:
    def test_duplicate_speaker_for_same_utterance_rejected(self, migrated_db):
        conn = get_connection()
        try:
            utterance_id = conn.execute(
                "INSERT INTO obs_events (session_id, kind, text) VALUES ('s1', 'utterance', 'hi')"
            ).lastrowid
            conn.execute(
                "INSERT INTO obs_events (session_id, kind, text, ref_id) VALUES ('s1', 'speaker', '{}', ?)",
                (utterance_id,),
            )
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO obs_events (session_id, kind, text, ref_id) VALUES ('s1', 'speaker', '{}', ?)",
                    (utterance_id,),
                )
        finally:
            conn.rollback()
            conn.close()


class TestObsEventsCheckConstraints:
    def test_delivered_requires_lesson_id(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO obs_events (session_id, kind, channel) VALUES ('s1', 'delivered', 'pull')"
                )
        finally:
            conn.rollback()
            conn.close()

    def test_human_withdraw_requires_ref_id(self, migrated_db):
        conn = get_connection()
        try:
            lesson_id = _insert_prevent_lesson(conn)
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO obs_events (session_id, kind, lesson_id) VALUES ('s1', 'human_withdraw', ?)",
                    (lesson_id,),
                )
        finally:
            conn.rollback()
            conn.close()

    def test_tool_requires_tool_name(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO obs_events (session_id, kind) VALUES ('s1', 'tool')")
        finally:
            conn.rollback()
            conn.close()

    def test_duplicate_src_uuid_in_same_session_rejected(self, migrated_db):
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO obs_events (session_id, kind, text, src_uuid) "
                "VALUES ('s1', 'reply', 'hello', 'u-1')"
            )
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO obs_events (session_id, kind, text, src_uuid) "
                    "VALUES ('s1', 'reply', 'hello again', 'u-1')"
                )
        finally:
            conn.rollback()
            conn.close()

    def test_handle_format_rejected_for_uppercase(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO lessons (kind, handle, body) VALUES ('tally', 'Bad-Handle', 'body')"
                )
        finally:
            conn.rollback()
            conn.close()


class TestFtsSync:
    def test_lesson_insert_creates_fts_row(self, migrated_db):
        conn = get_connection()
        try:
            lesson_id = conn.execute(
                "INSERT INTO lessons (kind, handle, body, quote) "
                "VALUES ('tally', 'fts-seed-lesson', 'original body text', 'original quote text')"
            ).lastrowid
            conn.commit()
            row = conn.execute(
                "SELECT handle, body, quote FROM lessons_fts WHERE rowid = ?", (lesson_id,)
            ).fetchone()
            assert row["handle"] == "fts-seed-lesson"
            assert row["body"] == "original body text"
            assert row["quote"] == "original quote text"
        finally:
            conn.close()

    def test_body_entry_updates_fts_body(self, migrated_db):
        conn = get_connection()
        try:
            lesson_id = conn.execute(
                "INSERT INTO lessons (kind, handle, body) VALUES ('tally', 'fts-update-lesson', 'old body')"
            ).lastrowid
            conn.execute(
                "INSERT INTO lesson_entries (lesson_id, kind, body) VALUES (?, 'body', 'new body text')",
                (lesson_id,),
            )
            conn.commit()
            row = conn.execute(
                "SELECT body FROM lessons_fts WHERE rowid = ?", (lesson_id,)
            ).fetchone()
            assert row["body"] == "new body text"
        finally:
            conn.close()


class TestExistingDataUnaffected:
    def test_migration_does_not_touch_unrelated_tables(self, db_before_0075):
        conn = get_connection()
        try:
            tag_id = conn.execute(
                "INSERT INTO tags (namespace, name) VALUES ('domain', 'vessel-migration-check')"
            ).lastrowid
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0075(db_before_0075)

        conn = get_connection()
        try:
            row = conn.execute("SELECT name FROM tags WHERE id = ?", (tag_id,)).fetchone()
            assert row["name"] == "vessel-migration-check"
        finally:
            conn.close()
