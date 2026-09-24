"""feedback_rules の pure 関数群の単体テスト。

parse_condition / validate_condition / evaluate_condition を対象とする。
"""
import re

import pytest

from src.services.feedback_rules import (
    ConditionError,
    evaluate_condition,
    maintenance_hint,
    parse_condition,
    validate_condition,
)


class TestParseCondition:
    def test_dict_passthrough(self):
        assert parse_condition({"tool": None, "all": []}) == {"tool": None, "all": []}

    def test_json_string_parsed(self):
        assert parse_condition('{"tool": "Bash", "all": []}') == {"tool": "Bash", "all": []}

    def test_invalid_json_raises(self):
        with pytest.raises(ConditionError):
            parse_condition("{not json")

    def test_non_object_raises(self):
        with pytest.raises(ConditionError):
            parse_condition("[1, 2, 3]")


class TestValidateConditionFormat:
    def test_minimal_valid_condition_accepted(self):
        result = validate_condition({"tool": None, "all": []}, strength="notify", timing="tool_fail")
        assert result == {"tool": None, "all": []}

    def test_unknown_key_rejected(self):
        with pytest.raises(ConditionError):
            validate_condition({"tool": None, "all": [], "extra": 1}, strength="notify", timing="tool_fail")

    def test_unknown_timing_rejected(self):
        with pytest.raises(ConditionError):
            validate_condition({"tool": None, "all": []}, strength="notify", timing="post_tool")

    def test_tool_empty_string_rejected(self):
        with pytest.raises(ConditionError):
            validate_condition({"tool": "", "all": []}, strength="notify", timing="tool_fail")

    def test_tool_non_string_rejected(self):
        with pytest.raises(ConditionError):
            validate_condition({"tool": 123, "all": []}, strength="notify", timing="tool_fail")

    def test_tool_valid_on_pre_tool_accepted(self):
        result = validate_condition(
            {"tool": "Bash", "all": []}, strength="block", timing="pre_tool"
        )
        assert result["tool"] == "Bash"

    def test_utterance_tool_non_null_rejected(self):
        with pytest.raises(ConditionError):
            validate_condition({"tool": "Bash", "all": []}, strength="notify", timing="utterance")

    def test_utterance_tool_null_accepted(self):
        result = validate_condition(
            {"tool": None, "all": [{"field": "prompt", "op": "len_gt", "value": 5}]},
            strength="notify",
            timing="utterance",
        )
        assert result["tool"] is None

    def test_all_over_3_clauses_rejected(self):
        clauses = [{"field": "x", "op": "len_gt", "value": 1} for _ in range(4)]
        with pytest.raises(ConditionError):
            validate_condition({"tool": None, "all": clauses}, strength="notify", timing="tool_fail")

    def test_all_not_a_list_rejected(self):
        with pytest.raises(ConditionError):
            validate_condition({"tool": None, "all": "x"}, strength="notify", timing="tool_fail")

    def test_clause_missing_key_rejected(self):
        with pytest.raises(ConditionError):
            validate_condition(
                {"tool": None, "all": [{"field": "x", "op": "len_gt"}]},
                strength="notify",
                timing="tool_fail",
            )

    def test_clause_extra_key_rejected(self):
        with pytest.raises(ConditionError):
            validate_condition(
                {"tool": None, "all": [{"field": "x", "op": "len_gt", "value": 1, "extra": 1}]},
                strength="notify",
                timing="tool_fail",
            )

    def test_clause_field_empty_rejected(self):
        with pytest.raises(ConditionError):
            validate_condition(
                {"tool": None, "all": [{"field": "", "op": "len_gt", "value": 1}]},
                strength="notify",
                timing="tool_fail",
            )

    def test_utterance_field_must_be_prompt(self):
        with pytest.raises(ConditionError):
            validate_condition(
                {"tool": None, "all": [{"field": "other", "op": "len_gt", "value": 1}]},
                strength="notify",
                timing="utterance",
            )

    def test_tool_fail_field_error_accepted(self):
        result = validate_condition(
            {"tool": None, "all": [{"field": "error", "op": "len_gt", "value": 1}]},
            strength="notify",
            timing="tool_fail",
        )
        assert result["all"][0]["field"] == "error"

    def test_op_out_of_range_rejected(self):
        with pytest.raises(ConditionError):
            validate_condition(
                {"tool": None, "all": [{"field": "x", "op": "contains", "value": "a"}]},
                strength="notify",
                timing="tool_fail",
            )

    def test_regex_value_not_string_rejected(self):
        with pytest.raises(ConditionError):
            validate_condition(
                {"tool": None, "all": [{"field": "x", "op": "regex", "value": 1}]},
                strength="notify",
                timing="tool_fail",
            )

    def test_regex_value_over_200_chars_rejected(self):
        with pytest.raises(ConditionError):
            validate_condition(
                {"tool": None, "all": [{"field": "x", "op": "regex", "value": "a" * 201}]},
                strength="notify",
                timing="tool_fail",
            )

    def test_regex_invalid_pattern_rejected(self):
        with pytest.raises(ConditionError):
            validate_condition(
                {"tool": None, "all": [{"field": "x", "op": "regex", "value": "("}]},
                strength="notify",
                timing="tool_fail",
            )

    def test_len_gt_value_not_numeric_rejected(self):
        with pytest.raises(ConditionError):
            validate_condition(
                {"tool": None, "all": [{"field": "x", "op": "len_gt", "value": "5"}]},
                strength="notify",
                timing="tool_fail",
            )

    def test_len_gt_value_bool_rejected(self):
        """boolはintのサブクラスなので明示的に弾く必要がある。"""
        with pytest.raises(ConditionError):
            validate_condition(
                {"tool": None, "all": [{"field": "x", "op": "len_gt", "value": True}]},
                strength="notify",
                timing="tool_fail",
            )

    def test_block_with_null_tool_and_empty_all_rejected(self):
        with pytest.raises(ConditionError):
            validate_condition({"tool": None, "all": []}, strength="block", timing="pre_tool")

    def test_block_with_tool_and_empty_all_accepted(self):
        result = validate_condition(
            {"tool": "Bash", "all": []}, strength="block", timing="pre_tool"
        )
        assert result == {"tool": "Bash", "all": []}

    def test_block_with_null_tool_and_nonempty_all_accepted(self):
        result = validate_condition(
            {"tool": None, "all": [{"field": "command", "op": "len_gt", "value": 0}]},
            strength="block",
            timing="pre_tool",
        )
        assert result["all"]


