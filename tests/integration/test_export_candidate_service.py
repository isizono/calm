"""collect_export_candidatesの統合テスト（relation走査・タグ・supersede・citation横断）"""
import os
import tempfile

import pytest

from src.db import get_connection, init_database
from src.services.activity_service import add_activity, update_activity
from src.services.export_candidate_service import collect_export_candidates
from src.services.material_service import add_material
from src.services.relation_service import add_relation
from src.services.retract_service import retract
from src.services.tag_service import _injected_tags
from src.services.topic_service import add_topic
from tests.helpers import add_decision, add_log

DEFAULT_TAGS = ["domain:test-export"]


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


def _topic(title="Topic", tags=None):
    result = add_topic(title=title, description=f"Description for {title}", tags=tags or DEFAULT_TAGS)
    assert "error" not in result
    return result["topic_id"]


def _activity(title="Activity", tags=None):
    result = add_activity(title=title, description=f"Description for {title}", tags=tags or DEFAULT_TAGS)
    assert "error" not in result
    return result["activity_id"]


def _material(title="Material", content="Content", tags=None):
    result = add_material(title=title, content=content, tags=tags or DEFAULT_TAGS, source="test")
    assert "error" not in result
    return result["material_id"]


def _decision(topic_id, decision="Decision text", reason="Reason text", tags=None):
    result = add_decision(decision=decision, reason=reason, topic_id=topic_id, tags=tags or DEFAULT_TAGS)
    assert "error" not in result
    return result["decision_id"]


def _log(topic_id, content="Log content", tags=None):
    result = add_log(topic_id=topic_id, content=content, tags=tags or DEFAULT_TAGS)
    assert "error" not in result
    return result["log_id"]


class TestBasicCollection:
    def test_multiple_roots_are_all_reached(self, temp_db):
        """複数起点(roots配列)をそれぞれ深度0のcandidateとして受け付けられる"""
        t1 = _topic("Topic A")
        t2 = _topic("Topic B")

        result = collect_export_candidates(
            roots=[{"type": "topic", "id": t1}, {"type": "topic", "id": t2}],
            max_depth=0,
        )

        assert "error" not in result
        ids = {c["id_raw"] for c in result["candidates"]}
        assert ids == {t1, t2}
        assert result["total_count"] == 2

    def test_decision_and_log_included_independent_of_get_map_contract(self, temp_db):
        """get_mapはdecision/logを経由ノードのみに使うが、本ツールはカタログ本体に含む"""
        t1 = _topic("Topic with children")
        d1 = _decision(t1, decision="Old decision text", reason="Old decision reason")
        l1 = _log(t1, content="Log body content")

        result = collect_export_candidates(
            roots=[{"type": "topic", "id": t1}],
            max_depth=1,
        )

        assert "error" not in result
        types_and_ids = {(c["type"], c["id_raw"]) for c in result["candidates"]}
        assert ("decision", d1) in types_and_ids
        assert ("log", l1) in types_and_ids
        assert ("topic", t1) in types_and_ids

    def test_id_raw_used_not_id(self, temp_db):
        """内部ID表記規約: candidateはid_rawキーを使い、生のidキーは持たない"""
        t1 = _topic("Topic")
        result = collect_export_candidates(roots=[{"type": "topic", "id": t1}], max_depth=0)

        assert "error" not in result
        candidate = result["candidates"][0]
        assert "id_raw" in candidate
        assert "id" not in candidate


