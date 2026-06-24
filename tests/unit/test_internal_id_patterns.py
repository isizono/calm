"""internal_id_patterns module の単体テスト。

code パターン / fullword パターン / 境界 / case 揺れ / エスケープ尊重前提を
finditer ベースで網羅する。本 module は pure regex 定数 + mapping のため、
ロジック側 (citations_pure._convert_line_raw_to_cite / preblock_hook._scan_tool_input)
での挙動は別ファイルで検証する。
"""
from __future__ import annotations

import pytest

from src.services.internal_id_patterns import (
    FULLWORD_TO_CODE,
    RAW_CITE_CODE_PATTERN,
    RAW_CITE_FULLWORD_PATTERN,
)


def _code_matches(text: str) -> list[str]:
    return [m.group(0) for m in RAW_CITE_CODE_PATTERN.finditer(text)]


def _fullword_matches(text: str) -> list[str]:
    return [m.group(0) for m in RAW_CITE_FULLWORD_PATTERN.finditer(text)]


class TestRawCiteCodePattern:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("M#123", ["M#123"]),
            ("D#456", ["D#456"]),
            ("L#789", ["L#789"]),
            ("A#321", ["A#321"]),
            ("T#654", ["T#654"]),
            ("see M#1 and D#2 and L#3", ["M#1", "D#2", "L#3"]),
            ("(M#10) [D#20] {L#30}", ["M#10", "D#20", "L#30"]),
        ],
    )
    def test_canonical_code_forms_match(self, text: str, expected: list[str]) -> None:
        assert _code_matches(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "m#123",
            "d#456",
            "l#789",
            "a#321",
            "t#654",
            "Md#1",
            "DL#1",
            "X#999",
            "Z#1",
        ],
    )
    def test_non_canonical_codes_do_not_match(self, text: str) -> None:
        assert _code_matches(text) == []

    @pytest.mark.parametrize(
        "text",
        [
            "aM#123",
            "x_M#123",
            "path/M#123",
            "fooM#123",
        ],
    )
    def test_leading_word_char_blocks_match(self, text: str) -> None:
        assert _code_matches(text) == []

    @pytest.mark.parametrize(
        "text",
        [
            "M#123abc",
            "M#123_suffix",
        ],
    )
    def test_trailing_word_char_blocks_match(self, text: str) -> None:
        assert _code_matches(text) == []

    def test_boundary_with_punctuation_allows_match(self) -> None:
        # 句読点・括弧・空白で囲まれている場合はマッチする
        assert _code_matches("see (M#10) and M#20, plus M#30.") == [
            "M#10",
            "M#20",
            "M#30",
        ]


class TestRawCiteFullwordPattern:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("log #1", ["log #1"]),
            ("decision #2", ["decision #2"]),
            ("activity #3", ["activity #3"]),
            ("material #4", ["material #4"]),
            ("topic #5", ["topic #5"]),
        ],
    )
    def test_canonical_fullword_lowercase_with_space(
        self, text: str, expected: list[str]
    ) -> None:
        assert _fullword_matches(text) == expected

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("log#1", ["log#1"]),
            ("decision#2", ["decision#2"]),
            ("activity#3", ["activity#3"]),
            ("material#4", ["material#4"]),
            ("topic#5", ["topic#5"]),
        ],
    )
    def test_canonical_fullword_lowercase_no_space(
        self, text: str, expected: list[str]
    ) -> None:
        assert _fullword_matches(text) == expected

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Log #1", ["Log #1"]),
            ("LOG #1", ["LOG #1"]),
            ("LoG #1", ["LoG #1"]),
            ("DECISION #2", ["DECISION #2"]),
            ("Activity #3", ["Activity #3"]),
        ],
    )
    def test_case_insensitive(self, text: str, expected: list[str]) -> None:
        assert _fullword_matches(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "log  #1",
            "log\t#1",
            "log: #1",
            "log:#1",
        ],
    )
    def test_excluded_separators(self, text: str) -> None:
        assert _fullword_matches(text) == []

    @pytest.mark.parametrize(
        "text",
        [
            "blog #1",
            "Xlog #2",
            "analog #3",
            "path/log #4",
            "_log #5",
        ],
    )
    def test_leading_word_char_blocks_match(self, text: str) -> None:
        assert _fullword_matches(text) == []

    @pytest.mark.parametrize(
        "text",
        [
            "log #123abc",
            "log #1_suffix",
        ],
    )
    def test_trailing_word_char_blocks_match(self, text: str) -> None:
        assert _fullword_matches(text) == []

    def test_unknown_typename_does_not_match(self) -> None:
        assert _fullword_matches("comment #1") == []
        assert _fullword_matches("issue #99") == []
        assert _fullword_matches("note #5") == []

    def test_short_alias_does_not_match(self) -> None:
        # スコープ確定で略称は対象外
        assert _fullword_matches("act #1") == []
        assert _fullword_matches("mat #2") == []
        assert _fullword_matches("dec #3") == []

    def test_japanese_does_not_match(self) -> None:
        # スコープ確定で日本語表記は対象外
        assert _fullword_matches("ログ #1") == []
        assert _fullword_matches("決定事項 #2") == []
        assert _fullword_matches("アクティビティ #3") == []

    def test_multiple_in_one_string(self) -> None:
        assert _fullword_matches("see log #1 and decision #2") == [
            "log #1",
            "decision #2",
        ]

    def test_mixed_case_and_separator(self) -> None:
        assert _fullword_matches("Log#1 and LOG #2 and log#3") == [
            "Log#1",
            "LOG #2",
            "log#3",
        ]


class TestFullwordToCode:
    def test_mapping_covers_all_five_types(self) -> None:
        assert set(FULLWORD_TO_CODE.keys()) == {
            "log",
            "decision",
            "activity",
            "material",
            "topic",
        }

    def test_mapping_values(self) -> None:
        assert FULLWORD_TO_CODE["log"] == "L"
        assert FULLWORD_TO_CODE["decision"] == "D"
        assert FULLWORD_TO_CODE["activity"] == "A"
        assert FULLWORD_TO_CODE["material"] == "M"
        assert FULLWORD_TO_CODE["topic"] == "T"
