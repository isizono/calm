"""migration 0063_add_decision_supersedes_kind のテスト

0063適用後に decision_supersedes へ kind 列（NOT NULL DEFAULT 'replaces',
CHECK IN ('replaces', 'destabilizes')）が追加され、既存行が全て kind='replaces'
として複製されること、decision_destabilization_resolutions テーブルが新設され
PRIMARY KEY (source_id, target_id) と CASCADE 削除が機能すること、relations_view
が decision_supersedes.kind に応じて relation_type を 'supersedes'/'destabilizes'
に出し分けることを確認する。
"""

import os
import sqlite3
import tempfile

import pytest
from yoyo import default_migration_table, read_migrations
from yoyo.connections import parse_uri
from yoyo.migrations import MigrationList

from src.db import MIGRATIONS_DIR, _VecSQLiteBackend, get_connection, init_database
from src.services.tag_service import _injected_tags


@pytest.fixture
def migrated_db():
    """全migration（0063含む）を適用済みのテスト用DBを提供する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
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

        _injected_tags.clear()
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


def _get_column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    """指定テーブルのカラム名セットを返す。"""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


def _insert_decision(conn: sqlite3.Connection, decision: str, reason: str = "理由") -> int:
    """decisionsに1行INSERTしてidを返す。"""
    cur = conn.execute(
        "INSERT INTO decisions (decision, reason) VALUES (?, ?)",
        (decision, reason),
    )
    return cur.lastrowid


class TestKindColumnAdded:
    """0063適用後にdecision_supersedes.kind列が仕様通りに追加されていることの確認"""

    def test_kind_column_not_null_with_default(self, migrated_db):
        """migration 0063適用後、decision_supersedesにkind列が存在し、既定値がreplacesになる"""
        conn = get_connection()
        try:
            column_names = _get_column_names(conn, "decision_supersedes")
            assert "kind" in column_names, "decision_supersedes.kind が 0063 適用後に存在しない"

            source_id = _insert_decision(conn, "決定A")
            target_id = _insert_decision(conn, "決定B")
            conn.execute(
                "INSERT INTO decision_supersedes (source_id, target_id) VALUES (?, ?)",
                (source_id, target_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT kind FROM decision_supersedes WHERE source_id = ? AND target_id = ?",
                (source_id, target_id),
            ).fetchone()
            assert row["kind"] == "replaces", "kind列を省略したINSERTの既定値がreplacesでない"
        finally:
            conn.close()

    def test_kind_column_absent_before_0063(self, db_before_0063):
        """0062適用時点ではkind列が存在しない（前提確認）"""
        conn = get_connection()
        try:
            column_names = _get_column_names(conn, "decision_supersedes")
            assert "kind" not in column_names, (
                "0063 適用前の decision_supersedes に kind 列が既に存在している"
            )
        finally:
            conn.close()

    def test_kind_check_constraint_rejects_invalid_value(self, migrated_db):
        """kindのCHECK制約が'replaces'/'destabilizes'以外を拒否する"""
        conn = get_connection()
        try:
            source_id = _insert_decision(conn, "決定A")
            target_id = _insert_decision(conn, "決定B")
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO decision_supersedes (source_id, target_id, kind) VALUES (?, ?, ?)",
                    (source_id, target_id, "invalid_kind"),
                )
        finally:
            conn.rollback()
            conn.close()

    def test_same_pair_allows_both_replaces_and_destabilizes(self, migrated_db):
        """同一(source_id, target_id)ペアにreplacesとdestabilizesの両方をINSERTできる（PKにkindを含む）"""
        conn = get_connection()
        try:
            source_id = _insert_decision(conn, "決定A")
            target_id = _insert_decision(conn, "決定B")
            conn.execute(
                "INSERT INTO decision_supersedes (source_id, target_id, kind) VALUES (?, ?, ?)",
                (source_id, target_id, "replaces"),
            )
            conn.execute(
                "INSERT INTO decision_supersedes (source_id, target_id, kind) VALUES (?, ?, ?)",
                (source_id, target_id, "destabilizes"),
            )
            conn.commit()
            rows = conn.execute(
                "SELECT kind FROM decision_supersedes WHERE source_id = ? AND target_id = ? ORDER BY kind",
                (source_id, target_id),
            ).fetchall()
            assert [r["kind"] for r in rows] == ["destabilizes", "replaces"]
        finally:
            conn.close()


class TestNoDataMutationBeyondKind:
    """0063が既存supersede行をreplacesとして保全し、他の値を書き換えないことの確認"""

    def test_existing_rows_become_replaces_after_0063(self, db_before_0063):
        """0062時点で存在するdecision_supersedes行は、0063適用後すべてkind='replaces'になる"""
        conn = get_connection()
        try:
            pairs = []
            for i in range(3):
                source_id = _insert_decision(conn, f"新決定{i}")
                target_id = _insert_decision(conn, f"旧決定{i}")
                conn.execute(
                    "INSERT INTO decision_supersedes (source_id, target_id) VALUES (?, ?)",
                    (source_id, target_id),
                )
                pairs.append((source_id, target_id))
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0063(db_before_0063)

        conn = get_connection()
        try:
            rows = conn.execute("SELECT source_id, target_id, kind FROM decision_supersedes").fetchall()
            assert len(rows) == len(pairs)
            got = {(r["source_id"], r["target_id"]): r["kind"] for r in rows}
            for source_id, target_id in pairs:
                assert got[(source_id, target_id)] == "replaces", (
                    f"({source_id}, {target_id}) が 0063 適用後 kind='replaces' になっていない"
                )
        finally:
            conn.close()


class TestDestabilizationResolutions:
    """decision_destabilization_resolutionsテーブルの制約確認"""

    def test_destabilization_resolutions_pk_rejects_duplicate(self, migrated_db):
        """同一(source_id, target_id)の2回目のINSERTが失敗する"""
        conn = get_connection()
        try:
            source_id = _insert_decision(conn, "決定A")
            target_id = _insert_decision(conn, "決定B")
            conn.execute(
                "INSERT INTO decision_destabilization_resolutions "
                "(source_id, target_id, resolution) VALUES (?, ?, ?)",
                (source_id, target_id, "reaffirmed"),
            )
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO decision_destabilization_resolutions "
                    "(source_id, target_id, resolution) VALUES (?, ?, ?)",
                    (source_id, target_id, "retracted"),
                )
        finally:
            conn.rollback()
            conn.close()

    def test_resolution_check_constraint_rejects_invalid_value(self, migrated_db):
        """resolutionのCHECK制約が'reaffirmed'/'revised'/'retracted'以外を拒否する"""
        conn = get_connection()
        try:
            source_id = _insert_decision(conn, "決定A")
            target_id = _insert_decision(conn, "決定B")
            conn.commit()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO decision_destabilization_resolutions "
                    "(source_id, target_id, resolution) VALUES (?, ?, ?)",
                    (source_id, target_id, "invalid_resolution"),
                )
        finally:
            conn.rollback()
            conn.close()

    def test_decision_delete_cascades_to_resolutions(self, migrated_db):
        """decisions行を削除すると対応するdecision_destabilization_resolutions行も消える"""
        conn = get_connection()
        try:
            source_id = _insert_decision(conn, "決定A")
            target_id = _insert_decision(conn, "決定B")
            conn.execute(
                "INSERT INTO decision_destabilization_resolutions "
                "(source_id, target_id, resolution) VALUES (?, ?, ?)",
                (source_id, target_id, "reaffirmed"),
            )
            conn.commit()

            conn.execute("DELETE FROM decisions WHERE id = ?", (target_id,))
            conn.commit()

            row = conn.execute(
                "SELECT 1 FROM decision_destabilization_resolutions "
                "WHERE source_id = ? AND target_id = ?",
                (source_id, target_id),
            ).fetchone()
            assert row is None, "target decision削除後もresolution行が残留している"
        finally:
            conn.close()


class TestRelationsView:
    """relations_viewがdecision_supersedes.kindに応じてrelation_typeを出し分けることの確認"""

    def test_relations_view_shows_supersedes_edge(self, migrated_db):
        """kind='replaces'の行はrelations_viewでrelation_type='supersedes'として見える"""
        conn = get_connection()
        try:
            source_id = _insert_decision(conn, "決定A")
            target_id = _insert_decision(conn, "決定B")
            conn.execute(
                "INSERT INTO decision_supersedes (source_id, target_id, kind) VALUES (?, ?, ?)",
                (source_id, target_id, "replaces"),
            )
            conn.commit()
            row = conn.execute(
                "SELECT relation_type FROM relations_view "
                "WHERE source_type = 'decision' AND source_id = ? "
                "AND target_type = 'decision' AND target_id = ?",
                (source_id, target_id),
            ).fetchone()
            assert row is not None
            assert row["relation_type"] == "supersedes"
        finally:
            conn.close()

    def test_relations_view_shows_destabilizes_edge(self, migrated_db):
        """kind='destabilizes'の行はrelations_viewでrelation_type='destabilizes'として見える"""
        conn = get_connection()
        try:
            source_id = _insert_decision(conn, "決定A")
            target_id = _insert_decision(conn, "決定B")
            conn.execute(
                "INSERT INTO decision_supersedes (source_id, target_id, kind) VALUES (?, ?, ?)",
                (source_id, target_id, "destabilizes"),
            )
            conn.commit()
            row = conn.execute(
                "SELECT relation_type FROM relations_view "
                "WHERE source_type = 'decision' AND source_id = ? "
                "AND target_type = 'decision' AND target_id = ?",
                (source_id, target_id),
            ).fetchone()
            assert row is not None
            assert row["relation_type"] == "destabilizes"
        finally:
            conn.close()
