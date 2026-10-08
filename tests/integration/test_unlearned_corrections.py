"""未教訓化（記録役が積んだ人の訂正）の持ち越しと解消のテスト。

仮データはすべて実際の書き込み経路（add_material・add_logs・add_relation・retract）で作る。
"""
import pytest

from src.db import get_connection
from src.services.activity_service import add_activity
from src.services.checkin_tier_service import collect_and_assemble
from src.services.correction_service import (
    LESSON_DELIVERY_TAG,
    LESSON_OBSERVED_TAG,
    LESSON_UNOBSERVABLE_TAG,
    SAME_TYPE_CHECKED_TAG,
    UNLEARNED_CORRECTION_TAG,
    correction_stats,
)
from src.services.discussion_log_service import add_logs
from src.services.material_service import add_material
from src.services.relation_service import add_relation
from src.services.retract_service import retract
from src.services.topic_service import add_topic

TAGS = ["domain:test"]


@pytest.fixture
def activity_id(temp_db):
    return add_activity(title="[作業] 訂正確認", description="d", tags=TAGS, check_in=False)["activity_id"]


def _material(title, tags, related):
    result = add_material(title, "本文", TAGS + tags, "recorder", related=related)
    assert "error" not in result
    return result["material_id"]


def _correction(title, activity_id):
    return _material(title, [UNLEARNED_CORRECTION_TAG], [{"type": "activity", "ids": [activity_id]}])


def _block(activity_id):
    return collect_and_assemble(activity_id)["control"].get("unlearned_corrections")


def test_correction_on_activity_is_carried_with_guide(activity_id):
    mid = _correction("未教訓化: 予告で止まる", activity_id)

    block = _block(activity_id)

    assert block["items"] == [{"id_raw": mid, "title": "未教訓化: 予告で止まる", "delivered": False, "observed": False}]
    assert "lesson-delivery" in block["guide"] and "lesson-observed" in block["guide"]


def test_correction_on_topic_of_activity_is_carried(activity_id):
    topic = add_topic(title="T", description="d", tags=TAGS)["topic_id"]
    add_relation("activity", activity_id, [{"type": "topic", "ids": [topic]}])
    mid = _material("未教訓化: topic側", [UNLEARNED_CORRECTION_TAG], [{"type": "topic", "ids": [topic]}])

    assert [i["id_raw"] for i in _block(activity_id)["items"]] == [mid]


def test_delivery_alone_keeps_it_carried(activity_id):
    mid = _correction("未教訓化: A", activity_id)
    _material("届け先: A", [LESSON_DELIVERY_TAG], [{"type": "material", "ids": [mid]}])

    assert _block(activity_id)["items"][0]["delivered"] is True
    assert _block(activity_id)["items"][0]["observed"] is False


def test_delivery_and_observation_resolve_it(activity_id):
    topic = add_topic(title="T", description="d", tags=TAGS)["topic_id"]
    mid = _correction("未教訓化: A", activity_id)
    other = _correction("未教訓化: B", activity_id)
    log = add_logs([{"topic_id": topic, "content": "届け先はorchタグのnotes", "tags": TAGS + [LESSON_DELIVERY_TAG]}])
    add_relation("log", log["created"][0]["log_id"], [{"type": "material", "ids": [mid]}])
    _material("観測: A", [LESSON_OBSERVED_TAG], [{"type": "material", "ids": [mid]}])

    assert [i["id_raw"] for i in _block(activity_id)["items"]] == [other]


def test_retracted_delivery_does_not_count(activity_id):
    mid = _correction("未教訓化: A", activity_id)
    delivery = _material("届け先: A", [LESSON_DELIVERY_TAG], [{"type": "material", "ids": [mid]}])
    _material("観測: A", [LESSON_OBSERVED_TAG], [{"type": "material", "ids": [mid]}])
    assert _block(activity_id) is None

    retract("material", [delivery])

    assert [i["id_raw"] for i in _block(activity_id)["items"]] == [mid]


def test_oldest_first_and_more(activity_id):
    ids = [_correction(f"未教訓化: {n}", activity_id) for n in range(5)]

    block = _block(activity_id)

    assert [i["id_raw"] for i in block["items"]] == ids[:3]
    assert block["more"] == 2


def test_stats_report_first_delivery_and_same_type(activity_id):
    first = _correction("未教訓化: 型X 1回目", activity_id)
    second = _correction("未教訓化: 型X 2回目", activity_id)
    add_relation("material", second, [{"type": "material", "ids": [first]}])
    _material("届け先", [LESSON_DELIVERY_TAG], [{"type": "material", "ids": [first]}])

    conn = get_connection(load_vec=False)
    try:
        stats = {s["id"]: s for s in correction_stats(conn)}
    finally:
        conn.close()

    assert stats[first]["delivered_at"] is not None and stats[first]["observed_at"] is None
    assert stats[first]["same_type_of"] == []
    assert stats[second]["same_type_of"] == [first]
    assert stats[second]["delivered_at"] is None


def test_unobservable_delivery_leaves_list_but_is_not_observed(activity_id):
    mid = _correction("未教訓化: skillに書いた", activity_id)
    _material("届け先: skill", [LESSON_DELIVERY_TAG, LESSON_UNOBSERVABLE_TAG], [{"type": "material", "ids": [mid]}])

    assert _block(activity_id) is None
    conn = get_connection(load_vec=False)
    try:
        stat = next(s for s in correction_stats(conn) if s["id"] == mid)
    finally:
        conn.close()
    assert stat["unobservable"] is True and stat["observed_at"] is None


def test_same_type_pending_until_checked(activity_id):
    first = _correction("未教訓化: 型X 1回目", activity_id)
    second = _correction("未教訓化: 型X 2回目", activity_id)
    lone = _correction("未教訓化: 型Y", activity_id)
    add_relation("material", second, [{"type": "material", "ids": [first]}])

    assert [i["id_raw"] for i in _block(activity_id)["same_type_pending"]] == [first, second]
    assert lone not in [i["id_raw"] for i in _block(activity_id)["same_type_pending"]]

    _material("同型の点検: 型X", [SAME_TYPE_CHECKED_TAG], [{"type": "material", "ids": [first, second]}])

    assert "same_type_pending" not in _block(activity_id)


def test_recurrence_of_resolved_type_is_flagged(activity_id):
    old = _correction("未教訓化: 型X 1回目", activity_id)
    _material("届け先", [LESSON_DELIVERY_TAG], [{"type": "material", "ids": [old]}])
    _material("観測", [LESSON_OBSERVED_TAG], [{"type": "material", "ids": [old]}])
    new = _correction("未教訓化: 型X 再来", activity_id)
    add_relation("material", new, [{"type": "material", "ids": [old]}])
    unrelated = _correction("未教訓化: 型Y", activity_id)

    items = {i["id_raw"]: i for i in _block(activity_id)["items"]}

    assert items[new].get("recurred") is True
    assert "recurred" not in items[unrelated]
    assert old not in items
