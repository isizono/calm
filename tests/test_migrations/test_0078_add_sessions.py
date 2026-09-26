"""migration 0078_add_sessions のテスト

0078適用後にsessionsテーブルと2本の索引が期待通り存在し、CHECK制約・部分一意索引が
機能することを、session_ledger_serviceを経由せず生SQLで検証する。
"""
import sqlite3

import pytest

from src.db import get_connection
from src.services.tag_service import _injected_tags
from test_migrations.conftest import db_before_migration, get_column_names, index_names, table_exists


@pytest.fixture
def migrated_db(temp_db):
    """全migration(0078含む)を適用済みのテスト用DBを提供する。"""
    yield temp_db


@pytest.fixture
def db_before_0078():
    """0077までのmigrationを適用したDBを提供する。0078の挙動を分離検証するために使う。"""
    with db_before_migration("0078") as db_path:
        _injected_tags.clear()
        yield db_path


def _insert_minimal(conn: sqlite3.Connection, session_id: str, **overrides) -> None:
    """CHECK制約を満たす最小限のsessions行をINSERTする。overridesで個別カラムを差し替える。"""
    row = {
        "session_id": session_id,
        "id_kind": "bridge",
        "mode": "interactive",
    }
    row.update(overrides)
    columns = ", ".join(row.keys())
    placeholders = ", ".join("?" for _ in row)
    conn.execute(
        f"INSERT INTO sessions ({columns}) VALUES ({placeholders})",
        tuple(row.values()),
    )


