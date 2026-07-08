"""activity-start / activity-finish SKILL.md の契約テスト

両skillに追加された新規手順（重複チェック、related候補特定、
IMPLEMENT_WORKFLOW_GUARD先回り、対象特定の優先順位、次activity提案、
他skillへの委譲）が本文に反映されていることをassertする。
"""
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ACTIVITY_START_SKILL_MD = _REPO_ROOT / "skills" / "activity-start" / "SKILL.md"
ACTIVITY_FINISH_SKILL_MD = _REPO_ROOT / "skills" / "activity-finish" / "SKILL.md"


@pytest.fixture
def activity_start_md() -> str:
    return ACTIVITY_START_SKILL_MD.read_text(encoding="utf-8")


@pytest.fixture
def activity_finish_md() -> str:
    return ACTIVITY_FINISH_SKILL_MD.read_text(encoding="utf-8")


class TestActivityStartDuplicateCheck:
    def test_search_before_add_activity_documented(self, activity_start_md):
        assert "重複チェック" in activity_start_md
        assert "add_activity` を呼ぶ前に" in activity_start_md

    def test_found_duplicate_proposes_check_in_not_new_activity(self, activity_start_md):
        assert "新規作成を提案せず" in activity_start_md
        assert "check-in（再開）" in activity_start_md

    def test_check_in_proposal_delegates_to_check_in_skill(self, activity_start_md):
        """再開提案が承認された場合、check_inツールを直接呼ばずcheck-in skillに委譲する旨が明記されていること
        （情報集約の手順がスキップされないため）。"""
        assert "[check-in](../check-in/SKILL.md) skillに委譲する" in activity_start_md
        assert "`check_in` ツールを直接呼ばず" in activity_start_md


class TestActivityStartRelatedCandidates:
    def test_related_candidate_step_documented(self, activity_start_md):
        assert "related候補の特定" in activity_start_md
        assert "手順3の検索結果を流用し" in activity_start_md


class TestActivityStartImplementGuard:
    def test_implement_workflow_guard_step_documented(self, activity_start_md):
        assert "IMPLEMENT_WORKFLOW_GUARD" in activity_start_md
        assert "intentが `implement` と判定された場合" in activity_start_md

    def test_no_agreed_decision_fallback_documented(self, activity_start_md):
        assert "いきなり実装に入る理由" in activity_start_md


class TestActivityFinishTargetIdentification:
    def test_priority_order_step_documented(self, activity_finish_md):
        assert "対象の特定" in activity_finish_md
        assert "このセッション内でcheck-in・作成したactivityを対象にする" in activity_finish_md

    def test_fallback_get_activities_has_explicit_limit(self, activity_finish_md):
        """get_activities のデフォルトlimitは5件のため、フォールバック一覧では
        limitを明示しないとアクティブなactivityが6件以上ある場合に対象が漏れる。"""
        assert "get_activities(orch_managed=False, limit=15)" in activity_finish_md

    def test_fallback_explains_why_limit_is_explicit(self, activity_finish_md):
        assert "デフォルト5件になり対象が一覧から漏れうる" in activity_finish_md


class TestActivityFinishNextActivityProposal:
    def test_next_activity_proposal_documented(self, activity_finish_md):
        assert "次のactivityを作るか" in activity_finish_md

    def test_proposal_approval_delegates_to_activity_start_skill(self, activity_finish_md):
        """次activity作成の提案が承認された場合、add_activityを直接呼ばず
        activity-start skillに委譲する旨が明記されていること
        （重複チェック・related候補特定・IMPLEMENT_WORKFLOW_GUARD先回りがスキップされないため）。"""
        assert "[activity-start](../activity-start/SKILL.md) skillに委譲する" in activity_finish_md
        assert "`add_activity` を直接呼ばず" in activity_finish_md
