"""citations_pure の pure 関数群の単体テスト。

extract_citations / convert_raw_to_cite / check_target_exists を対象とする。
extract_citations の挙動は test_citations_parser.py で既に網羅されているため、
ここでは convert_raw_to_cite を中心に検証する。
"""
import os
import sqlite3
import tempfile

import pytest

from src.services.citations_pure import (
    TYPE_CODE_TO_NAME,
    TYPE_TO_TABLE,
    check_target_exists,
    convert_raw_to_cite,
)


def _allow_all(target_type: str, target_id: int) -> bool:
    return True


def _deny_all(target_type: str, target_id: int) -> bool:
    return False


class TestBasicConversion:
    def test_single_material_raw_to_cite(self):
        out, counters = convert_raw_to_cite("See M#123 here.")
        assert out == "See {{cite:M#123}} here."
        assert counters["sanitized_count"] == 1

    def test_all_five_type_codes(self):
        text = "M#1 D#2 L#3 A#4 T#5"
        out, counters = convert_raw_to_cite(text)
        assert out == "{{cite:M#1}} {{cite:D#2}} {{cite:L#3}} {{cite:A#4}} {{cite:T#5}}"
        assert counters["sanitized_count"] == 5

    def test_no_raw_literals(self):
        out, counters = convert_raw_to_cite("plain text without ids")
        assert out == "plain text without ids"
        assert counters["sanitized_count"] == 0

    def test_empty_string(self):
        out, counters = convert_raw_to_cite("")
        assert out == ""
        assert counters["sanitized_count"] == 0

    def test_large_id_allowed(self):
        out, _ = convert_raw_to_cite("ref M#9999999 ok")
        assert out == "ref {{cite:M#9999999}} ok"


class TestCodeBlockSkip:
    """エッジケース表 #2, #3: コードブロック内は変換しない"""

    def test_inline_backtick_skips(self):
        out, counters = convert_raw_to_cite("see `D#456 raw` next")
        assert out == "see `D#456 raw` next"
        assert counters["sanitized_count"] == 0
        assert counters["skipped_in_codeblock"] == 1

    def test_inline_backtick_does_not_swallow_after_close(self):
        out, counters = convert_raw_to_cite("`inline` then M#1")
        assert out == "`inline` then {{cite:M#1}}"
        assert counters["sanitized_count"] == 1

    def test_fenced_code_block_skips(self):
        text = "intro\n```\nL#789 inside\n```\nafter D#1"
        out, counters = convert_raw_to_cite(text)
        assert out == "intro\n```\nL#789 inside\n```\nafter {{cite:D#1}}"
        assert counters["sanitized_count"] == 1
        assert counters["skipped_in_codeblock"] == 1

    def test_tilde_fence_skips(self):
        text = "intro\n~~~\nM#99 inside\n~~~\nafter"
        out, counters = convert_raw_to_cite(text)
        assert "M#99" in out
        assert "{{cite:M#99}}" not in out
        assert counters["skipped_in_codeblock"] == 1


class TestEscape:
    """エッジケース表 #4: エスケープ `\\X#NNN` は変換しない"""

    def test_backslash_escape_raw_skips(self):
        out, counters = convert_raw_to_cite(r"see \A#42 escaped")
        assert out == r"see \A#42 escaped"
        assert counters["sanitized_count"] == 0
        assert counters["skipped_escape"] == 1

    def test_backslash_cite_template_passes_through(self):
        # `\{{cite:M#1}}` 形式: 既存パーサの規律と同じく丸ごとスキップ。
        # 内部に含まれる `M#1` は escape による不変扱いとして skipped_escape に集計する。
        text = r"shown \{{cite:M#1}} only"
        out, counters = convert_raw_to_cite(text)
        assert out == text
        assert counters["sanitized_count"] == 0
        assert counters["skipped_escape"] == 1
        assert counters["skipped_in_existing_cite"] == 0

    def test_backslash_cite_template_then_raw_literal(self):
        # `\{{cite:M#1}}` の後ろに生リテラルがある: escape 内はスキップ、後者は変換される
        text = r"escaped \{{cite:M#1}} then real M#2"
        out, counters = convert_raw_to_cite(text)
        assert out == r"escaped \{{cite:M#1}} then real {{cite:M#2}}"
        assert counters["sanitized_count"] == 1
        assert counters["skipped_escape"] == 1


