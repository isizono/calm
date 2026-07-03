"""direction_service（方向性decisionの非ランク網羅列挙）の単体テスト"""
import os
import tempfile

import pytest

from src.db import get_connection, init_database
from src.services.decision_service import add_decisions
from src.services.direction_service import (
    DIRECTION_NAME,
    DIRECTION_NAMESPACE,
    get_direction_decisions,
    get_direction_tag_id,
)
from src.services.relation_service import add_relation
from src.services.tag_service import _injected_tags
from src.services.topic_service import add_topic
from tests.helpers import add_decision, retract_decision

DIRECTION_TAG = f"{DIRECTION_NAMESPACE}:{DIRECTION_NAME}"
DOMAIN_TAG = "domain:direction-test"
OTHER_DOMAIN_TAG = "domain:direction-other"


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
    t = add_topic(title="direction テスト", description="Desc", tags=[DOMAIN_TAG])
    return t["topic_id"]


def _add_direction_decision(topic_id: int, decision: str, title: str, tags=None) -> dict:
    all_tags = [DIRECTION_TAG] + (tags or [])
    result = add_decisions([
        {"topic_id": topic_id, "decision": decision, "reason": "r", "title": title, "tags": all_tags},
    ])
    assert "error" not in result, result
    assert not result["errors"], result["errors"]
    return result["created"][0]


def _link_supersede(newer_id: int, older_id: int) -> None:
    result = add_relation(
        "decision", newer_id, [{"type": "decision", "ids": [older_id]}],
        relation_type="supersedes",
    )
    assert "error" not in result, result


def _domain_tag_id(name: str) -> int:
    conn = get_connection()
    try:
        namespace, tag_name = name.split(":", 1)
        row = conn.execute(
            "SELECT id FROM tags WHERE namespace = ? AND name = ?",
            (namespace, tag_name),
        ).fetchone()
        return row["id"]
    finally:
        conn.close()


class TestGetDirectionTagId:
    def test_none_when_tag_never_created(self, temp_db):
        conn = get_connection()
        try:
            assert get_direction_tag_id(conn) is None
        finally:
            conn.close()

    def test_returns_id_after_first_use(self, temp_db, topic_id):
        _add_direction_decision(topic_id, "決定A", "方向性A")
        conn = get_connection()
        try:
            assert get_direction_tag_id(conn) is not None
        finally:
            conn.close()


class TestGetDirectionDecisions:
    def test_empty_when_no_direction_tag(self, temp_db):
        conn = get_connection()
        try:
            assert get_direction_decisions(conn) == []
        finally:
            conn.close()

    def test_only_directly_tagged_decisions_included(self, temp_db, topic_id):
        """layer:directionが直付けされたdecisionのみが対象。通常decisionは含まれない"""
        _add_direction_decision(topic_id, "方向性の決定", "方向性1")
        add_decision(decision="通常の決定", reason="r", topic_id=topic_id)

        conn = get_connection()
        try:
            results = get_direction_decisions(conn)
        finally:
            conn.close()
        assert len(results) == 1
        assert results[0]["decision"] == "方向性の決定"

    def test_topic_inherited_tag_does_not_count_as_direction(self, temp_db):
        """topicにlayer:directionタグが付いていても、decision自体に直付けしなければ対象外"""
        t = add_topic(title="direction継承テスト", description="d", tags=[DOMAIN_TAG, DIRECTION_TAG])
        add_decision(decision="継承のみの決定", reason="r", topic_id=t["topic_id"])

        conn = get_connection()
        try:
            results = get_direction_decisions(conn)
        finally:
            conn.close()
        assert results == []

    def test_excludes_retracted(self, temp_db, topic_id):
        created = _add_direction_decision(topic_id, "撤回される方向性", "撤回予定")
        retract_decision(created["decision_id"])

        conn = get_connection()
        try:
            results = get_direction_decisions(conn)
        finally:
            conn.close()
        assert results == []

    def test_excludes_superseded_by_default(self, temp_db, topic_id):
        old = _add_direction_decision(topic_id, "旧方向性", "旧")
        new = _add_direction_decision(topic_id, "新方向性", "新")
        _link_supersede(new["decision_id"], old["decision_id"])

        conn = get_connection()
        try:
            active = get_direction_decisions(conn)
            with_superseded = get_direction_decisions(conn, include_superseded=True)
        finally:
            conn.close()

        active_ids = {r["id"] for r in active}
        assert old["decision_id"] not in active_ids
        assert new["decision_id"] in active_ids

        all_ids = {r["id"] for r in with_superseded}
        assert old["decision_id"] in all_ids
        assert new["decision_id"] in all_ids

    def test_domain_filter_direct_tag(self, temp_db, topic_id):
        matched = _add_direction_decision(topic_id, "対象domainの方向性", "対象")
        other_topic = add_topic(title="別domain", description="d", tags=[OTHER_DOMAIN_TAG])
        _add_direction_decision(other_topic["topic_id"], "別domainの方向性", "別")

        conn = get_connection()
        try:
            results = get_direction_decisions(conn, domain_tag_ids=[_domain_tag_id(DOMAIN_TAG)])
        finally:
            conn.close()
        result_ids = {r["id"] for r in results}
        assert matched["decision_id"] in result_ids
        assert len(results) == 1

    def test_domain_filter_inherited_via_topic(self, temp_db, topic_id):
        """decision自体にdomainタグが無くても、親topicのdomainタグ継承でマッチする"""
        matched = _add_direction_decision(topic_id, "topic継承の方向性", "継承")

        conn = get_connection()
        try:
            results = get_direction_decisions(conn, domain_tag_ids=[_domain_tag_id(DOMAIN_TAG)])
        finally:
            conn.close()
        assert [r["id"] for r in results] == [matched["decision_id"]]

    def test_ordered_by_created_at_ascending(self, temp_db, topic_id):
        first = _add_direction_decision(topic_id, "最初の方向性", "第1")
        second = _add_direction_decision(topic_id, "次の方向性", "第2")

        conn = get_connection()
        try:
            results = get_direction_decisions(conn)
        finally:
            conn.close()
        assert [r["id"] for r in results] == [first["decision_id"], second["decision_id"]]

    def test_items_carry_staleness_block(self, temp_db, topic_id):
        created = _add_direction_decision(topic_id, "staleness確認", "第1")

        conn = get_connection()
        try:
            results = get_direction_decisions(conn)
        finally:
            conn.close()
        assert results[0]["id"] == created["decision_id"]
        assert "staleness" in results[0]
        assert results[0]["staleness"]["is_superseded"] is False
        assert results[0]["staleness"]["chain_heads"] == [created["decision_id"]]
