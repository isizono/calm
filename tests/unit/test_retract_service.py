"""retract_service のテスト

エンティティ（decision, log）のretract/un-retract操作、
冪等性、部分成功、バリデーションエラーをカバーする。
"""
import os
import tempfile
import pytest

from src.db import init_database, get_connection
from src.services.topic_service import add_topic
from src.services.discussion_log_service import add_logs
from src.services.decision_service import add_decisions
from src.services.retract_service import retract
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


class TestRetractDecision:
    """decisionのretract"""

    def test_retract_decision(self, topic):
        """decisionをretractできる"""
        tid = topic["topic_id"]
        result = add_decisions([
            {"topic_id": tid, "decision": "テスト決定", "reason": "テスト理由"},
        ])
        decision_id = result["created"][0]["decision_id"]

        retract_result = retract("decision", [decision_id])
        assert "error" not in retract_result
        assert decision_id in retract_result["success"]
        assert retract_result["errors"] == []

        # DB上でもretracted_atが設定されていることを確認
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT retracted_at FROM decisions WHERE id = ?", (decision_id,)
            ).fetchone()
            assert row["retracted_at"] is not None
        finally:
            conn.close()

    def test_retract_multiple_decisions(self, topic):
        """複数のdecisionを一括retractできる"""
        tid = topic["topic_id"]
        result = add_decisions([
            {"topic_id": tid, "decision": "決定1", "reason": "理由1"},
            {"topic_id": tid, "decision": "決定2", "reason": "理由2"},
        ])
        ids = [c["decision_id"] for c in result["created"]]

        retract_result = retract("decision", ids)
        assert len(retract_result["success"]) == 2
        assert retract_result["errors"] == []


class TestUnretractDecision:
    """decisionのun-retract"""

    def test_unretract_decision(self, topic):
        """retract済みdecisionをun-retractできる"""
        tid = topic["topic_id"]
        result = add_decisions([
            {"topic_id": tid, "decision": "テスト決定", "reason": "テスト理由"},
        ])
        decision_id = result["created"][0]["decision_id"]

        # retract → un-retract
        retract("decision", [decision_id])
        unretract_result = retract("decision", [decision_id], undo=True)

        assert "error" not in unretract_result
        assert decision_id in unretract_result["success"]

        # DB上でretracted_atがNULLに戻っていることを確認
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT retracted_at FROM decisions WHERE id = ?", (decision_id,)
            ).fetchone()
            assert row["retracted_at"] is None
        finally:
            conn.close()


class TestRetractLog:
    """logのretract"""

    def test_retract_log(self, topic):
        """logをretractできる"""
        tid = topic["topic_id"]
        result = add_logs([
            {"topic_id": tid, "content": "テストログ内容", "title": "テストログ"},
        ])
        log_id = result["created"][0]["log_id"]

        retract_result = retract("log", [log_id])
        assert "error" not in retract_result
        assert log_id in retract_result["success"]

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT retracted_at FROM discussion_logs WHERE id = ?", (log_id,)
            ).fetchone()
            assert row["retracted_at"] is not None
        finally:
            conn.close()

    def test_unretract_log(self, topic):
        """retract済みlogをun-retractできる"""
        tid = topic["topic_id"]
        result = add_logs([
            {"topic_id": tid, "content": "テストログ内容", "title": "テストログ"},
        ])
        log_id = result["created"][0]["log_id"]

        retract("log", [log_id])
        unretract_result = retract("log", [log_id], undo=True)

        assert "error" not in unretract_result
        assert log_id in unretract_result["success"]

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT retracted_at FROM discussion_logs WHERE id = ?", (log_id,)
            ).fetchone()
            assert row["retracted_at"] is None
        finally:
            conn.close()


class TestRetractIdempotent:
    """retract操作の冪等性"""

    def test_retract_twice_no_error(self, topic):
        """既にretracted状態でretractしても成功する"""
        tid = topic["topic_id"]
        result = add_decisions([
            {"topic_id": tid, "decision": "テスト決定", "reason": "テスト理由"},
        ])
        decision_id = result["created"][0]["decision_id"]

        result1 = retract("decision", [decision_id])
        result2 = retract("decision", [decision_id])

        assert "error" not in result1
        assert "error" not in result2
        assert decision_id in result2["success"]

    def test_unretract_nonretracted_no_error(self, topic):
        """retractされていない状態でun-retractしても成功する"""
        tid = topic["topic_id"]
        result = add_decisions([
            {"topic_id": tid, "decision": "テスト決定", "reason": "テスト理由"},
        ])
        decision_id = result["created"][0]["decision_id"]

        unretract_result = retract("decision", [decision_id], undo=True)
        assert "error" not in unretract_result
        assert decision_id in unretract_result["success"]


class TestRetractPartialSuccess:
    """部分成功"""

    def test_partial_success_with_nonexistent_id(self, topic):
        """存在するID + 存在しないIDで部分成功する"""
        tid = topic["topic_id"]
        result = add_decisions([
            {"topic_id": tid, "decision": "テスト決定", "reason": "テスト理由"},
        ])
        decision_id = result["created"][0]["decision_id"]

        retract_result = retract("decision", [decision_id, 99999])
        assert decision_id in retract_result["success"]
        assert len(retract_result["errors"]) == 1
        assert retract_result["errors"][0]["id"] == 99999
        assert "not found" in retract_result["errors"][0]["error"]["message"]


class TestRetractValidationErrors:
    """バリデーションエラー"""

    def test_invalid_entity_type_material(self, temp_db):
        """materialはretract対象外でバリデーションエラーになる"""
        result = retract("material", [1])
        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "Invalid entity_type" in result["error"]["message"]

    def test_invalid_entity_type_topic(self, temp_db):
        """topicはretract対象外でバリデーションエラーになる"""
        result = retract("topic", [1])
        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_empty_ids(self, temp_db):
        """空のidsでバリデーションエラーになる"""
        result = retract("decision", [])
        assert "error" in result
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "ids must not be empty" in result["error"]["message"]


class TestRetractWithPin:
    """pinned + retractの組み合わせ"""

    def test_retract_pinned_decision(self, topic):
        """pinされたdecisionもretractできる"""
        tid = topic["topic_id"]
        result = add_decisions([
            {"topic_id": tid, "decision": "pinされた決定", "reason": "理由"},
        ])
        decision_id = result["created"][0]["decision_id"]

        # pin（DBのpinned列を直接設定）→ retract
        conn = get_connection()
        try:
            conn.execute("UPDATE decisions SET pinned = 1 WHERE id = ?", (decision_id,))
            conn.commit()
        finally:
            conn.close()
        retract_result = retract("decision", [decision_id])

        assert "error" not in retract_result
        assert decision_id in retract_result["success"]

        # pinned=1かつretracted_atが設定されていることを確認
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT pinned, retracted_at FROM decisions WHERE id = ?", (decision_id,)
            ).fetchone()
            assert row["pinned"] == 1
            assert row["retracted_at"] is not None
        finally:
            conn.close()
