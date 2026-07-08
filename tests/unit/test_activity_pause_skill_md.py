"""skills/activity-pause/SKILL.md の契約テスト

activity-pauseがactivity-start/activity-finishと対をなす姉妹skillとして、
必須のfrontmatter要素と主要な手順見出しを備えていることをassertする。
"""
from pathlib import Path

import pytest

SKILL_MD = (
    Path(__file__).resolve().parent.parent.parent
    / "skills"
    / "activity-pause"
    / "SKILL.md"
)


@pytest.fixture
def skill_md() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


class TestActivityPauseSkillFrontmatter:
    def test_skill_md_exists(self):
        assert SKILL_MD.exists(), f"skills/activity-pause/SKILL.md が存在しない: {SKILL_MD}"

    def test_name_field(self, skill_md):
        assert "name: activity-pause" in skill_md

    def test_description_has_required_marker(self, skill_md):
        assert "【必須】" in skill_md

    def test_description_has_forbid_clause(self, skill_md):
        assert "このスキルを経由せずに" in skill_md
        assert "update_activity" in skill_md

    def test_description_has_trigger_phrases(self, skill_md):
        for phrase in (
            "一旦ここまで",
            "この作業いったん置いておく",
            "中断して別の作業に移る",
            "今日はここで止める",
        ):
            assert phrase in skill_md, f"トリガーフレーズ '{phrase}' の記載が無い"

    def test_description_has_non_trigger_cases(self, skill_md):
        # 完了(activity-finish)・セッション全体記録(sync-memory)には発動しない旨
        assert "activity-finish" in skill_md
        assert "sync-memory" in skill_md


class TestActivityPauseSkillBody:
    def test_title_heading(self, skill_md):
        assert "# activity-pause" in skill_md

    def test_procedure_heading(self, skill_md):
        assert "## 手順" in skill_md

    def test_caution_heading(self, skill_md):
        assert "## 注意" in skill_md


class TestActivityPauseSkillSteps:
    def test_target_identification_step(self, skill_md):
        assert "対象特定" in skill_md
        assert "get_activities" in skill_md

    def test_resume_memo_step(self, skill_md):
        assert "再開メモ" in skill_md
        assert "## 中断メモ (YYYY-MM-DD)" in skill_md
        assert "どこまで" in skill_md
        assert "次にやること" in skill_md
        assert "ブロッカー" in skill_md

    def test_status_change_step(self, skill_md):
        for status in ('"pending"', '"snoozed"', '"shelved"'):
            assert status in skill_md, f"status値 {status} の記載が無い"

    def test_report_step(self, skill_md):
        assert "check-in" in skill_md


class TestActivityPauseSkillCautions:
    def test_lightweight_principle(self, skill_md):
        assert "軽量原則" in skill_md

    def test_boundary_with_activity_finish(self, skill_md):
        assert "activity-finish" in skill_md and "境界" in skill_md
