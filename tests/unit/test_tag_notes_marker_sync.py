"""skills/tag-notes/SKILL.md と src/services/hint_service.py の抑制マーカー整合テスト

tag-notes/SKILL.mdは配布先での参照解決を避けるため、hint_service.pyの抑制マーカー名と
書式仕様をインライン記述して自己完結させている。hint_service.py側でマーカー定数が
追加・変更・削除された際にSKILL.md側が追随しないドリフトを検知する。
"""
import re
from pathlib import Path

import pytest

import src.services.hint_service as hint_service

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TAG_NOTES_SKILL_MD = _REPO_ROOT / "skills" / "tag-notes" / "SKILL.md"


def _hint_service_markers() -> set[str]:
    """hint_service.pyで定義されている抑制マーカー定数(MARKER_*)の実値集合。"""
    return {
        value
        for name, value in vars(hint_service).items()
        if name.startswith("MARKER_") and isinstance(value, str)
    }


def _skill_md_markers(content: str) -> set[str]:
    """SKILL.md本文からバッククォート囲みのマーカー文字列を抽出する。

    `<マーカー>-until:YYYY-MM-DD` のような日付付き表記はマーカー名と`:`区切りの
    日付を含み [a-z-]+ の範囲に収まらないため、このパターンには一致しない。
    """
    return set(re.findall(r"`(#[a-z-]+)`", content))


@pytest.fixture
def skill_md_content() -> str:
    assert TAG_NOTES_SKILL_MD.exists(), f"{TAG_NOTES_SKILL_MD} が存在しない"
    return TAG_NOTES_SKILL_MD.read_text(encoding="utf-8")


class TestTagNotesMarkerSyncWithHintService:
    def test_hint_service_has_expected_marker_count(self):
        # 0件だとテスト自体が意味を持たなくなるため、実装側の前提を明示的に固定する
        assert len(_hint_service_markers()) == 6

    def test_all_hint_service_markers_documented_in_skill_md(self, skill_md_content):
        missing = _hint_service_markers() - _skill_md_markers(skill_md_content)
        assert not missing, (
            f"hint_service.pyの抑制マーカーがtag-notes/SKILL.mdに未記載: {sorted(missing)}"
        )

    def test_no_undocumented_extra_markers_in_skill_md(self, skill_md_content):
        extra = _skill_md_markers(skill_md_content) - _hint_service_markers()
        assert not extra, (
            f"tag-notes/SKILL.mdにhint_service.pyに存在しないマーカーが記載されている: {sorted(extra)}"
        )

    def test_dated_suppression_format_documented(self, skill_md_content):
        assert "-until:YYYY-MM-DD" in skill_md_content

    def test_fail_open_behavior_documented(self, skill_md_content):
        assert "フェイルオープン" in skill_md_content
