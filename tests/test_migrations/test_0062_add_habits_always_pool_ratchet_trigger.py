"""migration 0062_add_habits_always_pool_ratchet_trigger のテスト

アプリ層のalways昇格ゲート（habit_service._check_always_promotion_gate_with_conn、
定員1500字）はupdate_habit経由の更新にのみ効く。本migrationはそのゲートを経由しない
直接SQL・将来のコードパス追加に備え、DBトリガーによる独立した上限（2000字）を
habitsテーブルに課す。本テストは、トリガーが定員超過かつ増加するINSERT/UPDATEのみを
拒否すること（縮む変更・無効化は超過中でも常に許可するラチェット）を確認する。
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
    """全migration（0062含む）を適用済みのテスト用DBを提供する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def db_before_0062():
    """0061までのmigrationを適用したDBを提供する。0062の挙動を分離検証するために使う。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path

        parsed = parse_uri(f"sqlite:///{db_path}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        backend.init_database()
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        pre_0062 = MigrationList([m for m in all_migs if m.id < "0062"])
        with backend.lock():
            backend.apply_migrations(pre_0062)

        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


def _apply_migration_0062(db_path: str) -> None:
    """db_pathに対してmigration 0062のみを適用する。"""
    parsed = parse_uri(f"sqlite:///{db_path}")
    backend = _VecSQLiteBackend(parsed, default_migration_table)
    all_migs = read_migrations(str(MIGRATIONS_DIR))
    only_0062 = MigrationList([m for m in all_migs if m.id.startswith("0062")])
    with backend.lock():
        backend.apply_migrations(only_0062)


def _neutralize_seed_always_pool(conn) -> None:
    """migration由来の初期habit（trigger_mode='always'）をintelligently化し、
    alwaysプール合計をテストごとに0からの決定論的な値にする。"""
    conn.execute(
        "UPDATE habits SET trigger_mode = 'intelligently' WHERE trigger_mode = 'always'"
    )
    conn.commit()


class TestTriggerExistence:
    """トリガー2本の適用前後の有無の確認"""

    def test_triggers_absent_before_migration(self, db_before_0062):
        conn = get_connection()
        try:
            names = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                ).fetchall()
            }
            assert "trg_habits_always_pool_ratchet_ceiling_ins" not in names
            assert "trg_habits_always_pool_ratchet_ceiling_upd" not in names
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
            assert "trg_habits_always_pool_ratchet_ceiling_ins" in names
            assert "trg_habits_always_pool_ratchet_ceiling_upd" in names
        finally:
            conn.close()


class TestRatchetCeilingOnInsert:
    """INSERTに対するラチェット天井の確認"""

    def test_insert_exceeding_ceiling_is_rejected(self, db_before_0062):
        """合計が2000字を超えるINSERTは拒否される"""
        conn = get_connection()
        try:
            _neutralize_seed_always_pool(conn)
        finally:
            conn.close()

        _apply_migration_0062(db_before_0062)

        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO habits (content, trigger_mode, active) VALUES (?, 'always', 1)",
                    ("x" * 2001,),
                )
        finally:
            conn.rollback()
            conn.close()

    def test_insert_within_ceiling_is_accepted(self, db_before_0062):
        """合計がちょうど2000字のINSERTは許可される（境界値）"""
        conn = get_connection()
        try:
            _neutralize_seed_always_pool(conn)
        finally:
            conn.close()

        _apply_migration_0062(db_before_0062)

        conn = get_connection()
        try:
            habit_id = conn.execute(
                "INSERT INTO habits (content, trigger_mode, active) VALUES (?, 'always', 1)",
                ("x" * 2000,),
            ).lastrowid
            conn.commit()
            row = conn.execute(
                "SELECT LENGTH(content) AS len FROM habits WHERE id = ?", (habit_id,)
            ).fetchone()
            assert row["len"] == 2000
        finally:
            conn.close()

    def test_insert_into_intelligently_ignores_ceiling(self, db_before_0062):
        """trigger_mode='intelligently'のINSERTはalwaysプール天井の対象外"""
        conn = get_connection()
        try:
            _neutralize_seed_always_pool(conn)
        finally:
            conn.close()

        _apply_migration_0062(db_before_0062)

        conn = get_connection()
        try:
            habit_id = conn.execute(
                "INSERT INTO habits (content, trigger_mode, active) VALUES (?, 'intelligently', 1)",
                ("x" * 3000,),
            ).lastrowid
            conn.commit()
            row = conn.execute(
                "SELECT LENGTH(content) AS len FROM habits WHERE id = ?", (habit_id,)
            ).fetchone()
            assert row["len"] == 3000
        finally:
            conn.close()


class TestRatchetCeilingOnUpdate:
    """UPDATEに対するラチェット天井（ラチェット則含む）の確認"""

    def test_promotion_exceeding_ceiling_is_rejected(self, db_before_0062):
        """既存プールが空でも、単独habitの昇格自体が2000字を超えるなら拒否される"""
        conn = get_connection()
        try:
            _neutralize_seed_always_pool(conn)
            habit_id = conn.execute(
                "INSERT INTO habits (content, trigger_mode, active) VALUES (?, 'intelligently', 1)",
                ("x" * 2001,),
            ).lastrowid
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0062(db_before_0062)

        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE habits SET trigger_mode = 'always' WHERE id = ?", (habit_id,)
                )
        finally:
            conn.rollback()
            conn.close()

    def test_content_increase_beyond_ceiling_is_rejected(self, db_before_0062):
        """既にalwaysなhabitのcontentを、天井を超える長さへ伸ばすUPDATEは拒否される"""
        conn = get_connection()
        try:
            _neutralize_seed_always_pool(conn)
            habit_id = conn.execute(
                "INSERT INTO habits (content, trigger_mode, active) VALUES (?, 'always', 1)",
                ("x" * 1000,),
            ).lastrowid
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0062(db_before_0062)

        conn = get_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE habits SET content = ? WHERE id = ?", ("x" * 2001, habit_id)
                )
        finally:
            conn.rollback()
            conn.close()

    def test_shrink_while_pool_already_over_ceiling_is_allowed(self, db_before_0062):
        """トリガー導入前から2000字超のプールが存在する状態でも、合計を減らす更新は許可される
        （ラチェットの核: 減少は常に許可、増加のみ拒否）"""
        conn = get_connection()
        try:
            _neutralize_seed_always_pool(conn)
            habit_id = conn.execute(
                "INSERT INTO habits (content, trigger_mode, active) VALUES (?, 'always', 1)",
                ("x" * 2500,),
            ).lastrowid
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0062(db_before_0062)

        conn = get_connection()
        try:
            # 2500 -> 2400: なお2000字超だが減少しているので許可される
            conn.execute(
                "UPDATE habits SET content = ? WHERE id = ?", ("x" * 2400, habit_id)
            )
            conn.commit()
            row = conn.execute(
                "SELECT LENGTH(content) AS len FROM habits WHERE id = ?", (habit_id,)
            ).fetchone()
            assert row["len"] == 2400
        finally:
            conn.close()

    def test_importance_score_only_update_ignores_ceiling(self, db_before_0062):
        """importance_scoreのみの更新は、プールが2000字超過中でも成功する
        （UPDATE OF content, active, trigger_modeの対象外であることの確認）"""
        conn = get_connection()
        try:
            _neutralize_seed_always_pool(conn)
            habit_id = conn.execute(
                "INSERT INTO habits (content, trigger_mode, active) VALUES (?, 'always', 1)",
                ("x" * 2500,),
            ).lastrowid
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0062(db_before_0062)

        conn = get_connection()
        try:
            conn.execute(
                "UPDATE habits SET importance_score = 2 WHERE id = ?", (habit_id,)
            )
            conn.commit()
            row = conn.execute(
                "SELECT importance_score FROM habits WHERE id = ?", (habit_id,)
            ).fetchone()
            assert row["importance_score"] == 2
        finally:
            conn.close()

    def test_deactivation_is_always_allowed(self, db_before_0062):
        """active=1から0への無効化は、プールが2000字超過中でも常に許可される"""
        conn = get_connection()
        try:
            _neutralize_seed_always_pool(conn)
            habit_id = conn.execute(
                "INSERT INTO habits (content, trigger_mode, active) VALUES (?, 'always', 1)",
                ("x" * 2500,),
            ).lastrowid
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0062(db_before_0062)

        conn = get_connection()
        try:
            conn.execute("UPDATE habits SET active = 0 WHERE id = ?", (habit_id,))
            conn.commit()
            row = conn.execute(
                "SELECT active FROM habits WHERE id = ?", (habit_id,)
            ).fetchone()
            assert row["active"] == 0
        finally:
            conn.close()


class TestExistingDataUnaffectedByMigration:
    """migration適用自体が既存データを書き換えないことの確認"""

    def test_existing_habit_content_unchanged_after_migration(self, db_before_0062):
        conn = get_connection()
        try:
            _neutralize_seed_always_pool(conn)
            habit_id = conn.execute(
                "INSERT INTO habits (content, trigger_mode, active) VALUES (?, 'always', 1)",
                ("x" * 2500,),
            ).lastrowid
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0062(db_before_0062)

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT LENGTH(content) AS len, trigger_mode, active FROM habits WHERE id = ?",
                (habit_id,),
            ).fetchone()
            assert row["len"] == 2500
            assert row["trigger_mode"] == "always"
            assert row["active"] == 1
        finally:
            conn.close()
