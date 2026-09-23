"""response_budget（全体予算切り詰めモジュール）のユニットテスト。

応答dictと方針(BudgetPolicy)だけを入力に取る、DBに触れない純粋なモジュールを
対象にする。ここでは check_in の実際の形に依存しない合成のpolicyを使い、
削る手順・pinnedの縮小・capped_sections・ハード上限の各契約を検証する。
check_inの実際の応答形（coverage書き換えの対応表等）に対する検証は
tests/integration/test_checkin_service.py 側で行う。
"""
import json

import pytest

from src.services import response_budget as rb


def _policy(**overrides) -> rb.BudgetPolicy:
    base = dict(
        budget_chars=100,
        hard_max_chars=100000,
        protected_paths=frozenset({"activity", "kept"}),
        capped_sections=(),
        pinned=None,
        cut_steps=(
            rb.CutStep(path="list_a", mode="tail_list"),
            rb.CutStep(path="list_b", mode="tail_list", coverage_key="b"),
        ),
    )
    base.update(overrides)
    return rb.BudgetPolicy(**base)


class TestMeasureChars:
    def test_matches_json_dumps_length(self):
        obj = {"a": "あ", "b": 1}
        assert rb.measure_chars(obj) == len(json.dumps(obj, ensure_ascii=False))


class TestApplyBudgetPassthrough:
    def test_error_response_untouched(self):
        response = {"error": {"code": "NOT_FOUND", "message": "x"}}
        out = rb.apply_budget(response, _policy())
        assert out == response

    def test_under_budget_no_truncated_key(self):
        response = {"activity": "a", "list_a": ["x"]}
        out = rb.apply_budget(response, _policy(budget_chars=10_000))
        assert "truncated" not in out

    def test_does_not_mutate_input(self):
        response = {"list_a": [str(i) * 20 for i in range(20)]}
        rb.apply_budget(response, _policy())
        assert len(response["list_a"]) == 20  # 呼び出し元の元dictは変えない


class TestCutSteps:
    def test_tail_list_removes_from_the_end(self):
        response = {"list_a": ["keep-1", "keep-2", "drop-3", "drop-4", "drop-5"]}
        # budget_charsを小さくして list_a だけで削らせる
        out = rb.apply_budget(response, _policy(budget_chars=40))
        assert out["list_a"][-1] != "drop-5"  # 末尾（新しい順の末尾=古いもの）から消える
        assert "truncated" in out
        cut_sections = [c["section"] for c in out["truncated"]["cuts"]]
        assert "list_a" in cut_sections

    def test_falsification_disabling_cut_step_leaves_over_budget(self):
        """cut_stepsを空にすると予算超過のまま残る（このテストがカバーする分岐が
        機能していないことを検出できる、という自己検証）。"""
        response = {"list_a": ["x" * 20] * 10}
        broken_policy = _policy(cut_steps=())
        out = rb.apply_budget(response, broken_policy)
        assert out["truncated"]["over_budget"] is True
        assert len(out["list_a"]) == 10  # 削られていない

    def test_coverage_numerator_rewritten_after_cut(self):
        response = {
            "coverage": {"b": "5/9"},
            "list_b": ["x" * 20] * 5,
        }
        out = rb.apply_budget(response, _policy(budget_chars=30))
        assert out["coverage"]["b"].endswith("/9")
        numerator = int(out["coverage"]["b"].split("/")[0])
        assert numerator == len(out["list_b"])
        assert numerator < 5

    def test_stub_dict_mode_replaces_with_stub(self):
        response = {"doc": {"id_raw": 7, "title": "T", "content": "x" * 500}}
        policy = _policy(
            budget_chars=50,
            cut_steps=(rb.CutStep(path="doc", mode="stub_dict", coverage_key=None),),
        )
        out = rb.apply_budget(response, policy)
        assert out["doc"] == {"id_raw": 7, "title": "T", "chars": rb.measure_chars(
            {"id_raw": 7, "title": "T", "content": "x" * 500}
        )}

    def test_pointer_attached_to_cut(self):
        response = {"list_a": ["x" * 20] * 10}
        policy = _policy(
            budget_chars=30,
            cut_steps=(
                rb.CutStep(
                    path="list_a", mode="tail_list",
                    pointer=lambda r: [{"tool": "get_map", "args": {"entity_id": 1}}],
                ),
            ),
        )
        out = rb.apply_budget(response, policy)
        assert out["truncated"]["cuts"][0]["next"] == [{"tool": "get_map", "args": {"entity_id": 1}}]


class TestProtectedPaths:
    def test_protected_path_never_cut_even_if_it_dominates_budget(self):
        response = {"activity": {"description": "x" * 500}, "list_a": ["y"] * 3}
        out = rb.apply_budget(response, _policy(budget_chars=50))
        assert out["activity"]["description"] == "x" * 500
        assert out["truncated"]["over_budget"] is True


