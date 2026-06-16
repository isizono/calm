"""リレーションサービスの統合テスト"""
import os
import tempfile

import pytest

from src.db import get_connection, init_database
from src.services.pin_service import add_pin
from src.services.relation_service import add_relation, get_map, remove_relation
from src.services.tag_service import _injected_tags
from tests.helpers import add_decision, retract_decision


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


def _create_topic(conn, title="Test Topic"):
    """テスト用トピックを作成する"""
    from src.services.tag_service import ensure_tag_ids, link_tags

    cursor = conn.execute(
        "INSERT INTO discussion_topics (title, description) VALUES (?, ?)",
        (title, f"Description for {title}"),
    )
    topic_id = cursor.lastrowid
    tag_ids = ensure_tag_ids(conn, DEFAULT_TAGS)
    link_tags(conn, "topic_tags", "topic_id", topic_id, tag_ids)
    return topic_id


def _create_activity(conn, title="Test Activity"):
    """テスト用アクティビティを作成する"""
    from src.services.tag_service import ensure_tag_ids, link_tags

    cursor = conn.execute(
        "INSERT INTO activities (title, description, status) VALUES (?, ?, ?)",
        (title, f"Description for {title}", "pending"),
    )
    activity_id = cursor.lastrowid
    tag_ids = ensure_tag_ids(conn, DEFAULT_TAGS)
    link_tags(conn, "activity_tags", "activity_id", activity_id, tag_ids)
    return activity_id


def _create_material(conn, title="Test Material"):
    """テスト用資材を作成する"""
    from src.services.tag_service import ensure_tag_ids, link_tags

    cursor = conn.execute(
        "INSERT INTO materials (title, content) VALUES (?, ?)",
        (title, f"Content for {title}"),
    )
    material_id = cursor.lastrowid
    tag_ids = ensure_tag_ids(conn, DEFAULT_TAGS)
    link_tags(conn, "material_tags", "material_id", material_id, tag_ids)
    return material_id


@pytest.fixture
def sample_entities(temp_db):
    """テスト用のトピック、アクティビティ、資材を作成する"""
    conn = get_connection()
    try:
        t1 = _create_topic(conn, "Topic A")
        t2 = _create_topic(conn, "Topic B")
        t3 = _create_topic(conn, "Topic C")
        a1 = _create_activity(conn, "Activity X")
        a2 = _create_activity(conn, "Activity Y")
        a3 = _create_activity(conn, "Activity Z")
        m1 = _create_material(conn, "Material P")
        m2 = _create_material(conn, "Material Q")
        conn.commit()
    finally:
        conn.close()
    return {"t1": t1, "t2": t2, "t3": t3, "a1": a1, "a2": a2, "a3": a3, "m1": m1, "m2": m2}


class TestAddRelation:
    """add_relationの統合テスト"""

    def test_add_topic_topic_relation(self, sample_entities):
        """topic↔topicリレーションが追加できる"""
        e = sample_entities
        result = add_relation("topic", e["t1"], [{"type": "topic", "ids": [e["t2"]]}])

        assert "error" not in result
        assert result["added"] == 1

    def test_add_topic_activity_relation(self, sample_entities):
        """topic↔activityリレーションが追加できる"""
        e = sample_entities
        result = add_relation("topic", e["t1"], [{"type": "activity", "ids": [e["a1"]]}])

        assert "error" not in result
        assert result["added"] == 1

    def test_add_activity_topic_relation(self, sample_entities):
        """activity→topicリレーション（逆方向指定）が追加できる"""
        e = sample_entities
        result = add_relation("activity", e["a1"], [{"type": "topic", "ids": [e["t1"]]}])

        assert "error" not in result
        assert result["added"] == 1

        # relationsテーブルに正規化されて格納されていることを確認
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM relations WHERE source_type = 'activity' AND source_id = ? AND target_type = 'topic' AND target_id = ?",
                (e["a1"], e["t1"]),
            ).fetchone()
            assert row is not None
        finally:
            conn.close()

    def test_add_activity_activity_relation(self, sample_entities):
        """activity↔activityリレーションが追加できる"""
        e = sample_entities
        result = add_relation("activity", e["a1"], [{"type": "activity", "ids": [e["a2"]]}])

        assert "error" not in result
        assert result["added"] == 1

    def test_add_multiple_targets(self, sample_entities):
        """複数ターゲットを一度に追加できる"""
        e = sample_entities
        result = add_relation("topic", e["t1"], [
            {"type": "topic", "ids": [e["t2"], e["t3"]]},
            {"type": "activity", "ids": [e["a1"]]},
        ])

        assert "error" not in result
        assert result["added"] == 3

    def test_add_relation_idempotent(self, sample_entities):
        """重複追加が冪等（エラーにならず、added=0）"""
        e = sample_entities
        result1 = add_relation("topic", e["t1"], [{"type": "topic", "ids": [e["t2"]]}])
        assert result1["added"] == 1

        result2 = add_relation("topic", e["t1"], [{"type": "topic", "ids": [e["t2"]]}])
        assert "error" not in result2
        assert result2["added"] == 0

    def test_add_relation_self_reference_skipped(self, sample_entities):
        """自己参照はスキップされる（エラーにならない）"""
        e = sample_entities
        result = add_relation("topic", e["t1"], [{"type": "topic", "ids": [e["t1"]]}])

        assert "error" not in result
        assert result["added"] == 0

    def test_add_relation_invalid_source_type(self, sample_entities):
        """不正なsource_typeでエラー"""
        e = sample_entities
        result = add_relation("invalid", e["t1"], [{"type": "topic", "ids": [e["t2"]]}])

        assert "error" in result
        assert result["error"]["code"] == "INVALID_ENTITY_TYPE"

    def test_add_relation_invalid_target_type(self, sample_entities):
        """不正なtarget_typeでエラー"""
        e = sample_entities
        result = add_relation("topic", e["t1"], [{"type": "invalid", "ids": [e["t2"]]}])

        assert "error" in result
        assert result["error"]["code"] == "INVALID_ENTITY_TYPE"

    def test_add_relation_empty_targets(self, sample_entities):
        """空のtargetsでエラー"""
        e = sample_entities
        result = add_relation("topic", e["t1"], [])

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_add_relation_nonexistent_id_succeeds(self, sample_entities):
        """存在しないIDへのリレーション追加はrelationsテーブルでは成功する（FK制約なし）"""
        e = sample_entities
        result = add_relation("topic", e["t1"], [{"type": "topic", "ids": [99999]}])

        assert "error" not in result
        assert result["added"] == 1

    def test_add_relation_normalizes_order(self, sample_entities):
        """topic_id_1 < topic_id_2に正規化される"""
        e = sample_entities
        # t2 > t1 のはずなので、t2からt1への追加でもt1,t2の順に格納される
        result = add_relation("topic", e["t2"], [{"type": "topic", "ids": [e["t1"]]}])
        assert result["added"] == 1

        # 正規化済みの順で格納されていることを確認
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM relations WHERE source_type = 'topic' AND source_id = ? AND target_type = 'topic' AND target_id = ?",
                (min(e["t1"], e["t2"]), max(e["t1"], e["t2"])),
            ).fetchone()
            assert row is not None
        finally:
            conn.close()


