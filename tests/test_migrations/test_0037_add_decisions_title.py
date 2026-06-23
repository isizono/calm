"""migration 0037_add_decisions_title のテスト

0037適用後に decisions テーブルへ title列が追加されること、
search_index投入トリガーが title優先（COALESCE(title, decision)）の
display titleを格納するようになること、
FTSインデックス（マッチ用）は decision本文/reason のまま不変であることを確認する。
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
    """全migration（0037含む）を適用済みのテスト用DBを提供する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def db_before_0037():
    """0036までのmigrationを適用したDBを提供する。0037の挙動を分離検証するために使う。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path

        parsed = parse_uri(f"sqlite:///{db_path}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        backend.init_database()
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        pre_0037 = MigrationList([m for m in all_migs if m.id < "0037"])
        with backend.lock():
            backend.apply_migrations(pre_0037)

        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def migrated_db_up_to_0037():
    """0037までのmigrationを適用したDBを提供する。

    後続 migration（0046/0047 等）で decisions.topic_id が物理削除されるため、
    0037 直後の「title 列追加と search_index トリガが topic_id 前提で動く」状態を
    検証するテストはここを使う。
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path

        parsed = parse_uri(f"sqlite:///{db_path}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        backend.init_database()
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        up_to_0037 = MigrationList([m for m in all_migs if m.id < "0038"])
        with backend.lock():
            backend.apply_migrations(up_to_0037)

        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


def _apply_migration_0037(db_path: str) -> None:
    """db_pathに対してmigration 0037のみを適用する。"""
    parsed = parse_uri(f"sqlite:///{db_path}")
    backend = _VecSQLiteBackend(parsed, default_migration_table)
    all_migs = read_migrations(str(MIGRATIONS_DIR))
    only_0037 = MigrationList([m for m in all_migs if m.id.startswith("0037")])
    with backend.lock():
        backend.apply_migrations(only_0037)


def _get_column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    """指定テーブルのカラム名セットを返す。"""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


def _insert_topic(conn: sqlite3.Connection) -> int:
    """テスト用トピックを1件INSERTしてIDを返す。"""
    cur = conn.execute(
        "INSERT INTO discussion_topics (title, description) VALUES (?, ?)",
        ("テストトピック", "説明"),
    )
    return cur.lastrowid


class TestTitleColumnAdded:
    """0037適用後にtitle列が追加されていることの確認"""

    def test_decisions_has_title_column_after_0037(self, migrated_db):
        """migration 0037適用後、decisionsテーブルにtitle列が存在する"""
        conn = get_connection()
        try:
            assert "title" in _get_column_names(conn, "decisions"), (
                "decisions.title が0037適用後に存在しない"
            )
        finally:
            conn.close()

    def test_decisions_has_no_title_column_before_0037(self, db_before_0037):
        """0036適用時点では decisions に title列が存在しない（前提確認）"""
        conn = get_connection()
        try:
            assert "title" not in _get_column_names(conn, "decisions"), (
                "0037適用前のdecisionsにtitle列が既に存在している"
            )
        finally:
            conn.close()

    def test_other_columns_intact_after_0037(self, migrated_db_up_to_0037):
        """0037適用後、decisionsのid/topic_id/decision/reason/created_at/retracted_atが保持される"""
        conn = get_connection()
        try:
            column_names = _get_column_names(conn, "decisions")
            for col in ["id", "topic_id", "decision", "reason", "created_at", "retracted_at"]:
                assert col in column_names, (
                    f"decisions.{col} が0037適用後に消えている"
                )
        finally:
            conn.close()

    def test_existing_rows_have_null_title(self, db_before_0037):
        """0036時点のdecision行は、0037適用後にtitleがNULLのまま残る"""
        conn = get_connection()
        try:
            tid = _insert_topic(conn)
            conn.execute(
                "INSERT INTO decisions (topic_id, decision, reason) VALUES (?, ?, ?)",
                (tid, "既存決定", "既存理由"),
            )
            conn.commit()
        finally:
            conn.close()

        _apply_migration_0037(db_before_0037)

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT title FROM decisions WHERE decision='既存決定'"
            ).fetchone()
            assert row is not None, "対象のdecision行が見つからない"
            assert row["title"] is None, (
                f"0037適用後、既存decisionのtitleがNULLでない: {row['title']!r}"
            )
        finally:
            conn.close()


