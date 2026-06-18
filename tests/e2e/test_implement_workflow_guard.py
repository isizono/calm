"""IMPLEMENT_WORKFLOW_GUARD のE2E（MCP tool 呼び出し経由）

src.main の add_activity（@mcp.tool() で公開される実体）を呼び、
intent:implement かつ related に decision を含まないケースが
IMPLEMENT_WORKFLOW_GUARD で弾かれることを確認する。

temp_db / disable_embedding フィクスチャは tests/conftest.py で共有。
"""
import pytest

from src.main import add_activity as mcp_add_activity
from src.services.topic_service import add_topic
from tests.helpers import add_decision


@pytest.fixture(autouse=True)
def _auto_disable_embedding(disable_embedding):
    """このファイル内の全テストでembedding服を無効化する"""


def test_mcp_add_activity_implement_without_decision_is_blocked(temp_db):
    """MCP tool add_activity が intent:implement + decision relate なしで弾かれる"""
    result = mcp_add_activity(
        title="Implement without decision",
        description="MCP-tool level call should reject bare implement.",
        tags=["domain:test", "intent:implement"],
        check_in=False,
    )
    assert "error" in result
    assert result["error"]["code"] == "IMPLEMENT_WORKFLOW_GUARD"


def test_mcp_add_activity_implement_with_decision_passes(temp_db):
    """MCP tool add_activity が intent:implement + decision relate ありで通過する"""
    topic = add_topic(
        title="E2E topic",
        description="Topic for e2e guard test.",
        tags=["domain:test"],
    )
    decision = add_decision(
        decision="Agreed implementation approach.",
        reason="Discussion completed.",
        topic_id=topic["topic_id"],
    )

    result = mcp_add_activity(
        title="Implement with decision",
        description="MCP-tool level call should pass with decision related.",
        tags=["domain:test", "intent:implement"],
        related=[{"type": "decision", "ids": [decision["decision_id"]]}],
        check_in=False,
    )
    assert "error" not in result
    assert "activity_id" in result
