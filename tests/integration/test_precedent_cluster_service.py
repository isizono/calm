"""precedent_cluster_service.expand_decision_cluster の統合テスト。

supersede 閉包（topic 境界越え・retract メンバー保持）、depth-1 の related / citation
エッジ展開、拡張ノードのみの retract フィルタ、予算超過時の catalog_overflow 降格、
edges の via 区別、membership の複数該当を検証する。
"""
import os
import tempfile

import pytest

from src.db import get_connection, init_database
from src.services.material_service import add_material
from src.services.precedent_cluster_service import expand_decision_cluster
from src.services.relation_service import add_relation
from src.services.retract_service import retract
from src.services.tag_service import _injected_tags
from src.services.topic_service import add_topic
from tests.helpers import add_decision

DEFAULT_TAGS = ["domain:test"]


@pytest.fixture
def temp_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture
def topic_id(temp_db):
    return add_topic(title="判例クラスタ展開テスト", description="Desc", tags=DEFAULT_TAGS)["topic_id"]


def _decision(topic_id: int, decision: str = "d", reason: str = "r") -> int:
    result = add_decision(decision=decision, reason=reason, topic_id=topic_id, tags=DEFAULT_TAGS)
    return result["decision_id"]


def _material(title: str, content: str = "内容") -> int:
    result = add_material(title=title, content=content, tags=DEFAULT_TAGS, source="test")
    return result["material_id"]


def _link_supersede(newer_id: int, older_id: int) -> None:
    result = add_relation(
        "decision", newer_id, [{"type": "decision", "ids": [older_id]}], relation_type="supersedes"
    )
    assert "error" not in result, result


def _link_related(source_type: str, source_id: int, target_type: str, target_id: int) -> None:
    result = add_relation(source_type, source_id, [{"type": target_type, "ids": [target_id]}])
    assert "error" not in result, result


class TestEmptySeed:
    def test_empty_seed_returns_empty_structure(self, temp_db):
        conn = get_connection()
        try:
            result = expand_decision_cluster(conn, [])
        finally:
            conn.close()
        assert result == {
            "decisions": [],
            "materials": [],
            "edges": [],
            "catalog_overflow": [],
            "excluded_retracted": 0,
            "truncated": False,
        }


class TestSupersedeClosure:
    def test_closure_crosses_topic_boundary(self, temp_db):
        """supersede chain は topic をまたいでも辿られる"""
        topic_a = add_topic(title="topic-a", description="d", tags=DEFAULT_TAGS)["topic_id"]
        topic_b = add_topic(title="topic-b", description="d", tags=DEFAULT_TAGS)["topic_id"]
        d_old = _decision(topic_a, "old", "r")
        d_new = _decision(topic_b, "new", "r")
        _link_supersede(d_new, d_old)

        conn = get_connection()
        try:
            result = expand_decision_cluster(conn, [d_old])
        finally:
            conn.close()

        ids = {d["id_raw"] for d in result["decisions"]}
        assert ids == {d_old, d_new}

        by_id = {d["id_raw"]: d for d in result["decisions"]}
        assert by_id[d_old]["membership"] == ["seed"]
        assert by_id[d_new]["membership"] == ["supersede"]

        assert {"source": f"decision:{d_new}", "target": f"decision:{d_old}", "via": "supersedes"} in result["edges"]

    def test_retracted_chain_member_included_with_flag(self, temp_db, topic_id):
        """retract 済み chain メンバーは除外されず is_retracted=true で含まれる"""
        d_old = _decision(topic_id, "old", "r")
        d_new = _decision(topic_id, "new", "r")
        _link_supersede(d_new, d_old)
        retract("decision", [d_old])

        conn = get_connection()
        try:
            result = expand_decision_cluster(conn, [d_new])
        finally:
            conn.close()

        by_id = {d["id_raw"]: d for d in result["decisions"]}
        assert d_old in by_id
        assert by_id[d_old]["is_retracted"] is True
        assert result["excluded_retracted"] == 0


