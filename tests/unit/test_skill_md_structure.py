"""全SKILL.md横断の構造smokeテスト。

プラグインローダーが機械的に要求する構造（ファイル存在・frontmatter必須
フィールド）のみを検証する。文言・見出し順序・トリガーフレーズ等の内容
チェックは対象外（docs/spec/test-convention.md §2 参照）。
"""
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_SKILL_MD_PATHS = sorted(
    {
        *(_REPO_ROOT / "skills").glob("*/SKILL.md"),
        *(_REPO_ROOT / ".claude" / "skills").glob("*/SKILL.md"),
    }
)

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def _skill_name_from_path(path: Path) -> str:
    return path.parent.name


@pytest.fixture(params=_SKILL_MD_PATHS, ids=_skill_name_from_path)
def skill_md_path(request) -> Path:
    return request.param


def test_at_least_one_skill_md_found():
    assert len(_SKILL_MD_PATHS) > 0, "SKILL.mdが1件も見つからない（探索パス設定を確認）"


def test_skill_md_exists(skill_md_path):
    assert skill_md_path.exists()


def test_skill_md_has_frontmatter(skill_md_path):
    content = skill_md_path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(content)
    assert match is not None, f"{skill_md_path} にfrontmatter（---で囲まれたブロック）が無い"


def test_skill_md_frontmatter_has_name_field(skill_md_path):
    content = skill_md_path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(content)
    assert match is not None
    frontmatter = match.group(1)
    assert re.search(r"^name:\s*\S+", frontmatter, re.MULTILINE), (
        f"{skill_md_path} のfrontmatterにname:フィールドが無い"
    )


def test_skill_md_frontmatter_has_description_field(skill_md_path):
    content = skill_md_path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(content)
    assert match is not None
    frontmatter = match.group(1)
    assert re.search(r"^description:\s*\S+", frontmatter, re.MULTILINE), (
        f"{skill_md_path} のfrontmatterにdescription:フィールドが無い"
    )


def test_skill_md_name_field_matches_directory(skill_md_path):
    content = skill_md_path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(content)
    assert match is not None
    frontmatter = match.group(1)
    name_match = re.search(r"^name:\s*(\S+)", frontmatter, re.MULTILINE)
    assert name_match is not None
    assert name_match.group(1) == _skill_name_from_path(skill_md_path), (
        f"{skill_md_path} のname:フィールドがディレクトリ名と一致しない"
    )
