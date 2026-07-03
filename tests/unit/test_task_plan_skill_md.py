""".claude/skills/task-plan/SKILL.md の契約テスト

GO判定予測（predicted）決定ステップが plan.md 作成フローに組み込まれていることを assert する。
"""
from pathlib import Path

import pytest

SKILL_MD = (
    Path(__file__).resolve().parent.parent.parent
    / ".claude"
    / "skills"
    / "task-plan"
    / "SKILL.md"
)

STEP_COUNT = 5


@pytest.fixture
def skill_md() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


class TestTaskPlanSkillMdExists:
    def test_skill_md_exists(self):
        assert SKILL_MD.exists(), f".claude/skills/task-plan/SKILL.md が存在しない: {SKILL_MD}"


class TestTaskPlanStepStructure:
    def test_all_steps_present_exactly_once(self, skill_md):
        for n in range(1, STEP_COUNT + 1):
            heading = f"### Step {n}:"
            assert skill_md.count(heading) == 1, f"{heading} が過不足なく1回登場していない"

    def test_steps_in_order(self, skill_md):
        last = -1
        for n in range(1, STEP_COUNT + 1):
            idx = skill_md.find(f"### Step {n}:")
            assert idx > last, f"Step {n} が前のステップより前にある"
            last = idx

    def test_predicted_step_title(self, skill_md):
        assert "### Step 2: GO判定予測（predicted）の決定" in skill_md


class TestPredictedClassificationRule:
    def test_three_classification_values_present(self, skill_md):
        for value in ("pre_go", "gray", "post_veto_candidate"):
            assert value in skill_md, f"分類値 '{value}' の記載が無い"

    def test_type_b_forces_pre_go(self, skill_md):
        assert "類型B（スキーマ/データ変更）" in skill_md
        assert "類型Eのうちデータに触れるもの" in skill_md

    def test_other_types_judged_from_blast_radius(self, skill_md):
        assert "ブラスト半径" in skill_md
        assert "revert容易性" in skill_md
        assert "安全側" in skill_md

    def test_gate_check_referenced_as_machine_source(self, skill_md):
        assert "scripts/gate_check.py" in skill_md


class TestPlanTemplatesCarryPredicted:
    def test_multi_pr_table_has_predicted_column(self, skill_md):
        assert "| # | 類型 | 内容 | ブランチ名 | base | predicted | 状態 |" in skill_md

    def test_subplan_and_single_pr_template_both_have_predicted_field(self, skill_md):
        # サブプランテンプレートと単一PRテンプレートの両方に predicted 欄がある
        assert skill_md.count("## GO判定予測") == 2
        assert skill_md.count("- predicted: {pre_go/gray/post_veto_candidate}") == 2

    def test_final_row_has_no_predicted_value(self, skill_md):
        # 統合マージ行(final)はPRではないのでpredicted値を持たない
        assert "| final | - | 統合マージ | feature/{統合ブランチ名} | main | - | 🔲未着手 |" in skill_md


class TestUserConfirmationIncludesPredicted:
    def test_step5_presents_predicted(self, skill_md):
        step5_idx = skill_md.find("### Step 5: ユーザー確認")
        assert step5_idx != -1
        step5_body = skill_md[step5_idx:]
        assert "GO判定予測（predicted、各PR）" in step5_body
