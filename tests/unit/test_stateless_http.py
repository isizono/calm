"""streamable HTTP をstatelessで動かす設定の検証。

サーバー再起動後、クライアントが古い mcp-session-id を持ったまま（initializeを
やり直さず）リクエストしても成功することを、実際のASGIアプリで確かめる。
また update_goal / report_signal が MCPセッションIDではなく bridge ID を記録することを確かめる。
"""
import json

import pytest
from starlette.testclient import TestClient

import src.main as main_module
from src.db import get_connection

_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}


def _tool_result(resp):
    """JSON / SSE どちらの応答形式でも tools/call の structuredContent を返す。"""
    body = resp.text
    if body.lstrip().startswith("event:") or "\ndata:" in body or body.startswith("data:"):
        body = next(ln[5:] for ln in body.splitlines() if ln.startswith("data:"))
    return json.loads(body)["result"]["structuredContent"]


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


def test_tool_call_without_initialize_succeeds(client):
    r = _call(client, "roll_dice", {"sides": 6})
    assert r.status_code == 200, r.text
    assert 1 <= _tool_result(r)["result"] <= 6


def test_tool_call_with_unknown_session_id_succeeds(client):
    r = _call(client, "roll_dice", {"sides": 6}, {"mcp-session-id": "stale-id-from-previous-server"})
    assert r.status_code == 200, r.text
    assert 1 <= _tool_result(r)["result"] <= 6


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


def test_update_goal_records_bridge_id(client):
    from src.main import judge_goal, set_goal
    from src.services.activity_service import add_activity

    act = add_activity(title="a", description="d", tags=["domain:test"], check_in=False)["activity_id"]
    created = set_goal(
        act,
        {"new": {"handle": "g", "statement": "終わる", "conditions": [
            {"statement": "c1", "actor": "claude", "state": "satisfied", "note": "済"},
        ]}},
    )
    goal_id = created["goal"]["goal_id_raw"]
    judge_goal(goal_id, "achieved")

    r = _call(
        client,
        "update_goal",
        {"goal_id": goal_id, "reopen_reason": "誤判定"},
        {"x-calm-bridge-session-id": "bridge-xyz", "mcp-session-id": "stale"},
    )
    assert r.status_code == 200, r.text
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT session_id FROM signal_events WHERE kind = 'goal_rollback'"
        ).fetchone()
    finally:
        conn.close()
    assert row["session_id"] == "bridge-xyz"
