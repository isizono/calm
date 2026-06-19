"""add_decisions / add_topic MCPツールの worker ガード統合テスト。

guard_service.check_worker_guard が main.py の MCP ツール冒頭で正しく実行され、
worker セッションから直接呼ばれた場合に WorkerGuardError が raise されること、
OW_ESCALATION=1 / OW_ROLE 未設定 / OW_ROLE=orch の場合は通過してツール本体が
実行されることを検証する。

add_logs は worker-sync の退場処理で必須の直接呼び出しになるためガード対象外。
本テストでも add_logs はカバーしない (回帰検出は既存 batch logs テストに任せる)。
"""
import os
import tempfile
from unittest.mock import MagicMock

import pytest

from src.db import init_database
from src.services.guard_service import WorkerGuardError
from src.services.topic_service import add_topic as service_add_topic


@pytest.fixture
def temp_db():
    """テスト用の一時SQLite DB。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


@pytest.fixture(autouse=True)
def _auto_disable_embedding(disable_embedding):
    """このファイル内の全テストで embedding を無効化する。"""


@pytest.fixture
def existing_topic(temp_db):
    """ガード通過後の本体実行で使う既存トピックを1件用意し、id を返す。"""
    result = service_add_topic(
        title="Guard test topic",
        description="Topic prepared for guard pass-through tests.",
        tags=["domain:test"],
    )
    return result["topic_id"]


class TestAddTopicWorkerGuard:
    """add_topic への worker ガード。"""

    def test_worker_session_raises(self, temp_db, monkeypatch):
        """OW_ROLE=worker のとき WorkerGuardError を raise する。"""
        from src.main import add_topic

        monkeypatch.setenv("OW_ROLE", "worker")
        with pytest.raises(WorkerGuardError) as exc_info:
            add_topic(
                title="Should fail",
                description="Worker must not write directly.",
                tags=["domain:test"],
            )
        assert "add_topic" in str(exc_info.value)

    def test_worker_with_escalation_passes(self, temp_db, monkeypatch):
        """OW_ROLE=worker かつ OW_ESCALATION=1 → ガード通過して topic 作成。"""
        from src.main import add_topic

        monkeypatch.setenv("OW_ROLE", "worker")
        monkeypatch.setenv("OW_ESCALATION", "1")
        result = add_topic(
            title="Escalation passes",
            description="orch_proxy escalation should pass through.",
            tags=["domain:test"],
        )
        assert "error" not in result
        assert "topic_id" in result

    def test_orch_role_passes(self, temp_db, monkeypatch):
        """OW_ROLE=orch → ガード通過。"""
        from src.main import add_topic

        monkeypatch.setenv("OW_ROLE", "orch")
        result = add_topic(
            title="Orch passes",
            description="orch may write directly.",
            tags=["domain:test"],
        )
        assert "error" not in result
        assert "topic_id" in result

    def test_no_role_passes(self, temp_db, monkeypatch):
        """OW_ROLE 未設定 (通常セッション) → ガード通過。"""
        from src.main import add_topic

        monkeypatch.delenv("OW_ROLE", raising=False)
        result = add_topic(
            title="Normal session",
            description="No OW_ROLE: regular write path.",
            tags=["domain:test"],
        )
        assert "error" not in result
        assert "topic_id" in result


class TestAddLogsBypassesGuard:
    """add_logs はガード対象外 (worker-sync 退場処理で必須直接呼び出し)。"""

    def test_worker_session_passes_through(self, temp_db, existing_topic, monkeypatch):
        """OW_ROLE=worker でも add_logs はガードに引っかからず通過する。"""
        from src.main import add_logs

        monkeypatch.setenv("OW_ROLE", "worker")
        monkeypatch.delenv("OW_ESCALATION", raising=False)
        result = add_logs(items=[{
            "topic_id": existing_topic,
            "content": "worker-sync exit log payload.",
        }])
        assert "error" not in result
        assert "created" in result


class TestAddDecisionsWorkerGuard:
    """add_decisions への worker ガード。"""

    def _make_ctx(self):
        """add_decisions は ctx.session_id を参照するため最小ダミーを渡す。"""
        ctx = MagicMock()
        ctx.session_id = "test-session"
        return ctx

    def test_worker_session_raises(self, temp_db, monkeypatch):
        """OW_ROLE=worker のとき WorkerGuardError を raise する。

        ctx は guard 通過前に raise されるため参照されない。
        """
        from src.main import add_decisions

        monkeypatch.setenv("OW_ROLE", "worker")
        with pytest.raises(WorkerGuardError) as exc_info:
            add_decisions(
                items=[{
                    "topic_id": 1,
                    "decision": "blocked",
                    "reason": "blocked",
                }],
                ctx=self._make_ctx(),
            )
        assert "add_decisions" in str(exc_info.value)

    def test_worker_with_escalation_passes(self, temp_db, existing_topic, monkeypatch):
        """OW_ROLE=worker かつ OW_ESCALATION=1 → ガード通過。"""
        from src.main import add_decisions

        monkeypatch.setenv("OW_ROLE", "worker")
        monkeypatch.setenv("OW_ESCALATION", "1")
        result = add_decisions(
            items=[{
                "topic_id": existing_topic,
                "decision": "Pass through under escalation.",
                "reason": "orch_proxy escalation path.",
            }],
            ctx=self._make_ctx(),
        )
        assert "error" not in result
        assert "created" in result

    def test_orch_role_passes(self, temp_db, existing_topic, monkeypatch):
        """OW_ROLE=orch → ガード通過。"""
        from src.main import add_decisions

        monkeypatch.setenv("OW_ROLE", "orch")
        result = add_decisions(
            items=[{
                "topic_id": existing_topic,
                "decision": "Orch decision.",
                "reason": "Orch may record directly.",
            }],
            ctx=self._make_ctx(),
        )
        assert "error" not in result
        assert "created" in result

    def test_no_role_passes(self, temp_db, existing_topic, monkeypatch):
        """OW_ROLE 未設定 → ガード通過。"""
        from src.main import add_decisions

        monkeypatch.delenv("OW_ROLE", raising=False)
        result = add_decisions(
            items=[{
                "topic_id": existing_topic,
                "decision": "Normal decision.",
                "reason": "Regular session.",
            }],
            ctx=self._make_ctx(),
        )
        assert "error" not in result
        assert "created" in result


class TestGuardMessageContent:
    """raise されたメッセージで案内文言が含まれることを統合経由で確認する。"""

    def test_message_includes_guidance(self, temp_db, monkeypatch):
        """メッセージで orch 経由の記録と OW_ESCALATION=1 通過手段を案内する。"""
        from src.main import add_topic

        monkeypatch.setenv("OW_ROLE", "worker")
        with pytest.raises(WorkerGuardError) as exc_info:
            add_topic(title="x", description="x", tags=["domain:test"])
        message = str(exc_info.value)
        assert "orch" in message
        assert "OW_ESCALATION=1" in message
