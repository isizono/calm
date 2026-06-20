"""citations_service.extract_citations のパーサ単体テスト"""
import pytest

from src.services.citations_service import extract_citations


class TestBasicParse:
    def test_single_material_cite(self):
        assert extract_citations("See {{cite:M#239}}.") == [("material", 239)]

    def test_all_five_type_codes(self):
        text = "{{cite:M#1}} {{cite:D#2}} {{cite:L#3}} {{cite:A#4}} {{cite:T#5}}"
        assert extract_citations(text) == [
            ("material", 1),
            ("decision", 2),
            ("log", 3),
            ("activity", 4),
            ("topic", 5),
        ]

    def test_occurrence_preserved_in_order(self):
        text = "{{cite:M#1}} ... {{cite:D#2}} ... {{cite:M#1}}"
        assert extract_citations(text) == [
            ("material", 1),
            ("decision", 2),
            ("material", 1),
        ]

    def test_no_citations(self):
        assert extract_citations("plain text without cites") == []

    def test_empty_string(self):
        assert extract_citations("") == []


class TestInvalidForms:
    def test_unknown_type_code_skipped(self, caplog):
        # `Z` は invalid type code: 正規表現にマッチしないため警告は malformed として記録される
        result = extract_citations("{{cite:Z#1}}")
        assert result == []

    def test_missing_id_skipped(self, caplog):
        result = extract_citations("{{cite:M#}}")
        assert result == []

    def test_random_garbage_skipped(self, caplog):
        result = extract_citations("{{cite:foo}}")
        assert result == []

    def test_valid_cite_after_invalid_still_parsed(self):
        result = extract_citations("{{cite:foo}} then {{cite:M#5}}")
        assert result == [("material", 5)]


class TestEscapeAndCode:
    def test_backslash_escape_skips(self):
        assert extract_citations(r"\{{cite:M#1}}") == []

    def test_inline_backtick_skips(self):
        assert extract_citations("see `{{cite:M#1}}` raw") == []

    def test_inline_backtick_does_not_swallow_after_close(self):
        assert extract_citations("see `inline` then {{cite:M#1}}") == [("material", 1)]

    def test_fenced_code_block_skips(self):
        text = "intro\n```\n{{cite:M#99}}\n```\nafter {{cite:D#1}}"
        assert extract_citations(text) == [("decision", 1)]

    def test_tilde_fence_skips(self):
        text = "intro\n~~~\n{{cite:M#99}}\n~~~\nafter"
        assert extract_citations(text) == []


class TestEdgeCases:
    def test_same_target_twice_two_occurrences(self):
        text = "{{cite:M#7}} and again {{cite:M#7}}"
        assert extract_citations(text) == [("material", 7), ("material", 7)]

    def test_large_id_allowed(self):
        assert extract_citations("{{cite:M#9999999}}") == [("material", 9999999)]

    def test_no_whitespace_in_template(self):
        # 仕様: 空白を含めない厳密マッチング (M#348 §3.1)
        assert extract_citations("{{cite: M#1}}") == []
        assert extract_citations("{{ cite:M#1}}") == []
