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
        # `\{{cite:M#1}}` 形式: 既存パーサの規律と同じく丸ごとスキップ
        text = r"shown \{{cite:M#1}} only"
        out, counters = convert_raw_to_cite(text)
        assert out == text
        assert counters["sanitized_count"] == 0


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
