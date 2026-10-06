"""AgentIdentityMiddleware と delivery_key() の単体テスト。

識別子引数を引数から必ず取り除き、ツール呼び出しの間だけ ContextVar に保持すること、
既出管理キーがサブエージェントごとに分かれ、識別できない呼び出しは記録しない契約を
保つことを検証する。
"""
from types import SimpleNamespace

import pytest

from src.infra import session_identity
from src.infra.session_identity import AGENT_ID_ARG, current_agent_id, delivery_key
from src.middleware.agent_identity_middleware import AgentIdentityMiddleware
from src.services import checkin_tier_service, search_service


@pytest.fixture(autouse=True)
def _no_leaked_agent_id():
    token = session_identity.set_current_agent_id(None)
    yield
    session_identity.reset_current_agent_id(token)


def _context(arguments):
    return SimpleNamespace(message=SimpleNamespace(name="check_in", arguments=arguments))


async def _run(arguments):
    """ミドルウェアを通し、call_next の中で観測した識別子と引数を返す。"""
    seen = {}

    async def call_next(ctx):
        seen["agent_id"] = current_agent_id()
        seen["arguments"] = dict(ctx.message.arguments or {})
        return "result"

    result = await AgentIdentityMiddleware().on_call_tool(_context(arguments), call_next)
    return result, seen


class TestDeliveryKey:
    def test_none_session_stays_none_even_inside_a_subagent(self):
        token = session_identity.set_current_agent_id("a1")
        try:
            assert delivery_key(None) is None
        finally:
            session_identity.reset_current_agent_id(token)

    def test_parent_call_uses_the_session_id_as_is(self):
        assert delivery_key("parent") == "parent"

    def test_subagent_call_appends_the_agent_id(self):
        token = session_identity.set_current_agent_id("a1")
        try:
            assert delivery_key("parent") == "parent#a1"
        finally:
            session_identity.reset_current_agent_id(token)


class TestAgentIdentityMiddleware:
    @pytest.mark.asyncio
    async def test_removes_the_argument_and_exposes_the_agent_during_the_call(self):
        result, seen = await _run({"activity_id": 3, AGENT_ID_ARG: "a1"})

        assert result == "result"
        assert seen["agent_id"] == "a1"
        assert seen["arguments"] == {"activity_id": 3}

    @pytest.mark.asyncio
    async def test_resets_the_agent_after_the_call(self):
        await _run({AGENT_ID_ARG: "a1"})

        assert current_agent_id() is None

    @pytest.mark.asyncio
    async def test_parent_call_without_the_argument_is_untouched(self):
        _, seen = await _run({"activity_id": 3})

        assert seen["agent_id"] is None
        assert seen["arguments"] == {"activity_id": 3}

    @pytest.mark.asyncio
    async def test_none_arguments_do_not_crash(self):
        _, seen = await _run(None)

        assert seen["agent_id"] is None

    @pytest.mark.asyncio
    async def test_malformed_value_is_dropped_and_not_used_as_identity(self):
        _, seen = await _run({"activity_id": 3, AGENT_ID_ARG: 12345})

        assert seen["agent_id"] is None
        assert seen["arguments"] == {"activity_id": 3}

    @pytest.mark.asyncio
    async def test_overlong_value_is_dropped_and_not_used_as_identity(self):
        too_long = "x" * (session_identity.AGENT_ID_MAX_LEN + 1)
        _, seen = await _run({AGENT_ID_ARG: too_long})

        assert seen["agent_id"] is None
        assert AGENT_ID_ARG not in seen["arguments"]

    @pytest.mark.asyncio
    async def test_the_argument_is_removed_even_when_the_call_raises(self):
        arguments = {AGENT_ID_ARG: "a1"}

        async def call_next(ctx):
            raise RuntimeError("tool failed")

        with pytest.raises(RuntimeError):
            await AgentIdentityMiddleware().on_call_tool(_context(arguments), call_next)

        assert AGENT_ID_ARG not in arguments
        assert current_agent_id() is None


class TestPresentedRecordsKeying:
    """検索の提示済み集合も、親とサブエージェントで別々に持つ。"""

    @pytest.fixture(autouse=True)
    def _clear_presented(self):
        search_service._presented_records.clear()
        yield
        search_service._presented_records.clear()

    def test_subagent_presentations_are_invisible_to_the_parent(self):
        key = ("log", 5)
        token = session_identity.set_current_agent_id("a1")
        try:
            search_service._presented_records_register("parent", [key])
            assert search_service._presented_records_contains("parent", key)
        finally:
            session_identity.reset_current_agent_id(token)

        assert not search_service._presented_records_contains("parent", key)


class TestEvictionKeepsParentEntries:
    """上限で押し出すとき、サブエージェントのキーを先に消して親のキーを残す。"""

    @pytest.fixture(autouse=True)
    def _clear_greeted(self):
        checkin_tier_service._greeted_sessions.clear()
        yield
        checkin_tier_service._greeted_sessions.clear()

    def test_evicts_the_oldest_subagent_key_before_any_parent_key(self):
        store = {"p1": True, "p2#a": True, "p3#b": True}

        session_identity.evict_for_new_key(store, 3)

        assert store == {"p1": True, "p3#b": True}

    def test_falls_back_to_the_oldest_key_when_there_is_no_subagent_key(self):
        store = {"p1": True, "p2": True, "p3": True}

        session_identity.evict_for_new_key(store, 3)

        assert store == {"p2": True, "p3": True}

    def test_parent_first_call_survives_a_flood_of_subagent_keys(self):
        limit = checkin_tier_service._GREETED_SESSIONS_MAX
        assert checkin_tier_service._consume_first_call_flag("parent") is True

        for i in range(limit * 2):
            token = session_identity.set_current_agent_id(f"a{i}")
            try:
                checkin_tier_service._consume_first_call_flag("parent")
            finally:
                session_identity.reset_current_agent_id(token)

        assert checkin_tier_service._consume_first_call_flag("parent") is False

    def test_parent_only_store_still_evicts_the_oldest_session(self):
        limit = checkin_tier_service._GREETED_SESSIONS_MAX
        for i in range(limit):
            checkin_tier_service._consume_first_call_flag(f"s{i}")

        checkin_tier_service._consume_first_call_flag("s_new")

        assert checkin_tier_service._consume_first_call_flag("s0") is True
