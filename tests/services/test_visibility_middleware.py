"""CapabilityVisibilityMiddleware の unit test。

on_initialize で resolve した role に応じて disable_components が呼ばれ、
hidden_tools_for と同じ tool 集合が渡ることを検証する。
DB fixture は conftest の temp_db を使う。
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.db import get_connection


def _make_mw_context(session_id: str | None = "sess-1"):
    fastmcp_ctx = MagicMock()
    if session_id is None:
        type(fastmcp_ctx).session_id = property(
            fget=lambda self: (_ for _ in ()).throw(RuntimeError("no session"))
        )
    else:
        fastmcp_ctx.session_id = session_id
    mw_context = MagicMock()
    mw_context.fastmcp_context = fastmcp_ctx
    return mw_context, fastmcp_ctx


@pytest.mark.asyncio
async def test_hides_orch_disallowed_tools_when_role_is_orch(temp_db):
    from src.services.visibility_middleware import CapabilityVisibilityMiddleware
    from src.services.capability_matrix import hidden_tools_for

    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO session_identity (session_id, role) VALUES (?, ?)",
            ("sess-orch", "orch"),
        )
        conn.commit()
    finally:
        conn.close()

    middleware = CapabilityVisibilityMiddleware()
    mw_context, _ = _make_mw_context("sess-orch")
    call_next = AsyncMock(return_value="initialize-result")

    with patch(
        "src.services.visibility_middleware.disable_components",
        new=AsyncMock(),
    ) as mock_disable:
        result = await middleware.on_initialize(mw_context, call_next)

    assert result == "initialize-result"
    mock_disable.assert_called_once()
    call_kwargs = mock_disable.call_args.kwargs
    assert call_kwargs["names"] == hidden_tools_for("orch")
    assert call_kwargs["components"] == {"tool"}


@pytest.mark.asyncio
async def test_does_nothing_when_role_is_unresolved(temp_db):
    from src.services.visibility_middleware import CapabilityVisibilityMiddleware

    middleware = CapabilityVisibilityMiddleware()
    mw_context, _ = _make_mw_context("sess-unknown")
    call_next = AsyncMock(return_value="initialize-result")

    with patch(
        "src.services.visibility_middleware.disable_components",
        new=AsyncMock(),
    ) as mock_disable:
        result = await middleware.on_initialize(mw_context, call_next)

    assert result == "initialize-result"
    mock_disable.assert_not_called()


@pytest.mark.asyncio
async def test_falls_back_to_env_ow_role(temp_db, monkeypatch):
    from src.services.visibility_middleware import CapabilityVisibilityMiddleware
    from src.services.capability_matrix import hidden_tools_for

    monkeypatch.setenv("OW_ROLE", "worker")
    middleware = CapabilityVisibilityMiddleware()
    mw_context, _ = _make_mw_context("sess-no-db-row")
    call_next = AsyncMock(return_value="initialize-result")

    with patch(
        "src.services.visibility_middleware.disable_components",
        new=AsyncMock(),
    ) as mock_disable:
        await middleware.on_initialize(mw_context, call_next)

    mock_disable.assert_called_once()
    assert mock_disable.call_args.kwargs["names"] == hidden_tools_for("worker")


@pytest.mark.asyncio
async def test_no_fastmcp_context_skips_hiding(temp_db):
    from src.services.visibility_middleware import CapabilityVisibilityMiddleware

    middleware = CapabilityVisibilityMiddleware()
    mw_context = MagicMock()
    mw_context.fastmcp_context = None
    call_next = AsyncMock(return_value="initialize-result")

    with patch(
        "src.services.visibility_middleware.disable_components",
        new=AsyncMock(),
    ) as mock_disable:
        result = await middleware.on_initialize(mw_context, call_next)

    assert result == "initialize-result"
    mock_disable.assert_not_called()


@pytest.mark.asyncio
async def test_session_id_runtime_error_uses_env_fallback(temp_db, monkeypatch):
    from src.services.visibility_middleware import CapabilityVisibilityMiddleware
    from src.services.capability_matrix import hidden_tools_for

    monkeypatch.setenv("OW_ROLE", "dispatcher")
    middleware = CapabilityVisibilityMiddleware()
    mw_context, _ = _make_mw_context(None)  # session_id raises RuntimeError
    call_next = AsyncMock(return_value="initialize-result")

    with patch(
        "src.services.visibility_middleware.disable_components",
        new=AsyncMock(),
    ) as mock_disable:
        await middleware.on_initialize(mw_context, call_next)

    mock_disable.assert_called_once()
    assert mock_disable.call_args.kwargs["names"] == hidden_tools_for("dispatcher")