class TestTypeSpecificFields:
    def test_retracted_flag_reflected_and_not_filtered_out(self, temp_db):
        """retracted済みエンティティは除外されず、retracted=Trueフラグ付きで候補に残る"""
        t1 = _topic("Topic")
        mat1 = _material("Some Material", content="content body")
        retract("material", [mat1])

        result = collect_export_candidates(
            roots=[{"type": "topic", "id": t1}, {"type": "material", "id": mat1}],
            max_depth=0,
        )

        assert "error" not in result
        material_candidate = next(c for c in result["candidates"] if c["type"] == "material")
        assert material_candidate["retracted"] is True

        topic_candidate = next(c for c in result["candidates"] if c["type"] == "topic")
        assert "retracted" not in topic_candidate

    def test_superseded_flag_only_on_decision(self, temp_db):
        """superseded判定はdecisionのみに付き、他型には付かない"""
        t1 = _topic("Topic")
        old_id = _decision(t1, decision="Old decision text", reason="Old reason")
        new_id = _decision(t1, decision="New decision text", reason="New reason")
        rel = add_relation("decision", new_id, [{"type": "decision", "ids": [old_id]}], relation_type="supersedes")
        assert "error" not in rel

        result = collect_export_candidates(
            roots=[{"type": "topic", "id": t1}],
            max_depth=1,
            include_types=["decision"],
        )

        assert "error" not in result
        by_id = {c["id_raw"]: c for c in result["candidates"]}
        assert by_id[old_id]["superseded"] is True
        assert by_id[new_id]["superseded"] is False

    def test_activity_status_field(self, temp_db):
        """statusはactivityのみに付き、他型には付かない"""
        act1 = _activity("Some Activity")
        update_result = update_activity(act1, status="in_progress")
        assert "error" not in update_result

        result = collect_export_candidates(roots=[{"type": "activity", "id": act1}], max_depth=0)

        assert "error" not in result
        activity_candidate = result["candidates"][0]
        assert activity_candidate["status"] == "in_progress"

        t1 = _topic("Topic")
        result2 = collect_export_candidates(roots=[{"type": "topic", "id": t1}], max_depth=0)
        assert "status" not in result2["candidates"][0]

    def test_parent_topic_title_reported_even_when_topic_not_in_selection(self, temp_db):
        """parent_topic_titleはbelongs_to先topicが候補集合に含まれていなくても報告される"""
        t1 = _topic("Parent Topic")
        d1 = _decision(t1, decision="Some decision text", reason="Some reason")

        result = collect_export_candidates(roots=[{"type": "decision", "id": d1}], max_depth=0)

        assert "error" not in result
        assert len(result["candidates"]) == 1
        assert result["candidates"][0]["parent_topic_title"] == "Parent Topic"
        # topic自体は選択集合外（max_depth=0のため辿られない）
        assert not any(c["type"] == "topic" for c in result["candidates"])


