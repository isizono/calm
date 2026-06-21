"""skills/worker/SKILL.md の契約テスト

退場処理 §Step 6 (auto-close) の記述存在と、独自 state "closed" の不在
(state machine 規約遵守) を assert する。
"""
from pathlib import Path

import pytest

SKILL_MD = (
    Path(__file__).resolve().parent.parent.parent
    / "skills"
    / "worker"
    / "SKILL.md"
)


@pytest.fixture
def skill_md() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


class TestWorkerSkillAutoClose:
    def test_step6_section_exists(self, skill_md):
        assert "### Step 6: auto-close" in skill_md

    def test_step6_documents_tmux_kill_pane(self, skill_md):
        assert "tmux kill-pane" in skill_md
        assert "$TMUX_PANE" in skill_md

    def test_step6_documents_iterm_path(self, skill_md):
        assert "ITERM_SESSION_ID" in skill_md
        assert "osascript" in skill_md

    def test_step6_documents_target_causes(self, skill_md):
        # auto-close 対象 cause
        assert "cancelled" in skill_md
        # 非対象 cause も記述されている
        assert "crashed-during-drain" in skill_md
        assert "dead" in skill_md

    def test_step6_after_step5(self, skill_md):
        step5_idx = skill_md.find("### Step 5:")
        step6_idx = skill_md.find("### Step 6:")
        assert step5_idx > 0 and step6_idx > step5_idx, (
            "Step 6 が Step 5 より後に配置されていない"
        )


class TestWorkerSkillStateMachineCompliance:
    def test_no_event_state_closed_as_own_state(self, skill_md):
        """event:state(closed) の独自 state 送信を残していない (D#2838 規約)。

        cause:closed としての "closed" は許容、独自 state "closed" のみ禁止。
        """
        assert '"state":"closed"' not in skill_md
        assert '"state": "closed"' not in skill_md

    def test_terminated_with_cause_closed_documented(self, skill_md):
        """退場処理は terminated + cause:closed パターンで記述されている。"""
        assert "terminated" in skill_md
        # cause:closed パターン (JSON 内記述)
        assert '"cause":"closed"' in skill_md or '"cause": "closed"' in skill_md
