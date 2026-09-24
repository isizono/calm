"""session_register/session_unregister エンドポイントの統合テスト

セッションカウント管理(SessionManager)とセッション台帳(session_ledger_service)への
書き込みが custom_route 経由で正しく連動することを検証する。custom_route関数を
直接呼び出すパターン(tests/integration/test_asks_http_api.py と同じ)を使う。
"""
import asyncio
import json
import socket

import pytest
from starlette.requests import Request

import src.main as main_module
from src.db import get_connection
from src.infra.session_manager import SessionManager
from src.services import session_ledger_service


@pytest.fixture(autouse=True)
def _force_runtime_db_path(monkeypatch):
    """get_db_path()がtemp_dbのDISCUSSION_DB_PATHを確実に見るようにする。

    src.config.DB_PATHはモジュール初回import時に一度だけ解決され固定される
    (src/db.pyのget_db_path()参照)。詳細はtests/unit/test_session_ledger_service.pyの
    同名フィクスチャのdocstring参照。
    """
    import src.config as config
    monkeypatch.setattr(config, "DB_PATH", None)


def _post_request(path: str, body: dict) -> Request:
    data = json.dumps(body).encode()
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
    }
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": data, "more_body": False}
        return {"type": "http.disconnect"}

    return Request(scope, receive)


@pytest.fixture
def session_manager(monkeypatch):
    """mainモジュールのSessionManagerシングルトンを、本番と同じコールバック配線で差し替える。"""
    mgr = SessionManager(
        grace_period_sec=0, liveness_timeout_sec=0,
        on_session_removed=lambda sid, reason: session_ledger_service.mark_ended(sid, reason),
    )
    monkeypatch.setattr(main_module, "_session_manager", mgr)
    return mgr


def _fetch_session(session_id: str):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


class TestSessionRegisterEndpoint:
    def test_register_writes_ledger_row_with_launcher_supplied_harness_and_host(
        self, temp_db, session_manager, monkeypatch
    ):
        monkeypatch.setattr(session_ledger_service, "resolve_cli_session", lambda sid: None)
        response = asyncio.run(
            main_module.session_register(
                _post_request(
                    "/session/register",
                    {"session_id": "s1", "harness": "codex", "host": "launcher-host"},
                )
            )
        )
        assert response.status_code == 200
        assert json.loads(response.body)["registered"] is True

        row = _fetch_session("s1")
        assert row is not None
        assert row["id_kind"] == "bridge"
        assert row["harness"] == "codex"
        assert row["host"] == "launcher-host"

    def test_register_falls_back_to_server_host_when_body_omits_it(
        self, temp_db, session_manager, monkeypatch
    ):
        """古いlauncher(harness/host未送信)との後方互換: hostはサーバー側で補う。"""
        monkeypatch.setattr(session_ledger_service, "resolve_cli_session", lambda sid: None)
        asyncio.run(
            main_module.session_register(
                _post_request("/session/register", {"session_id": "s1"})
            )
        )
        row = _fetch_session("s1")
        assert row["host"] == socket.gethostname()
        assert row["harness"] is None

    def test_ledger_write_failure_does_not_fail_endpoint(
        self, temp_db, session_manager, monkeypatch
    ):
        """台帳書き込みはベストエフォート: 失敗してもmgr.register()の成否は返す。"""
        def boom(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(session_ledger_service, "register", boom)
        response = asyncio.run(
            main_module.session_register(
                _post_request("/session/register", {"session_id": "s1"})
            )
        )
        assert response.status_code == 200
        assert json.loads(response.body)["registered"] is True


class TestSessionUnregisterEndpoint:
    def test_unregister_marks_ended_via_manager_callback(
        self, temp_db, session_manager, monkeypatch
    ):
        monkeypatch.setattr(session_ledger_service, "resolve_cli_session", lambda sid: None)
        asyncio.run(
            main_module.session_register(
                _post_request("/session/register", {"session_id": "s1"})
            )
        )
        response = asyncio.run(
            main_module.session_unregister(
                _post_request("/session/unregister", {"session_id": "s1"})
            )
        )
        assert response.status_code == 200
        assert json.loads(response.body)["unregistered"] is True
        row = _fetch_session("s1")
        assert row["ended_at"] is not None
        assert row["ended_reason"] == "unregister"

    def test_unregister_ends_ledger_row_even_when_manager_has_no_in_memory_record(
        self, temp_db, session_manager, monkeypatch
    ):
        """サーバー再起動直後等、mgr側にin-memory登録が無い(unregistered=False)場合でも
        台帳は直接mark_endedで閉じる(コールバック未発火をカバーする保険)。"""
        monkeypatch.setattr(session_ledger_service, "resolve_cli_session", lambda sid: None)
        session_ledger_service.register(
            "s1", id_kind="bridge", harness=None, host="h", mode="interactive",
        )
        response = asyncio.run(
            main_module.session_unregister(
                _post_request("/session/unregister", {"session_id": "s1"})
            )
        )
        assert response.status_code == 200
        assert json.loads(response.body)["unregistered"] is False
        row = _fetch_session("s1")
        assert row["ended_at"] is not None
        assert row["ended_reason"] == "unregister"
