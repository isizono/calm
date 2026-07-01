"""get_logs / get_decisions の entity_type 対応テスト"""
import os
import tempfile
import pytest

from src.db import init_database
from src.services.activity_service import add_activity
from src.services.topic_service import add_topic
from src.services.relation_service import add_relation
from src.services.discussion_log_service import get_logs
from src.services.decision_service import get_decisions
from src.services.retract_service import retract
from tests.helpers import add_log, add_decision
from src.services.tag_service import _injected_tags


DEFAULT_TAGS = ["domain:test"]


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


@pytest.fixture
def topic(temp_db):
    """テスト用トピックを作成する"""
    return add_topic(title="テストトピック", description="テスト用", tags=DEFAULT_TAGS)


@pytest.fixture
def activity(temp_db):
    """テスト用アクティビティを作成する"""
    result = add_activity(
        title="[作業] テスト",
        description="テスト用アクティビティ",
        tags=DEFAULT_TAGS,
        check_in=False,
    )
    return result


class TestGetLogsTopicType:
    """get_logs(entity_type="topic") の後方互換テスト"""

    def test_get_logs_by_topic(self, topic):
        """topic_typeでlogsが取得できる"""
        tid = topic["topic_id"]
        add_log(topic_id=tid, title="ログ1", content="内容1")
        add_log(topic_id=tid, title="ログ2", content="内容2")

        result = get_logs("topic", tid)

        assert "error" not in result
        assert len(result["logs"]) == 2
        titles = [l["title"] for l in result["logs"]]
        assert "ログ1" in titles
        assert "ログ2" in titles

    def test_get_logs_by_topic_has_content(self, topic):
        """topicタイプのlogsはcontentを含む"""
        tid = topic["topic_id"]
        add_log(topic_id=tid, title="ログ", content="詳細内容")

        result = get_logs("topic", tid)

        assert "error" not in result
        assert len(result["logs"]) == 1
        assert result["logs"][0]["content"] == "詳細内容"

    def test_get_logs_by_topic_empty(self, topic):
        """ログがない場合、空リストが返る"""
        result = get_logs("topic", topic["topic_id"])

        assert "error" not in result
        assert result["logs"] == []

    def test_get_logs_by_topic_pagination(self, topic):
        """start_idによるページネーションが機能する"""
        tid = topic["topic_id"]
        l1 = add_log(topic_id=tid, title="ログ1", content="内容1")
        l2 = add_log(topic_id=tid, title="ログ2", content="内容2")
        l3 = add_log(topic_id=tid, title="ログ3", content="内容3")

        first_log_id = l1["log_id"]
        second_log_id = l2["log_id"]

        # start_idを指定してページネーション
        result = get_logs("topic", tid, start_id=second_log_id)

        assert "error" not in result
        assert len(result["logs"]) == 2
        # α化により id は文字列 "title (#N)"、元の整数 ID は id_raw に退避
        ids = [l["id_raw"] for l in result["logs"]]
        assert first_log_id not in ids
        assert second_log_id in ids


