"""migration 0046_relations_belongs_to_unify のテスト

0046 適用前後で以下が成立することを検証する:

- relations の CHECK 制約が 'related' + 'belongs_to' の両方を受け付ける
- partial index 2 本 (idx_relations_belongs_to_tgt / idx_relations_belongs_to_src) が貼られる
- 既存 decisions/discussion_logs の topic_id が relations.belongs_to に複製される
- 既存 material/activity → topic の 'related' 行が 'belongs_to' に変換される
- decisions/discussion_logs の topic_id 列が NULLABLE 化されている (0046 適用後)
- 関連トリガー (search_index 3 本 + CASCADE 3 本) が再作成されている

0047 で topic_id が物理削除されるため、本テストは「0046 までを適用した中間状態」を
yoyo MigrationList の部分適用パターンで再現して検証する。
"""
import os
import sqlite3
import tempfile

import pytest
from yoyo import default_migration_table, read_migrations
from yoyo.connections import parse_uri
from yoyo.migrations import MigrationList

from src.db import MIGRATIONS_DIR, _VecSQLiteBackend


@pytest.fixture
def db_before_0046():
    """0045 まで適用した DB を提供する (relations CHECK は 'related' 固定の状態)。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        parsed = parse_uri(f"sqlite:///{db_path}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        backend.init_database()
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        pre_0046 = MigrationList([m for m in all_migs if m.id < "0046"])
        with backend.lock():
            backend.apply_migrations(pre_0046)
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def db_up_to_0046():
    """0046 まで適用した DB を提供する (0047 未適用なので topic_id 列は NULLABLE で残置)。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        parsed = parse_uri(f"sqlite:///{db_path}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        backend.init_database()
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        up_to_0046 = MigrationList([m for m in all_migs if m.id < "0047"])
        with backend.lock():
            backend.apply_migrations(up_to_0046)
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


class TestSchemaChanges:
    """0046 適用後のスキーマ変更を検証"""

    def test_relations_check_allows_belongs_to(self, db_up_to_0046):
        """relations CHECK 制約が 'belongs_to' を受け付ける"""
        conn = sqlite3.connect(db_up_to_0046)
        try:
            # topic を 1 件挿入
            conn.execute("INSERT INTO discussion_topics (title, description) VALUES ('t', 'd')")
            topic_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            # decision を 1 件挿入 (topic_id 不要)
            conn.execute("INSERT INTO decisions (decision, reason) VALUES ('d', 'r')")
            dec_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            # belongs_to で relations 行を INSERT — CHECK を通れば成功
            conn.execute(
                "INSERT INTO relations (source_type, source_id, target_type, target_id, relation_type) "
                "VALUES ('decision', ?, 'topic', ?, 'belongs_to')",
                (dec_id, topic_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT relation_type FROM relations WHERE source_id = ?",
                (dec_id,),
            ).fetchone()
            assert row[0] == "belongs_to"
        finally:
            conn.close()

    def test_relations_check_rejects_unknown_type(self, db_up_to_0046):
        """relations CHECK 制約が 'related'/'belongs_to' 以外を拒否する。
        正規化制約 ('activity' < 'topic') はパスし、relation_type CHECK のみで失敗することを担保する。
        """
        conn = sqlite3.connect(db_up_to_0046)
        try:
            with pytest.raises(sqlite3.IntegrityError, match="relation_type"):
                conn.execute(
                    "INSERT INTO relations (source_type, source_id, target_type, target_id, relation_type) "
                    "VALUES ('activity', 1, 'topic', 2, 'depends_on')"
                )
        finally:
            conn.close()

    def test_partial_indexes_created(self, db_up_to_0046):
        """partial index 2 本が作成されている"""
        conn = sqlite3.connect(db_up_to_0046)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='relations' "
                "AND name IN ('idx_relations_belongs_to_tgt', 'idx_relations_belongs_to_src')"
            ).fetchall()
            names = {r[0] for r in rows}
            assert names == {"idx_relations_belongs_to_tgt", "idx_relations_belongs_to_src"}

            # WHERE 句に relation_type='belongs_to' が含まれていることを sql 文字列で確認
            for name in names:
                sql = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE name=?", (name,)
                ).fetchone()[0]
                assert "WHERE relation_type" in sql and "belongs_to" in sql
        finally:
            conn.close()

    def test_decisions_topic_id_nullable(self, db_up_to_0046):
        """0046 適用後 decisions.topic_id が NULLABLE になっている (NULL で INSERT 可)"""
        conn = sqlite3.connect(db_up_to_0046)
        try:
            # topic_id を指定せず INSERT 成功すれば NULLABLE
            conn.execute("INSERT INTO decisions (decision, reason) VALUES ('d', 'r')")
            conn.commit()
            row = conn.execute(
                "SELECT topic_id FROM decisions ORDER BY id DESC LIMIT 1"
            ).fetchone()
            assert row[0] is None
        finally:
            conn.close()

    def test_discussion_logs_topic_id_nullable(self, db_up_to_0046):
        """0046 適用後 discussion_logs.topic_id が NULLABLE になっている"""
        conn = sqlite3.connect(db_up_to_0046)
        try:
            conn.execute("INSERT INTO discussion_logs (title, content) VALUES ('t', 'c')")
            conn.commit()
            row = conn.execute(
                "SELECT topic_id FROM discussion_logs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            assert row[0] is None
        finally:
            conn.close()


class TestDataMigration:
    """既存データが正しく relations.belongs_to に移行されることを検証"""

    def test_decisions_topic_id_replicated_to_belongs_to(self, db_before_0046):
        """0046 適用前にあった decisions.topic_id が relations.belongs_to に複製される"""
        # まず 0045 までの状態で decision を作る
        conn = sqlite3.connect(db_before_0046)
        try:
            conn.execute("INSERT INTO discussion_topics (title, description) VALUES ('t', 'd')")
            topic_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO decisions (topic_id, decision, reason) VALUES (?, ?, ?)",
                (topic_id, "old decision", "old reason"),
            )
            dec_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.commit()
        finally:
            conn.close()

        # 0046 を適用
        parsed = parse_uri(f"sqlite:///{db_before_0046}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        only_0046 = MigrationList([m for m in all_migs if m.id.startswith("0046_")])
        with backend.lock():
            backend.apply_migrations(only_0046)

        # relations に belongs_to が登録されている
        conn = sqlite3.connect(db_before_0046)
        try:
            row = conn.execute(
                "SELECT relation_type FROM relations "
                "WHERE source_type='decision' AND source_id=? AND target_type='topic' AND target_id=?",
                (dec_id, topic_id),
            ).fetchone()
            assert row is not None
            assert row[0] == "belongs_to"
        finally:
            conn.close()

    def test_logs_topic_id_replicated_to_belongs_to(self, db_before_0046):
        """0046 適用前にあった discussion_logs.topic_id が relations.belongs_to に複製される"""
        conn = sqlite3.connect(db_before_0046)
        try:
            conn.execute("INSERT INTO discussion_topics (title, description) VALUES ('t', 'd')")
            topic_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO discussion_logs (topic_id, content) VALUES (?, ?)",
                (topic_id, "old log content"),
            )
            log_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.commit()
        finally:
            conn.close()

        parsed = parse_uri(f"sqlite:///{db_before_0046}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        only_0046 = MigrationList([m for m in all_migs if m.id.startswith("0046_")])
        with backend.lock():
            backend.apply_migrations(only_0046)

        conn = sqlite3.connect(db_before_0046)
        try:
            row = conn.execute(
                "SELECT relation_type FROM relations "
                "WHERE source_type='log' AND source_id=? AND target_type='topic' AND target_id=?",
                (log_id, topic_id),
            ).fetchone()
            assert row is not None
            assert row[0] == "belongs_to"
        finally:
            conn.close()

    def test_material_topic_related_converted_to_belongs_to(self, db_before_0046):
        """0046 適用前の material→topic 'related' が 'belongs_to' に変換される"""
        conn = sqlite3.connect(db_before_0046)
        try:
            conn.execute("INSERT INTO discussion_topics (title, description) VALUES ('t', 'd')")
            topic_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("INSERT INTO materials (title, content) VALUES ('m', 'c')")
            mat_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            # material < topic で正規化、'related' で書き込み
            conn.execute(
                "INSERT INTO relations (source_type, source_id, target_type, target_id, relation_type) "
                "VALUES ('material', ?, 'topic', ?, 'related')",
                (mat_id, topic_id),
            )
            conn.commit()
        finally:
            conn.close()

        parsed = parse_uri(f"sqlite:///{db_before_0046}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        only_0046 = MigrationList([m for m in all_migs if m.id.startswith("0046_")])
        with backend.lock():
            backend.apply_migrations(only_0046)

        conn = sqlite3.connect(db_before_0046)
        try:
            row = conn.execute(
                "SELECT relation_type FROM relations "
                "WHERE source_type='material' AND source_id=? AND target_type='topic' AND target_id=?",
                (mat_id, topic_id),
            ).fetchone()
            assert row is not None
            assert row[0] == "belongs_to"
        finally:
            conn.close()

    def test_activity_topic_related_converted_to_belongs_to(self, db_before_0046):
        """0046 適用前の activity→topic 'related' が 'belongs_to' に変換される"""
        conn = sqlite3.connect(db_before_0046)
        try:
            conn.execute("INSERT INTO discussion_topics (title, description) VALUES ('t', 'd')")
            topic_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO activities (title, description, status) VALUES ('a', 'd', 'pending')"
            )
            act_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO relations (source_type, source_id, target_type, target_id, relation_type) "
                "VALUES ('activity', ?, 'topic', ?, 'related')",
                (act_id, topic_id),
            )
            conn.commit()
        finally:
            conn.close()

        parsed = parse_uri(f"sqlite:///{db_before_0046}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        only_0046 = MigrationList([m for m in all_migs if m.id.startswith("0046_")])
        with backend.lock():
            backend.apply_migrations(only_0046)

        conn = sqlite3.connect(db_before_0046)
        try:
            row = conn.execute(
                "SELECT relation_type FROM relations "
                "WHERE source_type='activity' AND source_id=? AND target_type='topic' AND target_id=?",
                (act_id, topic_id),
            ).fetchone()
            assert row is not None
            assert row[0] == "belongs_to"
        finally:
            conn.close()

    def test_non_parent_related_rows_preserved(self, db_before_0046):
        """activity-material や activity-activity 等の非親帰属 'related' 行は据置される"""
        conn = sqlite3.connect(db_before_0046)
        try:
            conn.execute(
                "INSERT INTO activities (title, description, status) VALUES ('a1', 'd', 'pending')"
            )
            a1 = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO activities (title, description, status) VALUES ('a2', 'd', 'pending')"
            )
            a2 = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("INSERT INTO materials (title, content) VALUES ('m', 'c')")
            mat = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            # activity-activity 'related'
            conn.execute(
                "INSERT INTO relations (source_type, source_id, target_type, target_id, relation_type) "
                "VALUES ('activity', ?, 'activity', ?, 'related')",
                (min(a1, a2), max(a1, a2)),
            )
            # activity-material 'related'
            conn.execute(
                "INSERT INTO relations (source_type, source_id, target_type, target_id, relation_type) "
                "VALUES ('activity', ?, 'material', ?, 'related')",
                (a1, mat),
            )
            conn.commit()
        finally:
            conn.close()

        parsed = parse_uri(f"sqlite:///{db_before_0046}")
        backend = _VecSQLiteBackend(parsed, default_migration_table)
        all_migs = read_migrations(str(MIGRATIONS_DIR))
        only_0046 = MigrationList([m for m in all_migs if m.id.startswith("0046_")])
        with backend.lock():
            backend.apply_migrations(only_0046)

        conn = sqlite3.connect(db_before_0046)
        try:
            # どちらも 'related' のまま (target_type != 'topic' なので変換対象外)
            rows = conn.execute(
                "SELECT relation_type FROM relations "
                "WHERE (source_type='activity' AND target_type='activity') "
                "   OR (source_type='activity' AND target_type='material')"
            ).fetchall()
            assert len(rows) == 2
            assert all(r[0] == "related" for r in rows)
        finally:
            conn.close()


