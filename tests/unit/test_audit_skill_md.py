"""skills/audit/SKILL.md の契約テスト

M#421 仕様 v0 + D#2843 (Q1-Q10 + 番外 全 A 採用) + D#2844 (intent:audit 新設) の
実装要件が SKILL.md に反映されていることを assert する。
"""
from pathlib import Path

import pytest

SKILL_MD = (
    Path(__file__).resolve().parent.parent.parent
    / "skills"
    / "audit"
    / "SKILL.md"
)


@pytest.fixture
def skill_md() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


class TestAuditSkillFrontmatter:
    def test_skill_md_exists(self):
        assert SKILL_MD.exists(), f"skills/audit/SKILL.md が存在しない: {SKILL_MD}"

    def test_name_field(self, skill_md):
        assert "name: audit" in skill_md

    def test_description_has_required_marker(self, skill_md):
        # 4 要因 (1): 【必須】マーカー
        assert "【必須】" in skill_md

    def test_description_has_forbid_clause(self, skill_md):
        # 4 要因 (2): 禁止ルール
        assert "このスキルを経由せずに" in skill_md
        assert "retract" in skill_md
        assert "supersede" in skill_md

    def test_description_has_trigger_structure(self, skill_md):
        # 4 要因 (3): TRIGGER / DO NOT TRIGGER 構造
        assert "TRIGGER:" in skill_md
        assert "DO NOT TRIGGER:" in skill_md

    def test_description_has_variation_marker(self, skill_md):
        # 4 要因 (4): 「など」+ バリエーション
        assert "など" in skill_md


class TestAuditSkillTriggers:
    def test_t_a_triggers_present(self, skill_md):
        for label in ("T-A1", "T-A2", "T-A3", "T-A4", "T-A5"):
            assert label in skill_md, f"自発トリガー {label} が記載されていない"

    def test_t_b_triggers_present(self, skill_md):
        for label in ("T-B1", "T-B2", "T-B3"):
            assert label in skill_md, f"ユーザー起点トリガー {label} が記載されていない"

    def test_t_c_non_triggers_present(self, skill_md):
        for label in ("T-C1", "T-C2", "T-C3", "T-C4"):
            assert label in skill_md, f"非発動条件 {label} が記載されていない"

    def test_user_utterance_examples(self, skill_md):
        # T-B 系の代表的なユーザー発話が網羅されている
        assert "これ前に決めなかったっけ" in skill_md
        assert "また同じ話してる" in skill_md
        assert "矛盾してない" in skill_md
        assert "グルグル" in skill_md


class TestAuditSkillPlaybook:
    def test_seven_steps_present(self, skill_md):
        for n in range(1, 8):
            assert f"### Step {n}:" in skill_md, f"Step {n} の見出しが無い"

    def test_steps_in_order(self, skill_md):
        # Step 1〜7 が順番に登場する
        last = -1
        for n in range(1, 8):
            idx = skill_md.find(f"### Step {n}:")
            assert idx > last, f"Step {n} が前のステップより前にある"
            last = idx

    def test_completion_marker_step(self, skill_md):
        # Step 7 完了後マーカー記述
        assert "#audited-YYYY-MM-DD" in skill_md

    def test_log_window_threshold(self, skill_md):
        # Q3=A: 直近 30 件 / 90 日
        assert "30 件" in skill_md
        assert "90 日" in skill_md


class TestAuditSkillMaterialFormat:
    def test_material_title_format(self, skill_md):
        # `audit: {主題} ({YYYY-MM-DD})` 形式
        assert "audit: {主題の要約}" in skill_md or "audit: {主題" in skill_md

    def test_required_tags(self, skill_md):
        for tag in ("audit", "reconsider", "intent:audit"):
            assert tag in skill_md, f"必須タグ '{tag}' の記載が無い"

    def test_content_sections(self, skill_md):
        for section in (
            "## 発端",
            "## 対象スコープ",
            "## 一次リソース",
            "## 関連 log",
            "## 全体像",
            "## 文脈不足の分析",
            "## 検証結果",
            "## 知識の pin 先選定",
            "## 完了マーカー",
            "## 残課題",
        ):
            assert section in skill_md, f"material content section '{section}' が無い"


class TestAuditSkillPinMatrix:
    def test_pin_matrix_section_exists(self, skill_md):
        assert "## pin 先判定マトリクス" in skill_md

    def test_four_pin_destinations(self, skill_md):
        # tag note / habit / anchor / material の 4 候補
        for dest in ("tag note", "habit", "anchor", "material"):
            assert dest in skill_md, f"pin 先 '{dest}' の記載が無い"

    def test_judgement_flow_q1_to_q5(self, skill_md):
        for q in ("Q1:", "Q2:", "Q3:", "Q4:", "Q5:"):
            assert q in skill_md, f"判定フロー {q} が無い"

    def test_autonomy_vs_userconfirm_lanes(self, skill_md):
        # 自律レーン / ユーザー確認レーン両方が明示されている
        assert "自律" in skill_md
        assert "ユーザー確認" in skill_md


class TestAuditSkillRelatedSkills:
    def test_related_skills_table(self, skill_md):
        for s in (
            "recompose-context",
            "setup-anchor",
            "cross-topic-bug-report",
            "postmortem",
        ):
            assert s in skill_md, f"関連 skill '{s}' との境界記述が無い"


class TestAuditSkillHintServiceBoundary:
    def test_hintservice_boundary_section(self, skill_md):
        assert "HintService" in skill_md
        # consistency_check の代替担当である旨
        assert "consistency_check" in skill_md


class TestAuditSkillEdgeCases:
    def test_edge_cases_section_exists(self, skill_md):
        assert "## Edge Cases" in skill_md


class TestAuditSkillSessionScope:
    def test_session_scope_section(self, skill_md):
        # recording skill と同パターンのセッション適用宣言
        assert "## 適用範囲" in skill_md
        assert "本セッション内のすべての後続ターン" in skill_md

    def test_24h_dedup_section(self, skill_md):
        # T-C2 (24h 重複) の判定方法を明文化
        assert "24h" in skill_md or "24 h" in skill_md
