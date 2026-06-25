"""capability matrix の全セル table-driven test と整合性検証。

CAPABILITY_MATRIX に登録された全 (role, tool) ペアに対し check_capability の
判定が matrix と一致することを確認する。また main.py で登録された全 @mcp.tool()
が matrix に登録されていることを確認し、新規 tool 追加時の matrix 登録忘れを
検出する。
"""
import asyncio
import functools

import pytest

from src.services import capability_matrix
from src.services.capability_matrix import CAPABILITY_MATRIX
from src.services.guard_service import CapabilityError, check_capability


@functools.lru_cache(maxsize=1)
def _fetch_mcp_tool_names() -> frozenset[str]:
    """mcp.list_tools() を 1 回だけ実行し tool 名を frozenset で返す。

    asyncio.run の event loop 生成・破棄を複数テストで重複させないため、
    test_literal_type_validation.py と同じ lru_cache パターンに揃える。
    """
    from src.main import mcp

    async def _list():
        return await mcp.list_tools()

    return frozenset(t.name for t in asyncio.run(_list()))


def _flatten_matrix() -> list[tuple[str, str, object]]:
    """(tool_name, role, expected_decision) の全セルを返す。"""
    cells: list[tuple[str, str, object]] = []
    for tool_name, row in CAPABILITY_MATRIX.items():
        for role, decision in row.items():
            cells.append((tool_name, role, decision))
    return cells


@pytest.mark.parametrize(
    "tool_name,role,expected", _flatten_matrix(),
    ids=lambda v: str(v),
)
def test_check_capability_matches_matrix(
    tool_name, role, expected, temp_db, monkeypatch
):
    """matrix の各セルが check_capability の挙動と一致する。"""
    monkeypatch.setenv("OW_ROLE", role)

    if expected is True:
        check_capability(tool_name)
        return

    if expected == "self":
        # self decision は self-target 判定が走り、引数なしでは reject される。
        with pytest.raises(CapabilityError):
            check_capability(tool_name, args={})
        return

    # expected is False: 通常の deny
    with pytest.raises(CapabilityError):
        check_capability(tool_name)


def test_all_mcp_tools_registered_in_matrix():
    """main.py で @mcp.tool() 登録された全 tool が capability_matrix に登録されている。

    新規 tool 追加時の matrix 登録忘れを検出する。
    """
    registered_names = _fetch_mcp_tool_names()
    matrix_names = set(CAPABILITY_MATRIX.keys())
    missing = registered_names - matrix_names
    assert not missing, f"capability_matrix.py に未登録の tool: {missing}"


def test_matrix_does_not_have_extra_tools():
    """matrix に登録されているが mcp に登録されていない tool が無いことを確認する。

    ow_* tool は matrix に含まれるが main.py の mcp には現状登録されていないため
    例外パターンとして許容する。
    """
    registered_names = _fetch_mcp_tool_names()
    matrix_names = set(CAPABILITY_MATRIX.keys())
    extra = matrix_names - registered_names
    ow_only = {
        name for name in extra if name.startswith("ow_")
    }
    unexpected = extra - ow_only
    assert not unexpected, (
        f"matrix にあるが mcp に無い tool (ow_* 以外で意図せず残存): {unexpected}"
    )


class TestEscalationAcrossRoles:
    """OW_ESCALATION=1 は role × tool の deny を一律で通過させる (orch_proxy 経路)。"""

    @pytest.mark.parametrize("role", ["orch", "dispatcher", "worker"])
    def test_escalation_unblocks_denied_tool(self, role, temp_db, monkeypatch):
        monkeypatch.setenv("OW_ROLE", role)
        monkeypatch.setenv("OW_ESCALATION", "1")
        # 各 role で確実に deny される tool を 1 つ選んで通過するか確認
        denied_tools = [
            tool
            for tool, row in CAPABILITY_MATRIX.items()
            if row.get(role) is False
        ]
        if not denied_tools:
            pytest.skip(
                f"role={role} に False セルが存在しないためエスカレーションテスト不可"
            )
        check_capability(denied_tools[0])


class TestUserRoleAbsence:
    """matrix に未登録の role (例: "user") はデフォルト deny。"""

    def test_unknown_role_denies_all(self, temp_db, monkeypatch):
        monkeypatch.setenv("OW_ROLE", "user")
        with pytest.raises(CapabilityError):
            check_capability("add_decisions")

    def test_unknown_role_hidden_set_is_full(self):
        hidden = capability_matrix.hidden_tools_for("user")
        # "user" は matrix に登録されてないので全 tool が hidden
        assert hidden == set(CAPABILITY_MATRIX.keys())