class TestRemoveRelation:
    """remove_relationの統合テスト"""

    def test_remove_relation_success(self, sample_entities):
        """リレーション削除が正常に動作する"""
        e = sample_entities
        add_relation("topic", e["t1"], [{"type": "topic", "ids": [e["t2"]]}])

        result = remove_relation("topic", e["t1"], [{"type": "topic", "ids": [e["t2"]]}])

        assert "error" not in result
        assert result["removed"] == 1

    def test_remove_nonexistent_relation(self, sample_entities):
        """存在しないリレーションの削除がエラーにならない"""
        e = sample_entities
        result = remove_relation("topic", e["t1"], [{"type": "topic", "ids": [e["t2"]]}])

        assert "error" not in result
        assert result["removed"] == 0

    def test_remove_topic_activity_relation(self, sample_entities):
        """topic↔activityリレーション削除が動作する"""
        e = sample_entities
        add_relation("topic", e["t1"], [{"type": "activity", "ids": [e["a1"]]}])

        result = remove_relation("topic", e["t1"], [{"type": "activity", "ids": [e["a1"]]}])

        assert "error" not in result
        assert result["removed"] == 1

    def test_remove_activity_activity_relation(self, sample_entities):
        """activity↔activityリレーション削除が動作する"""
        e = sample_entities
        add_relation("activity", e["a1"], [{"type": "activity", "ids": [e["a2"]]}])

        result = remove_relation("activity", e["a1"], [{"type": "activity", "ids": [e["a2"]]}])

        assert "error" not in result
        assert result["removed"] == 1

    def test_remove_activity_topic_relation(self, sample_entities):
        """activity→topic方向のリレーション削除が動作する"""
        e = sample_entities
        add_relation("activity", e["a1"], [{"type": "topic", "ids": [e["t1"]]}])

        result = remove_relation("activity", e["a1"], [{"type": "topic", "ids": [e["t1"]]}])

        assert "error" not in result
        assert result["removed"] == 1

    def test_remove_multiple_targets(self, sample_entities):
        """複数ターゲットの一括削除"""
        e = sample_entities
        add_relation("topic", e["t1"], [
            {"type": "topic", "ids": [e["t2"], e["t3"]]},
        ])

        result = remove_relation("topic", e["t1"], [
            {"type": "topic", "ids": [e["t2"], e["t3"]]},
        ])

        assert "error" not in result
        assert result["removed"] == 2


