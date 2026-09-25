"""delta_service: derive_scope / get_baseline / compute_delta のユニットテスト

temp_db / disable_embedding フィクスチャは tests/conftest.py で共有。
"""
import pytest

from src.db import get_connection
from src.services.activity_service import add_activity
from src.services.material_service import add_material
from src.services.relation_service import add_relation
from src.services.retract_service import retract
from src.services.topic_service import add_topic
from src.services.delta_service import compute_delta, derive_scope, get_baseline
from tests.helpers import add_decision, add_log


@pytest.fixture(autouse=True)
def _auto_disable_embedding(disable_embedding):
    """このファイル内の全テストでembedding呼び出しを無効化する"""


@pytest.fixture
def scope_topic(temp_db):
    """スコープ内のtopicを1件作成する"""
    result = add_topic(title="Scope Topic", description="in scope", tags=["domain:test"])
    return result["topic_id"]


@pytest.fixture
def other_topic(temp_db):
    """スコープ外のtopicを1件作成する"""
    result = add_topic(title="Other Topic", description="out of scope", tags=["domain:test"])
    return result["topic_id"]


def test_derive_scope_includes_direct_and_board_tagged_related_topics(temp_db, scope_topic):
    """activityから1段のtopicと、それに関連するboard素タグ付きtopicの和になることを
    確認する。boardタグの無い関連topicは含まれない。
    """
    activity_result = add_activity(
        title="Scope Activity", description="d", tags=["domain:test"],
        related=[{"type": "topic", "ids": [scope_topic]}], check_in=False,
    )
    activity_id = activity_result["activity_id"]

    board_topic = add_topic(title="Board Topic", description="d", tags=["domain:test", "board"])
    board_tid = board_topic["topic_id"]
    add_relation("topic", scope_topic, [{"type": "topic", "ids": [board_tid]}])

    plain_topic = add_topic(title="Plain Related Topic", description="d", tags=["domain:test"])
    plain_tid = plain_topic["topic_id"]
    add_relation("topic", scope_topic, [{"type": "topic", "ids": [plain_tid]}])

    conn = get_connection()
    try:
        scope = derive_scope(conn, activity_id)
    finally:
        conn.close()

    assert set(scope) == {scope_topic, board_tid}


def test_derive_scope_does_not_extend_through_chained_board_topics(temp_db, scope_topic):
    """boardタグ付きtopicがさらに別のboardタグ付きtopicと関連していても、
    3段目のtopicはスコープに含まれないことを確認する（2段構成の境界）。
    """
    activity_result = add_activity(
        title="Scope Activity", description="d", tags=["domain:test"],
        related=[{"type": "topic", "ids": [scope_topic]}], check_in=False,
    )
    activity_id = activity_result["activity_id"]

    board_topic = add_topic(title="Board Topic", description="d", tags=["domain:test", "board"])
    board_tid = board_topic["topic_id"]
    add_relation("topic", scope_topic, [{"type": "topic", "ids": [board_tid]}])

    chained_board_topic = add_topic(
        title="Chained Board Topic", description="d", tags=["domain:test", "board"],
    )
    chained_board_tid = chained_board_topic["topic_id"]
    add_relation("topic", board_tid, [{"type": "topic", "ids": [chained_board_tid]}])

    conn = get_connection()
    try:
        scope = derive_scope(conn, activity_id)
    finally:
        conn.close()

    assert set(scope) == {scope_topic, board_tid}
    assert chained_board_tid not in scope


def test_derive_scope_returns_empty_for_activity_without_related_topics(temp_db):
    activity_result = add_activity(
        title="Lonely Activity", description="d", tags=["domain:test"], check_in=False,
    )
    activity_id = activity_result["activity_id"]

    conn = get_connection()
    try:
        scope = derive_scope(conn, activity_id)
    finally:
        conn.close()

    assert scope == []


def test_get_baseline_returns_zero_for_empty_database(temp_db):
    conn = get_connection()
    try:
        baseline = get_baseline(conn)
    finally:
        conn.close()
    assert baseline == {"decision_id": 0, "log_id": 0, "material_id": 0}


def test_get_baseline_returns_global_max_regardless_of_topic(temp_db, scope_topic, other_topic):
    """topic scopeに関係なく、テーブル全体のmax idを返すことを確認する
    （既読位置の初期値は範囲内maxではなく全体maxにする、という仕様）。
    """
    add_decision("decision A", "reason A", topic_id=scope_topic)
    d2 = add_decision("decision B (other topic)", "reason B", topic_id=other_topic)
    l1 = add_log(topic_id=scope_topic, content="log A")
    m1 = add_material(
        title="Material A", content="content A", tags=["domain:test"], source="test",
        related=[{"type": "topic", "ids": [scope_topic]}],
    )

    conn = get_connection()
    try:
        baseline = get_baseline(conn)
    finally:
        conn.close()

    # scope_topicより後に作られたother_topicの決定のidが採用される
    # （get_baselineはtopicの区別をせず、テーブル全体のmaxを返すため）
    assert baseline["decision_id"] == d2["decision_id"]
    assert baseline["log_id"] == l1["log_id"]
    assert baseline["material_id"] == m1["material_id"]


