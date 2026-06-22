"""add_activity / update_activity の orch_managed フィールド受付テスト

カラム判定機構が API 経由で直接設定できることを検証する (タグ自動付与に
依存しない経路)。

temp_db / disable_embedding フィクスチャは tests/conftest.py で共有。
"""
import pytest

from src.db import get_connection
from src.services.activity_service import add_activity, get_activities, update_activity
from src.services.hint_service import is_orch_managed_activity
from src.services.topic_service import add_topic
from tests.helpers import add_decision


@pytest.fixture(autouse=True)
def _auto_disable_embedding(disable_embedding):
    """このファイル内の全テストで embedding 生成を無効化する"""


@pytest.fixture
def topic_with_decision(temp_db):
    """テスト用に topic と decision を 1 件作成し、(topic_id, decision_id) を返す"""
    topic = add_topic(
        title="orch_managed test topic",
        description="topic for orch_managed API tests",
        tags=["domain:test"],
    )
    topic_id = topic["topic_id"]
    decision = add_decision(
        decision="Approved direction",
        reason="Reviewed and agreed",
        topic_id=topic_id,
    )
    return topic_id, decision["decision_id"]


def _get_orch_managed(activity_id: int) -> int:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT orch_managed FROM activities WHERE id = ?",
            (activity_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return row["orch_managed"]


class TestAddActivityOrchManaged:
    """add_activity が orch_managed パラメータを受け付ける"""

    def test_default_is_false(self, temp_db, topic_with_decision):
        """orch_managed を指定せず作成すると activities.orch_managed=0 になる"""
        _, decision_id = topic_with_decision
        result = add_activity(
            title="[作業] 通常",
            description="d",
            tags=["domain:test", "intent:implement"],
            related=[{"type": "decision", "ids": [decision_id]}],
            check_in=False,
        )
        assert "error" not in result, result
        assert _get_orch_managed(result["activity_id"]) == 0

    def test_true_sets_column(self, temp_db, topic_with_decision):
        """orch_managed=True を指定すると activities.orch_managed=1 になる"""
        _, decision_id = topic_with_decision
        result = add_activity(
            title="[作業] orch 管理",
            description="d",
            tags=["domain:test", "intent:implement"],
            related=[{"type": "decision", "ids": [decision_id]}],
            check_in=False,
            orch_managed=True,
        )
        assert "error" not in result, result
        assert _get_orch_managed(result["activity_id"]) == 1

    def test_orch_managed_true_without_tag_is_detected_by_hint_service(
        self, temp_db, topic_with_decision
    ):
        """orch-managed タグ自動付与に依存せず、hint_service の判定で True になる"""
        _, decision_id = topic_with_decision
        result = add_activity(
            title="[作業] orch 管理",
            description="d",
            tags=["domain:test", "intent:implement"],
            related=[{"type": "decision", "ids": [decision_id]}],
            check_in=False,
            orch_managed=True,
        )
        assert "error" not in result, result
        conn = get_connection()
        try:
            assert is_orch_managed_activity(conn, result["activity_id"]) is True
        finally:
            conn.close()

    def test_false_explicit_is_same_as_default(self, temp_db, topic_with_decision):
        """orch_managed=False 明示は orch_managed 未指定と同等"""
        _, decision_id = topic_with_decision
        result = add_activity(
            title="[作業] 明示 false",
            description="d",
            tags=["domain:test", "intent:implement"],
            related=[{"type": "decision", "ids": [decision_id]}],
            check_in=False,
            orch_managed=False,
        )
        assert "error" not in result, result
        assert _get_orch_managed(result["activity_id"]) == 0


class TestUpdateActivityOrchManaged:
    """update_activity が orch_managed パラメータを受け付ける"""

    def test_update_to_true(self, temp_db, topic_with_decision):
        """既存 activity の orch_managed を 0 → 1 に切り替えできる"""
        _, decision_id = topic_with_decision
        created = add_activity(
            title="[作業] 切替対象",
            description="d",
            tags=["domain:test", "intent:implement"],
            related=[{"type": "decision", "ids": [decision_id]}],
            check_in=False,
        )
        aid = created["activity_id"]
        assert _get_orch_managed(aid) == 0

        result = update_activity(aid, orch_managed=True)
        assert "error" not in result, result
        assert _get_orch_managed(aid) == 1

    def test_update_to_false(self, temp_db, topic_with_decision):
        """既存 activity の orch_managed を 1 → 0 に切り替えできる"""
        _, decision_id = topic_with_decision
        created = add_activity(
            title="[作業] 切替対象",
            description="d",
            tags=["domain:test", "intent:implement"],
            related=[{"type": "decision", "ids": [decision_id]}],
            check_in=False,
            orch_managed=True,
        )
        aid = created["activity_id"]
        assert _get_orch_managed(aid) == 1

        result = update_activity(aid, orch_managed=False)
        assert "error" not in result, result
        assert _get_orch_managed(aid) == 0

    def test_orch_managed_only_update_does_not_require_other_fields(
        self, temp_db, topic_with_decision
    ):
        """orch_managed 単独指定で update_activity がエラーにならない"""
        _, decision_id = topic_with_decision
        created = add_activity(
            title="[作業] 単独更新",
            description="d",
            tags=["domain:test", "intent:implement"],
            related=[{"type": "decision", "ids": [decision_id]}],
            check_in=False,
        )
        aid = created["activity_id"]
        result = update_activity(aid, orch_managed=True)
        assert "error" not in result, result

    def test_no_fields_provided_returns_validation_error(
        self, temp_db, topic_with_decision
    ):
        """全フィールド未指定は VALIDATION_ERROR を返す"""
        _, decision_id = topic_with_decision
        created = add_activity(
            title="[作業] no-op",
            description="d",
            tags=["domain:test", "intent:implement"],
            related=[{"type": "decision", "ids": [decision_id]}],
            check_in=False,
        )
        aid = created["activity_id"]
        result = update_activity(aid)
        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_other_fields_unchanged_when_only_orch_managed_updated(
        self, temp_db, topic_with_decision
    ):
        """orch_managed のみ更新でも他フィールドは保持される"""
        _, decision_id = topic_with_decision
        created = add_activity(
            title="[作業] 保持確認",
            description="original description",
            tags=["domain:test", "intent:implement"],
            related=[{"type": "decision", "ids": [decision_id]}],
            check_in=False,
        )
        aid = created["activity_id"]

        update_activity(aid, orch_managed=True)

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT title, description, orch_managed FROM activities WHERE id = ?",
                (aid,),
            ).fetchone()
        finally:
            conn.close()
        assert row["title"] == "[作業] 保持確認"
        assert row["description"] == "original description"
        assert row["orch_managed"] == 1


