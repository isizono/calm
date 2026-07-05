"""hint_service: 統一hint APIのユニットテスト"""
import os
import tempfile

import pytest

from src.db import get_connection, init_database
from src.services.activity_service import add_activity
from src.services.decision_service import add_decisions
from src.services.direction_service import DIRECTION_NAME, DIRECTION_NAMESPACE
from src.services.hint_service import (
    DIRECTION_OVERFLOW_THRESHOLD,
    HINT_LOGS_SPARSE_MESSAGE,
    LOGS_SPARSE_LOG_THRESHOLD,
    MARKER_DIRECTION_OVERFLOW,
    MARKER_LOGS_SPARSE,
    MARKER_RECOMPOSE_BOOTSTRAP,
    MARKER_RECOMPOSE_DELTA,
    MARKER_RECOMPOSE_GENERIC,
    RECOMPOSE_BOOTSTRAP_THRESHOLD,
    RECOMPOSE_DELTA_THRESHOLD,
    get_hints,
    get_hints_with_conn,
    is_orch_managed_activity,
)
from src.services.material_service import add_material
from src.services.pin_service import add_pin
from src.services.topic_service import add_topic
from src.services.tag_service import _injected_tags, update_tag
from tests.helpers import add_decision

DOMAIN_TAG_NAME = "hint-domain"
DOMAIN_TAG = f"domain:{DOMAIN_TAG_NAME}"
DIRECTION_TAG = f"{DIRECTION_NAMESPACE}:{DIRECTION_NAME}"


def _add_direction_decision(topic_id: int, i: int) -> dict:
    result = add_decisions([{
        "topic_id": topic_id, "decision": f"方向性{i}", "reason": "r", "title": f"方向性{i}の要点",
        "tags": [DIRECTION_TAG],
    }])
    assert "error" not in result, result
    return result["created"][0]


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


def _tag_id(name: str, namespace: str = "domain") -> int:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM tags WHERE namespace = ? AND name = ?",
            (namespace, name),
        ).fetchone()
        return row["id"]
    finally:
        conn.close()


def _set_material_updated_at(material_id: int, ts: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE materials SET updated_at = ? WHERE id = ?",
            (ts, material_id),
        )
        conn.commit()
    finally:
        conn.close()


def _set_decision_created_at(decision_id: int, ts: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE decisions SET created_at = ? WHERE id = ?",
            (ts, decision_id),
        )
        conn.commit()
    finally:
        conn.close()


