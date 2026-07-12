"""precedent-format配布互換性の契約テスト。

docs/precedent-format.md（パーサ実装と一致させる正本）と
skills/decision-record/references/precedent-format.md（配布先CWDでも解決できる
skill同梱コピー）の内容が一致していることを検証する。あわせて、
decision-record/sync-memory 両SKILL.mdが配布先で解決しないrepo内部パス
（docs/配下、他skillのreferences/配下）に依存していないかを検証する。
"""
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CANONICAL_DOC = _REPO_ROOT / "docs" / "precedent-format.md"
SKILL_COPY = _REPO_ROOT / "skills" / "decision-record" / "references" / "precedent-format.md"
DECISION_RECORD_SKILL_MD = _REPO_ROOT / "skills" / "decision-record" / "SKILL.md"
SYNC_MEMORY_SKILL_MD = _REPO_ROOT / "skills" / "sync-memory" / "SKILL.md"

_RELATIVE_MD_PATH_RE = re.compile(r"references/[\w.\-/]+\.md")
_DOCS_PATH_RE = re.compile(r"docs/[\w.\-/]+\.md")


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


class TestDecisionRecordSkillReferencesResolveToExistingFiles:
    """decision-record SKILL.mdが本文中で言及する references/ 配下のパスは、
    配布先で実際にskillディレクトリ相対で解決できなければならない。
    言及パスをSKILL.md本文から正規表現で抽出し、ファイルシステム上の実在で
    検証する（特定ファイル名の文言一致ではなく、パス表記→実ファイルの
    導出型整合性lint）。
    """

    def test_referenced_relative_paths_exist(self):
        skill_md = DECISION_RECORD_SKILL_MD.read_text(encoding="utf-8")
        skill_dir = DECISION_RECORD_SKILL_MD.parent
        referenced_paths = sorted(set(_RELATIVE_MD_PATH_RE.findall(skill_md)))
        assert referenced_paths, (
            "decision-record SKILL.mdにreferences/配下へのパス参照が見つからない"
        )
        for rel_path in referenced_paths:
            assert (skill_dir / rel_path).exists(), (
                f"{rel_path} がSKILL.mdから参照されているが存在しない"
            )


class TestSyncMemoryNoRepoInternalPathReference:
    """sync-memory skillは配布先で解決しないrepo内部パスを本文に持たない
    （decision-recordのように詳細を読ませる必要はなく、要点を本文に持つ自己完結構成）。
    特定ファイル名ではなく、docs/配下・他skillのreferences/配下という
    パスパターン自体への参照有無を検証する。
    """

    def test_no_docs_path_reference(self):
        skill_md = SYNC_MEMORY_SKILL_MD.read_text(encoding="utf-8")
        match = _DOCS_PATH_RE.search(skill_md)
        assert match is None, (
            f"sync-memory SKILL.mdがdocs/配下のrepo内部パス '{match.group(0) if match else ''}' "
            "を参照している（配布先CWDでは解決しない）"
        )

    def test_no_skill_relative_path_reference(self):
        # sync-memoryは他skillのディレクトリ内ファイルにパス参照しない
        # （skill間はスキル名で言及するに留める。cross-skillファイルパスは配布形態によって解決を保証できない）
        skill_md = SYNC_MEMORY_SKILL_MD.read_text(encoding="utf-8")
        match = _RELATIVE_MD_PATH_RE.search(skill_md)
        assert match is None, (
            f"sync-memory SKILL.mdが他skillのreferences/配下パス '{match.group(0) if match else ''}' "
            "を参照している"
        )