class TestExpansionRetractFilter:
    def test_retracted_expansion_nodes_excluded_and_counted(self, temp_db, topic_id):
        """拡張（related）で到達した retract 済み decision/material は除外され計数される"""
        d1 = _decision(topic_id, "seed", "r")
        d2 = _decision(topic_id, "related-decision", "r")
        m1 = _material("related-material")
        _link_related("decision", d1, "decision", d2)
        _link_related("decision", d1, "material", m1)

        retract("decision", [d2])
        retract("material", [m1])

        conn = get_connection()
        try:
            result = expand_decision_cluster(conn, [d1])
        finally:
            conn.close()

        decision_ids = {d["id_raw"] for d in result["decisions"]}
        material_ids = {m["id_raw"] for m in result["materials"]}
        assert d2 not in decision_ids
        assert m1 not in material_ids
        assert result["excluded_retracted"] == 2


class TestRelatedDepthOne:
    def test_related_does_not_extend_beyond_depth_one(self, temp_db, topic_id):
        """related エッジは depth 1 のみ辿り、depth 2 には伸びない"""
        d1 = _decision(topic_id, "d1", "r")
        d2 = _decision(topic_id, "d2", "r")
        d3 = _decision(topic_id, "d3", "r")
        _link_related("decision", d1, "decision", d2)
        _link_related("decision", d2, "decision", d3)

        conn = get_connection()
        try:
            result = expand_decision_cluster(conn, [d1])
        finally:
            conn.close()

        decision_ids = {d["id_raw"] for d in result["decisions"]}
        assert d1 in decision_ids
        assert d2 in decision_ids
        assert d3 not in decision_ids

        by_id = {d["id_raw"]: d for d in result["decisions"]}
        assert by_id[d2]["membership"] == ["related"]


class TestCitationBothDirections:
    def test_forward_and_backward_citation_are_both_collected(self, temp_db, topic_id):
        """citation の順方向 (seed が cite する material) と逆方向 (seed を cite する decision) が両方拾われる"""
        m1 = _material("cited-material")

        # 順方向: d1 が m1 を cite する
        d1_created = add_decision(
            decision="採用",
            reason=f"理由本文 {{{{cite:M#{m1}}}}}",
            topic_id=topic_id,
            tags=DEFAULT_TAGS,
        )
        d1 = d1_created["decision_id"]

        # 逆方向: d2 が d1 を cite する
        d2 = _decision(topic_id, "citer", f"引用元 {{{{cite:D#{d1}}}}}")

        conn = get_connection()
        try:
            result = expand_decision_cluster(conn, [d1])
        finally:
            conn.close()

        material_ids = {m["id_raw"] for m in result["materials"]}
        assert m1 in material_ids
        by_material = {m["id_raw"]: m for m in result["materials"]}
        assert by_material[m1]["membership"] == ["cited"]

        decision_ids = {d["id_raw"] for d in result["decisions"]}
        assert d2 in decision_ids
        by_decision = {d["id_raw"]: d for d in result["decisions"]}
        assert by_decision[d2]["membership"] == ["cited"]

        assert {"source": f"decision:{d1}", "target": f"material:{m1}", "via": "citation"} in result["edges"]
        assert {"source": f"decision:{d2}", "target": f"decision:{d1}", "via": "citation"} in result["edges"]


class TestMaterialTags:
    def test_material_output_includes_tags(self, temp_db, topic_id):
        """展開で到達した material は紐づくタグ一覧を tags フィールドに含む"""
        d1 = _decision(topic_id, "seed", "r")
        m1 = add_material(
            title="tagged-material", content="内容", tags=["domain:test", "precedent"], source="test"
        )["material_id"]
        _link_related("decision", d1, "material", m1)

        conn = get_connection()
        try:
            result = expand_decision_cluster(conn, [d1])
        finally:
            conn.close()

        by_material = {m["id_raw"]: m for m in result["materials"]}
        assert m1 in by_material
        assert by_material[m1]["tags"] == ["domain:test", "precedent"]


