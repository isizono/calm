"""readable_id_format.py (format_readable_id) の単体テスト。

session_start_hook の activity 表示専用の整形 helper。sys.path に hooks/ を
追加してモジュール単体で読み込む (message_display_id_titles.py と同じ流儀)。
"""
from __future__ import annotations

import sys
from pathlib import Path

_HOOKS_DIR = Path(__file__).resolve().parents[2] / "hooks"
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

from readable_id_format import format_readable_id  # type: ignore  # noqa: E402


def test_format_readable_id_normal():
    """title 付きの readable 形式が組み立てられる"""
    assert format_readable_id(789, "[作業] something") == "[作業] something (#789)"


def test_format_readable_id_missing_title():
    """title=None の場合は (#NNN) のみ"""
    assert format_readable_id(100, None) == "(#100)"


def test_format_readable_id_empty_title():
    """title="" の場合は (#NNN) のみ"""
    assert format_readable_id(200, "") == "(#200)"
