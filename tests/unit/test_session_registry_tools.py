"""get_sessions / set_session_alias MCPツール（main.py配線）のユニットテスト。

session_registry_service自体のalias生成・衝突解決・GCロジックは
tests/unit/test_session_registry.py が担う。本ファイルはmain.py側の配線
（呼び出し元識別子の取得 → service呼び出し → レスポンス整形）を、実際の
session_registry_serviceを通して検証する。CLI session解決だけを外部境界として
mockする。
"""
import datetime as dt

import pytest

import src.main as main_module
from src.services import session_registry_service as srs
from tests.helpers import all_tool_descriptions


RELAY_IDENTITY = main_module.relay_identity


@pytest.fixture
def registry_path(tmp_path, monkeypatch):
    path = tmp_path / "session_aliases.json"
    monkeypatch.setenv(srs.REGISTRY_PATH_ENV, str(path))
    return path


def _stub_world(monkeypatch, sessions: dict[str, dict]):
    """sessions: {bridge_session_id: {"cli_pid", "cli_session_id", "name"}}"""

    def resolve(bridge_session_id):
        info = sessions.get(bridge_session_id)
        return dict(info, cwd=None, cli_status=None) if info else None

    def is_alive(pid):
        return any(info["cli_pid"] == pid for info in sessions.values())

    def read_cli(pid):
        for info in sessions.values():
            if info["cli_pid"] == pid:
                return dict(info, cwd=None, cli_status=None)
        return None

    monkeypatch.setattr(RELAY_IDENTITY, "resolve_cli_session", resolve)
    monkeypatch.setattr(srs, "is_process_alive", is_alive)
    monkeypatch.setattr(srs.cli_session, "read_cli_session", read_cli)


def _set_caller(monkeypatch, bridge_session_id):
    monkeypatch.setattr(RELAY_IDENTITY, "get_relay_identity", lambda: bridge_session_id)


def _sequential_timestamps(monkeypatch, count=10):
    """updated_atの大小比較に依存するテスト用の決定的なタイムスタンプ列。

    同一秒内に複数register_checkinが呼ばれるとタイムスタンプが衝突しうるため。
    """
    base = dt.datetime.now(dt.timezone.utc)
    values = iter(
        (base + dt.timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%SZ") for i in range(count)
    )
    monkeypatch.setattr(srs, "_now_iso", lambda: next(values))


class TestToolRegistration:
    def test_get_sessions_and_set_session_alias_are_exposed_via_mcp(self):
        descriptions = all_tool_descriptions()
        assert "get_sessions" in descriptions
        assert "set_session_alias" in descriptions


class TestGetSessions:
    def test_returns_descending_order_with_self_flag(self, registry_path, monkeypatch):
        _sequential_timestamps(monkeypatch)
        _stub_world(
            monkeypatch,
            {
                "bridge-a": {"cli_pid": 100, "cli_session_id": "cli-1", "name": "workspace-a1"},
                "bridge-b": {"cli_pid": 200, "cli_session_id": "cli-2", "name": "workspace-b1"},
            },
        )
        srs.register_checkin(
            bridge_session_id="bridge-a", activity_id=1, activity_title="First", activity_status="in_progress"
        )
        srs.register_checkin(
            bridge_session_id="bridge-b", activity_id=2, activity_title="Second", activity_status="in_progress"
        )
        _set_caller(monkeypatch, "bridge-b")

        result = main_module.get_sessions()

        assert result["count"] == 2
        assert [s["activity_title"] for s in result["sessions"]] == ["Second", "First"]
        assert result["sessions"][0]["is_self"] is True
        assert result["sessions"][1]["is_self"] is False

    def test_empty_registry_returns_empty_list_with_zero_count(self, registry_path, monkeypatch):
        _set_caller(monkeypatch, None)
        result = main_module.get_sessions()
        assert result == {"sessions": [], "count": 0}


class TestSetSessionAliasNotRegistered:
    def test_returns_not_registered_when_never_checked_in(self, registry_path, monkeypatch):
        _stub_world(
            monkeypatch,
            {"bridge-a": {"cli_pid": 100, "cli_session_id": "cli-1", "name": "workspace-a1"}},
        )
        _set_caller(monkeypatch, "bridge-a")

        result = main_module.set_session_alias("MyAlias")

        assert result["error"]["code"] == "NOT_REGISTERED"


class TestSetSessionAliasValidation:
    @pytest.mark.parametrize(
        "alias",
        ["a" * 25, "hello\nworld", "   "],
        ids=["over-24-chars", "embedded-newline", "whitespace-only"],
    )
    def test_invalid_alias_returns_validation_error(self, registry_path, monkeypatch, alias):
        _stub_world(
            monkeypatch,
            {"bridge-a": {"cli_pid": 100, "cli_session_id": "cli-1", "name": "workspace-a1"}},
        )
        srs.register_checkin(
            bridge_session_id="bridge-a", activity_id=1, activity_title="Foo", activity_status="in_progress"
        )
        _set_caller(monkeypatch, "bridge-a")

        result = main_module.set_session_alias(alias)

        assert result["error"]["code"] == "VALIDATION_ERROR"


class TestSetSessionAliasCollision:
    def test_collision_appends_suffix_and_preserves_requested_alias(self, registry_path, monkeypatch):
        _stub_world(
            monkeypatch,
            {
                "bridge-a": {"cli_pid": 100, "cli_session_id": "cli-1", "name": "workspace-a1"},
                "bridge-b": {"cli_pid": 200, "cli_session_id": "cli-2", "name": "workspace-b1"},
            },
        )
        srs.register_checkin(
            bridge_session_id="bridge-a", activity_id=1, activity_title="Foo", activity_status="in_progress"
        )
        srs.register_checkin(
            bridge_session_id="bridge-b", activity_id=2, activity_title="Bar", activity_status="in_progress"
        )
        _set_caller(monkeypatch, "bridge-b")

        result = main_module.set_session_alias("Foo")

        assert result["collided"] is True
        assert result["alias"] == "Foo-2"
        assert result["requested_alias"] == "Foo"
        assert result["name"] == "workspace-b1"

    def test_no_collision_returns_requested_alias_unchanged(self, registry_path, monkeypatch):
        _stub_world(
            monkeypatch,
            {"bridge-a": {"cli_pid": 100, "cli_session_id": "cli-1", "name": "workspace-a1"}},
        )
        srs.register_checkin(
            bridge_session_id="bridge-a", activity_id=1, activity_title="Foo", activity_status="in_progress"
        )
        _set_caller(monkeypatch, "bridge-a")

        result = main_module.set_session_alias("MyAlias")

        assert result["collided"] is False
        assert result["alias"] == "MyAlias"
        assert result["requested_alias"] == "MyAlias"


class TestSetSessionAliasUnresolved:
    def test_returns_session_unresolved_when_bridge_id_missing(self, registry_path, monkeypatch):
        _set_caller(monkeypatch, None)
        result = main_module.set_session_alias("MyAlias")
        assert result["error"]["code"] == "SESSION_UNRESOLVED"
