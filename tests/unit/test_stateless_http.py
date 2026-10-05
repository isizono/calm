"""streamable HTTP をstatelessで動かす設定の検証。

サーバー再起動後、クライアントが古い mcp-session-id を持ったまま（initializeを
やり直さず）リクエストしても成功することを、実際のASGIアプリで確かめる。
また update_goal / report_signal が MCPセッションIDではなく bridge ID を記録することを確かめる。
"""
import pytest
from starlette.testclient import TestClient

import src.main as main_module
from src.db import get_connection

_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}


def _call(client, name, arguments, extra_headers=None):
    return client.post(
        "/mcp",
        headers={**_HEADERS, **(extra_headers or {})},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )


@pytest.fixture
def client(temp_db):
    app = main_module.mcp.http_app(stateless_http=main_module.HTTP_STATELESS)
    with TestClient(app) as c:
        yield c


def test_server_runs_stateless():
    assert main_module.HTTP_STATELESS is True


def test_tool_call_without_initialize_succeeds(client):
    r = _call(client, "roll_dice", {"sides": 6})
    assert r.status_code == 200, r.text
    assert '"result"' in r.text


def test_tool_call_with_unknown_session_id_succeeds(client):
    r = _call(client, "roll_dice", {"sides": 6}, {"mcp-session-id": "stale-id-from-previous-server"})
    assert r.status_code == 200, r.text
    assert '"result"' in r.text


def test_report_signal_records_bridge_id(client):
    r = _call(
        client,
        "report_signal",
        {"kind": "friction", "summary": "stateless test"},
        {"x-calm-bridge-session-id": "bridge-abc", "mcp-session-id": "stale"},
    )
    assert r.status_code == 200, r.text
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT session_id FROM signal_events WHERE summary = 'stateless test'"
        ).fetchone()
    finally:
        conn.close()
    assert row["session_id"] == "bridge-abc"


def test_update_goal_passes_bridge_id(monkeypatch):
    seen = {}
    monkeypatch.setattr(main_module, "get_caller_session_id", lambda: "bridge-xyz")
    monkeypatch.setattr(
        main_module.goal_service,
        "update_goal",
        lambda *a, **kw: seen.update(kw) or {},
    )
    main_module.update_goal(1, reopen_reason="x")
    assert seen["session_id"] == "bridge-xyz"
