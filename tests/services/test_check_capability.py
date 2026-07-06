"""check_capability の挙動を検証する unit test。

role/matrix/escalation/self-target/grace period の各分岐を網羅する。
DB fixture は conftest の temp_db を使う。
"""
import pytest

from src.db import get_connection
from src.services import guard_service
from src.services.guard_service import (
    CapabilityError,
    WorkerGuardError,
    check_capability,
)


def _register(session_id: str, role: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO session_identity (session_id, role) VALUES (?, ?)",
            (session_id, role),
        )
        conn.commit()
    finally:
        conn.close()


class TestRolePassThrough:
    """非 ow セッション (role None) は通過する。"""

    def test_no_role_passes_through(self, temp_db, monkeypatch):
        check_capability("add_decisions")


class TestEnvFallback:
    """env OW_ROLE 経由で role が解決され matrix が適用される。"""

    def test_env_worker_rejects_admin_write(self, temp_db, monkeypatch):
        monkeypatch.setenv("OW_ROLE", "worker")
        with pytest.raises(CapabilityError):
            check_capability("add_decisions")

    def test_env_orch_allowed_for_admin_write(self, temp_db, monkeypatch):
        monkeypatch.setenv("OW_ROLE", "orch")
        check_capability("add_decisions")

    def test_env_dispatcher_rejected_for_admin_write(self, temp_db, monkeypatch):
        monkeypatch.setenv("OW_ROLE", "dispatcher")
        with pytest.raises(CapabilityError):
            check_capability("add_decisions")


class TestEscalationPassValve:
    """OW_ESCALATION=1 のとき role 違反でも通過する。"""

    def test_escalation_overrides_worker_block(self, temp_db, monkeypatch):
        monkeypatch.setenv("OW_ROLE", "worker")
        monkeypatch.setenv("OW_ESCALATION", "1")
        check_capability("add_decisions")


class TestMatrixDenial:
    """matrix の False / 未登録 tool は CapabilityError を返す。"""

    def test_worker_blocked_from_update_activity(self, temp_db, monkeypatch):
        monkeypatch.setenv("OW_ROLE", "worker")
        with pytest.raises(CapabilityError):
            check_capability("update_activity")

    def test_unknown_tool_default_deny(self, temp_db, monkeypatch):
        monkeypatch.setenv("OW_ROLE", "orch")
        with pytest.raises(CapabilityError):
            check_capability("nonexistent_tool")


class TestSelfTargetUpdateMaterial:
    """update_material の self 判定。"""

    def test_self_owned_material_passes(self, temp_db, monkeypatch):
        monkeypatch.setenv("OW_ROLE", "worker")
        conn = get_connection()
        try:
            cursor = conn.execute(
                "INSERT INTO materials (title, content, source, caller_session_id) "
                "VALUES (?, ?, ?, ?)",
                ("t", "c", "s", "sess-w1"),
            )
            material_id = cursor.lastrowid
            conn.commit()
        finally:
            conn.close()

        with monkeypatch.context() as m:
            m.setattr(
                "src.services.role_service.get_caller_session_id",
                lambda: "sess-w1",
            )
            check_capability("update_material", args={"material_id": material_id})

    def test_others_material_rejected(self, temp_db, monkeypatch):
        monkeypatch.setenv("OW_ROLE", "worker")
        conn = get_connection()
        try:
            cursor = conn.execute(
                "INSERT INTO materials (title, content, source, caller_session_id) "
                "VALUES (?, ?, ?, ?)",
                ("t", "c", "s", "sess-other"),
            )
            material_id = cursor.lastrowid
            conn.commit()
        finally:
            conn.close()

        with monkeypatch.context() as m:
            m.setattr(
                "src.services.role_service.get_caller_session_id",
                lambda: "sess-w1",
            )
            with pytest.raises(CapabilityError) as exc:
                check_capability(
                    "update_material", args={"material_id": material_id}
                )
            assert "self-target" in str(exc.value)

    def test_orch_can_update_anyones_material(self, temp_db, monkeypatch):
        monkeypatch.setenv("OW_ROLE", "orch")
        conn = get_connection()
        try:
            cursor = conn.execute(
                "INSERT INTO materials (title, content, source, caller_session_id) "
                "VALUES (?, ?, ?, ?)",
                ("t", "c", "s", "sess-other"),
            )
            material_id = cursor.lastrowid
            conn.commit()
        finally:
            conn.close()

        check_capability("update_material", args={"material_id": material_id})


class TestWorkerGuardWrapper:
    """check_worker_guard 旧 API の wrapper 動作 (後方互換)。"""

    def test_worker_env_blocks(self, temp_db, monkeypatch):
        monkeypatch.setenv("OW_ROLE", "worker")
        with pytest.raises(WorkerGuardError):
            guard_service.check_worker_guard("add_topic")

    def test_escalation_passes(self, temp_db, monkeypatch):
        monkeypatch.setenv("OW_ROLE", "worker")
        monkeypatch.setenv("OW_ESCALATION", "1")
        guard_service.check_worker_guard("add_topic")

    def test_orch_env_passes(self, temp_db, monkeypatch):
        monkeypatch.setenv("OW_ROLE", "orch")
        guard_service.check_worker_guard("add_topic")


class TestIsWorkerSession:
    """is_worker_session が lookup_role 経由 (DB) + env fallback で動く。"""

    def test_db_role_worker_returns_true(self, temp_db, monkeypatch):
        _register("sess-w1", "worker")
        with monkeypatch.context() as m:
            m.setattr(
                "src.services.role_service.get_caller_session_id",
                lambda: "sess-w1",
            )
            assert guard_service.is_worker_session() is True

    def test_db_role_orch_returns_false(self, temp_db, monkeypatch):
        _register("sess-o1", "orch")
        with monkeypatch.context() as m:
            m.setattr(
                "src.services.role_service.get_caller_session_id",
                lambda: "sess-o1",
            )
            assert guard_service.is_worker_session() is False

    def test_env_worker_returns_true(self, temp_db, monkeypatch):
        monkeypatch.setenv("OW_ROLE", "worker")
        assert guard_service.is_worker_session() is True

    def test_no_role_returns_false(self, temp_db):
        assert guard_service.is_worker_session() is False
