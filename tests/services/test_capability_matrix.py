"""capability_matrix の unit test。

CAPABILITY_MATRIX が想定通り (orch/dispatcher/worker × 全 tool) を網羅していること、
hidden_tools_for / is_allowed の判定が matrix の値と一致することを検証する。
"""
import pytest

from src.services.capability_matrix import (
    CAPABILITY_MATRIX,
    hidden_tools_for,
    is_allowed,
)


class TestMatrixCoverage:
    """matrix が想定 role を全行で扱っていることを保証する。"""

    @pytest.mark.parametrize("role", ["orch", "dispatcher", "worker", "user"])
    def test_every_tool_has_decision_for_role(self, role):
        for tool_name, matrix_row in CAPABILITY_MATRIX.items():
            assert role in matrix_row, (
                f"tool {tool_name!r} has no entry for role {role!r}"
            )

    def test_no_unknown_decision_values(self):
        for tool_name, matrix_row in CAPABILITY_MATRIX.items():
            for role, decision in matrix_row.items():
                assert decision in (True, False, "self"), (
                    f"unexpected decision {decision!r} for {tool_name}/{role}"
                )


class TestHiddenToolsFor:
    def test_orch_hides_dispatcher_only_tools(self):
        hidden = hidden_tools_for("orch")
        assert "ow_spawn_worker" in hidden
        assert "ow_recover" in hidden
        assert "ow_close_worker" in hidden  # decision=False for orch

    def test_worker_hides_write_admin_tools(self):
        hidden = hidden_tools_for("worker")
        assert "add_topic" in hidden
        assert "add_decisions" in hidden
        assert "add_relation" in hidden
        assert "update_activity" in hidden
        assert "retract" in hidden

    def test_dispatcher_hides_almost_all_write_tools(self):
        hidden = hidden_tools_for("dispatcher")
        assert "add_topic" in hidden
        assert "add_decisions" in hidden
        assert "add_material" in hidden
        assert "update_activity" in hidden

    def test_self_tools_are_not_hidden(self):
        hidden_worker = hidden_tools_for("worker")
        assert "ow_close_worker" not in hidden_worker
        assert "update_material" not in hidden_worker

    def test_unknown_role_hides_everything(self):
        hidden = hidden_tools_for("nobody")
        assert hidden == set(CAPABILITY_MATRIX.keys())

    def test_user_role_hides_everything(self):
        # user role は MCP tool 呼び出し主体にならない想定で、matrix 上は
        # 全 tool が False。hidden_tools_for は全 tool を返す。
        hidden = hidden_tools_for("user")
        assert hidden == set(CAPABILITY_MATRIX.keys())

    def test_read_tools_visible_to_all(self):
        for role in ("orch", "dispatcher", "worker"):
            hidden = hidden_tools_for(role)
            assert "search" not in hidden
            assert "get_decisions" not in hidden
            assert "check_in" not in hidden


class TestIsAllowed:
    def test_orch_allowed_for_writes(self):
        assert is_allowed("add_decisions", "orch") is True
        assert is_allowed("add_topic", "orch") is True

    def test_worker_denied_for_admin_writes(self):
        assert is_allowed("add_decisions", "worker") is False
        assert is_allowed("update_activity", "worker") is False

    def test_self_tools_return_self_token(self):
        assert is_allowed("ow_close_worker", "worker") == "self"
        assert is_allowed("update_material", "worker") == "self"

    def test_unknown_tool_default_deny(self):
        assert is_allowed("nonexistent_tool", "orch") is False

    def test_unknown_role_default_deny(self):
        assert is_allowed("search", "nobody") is False

    def test_user_role_default_deny(self):
        # user role は matrix 上では全 tool に対して False。
        assert is_allowed("search", "user") is False
        assert is_allowed("add_decisions", "user") is False
