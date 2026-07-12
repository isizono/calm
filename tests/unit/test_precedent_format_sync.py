"""precedent-format配布互換性の契約テスト。

docs/precedent-format.md（パーサ実装と一致させる正本）と
skills/decision-record/references/precedent-format.md（配布先CWDでも解決できる
skill同梱コピー）の内容が一致していることを検証する。あわせて、
decision-record/sync-memory 両SKILL.mdが配布先で解決しないrepo内部パス
（docs/precedent-format.md）に依存していないかを検証する。
"""
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CANONICAL_DOC = _REPO_ROOT / "docs" / "precedent-format.md"
SKILL_COPY = _REPO_ROOT / "skills" / "decision-record" / "references" / "precedent-format.md"
DECISION_RECORD_SKILL_MD = _REPO_ROOT / "skills" / "decision-record" / "SKILL.md"
SYNC_MEMORY_SKILL_MD = _REPO_ROOT / "skills" / "sync-memory" / "SKILL.md"


class TestPrecedentFormatSkillCopyInSync:
    """正本とskill同梱コピーが食い違うと、配布先のAIだけが古い書式を読む事故になる。
    内容一致を機械的に検証することで、docs/precedent-format.mdの更新時に
    skill側コピーの更新漏れを検知する（実装から期待値を導出する導出型整合性lint）。
    """

    def test_canonical_doc_exists(self):
        assert CANONICAL_DOC.exists(), f"{CANONICAL_DOC} が存在しない"

    def test_skill_copy_exists(self):
        assert SKILL_COPY.exists(), f"{SKILL_COPY} が存在しない"

    def test_skill_copy_matches_canonical(self):
        canonical = CANONICAL_DOC.read_text(encoding="utf-8")
        copy = SKILL_COPY.read_text(encoding="utf-8")
        assert copy == canonical, (
            "skills/decision-record/references/precedent-format.md が "
            "docs/precedent-format.md と食い違っている。両方を同時に更新すること"
        )


class TestDecisionRecordReferencesSkillRelativePath:
    def test_references_skill_local_copy(self):
        skill_md = DECISION_RECORD_SKILL_MD.read_text(encoding="utf-8")
        assert "references/precedent-format.md" in skill_md


class TestSyncMemoryNoRepoInternalPathReference:
    """sync-memory skillは配布先で解決しないrepo内部パスを本文に持たない
    （decision-recordのように詳細を読ませる必要はなく、要点を本文に持つ自己完結構成）。
    """

    def test_no_docs_path_reference(self):
        skill_md = SYNC_MEMORY_SKILL_MD.read_text(encoding="utf-8")
        assert "docs/precedent-format.md" not in skill_md

    def test_no_skill_relative_path_reference(self):
        # sync-memoryは他skillのディレクトリ内ファイルにパス参照しない
        # （skill間はスキル名で言及するに留める。cross-skillファイルパスは配布形態によって解決を保証できない）
        skill_md = SYNC_MEMORY_SKILL_MD.read_text(encoding="utf-8")
        assert "references/precedent-format.md" not in skill_md

    def test_mentions_decision_record_by_name(self):
        skill_md = SYNC_MEMORY_SKILL_MD.read_text(encoding="utf-8")
        assert "decision-record" in skill_md
