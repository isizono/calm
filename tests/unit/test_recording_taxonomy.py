"""記録先タクソノミー一本化の契約テスト

skills/recording/references/taxonomy.md（旧 docs/recording-taxonomy.md）の配置、
docs/recording-taxonomy.md に残るstub、および skills/recording・skills/sync-memory の
既存インライン基準が維持されたまま補足参照が整合していることを検証する。
"""
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TAXONOMY_DOC = _REPO_ROOT / "skills" / "recording" / "references" / "taxonomy.md"
TAXONOMY_STUB = _REPO_ROOT / "docs" / "recording-taxonomy.md"
RECORDING_SKILL_MD = _REPO_ROOT / "skills" / "recording" / "SKILL.md"
SYNC_MEMORY_SKILL_MD = _REPO_ROOT / "skills" / "sync-memory" / "SKILL.md"


@pytest.fixture
def taxonomy_doc() -> str:
    return TAXONOMY_DOC.read_text(encoding="utf-8")


@pytest.fixture
def taxonomy_stub() -> str:
    return TAXONOMY_STUB.read_text(encoding="utf-8")


@pytest.fixture
def recording_skill_md() -> str:
    return RECORDING_SKILL_MD.read_text(encoding="utf-8")


@pytest.fixture
def sync_memory_skill_md() -> str:
    return SYNC_MEMORY_SKILL_MD.read_text(encoding="utf-8")


class TestTaxonomyDocExists:
    def test_file_exists(self):
        assert TAXONOMY_DOC.exists(), f"{TAXONOMY_DOC} が存在しない"

    def test_four_sections_present(self, taxonomy_doc):
        for heading in (
            "## 1. log",
            "## 2. material",
            "## 3. decision",
            "## 4. report_signal",
        ):
            assert heading in taxonomy_doc, f"見出し '{heading}' が無い"

    def test_no_doc_sync_marker(self, taxonomy_doc):
        """運用ポリシー文書であり、DBスキーマ/tool定義の写しではないため
        ccm-doc-sync の自動陳腐化検知対象に含めない（HTMLコメントマーカー自体を
        敷設しない。本文中でこの方針に言及すること自体は許容する）。"""
        assert "<!-- ccm-doc-sync" not in taxonomy_doc


class TestTaxonomyStubExists:
    """配布先のCWDではskillディレクトリ外のdocs/パスは解決しないため、
    recording skillが参照する本体はskillディレクトリ内に移設した。
    旧パスにはstubを残し、移設先への案内のみ行う。"""

    def test_stub_file_exists(self):
        assert TAXONOMY_STUB.exists(), f"{TAXONOMY_STUB} が存在しない"

    def test_stub_points_to_new_location(self, taxonomy_stub):
        assert "skills/recording/references/taxonomy.md" in taxonomy_stub

    def test_stub_is_short(self, taxonomy_stub):
        # stubは本文を持たず移設案内のみであることを軽く検証する
        assert len(taxonomy_stub.splitlines()) < 15


class TestRecordingSkillPreservesInlineTables:
    def test_l_triggers_preserved(self, recording_skill_md):
        for label in ("L1", "L2", "L3", "L4", "L5"):
            assert label in recording_skill_md, f"{label} 表が削除されている"

    def test_m_triggers_preserved(self, recording_skill_md):
        for label in ("M1", "M2", "M3", "M4"):
            assert label in recording_skill_md, f"{label} 表が削除されている"

    def test_report_signal_kind_count_fixed(self, recording_skill_md):
        """report_signalのkindは全7種存在する旨が明記されている（旧: 3種のみの誤記）"""
        assert "全7種" in recording_skill_md
        assert "precedent_miss" in recording_skill_md
        assert "precedent_misapplied" in recording_skill_md
        assert "boundary_case" in recording_skill_md
        assert "rollback" in recording_skill_md

    def test_references_taxonomy_doc_via_skill_relative_path(self, recording_skill_md):
        """配布先でも解決できるよう、repo内絶対相対パス（docs/...）ではなく
        スキル自身のディレクトリ内パス（references/taxonomy.md）で参照する。"""
        assert "references/taxonomy.md" in recording_skill_md
        assert "docs/recording-taxonomy.md" not in recording_skill_md


class TestSyncMemorySkillPreservesInlineCriteria:
    def test_step3_material_criteria_preserved(self, sync_memory_skill_md):
        assert "### 3. 資材の保存 (add_material)" in sync_memory_skill_md
        assert "materialに入れるもの" in sync_memory_skill_md

    def test_step4_decision_criteria_preserved(self, sync_memory_skill_md):
        assert "### 4. 決定事項・ログの記録" in sync_memory_skill_md
        assert "決定事項の判定基準" in sync_memory_skill_md

    def test_step4_references_report_signal(self, sync_memory_skill_md):
        """Step 4にreport_signalへの言及が含まれる"""
        assert "report_signal" in sync_memory_skill_md

    def test_no_repo_internal_path_reference_to_taxonomy_doc(self, sync_memory_skill_md):
        """sync-memory skillはtaxonomy docへの直接参照を持たない（自己完結化）。
        report_signalのkind一覧は report_signal ツールのdocstringを正とする。"""
        assert "docs/recording-taxonomy.md" not in sync_memory_skill_md
        assert "references/taxonomy.md" not in sync_memory_skill_md
        assert "docstring" in sync_memory_skill_md