class TestGetActivitiesOrchManaged:
    """get_activities の orch_managed フィルタおよび返却 dict の orch_managed フィールド"""

    def test_response_includes_orch_managed_field(
        self, temp_db, topic_with_decision
    ):
        """get_activities が返す各 item に orch_managed フィールドが含まれる"""
        _, decision_id = topic_with_decision
        add_activity(
            title="[作業] 通常",
            description="d",
            tags=["domain:test", "intent:implement"],
            related=[{"type": "decision", "ids": [decision_id]}],
            check_in=False,
        )
        add_activity(
            title="[作業] orch",
            description="d",
            tags=["domain:test", "intent:implement"],
            related=[{"type": "decision", "ids": [decision_id]}],
            check_in=False,
            orch_managed=True,
        )
        result = get_activities(tags=["domain:test"], status="active")
        assert "activities" in result, result
        for item in result["activities"]:
            assert "orch_managed" in item, item
            assert isinstance(item["orch_managed"], bool)
        flags = {item["title"]: item["orch_managed"] for item in result["activities"]}
        assert flags.get("[作業] 通常") is False
        assert flags.get("[作業] orch") is True

    def test_filter_true_returns_only_orch_managed(
        self, temp_db, topic_with_decision
    ):
        """orch_managed=True 指定で orch_managed=1 のみが返る"""
        _, decision_id = topic_with_decision
        add_activity(
            title="[作業] 通常",
            description="d",
            tags=["domain:test", "intent:implement"],
            related=[{"type": "decision", "ids": [decision_id]}],
            check_in=False,
        )
        add_activity(
            title="[作業] orch",
            description="d",
            tags=["domain:test", "intent:implement"],
            related=[{"type": "decision", "ids": [decision_id]}],
            check_in=False,
            orch_managed=True,
        )
        result = get_activities(
            tags=["domain:test"], status="active", orch_managed=True,
        )
        titles = [item["title"] for item in result["activities"]]
        assert titles == ["[作業] orch"]

    def test_filter_false_returns_only_non_orch_managed(
        self, temp_db, topic_with_decision
    ):
        """orch_managed=False 指定で orch_managed=0 のみが返る"""
        _, decision_id = topic_with_decision
        add_activity(
            title="[作業] 通常",
            description="d",
            tags=["domain:test", "intent:implement"],
            related=[{"type": "decision", "ids": [decision_id]}],
            check_in=False,
        )
        add_activity(
            title="[作業] orch",
            description="d",
            tags=["domain:test", "intent:implement"],
            related=[{"type": "decision", "ids": [decision_id]}],
            check_in=False,
            orch_managed=True,
        )
        result = get_activities(
            tags=["domain:test"], status="active", orch_managed=False,
        )
        titles = [item["title"] for item in result["activities"]]
        assert titles == ["[作業] 通常"]
