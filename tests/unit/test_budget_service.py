"""budget_service のユニットテスト。

共通スコア関数（importance / recency / relevance / size_penalty）の境界値と、
precedent_pull_service から移設した allocate_decision_budget の挙動不変性
（移設前の _allocate_budget と同じ配分結果になること）を検証する。
"""
import math

import pytest

from src.config import PRECEDENT_BUDGET_CHARS, RECENCY_DECAY_FLOOR, RECENCY_DECAY_RATE
from src.services import budget_service


class TestBudgetDefaults:
    def test_budget_defaults_sourced_from_config(self):
        """BUDGET_DEFAULTSの値はsrc.configの値と一致する（ハードコードしていない）"""
        assert budget_service.BUDGET_DEFAULTS["precedent_budget_chars"] == PRECEDENT_BUDGET_CHARS
        assert budget_service.BUDGET_DEFAULTS["recency_decay_rate"] == RECENCY_DECAY_RATE
        assert budget_service.BUDGET_DEFAULTS["recency_decay_floor"] == RECENCY_DECAY_FLOOR


class TestImportanceScore:
    def test_pinned_returns_weight(self):
        assert budget_service.importance_score(is_pinned=True, weight=0.7) == 0.7

    def test_not_pinned_returns_zero(self):
        assert budget_service.importance_score(is_pinned=False) == 0.0

    def test_default_weight_is_one(self):
        assert budget_service.importance_score(is_pinned=True) == 1.0


class TestRecencyScore:
    def test_zero_age_is_near_one(self):
        assert budget_service.recency_score(0) == math.exp(0)

    def test_matches_search_service_decay_formula(self):
        age_days = 30
        expected = max(math.exp(-age_days * RECENCY_DECAY_RATE), RECENCY_DECAY_FLOOR)
        assert budget_service.recency_score(age_days) == expected

    def test_never_below_floor(self):
        assert budget_service.recency_score(10_000) == RECENCY_DECAY_FLOOR


class TestRelevanceScore:
    def test_rank_zero_is_max(self):
        assert budget_service.relevance_score(0, 10) == 1.0

    def test_last_rank_is_near_zero(self):
        assert budget_service.relevance_score(9, 10) == pytest.approx(0.1)

    def test_zero_total_returns_zero(self):
        assert budget_service.relevance_score(0, 0) == 0.0

    def test_never_negative(self):
        assert budget_service.relevance_score(20, 10) == 0.0


class TestSizePenalty:
    def test_within_budget_scales_linearly(self):
        assert budget_service.size_penalty(500, 1000) == 0.5

    def test_over_budget_caps_at_one(self):
        assert budget_service.size_penalty(2000, 1000) == 1.0

    def test_zero_budget_is_max_penalty(self):
        assert budget_service.size_penalty(100, 0) == 1.0


class TestAllocateDecisionBudget:
    """precedent_pull_service._allocate_budget の移設前後で挙動が変わっていないことを検証する。

    決定的な配分順（非superseded→新しい順 → superseded→新しい順）とcost計算
    （decision + reason の文字数合計）をここで直接固定する。
    """

    def _dec(self, did, decision, reason, created_at):
        return {"id": did, "decision": decision, "reason": reason, "created_at": created_at}

    def test_sufficient_budget_all_full(self):
        decision_by_id = {
            1: self._dec(1, "d1", "r" * 10, "2026-01-01"),
            2: self._dec(2, "d2", "r" * 10, "2026-01-02"),
        }
        full_ids, used = budget_service.allocate_decision_budget(
            [1, 2], decision_by_id, supersede_map={}, budget_chars=100_000
        )
        assert full_ids == {1, 2}
        assert used == sum(len(d["decision"]) + len(d["reason"]) for d in decision_by_id.values())

    def test_insufficient_budget_stops_allocating(self):
        # 各itemのcostは1001文字（"d" + "x"*1000）。budget_chars=2500だと2件（2002文字）
        # までは収まるが3件目（3003文字）で溢れて打ち切られる
        decision_by_id = {
            1: self._dec(1, "d", "x" * 1000, "2026-01-01"),
            2: self._dec(2, "d", "x" * 1000, "2026-01-02"),
            3: self._dec(3, "d", "x" * 1000, "2026-01-03"),
        }
        full_ids, used = budget_service.allocate_decision_budget(
            [1, 2, 3], decision_by_id, supersede_map={}, budget_chars=2500
        )
        assert len(full_ids) == 2
        assert used <= 2500

    def test_non_superseded_promoted_before_superseded(self):
        decision_by_id = {
            1: self._dec(1, "old", "x" * 500, "2026-01-01"),
            2: self._dec(2, "new", "x" * 500, "2026-01-02"),
        }
        supersede_map = {1: {"is_superseded": True}, 2: {"is_superseded": False}}
        full_ids, used = budget_service.allocate_decision_budget(
            [1, 2], decision_by_id, supersede_map, budget_chars=520
        )
        assert full_ids == {2}

    def test_newer_promoted_before_older_within_same_supersede_state(self):
        decision_by_id = {
            1: self._dec(1, "d", "x" * 500, "2026-01-01"),
            2: self._dec(2, "d", "x" * 500, "2026-01-02"),
        }
        full_ids, used = budget_service.allocate_decision_budget(
            [1, 2], decision_by_id, supersede_map={}, budget_chars=520
        )
        assert full_ids == {2}

    def test_allocation_is_deterministic(self):
        decision_by_id = {
            i: self._dec(i, "d", "x" * 800, "2026-01-01") for i in range(1, 7)
        }
        ids = list(decision_by_id.keys())
        result1, _ = budget_service.allocate_decision_budget(ids, decision_by_id, {}, 2500)
        result2, _ = budget_service.allocate_decision_budget(ids, decision_by_id, {}, 2500)
        assert result1 == result2