class TestSessionsTable:
    def test_table_does_not_exist_before_0078(self, db_before_0078):
        conn = get_connection()
        try:
            exists = table_exists(conn, "sessions")
        finally:
            conn.close()
        assert exists is False

    def test_table_and_indices_exist_after_0078(self, migrated_db):
        conn = get_connection()
        try:
            exists = table_exists(conn, "sessions")
            columns = get_column_names(conn, "sessions")
            indices = index_names(conn, "idx_sessions_%")
            pk_rows = conn.execute("PRAGMA table_info(sessions)").fetchall()
        finally:
            conn.close()
        assert exists is True
        assert columns == {
            "session_id", "id_kind", "harness", "host", "cwd",
            "cli_session_id", "cli_pid", "cli_resolve_status", "mode",
            "last_heartbeat_at", "last_tool_call_at",
            "last_checkin_activity_id", "last_checkin_at",
            "ended_at", "ended_reason",
        }
        assert indices == {"idx_sessions_live", "idx_sessions_cli_live"}
        pk_columns = {row["name"] for row in pk_rows if row["pk"] > 0}
        assert pk_columns == {"session_id"}

    def test_id_kind_rejects_unknown_value(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_minimal(conn, "s1", id_kind="unknown")
        finally:
            conn.rollback()
            conn.close()

    def test_mode_rejects_unknown_value(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_minimal(conn, "s1", mode="unknown")
        finally:
            conn.rollback()
            conn.close()

    def test_ended_reason_rejects_unknown_value(self, migrated_db):
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_minimal(
                    conn, "s1",
                    ended_at="2026-01-01T00:00:00Z", ended_reason="unknown",
                )
        finally:
            conn.rollback()
            conn.close()

    def test_ended_at_and_ended_reason_must_both_be_set_or_both_null(self, migrated_db):
        """ended_atのみ・ended_reasonのみの片方だけの行はCHECK制約で拒否される。"""
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_minimal(conn, "s1", ended_at="2026-01-01T00:00:00Z")
        finally:
            conn.rollback()
            conn.close()

        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_minimal(conn, "s2", ended_reason="unregister")
        finally:
            conn.rollback()
            conn.close()

    def test_ended_at_and_ended_reason_both_set_succeeds(self, migrated_db):
        conn = get_connection()
        try:
            _insert_minimal(
                conn, "s1",
                ended_at="2026-01-01T00:00:00Z", ended_reason="unregister",
            )
            conn.commit()
            row = conn.execute(
                "SELECT ended_at, ended_reason FROM sessions WHERE session_id = 's1'"
            ).fetchone()
        finally:
            conn.close()
        assert row["ended_at"] == "2026-01-01T00:00:00Z"
        assert row["ended_reason"] == "unregister"

    def test_cli_session_id_null_rows_can_coexist(self, migrated_db):
        """会話識別子がNULLの行は部分一意索引の対象外で、複数存在してよい。"""
        conn = get_connection()
        try:
            _insert_minimal(conn, "s1")
            _insert_minimal(conn, "s2")
            conn.commit()
            count = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE cli_session_id IS NULL"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 2

    def test_two_live_rows_with_same_harness_and_cli_session_id_rejected(self, migrated_db):
        """終了していない行同士で同じharness・cli_session_idが重複するとpartial unique indexで拒否される。"""
        conn = get_connection()
        try:
            _insert_minimal(conn, "s1", harness="claude_code", cli_session_id="cli-1")
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                _insert_minimal(conn, "s2", harness="claude_code", cli_session_id="cli-1")
        finally:
            conn.rollback()
            conn.close()

    def test_two_live_rows_with_different_harness_and_same_cli_session_id_coexist(self, migrated_db):
        """harnessが異なれば、cli_session_idが同じ値でも一意制約に抵触しない。

        会話識別子の番号体系はharnessごとに異なるため、異なるharness間で同じ
        cli_session_id値が偶然一致しても別の会話として扱う。索引が
        (harness, cli_session_id)の複合になっている理由そのものの検証。
        """
        conn = get_connection()
        try:
            _insert_minimal(conn, "s1", harness="claude_code", cli_session_id="cli-1")
            _insert_minimal(conn, "s2", harness="codex", cli_session_id="cli-1")
            conn.commit()
            count = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE cli_session_id = 'cli-1' AND ended_at IS NULL"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 2

    def test_two_live_rows_with_null_harness_and_same_cli_session_id_coexist(self, migrated_db):
        """harnessがNULLの行同士は、cli_session_idが同じでも一意制約に抵触しない。

        SQLiteのUNIQUE制約はNULL同士を区別する(等しいとみなさない)ため、既存の
        「会話識別子NULLの行は複数存在してよい」という挙動と同じ理屈で、
        harnessがNULLの行同士も複数存在できる(harness列を複合索引に加える前の
        挙動を維持する)。
        """
        conn = get_connection()
        try:
            _insert_minimal(conn, "s1", cli_session_id="cli-1")
            _insert_minimal(conn, "s2", cli_session_id="cli-1")
            conn.commit()
            count = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE cli_session_id = 'cli-1' AND ended_at IS NULL"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 2

    def test_generational_handover_requires_closing_old_row_first(self, migrated_db):
        """世代交代: 古い行をended化してから新しい行を挿入する順序を守れば成功する。

        逆順(先に新行をINSERTしてから旧行を閉じようとする)は idx_sessions_cli_live
        の部分一意索引違反でINSERT自体が失敗するため、閉じる→立てるの順序を守る
        必要があることを検証する。
        """
        conn = get_connection()
        try:
            _insert_minimal(conn, "s1", harness="claude_code", cli_session_id="cli-1")
            conn.commit()

            # 誤った順序(先にINSERT)は一意索引違反になる
            with pytest.raises(sqlite3.IntegrityError):
                _insert_minimal(conn, "s2", harness="claude_code", cli_session_id="cli-1")
            conn.rollback()

            # 正しい順序: 先に旧行をended化してから新行を挿入する
            conn.execute(
                "UPDATE sessions SET ended_at = '2026-01-01T00:00:00Z', "
                "ended_reason = 'superseded' WHERE session_id = 's1'"
            )
            _insert_minimal(conn, "s2", harness="claude_code", cli_session_id="cli-1")
            conn.commit()

            live = conn.execute(
                "SELECT session_id FROM sessions WHERE cli_session_id = 'cli-1' AND ended_at IS NULL"
            ).fetchall()
            ended = conn.execute(
                "SELECT ended_reason FROM sessions WHERE session_id = 's1'"
            ).fetchone()
        finally:
            conn.close()
        assert [r["session_id"] for r in live] == ["s2"]
        assert ended["ended_reason"] == "superseded"

    def test_ended_row_can_coexist_with_live_row_of_same_cli_session_id(self, migrated_db):
        """endedな行はpartial unique indexの対象外なので、同じcli_session_idの生存行と共存できる。"""
        conn = get_connection()
        try:
            _insert_minimal(
                conn, "s1", harness="claude_code", cli_session_id="cli-1",
                ended_at="2026-01-01T00:00:00Z", ended_reason="superseded",
            )
            _insert_minimal(conn, "s2", harness="claude_code", cli_session_id="cli-1")
            conn.commit()
            count = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE cli_session_id = 'cli-1'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 2
