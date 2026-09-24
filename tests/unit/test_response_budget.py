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

    def test_after_reflects_actual_size_once_hard_max_cuts_description(self):
        """truncated.afterはhard_max発動後の実サイズと一致する（hard_max発動前の
        古い値のままにならない）。"""
        response = {"activity": {"description": "d" * 40000}, "list_a": []}
        policy = _policy(budget_chars=100, hard_max_chars=1000)
        out = rb.apply_budget(response, policy)
        assert out["truncated"]["hard_max"] is True
        # truncated自体を除いた実サイズ（apply_budget内でtotalを数える対象と同じ範囲）
        actual_size_without_truncated_key = rb.measure_chars(
            {k: v for k, v in out.items() if k != "truncated"}
        )
        assert out["truncated"]["after"] == actual_size_without_truncated_key
        assert out["truncated"]["after"] < 40000

    def test_falsification_after_stale_without_resync(self):
        """afterがhard_max前の値のまま（今回の修正が無効化された場合）だと、
        実サイズと食い違ってこのテストが落ちる、という裏取り。"""
        response = {"activity": {"description": "d" * 40000}, "list_a": []}
        policy = _policy(budget_chars=100, hard_max_chars=1000)
        stale_after = rb.measure_chars(response)  # hard_max適用前の（古い）total相当
        out = rb.apply_budget(response, policy)
        assert out["truncated"]["after"] != stale_after


class TestBudgetPolicyValidation:
    """protected_pathsは宣言だけでなく、cut_steps/pinnedとの重複が無いことを
    構築時に検証する（宣言と実装のずれを事故る前に検出する）。"""

    def test_rejects_cut_step_targeting_a_protected_path(self):
        with pytest.raises(ValueError):
            rb.BudgetPolicy(
                budget_chars=100, hard_max_chars=1000,
                protected_paths=frozenset({"activity"}),
                capped_sections=(), pinned=None,
                cut_steps=(rb.CutStep(path="activity", mode="tail_list"),),
            )

    def test_rejects_pinned_path_that_is_also_protected(self):
        with pytest.raises(ValueError):
            rb.BudgetPolicy(
                budget_chars=100, hard_max_chars=1000,
                protected_paths=frozenset({"pinned"}),
                capped_sections=(),
                pinned=rb.PinnedPolicy(path="pinned", slot_chars=10, content_field={}),
                cut_steps=(),
            )

    def test_falsification_non_overlapping_policy_still_constructs(self):
        policy = _policy()  # protected_paths={"activity","kept"}, cut_steps targets list_a/list_b
        assert policy.protected_paths == frozenset({"activity", "kept"})


class TestGetPath:
    def test_returns_nested_value_for_dotted_path(self):
        assert rb.get_path({"a": {"b": {"c": 1}}}, "a.b.c") == 1

    def test_missing_key_returns_none(self):
        assert rb.get_path({"a": {"b": 1}}, "a.x") is None

    def test_non_dict_intermediate_node_returns_none(self):
        assert rb.get_path({"a": "not-a-dict"}, "a.b") is None

    def test_no_dot_behaves_like_top_level_key(self):
        assert rb.get_path({"a": 1}, "a") == 1


class TestDottedCutStepPaths:
    """CutStep.pathがドット区切りで入れ子キーを指すとき、response直下のキーと
    同じ手順（tail_list/stub_dict）が入れ子の値にもそのまま働くことを確認する。
    """

    def test_tail_list_cuts_nested_list(self):
        response = {"container": {"list_a": ["x" * 20] * 10}}
        policy = _policy(
            budget_chars=40,
            cut_steps=(rb.CutStep(path="container.list_a", mode="tail_list"),),
        )
        out = rb.apply_budget(response, policy)
        assert len(out["container"]["list_a"]) < 10
        assert out["truncated"]["cuts"][0]["section"] == "container.list_a"

    def test_stub_dict_replaces_nested_dict(self):
        response = {"container": {"doc": {"id_raw": 7, "title": "T", "content": "x" * 500}}}
        policy = _policy(
            budget_chars=50,
            cut_steps=(rb.CutStep(path="container.doc", mode="stub_dict"),),
        )
        out = rb.apply_budget(response, policy)
        assert out["container"]["doc"] == {
            "id_raw": 7, "title": "T",
            "chars": rb.measure_chars({"id_raw": 7, "title": "T", "content": "x" * 500}),
        }

    def test_falsification_top_level_path_does_not_reach_nested_list(self):
        """CutStep.pathをドット無しの"list_a"のままにすると、container配下の
        リストは見つからず削られない（ドット区切りpathが実際に入れ子を
        探索していることの裏取り）。"""
        response = {"container": {"list_a": ["x" * 20] * 10}}
        policy = _policy(budget_chars=40, cut_steps=(rb.CutStep(path="list_a", mode="tail_list"),))
        out = rb.apply_budget(response, policy)
        assert len(out["container"]["list_a"]) == 10
        assert out["truncated"]["over_budget"] is True


