"""migration 0077_add_goals のテスト

0077 適用後に goals / goal_conditions / goal_activities が期待通り存在し、
04_スキーマ.md「3.2 実行確認の結果」に列挙された拒否ケースが全件 IntegrityError に
なること、既存の activities 行が壊れないこと、CASCADE・FK・NOT NULL（WITHOUT ROWID）が
機能することを、goal_service を経由せず生SQLで検証する。
"""
import sqlite3

import pytest

from src.db import get_connection, init_database
from src.services.tag_service import _injected_tags
from test_migrations.conftest import db_before_migration, get_column_names, index_names, table_exists


@pytest.fixture
def migrated_db(temp_db):
    """全migration（0077含む）を適用済みのテスト用DBを提供する。"""
    yield temp_db


@pytest.fixture
def db_before_0077():
    """0076までのmigrationを適用したDBを提供する。0077の挙動を分離検証するために使う。"""
    with db_before_migration("0077") as db_path:
        _injected_tags.clear()
        yield db_path


def _insert_goal(conn: sqlite3.Connection, **overrides) -> int:
    fields = {"handle": "test-goal", "statement": "終わりの一文"}
    fields.update(overrides)
    columns = ", ".join(fields.keys())
    placeholders = ", ".join("?" for _ in fields)
    cur = conn.execute(
        f"INSERT INTO goals ({columns}) VALUES ({placeholders})",
        tuple(fields.values()),
    )
    return cur.lastrowid


def _insert_condition(conn: sqlite3.Connection, goal_id: int, **overrides) -> int:
    fields = {"goal_id": goal_id, "statement": "条件文", "actor": "claude"}
    fields.update(overrides)
    columns = ", ".join(fields.keys())
    placeholders = ", ".join("?" for _ in fields)
    cur = conn.execute(
        f"INSERT INTO goal_conditions ({columns}) VALUES ({placeholders})",
        tuple(fields.values()),
    )
    return cur.lastrowid


def _insert_activity(conn: sqlite3.Connection, title: str = "a1") -> int:
    cur = conn.execute(
        "INSERT INTO activities (title, description) VALUES (?, ?)",
        (title, "desc"),
    )
    return cur.lastrowid


class TestTablesCreated:
    def test_goal_tables_do_not_exist_before_0077(self, db_before_0077):
        conn = get_connection()
        try:
            assert not table_exists(conn, "goals")
            assert not table_exists(conn, "goal_conditions")
            assert not table_exists(conn, "goal_activities")
        finally:
            conn.close()

    def test_goal_tables_exist_after_0077(self, migrated_db):
        conn = get_connection()
        try:
            assert table_exists(conn, "goals")
            assert table_exists(conn, "goal_conditions")
            assert table_exists(conn, "goal_activities")
        finally:
            conn.close()

    def test_expected_columns(self, migrated_db):
        conn = get_connection()
        try:
            goals_cols = get_column_names(conn, "goals")
            conditions_cols = get_column_names(conn, "goal_conditions")
            activities_link_cols = get_column_names(conn, "goal_activities")
            activities_cols = get_column_names(conn, "activities")
        finally:
            conn.close()
        assert {
            "id", "handle", "statement", "closed", "verdict",
            "judged_by", "judged_at", "judge_note", "created_at",
        } <= goals_cols
        assert {
            "id", "goal_id", "statement", "actor", "state", "note",
            "last_satisfied_at", "bound_type", "bound_id", "created_at", "updated_at",
        } <= conditions_cols
        assert {"activity_id", "goal_id", "waiver_reason", "added_at"} <= activities_link_cols
        assert {"closed_at", "closed_by", "closed_reason"} <= activities_cols

    def test_expected_indexes(self, migrated_db):
        conn = get_connection()
        try:
            names = index_names(conn, "idx_goal%")
        finally:
            conn.close()
        assert {"idx_goal_conditions_goal", "idx_goal_activities_goal"} <= names

    def test_goal_activities_is_without_rowid(self, migrated_db):
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='goal_activities'"
            ).fetchone()
        finally:
            conn.close()
        assert "WITHOUT ROWID" in row["sql"]