class TestClosureWarnings:
    def test_supersede_target_outside_detected(self, temp_db):
        """supersede先が選択範囲外だとclosure_warningsで検知される"""
        t1 = _topic("Topic")
        old_id = _decision(t1, decision="Old decision text", reason="Old reason")
        new_id = _decision(t1, decision="New decision text", reason="New reason")
        add_relation("decision", new_id, [{"type": "decision", "ids": [old_id]}], relation_type="supersedes")

        result = collect_export_candidates(roots=[{"type": "decision", "id": new_id}], max_depth=0)

        assert "error" not in result
        assert [c["id_raw"] for c in result["candidates"]] == [new_id]
        warnings = [w for w in result["closure_warnings"] if w["kind"] == "supersede_target_outside"]
        assert len(warnings) == 1
        assert warnings[0]["target"] == {"type": "decision", "id_raw": old_id}
        assert warnings[0]["from_title"]

    def test_no_supersede_warning_when_target_in_selection(self, temp_db):
        """supersede先が選択集合内ならclosure_warningsは出ない"""
        t1 = _topic("Topic")
        old_id = _decision(t1, decision="Old decision text", reason="Old reason")
        new_id = _decision(t1, decision="New decision text", reason="New reason")
        add_relation("decision", new_id, [{"type": "decision", "ids": [old_id]}], relation_type="supersedes")

        result = collect_export_candidates(roots=[{"type": "decision", "id": new_id}], max_depth=2)

        assert "error" not in result
        assert not any(w["kind"] == "supersede_target_outside" for w in result["closure_warnings"])

    def test_cite_target_outside_detected(self, temp_db):
        """本文中citationの参照先が選択範囲外だとclosure_warningsで検知される"""
        t1 = _topic("Topic")
        cited = _material("Cited Material", content="referenced body")
        cite_literal = "{{cite:M#" + str(cited) + "}}"
        d1 = _decision(t1, decision="See material", reason=f"詳細は{cite_literal}を参照")

        result = collect_export_candidates(roots=[{"type": "decision", "id": d1}], max_depth=0)

        assert "error" not in result
        warnings = [w for w in result["closure_warnings"] if w["kind"] == "cite_target_outside"]
        assert len(warnings) == 1
        assert warnings[0]["target"] == {"type": "material", "id_raw": cited}
        assert warnings[0]["target_title"] == "Cited Material"

    def test_closure_warnings_computed_regardless_of_include_types_filter(self, temp_db):
        """closure_warningsはinclude_types(表示フィルタ)の影響を受けない"""
        t1 = _topic("Topic")
        old_id = _decision(t1, decision="Old decision text", reason="Old reason")
        new_id = _decision(t1, decision="New decision text", reason="New reason")
        add_relation("decision", new_id, [{"type": "decision", "ids": [old_id]}], relation_type="supersedes")

        result = collect_export_candidates(
            roots=[{"type": "decision", "id": new_id}], max_depth=0, include_types=["topic"]
        )

        assert "error" not in result
        assert result["candidates"] == []
        assert any(w["kind"] == "supersede_target_outside" for w in result["closure_warnings"])


class TestTagRoots:
    def test_tag_roots_seed_without_graph_expansion(self, temp_db):
        """tag_rootsは指定タグの全エンティティを深度0で合流させ、グラフ拡張はしない"""
        seed_tag = ["domain:seed-tag"]
        seed_one = _material("Seed Material One", tags=seed_tag)
        seed_two = _material("Seed Material Two", tags=seed_tag)
        outside_material = _material("Unrelated Material", tags=["domain:other-tag"])
        t1 = _topic("Unrelated Topic", tags=["domain:other-tag"])
        add_relation("material", seed_one, [{"type": "topic", "ids": [t1]}])

        result = collect_export_candidates(tag_roots=seed_tag)

        assert "error" not in result
        pairs = {(c["type"], c["id_raw"]) for c in result["candidates"]}
        assert pairs == {("material", seed_one), ("material", seed_two)}
        assert ("material", outside_material) not in pairs
        assert ("topic", t1) not in pairs
        for c in result["candidates"]:
            assert c["depth"] == 0

    def test_roots_and_tag_roots_merge(self, temp_db):
        """roots(グラフ走査)とtag_roots(タグシード)の結果は合流する"""
        t1 = _topic("Graph Topic")
        seed_tag = ["domain:seed-tag-two"]
        seed_material = _material("Seed Material", tags=seed_tag)

        result = collect_export_candidates(
            roots=[{"type": "topic", "id": t1}], max_depth=0, tag_roots=seed_tag
        )

        assert "error" not in result
        pairs = {(c["type"], c["id_raw"]) for c in result["candidates"]}
        assert pairs == {("topic", t1), ("material", seed_material)}

    def test_co_tags_aggregates_domain_overlap(self, temp_db):
        """co_tagsはtag_roots指定時にシード集合上のdomainタグ共起を集計する"""
        seed_tag = "domain:seed-tag-three"
        mat_one = _material("Mat One", tags=[seed_tag, "domain:co-occurring"])
        mat_two = _material("Mat Two", tags=[seed_tag, "domain:co-occurring"])
        mat_three = _material("Mat Three", tags=[seed_tag])

        result = collect_export_candidates(tag_roots=[seed_tag])

        assert "error" not in result
        assert "co_tags" in result
        co_tag_names = {c["tag"] for c in result["co_tags"]}
        assert "domain:co-occurring" in co_tag_names
        entry = next(c for c in result["co_tags"] if c["tag"] == "domain:co-occurring")
        assert entry["overlap"] == 2
        assert entry["share"] == round(2 / 3, 4)
        assert seed_tag not in co_tag_names

        assert mat_one and mat_two and mat_three  # 生成物を使用したことの明示（lint対策）

    def test_co_tags_absent_when_tag_roots_not_given(self, temp_db):
        """tag_roots未指定時はco_tagsキー自体が付かない"""
        t1 = _topic("Topic")
        result = collect_export_candidates(roots=[{"type": "topic", "id": t1}], max_depth=0)

        assert "error" not in result
        assert "co_tags" not in result