class TestExistingCiteSkip:
    """エッジケース表 #5: 既に `{{cite:X#NNN}}` 形式は二重変換しない"""

    def test_existing_cite_not_doubled(self):
        text = "already {{cite:T#7}} formed"
        out, counters = convert_raw_to_cite(text)
        assert out == text
        assert counters["sanitized_count"] == 0
        assert counters["skipped_in_existing_cite"] == 1


class TestIdempotent:
    """エッジケース表 #6: 2 回適用しても結果不変"""

    @pytest.mark.parametrize(
        "text",
        [
            "single M#1",
            "mixed M#1 and `D#2` and \\A#3 and {{cite:T#4}}",
            "intro\n```\nM#9\n```\nafter D#5",
            "multi-line\nwith M#1\nand D#2 ref\nplus existing {{cite:L#3}}",
            "edge: empty",
        ],
    )
    def test_idempotent(self, text):
        first, _ = convert_raw_to_cite(text)
        second, _ = convert_raw_to_cite(first)
        assert first == second


class TestTargetValidator:
    """エッジケース表 #7, #8: target_validator の挙動"""

    def test_validator_false_keeps_dangling(self):
        out, counters = convert_raw_to_cite(
            "ref M#9999999 missing", target_validator=_deny_all
        )
        assert out == "ref M#9999999 missing"
        assert counters["sanitized_count"] == 0
        assert counters["skipped_dangling"] == 1

    def test_validator_true_converts(self):
        out, counters = convert_raw_to_cite(
            "ref M#1 ok", target_validator=_allow_all
        )
        assert out == "ref {{cite:M#1}} ok"
        assert counters["sanitized_count"] == 1
        assert counters["skipped_dangling"] == 0

    def test_validator_none_converts_all(self):
        # debug モード: 存在チェックなしで全変換
        out, counters = convert_raw_to_cite("ref M#1 D#2", target_validator=None)
        assert out == "ref {{cite:M#1}} {{cite:D#2}}"
        assert counters["sanitized_count"] == 2

    def test_validator_called_with_correct_args(self):
        seen: list[tuple[str, int]] = []

        def spy(target_type: str, target_id: int) -> bool:
            seen.append((target_type, target_id))
            return True

        convert_raw_to_cite("M#1 and D#2", target_validator=spy)
        assert seen == [("material", 1), ("decision", 2)]

    def test_validator_mixed_allow_deny(self):
        def selective(target_type: str, target_id: int) -> bool:
            return target_id < 100

        out, counters = convert_raw_to_cite(
            "M#1 and M#999", target_validator=selective
        )
        assert out == "{{cite:M#1}} and M#999"
        assert counters["sanitized_count"] == 1
        assert counters["skipped_dangling"] == 1


class TestWordBoundary:
    """エッジケース表 #9 派生: 識別子の途中・JSON 数値フィールドは変換しない"""

    def test_id_inside_identifier_not_converted(self):
        # `XM#1` のように直前に英字が付くケース
        out, counters = convert_raw_to_cite("foo_M#1 keep")
        assert out == "foo_M#1 keep"
        assert counters["sanitized_count"] == 0

    def test_id_with_alphanumeric_suffix_not_converted(self):
        # `M#1a` のように直後に英字
        out, counters = convert_raw_to_cite("ref M#1a end")
        assert out == "ref M#1a end"
        assert counters["sanitized_count"] == 0

    def test_json_numeric_field_unchanged(self):
        # JSON の数値フィールドは X#NNN 形式ではないので影響なし
        text = '{"activity_id": 123, "title": "x"}'
        out, counters = convert_raw_to_cite(text)
        assert out == text
        assert counters["sanitized_count"] == 0

    def test_id_after_url_slash_not_converted(self):
        # URL の path 末尾 `/M#1` も識別子の一部とみなしてスキップ
        out, _ = convert_raw_to_cite("https://x.example/M#1")
        assert out == "https://x.example/M#1"