class TestGetLogsActivityType:
    """get_logs(entity_type="activity") のテスト"""

    def test_get_logs_by_activity_via_related_topics(self, temp_db):
        """activity経由でrelated topicsのlogsが取得できる"""
        topic = add_topic(title="トピック", description="Desc", tags=DEFAULT_TAGS)
        add_log(topic_id=topic["topic_id"], title="ログ1", content="内容1")
        add_log(topic_id=topic["topic_id"], title="ログ2", content="内容2")
        act = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", act["activity_id"], [{"type": "topic", "ids": [topic["topic_id"]]}])

        result = get_logs("activity", act["activity_id"])

        assert "error" not in result
        assert len(result["logs"]) == 2

    def test_get_logs_by_activity_no_related_topics(self, activity):
        """related topicsが0件の場合、空リストが返る"""
        result = get_logs("activity", activity["activity_id"])

        assert "error" not in result
        assert result["logs"] == []

    def test_get_logs_by_activity_multiple_topics(self, temp_db):
        """複数のrelated topicsのlogsが集約される"""
        t1 = add_topic(title="トピック1", description="Desc", tags=DEFAULT_TAGS)
        t2 = add_topic(title="トピック2", description="Desc", tags=DEFAULT_TAGS)
        add_log(topic_id=t1["topic_id"], title="T1ログ", content="内容")
        add_log(topic_id=t2["topic_id"], title="T2ログ", content="内容")
        act = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", act["activity_id"], [{"type": "topic", "ids": [t1["topic_id"], t2["topic_id"]]}])

        result = get_logs("activity", act["activity_id"])

        assert "error" not in result
        assert len(result["logs"]) == 2
        titles = {l["title"] for l in result["logs"]}
        assert "T1ログ" in titles
        assert "T2ログ" in titles

    def test_get_logs_by_activity_has_content(self, temp_db):
        """activityタイプのlogsはcontentを含む"""
        topic = add_topic(title="トピック", description="Desc", tags=DEFAULT_TAGS)
        add_log(topic_id=topic["topic_id"], title="ログ", content="詳細内容")
        act = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", act["activity_id"], [{"type": "topic", "ids": [topic["topic_id"]]}])

        result = get_logs("activity", act["activity_id"])

        assert "error" not in result
        assert len(result["logs"]) == 1
        assert result["logs"][0]["content"] == "詳細内容"

    def test_get_logs_by_activity_ordered_by_id_desc(self, temp_db):
        """activityタイプのlogsはID降順で返る"""
        topic = add_topic(title="トピック", description="Desc", tags=DEFAULT_TAGS)
        l1 = add_log(topic_id=topic["topic_id"], title="古いログ", content="内容1")
        l2 = add_log(topic_id=topic["topic_id"], title="新しいログ", content="内容2")
        act = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", act["activity_id"], [{"type": "topic", "ids": [topic["topic_id"]]}])

        result = get_logs("activity", act["activity_id"])

        assert "error" not in result
        assert len(result["logs"]) == 2
        # ID降順なので新しいログが先（α化により id_raw が元 ID）
        assert result["logs"][0]["id_raw"] == l2["log_id"]
        assert result["logs"][1]["id_raw"] == l1["log_id"]

    def test_get_logs_by_activity_pagination_start_id(self, temp_db):
        """activityタイプのstart_idはID大小でフィルタリングされる"""
        topic = add_topic(title="トピック", description="Desc", tags=DEFAULT_TAGS)
        l1 = add_log(topic_id=topic["topic_id"], title="ログ1", content="内容1")
        l2 = add_log(topic_id=topic["topic_id"], title="ログ2", content="内容2")
        l3 = add_log(topic_id=topic["topic_id"], title="ログ3", content="内容3")
        act = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", act["activity_id"], [{"type": "topic", "ids": [topic["topic_id"]]}])

        # start_id（l2のID）以下を取得（ID降順で全件から絞り込み）
        result = get_logs("activity", act["activity_id"], start_id=l2["log_id"])

        assert "error" not in result
        # l2とl1が返る（l3はl2より大きいIDなので除外）
        # α化により id は文字列 "title (#N)"、元の整数 ID は id_raw に退避
        ids = [l["id_raw"] for l in result["logs"]]
        assert l3["log_id"] not in ids
        assert l2["log_id"] in ids
        assert l1["log_id"] in ids


class TestGetLogsActivityTopicLimit:
    """get_logs(entity_type="activity") のrelated topics上限10件テスト"""

    def test_get_logs_by_activity_limits_related_topics_to_10(self, temp_db):
        """related topicsが10件を超える場合、10件で切られる"""
        # 11個のtopicを作成し、それぞれにログを追加
        topics = []
        for i in range(11):
            t = add_topic(title=f"トピック{i}", description="Desc", tags=DEFAULT_TAGS)
            add_log(topic_id=t["topic_id"], title=f"ログ{i}", content=f"内容{i}")
            topics.append(t)

        act = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation(
            "activity",
            act["activity_id"],
            [{"type": "topic", "ids": [t["topic_id"] for t in topics]}],
        )

        result = get_logs("activity", act["activity_id"])

        assert "error" not in result
        # 11個のtopicがあるが、10件の上限により最大10件分のlogsが返る
        assert len(result["logs"]) == 10