class TestGetMap:
    """get_mapの統合テスト"""

    def test_get_map_depth_0_returns_origin(self, sample_entities):
        """depth=0で起点のみ返る"""
        e = sample_entities
        # リレーションを追加（depth=0テストでは結果に含まれない想定）
        add_relation("topic", e["t1"], [{"type": "topic", "ids": [e["t2"]]}])

        result = get_map("topic", e["t1"], min_depth=0, max_depth=0)

        assert "error" not in result
        assert result["total_count"] == 1
        assert result["entities"][0]["type"] == "topic"
        assert result["entities"][0]["id"] == e["t1"]
        assert result["entities"][0]["depth"] == 0

    def test_get_map_depth_1_returns_direct_relations(self, sample_entities):
        """depth=1で直接関連が返る"""
        e = sample_entities
        add_relation("topic", e["t1"], [
            {"type": "topic", "ids": [e["t2"]]},
            {"type": "activity", "ids": [e["a1"]]},
        ])

        result = get_map("topic", e["t1"], min_depth=0, max_depth=1)

        assert "error" not in result
        # 起点(t1) + t2 + a1 = 3エンティティ
        assert result["total_count"] == 3
        types_ids = {(ent["type"], ent["id"]) for ent in result["entities"]}
        assert ("topic", e["t1"]) in types_ids
        assert ("topic", e["t2"]) in types_ids
        assert ("activity", e["a1"]) in types_ids

    def test_get_map_min_depth_filters(self, sample_entities):
        """min_depthで起点を除外できる"""
        e = sample_entities
        add_relation("topic", e["t1"], [{"type": "topic", "ids": [e["t2"]]}])

        result = get_map("topic", e["t1"], min_depth=1, max_depth=1)

        assert "error" not in result
        assert result["total_count"] == 1
        assert result["entities"][0]["id"] == e["t2"]

    def test_get_map_transitive_depth_2(self, sample_entities):
        """depth=2で間接関連が返る"""
        e = sample_entities
        # t1 - t2 - t3 のチェーン
        add_relation("topic", e["t1"], [{"type": "topic", "ids": [e["t2"]]}])
        add_relation("topic", e["t2"], [{"type": "topic", "ids": [e["t3"]]}])

        result = get_map("topic", e["t1"], min_depth=0, max_depth=2)

        assert "error" not in result
        # t1(0) + t2(1) + t3(2) = 3エンティティ
        assert result["total_count"] == 3
        types_ids = {(ent["type"], ent["id"]) for ent in result["entities"]}
        assert ("topic", e["t3"]) in types_ids

    def test_get_map_no_infinite_loop_on_cycle(self, sample_entities):
        """循環参照で無限ループしない"""
        e = sample_entities
        # t1 - t2 - t3 - t1 の循環
        add_relation("topic", e["t1"], [{"type": "topic", "ids": [e["t2"]]}])
        add_relation("topic", e["t2"], [{"type": "topic", "ids": [e["t3"]]}])
        add_relation("topic", e["t1"], [{"type": "topic", "ids": [e["t3"]]}])

        result = get_map("topic", e["t1"], min_depth=0, max_depth=5)

        assert "error" not in result
        # 循環してもt1, t2, t3の3つしかない
        assert result["total_count"] == 3

    def test_get_map_returns_catalog_with_title_and_tags(self, sample_entities):
        """カタログにtitleとtagsが含まれる"""
        e = sample_entities
        add_relation("topic", e["t1"], [{"type": "activity", "ids": [e["a1"]]}])

        result = get_map("topic", e["t1"], min_depth=0, max_depth=1)

        assert "error" not in result
        for entity in result["entities"]:
            assert "title" in entity
            assert "tags" in entity
            assert isinstance(entity["tags"], list)
            assert entity["title"] != ""

    def test_get_map_invalid_entity_type(self, temp_db):
        """不正なentity_typeでエラー"""
        result = get_map("invalid", 1, min_depth=0, max_depth=1)

        assert "error" in result
        assert result["error"]["code"] == "INVALID_ENTITY_TYPE"

    def test_get_map_invalid_min_depth(self, temp_db):
        """min_depth < 0でエラー"""
        result = get_map("topic", 1, min_depth=-1, max_depth=1)

        assert "error" in result
        assert result["error"]["code"] == "INVALID_PARAMETER"

    def test_get_map_max_depth_less_than_min_depth(self, temp_db):
        """max_depth < min_depthでエラー"""
        result = get_map("topic", 1, min_depth=2, max_depth=1)

        assert "error" in result
        assert result["error"]["code"] == "INVALID_PARAMETER"

    def test_get_map_max_depth_exceeds_limit(self, temp_db):
        """max_depth > 10でエラー"""
        result = get_map("topic", 1, min_depth=0, max_depth=11)

        assert "error" in result
        assert result["error"]["code"] == "INVALID_PARAMETER"
        assert "10" in result["error"]["message"]

    def test_get_map_max_depth_at_limit(self, sample_entities):
        """max_depth=10は許可される"""
        e = sample_entities
        result = get_map("topic", e["t1"], min_depth=0, max_depth=10)

        assert "error" not in result

    def test_get_map_no_relations(self, sample_entities):
        """リレーションなしの場合、起点のみ返る"""
        e = sample_entities
        result = get_map("topic", e["t1"], min_depth=0, max_depth=2)

        assert "error" not in result
        assert result["total_count"] == 1
        assert result["entities"][0]["id"] == e["t1"]

    def test_get_map_nonexistent_id_returns_empty(self, temp_db):
        """存在しないIDのget_mapは空結果を返す"""
        result = get_map("topic", 99999, min_depth=0, max_depth=1)

        assert "error" not in result
        assert result["total_count"] == 0
        assert result["entities"] == []

    def test_get_map_multiple_paths_returns_min_depth(self, sample_entities):
        """同じエンティティに異なるdepthで到達する場合、MIN(depth)が返る"""
        e = sample_entities
        # t1 - t2 - t3 (depth 2 for t3)
        # t1 - t3        (depth 1 for t3)
        add_relation("topic", e["t1"], [{"type": "topic", "ids": [e["t2"], e["t3"]]}])
        add_relation("topic", e["t2"], [{"type": "topic", "ids": [e["t3"]]}])

        result = get_map("topic", e["t1"], min_depth=0, max_depth=2)

        assert "error" not in result
        t3_entry = next(ent for ent in result["entities"] if ent["id"] == e["t3"])
        assert t3_entry["depth"] == 1  # 直接到達の方が浅い

    def test_get_map_sorted_by_depth(self, sample_entities):
        """結果がdepth順でソートされている"""
        e = sample_entities
        add_relation("topic", e["t1"], [{"type": "topic", "ids": [e["t2"]]}])
        add_relation("topic", e["t2"], [{"type": "topic", "ids": [e["t3"]]}])

        result = get_map("topic", e["t1"], min_depth=0, max_depth=2)

        depths = [ent["depth"] for ent in result["entities"]]
        assert depths == sorted(depths)

    def test_get_map_topic_has_gravity_counts(self, sample_entities):
        """topicエンティティにdecisions_count/materials_countが含まれる"""
        e = sample_entities
        # t1にdecisions 2件とmaterials 1件を紐づける
        add_decision(decision="決定1", reason="理由1", topic_id=e["t1"])
        add_decision(decision="決定2", reason="理由2", topic_id=e["t1"])
        add_relation("topic", e["t1"], [{"type": "material", "ids": [e["m1"]]}])

        result = get_map("topic", e["t1"], min_depth=0, max_depth=0)

        assert "error" not in result
        t1_entity = next(ent for ent in result["entities"] if ent["id"] == e["t1"])
        assert t1_entity["type"] == "topic"
        assert t1_entity["decisions_count"] == 2
        assert t1_entity["materials_count"] == 1

    def test_get_map_topic_zero_gravity_counts_present(self, sample_entities):
        """decisions/materialsがゼロのtopicでもdecisions_count=0, materials_count=0が返る"""
        e = sample_entities
        result = get_map("topic", e["t1"], min_depth=0, max_depth=0)

        assert "error" not in result
        t1_entity = result["entities"][0]
        assert t1_entity["type"] == "topic"
        assert t1_entity["decisions_count"] == 0
        assert t1_entity["materials_count"] == 0

    def test_get_map_topic_excludes_retracted_decisions(self, sample_entities):
        """retracted decisionsはdecisions_countに含まれない"""
        e = sample_entities
        add_decision(decision="有効", reason="理由", topic_id=e["t1"])
        retracted = add_decision(decision="撤回済み", reason="理由", topic_id=e["t1"])
        # 1件をretract
        retract_decision(retracted["decision_id"])

        result = get_map("topic", e["t1"], min_depth=0, max_depth=0)

        assert "error" not in result
        t1_entity = result["entities"][0]
        assert t1_entity["decisions_count"] == 1

    def test_get_map_activity_material_no_gravity_fields(self, sample_entities):
        """activity/materialエンティティにはdecisions_count/materials_countが付与されない"""
        e = sample_entities
        add_relation("topic", e["t1"], [
            {"type": "activity", "ids": [e["a1"]]},
            {"type": "material", "ids": [e["m1"]]},
        ])

        result = get_map("topic", e["t1"], min_depth=0, max_depth=1)

        assert "error" not in result
        non_topic = [ent for ent in result["entities"] if ent["type"] != "topic"]
        # activity, materialが含まれていることを確認
        types = {ent["type"] for ent in non_topic}
        assert "activity" in types
        assert "material" in types
        # これらのエンティティにはgravityフィールドが付与されていない
        for ent in non_topic:
            assert "decisions_count" not in ent
            assert "materials_count" not in ent

    def test_get_map_topic_materials_count_excludes_activity_indirect(self, sample_entities):
        """topicのmaterials_countはactivity経由の間接リレーションを含まない"""
        e = sample_entities
        # topic-activity-material の経路のみ設定（topicには直接紐づけない）
        add_relation("topic", e["t1"], [{"type": "activity", "ids": [e["a1"]]}])
        add_relation("activity", e["a1"], [{"type": "material", "ids": [e["m1"]]}])

        result = get_map("topic", e["t1"], min_depth=0, max_depth=2)

        assert "error" not in result
        t1_entity = next(ent for ent in result["entities"] if ent["type"] == "topic" and ent["id"] == e["t1"])
        # 直接リレーションがないためゼロ
        assert t1_entity["materials_count"] == 0

    def test_get_map_multi_topic_independent_gravity_counts(self, sample_entities):
        """深さ走査で到達した複数topicがそれぞれ独立した重力カウントを持つ"""
        e = sample_entities
        # t1 -> t2 (depth 1)
        add_relation("topic", e["t1"], [{"type": "topic", "ids": [e["t2"]]}])
        # t1にdecision 1件、t2にdecision 2件
        add_decision(decision="d1", reason="r", topic_id=e["t1"])
        add_decision(decision="d2a", reason="r", topic_id=e["t2"])
        add_decision(decision="d2b", reason="r", topic_id=e["t2"])
        # t1にmaterial 1件、t2にmaterial 2件を直接紐づけ
        add_relation("topic", e["t1"], [{"type": "material", "ids": [e["m1"]]}])
        add_relation("topic", e["t2"], [{"type": "material", "ids": [e["m1"], e["m2"]]}])

        result = get_map("topic", e["t1"], min_depth=0, max_depth=1)

        assert "error" not in result
        by_id = {ent["id"]: ent for ent in result["entities"] if ent["type"] == "topic"}
        assert by_id[e["t1"]]["decisions_count"] == 1
        assert by_id[e["t2"]]["decisions_count"] == 2
        assert by_id[e["t1"]]["materials_count"] == 1
        assert by_id[e["t2"]]["materials_count"] == 2