class TestMultilineMixed:
    def test_multi_line_japanese_text(self):
        text = "本文の中で M#100 を参照する。\n別段落で D#200 も触れる。"
        out, counters = convert_raw_to_cite(text)
        assert out == "本文の中で {{cite:M#100}} を参照する。\n別段落で {{cite:D#200}} も触れる。"
        assert counters["sanitized_count"] == 2

    def test_counters_aggregate_across_lines(self):
        text = "first M#1\n`second L#2`\n```\nthird A#3\n```\nfourth T#4\nfifth \\D#5"
        out, counters = convert_raw_to_cite(text)
        assert counters["sanitized_count"] == 2  # M#1 と T#4
        assert counters["skipped_in_codeblock"] == 2  # `L#2` とフェンス内 A#3
        assert counters["skipped_escape"] == 1  # \D#5

    def test_idempotent_with_validator(self):
        text = "M#1 and M#999"

        def selective(t: str, i: int) -> bool:
            return i < 100

        first, _ = convert_raw_to_cite(text, target_validator=selective)
        second, _ = convert_raw_to_cite(first, target_validator=selective)
        assert first == second


@pytest.fixture
def temp_conn():
    """簡易 DB を作って citations_pure 関連テーブルを用意する。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "t.db")
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        for table in TYPE_TO_TABLE.values():
            conn.execute(
                f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, retracted_at TEXT)"
            )
        conn.commit()
        yield conn
        conn.close()


class TestCheckTargetExists:
    """エッジケース表 #10, #11: 存在判定は物理削除のみで判断 (retracted_at は無視)"""

    def test_existing_row_returns_true(self, temp_conn):
        temp_conn.execute("INSERT INTO materials (id) VALUES (?)", (42,))
        temp_conn.commit()
        assert check_target_exists(temp_conn, "material", 42) is True

    def test_missing_row_returns_false(self, temp_conn):
        assert check_target_exists(temp_conn, "decision", 99) is False

    def test_retracted_row_still_returns_true(self, temp_conn):
        temp_conn.execute(
            "INSERT INTO decisions (id, retracted_at) VALUES (?, ?)",
            (7, "2026-06-21T00:00:00Z"),
        )
        temp_conn.commit()
        assert check_target_exists(temp_conn, "decision", 7) is True

    def test_all_five_target_types_supported(self, temp_conn):
        for table in TYPE_TO_TABLE.values():
            temp_conn.execute(f"INSERT INTO {table} (id) VALUES (?)", (1,))
        temp_conn.commit()
        for code, type_name in TYPE_CODE_TO_NAME.items():
            assert check_target_exists(temp_conn, type_name, 1) is True

    def test_unknown_target_type_returns_false(self, temp_conn):
        assert check_target_exists(temp_conn, "unknown_type", 1) is False


class TestConvertWithValidatorAgainstDb:
    """convert_raw_to_cite + check_target_exists の結合動作"""

    def test_validator_using_check_target_exists(self, temp_conn):
        temp_conn.execute("INSERT INTO materials (id) VALUES (?)", (1,))
        temp_conn.commit()

        def validator(t: str, i: int) -> bool:
            return check_target_exists(temp_conn, t, i)

        out, counters = convert_raw_to_cite(
            "exists M#1 missing M#999", target_validator=validator
        )
        assert out == "exists {{cite:M#1}} missing M#999"
        assert counters["sanitized_count"] == 1
        assert counters["skipped_dangling"] == 1


