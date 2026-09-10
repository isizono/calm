"""get_overview MCPツールの配線テスト。

境界値・COALESCEの落とし穴回帰検出・orch_managedを除外しないこと
(TestOrchManagedNotFiltered)等のロジック本体は
tests/unit/test_overview_service.py が持つ。ここではMCPツール層が
overview_service.get_overview へ引数をそのまま渡し、戻り値をそのまま
返すことだけを検証する。
"""
from src.main import get_overview
from src.services.activity_service import add_activity, update_activity


def _make_activity(title: str = "a1", status: str | None = None) -> int:
    activity_id = add_activity(
        title=title, description="d", tags=["domain:test"], check_in=False
    )["activity_id"]
    if status is not None:
        update_activity(activity_id, status=status)
    return activity_id


def test_get_overview_returns_four_sections(temp_db):
    _make_activity(status="in_progress")

    result = get_overview()

    assert "error" not in result
    assert set(result.keys()) == {
        "generated_at", "params", "working", "recently_done", "awaiting_human", "backlog",
    }


def test_get_overview_passes_days_and_limit_through_to_service(temp_db):
    for i in range(3):
        _make_activity(title=f"w{i}", status="in_progress")

    result = get_overview(days=1, limit=2)

    assert result["params"]["days"] == 1
    assert result["params"]["limit"] == 2
    assert len(result["working"]["items"]) == 2
    assert result["working"]["total_count"] == 3


def test_get_overview_surfaces_service_validation_error(temp_db):
    result = get_overview(limit=0)

    assert result["error"]["code"] == "INVALID_PARAMETER"
