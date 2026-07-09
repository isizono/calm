"""delta_service: get_baseline / compute_delta のユニットテスト

temp_db / disable_embedding フィクスチャは tests/conftest.py で共有。
"""
import pytest

from src.db import get_connection
from src.services.activity_service import add_activity
from src.services.material_service import add_material
from src.services.retract_service import retract
from src.services.topic_service import add_topic
from src.services.delta_service import compute_delta, get_baseline
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


def test_get_baseline_returns_zero_for_empty_topic_ids(temp_db):
    conn = get_connection()
    try:
        baseline = get_baseline(conn, [])
    finally:
        conn.close()
    assert baseline == {"decision_id": 0, "log_id": 0, "material_id": 0}


def test_get_baseline_returns_max_ids_within_scope(temp_db, scope_topic, other_topic):
    d1 = add_decision("decision A", "reason A", topic_id=scope_topic)
    add_decision("decision B (other topic)", "reason B", topic_id=other_topic)
    l1 = add_log(topic_id=scope_topic, content="log A")
    m1 = add_material(
        title="Material A", content="content A", tags=["domain:test"], source="test",
        related=[{"type": "topic", "ids": [scope_topic]}],
    )

    conn = get_connection()
    try:
        baseline = get_baseline(conn, [scope_topic])
    finally:
        conn.close()

    assert baseline["decision_id"] == d1["decision_id"]
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
        wm = get_baseline(conn, [scope_topic])
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
        wm = get_baseline(conn, [scope_topic])
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
        wm = get_baseline(conn, [scope_topic])
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
        wm = get_baseline(conn, [scope_topic])
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
        wm = get_baseline(conn, [scope_topic])
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


def test_get_baseline_includes_material_via_activity_scope(temp_db, scope_topic):
    """activity_idを渡した場合、activity経由のみのmaterialもbaselineに含まれ、
    check-in直後の最初のcompute_deltaで誤って新規と報告されない（false positive防止）ことを確認する。
    """
    activity_result = add_activity(
        title="Baseline Test Activity", description="d", tags=["domain:test"], check_in=False,
    )
    activity_id = activity_result["activity_id"]

    m_existing = add_material(
        title="Pre-existing via activity", content="x", tags=["domain:test"], source="test",
        related=[{"type": "activity", "ids": [activity_id]}],
    )

    conn = get_connection()
    try:
        baseline = get_baseline(conn, [scope_topic], activity_id=activity_id)
    finally:
        conn.close()
    assert baseline["material_id"] == m_existing["material_id"]

    conn = get_connection()
    try:
        delta = compute_delta(conn, [scope_topic], activity_id=activity_id, wm=baseline)
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
        wm = get_baseline(conn, [scope_topic])
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