class TestEvaluateConditionDotPath:
    def test_single_key_path_matches(self):
        cond = {"tool": None, "all": [{"field": "command", "op": "regex", "value": "rm -rf"}]}
        assert evaluate_condition(
            cond, timing="pre_tool", tool_input={"command": "rm -rf /tmp"}
        )

    def test_nested_key_path_matches(self):
        cond = {"tool": None, "all": [{"field": "a.b", "op": "len_gt", "value": 2}]}
        assert evaluate_condition(cond, timing="pre_tool", tool_input={"a": {"b": "xyz"}})

    def test_missing_key_does_not_match(self):
        cond = {"tool": None, "all": [{"field": "missing", "op": "len_gt", "value": 0}]}
        assert not evaluate_condition(cond, timing="pre_tool", tool_input={"a": 1})

    def test_path_through_list_does_not_match(self):
        """途中でlistに当たる場合は不一致（例外にしない）。"""
        cond = {"tool": None, "all": [{"field": "items.x", "op": "len_gt", "value": 0}]}
        assert not evaluate_condition(cond, timing="pre_tool", tool_input={"items": [1, 2, 3]})

    def test_terminal_list_value_is_stringified(self):
        """最終的に解決された値がlistの場合はstr()化してから評価する（途中で当たるのとは別）。"""
        cond = {"tool": None, "all": [{"field": "items", "op": "regex", "value": r"\[1, 2, 3\]"}]}
        assert evaluate_condition(cond, timing="pre_tool", tool_input={"items": [1, 2, 3]})

    def test_non_string_value_is_stringified(self):
        cond = {"tool": None, "all": [{"field": "count", "op": "regex", "value": "^42$"}]}
        assert evaluate_condition(cond, timing="pre_tool", tool_input={"count": 42})

    def test_null_value_is_stringified_to_none(self):
        cond = {"tool": None, "all": [{"field": "x", "op": "regex", "value": "^None$"}]}
        assert evaluate_condition(cond, timing="pre_tool", tool_input={"x": None})

    def test_tool_input_not_a_dict_does_not_match(self):
        cond = {"tool": None, "all": [{"field": "x", "op": "len_gt", "value": 0}]}
        assert not evaluate_condition(cond, timing="pre_tool", tool_input="not a dict")

    def test_tool_input_none_does_not_match(self):
        cond = {"tool": None, "all": [{"field": "x", "op": "len_gt", "value": 0}]}
        assert not evaluate_condition(cond, timing="pre_tool", tool_input=None)


