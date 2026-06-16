"""intent:thinking タグ（思考worker向けintent）のmigration挙動と利用可能性テスト。

migration 0038 で intent:thinking が tags テーブルに INSERT され、description と notes が
設定される。add_activity から intent:thinking タグを付けたactivityを作成できることも検証する。
"""
import os
import tempfile

import pytest

from src.db import get_connection, init_database
from src.services.activity_service import add_activity
from src.services.tag_service import _injected_tags, get_available_intents


@pytest.fixture
def temp_db():
    """migration全件適用済みの一時DB"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        os.environ["DISCUSSION_DB_PATH"] = db_path
        init_database()
        _injected_tags.clear()
        yield db_path
        if "DISCUSSION_DB_PATH" in os.environ:
            del os.environ["DISCUSSION_DB_PATH"]


class TestIntentThinkingMigration:
    """migration 0038_intent_thinking の挙動"""

    def test_intent_thinking_tag_exists_after_migration(self, temp_db):
        """migration適用後 tags テーブルに intent:thinking が存在する"""
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT name FROM tags WHERE namespace = 'intent' AND name = 'thinking'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row["name"] == "thinking"

    def test_intent_thinking_has_description(self, temp_db):
        """intent:thinking に description が設定されている"""
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT description FROM tags WHERE namespace = 'intent' AND name = 'thinking'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row["description"] is not None
        assert len(row["description"]) > 0

    def test_intent_thinking_has_notes(self, temp_db):
        """intent:thinking に notes（振る舞いガイド）が設定されている"""
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT notes FROM tags WHERE namespace = 'intent' AND name = 'thinking'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row["notes"] is not None
        # 思考workerの境界・ultratink マーカー言及を含む
        assert "ultratink" in row["notes"]

    def test_intent_thinking_in_available_intents(self, temp_db):
        """intent:thinking が get_available_intents の返り値に含まれる"""
        intents = get_available_intents()
        intent_names = [i["tag"] for i in intents]
        assert "intent:thinking" in intent_names


class TestIntentThinkingUsage:
    """add_activity から intent:thinking タグを使う"""

    def test_add_activity_with_intent_thinking(self, temp_db):
        """intent:thinking タグ付きでactivityを作成でき、DBにタグが紐づく"""
        result = add_activity(
            title="思考タスク",
            description="深い議論を行う",
            tags=["domain:test", "intent:thinking"],
            check_in=False,
        )
        assert "error" not in result
        activity_id = result["activity_id"]

        # activity_tags 経由で intent:thinking が紐づいているか確認
        conn = get_connection()
        try:
            rows = conn.execute(
                """
                SELECT t.namespace, t.name FROM tags t
                JOIN activity_tags at ON at.tag_id = t.id
                WHERE at.activity_id = ?
                """,
                (activity_id,),
            ).fetchall()
        finally:
            conn.close()
        tag_strs = {f"{r['namespace']}:{r['name']}" for r in rows}
        assert "intent:thinking" in tag_strs