class TestBudgetOverflow:
    def test_expansion_budget_demotes_overflow_to_catalog(self, temp_db, topic_id):
        """拡張ノードが予算を超えたら超過分が catalog_overflow に降格し truncated=true になる。
        supersede 閉包（seed 自身）は予算の対象外。"""
        d1 = _decision(topic_id, "seed", "r")
        material_ids = [_material(f"material-{i}") for i in range(8)]
        for mid in material_ids:
            _link_related("decision", d1, "material", mid)

        conn = get_connection()
        try:
            result = expand_decision_cluster(conn, [d1], max_expansion_nodes=5)
        finally:
            conn.close()

        assert result["truncated"] is True
        assert len(result["materials"]) == 5
        assert len(result["catalog_overflow"]) == 3
        # 総件数が保存される（silent drop なし）
        assert len(result["materials"]) + len(result["catalog_overflow"]) == len(material_ids)
        # seed 自身は予算の対象外で常に含まれる
        assert {d["id_raw"] for d in result["decisions"]} == {d1}

        overflow_entry = result["catalog_overflow"][0]
        assert overflow_entry["type"] == "material"
        assert "id_raw" in overflow_entry
        assert "title" in overflow_entry
        assert "content" not in overflow_entry
        assert "snippet" not in overflow_entry

    def test_supersede_closure_always_returned_in_full_despite_budget(self, temp_db, topic_id):
        """supersede 閉包は max_expansion_nodes に関わらず全件返る"""
        d_old = _decision(topic_id, "old", "r")
        d_new = _decision(topic_id, "new", "r")
        _link_supersede(d_new, d_old)

        conn = get_connection()
        try:
            result = expand_decision_cluster(conn, [d_old], max_expansion_nodes=0)
        finally:
            conn.close()

        assert {d["id_raw"] for d in result["decisions"]} == {d_old, d_new}


class TestEdgesDistinguishVia:
    def test_edges_distinguish_supersedes_related_citation(self, temp_db, topic_id):
        d_old = _decision(topic_id, "old", "r")
        d_new_created = add_decision(
            decision="new",
            reason=f"理由 {{{{cite:D#{d_old}}}}}",
            topic_id=topic_id,
            tags=DEFAULT_TAGS,
        )
        d_new = d_new_created["decision_id"]
        _link_supersede(d_new, d_old)

        m1 = _material("related-material")
        _link_related("decision", d_old, "material", m1)

        conn = get_connection()
        try:
            result = expand_decision_cluster(conn, [d_new])
        finally:
            conn.close()

        vias = {e["via"] for e in result["edges"]}
        assert vias == {"supersedes", "related", "citation"}

        supersede_edges = [e for e in result["edges"] if e["via"] == "supersedes"]
        assert {"source": f"decision:{d_new}", "target": f"decision:{d_old}", "via": "supersedes"} in supersede_edges

        related_edges = [e for e in result["edges"] if e["via"] == "related"]
        assert {"source": f"decision:{d_old}", "target": f"material:{m1}", "via": "related"} in related_edges

        citation_edges = [e for e in result["edges"] if e["via"] == "citation"]
        assert {"source": f"decision:{d_new}", "target": f"decision:{d_old}", "via": "citation"} in citation_edges


class TestMembershipMultipleValues:
    def test_membership_accumulates_multiple_reasons(self, temp_db, topic_id):
        """supersede 閉包メンバーが related エッジでも到達される場合、membership は両方を含む"""
        d1 = _decision(topic_id, "seed", "r")
        d2 = _decision(topic_id, "chain-member", "r")
        _link_supersede(d2, d1)
        _link_related("decision", d1, "decision", d2)

        conn = get_connection()
        try:
            result = expand_decision_cluster(conn, [d1])
        finally:
            conn.close()

        by_id = {d["id_raw"]: d for d in result["decisions"]}
        assert by_id[d2]["membership"] == ["supersede", "related"]


class TestIncludeBodies:
    def test_include_bodies_false_omits_text_fields(self, temp_db, topic_id):
        d1 = _decision(topic_id, "seed", "r")

        conn = get_connection()
        try:
            result = expand_decision_cluster(conn, [d1], include_bodies=False)
        finally:
            conn.close()

        item = result["decisions"][0]
        assert "decision" not in item
        assert "reason" not in item
        assert item["id_raw"] == d1


class TestDecisionPayloadFields:
    def test_returned_decision_has_superseded_by_and_precedent(self, temp_db, topic_id):
        d_old = _decision(topic_id, "old", "r")
        d_new = _decision(topic_id, "new", "却下案:\n- 案A: 理由A\n\n検証: 実機確認 / 2026-07-04\n")
        _link_supersede(d_new, d_old)

        conn = get_connection()
        try:
            result = expand_decision_cluster(conn, [d_old])
        finally:
            conn.close()

        by_id = {d["id_raw"]: d for d in result["decisions"]}
        assert by_id[d_old]["superseded_by"] == d_new
        assert by_id[d_new]["superseded_by"] is None
        assert by_id[d_new]["precedent"] == {
            "rejected_alternatives": 1,
            "scope": False,
            "verification_anchors": ["実機確認 / 2026-07-04"],
            "adjacent_check": [],
        }
        assert "precedent" not in by_id[d_old]
