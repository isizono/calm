"""session_ledger_service のユニットテスト

resolve_cli_session()(祖先pidチェーン探索・ファイルI/O)は外部境界としてmonkeypatchし、
register/mark_ended/record_checkinのDB書き込み契約のみを実DB(temp_db)で検証する。
"""
import threading

import pytest

from src.db import get_connection
from src.services import session_ledger_service


@pytest.fixture(autouse=True)
def _force_runtime_db_path(monkeypatch):
    """get_db_path()がtemp_dbのDISCUSSION_DB_PATHを確実に見るようにする。

    src.config.DB_PATHはモジュール初回import時に一度だけ解決され、以後
    固定値として扱われる(src/db.pyのget_db_path()参照)。本ファイルを単体で
    実行するなど、他のテストより先にDB系フィクスチャが動く場合、初回import
    タイミングが`_temp_db_template`フィクスチャ自身のテンプレートDB構築中と
    重なり、DB_PATHがテンプレートDBのパスに固定されてしまうことがある。
    sessions.session_idはTEXT PRIMARY KEYで、テストごとに同じ文字列
    ("s1"等)を使い回すため、この固定が起きるとテスト間でテンプレートDBを
    共有してしまい、他テストの行が見えてしまう。DB_PATHを明示的にNoneへ
    戻し、毎テストのDISCUSSION_DB_PATH(temp_db)を必ず優先させる。
    """
    import src.config as config
    monkeypatch.setattr(config, "DB_PATH", None)


def _fetch_row(session_id: str) -> dict:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        assert row is not None, f"session {session_id} not found"
        return dict(row)
    finally:
        conn.close()


def _fetch_row_or_none(session_id: str):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def _row_count() -> int:
    conn = get_connection()
    try:
        return conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    finally:
        conn.close()


