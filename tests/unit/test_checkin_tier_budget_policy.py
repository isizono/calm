"""checkin_tier_service.TIER_FORM_BUDGET_POLICY（tier形の全体予算方針）の単体テスト。

response_budget.py自体の汎用契約（削る手順・pinnedの縮小・capped_sections・
ハード上限）はtests/unit/test_response_budget.pyが合成policyで担保する。
ここではresponse_budgetに足したドット区切りパス対応が、実際のtier形方針
（anchor.pinned・env.tag_notes・env.coverage・anchor.activity等の入れ子）に
対して正しく機能することを、DBに触れずに検証する。

フィクスチャは実際の収集経路が作りうる形に合わせる（例: context.decisionsは
DECISIONS_FULL_LIMIT件まで、tag_notesは1タグあたり_TAG_NOTES_RATCHET_CEILING
字まで）。到達不能な形（例: 40件のcontrol.dependenciesが素のリストのまま）は
使わない。
"""
from src.services import checkin_tier_service as cts
from src.services import response_budget as rb
from src.services.checkin_service import DECISIONS_FULL_LIMIT
from src.services.tag_service import _decay_pointer_text, _TAG_NOTES_RATCHET_CEILING
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


def _long_decisions(count: int, body_chars: int) -> list[dict]:
    """titleを省略した場合の実際の形: decision本文がそのままtitleに入る
    （decision.titleは35字上限だがdecision本文自体には上限が無いため、長い
    本文がtitleフィールドに入ること自体は到達可能）。
    """
    return [{"id_raw": i, "title": "決定本文" + "z" * body_chars} for i in range(count)]


