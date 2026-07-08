"""skills/decision-record/SKILL.md の契約テスト

recording skill と対をなす decision 記録ガイドの SKILL.md に、必須の
frontmatter要素・主要セクションが揃っていることを assert する軽量なテスト。
"""
from pathlib import Path

import pytest

SKILL_MD = (
    Path(__file__).resolve().parent.parent.parent
    / "skills"
    / "decision-record"
    / "SKILL.md"
)


@pytest.fixture
def skill_md() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


class TestDecisionRecordSkillFrontmatter:
    def test_skill_md_exists(self):
        assert SKILL_MD.exists(), f"skills/decision-record/SKILL.md が存在しない: {SKILL_MD}"

    def test_name_field(self, skill_md):
        assert "name: decision-record" in skill_md

    def test_description_has_required_marker(self, skill_md):
        assert "【必須】" in skill_md

    def test_description_has_forbid_clause(self, skill_md):
        assert "このスキルを経由せずに" in skill_md
        assert "add_decisions" in skill_md

    def test_description_has_trigger_timing(self, skill_md):
        assert "OK" in skill_md
        assert "[議論中]" in skill_md

    def test_description_has_boundary_clause(self, skill_md):
        # recording (add_logs/add_material) は対象外であることの明示
        assert "add_logs" in skill_md
        assert "add_material" in skill_md
        assert "recording" in skill_md


class TestDecisionRecordSkillAgreementCriteria:
    def test_agreement_criteria_section(self, skill_md):
        assert "## 合意判定基準" in skill_md

    def test_agreement_criteria_examples(self, skill_md):
        for phrase in ("OK", "それでいこう", "〜で決定"):
            assert phrase in skill_md, f"合意判定基準の例 '{phrase}' が無い"

    def test_no_agreement_no_decision(self, skill_md):
        assert "ユーザー合意のないものを decision として記録しない" in skill_md


class TestDecisionRecordSkillOpenIssues:
    def test_open_issue_section(self, skill_md):
        assert "[議論中]" in skill_md

    def test_open_issue_no_agreement_needed(self, skill_md):
        assert "合意なしに記録してよい" in skill_md

    def test_mikan_prefix_mentioned_neutrally(self, skill_md):
        # [未完] を「decisionでは使わない」と否定せず、sync-memory側の整理観点として言及する
        assert "[未完]" in skill_md
        assert "sync-memory" in skill_md


class TestDecisionRecordSkillQualityBar:
    def test_quality_bar_section(self, skill_md):
        assert "## 呼び出し時の品質基準" in skill_md

    def test_title_recommendation(self, skill_md):
        assert "title" in skill_md
        assert "強く推奨" in skill_md

    def test_topic_id_required(self, skill_md):
        assert "topic_id" in skill_md
        assert "必須" in skill_md

    def test_tags_domain_required(self, skill_md):
        assert "domain:" in skill_md

    def test_reason_decision_separation(self, skill_md):
        assert "reason" in skill_md
        assert "decision" in skill_md


class TestDecisionRecordSkillPrecedentFormat:
    def test_precedent_section_exists(self, skill_md):
        assert "## precedent定型節" in skill_md

    def test_four_section_headings(self, skill_md):
        for heading in ("却下案:", "適用条件:", "適用外:", "検証:"):
            assert heading in skill_md, f"precedent節見出し '{heading}' が無い"

    def test_no_dummy_sections_rule(self, skill_md):
        assert "空項目・ダミー項目は書かない" in skill_md

    def test_response_echo_check(self, skill_md):
        assert "precedent_warnings" in skill_md

    def test_warnings_are_soft_validation(self, skill_md):
        # warningがあってもdecision作成自体は拒否されないことの明記
        assert "soft validation" in skill_md
        assert "拒否" in skill_md


class TestDecisionRecordSkillContradiction:
    def test_contradiction_section_exists(self, skill_md):
        assert "## 矛盾・重複への対処" in skill_md

    def test_related_decisions_and_report_signal(self, skill_md):
        assert "related_decisions" in skill_md
        assert 'report_signal(kind="contradiction")' in skill_md


class TestDecisionRecordSkillBoundaries:
    def test_recording_boundary_section(self, skill_md):
        assert "## recording との境界" in skill_md

    def test_sync_memory_relation_section(self, skill_md):
        assert "## sync-memory との関係" in skill_md