class TestCascadeDelete:
    """ON DELETE CASCADEの動作テスト"""

    def test_cascade_delete_topic_cleans_relations(self, sample_entities):
        """トピック削除時にrelationsテーブルの関連行が消える"""
        e = sample_entities
        add_relation("topic", e["t1"], [{"type": "topic", "ids": [e["t2"]]}])

        conn = get_connection()
        try:
            conn.execute("DELETE FROM discussion_topics WHERE id = ?", (e["t1"],))
            conn.commit()

            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM relations WHERE (source_type = 'topic' AND source_id = ?) OR (target_type = 'topic' AND target_id = ?)",
                (e["t1"], e["t1"]),
            ).fetchone()
            assert row["cnt"] == 0
        finally:
            conn.close()

    def test_cascade_delete_topic_cleans_activity_topic_relations(self, sample_entities):
        """トピック削除時にactivity-topicリレーションが消える"""
        e = sample_entities
        add_relation("topic", e["t1"], [{"type": "activity", "ids": [e["a1"]]}])

        conn = get_connection()
        try:
            conn.execute("DELETE FROM discussion_topics WHERE id = ?", (e["t1"],))
            conn.commit()

            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM relations WHERE target_type = 'topic' AND target_id = ?",
                (e["t1"],),
            ).fetchone()
            assert row["cnt"] == 0
        finally:
            conn.close()

    def test_cascade_delete_activity_cleans_activity_topic_relations(self, sample_entities):
        """アクティビティ削除時にactivity-topicリレーションが消える"""
        e = sample_entities
        add_relation("topic", e["t1"], [{"type": "activity", "ids": [e["a1"]]}])

        conn = get_connection()
        try:
            conn.execute("DELETE FROM activities WHERE id = ?", (e["a1"],))
            conn.commit()

            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM relations WHERE source_type = 'activity' AND source_id = ?",
                (e["a1"],),
            ).fetchone()
            assert row["cnt"] == 0
        finally:
            conn.close()

    def test_cascade_delete_activity_cleans_activity_relations(self, sample_entities):
        """アクティビティ削除時にactivity-activityリレーションが消える"""
        e = sample_entities
        add_relation("activity", e["a1"], [{"type": "activity", "ids": [e["a2"]]}])

        conn = get_connection()
        try:
            conn.execute("DELETE FROM activities WHERE id = ?", (e["a1"],))
            conn.commit()

            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM relations WHERE (source_type = 'activity' AND source_id = ?) OR (target_type = 'activity' AND target_id = ?)",
                (e["a1"], e["a1"]),
            ).fetchone()
            assert row["cnt"] == 0
        finally:
            conn.close()

    def test_cascade_delete_material_cleans_topic_material_relations(self, sample_entities):
        """資材削除時にmaterial-topicリレーションが消える"""
        e = sample_entities
        add_relation("topic", e["t1"], [{"type": "material", "ids": [e["m1"]]}])

        conn = get_connection()
        try:
            conn.execute("DELETE FROM materials WHERE id = ?", (e["m1"],))
            conn.commit()

            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM relations WHERE source_type = 'material' AND source_id = ?",
                (e["m1"],),
            ).fetchone()
            assert row["cnt"] == 0
        finally:
            conn.close()

    def test_cascade_delete_material_cleans_activity_material_relations(self, sample_entities):
        """資材削除時にactivity-materialリレーションが消える"""
        e = sample_entities
        add_relation("activity", e["a1"], [{"type": "material", "ids": [e["m1"]]}])

        conn = get_connection()
        try:
            conn.execute("DELETE FROM materials WHERE id = ?", (e["m1"],))
            conn.commit()

            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM relations WHERE (source_type = 'material' AND source_id = ?) OR (target_type = 'material' AND target_id = ?)",
                (e["m1"], e["m1"]),
            ).fetchone()
            assert row["cnt"] == 0
        finally:
            conn.close()


class TestMaterialRelations:
    """materialリレーションの統合テスト"""

    def test_add_topic_material_relation(self, sample_entities):
        """topic↔materialリレーションが追加できる"""
        e = sample_entities
        result = add_relation("topic", e["t1"], [{"type": "material", "ids": [e["m1"]]}])

        assert "error" not in result
        assert result["added"] == 1

    def test_add_material_topic_relation(self, sample_entities):
        """material→topicリレーション（逆方向指定）が追加できる"""
        e = sample_entities
        result = add_relation("material", e["m1"], [{"type": "topic", "ids": [e["t1"]]}])

        assert "error" not in result
        assert result["added"] == 1

        # relationsテーブルに正規化されて格納されていることを確認
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM relations WHERE source_type = 'material' AND source_id = ? AND target_type = 'topic' AND target_id = ?",
                (e["m1"], e["t1"]),
            ).fetchone()
            assert row is not None
        finally:
            conn.close()

    def test_add_activity_material_relation(self, sample_entities):
        """activity↔materialリレーションが追加できる"""
        e = sample_entities
        result = add_relation("activity", e["a1"], [{"type": "material", "ids": [e["m1"]]}])

        assert "error" not in result
        assert result["added"] == 1

    def test_add_material_activity_relation(self, sample_entities):
        """material→activityリレーション（逆方向指定）が追加できる"""
        e = sample_entities
        result = add_relation("material", e["m1"], [{"type": "activity", "ids": [e["a1"]]}])

        assert "error" not in result
        assert result["added"] == 1

        # relationsテーブルに正規化されて格納されていることを確認
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM relations WHERE source_type = 'activity' AND source_id = ? AND target_type = 'material' AND target_id = ?",
                (e["a1"], e["m1"]),
            ).fetchone()
            assert row is not None
        finally:
            conn.close()

    def test_remove_topic_material_relation(self, sample_entities):
        """topic↔materialリレーション削除が動作する"""
        e = sample_entities
        add_relation("topic", e["t1"], [{"type": "material", "ids": [e["m1"]]}])

        result = remove_relation("topic", e["t1"], [{"type": "material", "ids": [e["m1"]]}])

        assert "error" not in result
        assert result["removed"] == 1

    def test_remove_activity_material_relation(self, sample_entities):
        """activity↔materialリレーション削除が動作する"""
        e = sample_entities
        add_relation("activity", e["a1"], [{"type": "material", "ids": [e["m1"]]}])

        result = remove_relation("activity", e["a1"], [{"type": "material", "ids": [e["m1"]]}])

        assert "error" not in result
        assert result["removed"] == 1

    def test_get_map_includes_material(self, sample_entities):
        """get_mapでmaterialが含まれる"""
        e = sample_entities
        add_relation("activity", e["a1"], [{"type": "material", "ids": [e["m1"]]}])

        result = get_map("activity", e["a1"], min_depth=0, max_depth=1)

        assert "error" not in result
        types_ids = {(ent["type"], ent["id"]) for ent in result["entities"]}
        assert ("material", e["m1"]) in types_ids

    def test_get_map_material_has_tags(self, sample_entities):
        """get_mapのmaterialカタログにtagsが含まれる"""
        e = sample_entities
        add_relation("activity", e["a1"], [{"type": "material", "ids": [e["m1"]]}])

        result = get_map("activity", e["a1"], min_depth=0, max_depth=1)

        assert "error" not in result
        mat_entry = next(ent for ent in result["entities"] if ent["type"] == "material")
        assert "tags" in mat_entry
        assert isinstance(mat_entry["tags"], list)

    def test_material_self_reference_skipped(self, sample_entities):
        """material-materialの自己参照はスキップされる（エラーにならない）"""
        e = sample_entities
        result = add_relation("material", e["m1"], [{"type": "material", "ids": [e["m1"]]}])

        assert "error" not in result
        assert result["added"] == 0

    def test_material_material_relation_success(self, sample_entities):
        """material-material（異なるID）のrelatedリレーションが追加できる"""
        e = sample_entities
        result = add_relation("material", e["m1"], [{"type": "material", "ids": [e["m2"]]}])

        assert "error" not in result
        assert result["added"] == 1

    def test_remove_material_material_relation(self, sample_entities):
        """material-materialリレーション削除が動作する"""
        e = sample_entities
        add_relation("material", e["m1"], [{"type": "material", "ids": [e["m2"]]}])

        result = remove_relation("material", e["m1"], [{"type": "material", "ids": [e["m2"]]}])

        assert "error" not in result
        assert result["removed"] == 1


