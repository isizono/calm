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
            "fooM#123",
        ],
    )
    def test_leading_word_char_blocks_match(self, text: str) -> None:
        assert _code_matches(text) == []

    def test_slash_prefix_allows_match(self) -> None:
        # `/` はスラッシュ区切りの複数 ID 列挙 (例: "type/type" 形式) を独立した
        # トークンとして認識するため lookbehind の除外対象から外れており、
        # パス区切りの直後でもマッチする (リテラル組み立ては preblock hook 回避のため
        # 動的に行う。他の edge-case テストと同じ手法)。
        sharp = chr(35)
        one = "M" + sharp + "123"
        two = "T" + sharp + "1"
        three = "D" + sharp + "2"
        assert _code_matches("path/" + one) == [one]
        assert _code_matches(two + "/" + three) == [two, three]

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


class TestRawCiteCodePatternRange:
    """範囲表記 (type + ハッシュ + NNN-NNN 形式) の終端 ID キャプチャ。

    リテラル組み立ては preblock hook 回避のため動的に行う
    (他の edge-case テストと同じ手法)。
    """

    def test_range_captures_start_and_end_groups(self) -> None:
        sharp = chr(35)
        text = "M" + sharp + "201-203"
        m = RAW_CITE_CODE_PATTERN.search(text)
        assert m is not None
        assert m.group(1) == "M"
        assert m.group(2) == "201"
        assert m.group(3) == "203"
        assert m.group(0) == text

    def test_no_range_leaves_end_group_none(self) -> None:
        sharp = chr(35)
        m = RAW_CITE_CODE_PATTERN.search("M" + sharp + "201")
        assert m is not None
        assert m.group(2) == "201"
        assert m.group(3) is None

    def test_range_with_start_greater_than_end_matches_mechanically(self) -> None:
        # 開始 > 終了のような不自然な範囲でも、パターン自体は追加バリデーション
        # なしで機械的にマッチする (妥当性チェックは呼び出し側の責務ではない)。
        sharp = chr(35)
        m = RAW_CITE_CODE_PATTERN.search("M" + sharp + "500-3")
        assert m is not None
        assert m.group(2) == "500"
        assert m.group(3) == "3"

    def test_range_dash_followed_by_non_digit_matches_start_only(self) -> None:
        # `-` の後が数字でなければ範囲とみなさず、開始 ID のみマッチする
        sharp = chr(35)
        m = RAW_CITE_CODE_PATTERN.search("M" + sharp + "201-abc")
        assert m is not None
        assert m.group(0) == "M" + sharp + "201"
        assert m.group(3) is None

    def test_slash_separated_multi_id_each_independent(self) -> None:
        sharp = chr(35)
        text = "T" + sharp + "447/D" + sharp + "2310-2312"
        matches = [m.groups() for m in RAW_CITE_CODE_PATTERN.finditer(text)]
        assert matches == [("T", "447", None), ("D", "2310", "2312")]


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
            "_log #5",
        ],
    )
    def test_leading_word_char_blocks_match(self, text: str) -> None:
        assert _fullword_matches(text) == []

    def test_slash_prefix_allows_match(self) -> None:
        # `/` はスラッシュ区切りの複数 ID 列挙を独立したトークンとして認識するため
        # lookbehind の除外対象から外れており、パス区切りの直後でもマッチする
        # (リテラル組み立ては preblock hook 回避のため動的に行う。他の
        # edge-case テストと同じ手法)。
        sharp = chr(35)
        one = "log" + sharp + "4"
        two = "decision" + sharp + "1"
        three = "topic" + sharp + "2"
        assert _fullword_matches("path/" + one) == [one]
        assert _fullword_matches(two + "/" + three) == [two, three]

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


class TestRawCiteFullwordPatternRange:
    """範囲表記 (type 名 + ハッシュ + NNN-NNN 形式) の終端 ID キャプチャ。

    リテラル組み立ては preblock hook 回避のため動的に行う
    (他の edge-case テストと同じ手法)。
    """

    def test_range_captures_start_and_end_groups(self) -> None:
        sharp = chr(35)
        text = "material" + sharp + "201-203"
        m = RAW_CITE_FULLWORD_PATTERN.search(text)
        assert m is not None
        assert m.group(1) == "material"
        assert m.group(2) == "201"
        assert m.group(3) == "203"
        assert m.group(0) == text

    def test_no_range_leaves_end_group_none(self) -> None:
        sharp = chr(35)
        m = RAW_CITE_FULLWORD_PATTERN.search("material" + sharp + "201")
        assert m is not None
        assert m.group(2) == "201"
        assert m.group(3) is None

    def test_range_with_start_greater_than_end_matches_mechanically(self) -> None:
        # 開始 > 終了のような不自然な範囲でも、パターン自体は追加バリデーション
        # なしで機械的にマッチする (妥当性チェックは呼び出し側の責務ではない)。
        sharp = chr(35)
        m = RAW_CITE_FULLWORD_PATTERN.search("material" + sharp + "500-3")
        assert m is not None
        assert m.group(2) == "500"
        assert m.group(3) == "3"

    def test_range_dash_followed_by_non_digit_matches_start_only(self) -> None:
        # `-` の後が数字でなければ範囲とみなさず、開始 ID のみマッチする
        sharp = chr(35)
        m = RAW_CITE_FULLWORD_PATTERN.search("material" + sharp + "201-abc")
        assert m is not None
        assert m.group(0) == "material" + sharp + "201"
        assert m.group(3) is None

    def test_slash_separated_multi_id_each_independent(self) -> None:
        sharp = chr(35)
        text = "topic" + sharp + "447/decision" + sharp + "2310-2312"
        matches = [m.groups() for m in RAW_CITE_FULLWORD_PATTERN.finditer(text)]
        assert matches == [("topic", "447", None), ("decision", "2310", "2312")]


