"""destabilization読み出し4経路（get_decisions / pull_precedents / get_by_ids /
check_in のpinned decision）の統合テスト。

decision_supersedes に kind='destabilizes' で張られたエッジが、未resolveの間は
4経路全てで一貫して destabilization セクションとして見え、resolve後は4経路全てから
キーが消えることを検証する。あわせて、kind='replaces' 由来の is_superseded /
supersede_chain が destabilizes エッジの有無に影響されないこと（TODO1の非破壊確認）も
検証する。
"""
import os
import tempfile

import pytest

from src.db import init_database
from src.services.activity_service import add_activity
from src.services.checkin_service import check_in
from src.services.decision_service import get_decisions
from src.services.destabilization_service import resolve_destabilization
from src.services.pin_service import add_pin
from src.services.precedent_pull_service import pull_precedents
from src.services.relation_service import add_relation
from src.services.search_service import get_by_ids
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
    t = add_topic(title="destabilization読み出しテスト", description="Desc", tags=DEFAULT_TAGS)
    return t["topic_id"]


def _link_destabilizes(source_id: int, target_id: int) -> None:
    """source が target を destabilize するリレーションを張る。"""
    result = add_relation(
        "decision",
        source_id,
        [{"type": "decision", "ids": [target_id]}],
        relation_type="destabilizes",
    )
    assert "error" not in result, result


def _link_supersede(newer_id: int, older_id: int) -> None:
    """newer が older を supersede するリレーションを張る。"""
    result = add_relation(
        "decision",
        newer_id,
        [{"type": "decision", "ids": [older_id]}],
        relation_type="supersedes",
    )
    assert "error" not in result, result


def _find_decision_item(items: list[dict], decision_id: int) -> dict:
    """id_raw が decision_id に一致する item を探す（見つからなければ AssertionError）。"""
    for item in items:
        if item.get("id_raw") == decision_id:
            return item
    raise AssertionError(f"decision {decision_id} not found in {items}")


def _get_decisions_item(topic_id: int, decision_id: int) -> dict:
    result = get_decisions("topic", topic_id)
    assert "error" not in result, result
    return _find_decision_item(result["decisions"], decision_id)


def _pull_precedents_item(topic_id: int, decision_id: int) -> dict:
    result = pull_precedents("destabilization読み出しテスト", topic_ids=[topic_id])
    assert "error" not in result, result
    assert result["guarantee"] == "enumerated"
    decisions = result["topics"][0]["decisions"]
    return _find_decision_item(decisions, decision_id)


def _get_by_ids_item(decision_id: int) -> dict:
    result = get_by_ids([{"type": "decision", "id": decision_id}])
    assert "error" not in result, result
    entry = result["results"][0]
    assert "error" not in entry, entry
    return entry["data"]


def _check_in_pinned_item(activity_id: int, decision_id: int) -> dict:
    result = check_in(activity_id)
    assert "error" not in result, result
    pinned_decisions = result.get("pinned", {}).get("decisions", [])
    return _find_decision_item(pinned_decisions, decision_id)