def _create_decision(conn, topic_id, title="Test Decision"):
    """テスト用decisionを作成する"""
    cursor = conn.execute(
        "INSERT INTO decisions (topic_id, decision, reason) VALUES (?, ?, ?)",
        (topic_id, title, f"Reason for {title}"),
    )
    return cursor.lastrowid


def _create_log(conn, topic_id, title="Test Log"):
    """テスト用discussion_logを作成する"""
    cursor = conn.execute(
        "INSERT INTO discussion_logs (topic_id, content) VALUES (?, ?)",
        (topic_id, f"Content for {title}"),
    )
    return cursor.lastrowid


@pytest.fixture
def entities_with_decision_log(temp_db):
    """テスト用のtopic, activity, material, decision, logを作成する"""
    conn = get_connection()
    try:
        t1 = _create_topic(conn, "Topic A")
        t2 = _create_topic(conn, "Topic B")
        a1 = _create_activity(conn, "Activity X")
        a2 = _create_activity(conn, "Activity Y")
        m1 = _create_material(conn, "Material P")
        m2 = _create_material(conn, "Material Q")
        d1 = _create_decision(conn, t1, "Decision 1")
        d2 = _create_decision(conn, t1, "Decision 2")
        d3 = _create_decision(conn, t2, "Decision 3")
        l1 = _create_log(conn, t1, "Log 1")
        l2 = _create_log(conn, t2, "Log 2")
        conn.commit()
    finally:
        conn.close()
    return {
        "t1": t1, "t2": t2,
        "a1": a1, "a2": a2,
        "m1": m1, "m2": m2,
        "d1": d1, "d2": d2, "d3": d3,
        "l1": l1, "l2": l2,
    }


class TestNewEntityTypeRelations:
    """decision/logエンティティタイプのrelatedリレーションテスト"""

    def test_decision_decision_related(self, entities_with_decision_log):
        """decision↔decisionのrelatedリレーションが追加・削除できる"""
        e = entities_with_decision_log
        result = add_relation("decision", e["d1"], [{"type": "decision", "ids": [e["d2"]]}])
        assert "error" not in result
        assert result["added"] == 1

        result = remove_relation("decision", e["d1"], [{"type": "decision", "ids": [e["d2"]]}])
        assert "error" not in result
        assert result["removed"] == 1

    def test_log_log_related(self, entities_with_decision_log):
        """log↔logのrelatedリレーションが追加・削除できる"""
        e = entities_with_decision_log
        result = add_relation("log", e["l1"], [{"type": "log", "ids": [e["l2"]]}])
        assert "error" not in result
        assert result["added"] == 1

        result = remove_relation("log", e["l1"], [{"type": "log", "ids": [e["l2"]]}])
        assert "error" not in result
        assert result["removed"] == 1

    def test_topic_decision_related(self, entities_with_decision_log):
        """topic↔decisionのrelatedリレーションが追加できる"""
        e = entities_with_decision_log
        result = add_relation("topic", e["t1"], [{"type": "decision", "ids": [e["d1"]]}])
        assert "error" not in result
        assert result["added"] == 1

    def test_activity_log_related(self, entities_with_decision_log):
        """activity↔logのrelatedリレーションが追加できる"""
        e = entities_with_decision_log
        result = add_relation("activity", e["a1"], [{"type": "log", "ids": [e["l1"]]}])
        assert "error" not in result
        assert result["added"] == 1

    def test_decision_material_related(self, entities_with_decision_log):
        """decision↔materialのrelatedリレーションが追加できる"""
        e = entities_with_decision_log
        result = add_relation("decision", e["d1"], [{"type": "material", "ids": [e["m1"]]}])
        assert "error" not in result
        assert result["added"] == 1

    def test_log_topic_related(self, entities_with_decision_log):
        """log→topicのrelatedリレーション（逆方向指定）が追加できる"""
        e = entities_with_decision_log
        result = add_relation("log", e["l1"], [{"type": "topic", "ids": [e["t1"]]}])
        assert "error" not in result
        assert result["added"] == 1


