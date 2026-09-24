"""checkin_tier_service.TIER_FORM_BUDGET_POLICY（tier形の全体予算方針）の単体テスト。

response_budget.py自体の汎用契約（削る手順・pinnedの縮小・capped_sections・
ハード上限）はtests/unit/test_response_budget.pyが合成policyで担保する。
ここではresponse_budgetに足したドット区切りパス対応が、実際のtier形方針
（anchor.pinned・env.tag_notes・env.coverage・anchor.activity等の入れ子）に
対して正しく機能することを、DBに触れずに検証する。
"""
from src.services import checkin_tier_service as cts
from src.services import response_budget as rb
from src.config import (
    CHECKIN_BUDGET_CHARS,
    CHECKIN_CONTROL_CAP_CHARS,
    CHECKIN_TAG_NOTES_CAP_CHARS,
)


def _activity(desc: str = "d") -> dict:
    return {
        "id_raw": 1, "title": "T", "description": desc,
        "status": "in_progress", "tags": ["domain:test"],
    }


def _base_response(**overrides) -> dict:
    """anchor.activity/control.goal/env.coverage/env.sessionだけを持つ最小形。"""
    response = {
        "anchor": {"activity": _activity()},
        "control": {"goal": {"label": "undefined"}},
        "env": {
            "coverage": {"decisions": "0/0", "materials": "0/0", "logs": "0/0"},
            "session": {"registered": False, "reason": "cli_unresolved"},
        },
    }
    for key, value in overrides.items():
        response[key] = value
    return response


class TestTagNotesExcludedFromMainBudget:
    def test_huge_tag_notes_does_not_cut_other_sections(self):
        """env.tag_notesが全体予算(10,000字)を大きく超える量でも、他の枠は
        一切削られない（tag_notesは全体予算に数えない）。
        """
        response = _base_response()
        # 1件で天井(6,000字)を超えるnotesを3件、天井を大きく超えさせる
        response["env"]["tag_notes"] = [
            {"tag": f"domain:huge-{i}", "notes": "x" * 7000} for i in range(3)
        ]
        response["context"] = {"decisions": [{"id_raw": 9, "title": "生き残るはずの決定"}]}

        out = rb.apply_budget(response, cts.TIER_FORM_BUDGET_POLICY)

        assert out["context"]["decisions"] == [{"id_raw": 9, "title": "生き残るはずの決定"}]
        assert "truncated" in out
        assert out["truncated"].get("tag_notes_over") is True
        assert "cuts" not in out["truncated"]  # 削る手順は一度も走っていない

    def test_over_cap_tag_notes_are_folded_to_decay_pointer(self):
        response = _base_response()
        response["env"]["tag_notes"] = [
            {"tag": "domain:huge-a", "notes": "x" * 7000},
            {"tag": "domain:huge-b", "notes": "y" * 7000},
        ]

        out = rb.apply_budget(response, cts.TIER_FORM_BUDGET_POLICY)

        notes = out["env"]["tag_notes"]
        assert rb.measure_chars(notes) <= CHECKIN_TAG_NOTES_CAP_CHARS
        # 少なくとも1件はdecayと同じ1行ポインタへ縮退している
        assert any("全文表示を省略した" in n["notes"] for n in notes)

    def test_falsification_small_tag_notes_are_not_folded(self):
        response = _base_response()
        response["env"]["tag_notes"] = [{"tag": "domain:small", "notes": "短い教訓"}]

        out = rb.apply_budget(response, cts.TIER_FORM_BUDGET_POLICY)

        assert out["env"]["tag_notes"] == [{"tag": "domain:small", "notes": "短い教訓"}]
        assert "truncated" not in out


class TestControlExcludedFromMainBudgetAndNeverFolded:
    def test_huge_control_sets_flag_but_is_never_shrunk(self):
        """control(goal/asks/dependencies)が天井(3,000字)を超えても、中身は
        削らずtruncated.control_overを立てるだけ（畳む手段を持たない）。
        """
        response = _base_response()
        response["control"]["dependencies"] = [
            {"id": i, "title": "x" * 100, "status": "pending"} for i in range(40)
        ]
        assert rb.measure_chars(response["control"]) > CHECKIN_CONTROL_CAP_CHARS
        original_dependencies = list(response["control"]["dependencies"])

        out = rb.apply_budget(response, cts.TIER_FORM_BUDGET_POLICY)

        assert out["truncated"]["control_over"] is True
        assert out["control"]["dependencies"] == original_dependencies


class TestNestedCutSteps:
    def test_context_decisions_cut_reports_dotted_section_and_rewrites_coverage(self):
        response = _base_response()
        response["env"]["coverage"]["decisions"] = "20/20"
        response["context"] = {
            "decisions": [{"id_raw": i, "title": "決定" + "z" * 700} for i in range(20)],
        }
        assert rb.measure_chars(response) > CHECKIN_BUDGET_CHARS

        out = rb.apply_budget(response, cts.TIER_FORM_BUDGET_POLICY)

        sections = {c["section"] for c in out["truncated"]["cuts"]}
        assert "context.decisions" in sections
        kept = len(out["context"]["decisions"])
        assert kept < 20
        assert out["env"]["coverage"]["decisions"] == f"{kept}/20"

    def test_catalog_map_cut_before_context_decisions(self):
        """削る順番どおり、catalog.mapがcontext.decisionsより先に削られる。

        catalog.mapだけで予算超過を解消できる量にし、context.decisionsは
        手を付けられずに残ることを確認する。
        """
        response = _base_response()
        response["catalog"] = {
            "map": [{"id_raw": i, "type": "activity", "title": "隣接" + "z" * 700} for i in range(20)],
        }
        response["context"] = {
            "decisions": [{"id_raw": 1, "title": "残るはずの決定"}],
        }
        assert rb.measure_chars(response) > CHECKIN_BUDGET_CHARS

        out = rb.apply_budget(response, cts.TIER_FORM_BUDGET_POLICY)

        sections = [c["section"] for c in out["truncated"]["cuts"]]
        assert "catalog.map" in sections
        assert "context.decisions" not in sections
        assert out["context"]["decisions"] == [{"id_raw": 1, "title": "残るはずの決定"}]


class TestAnchorProtectedAndHardMax:
    def test_anchor_activity_description_untouched_until_hard_max(self):
        response = _base_response()
        response["anchor"]["activity"]["description"] = "d" * 40_000

        out = rb.apply_budget(response, cts.TIER_FORM_BUDGET_POLICY)

        assert out["truncated"]["hard_max"] is True
        assert len(out["anchor"]["activity"]["description"]) < 40_000
        assert out["anchor"]["activity"]["description_truncated"] is True
        assert out["anchor"]["activity"]["description_next"] == [
            {"tool": "get_by_ids", "args": {"items": [{"type": "activity", "id": 1}]}}
        ]

    def test_env_hints_survive_even_when_everything_else_is_exhausted(self):
        """V10: 予算を使い切ってもenv.hintsは削られない（protected_paths）。"""
        response = _base_response()
        response["env"]["hints"] = ["recomposeをおすすめします"]
        response["context"] = {
            "decisions": [{"id_raw": i, "title": "決定" + "z" * 400} for i in range(30)],
        }

        out = rb.apply_budget(response, cts.TIER_FORM_BUDGET_POLICY)

        assert out["env"]["hints"] == ["recomposeをおすすめします"]