class TestSearchIndexTitleFallback:
    """search_index投入トリガーが title優先のdisplay titleを格納する確認"""

    def test_insert_decision_with_title_indexes_title(self, migrated_db_up_to_0037):
        """titleを指定したdecisionのsearch_index.titleがtitleになる"""
        conn = get_connection()
        try:
            tid = _insert_topic(conn)
            conn.execute(
                "INSERT INTO decisions (topic_id, decision, reason, title) VALUES (?, ?, ?, ?)",
                (tid, "長い決定本文がここに入る", "理由", "要点1行"),
            )
            conn.commit()

            row = conn.execute(
                """
                SELECT si.title FROM search_index si
                JOIN decisions d ON d.id = si.source_id
                WHERE si.source_type = 'decision' AND d.decision = '長い決定本文がここに入る'
                """
            ).fetchone()
            assert row is not None, "search_indexにdecision行が無い"
            assert row["title"] == "要点1行", (
                f"search_index.title が title になっていない: {row['title']!r}"
            )
        finally:
            conn.close()

    def test_insert_decision_without_title_falls_back_to_decision(self, migrated_db_up_to_0037):
        """title未指定のdecisionのsearch_index.titleがdecision本文にfallbackする"""
        conn = get_connection()
        try:
            tid = _insert_topic(conn)
            conn.execute(
                "INSERT INTO decisions (topic_id, decision, reason) VALUES (?, ?, ?)",
                (tid, "titleなし決定", "理由"),
            )
            conn.commit()

            row = conn.execute(
                """
                SELECT si.title FROM search_index si
                JOIN decisions d ON d.id = si.source_id
                WHERE si.source_type = 'decision' AND d.decision = 'titleなし決定'
                """
            ).fetchone()
            assert row is not None
            assert row["title"] == "titleなし決定", (
                f"title未指定時にdecision本文へfallbackしていない: {row['title']!r}"
            )
        finally:
            conn.close()

    def test_update_decision_title_updates_search_index(self, migrated_db_up_to_0037):
        """decisionのtitleを後からUPDATEするとsearch_index.titleも追従する"""
        conn = get_connection()
        try:
            tid = _insert_topic(conn)
            cur = conn.execute(
                "INSERT INTO decisions (topic_id, decision, reason) VALUES (?, ?, ?)",
                (tid, "更新前決定", "理由"),
            )
            did = cur.lastrowid
            conn.commit()

            conn.execute(
                "UPDATE decisions SET title = ? WHERE id = ?",
                ("更新後の要点", did),
            )
            conn.commit()

            row = conn.execute(
                "SELECT title FROM search_index WHERE source_type = 'decision' AND source_id = ?",
                (did,),
            ).fetchone()
            assert row is not None
            assert row["title"] == "更新後の要点", (
                f"UPDATE後にsearch_index.titleが追従していない: {row['title']!r}"
            )
        finally:
            conn.close()

    def test_fts_body_still_matches_decision_text(self, migrated_db_up_to_0037):
        """title指定decisionでも、FTSは decision本文/reason で全文検索できる（マッチ用indexは不変）"""
        conn = get_connection()
        try:
            tid = _insert_topic(conn)
            conn.execute(
                "INSERT INTO decisions (topic_id, decision, reason, title) VALUES (?, ?, ?, ?)",
                (tid, "ユニークキーワードzephyrを含む決定本文", "ユニーク理由quasar", "短い要点"),
            )
            conn.commit()

            # decision本文中の語でFTSヒットする（title欄ではなくdecision本文がFTS title欄に入っている）
            hit_decision = conn.execute(
                """
                SELECT si.source_id FROM search_index_fts
                JOIN search_index si ON si.id = search_index_fts.rowid
                WHERE search_index_fts MATCH 'zephyr' AND si.source_type = 'decision'
                """
            ).fetchall()
            assert len(hit_decision) == 1, "decision本文の語でFTSヒットしない（FTS titleが壊れている）"

            # reason中の語でFTSヒットする（FTS body）
            hit_reason = conn.execute(
                """
                SELECT si.source_id FROM search_index_fts
                JOIN search_index si ON si.id = search_index_fts.rowid
                WHERE search_index_fts MATCH 'quasar' AND si.source_type = 'decision'
                """
            ).fetchall()
            assert len(hit_reason) == 1, "reasonの語でFTSヒットしない（FTS bodyが壊れている）"
        finally:
            conn.close()