class TestRegister:
    def test_resolved_cli_identity_fills_columns(self, temp_db, monkeypatch):
        """会話識別子が解決できた場合、cli_session_id等を埋めてcli_resolve_status='resolved'にする。"""
        monkeypatch.setattr(
            session_ledger_service, "resolve_cli_session",
            lambda sid: {"cli_session_id": "cli-1", "cli_pid": 111, "cwd": "/tmp/work"},
        )
        session_ledger_service.register(
            "s1", id_kind="bridge", harness="claude_code", host="myhost", mode="interactive",
        )
        row = _fetch_row("s1")
        assert row["id_kind"] == "bridge"
        assert row["harness"] == "claude_code"
        assert row["host"] == "myhost"
        assert row["cli_session_id"] == "cli-1"
        assert row["cli_pid"] == 111
        assert row["cwd"] == "/tmp/work"
        assert row["cli_resolve_status"] == "resolved"
        assert row["mode"] == "interactive"
        assert row["last_heartbeat_at"] is not None
        assert row["ended_at"] is None
        assert row["ended_reason"] is None

    def test_cli_session_found_without_session_id_is_marked_stale_not_resolved(self, temp_db, monkeypatch):
        """resolve_cli_sessionがcli_session_id=Noneの辞書を返す場合、'resolved'を名乗らない。

        read_cli_session()はCLIセッションファイルにnameさえあればsessionIdフィールドが
        無くても辞書を返しうる(src/infra/cli_session.py)。このとき会話識別子として使える
        値が無いのにcli_resolve_status='resolved'にすると、宛先候補側が「識別子が使える」
        と誤解する。
        """
        monkeypatch.setattr(
            session_ledger_service, "resolve_cli_session",
            lambda sid: {"cli_session_id": None, "cli_pid": 111, "cwd": "/tmp/work"},
        )
        session_ledger_service.register(
            "s1", id_kind="bridge", harness=None, host="myhost", mode="interactive",
        )
        row = _fetch_row("s1")
        assert row["cli_session_id"] is None
        assert row["cli_resolve_status"] == "stale"

    def test_unresolvable_cli_identity_leaves_columns_null(self, temp_db, monkeypatch):
        """会話識別子が解決できない場合、行の作成自体は失敗させずNULL埋めで進める。"""
        monkeypatch.setattr(session_ledger_service, "resolve_cli_session", lambda sid: None)
        session_ledger_service.register(
            "s1", id_kind="bridge", harness=None, host="myhost", mode="interactive",
        )
        row = _fetch_row("s1")
        assert row["cli_session_id"] is None
        assert row["cli_pid"] is None
        assert row["cwd"] is None
        assert row["cli_resolve_status"] == "file_not_found"

    def test_heartbeat_resend_updates_same_row_without_duplicate(self, temp_db, monkeypatch):
        """同一session_idの再登録(heartbeat)は新規行を作らずUPDATEする。"""
        monkeypatch.setattr(
            session_ledger_service, "resolve_cli_session",
            lambda sid: {"cli_session_id": "cli-1", "cli_pid": 111, "cwd": "/tmp"},
        )
        session_ledger_service.register(
            "s1", id_kind="bridge", harness="claude_code", host="host-a", mode="interactive",
        )
        # last_heartbeat_at(CURRENT_TIMESTAMPは秒精度)が実際に更新されたことを
        # 検証するため、いったん過去日時へ書き換えてから2回目のregisterを呼ぶ。
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE sessions SET last_heartbeat_at = '2000-01-01T00:00:00Z' WHERE session_id = 's1'"
            )
            conn.commit()
        finally:
            conn.close()

        session_ledger_service.register(
            "s1", id_kind="bridge", harness="claude_code", host="host-a", mode="interactive",
        )
        second = _fetch_row("s1")
        assert _row_count() == 1
        assert second["last_heartbeat_at"] != "2000-01-01T00:00:00Z"
        assert second["last_heartbeat_at"] is not None

    def test_generational_handover_closes_old_row_and_creates_new_one(self, temp_db, monkeypatch):
        """起動器プロセス再起動(同じcli_session_id・異なるsession_id)は旧行をsupersededで閉じる。"""
        monkeypatch.setattr(
            session_ledger_service, "resolve_cli_session",
            lambda sid: {"cli_session_id": "cli-1", "cli_pid": 111, "cwd": "/tmp"},
        )
        session_ledger_service.register(
            "s1", id_kind="bridge", harness="claude_code", host="host-a", mode="interactive",
        )
        session_ledger_service.register(
            "s2", id_kind="bridge", harness="claude_code", host="host-a", mode="interactive",
        )
        old = _fetch_row("s1")
        new = _fetch_row("s2")
        assert old["ended_at"] is not None
        assert old["ended_reason"] == "superseded"
        assert new["ended_at"] is None
        assert new["cli_session_id"] == "cli-1"

    def test_ended_row_is_not_revived_by_late_heartbeat(self, temp_db, monkeypatch):
        """supersededで閉じた旧行に遅延heartbeatが届いても復活させない。"""
        monkeypatch.setattr(
            session_ledger_service, "resolve_cli_session",
            lambda sid: {"cli_session_id": "cli-1", "cli_pid": 111, "cwd": "/tmp"},
        )
        session_ledger_service.register(
            "s1", id_kind="bridge", harness="claude_code", host="host-a", mode="interactive",
        )
        session_ledger_service.register(
            "s2", id_kind="bridge", harness="claude_code", host="host-a", mode="interactive",
        )
        # s1(旧世代)へ遅延heartbeatが届く想定の再register呼び出し
        session_ledger_service.register(
            "s1", id_kind="bridge", harness="claude_code", host="host-a", mode="interactive",
        )
        old = _fetch_row("s1")
        new = _fetch_row("s2")
        assert old["ended_at"] is not None
        assert old["ended_reason"] == "superseded"
        assert new["ended_at"] is None
        # 部分一意索引違反を起こさず、生存行はs2のみであること
        conn = get_connection()
        try:
            live = conn.execute(
                "SELECT session_id FROM sessions WHERE cli_session_id = 'cli-1' AND ended_at IS NULL"
            ).fetchall()
        finally:
            conn.close()
        assert [r["session_id"] for r in live] == ["s2"]

    def test_transient_resolve_failure_keeps_previously_resolved_identity(self, temp_db, monkeypatch):
        """resolve_cli_session()のfail-close設計により、一時的な解決失敗で既知の会話識別子をNULLに戻さない。"""
        monkeypatch.setattr(
            session_ledger_service, "resolve_cli_session",
            lambda sid: {"cli_session_id": "cli-1", "cli_pid": 111, "cwd": "/tmp/work"},
        )
        session_ledger_service.register(
            "s1", id_kind="bridge", harness="claude_code", host="host-a", mode="interactive",
        )
        monkeypatch.setattr(session_ledger_service, "resolve_cli_session", lambda sid: None)
        session_ledger_service.register(
            "s1", id_kind="bridge", harness="claude_code", host="host-a", mode="interactive",
        )
        row = _fetch_row("s1")
        assert row["cli_session_id"] == "cli-1"
        assert row["cli_pid"] == 111
        assert row["cwd"] == "/tmp/work"
        assert row["cli_resolve_status"] == "resolved"

    def test_concurrent_registrations_with_same_cli_session_id_do_not_violate_unique_index(
        self, temp_db, monkeypatch
    ):
        """トランザクション境界により、同時登録でも一意制約違反で書き込みが落ちない。"""
        monkeypatch.setattr(
            session_ledger_service, "resolve_cli_session",
            lambda sid: {"cli_session_id": "cli-1", "cli_pid": 111, "cwd": "/tmp"},
        )
        errors: list[Exception] = []

        def _register(sid: str) -> None:
            try:
                session_ledger_service.register(
                    sid, id_kind="bridge", harness="claude_code", host="host-a", mode="interactive",
                )
            except Exception as e:  # pragma: no cover - 失敗時のみ使う診断経路
                errors.append(e)

        threads = [threading.Thread(target=_register, args=(sid,)) for sid in ("s1", "s2")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == []
        conn = get_connection()
        try:
            live = conn.execute(
                "SELECT session_id FROM sessions WHERE cli_session_id = 'cli-1' AND ended_at IS NULL"
            ).fetchall()
        finally:
            conn.close()
        assert len(live) == 1


class TestMarkEnded:
    def test_sets_ended_at_and_reason_unregister(self, temp_db, monkeypatch):
        monkeypatch.setattr(session_ledger_service, "resolve_cli_session", lambda sid: None)
        session_ledger_service.register(
            "s1", id_kind="bridge", harness=None, host="host-a", mode="interactive",
        )
        session_ledger_service.mark_ended("s1", "unregister")
        row = _fetch_row("s1")
        assert row["ended_at"] is not None
        assert row["ended_reason"] == "unregister"

    def test_sets_ended_at_and_reason_ttl(self, temp_db, monkeypatch):
        monkeypatch.setattr(session_ledger_service, "resolve_cli_session", lambda sid: None)
        session_ledger_service.register(
            "s1", id_kind="bridge", harness=None, host="host-a", mode="interactive",
        )
        session_ledger_service.mark_ended("s1", "ttl")
        row = _fetch_row("s1")
        assert row["ended_reason"] == "ttl"

    def test_is_idempotent_once_already_ended(self, temp_db, monkeypatch):
        """既にended済みの行への2回目のmark_endedはended_reasonを上書きしない(冪等)。"""
        monkeypatch.setattr(session_ledger_service, "resolve_cli_session", lambda sid: None)
        session_ledger_service.register(
            "s1", id_kind="bridge", harness=None, host="host-a", mode="interactive",
        )
        session_ledger_service.mark_ended("s1", "unregister")
        session_ledger_service.mark_ended("s1", "ttl")
        row = _fetch_row("s1")
        assert row["ended_reason"] == "unregister"

    def test_unknown_session_id_is_noop(self, temp_db):
        session_ledger_service.mark_ended("does-not-exist", "unregister")
        assert _fetch_row_or_none("does-not-exist") is None


class TestRecordCheckin:
    def test_updates_last_checkin_columns(self, temp_db, monkeypatch):
        monkeypatch.setattr(session_ledger_service, "resolve_cli_session", lambda sid: None)
        session_ledger_service.register(
            "s1", id_kind="bridge", harness=None, host="host-a", mode="interactive",
        )
        session_ledger_service.record_checkin("s1", 42)
        row = _fetch_row("s1")
        assert row["last_checkin_activity_id"] == 42
        assert row["last_checkin_at"] is not None

    def test_none_session_id_is_noop(self, temp_db):
        session_ledger_service.record_checkin(None, 42)
        assert _row_count() == 0

    def test_unknown_session_id_is_noop(self, temp_db):
        session_ledger_service.record_checkin("does-not-exist", 42)
        assert _fetch_row_or_none("does-not-exist") is None
