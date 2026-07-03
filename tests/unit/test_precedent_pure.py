"""precedent_pure の pure 関数群の単体テスト。

parse_precedent_sections / summarize_precedent を対象とする。
定型節フォーマット（却下案:/適用条件:/適用外:/検証:）の正本は
docs/precedent-format.md。
"""
from src.services.precedent_pure import (
    NEAR_MISS_HEADERS,
    SECTION_HEADERS,
    parse_precedent_sections,
    summarize_precedent,
)


class TestSectionDetection:
    """4見出しの検出と節本文の項目分解"""

    def test_all_four_headers_detected(self):
        reason = (
            "自由記述の理由本文。\n"
            "\n"
            "却下案:\n"
            "- 案A: 理由A\n"
            "- 案B: 理由B\n"
            "\n"
            "適用条件:\n"
            "- 条件1\n"
            "\n"
            "適用外:\n"
            "- 除外1\n"
            "\n"
            "検証: 実機確認 / abcdef1 / 2026-07-04\n"
        )
        parsed = parse_precedent_sections(reason)
        assert parsed is not None
        assert parsed["rejected_alternatives"] == [
            {"alternative": "案A", "reason": "理由A"},
            {"alternative": "案B", "reason": "理由B"},
        ]
        assert parsed["scope_in"] == ["条件1"]
        assert parsed["scope_out"] == ["除外1"]
        assert len(parsed["verification_anchors"]) == 1
        assert parsed["verification_anchors"][0]["raw"] == "実機確認 / abcdef1 / 2026-07-04"
        assert parsed["warnings"] == []

    def test_only_rejected_alternatives_section(self):
        reason = "本文\n\n却下案:\n- 案C: 理由C\n"
        parsed = parse_precedent_sections(reason)
        assert parsed is not None
        assert parsed["rejected_alternatives"] == [{"alternative": "案C", "reason": "理由C"}]
        assert parsed["scope_in"] == []
        assert parsed["scope_out"] == []
        assert parsed["verification_anchors"] == []

    def test_free_text_before_heading_is_ignored(self):
        reason = "これは自由記述の本文で複数行にわたる。\n続きの行。\n\n適用条件:\n- 前提X\n"
        parsed = parse_precedent_sections(reason)
        assert parsed is not None
        assert parsed["scope_in"] == ["前提X"]


class TestRejectedAlternativeSplit:
    """却下案項目の `案: 理由` 2分割、区切り無し項目の alternative-only 扱い"""

    def test_colon_space_split(self):
        reason = "却下案:\n- 案X: 理由X\n"
        parsed = parse_precedent_sections(reason)
        assert parsed["rejected_alternatives"] == [{"alternative": "案X", "reason": "理由X"}]
        assert parsed["warnings"] == []

    def test_fullwidth_colon_split(self):
        reason = "却下案:\n- 案Y：理由Y\n"
        parsed = parse_precedent_sections(reason)
        assert parsed["rejected_alternatives"] == [{"alternative": "案Y", "reason": "理由Y"}]

    def test_no_separator_is_alternative_only(self):
        reason = "却下案:\n- 単に案の要約だけ\n"
        parsed = parse_precedent_sections(reason)
        assert parsed["rejected_alternatives"] == [
            {"alternative": "単に案の要約だけ", "reason": ""}
        ]
        assert any("separator" in w for w in parsed["warnings"])


class TestNoHeadingReturnsNone:
    """見出しが1つも無い reason は None を返す（legacy 互換の要）"""

    def test_plain_reason_returns_none(self):
        assert parse_precedent_sections("ただの理由本文で節は無い。") is None

    def test_empty_string_returns_none(self):
        assert parse_precedent_sections("") is None

    def test_none_input_returns_none(self):
        assert parse_precedent_sections(None) is None

    def test_heading_word_only_mentioned_inline_returns_none(self):
        reason = "この却下案については別途相談する。適用条件も未定。"
        assert parse_precedent_sections(reason) is None


class TestMidLineHeadingNotDetected:
    """行頭以外に現れた見出し語（本文中の言及）を節として誤検出しない"""

    def test_heading_word_mid_sentence_not_a_section(self):
        reason = (
            "対応する却下案: 別のセクションで詳述、とだけ触れておく。\n"
            "\n"
            "検証: 実機確認 / 2026-07-04\n"
        )
        parsed = parse_precedent_sections(reason)
        assert parsed is not None
        # 行頭ではないため却下案は開かず、検証のみ検出される
        assert parsed["rejected_alternatives"] == []
        assert len(parsed["verification_anchors"]) == 1

    def test_indented_heading_not_detected_as_section(self):
        reason = "本文\n  却下案:\n- これは項目にならない\n"
        parsed = parse_precedent_sections(reason)
        # 正規見出し・近似見出しどちらも行頭マッチしないため None
        assert parsed is None


