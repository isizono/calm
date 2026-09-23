"""precedent-format配布互換性の契約テスト。

docs/precedent-format.md（パーサ実装と一致させる正本）と、判例decisionの書式に
言及する各skill同梱コピー（配布先CWDでも解決できる references/precedent-format.md）
の内容が一致していることを検証する。あわせて、これらのSKILL.mdが配布先で
解決しないrepo内部パス（docs/配下、他skillのreferences/配下）に依存して
いないかを検証する。
"""
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CANONICAL_DOC = _REPO_ROOT / "docs" / "precedent-format.md"
SYNC_MEMORY_SKILL_MD = _REPO_ROOT / "skills" / "sync-memory" / "SKILL.md"

# skills/<name>/references/precedent-format.md を同梱しているskill一覧。
# 一覧をハードコードせず実ファイル配置から導出することで、新規追加時の
# 更新漏れそのものを構造的に無くす。
PRECEDENT_FORMAT_SKILLS = sorted(
    p.parent.parent.name for p in (_REPO_ROOT / "skills").glob("*/references/precedent-format.md")
)

# decision-recordのみ、正本の所在（docs/precedent-format.md）を説明する専用パラグラフを
# 本文に持つため、docsパス非参照チェックの対象から除く。
PRECEDENT_FORMAT_SKILLS_WITHOUT_CANONICAL_EXPLANATION = [
    name for name in PRECEDENT_FORMAT_SKILLS if name != "decision-record"
]

_RELATIVE_MD_PATH_RE = re.compile(r"references/[\w.\-/]+\.md")
_DOCS_PATH_RE = re.compile(r"docs/[\w.\-/]+\.md")


class TestPrecedentFormatSkillCopyInSync:
    """正本とskill同梱コピーが食い違うと、配布先のAIだけが古い書式を読む事故になる。
    内容一致を機械的に検証することで、docs/precedent-format.mdの更新時に
    skill側コピーの更新漏れを検知する（実装から期待値を導出する導出型整合性lint）。
    """

    def test_canonical_doc_exists(self):
        assert CANONICAL_DOC.exists(), f"{CANONICAL_DOC} が存在しない"

    def test_at_least_one_skill_copy_discovered(self):
        # globで導出したPRECEDENT_FORMAT_SKILLSが空だと、以降のparametrizeテストが
        # 0件成功のまま静かに通ってしまう（同梱コピーが全部消えても検知できない）。
        assert PRECEDENT_FORMAT_SKILLS, "skills/*/references/precedent-format.md が1件も見つからない"

    @pytest.mark.parametrize("skill_name", PRECEDENT_FORMAT_SKILLS)
    def test_skill_copy_exists(self, skill_name):
        skill_copy = _REPO_ROOT / "skills" / skill_name / "references" / "precedent-format.md"
        assert skill_copy.exists(), f"{skill_copy} が存在しない"

    @pytest.mark.parametrize("skill_name", PRECEDENT_FORMAT_SKILLS)
    def test_skill_copy_matches_canonical(self, skill_name):
        skill_copy = _REPO_ROOT / "skills" / skill_name / "references" / "precedent-format.md"
        canonical = CANONICAL_DOC.read_text(encoding="utf-8")
        copy = skill_copy.read_text(encoding="utf-8")
        assert copy == canonical, (
            f"skills/{skill_name}/references/precedent-format.md が "
            "docs/precedent-format.md と食い違っている。両方を同時に更新すること"
        )


class TestPrecedentFormatSkillReferencesResolveToExistingFiles:
    """判例decisionの書式に言及するSKILL.mdが本文中で言及する references/ 配下の
    パスは、配布先で実際にskillディレクトリ相対で解決できなければならない。
    言及パスをSKILL.md本文から正規表現で抽出し、ファイルシステム上の実在で
    検証する（特定ファイル名の文言一致ではなく、パス表記→実ファイルの
    導出型整合性lint）。
    """

    @pytest.mark.parametrize("skill_name", PRECEDENT_FORMAT_SKILLS)
    def test_referenced_relative_paths_exist(self, skill_name):
        skill_md_path = _REPO_ROOT / "skills" / skill_name / "SKILL.md"
        skill_md = skill_md_path.read_text(encoding="utf-8")
        skill_dir = skill_md_path.parent
        referenced_paths = sorted(set(_RELATIVE_MD_PATH_RE.findall(skill_md)))
        assert referenced_paths, (
            f"{skill_name} SKILL.mdにreferences/配下へのパス参照が見つからない"
        )
        for rel_path in referenced_paths:
            assert (skill_dir / rel_path).exists(), (
                f"{rel_path} が{skill_name} SKILL.mdから参照されているが存在しない"
            )

    @pytest.mark.parametrize("skill_name", PRECEDENT_FORMAT_SKILLS_WITHOUT_CANONICAL_EXPLANATION)
    def test_no_docs_path_reference(self, skill_name):
        skill_md_path = _REPO_ROOT / "skills" / skill_name / "SKILL.md"
        skill_md = skill_md_path.read_text(encoding="utf-8")
        match = _DOCS_PATH_RE.search(skill_md)
        assert match is None, (
            f"{skill_name} SKILL.mdがdocs/配下のrepo内部パス "
            f"'{match.group(0) if match else ''}' を参照している（配布先CWDでは解決しない）"
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
