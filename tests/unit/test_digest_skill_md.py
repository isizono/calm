"""skills/digest/SKILL.md の契約テスト

frontmatterの必須要素と主要見出しの存在をassertする軽量テスト。
"""
from pathlib import Path

import pytest

SKILL_MD = (
    Path(__file__).resolve().parent.parent.parent
    / "skills"
    / "digest"
    / "SKILL.md"
)


@pytest.fixture
def skill_md() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


class TestDigestSkillFrontmatter:
    def test_skill_md_exists(self):
        assert SKILL_MD.exists(), f"skills/digest/SKILL.md が存在しない: {SKILL_MD}"

    def test_name_field(self, skill_md):
        assert "name: digest" in skill_md

    def test_description_has_trigger_phrases(self, skill_md):
        for phrase in (
            "/digest",
            "最近何やったっけ",
            "今週のまとめ",
            "ここ数日の動きを見せて",
            "先週から何が決まった",
        ):
            assert phrase in skill_md, f"トリガーフレーズ '{phrase}' の記載が無い"

    def test_description_has_non_trigger_boundary(self, skill_md):
        for skill_name in ("check-in", "postmortem", "scribe"):
            assert skill_name in skill_md, f"非発動境界として '{skill_name}' への言及が無い"

    def test_description_has_variation_marker(self, skill_md):
        assert "など" in skill_md


class TestDigestSkillProcedure:
    def test_period_decision_step(self, skill_md):
        assert "期間の決定" in skill_md
        assert "デフォルト" in skill_md and "7日" in skill_md

    def test_data_fetch_step(self, skill_md):
        assert "データ取得" in skill_md
        assert "get_activities" in skill_md
        assert "get_timeline" in skill_md

    def test_grouping_step(self, skill_md):
        assert "グルーピング" in skill_md
        assert "domain:" in skill_md

    def test_output_step(self, skill_md):
        assert "## 手順" in skill_md
        assert "### 4. 出力" in skill_md

    def test_steps_in_order(self, skill_md):
        headings = [
            "### 1. 期間の決定",
            "### 2. データ取得",
            "### 3. グルーピング",
            "### 4. 出力",
        ]
        last = -1
        for heading in headings:
            idx = skill_md.find(heading)
            assert idx >= 0, f"見出し '{heading}' が無い"
            assert idx > last, f"見出し '{heading}' が前のステップより前にある"
            last = idx


class TestDigestSkillOutputContract:
    def test_output_sections_present(self, skill_md):
        for section in (
            "動いたアクティビティ",
            "決まったこと",
            "継続中の論点",
            "保存された成果物",
        ):
            assert section in skill_md, f"出力セクション '{section}' の記載が無い"

    def test_ongoing_discussion_prefix(self, skill_md):
        assert "[議論中]" in skill_md

    def test_omit_empty_sections_rule(self, skill_md):
        assert "省略" in skill_md


class TestDigestSkillConstraints:
    def test_read_only_declaration(self, skill_md):
        assert "読み取り専用" in skill_md

    def test_no_write_tools_mentioned_as_used(self, skill_md):
        assert "add_*" in skill_md or "add_" in skill_md
        assert "呼ばない" in skill_md

    def test_no_internal_id_exposure_rule(self, skill_md):
        assert "内部ID" in skill_md

    def test_related_skills_boundary_section(self, skill_md):
        assert "## 関連skillとの境界" in skill_md
        for s in ("check-in", "postmortem", "scribe"):
            assert s in skill_md
