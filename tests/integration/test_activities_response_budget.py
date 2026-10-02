"""get_activitiesへの全体予算適用の統合テスト。

check_inのtests/integration/test_checkin_response_budget.pyと同じ構造
（src.mainのツール関数を実DB経由で呼び、truncatedの有無・中身を検証する）。
"""
from src.config import ACTIVITIES_BUDGET_CHARS
from src.main import get_activities as tool_get_activities
from src.services.activity_service import ACTIVITY_DESC_MAX_LEN, add_activity

DEFAULT_TAGS = ["domain:test"]


def _make_pending_activities(n: int, desc_len: int = ACTIVITY_DESC_MAX_LEN) -> None:
    for i in range(n):
        add_activity(
            title=f"Activity {i}",
            description="d" * desc_len,
            tags=DEFAULT_TAGS,
            check_in=False,
        )


class TestActivitiesBudget:
    def test_large_result_gets_truncated(self, temp_db):
        """応答全体がACTIVITIES_BUDGET_CHARSを超えるとactivitiesが後方から切られ、
        truncated（budget/before/after/cuts）が付く。total_countは母集団件数のまま。"""
        _make_pending_activities(60)

        result = tool_get_activities(status="pending", limit=60)

        assert "error" not in result
        assert "truncated" in result
        assert result["truncated"]["budget"] == ACTIVITIES_BUDGET_CHARS
        assert result["truncated"]["before"] > ACTIVITIES_BUDGET_CHARS
        assert result["truncated"]["after"] <= ACTIVITIES_BUDGET_CHARS
        assert result["truncated"]["over_budget"] is False

        cuts = result["truncated"]["cuts"]
        cut = next(c for c in cuts if c["section"] == "activities")
        assert cut["cut"] > 0
        assert cut["kept"] == len(result["activities"])
        assert cut["next"] == [
            {"hint": "tags/status/since/untilで対象を絞るか、limitを下げて再実行してください"}
        ]

        assert len(result["activities"]) < 60
        assert result["total_count"] == 60

    def test_small_result_not_truncated(self, temp_db):
        """予算未満なら従来どおりtruncatedキーは付かず、全件そのまま返る。"""
        _make_pending_activities(2)

        result = tool_get_activities(status="pending", limit=5)

        assert "error" not in result
        assert "truncated" not in result
        assert len(result["activities"]) == 2
        assert result["total_count"] == 2