class TestNearMissHeadings:
    """近似見出し（却下例 等）は節として採らず warning に積まれる"""

    def test_near_miss_alone_produces_warning_but_no_section(self):
        reason = "本文\n却下例:\n- これは節にならない\n"
        parsed = parse_precedent_sections(reason)
        assert parsed is not None
        assert parsed["rejected_alternatives"] == []
        assert any("却下例" in w for w in parsed["warnings"])

    def test_all_near_miss_headers_are_defined(self):
        expected = {
            "却下例", "棄却案", "不採用案", "適用範囲", "対象外", "検証済み",
            "rejected", "scope",
        }
        assert set(NEAR_MISS_HEADERS) == expected

    def test_near_miss_does_not_close_real_open_section(self):
        reason = "却下案:\n- 案A: 理由A\n対象外:\nそのまま\n"
        parsed = parse_precedent_sections(reason)
        assert parsed is not None
        # near-miss 行の後も却下案項目としては1件のみ
        assert parsed["rejected_alternatives"] == [{"alternative": "案A", "reason": "理由A"}]
        assert any("対象外" in w for w in parsed["warnings"])


class TestEmptySectionAndMissingDateWarnings:
    """空節・検証行の日付欠落が warning になる"""

    def test_empty_section_warns(self):
        reason = "却下案:\n\n適用条件:\n- 前提\n"
        parsed = parse_precedent_sections(reason)
        assert parsed["rejected_alternatives"] == []
        assert any("empty section" in w and "却下案" in w for w in parsed["warnings"])

    def test_verification_without_date_warns(self):
        reason = "検証: 実機確認のみ、日付なし\n"
        parsed = parse_precedent_sections(reason)
        assert parsed["verification_anchors"][0]["date"] is None
        assert any("date" in w for w in parsed["warnings"])

    def test_verification_with_date_has_no_warning(self):
        reason = "検証: 実機確認 / 2026-07-04\n"
        parsed = parse_precedent_sections(reason)
        assert parsed["verification_anchors"][0]["date"] == "2026-07-04"
        assert parsed["warnings"] == []


class TestVerificationAnchorExtraction:
    """検証行から日付（YYYY-MM-DD）と hex SHA が位置非依存で抽出される"""

    def test_date_and_sha_extracted_regardless_of_order(self):
        reason = "検証: 2026-07-04 の時点で abcdef1234 を確認\n"
        parsed = parse_precedent_sections(reason)
        anchor = parsed["verification_anchors"][0]
        assert anchor["date"] == "2026-07-04"
        assert anchor["commit"] == "abcdef1234"

    def test_sha_only_no_date(self):
        reason = "検証: commit 0123456789abcdef で確認\n"
        parsed = parse_precedent_sections(reason)
        anchor = parsed["verification_anchors"][0]
        assert anchor["date"] is None
        assert anchor["commit"] == "0123456789abcdef"

    def test_short_hex_below_seven_chars_not_extracted_as_commit(self):
        reason = "検証: bugfix ab12cd で確認 / 2026-07-04\n"
        parsed = parse_precedent_sections(reason)
        anchor = parsed["verification_anchors"][0]
        assert anchor["commit"] is None
        assert anchor["date"] == "2026-07-04"


class TestCrlfAndWhitespaceAndMultipleVerification:
    """CRLF・見出し前後の空白・複数検証行の許容"""

    def test_crlf_line_endings(self):
        reason = "却下案:\r\n- 案A: 理由A\r\n\r\n検証: 実機確認 / 2026-07-04\r\n"
        parsed = parse_precedent_sections(reason)
        assert parsed["rejected_alternatives"] == [{"alternative": "案A", "reason": "理由A"}]
        assert parsed["verification_anchors"][0]["date"] == "2026-07-04"

    def test_whitespace_around_heading_colon(self):
        reason = "適用条件 :  \n- 条件A\n"
        parsed = parse_precedent_sections(reason)
        assert parsed["scope_in"] == ["条件A"]

    def test_multiple_verification_lines(self):
        reason = "検証: 実機確認 / 2026-07-01\n検証: 再検証 / 2026-07-04\n"
        parsed = parse_precedent_sections(reason)
        assert len(parsed["verification_anchors"]) == 2
        assert parsed["verification_anchors"][0]["date"] == "2026-07-01"
        assert parsed["verification_anchors"][1]["date"] == "2026-07-04"


class TestSummarizePrecedent:
    """summarize_precedent のコンパクト形が仕様のキー・型で返る"""

    def test_compact_form_keys_and_types(self):
        reason = (
            "却下案:\n- 案A: 理由A\n- 案B: 理由B\n\n"
            "適用外:\n- 除外1\n\n"
            "検証: 実機確認 / 2026-07-04\n"
        )
        parsed = parse_precedent_sections(reason)
        compact = summarize_precedent(parsed)
        assert set(compact.keys()) == {
            "rejected_alternatives", "scope", "verification_anchors",
        }
        assert compact["rejected_alternatives"] == 2
        assert compact["scope"] is True
        assert compact["verification_anchors"] == ["実機確認 / 2026-07-04"]

    def test_compact_form_no_scope_no_anchor(self):
        reason = "却下案:\n- 案A: 理由A\n"
        parsed = parse_precedent_sections(reason)
        compact = summarize_precedent(parsed)
        assert compact["scope"] is False
        assert compact["verification_anchors"] == []
        assert compact["rejected_alternatives"] == 1


def test_section_headers_constant():
    assert SECTION_HEADERS == ("却下案", "適用条件", "適用外", "検証")