class TestExistingActivitiesUnaffected:
    def test_pre_0077_activity_gets_null_new_columns_after_full_migration(self):
        with db_before_migration("0077"):
            conn = get_connection()
            try:
                activity_id = _insert_activity(conn, title="pre-existing")
                conn.commit()
                row = conn.execute(
                    "SELECT title, status FROM activities WHERE id = ?", (activity_id,)
                ).fetchone()
                assert row["title"] == "pre-existing"
                assert row["status"] == "pending"
            finally:
                conn.close()

            # 0077以降の残りのmigrationを同じDBファイルに適用する。
            init_database()
            conn = get_connection()
            try:
                row = conn.execute(
                    "SELECT title, closed_at, closed_by, closed_reason FROM activities WHERE id = ?",
                    (activity_id,),
                ).fetchone()
            finally:
                conn.close()
        _injected_tags.clear()
        assert row["title"] == "pre-existing"
        assert row["closed_at"] is None
        assert row["closed_by"] is None
        assert row["closed_reason"] is None


class TestGoalsCheckConstraints:
    def test_handle_uppercase_rejected(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_goal(conn, handle="Bad-Handle")
        finally:
            conn.rollback()
            conn.close()

    def test_handle_with_whitespace_rejected(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_goal(conn, handle="bad handle")
        finally:
            conn.rollback()
            conn.close()

    def test_handle_empty_rejected(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_goal(conn, handle="")
        finally:
            conn.rollback()
            conn.close()

    def test_handle_duplicate_rejected(self, migrated_db):
        conn = get_connection()
        try:
            _insert_goal(conn, handle="dup-handle")
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                _insert_goal(conn, handle="dup-handle")
        finally:
            conn.rollback()
            conn.close()

    def test_statement_whitespace_only_rejected(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_goal(conn, statement="   ")
        finally:
            conn.rollback()
            conn.close()

    def test_closed_without_verdict_rejected(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_goal(conn, closed=1)
        finally:
            conn.rollback()
            conn.close()

    def test_verdict_without_judged_at_rejected(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_goal(conn, verdict="achieved", judged_by="session")
        finally:
            conn.rollback()
            conn.close()

    def test_failed_without_reason_rejected(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_goal(
                    conn,
                    verdict="failed",
                    judged_by="session",
                    judged_at="2026-01-01 00:00:00",
                )
        finally:
            conn.rollback()
            conn.close()

    def test_judged_by_out_of_range_rejected(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_goal(
                    conn,
                    verdict="achieved",
                    judged_by="claude",
                    judged_at="2026-01-01 00:00:00",
                )
        finally:
            conn.rollback()
            conn.close()

    def test_achieved_verdict_without_note_accepted(self, migrated_db):
        conn = get_connection()
        try:
            goal_id = _insert_goal(
                conn,
                handle="achieved-goal",
                closed=1,
                verdict="achieved",
                judged_by="session",
                judged_at="2026-01-01 00:00:00",
            )
            conn.commit()
            row = conn.execute("SELECT verdict FROM goals WHERE id = ?", (goal_id,)).fetchone()
        finally:
            conn.close()
        assert row["verdict"] == "achieved"


class TestGoalConditionsCheckConstraints:
    def test_satisfied_without_last_satisfied_at_rejected(self, migrated_db):
        conn = get_connection()
        try:
            goal_id = _insert_goal(conn)
            with pytest.raises(sqlite3.IntegrityError):
                _insert_condition(conn, goal_id, state="satisfied")
        finally:
            conn.rollback()
            conn.close()

    def test_waived_without_note_rejected(self, migrated_db):
        conn = get_connection()
        try:
            goal_id = _insert_goal(conn)
            with pytest.raises(sqlite3.IntegrityError):
                _insert_condition(conn, goal_id, state="waived")
        finally:
            conn.rollback()
            conn.close()

    def test_bound_type_without_bound_id_rejected(self, migrated_db):
        conn = get_connection()
        try:
            goal_id = _insert_goal(conn)
            with pytest.raises(sqlite3.IntegrityError):
                _insert_condition(conn, goal_id, bound_type="activity")
        finally:
            conn.rollback()
            conn.close()

    def test_bound_type_external_rejected(self, migrated_db):
        """束縛の型は activity/decision/ask の3種に限る。externalは値域外。"""
        conn = get_connection()
        try:
            goal_id = _insert_goal(conn)
            with pytest.raises(sqlite3.IntegrityError):
                _insert_condition(conn, goal_id, bound_type="external", bound_id=1)
        finally:
            conn.rollback()
            conn.close()

    def test_actor_out_of_range_rejected(self, migrated_db):
        conn = get_connection()
        try:
            goal_id = _insert_goal(conn)
            with pytest.raises(sqlite3.IntegrityError):
                _insert_condition(conn, goal_id, actor="ai")
        finally:
            conn.rollback()
            conn.close()

    def test_condition_for_nonexistent_goal_rejected(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_condition(conn, 999999)
        finally:
            conn.rollback()
            conn.close()

    def test_satisfied_with_timestamp_accepted(self, migrated_db):
        conn = get_connection()
        try:
            goal_id = _insert_goal(conn)
            condition_id = _insert_condition(
                conn, goal_id, state="satisfied", last_satisfied_at="2026-01-01 00:00:00"
            )
            conn.commit()
            row = conn.execute(
                "SELECT state FROM goal_conditions WHERE id = ?", (condition_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row["state"] == "satisfied"


class TestGoalActivitiesConstraints:
    def test_link_and_waiver_together_rejected(self, migrated_db):
        conn = get_connection()
        try:
            goal_id = _insert_goal(conn)
            activity_id = _insert_activity(conn)
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO goal_activities (activity_id, goal_id, waiver_reason) VALUES (?, ?, ?)",
                    (activity_id, goal_id, "不要"),
                )
        finally:
            conn.rollback()
            conn.close()

    def test_neither_link_nor_waiver_rejected(self, migrated_db):
        conn = get_connection()
        try:
            activity_id = _insert_activity(conn)
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO goal_activities (activity_id) VALUES (?)",
                    (activity_id,),
                )
        finally:
            conn.rollback()
            conn.close()

    def test_second_row_for_same_activity_rejected(self, migrated_db):
        conn = get_connection()
        try:
            goal_id = _insert_goal(conn)
            activity_id = _insert_activity(conn)
            conn.execute(
                "INSERT INTO goal_activities (activity_id, goal_id) VALUES (?, ?)",
                (activity_id, goal_id),
            )
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO goal_activities (activity_id, waiver_reason) VALUES (?, ?)",
                    (activity_id, "不要"),
                )
        finally:
            conn.rollback()
            conn.close()

    def test_waiver_reason_whitespace_only_rejected(self, migrated_db):
        conn = get_connection()
        try:
            activity_id = _insert_activity(conn)
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO goal_activities (activity_id, waiver_reason) VALUES (?, ?)",
                    (activity_id, "   "),
                )
        finally:
            conn.rollback()
            conn.close()

    def test_missing_activity_id_rejected(self, migrated_db):
        """WITHOUT ROWIDなので、activity_idを省いたINSERTがNOT NULLで拒否される
        （rowidの別名になっていれば自動採番で通ってしまう）。"""
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO goal_activities (waiver_reason) VALUES (?)",
                    ("不要",),
                )
        finally:
            conn.rollback()
            conn.close()

    def test_link_to_nonexistent_activity_rejected(self, migrated_db):
        conn = get_connection()
        try:
            goal_id = _insert_goal(conn)
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO goal_activities (activity_id, goal_id) VALUES (?, ?)",
                    (999999, goal_id),
                )
        finally:
            conn.rollback()
            conn.close()

    def test_normal_link_accepted(self, migrated_db):
        conn = get_connection()
        try:
            goal_id = _insert_goal(conn)
            activity_id = _insert_activity(conn)
            conn.execute(
                "INSERT INTO goal_activities (activity_id, goal_id) VALUES (?, ?)",
                (activity_id, goal_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT goal_id FROM goal_activities WHERE activity_id = ?", (activity_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row["goal_id"] == goal_id

    def test_waiver_replaced_with_link_rewrites_added_at(self, migrated_db):
        conn = get_connection()
        try:
            activity_id = _insert_activity(conn)
            conn.execute(
                "INSERT INTO goal_activities (activity_id, waiver_reason, added_at) VALUES (?, ?, ?)",
                (activity_id, "常駐タスク", "2020-01-01 00:00:00"),
            )
            conn.commit()

            goal_id = _insert_goal(conn)
            conn.execute(
                """
                UPDATE goal_activities SET goal_id = ?, waiver_reason = NULL, added_at = CURRENT_TIMESTAMP
                WHERE activity_id = ?
                """,
                (goal_id, activity_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT goal_id, waiver_reason, added_at FROM goal_activities WHERE activity_id = ?",
                (activity_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row["goal_id"] == goal_id
        assert row["waiver_reason"] is None
        assert row["added_at"] != "2020-01-01 00:00:00"


class TestCascadeDeletes:
    def test_deleting_goal_cascades_conditions_and_links(self, migrated_db):
        conn = get_connection()
        try:
            goal_id = _insert_goal(conn)
            condition_id = _insert_condition(conn, goal_id)
            activity_id = _insert_activity(conn)
            conn.execute(
                "INSERT INTO goal_activities (activity_id, goal_id) VALUES (?, ?)",
                (activity_id, goal_id),
            )
            conn.commit()

            conn.execute("DELETE FROM goals WHERE id = ?", (goal_id,))
            conn.commit()

            remaining_conditions = conn.execute(
                "SELECT COUNT(*) FROM goal_conditions WHERE id = ?", (condition_id,)
            ).fetchone()[0]
            remaining_links = conn.execute(
                "SELECT COUNT(*) FROM goal_activities WHERE activity_id = ?", (activity_id,)
            ).fetchone()[0]
        finally:
            conn.close()
        assert remaining_conditions == 0
        assert remaining_links == 0

    def test_deleting_activity_cascades_link(self, migrated_db):
        conn = get_connection()
        try:
            goal_id = _insert_goal(conn)
            activity_id = _insert_activity(conn)
            conn.execute(
                "INSERT INTO goal_activities (activity_id, goal_id) VALUES (?, ?)",
                (activity_id, goal_id),
            )
            conn.commit()

            conn.execute("DELETE FROM activities WHERE id = ?", (activity_id,))
            conn.commit()

            remaining = conn.execute(
                "SELECT COUNT(*) FROM goal_activities WHERE activity_id = ?", (activity_id,)
            ).fetchone()[0]
        finally:
            conn.close()
        assert remaining == 0


class TestActivitiesClosedColumns:
    def test_closed_by_without_closed_at_rejected(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO activities (title, description, closed_by) VALUES (?, ?, ?)",
                    ("a", "d", "user"),
                )
        finally:
            conn.rollback()
            conn.close()

    def test_closed_by_out_of_range_rejected(self, migrated_db):
        conn = get_connection()
        try:
            activity_id = _insert_activity(conn)
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE activities SET closed_at = CURRENT_TIMESTAMP, closed_by = ? WHERE id = ?",
                    ("goal_auto", activity_id),
                )
        finally:
            conn.rollback()
            conn.close()

    def test_clearing_closed_at_while_closed_by_remains_rejected(self, migrated_db):
        conn = get_connection()
        try:
            activity_id = _insert_activity(conn)
            conn.execute(
                "UPDATE activities SET closed_at = CURRENT_TIMESTAMP, closed_by = 'user' WHERE id = ?",
                (activity_id,),
            )
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE activities SET closed_at = NULL WHERE id = ?", (activity_id,)
                )
        finally:
            conn.rollback()
            conn.close()

    def test_closed_at_only_then_closed_by_accepted(self, migrated_db):
        """closed_atだけを先に書き、その後closed_byを書く経路が通ることを確かめる
        （closed_byのCHECKがclosed_atを参照するため、DDLでclosed_atを先に足した）。"""
        conn = get_connection()
        try:
            activity_id = _insert_activity(conn)
            conn.execute(
                "UPDATE activities SET closed_at = CURRENT_TIMESTAMP WHERE id = ?", (activity_id,)
            )
            conn.commit()
            conn.execute(
                "UPDATE activities SET closed_by = 'claude', closed_reason = '完了' WHERE id = ?",
                (activity_id,),
            )
            conn.commit()
            row = conn.execute(
                "SELECT closed_by, closed_reason FROM activities WHERE id = ?", (activity_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row["closed_by"] == "claude"
        assert row["closed_reason"] == "完了"