class TestFullwordConversionHashOmittedNotConverted:
    """自動変換 (`convert_raw_to_cite`) は `#` 必須パターン
    (internal_id_patterns.RAW_CITE_FULLWORD_HASH_REQUIRED_PATTERN) のみを対象とし、
    `#` を省略した「type 名+スペース+数字」の並び (例: "decision 14") は変換しない。

    `#` 省略パターンは preblock_hook (block 用途) 専用であり、DB 上に該当 ID が
    実在する場合に "Activity 1" のような自然文まで citation へ書き換えてしまう
    実害があったため、自動変換側では `#` 必須パターンのみを使う仕様に切り分けている。

    期待値の組み立てで `#` を直接ソースに書くとこのファイル自体が PreToolUse hook
    のブロック対象になるため、`sharp = chr(35)` を使い動的に組み立てる
    (test_preblock_hook.py と同じ手法)。
    """

    def test_hash_omitted_one_space_not_converted(self) -> None:
        text = "see decision 14 here"
        out, counters = convert_raw_to_cite(text)
        assert out == text
        assert counters["sanitized_count"] == 0

    def test_hash_omitted_all_five_typenames_not_converted(self) -> None:
        text = "log 1 decision 2 activity 3 material 4 topic 5"
        out, counters = convert_raw_to_cite(text)
        assert out == text
        assert counters["sanitized_count"] == 0

    def test_hash_omitted_no_space_not_converted(self) -> None:
        out, counters = convert_raw_to_cite("decision14 is not converted")
        assert out == "decision14 is not converted"
        assert counters["sanitized_count"] == 0

    def test_hash_omitted_multiple_spaces_not_converted(self) -> None:
        out, counters = convert_raw_to_cite("decision  14 stays raw")
        assert out == "decision  14 stays raw"
        assert counters["sanitized_count"] == 0

    def test_hash_omitted_natural_sentence_with_trailing_word_not_converted(
        self,
    ) -> None:
        # レビュー指摘: 数字の後ろに単語が続く自然文 ("decision 14 days" 等) の
        # 誤変換シナリオに対する回帰テスト。
        text = "we will revisit this decision 14 days from now"
        out, counters = convert_raw_to_cite(text)
        assert out == text
        assert counters["sanitized_count"] == 0

    def test_hash_omitted_matches_real_ci_regression_fixtures(self) -> None:
        # test_active_context.py / test_topic_read.py で実際に誤変換が起きていた
        # fixture 文言 ("Activity 1" 等) そのものでの回帰テスト。
        for text in ("Activity 1", "Log 1", "Decision 1"):
            out, counters = convert_raw_to_cite(text)
            assert out == text
            assert counters["sanitized_count"] == 0

    def test_hash_omitted_escape_not_needed_and_text_unchanged(self) -> None:
        text = "see \\decision 14 literal"
        out, counters = convert_raw_to_cite(text)
        assert out == text
        assert counters["sanitized_count"] == 0
        assert counters["skipped_escape"] == 0

    def test_hash_omitted_inside_codeblock_not_converted(self) -> None:
        text = "before decision 14\n```\ndecision 15\n```\nafter"
        out, counters = convert_raw_to_cite(text)
        assert out == text
        assert counters["sanitized_count"] == 0
        assert counters["skipped_in_codeblock"] == 0

    def test_hash_omitted_idempotent(self) -> None:
        first, _ = convert_raw_to_cite("see decision 14 here")
        second, _ = convert_raw_to_cite(first)
        assert first == second

    def test_hash_omitted_validator_does_not_trigger_conversion(self) -> None:
        def validator(t: str, i: int) -> bool:
            return True

        text = "decision 1 and decision 999"
        out, counters = convert_raw_to_cite(text, target_validator=validator)
        assert out == text
        assert counters["sanitized_count"] == 0
        assert counters["skipped_dangling"] == 0

    def test_hash_required_form_still_converts(self) -> None:
        # 対照確認: `#` ありの従来形式は引き続き変換される。
        sharp = chr(35)
        out, counters = convert_raw_to_cite("see decision " + sharp + "14 here")
        assert out == "see {{cite:D" + sharp + "14}} here"
        assert counters["sanitized_count"] == 1


