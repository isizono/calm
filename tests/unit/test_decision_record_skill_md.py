"""skills/decision-record/SKILL.md の契約テスト

recording skill と対をなす decision 記録ガイドの SKILL.md に、必須の
frontmatter要素・主要セクションが揃っていることを assert する軽量なテスト。
"""
from pathlib import Path

import pytest

from tests.helpers import all_tool_descriptions

SKILL_MD = (
    Path(__file__).resolve().parent.parent.parent
    / "skills"
    / "decision-record"
    / "SKILL.md"
)

SYNC_MEMORY_SKILL_MD = (
    Path(__file__).resolve().parent.parent.parent
    / "skills"
    / "sync-memory"
    / "SKILL.md"
)


@pytest.fixture
def skill_md() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


@pytest.fixture
def sync_memory_skill_md() -> str:
    return SYNC_MEMORY_SKILL_MD.read_text(encoding="utf-8")


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

    def test_references_canonical_format_doc(self, skill_md):
        # 書式の正本はdocs/precedent-format.mdであり、本スキルは複製しない
        assert "docs/precedent-format.md" in skill_md
        assert "正本" in skill_md

    def test_does_not_duplicate_full_template(self, skill_md):
        # precedent-format.md固有のプレースホルダ付きfenced templateを複製していないこと
        assert "<案の要約>: <却下理由>" not in skill_md
        assert "<対象コミットSHA・バージョン等>" not in skill_md

    def test_mentions_near_miss_headings(self, skill_md):
        # precedent_warningsの原因診断として近似見出し（docs/precedent-format.md 4章）に触れる
        for near_miss in (
            "却下例",
            "棄却案",
            "不採用案",
            "適用範囲",
            "対象外",
            "検証済み",
            "rejected",
            "scope",
        ):
            assert near_miss in skill_md, f"近似見出し '{near_miss}' への言及が無い"


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


class TestDecisionRecordSyncMemoryCanonicalSource:
    """decision-record ⇔ sync-memory 間の重複記述に正本規定があることを検証する。

    sync-memory は本スキルの発動を前提とせず単体で完結する必要があるため
    合意判定基準・[議論中]記録基準を意図的にインラインで残す（docs/recording-taxonomy.md
    の「各skill本文は自己完結」方針と同じ）。重複そのものは許容するが、食い違った
    場合にどちらを正とするかの規定が両ファイルに要る。
    """

    def test_decision_record_declares_itself_canonical(self, skill_md):
        assert "正本とする" in skill_md
        assert "sync-memory" in skill_md

    def test_sync_memory_step4_references_decision_record(self, sync_memory_skill_md):
        assert "### 4. 決定事項・ログの記録" in sync_memory_skill_md
        assert "decision-record" in sync_memory_skill_md
        assert "合意判定基準" in sync_memory_skill_md

    def test_sync_memory_step6_references_decision_record(self, sync_memory_skill_md):
        assert "### 6. 未決定の論点を記録" in sync_memory_skill_md
        assert "未決論点の記録" in sync_memory_skill_md


class TestAddDecisionsToolGatedByDecisionRecordSkill:
    """add_decisionsのdocstringにdecision-record skillへの誘導があることを検証する。

    add_logsのdocstring（recording skillへの誘導）と対称にする。
    """

    def test_add_decisions_mentions_decision_record_skill(self):
        desc = all_tool_descriptions()["add_decisions"]
        assert "decision-record" in desc

    def test_add_logs_mentions_recording_skill(self):
        # 対称性の回帰確認: add_logs側の既存の誘導文言が壊れていないこと
        desc = all_tool_descriptions()["add_logs"]
        assert "recording" in desc