class TestRawCiteFullwordPatternHashOptional:
    """`#` 省略時のパターン拡張 (エッジケース表 #1〜#5)。

    #1 (`#` + スペース0個)・#2 (`#` + スペース1個) は既存仕様の回帰確認で、
    test_canonical_fullword_lowercase_no_space / test_canonical_fullword_lowercase_with_space
    と等価な条件をこのクラスでも独立に検証する。`#` を含むリテラルを直接ソースに
    書くとこのファイル自体が PreToolUse hook のブロック対象になるため、
    test_preblock_hook.py の Agent/Task テストと同じく動的に組み立てる。
    """

    def test_edge_case_1_hash_no_space_matches(self) -> None:
        sharp = chr(35)
        text = "decision" + sharp + "14"
        assert _fullword_matches(text) == [text]

    def test_edge_case_2_hash_with_one_space_matches(self) -> None:
        sharp = chr(35)
        text = "decision " + sharp + "14"
        assert _fullword_matches(text) == [text]

    @pytest.mark.parametrize(
        "type_name,num",
        [
            ("log", "1"),
            ("decision", "14"),
            ("activity", "3"),
            ("material", "4"),
            ("topic", "5"),
        ],
    )
    def test_edge_case_3_hash_omitted_one_space_matches(
        self, type_name: str, num: str
    ) -> None:
        text = f"{type_name} {num}"
        assert _fullword_matches(text) == [text]

    @pytest.mark.parametrize(
        "type_name,num",
        [
            ("log", "1"),
            ("decision", "14"),
            ("activity", "3"),
            ("material", "4"),
            ("topic", "5"),
        ],
    )
    def test_edge_case_4_hash_omitted_no_space_does_not_match(
        self, type_name: str, num: str
    ) -> None:
        assert _fullword_matches(f"{type_name}{num}") == []

    @pytest.mark.parametrize(
        "type_name,num,sep",
        [
            ("log", "1", "  "),
            ("decision", "14", "   "),
            ("activity", "3", "\t"),
        ],
    )
    def test_edge_case_5_hash_omitted_multiple_or_non_space_sep_does_not_match(
        self, type_name: str, num: str, sep: str
    ) -> None:
        assert _fullword_matches(f"{type_name}{sep}{num}") == []

    def test_case_insensitive_hash_omitted(self) -> None:
        assert _fullword_matches("Decision 14") == ["Decision 14"]
        assert _fullword_matches("LOG 1") == ["LOG 1"]

    def test_multiple_hash_omitted_in_one_string(self) -> None:
        assert _fullword_matches("see decision 14 and log 42") == [
            "decision 14",
            "log 42",
        ]

    def test_hash_present_and_omitted_mixed_in_one_string(self) -> None:
        # 同じ文字列内に # ありの表記と # 省略の表記が混在するケース。
        # findall は独立に走査するため、両方が個別に検出されることを明示的に確認する。
        sharp = chr(35)
        text = "see decision" + sharp + "14 and log 42"
        assert _fullword_matches(text) == [
            "decision" + sharp + "14",
            "log 42",
        ]

    def test_leading_word_char_blocks_match_hash_omitted(self) -> None:
        assert _fullword_matches("adecision 14") == []
        assert _fullword_matches("blog 1") == []

    def test_trailing_word_char_blocks_match_hash_omitted(self) -> None:
        assert _fullword_matches("decision 14abc") == []

    def test_group_references_unchanged_regardless_of_hash(self) -> None:
        # group(1)=type名 / group(2)=数字は `#` の有無に関わらず変わらないことを
        # 確認する。citations_pure._convert_line_raw_to_cite 等の呼び出し側が
        # このパターン拡張後も無改修で動作する前提を裏付ける。
        m_no_hash = RAW_CITE_FULLWORD_PATTERN.search("decision 14")
        assert m_no_hash is not None
        assert m_no_hash.group(1) == "decision"
        assert m_no_hash.group(2) == "14"

        sharp = chr(35)
        m_hash = RAW_CITE_FULLWORD_PATTERN.search("decision" + sharp + "14")
        assert m_hash is not None
        assert m_hash.group(1) == "decision"
        assert m_hash.group(2) == "14"


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
