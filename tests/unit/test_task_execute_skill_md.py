""".claude/skills/task-execute/SKILL.md の契約テスト

判例確認（pull_precedents）必須ステップと、GO判定パッケージ生成・shadow人間判定
記録・PR番号追記・material保存の各手順が実装フローに組み込まれていることを assert する。
"""
from pathlib import Path

import pytest

SKILL_MD = (
    Path(__file__).resolve().parent.parent.parent
    / ".claude"
    / "skills"
    / "task-execute"
    / "SKILL.md"
)

STEP_COUNT = 9


@pytest.fixture
def skill_md() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


class TestTaskExecuteSkillMdExists:
    def test_skill_md_exists(self):
        assert SKILL_MD.exists(), f".claude/skills/task-execute/SKILL.md が存在しない: {SKILL_MD}"


class TestTaskExecuteStepStructure:
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

    def test_re_delegate_loops_back_to_review_step(self, skill_md):
        # RE_DELEGATEはレビューSA起動ステップ(Step 5)に戻る
        assert "→ 修正SA再起動 → Step 5 に戻る" in skill_md


class TestPullPrecedentsStep:
    def test_step2_title(self, skill_md):
        assert "### Step 2: 判例確認（pull_precedents）" in skill_md

    def test_pull_precedents_tool_invoked(self, skill_md):
        assert "`pull_precedents` を `context`" in skill_md

    def test_response_saved_to_iterations(self, skill_md):
        assert "iterations/{nn}-pull.json" in skill_md

    def test_guarantee_values_handled(self, skill_md):
        for value in ("routing_miss", "routing_unavailable", "enumerated"):
            assert value in skill_md, f"guarantee値 '{value}' の扱いが記載されていない"

    def test_skippable_before_pull_mechanism_live(self, skill_md):
        # pull_precedentsツール未稼働時は省略可という注記がある
        assert "省略してよい" in skill_md
        assert "unavailable" in skill_md


class TestGoPackageGeneration:
    def test_step7_title(self, skill_md):
        assert "### Step 7: コミット + GO判定パッケージ + ユーザーへ報告" in skill_md

    def test_commit_precedes_package_generation(self, skill_md):
        step7_idx = skill_md.find("### Step 7:")
        step8_idx = skill_md.find("### Step 8:")
        step7_body = skill_md[step7_idx:step8_idx]
        commit_idx = step7_body.find("コミットを作成する")
        package_idx = step7_body.find("go_package.py new")
        assert commit_idx != -1
        assert package_idx != -1
        assert commit_idx < package_idx, "コミットがGO判定パッケージ生成より後になっている"

    def test_go_package_new_invocation(self, skill_md):
        assert "uv run python scripts/go_package.py new --activity" in skill_md
        assert "--pull-json iterations/{nn}-pull.json" in skill_md
        assert "--out iterations/{nn}-go-package.md" in skill_md

    def test_placeholder_lint_before_human_sections_filled(self, skill_md):
        assert "go_package.py lint iterations/{nn}-go-package.md --mode shadow --allow-placeholder" in skill_md

    def test_final_lint_without_placeholder(self, skill_md):
        assert "go_package.py lint iterations/{nn}-go-package.md --mode shadow`\n   を（`--allow-placeholder` なしで）再度通す" in skill_md

    def test_empty_precedent_sections_must_say_none(self, skill_md):
        assert "「なし」と明記する（空欄禁止）" in skill_md


class TestShadowHumanJudgment:
    def test_report_template_asks_shadow_question(self, skill_md):
        assert "コード読みが必要な類（事前go相当）" in skill_md
        assert "パッケージだけで判断できる類（事後拒否権相当）" in skill_md

    def test_answer_recorded_into_machine_block(self, skill_md):
        assert "shadow.human" in skill_md
        assert "shadow.divergence" in skill_md
        assert "docs/spec/go-gate.md" in skill_md


class TestPrCreationStep:
    def test_step8_title(self, skill_md):
        assert "### Step 8: PR作成" in skill_md

    def test_pr_number_appended_to_prs_field(self, skill_md):
        step8_idx = skill_md.find("### Step 8:")
        step9_idx = skill_md.find("### Step 9:")
        step8_body = skill_md[step8_idx:step9_idx]
        assert "`prs` フィールドに" in step8_body

    def test_material_saved_with_required_tags(self, skill_md):
        step8_idx = skill_md.find("### Step 8:")
        step9_idx = skill_md.find("### Step 9:")
        step8_body = skill_md[step8_idx:step9_idx]
        assert "add_material" in step8_body
        assert "go-package" in step8_body
        assert "domain:cc-memory" in step8_body

    def test_package_not_embedded_in_pr_body(self, skill_md):
        assert "GO判定パッケージはPR本文には" in skill_md
        assert "載せない" in skill_md


class TestIntegrationMergeCheckStep:
    def test_step9_title(self, skill_md):
        assert "### Step 9: 統合マージチェック" in skill_md

    def test_step9_references_step8_completion(self, skill_md):
        assert "Step 8完了後" in skill_md


class TestIterationsDirectoryListing:
    def test_pull_json_and_go_package_listed(self, skill_md):
        assert "01-pull.json" in skill_md
        assert "01-go-package.md" in skill_md