class TestDottedCappedSectionExclusion:
    """capped_sections.pathsがドット区切りで、同じ祖先を共有する複数の入れ子pathを
    指すとき、両方とも全体予算から除外される（片方だけでは除外し切れない）ことを
    確認する。
    """

    def test_multiple_nested_paths_sharing_an_ancestor_are_both_excluded(self):
        response = {"env": {"tag_notes": "n" * 300, "hints": "h" * 300, "kept_sibling": "k" * 5}}
        policy = _policy(
            budget_chars=50,
            cut_steps=(),
            capped_sections=(
                rb.CappedSection(
                    name="capped", paths=("env.tag_notes", "env.hints"),
                    cap_chars=100_000, fold=None,
                ),
            ),
        )
        out = rb.apply_budget(response, policy)
        # tag_notes/hints抜きならenv.kept_sibling(5字)だけなので予算(50字)内に収まる
        assert "truncated" not in out
        # 祖先(env)自体はコピーとして残り、除外対象でない兄弟キーは保たれる
        assert out["env"]["kept_sibling"] == "k" * 5

    def test_sibling_key_under_shared_ancestor_still_counted(self):
        """除外対象に指定していない兄弟キー(env.other)は、祖先を共有していても
        引き続き全体予算に数えられる。"""
        response = {"env": {"tag_notes": "n" * 5, "other": "o" * 200}}
        policy = _policy(
            budget_chars=50,
            cut_steps=(),
            capped_sections=(
                rb.CappedSection(name="capped", paths=("env.tag_notes",), cap_chars=100_000, fold=None),
            ),
        )
        out = rb.apply_budget(response, policy)
        assert out["truncated"]["over_budget"] is True  # env.otherだけで予算超過

    def test_falsification_excluding_only_one_of_two_nested_paths_still_over_budget(self):
        """env.tag_notes/env.hintsの片方だけを除外指定すると、除外し損ねた方が
        残って予算超過のままになる（上のテストが両方の除外を見ていることの裏取り）。"""
        response = {"env": {"tag_notes": "n" * 300, "hints": "h" * 300}}
        policy = _policy(
            budget_chars=50,
            cut_steps=(),
            capped_sections=(
                rb.CappedSection(name="capped", paths=("env.tag_notes",), cap_chars=100_000, fold=None),
            ),
        )
        out = rb.apply_budget(response, policy)
        assert out["truncated"]["over_budget"] is True


class TestNonDefaultCoveragePath:
    def test_coverage_numerator_rewritten_at_nested_coverage_path(self):
        response = {
            "env": {"coverage": {"b": "5/9"}},
            "list_b": ["x" * 20] * 5,
        }
        policy = _policy(budget_chars=30, coverage_path="env.coverage")
        out = rb.apply_budget(response, policy)
        assert out["env"]["coverage"]["b"].endswith("/9")
        numerator = int(out["env"]["coverage"]["b"].split("/")[0])
        assert numerator == len(out["list_b"])
        assert numerator < 5

    def test_falsification_default_coverage_path_does_not_reach_nested_coverage(self):
        """coverage_pathを明示せず既定値"coverage"のままだと、env.coverage配下は
        見つからずnumeratorが書き換わらない（coverage_pathの指定が実際に
        効いていることの裏取り。list_bの切り詰め自体はcoverage解決と独立に起きる）。"""
        response = {
            "env": {"coverage": {"b": "5/9"}},
            "list_b": ["x" * 20] * 5,
        }
        policy = _policy(budget_chars=30)  # coverage_pathは既定値"coverage"のまま
        out = rb.apply_budget(response, policy)
        assert out["env"]["coverage"]["b"] == "5/9"
        assert len(out["list_b"]) < 5


class TestNonDefaultActivityPath:
    def test_hard_max_cuts_description_at_nested_activity_path(self):
        response = {"anchor": {"activity": {"description": "d" * 40000}}, "list_a": []}
        policy = _policy(budget_chars=100, hard_max_chars=1000, activity_path="anchor.activity")
        out = rb.apply_budget(response, policy)
        assert len(out["anchor"]["activity"]["description"]) < 40000
        assert out["anchor"]["activity"]["description_truncated"] is True
        assert out["truncated"]["hard_max"] is True

    def test_falsification_default_activity_path_does_not_reach_nested_activity(self):
        """activity_pathを明示せず既定値"activity"のままだと、anchor.activity配下は
        見つからずhard_maxが発動しない（activity_pathの指定が実際に効いている
        ことの裏取り）。"""
        response = {"anchor": {"activity": {"description": "d" * 40000}}, "list_a": []}
        policy = _policy(budget_chars=100, hard_max_chars=1000)  # activity_pathは既定値のまま
        out = rb.apply_budget(response, policy)
        assert "hard_max" not in out.get("truncated", {})
        assert len(out["anchor"]["activity"]["description"]) == 40000
