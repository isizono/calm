"""check_inのcontrol.decision_candidates（記録役が退避した閉じていない決定候補）のテスト。

仮データはすべて実際の書き込み経路（add_material・add_decision・add_relation・retract）で作る。
"""
import pytest

import src.services.checkin_tier_service as tier
from src.services.activity_service import add_activity
from src.services.checkin_queries import RECORDER_DECISION_CANDIDATE_TAG
from src.services.checkin_tier_service import collect_and_assemble
from src.services.material_service import add_material
from src.services.relation_service import add_relation
from src.services.retract_service import retract
from src.services.topic_service import add_topic
from tests.helpers import add_decision

TAGS = ["domain:test"]
CANDIDATE_TAGS = TAGS + [RECORDER_DECISION_CANDIDATE_TAG]


@pytest.fixture
def activity_id(temp_db):
    return add_activity(title="[作業] 候補確認", description="d", tags=TAGS, check_in=False)["activity_id"]


def _candidate(title, related, tags=CANDIDATE_TAGS):
    result = add_material(title, "本文", tags, "recorder", related=related)
    assert "error" not in result
    return result["material_id"]


def _items(activity_id):
    result = collect_and_assemble(activity_id)
    assert "error" not in result
    return (result["control"].get("decision_candidates") or {}).get("items", [])


def test_unpromoted_candidate_on_activity_is_listed_with_guide(activity_id):
    mid = _candidate("候補A", [{"type": "activity", "ids": [activity_id]}])

    block = collect_and_assemble(activity_id)["control"]["decision_candidates"]

    assert [i["id_raw"] for i in block["items"]] == [mid]
    assert block["items"][0]["title"] == "候補A"
    assert "add_decisions" in block["guide"] and "retract" in block["guide"]
    assert "more" not in block


def test_absent_when_no_candidates(activity_id):
    assert "decision_candidates" not in collect_and_assemble(activity_id)["control"]


def test_candidate_tied_to_decision_is_not_listed(activity_id):
    topic = add_topic(title="T", description="d", tags=TAGS)["topic_id"]
    mid = _candidate("昇格済み", [{"type": "activity", "ids": [activity_id]}])
    other = _candidate("未昇格", [{"type": "activity", "ids": [activity_id]}])
    did = add_decision(decision="決定", reason="r", topic_id=topic)["decision_id"]
    add_relation("material", mid, [{"type": "decision", "ids": [did]}])

    assert [i["id_raw"] for i in _items(activity_id)] == [other]


def test_retracted_candidate_is_not_listed(activity_id):
    mid = _candidate("却下", [{"type": "activity", "ids": [activity_id]}])
    assert _items(activity_id)
    assert "error" not in retract("material", [mid])

    assert _items(activity_id) == []


def test_candidate_via_activity_topic_is_listed(activity_id):
    topic = add_topic(title="T", description="d", tags=TAGS)["topic_id"]
    add_relation("activity", activity_id, [{"type": "topic", "ids": [topic]}])
    mid = _candidate("topic経由", [{"type": "topic", "ids": [topic]}])

    assert [i["id_raw"] for i in _items(activity_id)] == [mid]


def test_candidate_on_unrelated_topic_or_without_tag_is_not_listed(activity_id):
    other_topic = add_topic(title="別", description="d", tags=TAGS)["topic_id"]
    _candidate("無関係topic", [{"type": "topic", "ids": [other_topic]}])
    _candidate("タグ無し", [{"type": "activity", "ids": [activity_id]}], tags=TAGS)

    assert _items(activity_id) == []


def test_overflow_folds_into_count_and_titles_are_cut(activity_id):
    n = tier.DECISION_CANDIDATES_MAX + 2
    for i in range(n):
        _candidate(f"候補{i}", [{"type": "activity", "ids": [activity_id]}])

    block = collect_and_assemble(activity_id)["control"]["decision_candidates"]

    assert len(block["items"]) == tier.DECISION_CANDIDATES_MAX
    assert block["items"][0]["title"] == f"候補{n - 1}"  # 新しい順
    assert block["more"] == 2
    assert block["next"][0]["tool"] == "get_timeline"


def test_control_stays_within_cap_with_max_candidates(activity_id):
    from src.config import CHECKIN_CONTROL_CAP_CHARS
    from src.services import response_budget as rb

    for i in range(tier.DECISION_CANDIDATES_MAX + 5):
        _candidate(("長" * 35) + str(i), [{"type": "activity", "ids": [activity_id]}])

    control = collect_and_assemble(activity_id)["control"]

    assert rb.measure_chars(control) < CHECKIN_CONTROL_CAP_CHARS
