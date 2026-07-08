"""skill descriptionのトリガーフレーズ・非発動条件の契約テスト

check-in / postmortem / scribe / sync-memory / tag-cleanup / tag-notes の各
SKILL.mdに追加した「発動トリガーフレーズ」「非発動条件（他skillへの言及）」が
description行から削除・改変されていないことを検証する。
"""
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SKILLS_DIR = _REPO_ROOT / "skills"

# skill名 -> (トリガーフレーズ一覧, 非発動条件として言及されるべき他skill名一覧)
_EXPECTATIONS: dict[str, tuple[list[str], list[str]]] = {
    "check-in": (
        ["/check-in", "チェックイン", "続きやる", "再開しよう", "前回の続き", "どこまでやったっけ"],
        ["activity-start"],
    ),
    "postmortem": (
        ["/postmortem", "ポストモーテムやろう", "振り返りしたい", "反省会", "この作業を振り返りたい"],
        ["activity-finish", "sync-memory"],
    ),
    "scribe": (
        ["ドキュメント化して", "ADR書いて", "この議論を書き出して", "議事録にまとめて", "設計ドキュメントに起こして"],
        ["recompose-context", "postmortem"],
    ),
    "sync-memory": (
        ["/sync-memory", "同期して", "今日の分を記録して", "セッション終わるから残しておいて"],
        ["recording"],
    ),
    "tag-cleanup": (
        ["/tag-cleanup", "タグ整理して", "タグが散らかってきた", "似たタグをまとめて", "タグの棚卸し"],
        ["tag-notes"],
    ),
    "tag-notes": (
        ["/tag-notes", "タグノート見せて", "このタグのnotes更新して", "〜のtag notesに追記して"],
        ["remember"],
    ),
}


def _skill_md_path(skill_name: str) -> Path:
    return _SKILLS_DIR / skill_name / "SKILL.md"


def _description_line(skill_name: str) -> str:
    path = _skill_md_path(skill_name)
    assert path.exists(), f"{path} が存在しない"
    content = path.read_text(encoding="utf-8")
    match = re.search(r"^description:\s*(.*)$", content, re.MULTILINE)
    assert match is not None, f"{path} にdescription行が無い"
    return match.group(1)


@pytest.mark.parametrize("skill_name", sorted(_EXPECTATIONS))
def test_trigger_phrases_present_in_description(skill_name):
    triggers, _ = _EXPECTATIONS[skill_name]
    description = _description_line(skill_name)
    for phrase in triggers:
        assert phrase in description, (
            f"skills/{skill_name}/SKILL.mdのdescriptionからトリガーフレーズ"
            f"「{phrase}」が失われている"
        )


@pytest.mark.parametrize("skill_name", sorted(_EXPECTATIONS))
def test_non_trigger_references_present_in_description(skill_name):
    _, refs = _EXPECTATIONS[skill_name]
    description = _description_line(skill_name)
    for ref in refs:
        assert ref in description, (
            f"skills/{skill_name}/SKILL.mdのdescriptionから非発動条件の言及"
            f"「{ref}」が失われている"
        )