class TestGetLogsInvalidType:
    """get_logs の不正な entity_type テスト"""

    def test_get_logs_invalid_entity_type(self, temp_db):
        """不正なentity_typeでVALIDATION_ERRORが返る"""
        result = get_logs("invalid", 1)

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"


class TestGetDecisionsTopicType:
    """get_decisions(entity_type="topic") の後方互換テスト"""

    def test_get_decisions_by_topic(self, topic):
        """topic_typeでdecisionsが取得できる"""
        tid = topic["topic_id"]
        add_decision(decision="決定1", reason="理由1", topic_id=tid)
        add_decision(decision="決定2", reason="理由2", topic_id=tid)

        result = get_decisions("topic", tid)

        assert "error" not in result
        assert result["topic_id"] == tid
        assert result["topic_name"] == "テストトピック"
        assert len(result["decisions"]) == 2

    def test_get_decisions_by_topic_not_found(self, temp_db):
        """存在しないtopic_idの場合、空リストが返る"""
        result = get_decisions("topic", 99999)

        assert "error" not in result
        assert result["topic_id"] == 99999
        assert result["topic_name"] is None
        assert result["decisions"] == []

    def test_get_decisions_by_topic_pagination(self, topic):
        """start_idによるページネーションが機能する"""
        tid = topic["topic_id"]
        d1 = add_decision(decision="決定1", reason="理由1", topic_id=tid)
        d2 = add_decision(decision="決定2", reason="理由2", topic_id=tid)
        d3 = add_decision(decision="決定3", reason="理由3", topic_id=tid)

        second_id = d2["decision_id"]

        result = get_decisions("topic", tid, start_id=second_id)

        assert "error" not in result
        assert len(result["decisions"]) == 2
        # α化により id は文字列 "title (#N)"、元の整数 ID は id_raw に退避
        ids = [d["id_raw"] for d in result["decisions"]]
        assert d1["decision_id"] not in ids
        assert second_id in ids


