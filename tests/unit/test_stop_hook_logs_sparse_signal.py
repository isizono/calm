"""hooks/stop_hook.py::_collect_logs_sparse_nudges のユニットテスト

logs_sparse判定のimport失敗・get_hints失敗が、既存のstderr通知に加えて
signal_eventsへmachine_errorとして記録されること（沈黙防止）を検証する。
"""
import sys

import src.services as services_pkg
from hooks.stop_hook import _collect_logs_sparse_nudges
from src.db import get_connection

# collection時点でconfig.DB_PATHをNoneに確定させ、temp_dbの一時パスがテンプレDBに
# 固定される既存の潜在バグ(本ファイル単体実行時のみ再現)を回避する。
from src import config  # noqa: F401,E402


def _decision_events(topic_id: int = 1) -> list[dict]:
    return [{"e": "tool", "name": "add_decisions", "turn": 1, "topic_ids": [topic_id]}]


def test_hint_service_import_failure_records_machine_error_signal(temp_db, monkeypatch):
    # fromlist importはキャッシュ済み属性を優先するため、属性自体も削除する。
    monkeypatch.delattr(services_pkg, "hint_service", raising=False)
    monkeypatch.setitem(sys.modules, "src.services.hint_service", None)

    result = _collect_logs_sparse_nudges(_decision_events(), current_turn=1)

    assert result == []
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM signal_events").fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["kind"] == "machine_error"
    assert row["source"] == "hook:stop:logs_sparse"


def test_get_hints_failure_records_machine_error_signal(temp_db, monkeypatch):
    from src.services import hint_service

    def _boom(entity_type, entity_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(hint_service, "get_hints", _boom)

    result = _collect_logs_sparse_nudges(_decision_events(), current_turn=1)

    assert result == []
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM signal_events").fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["kind"] == "machine_error"
    assert row["source"] == "hook:stop:logs_sparse"
    assert "boom" in row["summary"]
