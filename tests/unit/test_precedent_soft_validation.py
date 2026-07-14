"""add_decisions の soft validation（precedent echo / precedent_warnings）テスト。

reason に定型節（却下案:/適用条件:/適用外:/検証:/隣接確認:。書式は docs/precedent-format.md）が
あれば precedent コンパクト形が created 要素に echo されること、書式ゆれ等の warning が
あれば precedent_warnings が付くこと、いずれの場合も decision 作成自体は拒否されない
（soft validation）ことを検証する。tagsに intent:design を含む decision で「隣接確認:」節が
無い場合の nudge warning（TestAdjacentCheckWarning）も対象に含む。
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

ADJACENT_CHECK_REASON = (
    "自由記述の理由。\n"
    "\n"
    "隣接確認:\n"
    "- 実行時: 誰が起動するか確認した\n"
    "- 関連既決との整合: 既存decisionと矛盾しないか確認した\n"
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
            "adjacent_check": [],
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


class TestAdjacentCheckWarning:
    """intent:design タグ付き decision の「隣接確認:」節 soft validation"""

    def test_design_item_without_section_gets_warning(self, topic):
        tid = topic["topic_id"]
        result = add_decisions([
            {
                "topic_id": tid,
                "decision": "採用する",
                "reason": PLAIN_REASON,
                "tags": ["intent:design"],
            },
        ])

        assert "error" not in result
        created = result["created"][0]
        assert "precedent_warnings" in created
        assert any(
            "intent:design" in w and "隣接確認" in w for w in created["precedent_warnings"]
        )

    def test_design_item_with_section_has_no_warning(self, topic):
        tid = topic["topic_id"]
        result = add_decisions([
            {
                "topic_id": tid,
                "decision": "採用する",
                "reason": ADJACENT_CHECK_REASON,
                "tags": ["intent:design"],
            },
        ])

        assert "error" not in result
        created = result["created"][0]
        assert "precedent_warnings" not in created
        assert created["precedent"]["adjacent_check"] == [
            "実行時: 誰が起動するか確認した",
            "関連既決との整合: 既存decisionと矛盾しないか確認した",
        ]

    def test_non_design_item_without_section_has_no_warning(self, topic):
        tid = topic["topic_id"]
        result = add_decisions([
            {"topic_id": tid, "decision": "採用する", "reason": PLAIN_REASON},
        ])

        assert "error" not in result
        created = result["created"][0]
        assert "precedent_warnings" not in created

    def test_warning_merges_with_existing_precedent_warnings(self, topic):
        tid = topic["topic_id"]
        result = add_decisions([
            {
                "topic_id": tid,
                "decision": "採用する",
                "reason": NEAR_MISS_REASON,
                "tags": ["intent:design"],
            },
        ])

        assert "error" not in result
        created = result["created"][0]
        assert "precedent_warnings" in created
        assert len(created["precedent_warnings"]) == 2
        assert any("却下例" in w for w in created["precedent_warnings"])
        assert any("intent:design" in w for w in created["precedent_warnings"])
        # precedent.warnings（ネスト）とprecedent_warnings（トップレベル）は
        # 同一ソースから導出され、内容が一致していなければならない
        assert created["precedent"]["warnings"] == created["precedent_warnings"]

    def test_warning_matches_nested_precedent_warnings_when_section_present(self, topic):
        """節がある場合、precedent.warningsとprecedent_warningsが食い違わないこと。"""
        tid = topic["topic_id"]
        result = add_decisions([
            {
                "topic_id": tid,
                "decision": "採用する",
                "reason": PLAIN_REASON,
                "tags": ["intent:design"],
            },
        ])

        assert "error" not in result
        created = result["created"][0]
        # PLAIN_REASONは節が一つも無いのでprecedentキー自体は新設されない
        # （legacy本文との区別を崩さない）。precedent_warningsのみ単独で付く。
        assert "precedent" not in created
        assert created["precedent_warnings"] == [
            "intent:design decision missing '隣接確認:' section "
            "(axes to consider: 実行時, 関連既決との整合)"
        ]


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