class TestEvaluateConditionToolMatch:
    def test_tool_none_matches_any_tool(self):
        cond = {"tool": None, "all": []}
        assert evaluate_condition(cond, timing="pre_tool", tool_name="Bash", tool_input={})

    def test_tool_exact_match(self):
        cond = {"tool": "Bash", "all": []}
        assert evaluate_condition(cond, timing="pre_tool", tool_name="Bash", tool_input={})

    def test_tool_mismatch_does_not_match(self):
        cond = {"tool": "Bash", "all": []}
        assert not evaluate_condition(cond, timing="pre_tool", tool_name="Write", tool_input={})

    def test_tool_set_on_utterance_never_matches(self):
        """write時に拒否されるはずの組み合わせだが、評価器側も防御的にFalseにする。"""
        cond = {"tool": "Bash", "all": []}
        assert not evaluate_condition(cond, timing="utterance", prompt_text="hello")


class TestEvaluateConditionUtteranceAndToolFail:
    def test_utterance_prompt_field_matches(self):
        cond = {"tool": None, "all": [{"field": "prompt", "op": "regex", "value": "help"}]}
        assert evaluate_condition(cond, timing="utterance", prompt_text="please help me")

    def test_tool_fail_error_field_matches(self):
        cond = {"tool": None, "all": [{"field": "error", "op": "regex", "value": "timeout"}]}
        assert evaluate_condition(cond, timing="tool_fail", error_text="connection timeout")

    def test_tool_fail_non_error_field_uses_tool_input(self):
        cond = {"tool": None, "all": [{"field": "file_path", "op": "regex", "value": r"\.py$"}]}
        assert evaluate_condition(
            cond, timing="tool_fail", tool_input={"file_path": "foo.py"}, error_text="boom"
        )


class TestEvaluateConditionTruncation:
    def test_text_truncated_before_len_gt(self):
        long_text = "a" * 25_000
        cond = {"tool": None, "all": [{"field": "prompt", "op": "len_gt", "value": 20_000}]}
        # 20,000字で切り詰められるため、25,000字でも20,000を超えない
        assert not evaluate_condition(cond, timing="utterance", prompt_text=long_text)

    def test_regex_only_searches_within_truncated_prefix(self):
        text = "a" * 20_000 + "NEEDLE"
        cond = {"tool": None, "all": [{"field": "prompt", "op": "regex", "value": "NEEDLE"}]}
        assert not evaluate_condition(cond, timing="utterance", prompt_text=text)


class TestEvaluateConditionCorruptedStoredRegex:
    def test_invalid_stored_regex_raises(self):
        """保存後に壊れた正規表現（write時検証をすり抜けた想定）はre.errorを送出する。

        呼び出し側(hook)がエントリ単位でtry/exceptし、そのエントリだけ
        スキップする設計を前提にしているため、ここでは握りつぶさない。
        """
        cond = {"tool": None, "all": [{"field": "prompt", "op": "regex", "value": "("}]}
        with pytest.raises(re.error):
            evaluate_condition(cond, timing="utterance", prompt_text="anything")


class TestEvaluateConditionAllClauses:
    def test_all_clauses_must_match(self):
        cond = {
            "tool": None,
            "all": [
                {"field": "a", "op": "len_gt", "value": 0},
                {"field": "b", "op": "len_gt", "value": 0},
            ],
        }
        assert not evaluate_condition(cond, timing="pre_tool", tool_input={"a": "x"})
        assert evaluate_condition(cond, timing="pre_tool", tool_input={"a": "x", "b": "y"})

    def test_empty_all_with_tool_match_matches(self):
        cond = {"tool": "Bash", "all": []}
        assert evaluate_condition(cond, timing="pre_tool", tool_name="Bash", tool_input={})


class TestMaintenanceHint:
    def test_review_multiple_of_10_returns_review_line_only(self):
        for result in (maintenance_hint(10, 0), maintenance_hint(20, 0)):
            assert "見直し時期" in result
            assert "未処理の躓き" not in result
            assert "\n" not in result

    def test_just_below_or_above_10_returns_empty(self):
        assert maintenance_hint(9, 0) == ""
        assert maintenance_hint(11, 0) == ""

    def test_delivered_n_zero_returns_empty(self):
        assert maintenance_hint(0, 0) == ""

    def test_pending_stumbles_at_promote_threshold_returns_promote_line(self):
        result = maintenance_hint(1, 3)
        assert "未処理の躓き3件" in result
        assert "見直し時期" not in result

    def test_pending_stumbles_below_threshold_returns_empty(self):
        assert maintenance_hint(1, 2) == ""

    def test_both_conditions_join_review_then_promote_no_edge_newlines(self):
        result = maintenance_hint(10, 3)
        lines = result.split("\n")
        assert len(lines) == 2
        assert "見直し時期" in lines[0]
        assert "未処理の躓き3件" in lines[1]
        assert not result.startswith("\n")
        assert not result.endswith("\n")