class TestSupersedes:
    """supersedesリレーションのテスト"""

    def test_supersedes_add_and_remove(self, entities_with_decision_log):
        """decision→decisionのsupersedesリレーションが追加・削除できる"""
        e = entities_with_decision_log
        result = add_relation(
            "decision", e["d1"], [{"type": "decision", "ids": [e["d2"]]}],
            relation_type="supersedes",
        )
        assert "error" not in result
        assert result["added"] == 1

        result = remove_relation(
            "decision", e["d1"], [{"type": "decision", "ids": [e["d2"]]}],
            relation_type="supersedes",
        )
        assert "error" not in result
        assert result["removed"] == 1

    def test_supersedes_direct_cycle_rejected(self, entities_with_decision_log):
        """supersedes直接循環（A→B, B→A）がCIRCULAR_SUPERSEDESエラーで拒否される"""
        e = entities_with_decision_log
        result = add_relation(
            "decision", e["d1"], [{"type": "decision", "ids": [e["d2"]]}],
            relation_type="supersedes",
        )
        assert "error" not in result
        assert result["added"] == 1

        result = add_relation(
            "decision", e["d2"], [{"type": "decision", "ids": [e["d1"]]}],
            relation_type="supersedes",
        )
        assert "error" in result
        assert result["error"]["code"] == "CIRCULAR_SUPERSEDES"

    def test_supersedes_transitive_cycle_rejected(self, entities_with_decision_log):
        """supersedes推移的循環（A→B, B→C, C→A）がCIRCULAR_SUPERSEDESエラーで拒否される"""
        e = entities_with_decision_log
        result = add_relation(
            "decision", e["d1"], [{"type": "decision", "ids": [e["d2"]]}],
            relation_type="supersedes",
        )
        assert "error" not in result

        result = add_relation(
            "decision", e["d2"], [{"type": "decision", "ids": [e["d3"]]}],
            relation_type="supersedes",
        )
        assert "error" not in result

        result = add_relation(
            "decision", e["d3"], [{"type": "decision", "ids": [e["d1"]]}],
            relation_type="supersedes",
        )
        assert "error" in result
        assert result["error"]["code"] == "CIRCULAR_SUPERSEDES"

    def test_supersedes_non_decision_source_rejected(self, entities_with_decision_log):
        """supersedesのsource_typeがdecision以外の場合INVALID_RELATION_TYPEエラーになる"""
        e = entities_with_decision_log
        result = add_relation(
            "topic", e["t1"], [{"type": "decision", "ids": [e["d1"]]}],
            relation_type="supersedes",
        )
        assert "error" in result
        assert result["error"]["code"] == "INVALID_RELATION_TYPE"

    def test_supersedes_non_decision_target_rejected(self, entities_with_decision_log):
        """supersedesのtarget_typeがdecision以外の場合INVALID_RELATION_TYPEエラーになる"""
        e = entities_with_decision_log
        result = add_relation(
            "decision", e["d1"], [{"type": "topic", "ids": [e["t1"]]}],
            relation_type="supersedes",
        )
        assert "error" in result
        assert result["error"]["code"] == "INVALID_RELATION_TYPE"

    def test_supersedes_idempotent(self, entities_with_decision_log):
        """同じsupersedesペアを再追加してもエラーにならずadded=0が返る"""
        e = entities_with_decision_log
        result1 = add_relation(
            "decision", e["d1"], [{"type": "decision", "ids": [e["d2"]]}],
            relation_type="supersedes",
        )
        assert result1["added"] == 1

        result2 = add_relation(
            "decision", e["d1"], [{"type": "decision", "ids": [e["d2"]]}],
            relation_type="supersedes",
        )
        assert "error" not in result2
        assert result2["added"] == 0

    def test_supersedes_cycle_rollback(self, entities_with_decision_log):
        """循環検出時にトランザクションがロールバックされ、同一バッチ内の先行追加も取り消される"""
        e = entities_with_decision_log
        # d1→d2 を先に追加
        add_relation(
            "decision", e["d1"], [{"type": "decision", "ids": [e["d2"]]}],
            relation_type="supersedes",
        )

        # d2→d3 と d2→d1 を同一バッチで追加（d2→d1で循環検出）
        result = add_relation(
            "decision", e["d2"],
            [{"type": "decision", "ids": [e["d3"], e["d1"]]}],
            relation_type="supersedes",
        )
        assert "error" in result
        assert result["error"]["code"] == "CIRCULAR_SUPERSEDES"

        # d2→d3も追加されていないことを確認（ロールバック）
        conn = get_connection()
        try:
            count = conn.execute(
                "SELECT COUNT(*) as cnt FROM decision_supersedes WHERE source_id = ? AND target_id = ?",
                (e["d2"], e["d3"]),
            ).fetchone()["cnt"]
            assert count == 0
        finally:
            conn.close()

    def test_supersedes_self_reference_skipped(self, entities_with_decision_log):
        """supersedes自己参照はスキップされる（エラーにならない）"""
        e = entities_with_decision_log
        result = add_relation(
            "decision", e["d1"], [{"type": "decision", "ids": [e["d1"]]}],
            relation_type="supersedes",
        )
        assert "error" not in result
        assert result["added"] == 0

    def test_remove_supersedes_non_decision_rejected(self, entities_with_decision_log):
        """supersedes削除でsource_typeがdecision以外の場合エラーになる"""
        e = entities_with_decision_log
        result = remove_relation(
            "topic", e["t1"], [{"type": "topic", "ids": [e["t2"]]}],
            relation_type="supersedes",
        )
        assert "error" in result
        assert result["error"]["code"] == "INVALID_RELATION_TYPE"

    def test_remove_nonexistent_supersedes(self, entities_with_decision_log):
        """存在しないsupersedesの削除はエラーにならずremoved=0が返る"""
        e = entities_with_decision_log
        result = remove_relation(
            "decision", e["d1"], [{"type": "decision", "ids": [e["d2"]]}],
            relation_type="supersedes",
        )
        assert "error" not in result
        assert result["removed"] == 0