class TestTagNotesExcludedFromMainBudget:
    def test_tag_notes_under_cap_are_excluded_from_the_main_budget(self):
        """env.tag_notesは天井(6,000字)未満で畳まれないが、それでも全体予算
        (10,000字)には数えない。「envを含めた生の総字数は10,000字を超えるが、
        tag_notes抜きなら10,000字未満」という状況を作り、cutsが一切走らない
        ことを確認する（tag_notesが正しく除外されていないと、この場合は
        context.decisionsが削られてしまう）。
        """
        response = _base_response()
        # 1タグの上限(4,000字)未満に収め、天井(6,000字)超過によるfoldは起きない量にする
        response["env"]["tag_notes"] = [
            {"tag": "domain:huge", "notes": "x" * (_TAG_NOTES_RATCHET_CEILING - 100)}
        ]
        response["context"] = {"decisions": _long_decisions(12, 500)}

        without_tag_notes = {"env": {k: v for k, v in response["env"].items() if k != "tag_notes"}}
        without_tag_notes.update({k: v for k, v in response.items() if k != "env"})
        other_only = rb.measure_chars(without_tag_notes)
        raw_total = rb.measure_chars(response)
        assert other_only < CHECKIN_BUDGET_CHARS, "前提が崩れている: tag_notes抜きでも予算超過"
        assert raw_total > CHECKIN_BUDGET_CHARS, "前提が崩れている: tag_notes込みでも予算内に収まっている"

        out = rb.apply_budget(response, cts.TIER_FORM_BUDGET_POLICY)

        assert out["context"]["decisions"] == response["context"]["decisions"]
        assert "truncated" not in out

    def test_over_cap_tag_notes_are_folded_to_decay_pointer(self):
        response = _base_response()
        # 2タグとも上限(4,000字)未満だが、合計は天井(6,000字)を超える
        response["env"]["tag_notes"] = [
            {"tag": "domain:huge-a", "notes": "x" * (_TAG_NOTES_RATCHET_CEILING - 100)},
            {"tag": "domain:huge-b", "notes": "y" * (_TAG_NOTES_RATCHET_CEILING - 100)},
        ]

        out = rb.apply_budget(response, cts.TIER_FORM_BUDGET_POLICY)

        notes = out["env"]["tag_notes"]
        assert rb.measure_chars(notes) <= CHECKIN_TAG_NOTES_CAP_CHARS
        # 少なくとも1件はdecayと同じ1行ポインタへ縮退している
        assert any(n["notes"] == _decay_pointer_text(n["tag"]) for n in notes)

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
        askはASKS_MAX(5件)以内・answer_body無しの素朴な形にし、question本文の
        長さだけで天井を超えさせる（question自体に字数上限は無い）。
        """
        response = _base_response()
        response["control"]["asks"] = {
            "awaiting_answer": [
                {"id_raw": i, "question": "なぜこの実装が必要か" + "z" * 600, "last_seen_at": "2026-01-01"}
                for i in range(5)
            ],
            "awaiting_triage": [],
        }
        assert rb.measure_chars(response["control"]) > CHECKIN_CONTROL_CAP_CHARS
        original_asks = response["control"]["asks"]

        out = rb.apply_budget(response, cts.TIER_FORM_BUDGET_POLICY)

        assert out["truncated"]["control_over"] is True
        assert out["control"]["asks"] == original_asks


class TestNestedCutSteps:
    def test_context_decisions_cut_reports_dotted_section_and_rewrites_coverage(self):
        response = _base_response()
        response["env"]["coverage"]["decisions"] = f"{DECISIONS_FULL_LIMIT}/{DECISIONS_FULL_LIMIT}"
        response["context"] = {"decisions": _long_decisions(DECISIONS_FULL_LIMIT, 700)}
        assert rb.measure_chars(response) > CHECKIN_BUDGET_CHARS

        out = rb.apply_budget(response, cts.TIER_FORM_BUDGET_POLICY)

        sections = {c["section"] for c in out["truncated"]["cuts"]}
        assert "context.decisions" in sections
        kept = len(out["context"]["decisions"])
        assert kept < DECISIONS_FULL_LIMIT
        assert out["env"]["coverage"]["decisions"] == f"{kept}/{DECISIONS_FULL_LIMIT}"

    def test_catalog_map_cut_before_context_decisions(self):
        """削る順番どおり、catalog.mapがcontext.decisionsより先に削られる。

        catalog.map(実際の上限30件、隣接activity/topic/material由来の短い
        title)を予算超過ぎりぎりまで積み、context.decisionsは手を付けられずに
        残ることを確認する。
        """
        response = _base_response()
        # catalog.mapの実際の形: type/id_raw/title/tags/depth。titleは
        # activity(35字)・material(40字)・topic相当の短い文字列に留める
        response["catalog"] = {
            "map": [
                {
                    "type": "activity", "id_raw": i,
                    "title": f"隣接アクティビティ{i:02d}の作業内容確認",
                    "tags": ["domain:test"], "depth": 1,
                }
                for i in range(30)
            ],
        }
        # context.decisionsは予算内ぎりぎりに収まる量にしておき、catalog.mapの
        # 追加分だけで超過させる（件数はDECISIONS_FULL_LIMIT以内で到達可能）
        response["context"] = {"decisions": _long_decisions(DECISIONS_FULL_LIMIT, 600)}
        base_without_map = rb.measure_chars({k: v for k, v in response.items() if k != "catalog"})
        assert base_without_map < CHECKIN_BUDGET_CHARS, "前提が崩れている: catalog.map抜きで既に予算超過"
        assert rb.measure_chars(response) > CHECKIN_BUDGET_CHARS, "前提が崩れている: catalog.mapを足しても予算内"

        out = rb.apply_budget(response, cts.TIER_FORM_BUDGET_POLICY)

        sections = [c["section"] for c in out["truncated"]["cuts"]]
        assert "catalog.map" in sections
        assert "context.decisions" not in sections
        assert out["context"]["decisions"] == response["context"]["decisions"]


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
        response["context"] = {"decisions": _long_decisions(DECISIONS_FULL_LIMIT, 700)}

        out = rb.apply_budget(response, cts.TIER_FORM_BUDGET_POLICY)

        assert out["truncated"].get("cuts"), "前提が崩れている: 削る手順が一度も走っていない"
        assert out["env"]["hints"] == ["recomposeをおすすめします"]
