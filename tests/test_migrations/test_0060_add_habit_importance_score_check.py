"""migration 0060_add_habit_importance_score_check のテスト

0058でimportance_scoreを追加した時点の既定値は1.0（旧「値が大きいほど優先度が
高い」前提の名残）だが、0059/0060と同時に導入されるアプリケーション側の意味づけは
1=critical/2=important/3=defaultという順位型になった。0060適用前に
trigger_mode='intelligently'へ切り替え済みでimportance_scoreを未設定（既定値1.0の
まま）だったhabitは、意味づけ変更後は「まだトリアージされていないだけ」なのに
critical扱いされてしまう。本テストは、0060がこのケースをimportance_score=3
（default）へ補正すること、既に別の値が入っている行やalwaysモードの行は
補正対象にしないこと、そしてimportance_scoreへのCHECK制約（1/2/3のみ許可）が
テーブル再構築後に機能することを確認する。
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
    """全migration（0060含む）を適用済みのテスト用DBを提供する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def db_before_0060():
    """0059までのmigrationを適用したDBを提供する。0060の挙動を分離検証するために使う。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path

        parsed = parse_uri(f"sqlite:///{db_path}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        backend.init_database()
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        pre_0060 = MigrationList([m for m in all_migs if m.id < "0060"])
        with backend.lock():
            backend.apply_migrations(pre_0060)

        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


def _apply_migration_0060(db_path: str) -> None:
    """db_pathに対してmigration 0060のみを適用する。"""
    parsed = parse_uri(f"sqlite:///{db_path}")
    backend = _VecSQLiteBackend(parsed, default_migration_table)
    all_migs = read_migrations(str(MIGRATIONS_DIR))
    only_0060 = MigrationList([m for m in all_migs if m.id.startswith("0060")])
    with backend.lock():
        backend.apply_migrations(only_0060)


class TestExistingDataMigration:
    """0060適用時の既存importance_score補正の確認"""

    def test_untriaged_intelligently_habit_becomes_default(self, db_before_0060):
        """trigger_mode='intelligently'かつimportance_score=1.0(未設定)のhabitは3に補正される"""
        conn = get_connection()
        try:
            habit_id = conn.execute(
                "INSERT INTO habits (content, trigger_mode) VALUES (?, ?)",
                ("untriaged intelligently habit", "intelligently"),
            ).lastrowid
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0060(db_before_0060)

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT importance_score FROM habits WHERE id = ?", (habit_id,)
            ).fetchone()
            assert row["importance_score"] == 3
        finally:
            conn.close()

    def test_explicitly_scored_intelligently_habit_unaffected(self, db_before_0060):
        """importance_scoreが既に1.0以外に設定済みのintelligently habitは補正されない"""
        conn = get_connection()
        try:
            habit_id = conn.execute(
                "INSERT INTO habits (content, trigger_mode, importance_score) VALUES (?, ?, ?)",
                ("already scored habit", "intelligently", 2),
            ).lastrowid
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0060(db_before_0060)

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT importance_score FROM habits WHERE id = ?", (habit_id,)
            ).fetchone()
            assert row["importance_score"] == 2
        finally:
            conn.close()

    def test_always_mode_habit_with_default_score_unaffected(self, db_before_0060):
        """trigger_mode='always'のhabitはimportance_score=1.0のままでも補正対象にならない"""
        conn = get_connection()
        try:
            habit_id = conn.execute(
                "INSERT INTO habits (content) VALUES (?)",
                ("always mode habit",),
            ).lastrowid
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0060(db_before_0060)

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT trigger_mode, importance_score FROM habits WHERE id = ?", (habit_id,)
            ).fetchone()
            assert row["trigger_mode"] == "always"
            assert row["importance_score"] == 1.0
        finally:
            conn.close()


class TestImportanceScoreCheckConstraint:
    """0060適用後のimportance_score CHECK制約の確認"""

    def test_check_constraint_rejects_invalid_value(self, migrated_db):
        """importance_scoreのCHECK制約が1/2/3以外を拒否する"""
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO habits (content, importance_score) VALUES (?, ?)",
                    ("不正なスコア", 5),
                )
        finally:
            conn.rollback()
            conn.close()

    def test_check_constraint_rejects_non_integer_value(self, migrated_db):
        """importance_scoreのCHECK制約が1.5等の非整数値も拒否する"""
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO habits (content, importance_score) VALUES (?, ?)",
                    ("中途半端なスコア", 1.5),
                )
        finally:
            conn.rollback()
            conn.close()

    @pytest.mark.parametrize("score", [1, 2, 3])
    def test_check_constraint_accepts_valid_values(self, migrated_db, score):
        """importance_scoreに1/2/3を指定してINSERTできる"""
        conn = get_connection()
        try:
            habit_id = conn.execute(
                "INSERT INTO habits (content, importance_score) VALUES (?, ?)",
                ("有効なスコア", score),
            ).lastrowid
            conn.commit()
            row = conn.execute(
                "SELECT importance_score FROM habits WHERE id = ?", (habit_id,)
            ).fetchone()
            assert row["importance_score"] == score
        finally:
            conn.close()

    def test_default_value_still_satisfies_check(self, migrated_db):
        """importance_scoreを指定せずINSERTしても既定値1.0がCHECK制約を満たす"""
        conn = get_connection()
        try:
            habit_id = conn.execute(
                "INSERT INTO habits (content) VALUES (?)", ("既定値のまま",)
            ).lastrowid
            conn.commit()
            row = conn.execute(
                "SELECT importance_score FROM habits WHERE id = ?", (habit_id,)
            ).fetchone()
            assert row["importance_score"] == 1.0
        finally:
            conn.close()


class TestSchemaPreservedAfterRebuild:
    """テーブル再構築後も既存カラム・制約が保持されていることの確認"""

    def test_status_check_constraint_still_enforced(self, migrated_db):
        """0059で追加されたstatusのCHECK制約が再構築後も機能する"""
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO habits (content, status) VALUES (?, ?)",
                    ("不正なstatus", "deleted"),
                )
        finally:
            conn.rollback()
            conn.close()

    def test_trigger_mode_check_constraint_still_enforced(self, migrated_db):
        """0058で追加されたtrigger_modeのCHECK制約が再構築後も機能する"""
        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO habits (content, trigger_mode) VALUES (?, ?)",
                    ("不正なtrigger_mode", "sometimes"),
                )
        finally:
            conn.rollback()
            conn.close()

    def test_all_columns_preserved(self, migrated_db):
        """再構築後もhabitsの全カラムが揃っている"""
        conn = get_connection()
        try:
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(habits)").fetchall()
            }
            assert columns == {
                "id",
                "content",
                "active",
                "created_at",
                "description",
                "trigger_mode",
                "importance_score",
                "last_recalled_at",
                "status",
            }
        finally:
            conn.close()