class TestPaginationAndSnippets:
    def test_limit_offset_pagination(self, temp_db):
        """limit/offsetで応答サイズを制御でき、total_count/truncatedが正しい"""
        seed_tag = ["domain:page-tag"]
        letters = ["Alpha", "Beta", "Gamma", "Delta", "Epsilon"]
        for name in letters:
            _material(f"Page Material {name}", tags=seed_tag)

        page1 = collect_export_candidates(tag_roots=seed_tag, limit=2, offset=0)
        assert "error" not in page1
        assert len(page1["candidates"]) == 2
        assert page1["total_count"] == 5
        assert page1["truncated"] is True

        page3 = collect_export_candidates(tag_roots=seed_tag, limit=2, offset=4)
        assert len(page3["candidates"]) == 1
        assert page3["truncated"] is False

    def test_include_snippets_false_omits_snippet_key(self, temp_db):
        """include_snippets=Falseで各candidateからsnippetキーが省かれる"""
        t1 = _topic("Topic")

        with_snippet = collect_export_candidates(roots=[{"type": "topic", "id": t1}], max_depth=0)
        assert "snippet" in with_snippet["candidates"][0]

        without_snippet = collect_export_candidates(
            roots=[{"type": "topic", "id": t1}], max_depth=0, include_snippets=False
        )
        assert "snippet" not in without_snippet["candidates"][0]


class TestValidation:
    def test_no_roots_and_no_tag_roots_is_error(self, temp_db):
        result = collect_export_candidates()
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_invalid_root_entity_type(self, temp_db):
        result = collect_export_candidates(roots=[{"type": "bogus", "id": 1}])
        assert result["error"]["code"] == "INVALID_ENTITY_TYPE"

    def test_root_missing_id_field(self, temp_db):
        result = collect_export_candidates(roots=[{"type": "topic"}])
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_max_depth_out_of_range(self, temp_db):
        t1 = _topic("Topic")
        too_deep = collect_export_candidates(roots=[{"type": "topic", "id": t1}], max_depth=11)
        assert too_deep["error"]["code"] == "INVALID_PARAMETER"

        negative = collect_export_candidates(roots=[{"type": "topic", "id": t1}], max_depth=-1)
        assert negative["error"]["code"] == "INVALID_PARAMETER"

    def test_invalid_include_types_entry(self, temp_db):
        t1 = _topic("Topic")
        result = collect_export_candidates(
            roots=[{"type": "topic", "id": t1}], include_types=["bogus"]
        )
        assert result["error"]["code"] == "INVALID_ENTITY_TYPE"

    def test_invalid_limit_and_offset(self, temp_db):
        t1 = _topic("Topic")
        bad_limit = collect_export_candidates(roots=[{"type": "topic", "id": t1}], limit=0)
        assert bad_limit["error"]["code"] == "INVALID_PARAMETER"

        bad_offset = collect_export_candidates(roots=[{"type": "topic", "id": t1}], offset=-1)
        assert bad_offset["error"]["code"] == "INVALID_PARAMETER"
