"""scripts/check_test_removal.py のうちgitを介さない純粋関数を検証する unit test。

git worktree/uv sync/pytest --collect-only を実際に起動する経路
(`collect_test_ids` / `detect_file_renames` / `main` の統合的な振る舞い)は
tests/e2e/test_check_test_removal.py で検証する。
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.check_test_removal import (  # noqa: E402
    apply_renames,
    decide,
    has_removal_marker,
    parse_collect_output,
    to_function_id,
)


class TestToFunctionId:
    def test_strips_parametrize_suffix_on_plain_function(self):
        assert to_function_id("tests/unit/test_foo.py::test_bar[case1]") == "tests/unit/test_foo.py::test_bar"

    def test_strips_parametrize_suffix_on_class_method(self):
        node_id = "tests/unit/test_foo.py::TestFoo::test_bar[case1]"
        assert to_function_id(node_id) == "tests/unit/test_foo.py::TestFoo::test_bar"

    def test_no_parametrize_suffix_is_unchanged(self):
        node_id = "tests/unit/test_foo.py::TestFoo::test_bar"
        assert to_function_id(node_id) == node_id

    def test_parametrize_id_containing_brackets_is_still_stripped_from_first_bracket(self):
        node_id = "tests/unit/test_foo.py::test_bar[value with [nested] bracket]"
        assert to_function_id(node_id) == "tests/unit/test_foo.py::test_bar"


class TestParseCollectOutput:
    def test_extracts_node_ids_and_ignores_summary_lines(self):
        stdout = (
            "tests/unit/test_a.py::test_one\n"
            "tests/unit/test_a.py::TestA::test_two[case1]\n"
            "\n"
            "2 tests collected in 0.02s\n"
        )
        assert parse_collect_output(stdout) == {
            "tests/unit/test_a.py::test_one",
            "tests/unit/test_a.py::TestA::test_two",
        }

    def test_parametrize_id_with_embedded_space_is_kept_as_one_entry(self):
        # pytestのparametrize idは `[...]` 内に空白を含みうる。行頭が `<path>.py::`
        # であれば行全体を1つのnode IDとして扱う必要がある。
        stdout = "tests/unit/test_a.py::TestA::test_two[case with space]\n\n1 test collected in 0.01s\n"
        assert parse_collect_output(stdout) == {"tests/unit/test_a.py::TestA::test_two"}

    def test_warning_and_deprecation_lines_are_not_mistaken_for_node_ids(self):
        stdout = (
            "tests/unit/test_a.py::test_one\n"
            "=============================== warnings summary ===============================\n"
            ".venv/lib/python3.12/site-packages/foo.py:14\n"
            "  DeprecationWarning: foo is deprecated\n"
        )
        assert parse_collect_output(stdout) == {"tests/unit/test_a.py::test_one"}

    def test_empty_output_returns_empty_set(self):
        assert parse_collect_output("") == set()


class TestApplyRenames:
    def test_remaps_path_component_of_matching_ids(self):
        ids = {"tests/unit/test_old.py::TestFoo::test_bar", "tests/unit/test_other.py::test_baz"}
        renames = {"tests/unit/test_old.py": "tests/unit/test_new.py"}
        assert apply_renames(ids, renames) == {
            "tests/unit/test_new.py::TestFoo::test_bar",
            "tests/unit/test_other.py::test_baz",
        }

    def test_no_renames_returns_original_set_unchanged(self):
        ids = {"tests/unit/test_a.py::test_one"}
        assert apply_renames(ids, {}) == ids

    def test_id_with_path_not_in_rename_map_is_left_alone(self):
        ids = {"tests/unit/test_untouched.py::test_one"}
        renames = {"tests/unit/test_other.py": "tests/unit/test_renamed.py"}
        assert apply_renames(ids, renames) == ids


class TestHasRemovalMarker:
    def test_detects_marker_line(self):
        body = "some description\n\n[test-removal: 重複テストの整理]\n\nmore text\n"
        assert has_removal_marker(body) is True

    def test_detects_marker_with_leading_whitespace(self):
        body = "  [test-removal: reason here]\n"
        assert has_removal_marker(body) is True

    def test_no_marker_returns_false(self):
        assert has_removal_marker("just a normal PR description\n") is False

    def test_marker_text_mentioned_mid_sentence_is_not_a_marker(self):
        # 行頭一致のみを許可する。文中に埋め込まれた言及では通過させない
        # (無自覚な削除の見逃しを防ぐ設計)。
        body = "この変更は [test-removal: ...] のような形式を今後使う予定、という説明文\n"
        assert has_removal_marker(body) is False

    def test_empty_body_returns_false(self):
        assert has_removal_marker("") is False


class TestDecide:
    def test_no_removed_functions_is_allowed_regardless_of_marker(self):
        base_ids = {"tests/unit/test_a.py::test_one"}
        head_ids = {"tests/unit/test_a.py::test_one", "tests/unit/test_a.py::test_two"}
        removed, allowed = decide(base_ids, head_ids, pr_body="")
        assert removed == []
        assert allowed is True

    def test_removed_function_without_marker_is_not_allowed(self):
        base_ids = {"tests/unit/test_a.py::test_one", "tests/unit/test_a.py::test_two"}
        head_ids = {"tests/unit/test_a.py::test_one"}
        removed, allowed = decide(base_ids, head_ids, pr_body="normal description\n")
        assert removed == ["tests/unit/test_a.py::test_two"]
        assert allowed is False

    def test_removed_function_with_marker_is_allowed(self):
        base_ids = {"tests/unit/test_a.py::test_one", "tests/unit/test_a.py::test_two"}
        head_ids = {"tests/unit/test_a.py::test_one"}
        removed, allowed = decide(base_ids, head_ids, pr_body="[test-removal: 重複テストの整理]\n")
        assert removed == ["tests/unit/test_a.py::test_two"]
        assert allowed is True

    def test_removed_list_is_sorted(self):
        base_ids = {"tests/unit/test_a.py::test_z", "tests/unit/test_a.py::test_a"}
        head_ids = set()
        removed, _allowed = decide(base_ids, head_ids, pr_body="")
        assert removed == ["tests/unit/test_a.py::test_a", "tests/unit/test_a.py::test_z"]