class TestGetMapDecisionLogFilter:
    """get_mapのdecision/logフィルタテスト"""

    def test_get_map_excludes_decision_from_output(self, entities_with_decision_log):
        """decision経由で走査するが、出力にdecisionが含まれない"""
        e = entities_with_decision_log
        # t1 - d1 - t2 のチェーン（t1→d1はrelated、d1→t2もrelated）
        add_relation("topic", e["t1"], [{"type": "decision", "ids": [e["d1"]]}])
        add_relation("decision", e["d1"], [{"type": "topic", "ids": [e["t2"]]}])

        result = get_map("topic", e["t1"], min_depth=0, max_depth=2)

        assert "error" not in result
        types_in_result = {ent["type"] for ent in result["entities"]}
        assert "decision" not in types_in_result
        # t1(depth=0) と t2(depth=2, decision経由) が含まれる
        ids_in_result = {(ent["type"], ent["id"]) for ent in result["entities"]}
        assert ("topic", e["t1"]) in ids_in_result
        assert ("topic", e["t2"]) in ids_in_result

    def test_get_map_excludes_log_from_output(self, entities_with_decision_log):
        """log経由で走査するが、出力にlogが含まれない"""
        e = entities_with_decision_log
        # a1 - l1 - a2 のチェーン
        add_relation("activity", e["a1"], [{"type": "log", "ids": [e["l1"]]}])
        add_relation("activity", e["a2"], [{"type": "log", "ids": [e["l1"]]}])

        result = get_map("activity", e["a1"], min_depth=0, max_depth=2)

        assert "error" not in result
        types_in_result = {ent["type"] for ent in result["entities"]}
        assert "log" not in types_in_result
        # a1(depth=0) と a2(depth=2, log経由) が含まれる
        ids_in_result = {(ent["type"], ent["id"]) for ent in result["entities"]}
        assert ("activity", e["a1"]) in ids_in_result
        assert ("activity", e["a2"]) in ids_in_result

    def test_get_map_decision_traversal_reaches_material(self, entities_with_decision_log):
        """decision経由でmaterialに到達できる"""
        e = entities_with_decision_log
        # t1 - d1 - m1 のチェーン
        add_relation("topic", e["t1"], [{"type": "decision", "ids": [e["d1"]]}])
        add_relation("decision", e["d1"], [{"type": "material", "ids": [e["m1"]]}])

        result = get_map("topic", e["t1"], min_depth=0, max_depth=2)

        assert "error" not in result
        ids_in_result = {(ent["type"], ent["id"]) for ent in result["entities"]}
        assert ("material", e["m1"]) in ids_in_result
        assert ("topic", e["t1"]) in ids_in_result

    def test_get_map_decision_origin_returns_no_decision(self, entities_with_decision_log):
        """decisionを起点にget_mapを実行すると、起点のdecision自体も出力から除外される"""
        e = entities_with_decision_log
        add_relation("decision", e["d1"], [{"type": "topic", "ids": [e["t1"]]}])

        result = get_map("decision", e["d1"], min_depth=0, max_depth=1)

        assert "error" not in result
        types_in_result = {ent["type"] for ent in result["entities"]}
        assert "decision" not in types_in_result
        # depth=1のtopicだけ返る
        ids_in_result = {(ent["type"], ent["id"]) for ent in result["entities"]}
        assert ("topic", e["t1"]) in ids_in_result

    def test_get_map_traverses_supersedes_edge(self, entities_with_decision_log):
        """supersedes経由でdecisionに紐づくtopicに到達できる"""
        e = entities_with_decision_log
        # d2 supersedes d1、d1 related t1 のチェーン
        add_relation(
            "decision", e["d2"], [{"type": "decision", "ids": [e["d1"]]}],
            relation_type="supersedes",
        )
        add_relation("decision", e["d1"], [{"type": "topic", "ids": [e["t1"]]}])

        result = get_map("decision", e["d2"], min_depth=0, max_depth=2)

        assert "error" not in result
        # supersedes経由でd1に到達し、d1→t1のrelatedでt1に到達する
        ids_in_result = {(ent["type"], ent["id"]) for ent in result["entities"]}
        assert ("topic", e["t1"]) in ids_in_result
        # decision自体は出力に含まれない
        types_in_result = {ent["type"] for ent in result["entities"]}
        assert "decision" not in types_in_result