def test_compute_delta_returns_new_entities_after_watermark(temp_db, scope_topic):
    d_old = add_decision("old decision", "reason", topic_id=scope_topic)
    l_old = add_log(topic_id=scope_topic, content="old log")
    m_old = add_material(
        title="Old Material", content="old", tags=["domain:test"], source="test",
        related=[{"type": "topic", "ids": [scope_topic]}],
    )

    conn = get_connection()
    try:
        wm = get_baseline(conn)
    finally:
        conn.close()

    d_new = add_decision("new decision", "reason", topic_id=scope_topic, tags=None)
    l_new = add_log(topic_id=scope_topic, content="new log", title="New Log Title")
    m_new = add_material(
        title="New Material", content="new", tags=["domain:test"], source="test",
        related=[{"type": "topic", "ids": [scope_topic]}],
    )

    conn = get_connection()
    try:
        delta = compute_delta(conn, [scope_topic], activity_id=None, wm=wm)
    finally:
        conn.close()

    assert delta["new_decisions"] == [{"id": d_new["decision_id"], "title": "new decision"}]
    assert delta["new_logs"] == [{"id": l_new["log_id"], "title": "New Log Title"}]
    assert delta["new_materials"] == [{"id": m_new["material_id"], "title": "New Material"}]

    # 古いエンティティは拾わない
    old_decision_ids = {d["id"] for d in delta["new_decisions"]}
    assert d_old["decision_id"] not in old_decision_ids


def test_compute_delta_excludes_scope_external_topics(temp_db, scope_topic, other_topic):
    conn = get_connection()
    try:
        wm = get_baseline(conn)
    finally:
        conn.close()

    add_decision("decision in other topic", "reason", topic_id=other_topic)
    add_log(topic_id=other_topic, content="log in other topic")
    add_material(
        title="Material in other topic", content="x", tags=["domain:test"], source="test",
        related=[{"type": "topic", "ids": [other_topic]}],
    )

    conn = get_connection()
    try:
        delta = compute_delta(conn, [scope_topic], activity_id=None, wm=wm)
    finally:
        conn.close()

    assert delta == {"new_decisions": [], "new_logs": [], "new_materials": []}


def test_compute_delta_excludes_retracted_decision(temp_db, scope_topic):
    conn = get_connection()
    try:
        wm = get_baseline(conn)
    finally:
        conn.close()

    d_new = add_decision("will be retracted", "reason", topic_id=scope_topic)
    retract("decision", [d_new["decision_id"]])

    conn = get_connection()
    try:
        delta = compute_delta(conn, [scope_topic], activity_id=None, wm=wm)
    finally:
        conn.close()

    assert delta["new_decisions"] == []


def test_compute_delta_excludes_retracted_log(temp_db, scope_topic):
    conn = get_connection()
    try:
        wm = get_baseline(conn)
    finally:
        conn.close()

    l_new = add_log(topic_id=scope_topic, content="will be retracted")
    retract("log", [l_new["log_id"]])

    conn = get_connection()
    try:
        delta = compute_delta(conn, [scope_topic], activity_id=None, wm=wm)
    finally:
        conn.close()

    assert delta["new_logs"] == []


def test_compute_delta_excludes_retracted_material(temp_db, scope_topic):
    conn = get_connection()
    try:
        wm = get_baseline(conn)
    finally:
        conn.close()

    m_new = add_material(
        title="Will be retracted", content="x", tags=["domain:test"], source="test",
        related=[{"type": "topic", "ids": [scope_topic]}],
    )
    retract("material", [m_new["material_id"]])

    conn = get_connection()
    try:
        delta = compute_delta(conn, [scope_topic], activity_id=None, wm=wm)
    finally:
        conn.close()

    assert delta["new_materials"] == []


def test_compute_delta_includes_material_via_activity_scope(temp_db, scope_topic):
    """topicに一切紐付かず、activity経由のみで関連するmaterialも拾えること"""
    activity_result = add_activity(
        title="Delta Test Activity", description="d", tags=["domain:test"], check_in=False,
    )
    activity_id = activity_result["activity_id"]

    conn = get_connection()
    try:
        wm = get_baseline(conn)
    finally:
        conn.close()

    m_new = add_material(
        title="Material via activity", content="x", tags=["domain:test"], source="test",
        related=[{"type": "activity", "ids": [activity_id]}],
    )

    conn = get_connection()
    try:
        delta_without_activity = compute_delta(conn, [scope_topic], activity_id=None, wm=wm)
        delta_with_activity = compute_delta(conn, [scope_topic], activity_id=activity_id, wm=wm)
    finally:
        conn.close()

    assert delta_without_activity["new_materials"] == []
    assert delta_with_activity["new_materials"] == [
        {"id": m_new["material_id"], "title": "Material via activity"}
    ]