class TestGetDecisionsActivityType:
    """get_decisions(entity_type="activity") のテスト"""

    def test_get_decisions_by_activity_via_related_topics(self, temp_db):
        """activity経由でrelated topicsのdecisionsが取得できる"""
        topic = add_topic(title="トピック", description="Desc", tags=DEFAULT_TAGS)
        add_decision(decision="決定1", reason="理由1", topic_id=topic["topic_id"])
        add_decision(decision="決定2", reason="理由2", topic_id=topic["topic_id"])
        act = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", act["activity_id"], [{"type": "topic", "ids": [topic["topic_id"]]}])

        result = get_decisions("activity", act["activity_id"])

        assert "error" not in result
        assert len(result["decisions"]) == 2

    def test_get_decisions_by_activity_no_related_topics(self, activity):
        """related topicsが0件の場合、空リストが返る"""
        result = get_decisions("activity", activity["activity_id"])

        assert "error" not in result
        assert result["decisions"] == []

    def test_get_decisions_by_activity_multiple_topics(self, temp_db):
        """複数のrelated topicsのdecisionsが集約される"""
        t1 = add_topic(title="トピック1", description="Desc", tags=DEFAULT_TAGS)
        t2 = add_topic(title="トピック2", description="Desc", tags=DEFAULT_TAGS)
        add_decision(decision="T1決定", reason="理由", topic_id=t1["topic_id"])
        add_decision(decision="T2決定", reason="理由", topic_id=t2["topic_id"])
        act = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", act["activity_id"], [{"type": "topic", "ids": [t1["topic_id"], t2["topic_id"]]}])

        result = get_decisions("activity", act["activity_id"])

        assert "error" not in result
        assert len(result["decisions"]) == 2
        decisions_text = {d["decision"] for d in result["decisions"]}
        assert "T1決定" in decisions_text
        assert "T2決定" in decisions_text

    def test_get_decisions_by_activity_ordered_by_id_desc(self, temp_db):
        """activityタイプのdecisionsはID降順で返る"""
        topic = add_topic(title="トピック", description="Desc", tags=DEFAULT_TAGS)
        d1 = add_decision(decision="古い決定", reason="理由", topic_id=topic["topic_id"])
        d2 = add_decision(decision="新しい決定", reason="理由", topic_id=topic["topic_id"])
        act = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", act["activity_id"], [{"type": "topic", "ids": [topic["topic_id"]]}])

        result = get_decisions("activity", act["activity_id"])

        assert "error" not in result
        assert len(result["decisions"]) == 2
        # ID降順なので新しい決定が先（α化により id_raw が元 ID）
        assert result["decisions"][0]["id_raw"] == d2["decision_id"]
        assert result["decisions"][1]["id_raw"] == d1["decision_id"]

    def test_get_decisions_by_activity_pagination(self, temp_db):
        """activityタイプのstart_idはID大小でフィルタリングされる"""
        topic = add_topic(title="トピック", description="Desc", tags=DEFAULT_TAGS)
        d1 = add_decision(decision="決定1", reason="理由", topic_id=topic["topic_id"])
        d2 = add_decision(decision="決定2", reason="理由", topic_id=topic["topic_id"])
        d3 = add_decision(decision="決定3", reason="理由", topic_id=topic["topic_id"])
        act = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", act["activity_id"], [{"type": "topic", "ids": [topic["topic_id"]]}])

        # start_id（d2のID）以下を取得
        result = get_decisions("activity", act["activity_id"], start_id=d2["decision_id"])

        assert "error" not in result
        # α化により id は文字列 "title (#N)"、元の整数 ID は id_raw に退避
        ids = [d["id_raw"] for d in result["decisions"]]
        assert d3["decision_id"] not in ids
        assert d2["decision_id"] in ids
        assert d1["decision_id"] in ids


class TestGetDecisionsActivityTopicLimit:
    """get_decisions(entity_type="activity") のrelated topics上限10件テスト"""

    def test_get_decisions_by_activity_limits_related_topics_to_10(self, temp_db):
        """related topicsが10件を超える場合、10件で切られる"""
        topics = []
        for i in range(11):
            t = add_topic(title=f"トピック{i}", description="Desc", tags=DEFAULT_TAGS)
            add_decision(decision=f"決定{i}", reason=f"理由{i}", topic_id=t["topic_id"])
            topics.append(t)

        act = add_activity(title="タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation(
            "activity",
            act["activity_id"],
            [{"type": "topic", "ids": [t["topic_id"] for t in topics]}],
        )

        result = get_decisions("activity", act["activity_id"])

        assert "error" not in result
        # 11個のtopicがあるが、10件の上限により最大10件分のdecisionsが返る
        assert len(result["decisions"]) == 10


class TestGetDecisionsInvalidType:
    """get_decisions の不正な entity_type テスト"""

    def test_get_decisions_invalid_entity_type(self, temp_db):
        """不正なentity_typeでVALIDATION_ERRORが返る"""
        result = get_decisions("invalid", 1)

        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"