class TestSupersedesPinTransfer:
    """supersedesリレーション追加時のpin自動引き継ぎテスト"""

    def test_target_side_pin_transferred(self, entities_with_decision_log):
        """pin(activity, old_decision) は supersedes(new, old) で pin(activity, new_decision) に付け替わる"""
        e = entities_with_decision_log
        # 旧decisionへpin
        add_pin("activity", e["a1"], "decision", e["d1"])

        # d2 supersedes d1
        result = add_relation(
            "decision", e["d2"], [{"type": "decision", "ids": [e["d1"]]}],
            relation_type="supersedes",
        )
        assert "error" not in result
        assert result["added"] == 1
        assert result.get("pins_transferred") == 1

        # 付け替え確認
        conn = get_connection()
        try:
            old_row = conn.execute(
                "SELECT * FROM pins WHERE target_type='decision' AND target_id=?",
                (e["d1"],),
            ).fetchone()
            new_row = conn.execute(
                "SELECT * FROM pins WHERE target_type='decision' AND target_id=?",
                (e["d2"],),
            ).fetchone()
            assert old_row is None
            assert new_row is not None
            assert new_row["source_type"] == "activity"
            assert new_row["source_id"] == e["a1"]
        finally:
            conn.close()

    def test_source_side_pin_transferred(self, entities_with_decision_log):
        """pin(old_decision, material) は supersedes(new, old) で pin(new_decision, material) に付け替わる"""
        e = entities_with_decision_log
        add_pin("decision", e["d1"], "material", e["m1"])

        result = add_relation(
            "decision", e["d2"], [{"type": "decision", "ids": [e["d1"]]}],
            relation_type="supersedes",
        )
        assert result.get("pins_transferred") == 1

        conn = get_connection()
        try:
            new_row = conn.execute(
                "SELECT * FROM pins WHERE source_type='decision' AND source_id=?",
                (e["d2"],),
            ).fetchone()
            assert new_row is not None
            assert new_row["target_type"] == "material"
            assert new_row["target_id"] == e["m1"]
        finally:
            conn.close()

    def test_both_sides_transferred_simultaneously(self, entities_with_decision_log):
        """target側とsource側のpinが同一add_relationで両方付け替わる"""
        e = entities_with_decision_log
        add_pin("activity", e["a1"], "decision", e["d1"])  # target側
        add_pin("decision", e["d1"], "material", e["m1"])  # source側

        result = add_relation(
            "decision", e["d2"], [{"type": "decision", "ids": [e["d1"]]}],
            relation_type="supersedes",
        )
        assert result.get("pins_transferred") == 2

        conn = get_connection()
        try:
            d1_count = conn.execute(
                "SELECT COUNT(*) AS c FROM pins WHERE "
                "(source_type='decision' AND source_id=?) OR (target_type='decision' AND target_id=?)",
                (e["d1"], e["d1"]),
            ).fetchone()["c"]
            assert d1_count == 0
        finally:
            conn.close()

    def test_created_at_preserved(self, entities_with_decision_log):
        """付け替え後の新pinのcreated_atは旧pinの値から引き継ぐ"""
        e = entities_with_decision_log
        add_pin("activity", e["a1"], "decision", e["d1"])

        conn = get_connection()
        try:
            # 古いcreated_atに書き換える
            conn.execute(
                "UPDATE pins SET created_at='2020-01-01 00:00:00' "
                "WHERE target_type='decision' AND target_id=?",
                (e["d1"],),
            )
            conn.commit()
        finally:
            conn.close()

        add_relation(
            "decision", e["d2"], [{"type": "decision", "ids": [e["d1"]]}],
            relation_type="supersedes",
        )

        conn = get_connection()
        try:
            new_row = conn.execute(
                "SELECT created_at FROM pins WHERE target_type='decision' AND target_id=?",
                (e["d2"],),
            ).fetchone()
            assert new_row["created_at"] == "2020-01-01 00:00:00"
        finally:
            conn.close()

    def test_conflict_merges_via_or_ignore(self, entities_with_decision_log):
        """new側に既存pinがあると衝突マージ（OR IGNORE）され、旧行は削除、pins_transferredは実INSERT数のみ"""
        e = entities_with_decision_log
        add_pin("activity", e["a1"], "decision", e["d1"])  # 旧側
        add_pin("activity", e["a1"], "decision", e["d2"])  # 新側に既存（衝突相手）

        result = add_relation(
            "decision", e["d2"], [{"type": "decision", "ids": [e["d1"]]}],
            relation_type="supersedes",
        )
        # 衝突マージなので実INSERT数は0、pins_transferredキーは付かない
        assert "pins_transferred" not in result

        conn = get_connection()
        try:
            old_count = conn.execute(
                "SELECT COUNT(*) AS c FROM pins WHERE target_type='decision' AND target_id=?",
                (e["d1"],),
            ).fetchone()["c"]
            new_count = conn.execute(
                "SELECT COUNT(*) AS c FROM pins WHERE target_type='decision' AND target_id=?",
                (e["d2"],),
            ).fetchone()["c"]
            assert old_count == 0
            assert new_count == 1
        finally:
            conn.close()

    def test_self_referencing_pin_vanishes(self, entities_with_decision_log):
        """付け替えで自己参照化するpin（pin(new, old) / pin(old, new)）は消滅する"""
        e = entities_with_decision_log
        # pin(d2, d1): supersedes(d2, d1) で pin(d2, d2) になりかけるが除外
        add_pin("decision", e["d2"], "decision", e["d1"])
        # pin(d1, d2): pin(d2, d2) になりかけるが除外
        add_pin("decision", e["d1"], "decision", e["d2"])

        result = add_relation(
            "decision", e["d2"], [{"type": "decision", "ids": [e["d1"]]}],
            relation_type="supersedes",
        )
        # どちらも自己参照化で消えるためpins_transferredキーは付かない
        assert "pins_transferred" not in result

        conn = get_connection()
        try:
            self_ref = conn.execute(
                "SELECT COUNT(*) AS c FROM pins WHERE "
                "source_type='decision' AND source_id=? AND target_type='decision' AND target_id=?",
                (e["d2"], e["d2"]),
            ).fetchone()["c"]
            assert self_ref == 0
            # d1への参照も残らない
            d1_count = conn.execute(
                "SELECT COUNT(*) AS c FROM pins WHERE "
                "(source_type='decision' AND source_id=?) OR (target_type='decision' AND target_id=?)",
                (e["d1"], e["d1"]),
            ).fetchone()["c"]
            assert d1_count == 0
        finally:
            conn.close()

    def test_no_pins_no_op(self, entities_with_decision_log):
        """旧decisionにpinが無い場合 pins_transferred キーが付かない"""
        e = entities_with_decision_log
        result = add_relation(
            "decision", e["d2"], [{"type": "decision", "ids": [e["d1"]]}],
            relation_type="supersedes",
        )
        assert result["added"] == 1
        assert "pins_transferred" not in result

    def test_duplicate_add_does_not_re_fire_hook(self, entities_with_decision_log):
        """重複add（INSERT OR IGNOREで弾かれる）時はpin引き継ぎhookが発火しない"""
        e = entities_with_decision_log
        # 初回supersedesでpin移動を完了させる
        add_pin("activity", e["a1"], "decision", e["d1"])
        result1 = add_relation(
            "decision", e["d2"], [{"type": "decision", "ids": [e["d1"]]}],
            relation_type="supersedes",
        )
        assert result1.get("pins_transferred") == 1

        # この時点で pin(activity, d2) に移動済み。ここで pin(activity, d2) を pin(activity, d1) に戻して、
        # もう一度同じsupersedesを張ってhookが不発火（pins_transferredキー無し）なことを確認する
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE pins SET target_id=? WHERE target_type='decision' AND target_id=?",
                (e["d1"], e["d2"]),
            )
            conn.commit()
        finally:
            conn.close()

        result2 = add_relation(
            "decision", e["d2"], [{"type": "decision", "ids": [e["d1"]]}],
            relation_type="supersedes",
        )
        assert result2["added"] == 0
        assert "pins_transferred" not in result2
        # pin(activity, d1) のまま移動していない
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM pins WHERE target_type='decision' AND target_id=?",
                (e["d1"],),
            ).fetchone()
            assert row is not None
        finally:
            conn.close()

    def test_cycle_rollback_does_not_move_pins(self, entities_with_decision_log):
        """循環検出時のrollbackでpinの移動も巻き戻される"""
        e = entities_with_decision_log
        # 既存: d1 → d2 (d1 supersedes d2)
        add_relation(
            "decision", e["d1"], [{"type": "decision", "ids": [e["d2"]]}],
            relation_type="supersedes",
        )
        add_pin("activity", e["a1"], "decision", e["d2"])

        # d2 supersedes [d3, d1] を同一バッチで張ると、d2→d3 INSERT後にd2→d1で循環検出 → rollback
        result = add_relation(
            "decision", e["d2"],
            [{"type": "decision", "ids": [e["d3"], e["d1"]]}],
            relation_type="supersedes",
        )
        assert "error" in result
        assert result["error"]["code"] == "CIRCULAR_SUPERSEDES"

        # pin(activity, d2) が残っている（移動していない）
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM pins WHERE target_type='decision' AND target_id=?",
                (e["d2"],),
            ).fetchone()
            assert row is not None
        finally:
            conn.close()

    def test_mixed_entity_type_pin_target_side(self, entities_with_decision_log):
        """pin相手側がdecision以外（topic/material/activity）でも正常に付け替わる"""
        e = entities_with_decision_log
        add_pin("topic", e["t1"], "decision", e["d1"])
        add_pin("material", e["m1"], "decision", e["d1"])
        add_pin("activity", e["a1"], "decision", e["d1"])

        result = add_relation(
            "decision", e["d2"], [{"type": "decision", "ids": [e["d1"]]}],
            relation_type="supersedes",
        )
        assert result.get("pins_transferred") == 3

        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT source_type FROM pins WHERE target_type='decision' AND target_id=? "
                "ORDER BY source_type",
                (e["d2"],),
            ).fetchall()
            types = sorted(r["source_type"] for r in rows)
            assert types == ["activity", "material", "topic"]
        finally:
            conn.close()


class TestFKLossImpact:
    """FK制約がないことによる影響テスト"""

    def test_nonexistent_id_relation_excluded_from_get_map(self, entities_with_decision_log):
        """存在しないIDへのリレーション追加後、get_mapで正しく除外される"""
        e = entities_with_decision_log
        # 存在するtopicと存在しないtopicの両方にリレーションを追加
        add_relation("topic", e["t1"], [{"type": "topic", "ids": [99999, e["t2"]]}])

        result = get_map("topic", e["t1"], min_depth=1, max_depth=1)

        assert "error" not in result
        # 存在しないID 99999はカタログに含まれない
        ids_in_result = {(ent["type"], ent["id"]) for ent in result["entities"]}
        assert ("topic", 99999) not in ids_in_result
        # 存在するt2は含まれる
        assert ("topic", e["t2"]) in ids_in_result