class TestCappedSections:
    def test_capped_section_not_counted_toward_main_budget(self):
        response = {"control_a": "x" * 500, "list_a": ["y"]}
        policy = _policy(
            budget_chars=50,
            capped_sections=(
                rb.CappedSection(name="control", paths=("control_a",), cap_chars=100_000, fold=None),
            ),
        )
        out = rb.apply_budget(response, policy)
        # control_aは予算に数えないので、それだけでbudgetを超えていても
        # list_aは削られない(under budget)
        assert "truncated" not in out

    def test_capped_section_over_cap_sets_flag_without_fold(self):
        response = {"control_a": "x" * 50}
        policy = _policy(
            budget_chars=100_000,
            capped_sections=(
                rb.CappedSection(name="control", paths=("control_a",), cap_chars=10, fold=None),
            ),
        )
        out = rb.apply_budget(response, policy)
        assert out["truncated"]["control_over"] is True
        assert out["control_a"] == "x" * 50  # foldが無いので中身はそのまま

    def test_capped_section_fold_is_invoked_when_over_cap(self):
        calls = []

        def _fold(response):
            calls.append(True)
            response["control_a"] = "folded"

        response = {"control_a": "x" * 50}
        policy = _policy(
            budget_chars=100_000,
            capped_sections=(
                rb.CappedSection(name="control", paths=("control_a",), cap_chars=10, fold=_fold),
            ),
        )
        out = rb.apply_budget(response, policy)
        assert calls == [True]
        assert out["control_a"] == "folded"

    def test_falsification_fold_not_called_when_under_cap(self):
        calls = []

        def _fold(response):
            calls.append(True)

        response = {"control_a": "x" * 5}
        policy = _policy(
            budget_chars=100_000,
            capped_sections=(
                rb.CappedSection(name="control", paths=("control_a",), cap_chars=1000, fold=_fold),
            ),
        )
        rb.apply_budget(response, policy)
        assert calls == []


class TestPinned:
    def _pinned_policy(self, slot_chars):
        return rb.PinnedPolicy(
            path="pinned", slot_chars=slot_chars,
            content_field={"materials": "content"},
            pointer=lambda r, child_key, item_id: [{"tool": "get_material", "args": {"material_id": item_id}}],
        )

    def test_small_items_kept_whole_large_item_stubbed(self):
        response = {
            "pinned": {
                "materials": [
                    {"id_raw": 1, "title": "small", "content": "x" * 10},
                    {"id_raw": 2, "title": "big", "content": "y" * 500},
                ]
            }
        }
        # pinned自体（実測631字）が全体予算を超えるように budget_chars を小さくする
        # （pinnedの枠は総字数が全体予算を超えたときにしか働かない、という仕様どおりの
        # 前提）。ただし小さすぎると枠内シュリンク後もなお予算超過のままとなり、末尾の
        # 「pinnedの枠内分をさらに0へ向けて縮める」手順まで走ってしまうため、
        # 1回目の枠(60字)シュリンク後のサイズより大きい値にする。
        policy = _policy(budget_chars=300, pinned=self._pinned_policy(slot_chars=60))
        out = rb.apply_budget(response, policy)
        items = out["pinned"]["materials"]
        small = next(i for i in items if i["id_raw"] == 1)
        big = next(i for i in items if i["id_raw"] == 2)
        assert small["content"] == "x" * 10  # 小さい方は丸ごと残る
        assert "content" not in big or len(big.get("content", "")) < 500  # 大きい方はスタブ化/切り詰め
        assert big.get("next") == [{"tool": "get_material", "args": {"material_id": 2}}]

    def test_crossing_item_gets_prefix_and_content_truncated_flag(self):
        response = {
            "pinned": {
                "materials": [
                    {"id_raw": 1, "title": "only", "content": "z" * 200},
                ]
            }
        }
        policy = _policy(budget_chars=100, pinned=self._pinned_policy(slot_chars=60))
        out = rb.apply_budget(response, policy)
        item = out["pinned"]["materials"][0]
        assert item["content_truncated"] is True
        assert item["content"] == ("z" * 200)[: len(item["content"])]
        assert len(item["content"]) < 200

    def test_within_slot_pinned_not_touched_even_if_total_over_budget(self):
        """全体予算は超えているが、pinned自体は枠(3000字)以内なら削らない
        （pinnedはリスト系(cut_steps)より先に処理され、枠内なら手を付けない）。
        """
        response = {
            "pinned": {"materials": [{"id_raw": 1, "title": "t", "content": "x" * 10}]},
            "list_a": ["y" * 20] * 10,
        }
        # 実測: pinned単体81字、list_a込みで333字。budget_chars=300なら、list_aを
        # 数件削るだけで予算内に収まり、pinnedの0へ向けたシュリンクまでは走らない。
        policy = _policy(budget_chars=300, pinned=self._pinned_policy(slot_chars=3000))
        out = rb.apply_budget(response, policy)
        assert out["pinned"] == response["pinned"]
        assert out["truncated"]["over_budget"] is False


class TestHardMax:
    def test_description_cut_only_when_hard_max_exceeded(self):
        # activityは保護パス（cut_stepsでは削られない）なので、40,000字のdescriptionは
        # budget_chars(100)を超えたまま他の削る手順を使い切ってもover_budgetで残る。
        # そこをhard_max_chars(1000)が最後の手段として切る。
        response = {"activity": {"description": "d" * 40000}, "list_a": []}
        policy = _policy(budget_chars=100, hard_max_chars=1000)
        out = rb.apply_budget(response, policy)
        assert len(out["activity"]["description"]) < 40000
        assert out["activity"]["description_truncated"] is True
        assert out["truncated"]["hard_max"] is True

    def test_falsification_hard_max_not_applied_under_hard_max_chars(self):
        response = {"activity": {"description": "d" * 200}, "list_a": ["y" * 20] * 10}
        policy = _policy(budget_chars=30, hard_max_chars=100_000)
        out = rb.apply_budget(response, policy)
        assert out["activity"]["description"] == "d" * 200
        assert "hard_max" not in out.get("truncated", {})