class TestGetDecisionsSupersedeMetadata:
    """get_decisions が is_superseded / is_retracted / supersede_chain を返す"""

    def test_solo_decision_marks_chain_as_self_only(self, topic):
        """supersede 関係が無い decision は chain=[self]、is_superseded=False、is_retracted=False"""
        tid = topic["topic_id"]
        d = add_decision(decision="独立", reason="理由", topic_id=tid)

        result = get_decisions("topic", tid)

        assert "error" not in result
        assert len(result["decisions"]) == 1
        item = result["decisions"][0]
        assert item["is_superseded"] is False
        assert item["is_retracted"] is False
        assert item["supersede_chain"] == [d["decision_id"]]

    def test_linear_chain_returns_full_history_ordered(self, topic):
        """d_old→d_new の chain で両方の supersede_chain が [old, new] を返す"""
        tid = topic["topic_id"]
        d_old = add_decision(decision="古", reason="理由", topic_id=tid)
        d_new = add_decision(decision="新", reason="理由", topic_id=tid)
        add_relation(
            "decision", d_new["decision_id"],
            [{"type": "decision", "ids": [d_old["decision_id"]]}],
            relation_type="supersedes",
        )

        result = get_decisions("topic", tid)
        by_id = {item["id_raw"]: item for item in result["decisions"]}

        chain = [d_old["decision_id"], d_new["decision_id"]]
        assert by_id[d_old["decision_id"]]["supersede_chain"] == chain
        assert by_id[d_old["decision_id"]]["is_superseded"] is True
        assert by_id[d_new["decision_id"]]["supersede_chain"] == chain
        assert by_id[d_new["decision_id"]]["is_superseded"] is False

    def test_retracted_decision_flag_true(self, topic):
        """retract 済み decision は is_retracted=True (include_retracted 経由で確認)"""
        tid = topic["topic_id"]
        d = add_decision(decision="retracted", reason="r", topic_id=tid)
        retract("decision", [d["decision_id"]])

        result = get_decisions("topic", tid, include_retracted=True)

        assert "error" not in result
        assert len(result["decisions"]) == 1
        item = result["decisions"][0]
        assert item["is_retracted"] is True


class TestGetDecisionsActivitySupersedeMetadata:
    """get_decisions(entity_type="activity") も is_superseded / is_retracted / supersede_chain を返す"""

    def _activity_for_topic(self, topic_id: int) -> int:
        """topic に belongs_to で紐づく activity を作って activity_id を返す。"""
        act = add_activity(title="[作業] タスク", description="Desc", tags=DEFAULT_TAGS, check_in=False)
        add_relation("activity", act["activity_id"], [{"type": "topic", "ids": [topic_id]}])
        return act["activity_id"]

    def test_solo_decision_marks_chain_as_self_only(self, topic):
        """supersede 関係が無い decision は chain=[self]、is_superseded=False、is_retracted=False"""
        tid = topic["topic_id"]
        d = add_decision(decision="独立", reason="理由", topic_id=tid)
        activity_id = self._activity_for_topic(tid)

        result = get_decisions("activity", activity_id)

        assert "error" not in result
        assert len(result["decisions"]) == 1
        item = result["decisions"][0]
        assert item["is_superseded"] is False
        assert item["is_retracted"] is False
        assert item["supersede_chain"] == [d["decision_id"]]

    def test_linear_chain_returns_full_history_ordered(self, topic):
        """d_old→d_new の chain で両方の supersede_chain が [old, new]、is_superseded が向き別に返る"""
        tid = topic["topic_id"]
        d_old = add_decision(decision="古", reason="理由", topic_id=tid)
        d_new = add_decision(decision="新", reason="理由", topic_id=tid)
        add_relation(
            "decision", d_new["decision_id"],
            [{"type": "decision", "ids": [d_old["decision_id"]]}],
            relation_type="supersedes",
        )
        activity_id = self._activity_for_topic(tid)

        result = get_decisions("activity", activity_id)
        by_id = {item["id_raw"]: item for item in result["decisions"]}

        chain = [d_old["decision_id"], d_new["decision_id"]]
        assert by_id[d_old["decision_id"]]["supersede_chain"] == chain
        assert by_id[d_old["decision_id"]]["is_superseded"] is True
        assert by_id[d_new["decision_id"]]["supersede_chain"] == chain
        assert by_id[d_new["decision_id"]]["is_superseded"] is False

    def test_retracted_decision_flag_true(self, topic):
        """retract 済み decision は is_retracted=True (include_retracted 経由で確認)"""
        tid = topic["topic_id"]
        d = add_decision(decision="retracted", reason="r", topic_id=tid)
        retract("decision", [d["decision_id"]])
        activity_id = self._activity_for_topic(tid)

        result = get_decisions("activity", activity_id, include_retracted=True)

        assert "error" not in result
        assert len(result["decisions"]) == 1
        item = result["decisions"][0]
        assert item["is_retracted"] is True