class TestTriggersRecreated:
    """テーブル再作成で削除されたトリガー群が再作成されていることを検証"""

    def test_decisions_search_index_trigger(self, db_up_to_0046):
        """decisions INSERT で search_index に行が入る (再作成 trigger 経由)"""
        conn = sqlite3.connect(db_up_to_0046)
        try:
            conn.execute("INSERT INTO decisions (decision, reason, title) VALUES ('d', 'r', 't')")
            dec_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            row = conn.execute(
                "SELECT title FROM search_index WHERE source_type='decision' AND source_id=?",
                (dec_id,),
            ).fetchone()
            assert row is not None
            assert row[0] == "t"
        finally:
            conn.close()

    def test_decisions_cascade_trigger_relations(self, db_up_to_0046):
        """decisions DELETE で relations が cascade される"""
        conn = sqlite3.connect(db_up_to_0046)
        try:
            conn.execute("INSERT INTO discussion_topics (title, description) VALUES ('t', 'd')")
            topic_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("INSERT INTO decisions (decision, reason) VALUES ('d', 'r')")
            dec_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO relations (source_type, source_id, target_type, target_id, relation_type) "
                "VALUES ('decision', ?, 'topic', ?, 'belongs_to')",
                (dec_id, topic_id),
            )
            conn.execute("DELETE FROM decisions WHERE id = ?", (dec_id,))
            row = conn.execute(
                "SELECT COUNT(*) FROM relations WHERE source_type='decision' AND source_id=?",
                (dec_id,),
            ).fetchone()
            assert row[0] == 0
        finally:
            conn.close()

    def test_logs_search_index_trigger(self, db_up_to_0046):
        """discussion_logs INSERT で search_index に行が入る"""
        conn = sqlite3.connect(db_up_to_0046)
        try:
            conn.execute("INSERT INTO discussion_logs (title, content) VALUES ('lt', 'lc')")
            log_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            row = conn.execute(
                "SELECT title FROM search_index WHERE source_type='log' AND source_id=?",
                (log_id,),
            ).fetchone()
            assert row is not None
            assert row[0] == "lt"
        finally:
            conn.close()

    def test_logs_cascade_trigger_relations(self, db_up_to_0046):
        """discussion_logs DELETE で relations が cascade される"""
        conn = sqlite3.connect(db_up_to_0046)
        try:
            conn.execute("INSERT INTO discussion_topics (title, description) VALUES ('t', 'd')")
            topic_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("INSERT INTO discussion_logs (content) VALUES ('lc')")
            log_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO relations (source_type, source_id, target_type, target_id, relation_type) "
                "VALUES ('log', ?, 'topic', ?, 'belongs_to')",
                (log_id, topic_id),
            )
            conn.execute("DELETE FROM discussion_logs WHERE id = ?", (log_id,))
            row = conn.execute(
                "SELECT COUNT(*) FROM relations WHERE source_type='log' AND source_id=?",
                (log_id,),
            ).fetchone()
            assert row[0] == 0
        finally:
            conn.close()


class TestMigration0047:
    """0047 適用後、topic_id 列が物理削除されることを検証"""

    def test_decisions_topic_id_dropped(self):
        """0047 適用後、decisions.topic_id 列が存在しない"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            os.environ["DISCUSSION_DB_PATH"] = db_path
            try:
                parsed = parse_uri(f"sqlite:///{db_path}")
                backend = _VecSQLiteBackend(parsed, default_migration_table)
                backend.init_database()
                all_migs = read_migrations(str(MIGRATIONS_DIR))
                with backend.lock():
                    backend.apply_migrations(all_migs)

                conn = sqlite3.connect(db_path)
                try:
                    cols = [r[1] for r in conn.execute("PRAGMA table_info(decisions)").fetchall()]
                    assert "topic_id" not in cols
                    cols = [r[1] for r in conn.execute("PRAGMA table_info(discussion_logs)").fetchall()]
                    assert "topic_id" not in cols
                finally:
                    conn.close()
            finally:
                if "DISCUSSION_DB_PATH" in os.environ:
                    del os.environ["DISCUSSION_DB_PATH"]
