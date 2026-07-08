"""readable_id_format.py (format_readable_id) の単体テスト。

session_start_hook の activity 表示専用の整形 helper。sys.path に hooks/ を
追加してモジュール単体で読み込む (message_display_id_titles.py と同じ流儀)。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HOOKS_DIR = Path(__file__).resolve().parents[2] / "hooks"
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

from readable_id_format import format_readable_id  # type: ignore  # noqa: E402


@pytest.mark.parametrize(
    "entity_type,id_int,title,expected",
    [
        ("topic", 123, "my topic", "my topic (#123)"),
        ("decision", 456, "ある決定", "ある決定 (#456)"),
        ("activity", 789, "[作業] something", "[作業] something (#789)"),
        ("log", 42, "log title", "log title (#42)"),
        ("material", 7, "material title", "material title (#7)"),
    ],
)
def test_format_readable_id_normal(entity_type, id_int, title, expected):
    """5 entity type で title 付きの readable 形式が組み立てられる"""
    assert format_readable_id(entity_type, id_int, title) == expected


def test_format_readable_id_missing_title():
    """title=None の場合は (#NNN) のみ"""
    assert format_readable_id("topic", 100, None) == "(#100)"


def test_format_readable_id_empty_title():
    """title="" の場合は (#NNN) のみ"""
    assert format_readable_id("decision", 200, "") == "(#200)"


def test_format_readable_id_explicit_readable_flavor():
    """flavor='readable' を明示指定しても同じ結果"""
    assert format_readable_id("activity", 5, "title", flavor="readable") == "title (#5)"


def test_format_readable_id_invalid_entity_type():
    """不正な entity_type は ValueError"""
    with pytest.raises(ValueError, match="Invalid entity_type"):
        format_readable_id("unknown", 1, "x")  # type: ignore[arg-type]


def test_format_readable_id_raw_flavor_not_implemented():
    """flavor='raw' は将来実装予定"""
    with pytest.raises(NotImplementedError, match="将来実装予定"):
        format_readable_id("topic", 1, "x", flavor="raw")


def test_format_readable_id_internal_flavor_not_implemented():
    """flavor='internal' は将来実装予定"""
    with pytest.raises(NotImplementedError, match="将来実装予定"):
        format_readable_id("topic", 1, "x", flavor="internal")
