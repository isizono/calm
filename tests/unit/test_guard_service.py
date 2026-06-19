"""guard_service の単体テスト。

OW_ROLE=worker と OW_ESCALATION の組み合わせに対する判定・例外送出を検証する。

- OW_ROLE=worker 単独 → is_worker_session=True, check_worker_guard で raise
- OW_ROLE=worker かつ OW_ESCALATION=1 → check_worker_guard 通過
- OW_ROLE 未設定 → is_worker_session=False, check_worker_guard 通過
- OW_ROLE=orch → check_worker_guard 通過
- raise されるメッセージに tool_name と「recording skill」「orch」「OW_ESCALATION」案内が含まれる
"""
import pytest

from src.services import guard_service
from src.services.guard_service import (
    WorkerGuardError,
    check_worker_guard,
    is_escalation_mode,
    is_worker_session,
)


class TestIsWorkerSession:
    """is_worker_session() の OW_ROLE 判定。"""

    def test_role_worker_true(self, monkeypatch):
        """OW_ROLE=worker が立っているとき True を返す。"""
        monkeypatch.setenv("OW_ROLE", "worker")
        assert is_worker_session() is True

    def test_role_orch_false(self, monkeypatch):
        """OW_ROLE=orch のとき worker 扱いしない。"""
        monkeypatch.setenv("OW_ROLE", "orch")
        assert is_worker_session() is False

    def test_role_unset_false(self, monkeypatch):
        """OW_ROLE 未設定のとき worker 扱いしない (通常セッション)。"""
        monkeypatch.delenv("OW_ROLE", raising=False)
        assert is_worker_session() is False

    def test_role_empty_false(self, monkeypatch):
        """OW_ROLE が空文字のとき worker 扱いしない。"""
        monkeypatch.setenv("OW_ROLE", "")
        assert is_worker_session() is False

    def test_escalation_does_not_affect_role_check(self, monkeypatch):
        """OW_ESCALATION=1 でも OW_ROLE=worker なら is_worker_session は True。

        is_worker_session() は OW_ROLE だけを見る (役割の判定)。
        エスカレーションによる通過判定は check_worker_guard 側で行う。
        """
        monkeypatch.setenv("OW_ROLE", "worker")
        monkeypatch.setenv("OW_ESCALATION", "1")
        assert is_worker_session() is True


class TestIsEscalationMode:
    """is_escalation_mode() の OW_ESCALATION 判定。"""

    def test_escalation_one_true(self, monkeypatch):
        """OW_ESCALATION=1 のとき True を返す。"""
        monkeypatch.setenv("OW_ESCALATION", "1")
        assert is_escalation_mode() is True

    def test_escalation_zero_false(self, monkeypatch):
        """OW_ESCALATION=0 のときは通過させない。"""
        monkeypatch.setenv("OW_ESCALATION", "0")
        assert is_escalation_mode() is False

    def test_escalation_true_string_false(self, monkeypatch):
        """OW_ESCALATION='true' のような値も通過対象外 (厳格に '1' のみ)。"""
        monkeypatch.setenv("OW_ESCALATION", "true")
        assert is_escalation_mode() is False

    def test_escalation_unset_false(self, monkeypatch):
        """OW_ESCALATION 未設定のとき False を返す。"""
        monkeypatch.delenv("OW_ESCALATION", raising=False)
        assert is_escalation_mode() is False


class TestCheckWorkerGuard:
    """check_worker_guard() の通過・例外送出。"""

    def test_worker_without_escalation_raises(self, monkeypatch):
        """OW_ROLE=worker かつ OW_ESCALATION 未設定 → WorkerGuardError を raise。"""
        monkeypatch.setenv("OW_ROLE", "worker")
        monkeypatch.delenv("OW_ESCALATION", raising=False)
        with pytest.raises(WorkerGuardError):
            check_worker_guard("add_decisions")

    def test_worker_with_escalation_passes(self, monkeypatch):
        """OW_ROLE=worker かつ OW_ESCALATION=1 → 通過 (orch_proxy 経路)。"""
        monkeypatch.setenv("OW_ROLE", "worker")
        monkeypatch.setenv("OW_ESCALATION", "1")
        check_worker_guard("add_decisions")

    def test_orch_passes(self, monkeypatch):
        """OW_ROLE=orch → 通過。"""
        monkeypatch.setenv("OW_ROLE", "orch")
        monkeypatch.delenv("OW_ESCALATION", raising=False)
        check_worker_guard("add_decisions")

    def test_unset_passes(self, monkeypatch):
        """OW_ROLE 未設定 (通常セッション) → 通過。"""
        monkeypatch.delenv("OW_ROLE", raising=False)
        monkeypatch.delenv("OW_ESCALATION", raising=False)
        check_worker_guard("add_decisions")

    def test_worker_with_escalation_zero_raises(self, monkeypatch):
        """OW_ESCALATION=0 では通過しない (厳格に '1' のみ通過)。"""
        monkeypatch.setenv("OW_ROLE", "worker")
        monkeypatch.setenv("OW_ESCALATION", "0")
        with pytest.raises(WorkerGuardError):
            check_worker_guard("add_decisions")


class TestWorkerGuardErrorMessage:
    """WorkerGuardError のメッセージ内容を検証する。"""

    def test_message_includes_tool_name(self, monkeypatch):
        """raise されるメッセージに tool_name 引数が埋め込まれる。"""
        monkeypatch.setenv("OW_ROLE", "worker")
        with pytest.raises(WorkerGuardError) as exc_info:
            check_worker_guard("add_topic")
        assert "add_topic" in str(exc_info.value)

    def test_message_guides_to_orch(self, monkeypatch):
        """メッセージで orch 経由でユーザー合意を取って記録する方針を案内する。"""
        monkeypatch.setenv("OW_ROLE", "worker")
        with pytest.raises(WorkerGuardError) as exc_info:
            check_worker_guard("add_decisions")
        message = str(exc_info.value)
        assert "orch" in message
        assert "ユーザー合意" in message

    def test_message_mentions_escalation_env(self, monkeypatch):
        """メッセージで OW_ESCALATION=1 通過手段を明示する。"""
        monkeypatch.setenv("OW_ROLE", "worker")
        with pytest.raises(WorkerGuardError) as exc_info:
            check_worker_guard("add_decisions")
        assert "OW_ESCALATION=1" in str(exc_info.value)


class TestWorkerGuardErrorClass:
    """WorkerGuardError の class 性質。"""

    def test_is_runtime_error(self):
        """WorkerGuardError は RuntimeError サブクラス (broad except に拾われやすくする)。"""
        assert issubclass(WorkerGuardError, RuntimeError)

    def test_exported_from_guard_service(self):
        """guard_service モジュールから直接参照できる。"""
        assert guard_service.WorkerGuardError is WorkerGuardError
