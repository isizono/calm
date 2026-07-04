"""add_decisions の soft validation（precedent echo / precedent_warnings）テスト。

reason に定型節（却下案:/適用条件:/適用外:/検証:。書式は docs/precedent-format.md）が
あれば precedent コンパクト形が created 要素に echo されること、書式ゆれ等の warning が
あれば precedent_warnings が付くこと、いずれの場合も decision 作成自体は拒否されない
（soft validation）ことを検証する。
"""
import os
import tempfile

import pytest

from src.db import init_database
from src.services.decision_service import add_decisions
from src.services.tag_service import _injected_tags
from src.services.topic_service import add_topic

DEFAULT_TAGS = ["domain:test"]

PRECEDENT_REASON = (
    "自由記述の理由。\n"
    "\n"
    "却下案:\n"
    "- 案A: 理由A\n"
    "\n"
    "適用条件:\n"
    "- 対象領域\n"
    "\n"
    "検証: 実機確認 / 2026-07-04\n"
)

PLAIN_REASON = "普通の理由本文。節は無い。"

# 近似見出し（却下例:）で warning が出るケース
NEAR_MISS_REASON = (
    "自由記述の理由。\n"
    "\n"
    "却下例:\n"
    "- 案A: 理由A\n"
)

# 空節で warning が出るケース（却下案: の直後に項目が無い）
EMPTY_SECTION_REASON = (
    "自由記述の理由。\n"
    "\n"
    "却下案:\n"
    "\n"
    "検証: 実機確認 / 2026-07-04\n"
)


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
    return add_topic(title="判例soft validationテスト", description="テスト用", tags=DEFAULT_TAGS)


class TestPrecedentEcho:
    def test_reason_with_sections_gets_precedent_echo(self, topic):
        tid = topic["topic_id"]
        result = add_decisions([
            {"topic_id": tid, "decision": "採用する", "reason": PRECEDENT_REASON},
        ])

        assert "error" not in result
        assert len(result["created"]) == 1
        created = result["created"][0]
        assert created["precedent"] == {
            "rejected_alternatives": 1,
            "scope": True,
            "verification_anchors": ["実機確認 / 2026-07-04"],
        }
        assert "precedent_warnings" not in created

    def test_reason_without_sections_has_no_precedent_key(self, topic):
        tid = topic["topic_id"]
        result = add_decisions([
            {"topic_id": tid, "decision": "採用する", "reason": PLAIN_REASON},
        ])

        assert "error" not in result
        created = result["created"][0]
        assert "precedent" not in created
        assert "precedent_warnings" not in created


class TestPrecedentWarnings:
    def test_near_miss_heading_produces_warning_but_still_creates(self, topic):
        tid = topic["topic_id"]
        result = add_decisions([
            {"topic_id": tid, "decision": "採用する", "reason": NEAR_MISS_REASON},
        ])

        assert "error" not in result
        assert len(result["created"]) == 1
        assert len(result["errors"]) == 0
        created = result["created"][0]
        assert "precedent_warnings" in created
        assert len(created["precedent_warnings"]) == 1
        assert "却下例" in created["precedent_warnings"][0]

    def test_empty_section_produces_warning_but_still_creates(self, topic):
        tid = topic["topic_id"]
        result = add_decisions([
            {"topic_id": tid, "decision": "採用する", "reason": EMPTY_SECTION_REASON},
        ])

        assert "error" not in result
        assert len(result["created"]) == 1
        created = result["created"][0]
        # 空節は rejected_alternatives には積まれず、warning にのみ現れる
        assert created["precedent"]["rejected_alternatives"] == 0
        assert "precedent_warnings" in created
        assert any("empty section" in w for w in created["precedent_warnings"])

    def test_warnings_do_not_block_decision_creation(self, topic):
        """soft validationの保証: warningがあってもdecision作成自体は拒否されない。"""
        tid = topic["topic_id"]
        result = add_decisions([
            {"topic_id": tid, "decision": "採用する1", "reason": NEAR_MISS_REASON},
            {"topic_id": tid, "decision": "採用する2", "reason": PRECEDENT_REASON},
            {"topic_id": tid, "decision": "採用する3", "reason": PLAIN_REASON},
        ])

        assert "error" not in result
        assert len(result["created"]) == 3
        assert len(result["errors"]) == 0


class TestExistingResponseKeysUnchanged:
    def test_related_decisions_and_propagation_keys_unaffected(self, topic):
        tid = topic["topic_id"]
        result = add_decisions([
            {
                "topic_id": tid,
                "decision": "採用する",
                "reason": PRECEDENT_REASON,
                "propagate_to": {"type": "habit", "content": "テスト用habit"},
            },
        ])

        assert "error" not in result
        created = result["created"][0]
        assert "related_decisions" in created
        assert created["propagation"]["status"] == "ok"
        assert created["propagation"]["type"] == "habit"
        # レスポンス軽量化された既存フィールドは従来通り除去される
        assert "decision" not in created
        assert "reason" not in created
        assert "topic_id" not in created
        assert "tags" not in created
        assert "created_at" not in created
