"""skills/sync-memory/SKILL.md の契約テスト。

「聞き返しの後追い検出」ステップの挿入と、それに伴うStep 9/10のリナンバーが
文面レベルで一貫していることを検証する。エッジケース表の#7〜#9（summaryフォーマット・
degraded保守側フォールバック・候補上限N超過の明示）はSKILL.md文面の記述確認で担保する。
"""
from pathlib import Path

import pytest

SKILL_MD = (
    Path(__file__).resolve().parent.parent.parent
    / "skills"
    / "sync-memory"
    / "SKILL.md"
)


@pytest.fixture
def skill_md() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


class TestSyncMemorySkillFrontmatter:
    def test_skill_md_exists(self):
        assert SKILL_MD.exists(), f"skills/sync-memory/SKILL.md が存在しない: {SKILL_MD}"

    def test_name_field(self, skill_md):
        assert "name: sync-memory" in skill_md


class TestStepRenumbering:
    def test_headings_in_order_with_new_step_9(self, skill_md):
        headings = [
            "### 8. 抜け漏れチェック",
            "### 9. 聞き返しの後追い検出",
            "### 10. 棚卸し・remember",
            "#### 10a. アクティビティの棚卸し",
            "#### 10b. 記憶すべき知見の判定（remember）",
            "## 11. 完了報告",
        ]
        last = -1
        for heading in headings:
            idx = skill_md.find(heading)
            assert idx >= 0, f"見出し '{heading}' が無い"
            assert idx > last, f"見出し '{heading}' が前のステップより前にある"
            last = idx

    def test_old_step_9_heading_absent(self, skill_md):
        assert "### 9. 棚卸し・remember" not in skill_md
        assert "#### 9a." not in skill_md
        assert "#### 9b." not in skill_md

    def test_old_step_10_report_heading_absent(self, skill_md):
        assert "## 10. 完了報告" not in skill_md

    def test_no_stray_old_numbering_text(self, skill_md):
        """リナンバー前の文言がそのまま残っていないことを直接確認する。

        \\bは日本語文字をUnicode単語構成文字として扱うため境界判定に使えない
        （数字直後が日本語のケースで誤って非マッチになる）。旧文言の直接一致で確認する。
        """
        stale_phrases = [
            "ステップ9a〜9bの判定に基づき",
            "処理内容はステップ10の完了報告にまとめて記載する",
            "9a〜9bが何も無ければステップ9全体をサイレントスキップする",
            "自動クローズしたものはStep 9で事後報告する",
            "Step 9aの棚卸しで自動的に処理する",
            "ステップ1〜9ではツール呼び出しの前後に判断理由",
            "ユーザーに見せるのはステップ10の完了報告のみ",
            "記録すべきものがなければ9bは空とする",
        ]
        for phrase in stale_phrases:
            assert phrase not in skill_md, f"リナンバー前の文言が残っている: '{phrase}'"

    def test_step10_body_references_step11_for_report(self, skill_md):
        assert "ステップ11の完了報告にまとめて記載する" in skill_md

    def test_step2_high_confidence_references_step10_for_report(self, skill_md):
        assert "自動クローズしたものはStep 10で事後報告する" in skill_md

    def test_final_note_references_step11(self, skill_md):
        assert "ステップ1〜10ではツール呼び出しの前後に判断理由" in skill_md
        assert "ユーザーに見せるのはステップ11の完了報告のみ" in skill_md


class TestReaskDetectionStepContent:
    def test_skip_when_zero_candidates(self, skill_md):
        assert "候補が0件のときはこのステップ全体をサイレントスキップする" in skill_md

    def test_extraction_script_invocation(self, skill_md):
        assert "scripts/detect_reask_candidates.py" in skill_md

    def test_exclusion_categories_present(self, skill_md):
        for phrase in ("意見・選好を求める質問", "選好・状況を尋ねる質問", "セッション外の環境事実"):
            assert phrase in skill_md, f"除外カテゴリの記述 '{phrase}' が無い"

    def test_report_signal_kind_precedent_miss(self, skill_md):
        assert 'report_signal(kind="precedent_miss"' in skill_md


class TestEdgeCase7SummaryFormat:
    """Row #7: summaryは決定論的フォーマットに固定、複数ヒット時は最高scoreを代表にする。"""

    def test_deterministic_summary_template(self, skill_md):
        assert "missed: <最上位ヒットの既存記録type>#<id>" in skill_md

    def test_free_text_goes_to_detail(self, skill_md):
        assert "自由記述の要約はdetailに書く" in skill_md

    def test_representative_hit_is_highest_score(self, skill_md):
        assert "最もscoreが高いものを代表として使う" in skill_md

    def test_fingerprint_stability_reasoning(self, skill_md):
        assert "fingerprint計算にそのまま使われる" in skill_md


class TestEdgeCase8DegradedFallback:
    """Row #8: search degraded=true の候補はprecedent_miss記録しない（保守側スキップ）。"""

    def test_degraded_true_conservative_skip(self, skill_md):
        assert "`degraded=true` のときは判定を保守側に倒す" in skill_md
        assert "precedent_miss記録を行わない" in skill_md

    def test_skip_count_goes_to_completion_report(self, skill_md):
        assert "degraded で判定を保守側に倒した件数" in skill_md


class TestEdgeCase9CandidateLimit:
    """Row #9: 候補上限N超過分は判定対象外、超過した旨を完了報告に一行残す。"""

    def test_candidate_limit_documented(self, skill_md):
        assert "先頭N件" in skill_md
        assert "Nを超える候補は判定対象外とし、超過した旨を完了報告に一行残す" in skill_md

    def test_initial_n_value_noted(self, skill_md):
        assert "判定に回す候補上限N: 5〜10件" in skill_md

    def test_similarity_threshold_noted(self, skill_md):
        assert "search score 0.4以上" in skill_md


class TestEdgeCaseNoHighSimilarityHit:
    """高類似ヒットが1件もない候補は判定・記録の対象外(decision本文の中核要件)。"""

    def test_no_hit_candidates_excluded_from_judgment(self, skill_md):
        assert "高類似ヒットが1件もない候補は判定・記録の対象外とする" in skill_md


class TestCompletionReportSection:
    def test_reask_section_present(self, skill_md):
        assert "### 聞き返し検出" in skill_md

    def test_reask_section_omit_when_empty(self, skill_md):
        idx = skill_md.find("### 聞き返し検出")
        assert idx >= 0
        following = skill_md[idx : idx + 300]
        assert "該当なしの場合はこのセクションを省略" in following

    def test_reask_section_before_retrospective(self, skill_md):
        assert skill_md.find("### 聞き返し検出") < skill_md.find("### ふりかえり")

    def test_dismiss_note_present(self, skill_md):
        idx = skill_md.find("### 聞き返し検出")
        following = skill_md[idx : idx + 300]
        assert "report_signal 側で dismiss できます" in following
