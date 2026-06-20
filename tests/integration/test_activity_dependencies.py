"""activity_dependenciesテーブルとrelations_viewのrelation_type列のテスト"""

import os
import sqlite3
import tempfile

import pytest

from src.db import get_connection, init_database
from src.services.tag_service import _injected_tags, ensure_tag_ids, link_tags


DEFAULT_TAGS = [("domain", "test")]


@pytest.fixture
def temp_db():
    """テスト用の一時的なデータベースを作成する"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


def _create_activity(conn, title="Test Activity"):
    """テスト用アクティビティを作成する"""
    cursor = conn.execute(
        "INSERT INTO activities (title, description, status) VALUES (?, ?, ?)",
        (title, f"Description for {title}", "pending"),
    )
    activity_id = cursor.lastrowid
    tag_ids = ensure_tag_ids(conn, DEFAULT_TAGS)
    link_tags(conn, "activity_tags", "activity_id", activity_id, tag_ids)
    return activity_id


class TestActivityDependenciesTable:
    """activity_dependenciesテーブルの存在と制約のテスト"""

    def test_table_exists(self, temp_db):
        """activity_dependenciesテーブルが作成されている"""
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='activity_dependencies'"
            ).fetchone()
            assert row is not None
        finally:
            conn.close()

    def test_insert_dependency(self, temp_db):
        """依存関係をINSERTできる"""
        conn = get_connection()
        try:
            a1 = _create_activity(conn, "Activity A")
            a2 = _create_activity(conn, "Activity B")
            conn.execute(
                "INSERT INTO activity_dependencies (dependent_id, dependency_id) VALUES (?, ?)",
                (a1, a2),
            )
            conn.commit()

            row = conn.execute(
                "SELECT * FROM activity_dependencies WHERE dependent_id = ? AND dependency_id = ?",
                (a1, a2),
            ).fetchone()
            assert row is not None
        finally:
            conn.close()

    def test_check_self_dependency_rejected(self, temp_db):
        """CHECK制約: 自己依存がINSERT時に弾かれる"""
        conn = get_connection()
        try:
            a1 = _create_activity(conn, "Activity A")
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO activity_dependencies (dependent_id, dependency_id) VALUES (?, ?)",
                    (a1, a1),
                )
        finally:
            conn.close()

    def test_pk_duplicate_rejected(self, temp_db):
        """PK制約: 同一ペアの重複INSERTが弾かれる"""
        conn = get_connection()
        try:
            a1 = _create_activity(conn, "Activity A")
            a2 = _create_activity(conn, "Activity B")
            conn.execute(
                "INSERT INTO activity_dependencies (dependent_id, dependency_id) VALUES (?, ?)",
                (a1, a2),
            )
            conn.commit()

            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO activity_dependencies (dependent_id, dependency_id) VALUES (?, ?)",
                    (a1, a2),
                )
        finally:
            conn.close()

    def test_cascade_delete_dependent(self, temp_db):
        """ON DELETE CASCADE: dependent側アクティビティ削除時に依存関係も削除される"""
        conn = get_connection()
        try:
            a1 = _create_activity(conn, "Activity A")
            a2 = _create_activity(conn, "Activity B")
            conn.execute(
                "INSERT INTO activity_dependencies (dependent_id, dependency_id) VALUES (?, ?)",
                (a1, a2),
            )
            conn.commit()

            conn.execute("DELETE FROM activities WHERE id = ?", (a1,))
            conn.commit()

            count = conn.execute(
                "SELECT COUNT(*) as cnt FROM activity_dependencies WHERE dependent_id = ?",
                (a1,),
            ).fetchone()["cnt"]
            assert count == 0
        finally:
            conn.close()

    def test_cascade_delete_dependency(self, temp_db):
        """ON DELETE CASCADE: dependency側アクティビティ削除時に依存関係も削除される"""
        conn = get_connection()
        try:
            a1 = _create_activity(conn, "Activity A")
            a2 = _create_activity(conn, "Activity B")
            conn.execute(
                "INSERT INTO activity_dependencies (dependent_id, dependency_id) VALUES (?, ?)",
                (a1, a2),
            )
            conn.commit()

            conn.execute("DELETE FROM activities WHERE id = ?", (a2,))
            conn.commit()

            count = conn.execute(
                "SELECT COUNT(*) as cnt FROM activity_dependencies WHERE dependency_id = ?",
                (a2,),
            ).fetchone()["cnt"]
            assert count == 0
        finally:
            conn.close()


class TestRelationsViewRelationType:
    """relations_viewのrelation_type列のテスト"""

    def test_relations_view_has_relation_type_column(self, temp_db):
        """relations_viewにrelation_type列が存在する"""
        conn = get_connection()
        try:
            # VIEWのカラム情報を取得
            cursor = conn.execute("PRAGMA table_info(relations_view)")
            columns = [row["name"] for row in cursor.fetchall()]
            assert "relation_type" in columns
        finally:
            conn.close()

    def test_existing_relations_have_related_type(self, temp_db):
        """既存のリレーション（relationsテーブル）はrelation_type='related'"""
        from src.services.relation_service import add_relation

        conn = get_connection()
        try:
            # トピックを作成してリレーションを追加
            cursor = conn.execute(
                "INSERT INTO discussion_topics (title, description) VALUES (?, ?)",
                ("Topic A", "Desc A"),
            )
            t1 = cursor.lastrowid
            tag_ids = ensure_tag_ids(conn, DEFAULT_TAGS)
            link_tags(conn, "topic_tags", "topic_id", t1, tag_ids)

            cursor = conn.execute(
                "INSERT INTO discussion_topics (title, description) VALUES (?, ?)",
                ("Topic B", "Desc B"),
            )
            t2 = cursor.lastrowid
            link_tags(conn, "topic_tags", "topic_id", t2, tag_ids)
            conn.commit()
        finally:
            conn.close()

        add_relation("topic", t1, [{"type": "topic", "ids": [t2]}])

        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT relation_type FROM relations_view WHERE source_type = 'topic' AND target_type = 'topic'"
            ).fetchall()
            assert len(rows) > 0
            for row in rows:
                assert row["relation_type"] == "related"
        finally:
            conn.close()

    def test_dependency_has_depends_on_type(self, temp_db):
        """activity_dependenciesからの行はrelation_type='depends_on'"""
        conn = get_connection()
        try:
            a1 = _create_activity(conn, "Activity A")
            a2 = _create_activity(conn, "Activity B")
            conn.execute(
                "INSERT INTO activity_dependencies (dependent_id, dependency_id) VALUES (?, ?)",
                (a1, a2),
            )
            conn.commit()

            rows = conn.execute(
                "SELECT * FROM relations_view WHERE relation_type = 'depends_on'"
            ).fetchall()
            assert len(rows) == 1
            assert rows[0]["source_id"] == a1
            assert rows[0]["source_type"] == "activity"
            assert rows[0]["target_id"] == a2
            assert rows[0]["target_type"] == "activity"
            assert rows[0]["relation_type"] == "depends_on"
        finally:
            conn.close()

    def test_depends_on_is_unidirectional(self, temp_db):
        """depends_on行はdependent→dependencyの1方向のみ（双方向化されない）"""
        conn = get_connection()
        try:
            a1 = _create_activity(conn, "Activity A")
            a2 = _create_activity(conn, "Activity B")
            conn.execute(
                "INSERT INTO activity_dependencies (dependent_id, dependency_id) VALUES (?, ?)",
                (a1, a2),
            )
            conn.commit()

            # depends_on行は1つだけ（a1→a2）、逆方向（a2→a1）は存在しない
            rows = conn.execute(
                "SELECT * FROM relations_view WHERE relation_type = 'depends_on'"
            ).fetchall()
            assert len(rows) == 1
            assert rows[0]["source_id"] == a1
            assert rows[0]["target_id"] == a2

            # 逆方向を確認
            reverse = conn.execute(
                "SELECT * FROM relations_view WHERE relation_type = 'depends_on' AND source_id = ? AND target_id = ?",
                (a2, a1),
            ).fetchall()
            assert len(reverse) == 0
        finally:
            conn.close()

    def test_existing_view_queries_not_broken(self, temp_db):
        """既存のrelations_view経由のクエリ（relation_typeを指定しない）が壊れない"""
        from src.services.relation_service import add_relation

        conn = get_connection()
        try:
            a1 = _create_activity(conn, "Activity A")
            a2 = _create_activity(conn, "Activity B")
            a3 = _create_activity(conn, "Activity C")
            conn.commit()
        finally:
            conn.close()

        # relationsテーブルを使うrelated関係
        add_relation("activity", a1, [{"type": "activity", "ids": [a2]}])

        # activity_dependenciesを使うdepends_on関係
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO activity_dependencies (dependent_id, dependency_id) VALUES (?, ?)",
                (a1, a3),
            )
            conn.commit()

            # relation_typeを指定しないクエリで全行が返る
            rows = conn.execute(
                "SELECT source_id, source_type, target_id, target_type, created_at "
                "FROM relations_view WHERE source_id = ? AND source_type = 'activity'",
                (a1,),
            ).fetchall()
            # a1→a2 (related, 双方向), a1→a3 (depends_on) の少なくとも2行
            target_ids = {row["target_id"] for row in rows}
            assert a2 in target_ids
            assert a3 in target_ids
        finally:
            conn.close()


class TestGetMapWithDependencies:
    """get_mapがdepends_on関係を含むシナリオのテスト"""

    def test_get_map_includes_depends_on_target(self, temp_db):
        """get_mapがdepends_on先のアクティビティを返す"""
        from src.services.relation_service import get_map

        conn = get_connection()
        try:
            a1 = _create_activity(conn, "Dependent")
            a2 = _create_activity(conn, "Dependency")
            conn.execute(
                "INSERT INTO activity_dependencies (dependent_id, dependency_id) VALUES (?, ?)",
                (a1, a2),
            )
            conn.commit()
        finally:
            conn.close()

        result = get_map("activity", a1, min_depth=1, max_depth=1)
        assert "entities" in result
        entity_ids = {(e["type"], e["id_raw"]) for e in result["entities"]}
        assert ("activity", a2) in entity_ids

    def test_get_map_depends_on_not_reverse(self, temp_db):
        """get_mapでdependency側から起点にした場合、dependent側は返らない（非対称）"""
        from src.services.relation_service import get_map

        conn = get_connection()
        try:
            a1 = _create_activity(conn, "Dependent")
            a2 = _create_activity(conn, "Dependency")
            conn.execute(
                "INSERT INTO activity_dependencies (dependent_id, dependency_id) VALUES (?, ?)",
                (a1, a2),
            )
            conn.commit()
        finally:
            conn.close()

        result = get_map("activity", a2, min_depth=1, max_depth=1)
        entity_ids = {(e["type"], e["id_raw"]) for e in result["entities"]}
        # a2→a1 方向のdepends_onはviewに存在しないため、a1は返らない
        assert ("activity", a1) not in entity_ids

    def test_get_map_mixed_related_and_depends_on(self, temp_db):
        """get_mapがrelated関係とdepends_on関係の両方を含む場合に正しく返す"""
        from src.services.relation_service import add_relation, get_map

        conn = get_connection()
        try:
            a1 = _create_activity(conn, "Main")
            a2 = _create_activity(conn, "Related Peer")
            a3 = _create_activity(conn, "Dependency")
            conn.commit()
        finally:
            conn.close()

        # a1 ←related→ a2
        add_relation("activity", a1, [{"type": "activity", "ids": [a2]}])

        # a1 →depends_on→ a3
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO activity_dependencies (dependent_id, dependency_id) VALUES (?, ?)",
                (a1, a3),
            )
            conn.commit()
        finally:
            conn.close()

        result = get_map("activity", a1, min_depth=1, max_depth=1)
        entity_ids = {(e["type"], e["id_raw"]) for e in result["entities"]}
        assert ("activity", a2) in entity_ids
        assert ("activity", a3) in entity_ids


class TestCyclicDependencyDetection:
    """add_relation(relation_type="depends_on")での循環依存検出テスト"""

    def test_direct_cycle_rejected(self, temp_db):
        """直接循環 (A→B, B→A) がサービス層で弾かれる"""
        from src.services.relation_service import add_relation

        conn = get_connection()
        try:
            a1 = _create_activity(conn, "Activity A")
            a2 = _create_activity(conn, "Activity B")
            conn.commit()
        finally:
            conn.close()

        # A→B は成功
        result = add_relation("activity", a1, [{"type": "activity", "ids": [a2]}], relation_type="depends_on")
        assert "error" not in result
        assert result["added"] == 1

        # B→A は循環になるため拒否
        result = add_relation("activity", a2, [{"type": "activity", "ids": [a1]}], relation_type="depends_on")
        assert "error" in result
        assert result["error"]["code"] == "CIRCULAR_DEPENDENCY"

    def test_transitive_cycle_rejected(self, temp_db):
        """推移的循環 (A→B, B→C, C→A) がサービス層で弾かれる"""
        from src.services.relation_service import add_relation

        conn = get_connection()
        try:
            a1 = _create_activity(conn, "Activity A")
            a2 = _create_activity(conn, "Activity B")
            a3 = _create_activity(conn, "Activity C")
            conn.commit()
        finally:
            conn.close()

        # A→B 成功
        result = add_relation("activity", a1, [{"type": "activity", "ids": [a2]}], relation_type="depends_on")
        assert "error" not in result

        # B→C 成功
        result = add_relation("activity", a2, [{"type": "activity", "ids": [a3]}], relation_type="depends_on")
        assert "error" not in result

        # C→A は推移的循環になるため拒否
        result = add_relation("activity", a3, [{"type": "activity", "ids": [a1]}], relation_type="depends_on")
        assert "error" in result
        assert result["error"]["code"] == "CIRCULAR_DEPENDENCY"

    def test_non_cyclic_chain_allowed(self, temp_db):
        """非循環の依存チェーン (A→B, B→C) は通る"""
        from src.services.relation_service import add_relation

        conn = get_connection()
        try:
            a1 = _create_activity(conn, "Activity A")
            a2 = _create_activity(conn, "Activity B")
            a3 = _create_activity(conn, "Activity C")
            conn.commit()
        finally:
            conn.close()

        result = add_relation("activity", a1, [{"type": "activity", "ids": [a2]}], relation_type="depends_on")
        assert "error" not in result
        assert result["added"] == 1

        result = add_relation("activity", a2, [{"type": "activity", "ids": [a3]}], relation_type="depends_on")
        assert "error" not in result
        assert result["added"] == 1

    def test_self_reference_skipped(self, temp_db):
        """自己参照 (A→A) はサービス層でスキップされる（CHECK制約に到達しない）"""
        from src.services.relation_service import add_relation

        conn = get_connection()
        try:
            a1 = _create_activity(conn, "Activity A")
            conn.commit()
        finally:
            conn.close()

        result = add_relation("activity", a1, [{"type": "activity", "ids": [a1]}], relation_type="depends_on")
        assert "error" not in result
        assert result["added"] == 0

    def test_depends_on_only_for_activity(self, temp_db):
        """depends_onはactivity以外のsource_typeでエラーになる"""
        from src.services.relation_service import add_relation

        conn = get_connection()
        try:
            cursor = conn.execute(
                "INSERT INTO discussion_topics (title, description) VALUES (?, ?)",
                ("Topic A", "Desc A"),
            )
            t1 = cursor.lastrowid
            tag_ids = ensure_tag_ids(conn, [("domain", "test")])
            link_tags(conn, "topic_tags", "topic_id", t1, tag_ids)
            conn.commit()
        finally:
            conn.close()

        result = add_relation("topic", t1, [{"type": "topic", "ids": [t1]}], relation_type="depends_on")
        assert "error" in result
        assert result["error"]["code"] == "INVALID_RELATION_TYPE"

    def test_cycle_rollback_preserves_prior_state(self, temp_db):
        """循環検出時にトランザクションがロールバックされ、同一バッチ内の先行追加も取り消される"""
        from src.services.relation_service import add_relation

        conn = get_connection()
        try:
            a1 = _create_activity(conn, "Activity A")
            a2 = _create_activity(conn, "Activity B")
            a3 = _create_activity(conn, "Activity C")
            conn.commit()
        finally:
            conn.close()

        # A→B を先に追加
        add_relation("activity", a1, [{"type": "activity", "ids": [a2]}], relation_type="depends_on")

        # B→C と B→A を同一バッチで追加（B→Aで循環検出）
        result = add_relation(
            "activity", a2,
            [{"type": "activity", "ids": [a3, a1]}],
            relation_type="depends_on",
        )
        assert "error" in result
        assert result["error"]["code"] == "CIRCULAR_DEPENDENCY"

        # B→Cも追加されていないことを確認（ロールバック）
        conn = get_connection()
        try:
            count = conn.execute(
                "SELECT COUNT(*) as cnt FROM activity_dependencies WHERE dependent_id = ? AND dependency_id = ?",
                (a2, a3),
            ).fetchone()["cnt"]
            assert count == 0
        finally:
            conn.close()

    def test_depends_on_idempotent(self, temp_db):
        """同一のdepends_on関係を再追加してもエラーにならずadded=0が返る"""
        from src.services.relation_service import add_relation

        conn = get_connection()
        try:
            a1 = _create_activity(conn, "Activity A")
            a2 = _create_activity(conn, "Activity B")
            conn.commit()
        finally:
            conn.close()

        result1 = add_relation("activity", a1, [{"type": "activity", "ids": [a2]}], relation_type="depends_on")
        assert result1["added"] == 1

        result2 = add_relation("activity", a1, [{"type": "activity", "ids": [a2]}], relation_type="depends_on")
        assert "error" not in result2
        assert result2["added"] == 0

    def test_depends_on_target_not_activity_rejected(self, temp_db):
        """depends_onのtarget_typeがactivity以外の場合エラーになる"""
        from src.services.relation_service import add_relation

        conn = get_connection()
        try:
            a1 = _create_activity(conn, "Activity A")
            cursor = conn.execute(
                "INSERT INTO discussion_topics (title, description) VALUES (?, ?)",
                ("Topic A", "Desc A"),
            )
            t1 = cursor.lastrowid
            tag_ids = ensure_tag_ids(conn, [("domain", "test")])
            link_tags(conn, "topic_tags", "topic_id", t1, tag_ids)
            conn.commit()
        finally:
            conn.close()

        result = add_relation("activity", a1, [{"type": "topic", "ids": [t1]}], relation_type="depends_on")
        assert "error" in result
        assert result["error"]["code"] == "INVALID_RELATION_TYPE"


class TestRemoveDependsOn:
    """remove_relation(relation_type="depends_on")のテスト"""

    def test_remove_depends_on_success(self, temp_db):
        """depends_on関係を削除できる"""
        from src.services.relation_service import add_relation, remove_relation

        conn = get_connection()
        try:
            a1 = _create_activity(conn, "Activity A")
            a2 = _create_activity(conn, "Activity B")
            conn.commit()
        finally:
            conn.close()

        add_relation("activity", a1, [{"type": "activity", "ids": [a2]}], relation_type="depends_on")

        result = remove_relation("activity", a1, [{"type": "activity", "ids": [a2]}], relation_type="depends_on")
        assert "error" not in result
        assert result["removed"] == 1

        # DBから消えていることを確認
        conn = get_connection()
        try:
            count = conn.execute(
                "SELECT COUNT(*) as cnt FROM activity_dependencies WHERE dependent_id = ? AND dependency_id = ?",
                (a1, a2),
            ).fetchone()["cnt"]
            assert count == 0
        finally:
            conn.close()

    def test_remove_nonexistent_depends_on(self, temp_db):
        """存在しないdepends_on関係の削除はエラーにならずremoved=0が返る"""
        from src.services.relation_service import remove_relation

        conn = get_connection()
        try:
            a1 = _create_activity(conn, "Activity A")
            a2 = _create_activity(conn, "Activity B")
            conn.commit()
        finally:
            conn.close()

        result = remove_relation("activity", a1, [{"type": "activity", "ids": [a2]}], relation_type="depends_on")
        assert "error" not in result
        assert result["removed"] == 0

    def test_remove_depends_on_non_activity_source_rejected(self, temp_db):
        """depends_on削除でsource_typeがactivity以外の場合エラーになる"""
        from src.services.relation_service import remove_relation

        conn = get_connection()
        try:
            cursor = conn.execute(
                "INSERT INTO discussion_topics (title, description) VALUES (?, ?)",
                ("Topic A", "Desc A"),
            )
            t1 = cursor.lastrowid
            tag_ids = ensure_tag_ids(conn, [("domain", "test")])
            link_tags(conn, "topic_tags", "topic_id", t1, tag_ids)
            conn.commit()
        finally:
            conn.close()

        result = remove_relation("topic", t1, [{"type": "topic", "ids": [t1]}], relation_type="depends_on")
        assert "error" in result
        assert result["error"]["code"] == "INVALID_RELATION_TYPE"

    def test_remove_depends_on_non_activity_target_rejected(self, temp_db):
        """depends_on削除でtarget_typeがactivity以外の場合エラーになる"""
        from src.services.relation_service import remove_relation

        conn = get_connection()
        try:
            a1 = _create_activity(conn, "Activity A")
            cursor = conn.execute(
                "INSERT INTO discussion_topics (title, description) VALUES (?, ?)",
                ("Topic A", "Desc A"),
            )
            t1 = cursor.lastrowid
            tag_ids = ensure_tag_ids(conn, [("domain", "test")])
            link_tags(conn, "topic_tags", "topic_id", t1, tag_ids)
            conn.commit()
        finally:
            conn.close()

        result = remove_relation("activity", a1, [{"type": "topic", "ids": [t1]}], relation_type="depends_on")
        assert "error" in result
        assert result["error"]["code"] == "INVALID_RELATION_TYPE"

    def test_remove_depends_on_allows_previously_cyclic_addition(self, temp_db):
        """depends_on削除後、以前は循環になった依存関係を追加できるようになる"""
        from src.services.relation_service import add_relation, remove_relation

        conn = get_connection()
        try:
            a1 = _create_activity(conn, "Activity A")
            a2 = _create_activity(conn, "Activity B")
            conn.commit()
        finally:
            conn.close()

        # A→B追加
        add_relation("activity", a1, [{"type": "activity", "ids": [a2]}], relation_type="depends_on")

        # B→Aは循環で拒否
        result = add_relation("activity", a2, [{"type": "activity", "ids": [a1]}], relation_type="depends_on")
        assert "error" in result

        # A→Bを削除
        remove_relation("activity", a1, [{"type": "activity", "ids": [a2]}], relation_type="depends_on")

        # B→Aが追加できるようになる
        result = add_relation("activity", a2, [{"type": "activity", "ids": [a1]}], relation_type="depends_on")
        assert "error" not in result
        assert result["added"] == 1

    def test_remove_depends_on_multiple_targets(self, temp_db):
        """depends_on関係の複数ターゲット一括削除ができる"""
        from src.services.relation_service import add_relation, remove_relation

        conn = get_connection()
        try:
            a1 = _create_activity(conn, "Activity A")
            a2 = _create_activity(conn, "Activity B")
            a3 = _create_activity(conn, "Activity C")
            conn.commit()
        finally:
            conn.close()

        add_relation("activity", a1, [{"type": "activity", "ids": [a2, a3]}], relation_type="depends_on")

        result = remove_relation("activity", a1, [{"type": "activity", "ids": [a2, a3]}], relation_type="depends_on")
        assert "error" not in result
        assert result["removed"] == 2

    def test_remove_relation_default_type_is_related(self, temp_db):
        """relation_typeを省略するとrelated（デフォルト）として動作し、depends_on行は削除されない"""
        from src.services.relation_service import add_relation, remove_relation

        conn = get_connection()
        try:
            a1 = _create_activity(conn, "Activity A")
            a2 = _create_activity(conn, "Activity B")
            conn.commit()
        finally:
            conn.close()

        # depends_on関係を追加
        add_relation("activity", a1, [{"type": "activity", "ids": [a2]}], relation_type="depends_on")

        # relation_type省略（デフォルトrelated）で削除→relationsテーブルには行がないのでremoved=0
        result = remove_relation("activity", a1, [{"type": "activity", "ids": [a2]}])
        assert "error" not in result
        assert result["removed"] == 0

        # depends_on関係はそのまま残っている
        conn = get_connection()
        try:
            count = conn.execute(
                "SELECT COUNT(*) as cnt FROM activity_dependencies WHERE dependent_id = ? AND dependency_id = ?",
                (a1, a2),
            ).fetchone()["cnt"]
            assert count == 1
        finally:
            conn.close()

    def test_remove_relation_invalid_relation_type(self, temp_db):
        """不正なrelation_typeを指定するとエラーになる"""
        from src.services.relation_service import remove_relation

        conn = get_connection()
        try:
            a1 = _create_activity(conn, "Activity A")
            a2 = _create_activity(conn, "Activity B")
            conn.commit()
        finally:
            conn.close()

        result = remove_relation("activity", a1, [{"type": "activity", "ids": [a2]}], relation_type="invalid_type")
        assert "error" in result
        assert result["error"]["code"] == "INVALID_RELATION_TYPE"