class TestRecomposeBootstrap:
    def test_fires_at_threshold(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        for i in range(RECOMPOSE_BOOTSTRAP_THRESHOLD):
            add_decision(decision=f"d{i}", reason="r", topic_id=topic["topic_id"])

        hints = get_hints("tag", _tag_id(DOMAIN_TAG_NAME))
        assert len(hints) == 1
        assert hints[0]["type"] == "recompose_bootstrap"
        assert hints[0]["delivery_hint"] == "immediate"
        assert hints[0]["severity"] == "info"
        assert str(RECOMPOSE_BOOTSTRAP_THRESHOLD) in hints[0]["message"]

    def test_silent_below_threshold(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        for i in range(RECOMPOSE_BOOTSTRAP_THRESHOLD - 1):
            add_decision(decision=f"d{i}", reason="r", topic_id=topic["topic_id"])

        assert get_hints("tag", _tag_id(DOMAIN_TAG_NAME)) == []

    def test_plain_tag_namespace_not_targeted(self, temp_db):
        """素タグ namespace='' は判定対象外。namespaceフィルタが効いていることを確認。"""
        plain_topic = add_topic(
            title="t", description="d", tags=["plain-only"]
        )
        for i in range(RECOMPOSE_BOOTSTRAP_THRESHOLD + 5):
            add_decision(decision=f"d{i}", reason="r", topic_id=plain_topic["topic_id"])

        plain_tag_id = _tag_id("plain-only", namespace="")
        assert get_hints("tag", plain_tag_id) == []

    def test_suppressed_by_specific_marker(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        for i in range(RECOMPOSE_BOOTSTRAP_THRESHOLD):
            add_decision(decision=f"d{i}", reason="r", topic_id=topic["topic_id"])
        update_tag(DOMAIN_TAG, notes=f"既に整理済。{MARKER_RECOMPOSE_BOOTSTRAP}")

        assert get_hints("tag", _tag_id(DOMAIN_TAG_NAME)) == []

    def test_suppressed_by_generic_marker(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        for i in range(RECOMPOSE_BOOTSTRAP_THRESHOLD):
            add_decision(decision=f"d{i}", reason="r", topic_id=topic["topic_id"])
        update_tag(DOMAIN_TAG, notes=f"{MARKER_RECOMPOSE_GENERIC} 任意ノート")

        assert get_hints("tag", _tag_id(DOMAIN_TAG_NAME)) == []


class TestRecomposeDelta:
    def test_fires_at_threshold(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        mat = add_material(
            title="m", content="c", tags=[DOMAIN_TAG], source="s",
        )
        add_pin("tag", DOMAIN_TAG, "material", mat["material_id"])
        _set_material_updated_at(mat["material_id"], "2024-06-01 00:00:00")

        for i in range(RECOMPOSE_DELTA_THRESHOLD):
            d = add_decision(decision=f"d{i}", reason="r", topic_id=topic["topic_id"])
            _set_decision_created_at(d["decision_id"], "2024-07-01 00:00:00")

        hints = get_hints("tag", _tag_id(DOMAIN_TAG_NAME))
        assert len(hints) == 1
        assert hints[0]["type"] == "recompose_delta"
        assert hints[0]["delivery_hint"] == "immediate"
        assert str(RECOMPOSE_DELTA_THRESHOLD) in hints[0]["message"]

    def test_silent_below_threshold(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        mat = add_material(
            title="m", content="c", tags=[DOMAIN_TAG], source="s",
        )
        add_pin("tag", DOMAIN_TAG, "material", mat["material_id"])
        _set_material_updated_at(mat["material_id"], "2024-06-01 00:00:00")

        for i in range(RECOMPOSE_DELTA_THRESHOLD - 1):
            d = add_decision(decision=f"d{i}", reason="r", topic_id=topic["topic_id"])
            _set_decision_created_at(d["decision_id"], "2024-07-01 00:00:00")

        assert get_hints("tag", _tag_id(DOMAIN_TAG_NAME)) == []

    def test_decisions_before_base_time_excluded(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        mat = add_material(
            title="m", content="c", tags=[DOMAIN_TAG], source="s",
        )
        add_pin("tag", DOMAIN_TAG, "material", mat["material_id"])
        _set_material_updated_at(mat["material_id"], "2024-06-01 00:00:00")

        for i in range(RECOMPOSE_DELTA_THRESHOLD):
            d = add_decision(decision=f"d{i}", reason="r", topic_id=topic["topic_id"])
            _set_decision_created_at(d["decision_id"], "2024-05-01 00:00:00")

        assert get_hints("tag", _tag_id(DOMAIN_TAG_NAME)) == []

    def test_suppressed_by_delta_marker(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        mat = add_material(
            title="m", content="c", tags=[DOMAIN_TAG], source="s",
        )
        add_pin("tag", DOMAIN_TAG, "material", mat["material_id"])
        _set_material_updated_at(mat["material_id"], "2024-06-01 00:00:00")

        for i in range(RECOMPOSE_DELTA_THRESHOLD):
            d = add_decision(decision=f"d{i}", reason="r", topic_id=topic["topic_id"])
            _set_decision_created_at(d["decision_id"], "2024-07-01 00:00:00")
        update_tag(DOMAIN_TAG, notes=f"{MARKER_RECOMPOSE_DELTA}")

        assert get_hints("tag", _tag_id(DOMAIN_TAG_NAME)) == []


class TestDirectionOverflow:
    def test_fires_at_threshold(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        for i in range(DIRECTION_OVERFLOW_THRESHOLD):
            _add_direction_decision(topic["topic_id"], i)

        hints = get_hints("tag", _tag_id(DOMAIN_TAG_NAME))
        direction_hints = [h for h in hints if h["type"] == "direction_overflow"]
        assert len(direction_hints) == 1
        assert direction_hints[0]["delivery_hint"] == "immediate"
        assert direction_hints[0]["severity"] == "info"
        assert str(DIRECTION_OVERFLOW_THRESHOLD) in direction_hints[0]["message"]

    def test_silent_below_threshold(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        for i in range(DIRECTION_OVERFLOW_THRESHOLD - 1):
            _add_direction_decision(topic["topic_id"], i)

        hints = get_hints("tag", _tag_id(DOMAIN_TAG_NAME))
        assert [h for h in hints if h["type"] == "direction_overflow"] == []

    def test_suppressed_by_direction_marker(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        for i in range(DIRECTION_OVERFLOW_THRESHOLD):
            _add_direction_decision(topic["topic_id"], i)
        update_tag(DOMAIN_TAG, notes=f"{MARKER_DIRECTION_OVERFLOW}")

        hints = get_hints("tag", _tag_id(DOMAIN_TAG_NAME))
        assert [h for h in hints if h["type"] == "direction_overflow"] == []

    def test_not_suppressed_by_generic_recompose_marker(self, temp_db):
        """direction_overflowはrecompose系と独立した抑制マーカーを持つ。
        汎用recomposeマーカーでは抑制されない"""
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        for i in range(DIRECTION_OVERFLOW_THRESHOLD):
            _add_direction_decision(topic["topic_id"], i)
        update_tag(DOMAIN_TAG, notes=f"{MARKER_RECOMPOSE_GENERIC}")

        hints = get_hints("tag", _tag_id(DOMAIN_TAG_NAME))
        assert [h for h in hints if h["type"] == "direction_overflow"] != []

    def test_excludes_retracted_and_superseded_from_count(self, temp_db):
        """有効(active)件数のみをカウントする。件数不足ならfireしない"""
        from src.services.relation_service import add_relation
        from tests.helpers import retract_decision

        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        decisions = [_add_direction_decision(topic["topic_id"], i) for i in range(DIRECTION_OVERFLOW_THRESHOLD)]
        retract_decision(decisions[0]["decision_id"])
        add_relation(
            "decision", decisions[1]["decision_id"],
            [{"type": "decision", "ids": [decisions[2]["decision_id"]]}],
            relation_type="supersedes",
        )

        hints = get_hints("tag", _tag_id(DOMAIN_TAG_NAME))
        assert [h for h in hints if h["type"] == "direction_overflow"] == []

    def test_recompose_marker_does_not_suppress_when_scoped_to_delta(self, temp_db):
        """coexistence: recompose_bootstrapとdirection_overflowが同時に発火しうる"""
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        for i in range(RECOMPOSE_BOOTSTRAP_THRESHOLD):
            add_decision(decision=f"d{i}", reason="r", topic_id=topic["topic_id"])
        for i in range(DIRECTION_OVERFLOW_THRESHOLD):
            _add_direction_decision(topic["topic_id"], i)

        hints = get_hints("tag", _tag_id(DOMAIN_TAG_NAME))
        types = {h["type"] for h in hints}
        assert "recompose_bootstrap" in types
        assert "direction_overflow" in types


class TestLogsSparse:
    def test_fires_when_logs_below_threshold(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        add_decision(decision="d", reason="r", topic_id=topic["topic_id"])

        hints = get_hints("topic", topic["topic_id"])
        assert len(hints) == 1
        assert hints[0]["type"] == "logs_sparse"
        assert hints[0]["delivery_hint"] == "deferred"
        assert hints[0]["severity"] == "info"
        assert hints[0]["message"] == HINT_LOGS_SPARSE_MESSAGE

    def test_silent_when_no_decisions(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        assert get_hints("topic", topic["topic_id"]) == []

    def test_silent_when_logs_at_threshold(self, temp_db):
        from tests.helpers import add_log

        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        add_decision(decision="d", reason="r", topic_id=topic["topic_id"])
        for i in range(LOGS_SPARSE_LOG_THRESHOLD):
            add_log(topic_id=topic["topic_id"], content=f"l{i}")

        assert get_hints("topic", topic["topic_id"]) == []

    def test_suppressed_by_marker(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        add_decision(decision="d", reason="r", topic_id=topic["topic_id"])
        update_tag(DOMAIN_TAG, notes=f"以後logsは付けない方針。{MARKER_LOGS_SPARSE}")

        assert get_hints("topic", topic["topic_id"]) == []


class TestActivityScope:
    def test_aggregates_domain_tag_recompose_hints(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        for i in range(RECOMPOSE_BOOTSTRAP_THRESHOLD):
            add_decision(decision=f"d{i}", reason="r", topic_id=topic["topic_id"])
        dec0 = add_decision(decision="anchor", reason="r", topic_id=topic["topic_id"])
        activity = add_activity(
            title="[作業] x", description="d",
            tags=[DOMAIN_TAG, "intent:implement"],
            related=[{"type": "decision", "ids": [dec0["decision_id"]]}],
            check_in=False,
        )

        hints = get_hints("activity", activity["activity_id"])
        assert any(h["type"] == "recompose_bootstrap" for h in hints)


class TestIsOrchManagedActivity:
    def test_true_when_orch_managed_column_set(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        dec = add_decision(decision="d", reason="r", topic_id=topic["topic_id"])
        a = add_activity(
            title="[orch] x", description="d",
            tags=[DOMAIN_TAG, "intent:implement"],
            related=[{"type": "decision", "ids": [dec["decision_id"]]}],
            check_in=False,
            orch_managed=True,
        )
        conn = get_connection()
        try:
            assert is_orch_managed_activity(conn, a["activity_id"]) is True
        finally:
            conn.close()

    def test_false_when_orch_managed_column_not_set(self, temp_db):
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        dec = add_decision(decision="d", reason="r", topic_id=topic["topic_id"])
        a = add_activity(
            title="[作業] x", description="d",
            tags=[DOMAIN_TAG, "intent:implement"],
            related=[{"type": "decision", "ids": [dec["decision_id"]]}],
            check_in=False,
        )
        conn = get_connection()
        try:
            assert is_orch_managed_activity(conn, a["activity_id"]) is False
        finally:
            conn.close()

    def test_false_when_only_tag_present_without_column(self, temp_db):
        """orch-managed タグだけ付与しても orch_managed カラムが 0 なら False (カラム判定優先)。

        移行期にタグだけ残った状態でも、判定はカラム値のみに依存する。
        """
        topic = add_topic(title="t", description="d", tags=[DOMAIN_TAG])
        dec = add_decision(decision="d", reason="r", topic_id=topic["topic_id"])
        a = add_activity(
            title="[orch] x", description="d",
            tags=[DOMAIN_TAG, "orch-managed", "intent:implement"],
            related=[{"type": "decision", "ids": [dec["decision_id"]]}],
            check_in=False,
        )
        conn = get_connection()
        try:
            assert is_orch_managed_activity(conn, a["activity_id"]) is False
        finally:
            conn.close()

    def test_false_for_unknown_activity_id(self, temp_db):
        """存在しない activity_id は False (フェイルオープン)。"""
        conn = get_connection()
        try:
            assert is_orch_managed_activity(conn, 999_999) is False
        finally:
            conn.close()


class TestEdgeCases:
    def test_unknown_scope_returns_empty(self, temp_db):
        conn = get_connection()
        try:
            assert get_hints_with_conn(conn, "tag", 999_999) == []
            assert get_hints_with_conn(conn, "topic", 999_999) == []
            assert get_hints_with_conn(conn, "activity", 999_999) == []
        finally:
            conn.close()

    def test_intent_tag_not_targeted_for_recompose(self, temp_db):
        topic = add_topic(title="t", description="d", tags=["domain:other"])
        for i in range(RECOMPOSE_BOOTSTRAP_THRESHOLD):
            add_decision(
                decision=f"d{i}", reason="r", topic_id=topic["topic_id"],
                tags=["intent:design"],
            )

        intent_tag_id = _tag_id("design", namespace="intent")
        assert get_hints("tag", intent_tag_id) == []