class TestDestabilizationCrossPathConsistency:
    """4経路（get_decisions/pull_precedents/get_by_ids/check_in pinned）の一貫性"""

    @pytest.fixture
    def scenario(self, topic_id):
        """軸変更decision(source)がtarget decisionをdestabilizeし、targetをpinしたactivity"""
        source = add_decision(decision="軸変更", reason="軸変更の理由", topic_id=topic_id)
        target = add_decision(decision="影響先", reason="影響先の理由", topic_id=topic_id)
        _link_destabilizes(source["decision_id"], target["decision_id"])

        activity = add_activity(
            title="[作業] destabilization確認",
            description="pinned decisionのdestabilization表示を確認する",
            tags=DEFAULT_TAGS,
            check_in=False,
        )
        add_pin("activity", activity["activity_id"], "decision", target["decision_id"])

        return {
            "topic_id": topic_id,
            "source_id": source["decision_id"],
            "target_id": target["decision_id"],
            "activity_id": activity["activity_id"],
        }

    def test_get_decisions_shows_destabilization(self, scenario):
        item = _get_decisions_item(scenario["topic_id"], scenario["target_id"])
        assert item["destabilization"]["unresolved_count"] == 1
        assert item["destabilization"]["destabilized_by"] == [scenario["source_id"]]

    def test_pull_precedents_shows_destabilization(self, scenario):
        item = _pull_precedents_item(scenario["topic_id"], scenario["target_id"])
        assert item["destabilization"]["unresolved_count"] == 1
        assert item["destabilization"]["destabilized_by"] == [scenario["source_id"]]

    def test_get_by_ids_shows_destabilization(self, scenario):
        item = _get_by_ids_item(scenario["target_id"])
        assert item["destabilization"]["unresolved_count"] == 1
        assert item["destabilization"]["destabilized_by"] == [scenario["source_id"]]

    def test_check_in_pinned_shows_destabilization(self, scenario):
        item = _check_in_pinned_item(scenario["activity_id"], scenario["target_id"])
        assert item["destabilization"]["unresolved_count"] == 1
        assert item["destabilization"]["destabilized_by"] == [scenario["source_id"]]

    def test_get_decisions_activity_entity_type_shows_destabilization(self, scenario):
        """get_decisionsのentity_type="activity"分岐（related topics経由でDISTINCT集約する
        別クエリ経路）でも、entity_type="topic"分岐と同様にdestabilizationが付与される"""
        add_relation(
            "activity",
            scenario["activity_id"],
            [{"type": "topic", "ids": [scenario["topic_id"]]}],
        )
        result = get_decisions("activity", scenario["activity_id"])
        assert "error" not in result, result
        item = _find_decision_item(result["decisions"], scenario["target_id"])
        assert item["destabilization"]["unresolved_count"] == 1
        assert item["destabilization"]["destabilized_by"] == [scenario["source_id"]]

    def test_all_four_paths_agree_on_sources_kind_reason(self, scenario):
        """4経路のdestabilization.sources[0].kind_reasonが同一の値を返す（一貫性）"""
        items = [
            _get_decisions_item(scenario["topic_id"], scenario["target_id"]),
            _pull_precedents_item(scenario["topic_id"], scenario["target_id"]),
            _get_by_ids_item(scenario["target_id"]),
            _check_in_pinned_item(scenario["activity_id"], scenario["target_id"]),
        ]
        kind_reasons = {item["destabilization"]["sources"][0]["kind_reason"] for item in items}
        assert kind_reasons == {"軸変更の理由"}

    def test_no_destabilization_edge_omits_key_in_all_four_paths(self, scenario):
        """destabilizesエッジが無いdecisionは4経路いずれもdestabilizationキーが付かない"""
        untouched = add_decision(decision="無関係", reason="r", topic_id=scenario["topic_id"])
        add_pin("activity", scenario["activity_id"], "decision", untouched["decision_id"])

        assert "destabilization" not in _get_decisions_item(
            scenario["topic_id"], untouched["decision_id"]
        )
        assert "destabilization" not in _pull_precedents_item(
            scenario["topic_id"], untouched["decision_id"]
        )
        assert "destabilization" not in _get_by_ids_item(untouched["decision_id"])
        assert "destabilization" not in _check_in_pinned_item(
            scenario["activity_id"], untouched["decision_id"]
        )

    def test_resolve_removes_destabilization_key_from_all_four_paths(self, scenario):
        """resolve_destabilizationで解消すると4経路全てからdestabilizationキーが消える"""
        resolve_result = resolve_destabilization(
            scenario["source_id"], scenario["target_id"], "reaffirmed"
        )
        assert "error" not in resolve_result

        assert "destabilization" not in _get_decisions_item(
            scenario["topic_id"], scenario["target_id"]
        )
        assert "destabilization" not in _pull_precedents_item(
            scenario["topic_id"], scenario["target_id"]
        )
        assert "destabilization" not in _get_by_ids_item(scenario["target_id"])
        assert "destabilization" not in _check_in_pinned_item(
            scenario["activity_id"], scenario["target_id"]
        )


class TestDestabilizationIndependentFromSupersede:
    """is_superseded(replaces由来)とdestabilization(destabilizes由来)の独立併記"""

    def test_get_decisions_shows_both_independently(self, topic_id):
        """同一decisionにreplacesとdestabilizesの両エッジが張られても、
        is_superseded/supersede_chainとdestabilizationは独立して両方見える"""
        replaces_source = add_decision(decision="改訂後", reason="r", topic_id=topic_id)
        axis_change = add_decision(decision="軸変更", reason="r", topic_id=topic_id)
        target = add_decision(decision="影響先", reason="r", topic_id=topic_id)
        _link_supersede(replaces_source["decision_id"], target["decision_id"])
        _link_destabilizes(axis_change["decision_id"], target["decision_id"])

        item = _get_decisions_item(topic_id, target["decision_id"])

        assert item["is_superseded"] is True
        assert replaces_source["decision_id"] in item["supersede_chain"]
        assert item["destabilization"]["unresolved_count"] == 1
        assert item["destabilization"]["destabilized_by"] == [axis_change["decision_id"]]