class TestFullwordConversion:
    """英語フルワード形式 (log/decision/activity/material/topic + #NNN) の変換。

    既存 code 形式と並行して変換され、結果は大文字 code 形式に正規化される
    (例: `log #123` → `{{cite:L#123}}`)。
    """

    def test_lowercase_with_space(self):
        out, counters = convert_raw_to_cite("see log #123 here")
        assert out == "see {{cite:L#123}} here"
        assert counters["sanitized_count"] == 1

    def test_lowercase_no_space(self):
        out, counters = convert_raw_to_cite("see log#123 here")
        assert out == "see {{cite:L#123}} here"
        assert counters["sanitized_count"] == 1

    def test_case_insensitive(self):
        out, counters = convert_raw_to_cite("Log #1 and LOG #2 and LoG #3")
        assert out == "{{cite:L#1}} and {{cite:L#2}} and {{cite:L#3}}"
        assert counters["sanitized_count"] == 3

    def test_all_five_typenames(self):
        out, _ = convert_raw_to_cite(
            "log #1 decision #2 activity #3 material #4 topic #5"
        )
        assert out == (
            "{{cite:L#1}} {{cite:D#2}} {{cite:A#3}} "
            "{{cite:M#4}} {{cite:T#5}}"
        )

    def test_double_space_does_not_convert(self):
        out, counters = convert_raw_to_cite("log  #1")
        assert out == "log  #1"
        assert counters["sanitized_count"] == 0

    def test_colon_does_not_convert(self):
        out, _ = convert_raw_to_cite("log: #1 and log:#2")
        assert out == "log: #1 and log:#2"

    def test_japanese_does_not_convert(self):
        out, _ = convert_raw_to_cite("ログ #1 and 決定事項 #2")
        assert out == "ログ #1 and 決定事項 #2"

    def test_word_boundary_blog_not_match(self):
        out, _ = convert_raw_to_cite("the blog #1 was published")
        assert out == "the blog #1 was published"

    def test_word_boundary_path_log_not_match(self):
        out, _ = convert_raw_to_cite("/var/log #1 is full")
        assert out == "/var/log #1 is full"

    def test_word_boundary_trailing_alnum_not_match(self):
        out, _ = convert_raw_to_cite("log #1abc is junk")
        assert out == "log #1abc is junk"

    def test_escape_backslash_not_converted(self):
        out, counters = convert_raw_to_cite("see \\log #1 literal")
        assert out == "see \\log #1 literal"
        assert counters["sanitized_count"] == 0
        assert counters["skipped_escape"] == 1

    def test_escape_works_for_all_typenames(self):
        out, counters = convert_raw_to_cite(
            "\\log #1 \\decision #2 \\activity #3 \\material #4 \\topic #5"
        )
        assert out == (
            "\\log #1 \\decision #2 \\activity #3 \\material #4 \\topic #5"
        )
        assert counters["sanitized_count"] == 0
        assert counters["skipped_escape"] == 5

    def test_codeblock_skips_fullword(self):
        text = "before log #1\n```\nlog #2\n```\nafter log #3"
        out, counters = convert_raw_to_cite(text)
        assert out == (
            "before {{cite:L#1}}\n```\nlog #2\n```\nafter {{cite:L#3}}"
        )
        assert counters["sanitized_count"] == 2
        assert counters["skipped_in_codeblock"] == 1

    def test_inline_backtick_skips_fullword(self):
        out, counters = convert_raw_to_cite("convert log #1 but not `log #2` here")
        assert out == "convert {{cite:L#1}} but not `log #2` here"
        assert counters["sanitized_count"] == 1
        assert counters["skipped_in_codeblock"] == 1

    def test_mixed_code_and_fullword(self):
        out, counters = convert_raw_to_cite(
            "M#1 and log #2 and D#3 and decision #4"
        )
        assert out == (
            "{{cite:M#1}} and {{cite:L#2}} and "
            "{{cite:D#3}} and {{cite:D#4}}"
        )
        assert counters["sanitized_count"] == 4

    def test_idempotent_existing_cite_not_reconverted(self):
        text = "{{cite:L#1}} and log #2"
        out, _ = convert_raw_to_cite(text)
        assert out == "{{cite:L#1}} and {{cite:L#2}}"
        # 2 回適用しても同じ結果
        out2, _ = convert_raw_to_cite(out)
        assert out2 == out

    def test_validator_blocks_dangling_fullword(self):
        def validator(t: str, i: int) -> bool:
            return t == "log" and i == 1

        out, counters = convert_raw_to_cite(
            "log #1 and log #999", target_validator=validator
        )
        assert out == "{{cite:L#1}} and log #999"
        assert counters["sanitized_count"] == 1
        assert counters["skipped_dangling"] == 1
