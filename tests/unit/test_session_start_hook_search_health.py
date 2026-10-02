"""hooks/session_start_hook.py の search_health セクションのテスト

tests/unit/test_search_health_service.py が check_search_health() 自体の閾値判定を
検証するのに対し、本ファイルはhook側の配線（1行注意の文言・signal_events書込・
再起動しても重複しないdedup）をin-process importで検証する。
"""
import json
from datetime import datetime, timedelta, timezone

from hooks import session_start_hook
from src.db import get_connection


def _seed_many(conn, count: int, *, degraded: bool, qe_expansions: list, start_days_ago: float = 0.1,
               step_seconds: float = 1.0) -> None:
    for i in range(count):
        days_ago = start_days_ago - (i * step_seconds) / 86400
        ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S.%f")
        diagnostics = {"degraded": degraded, "qe_expansions": qe_expansions}
        conn.execute(
            "INSERT INTO search_telemetry (query, parameters, result_count, diagnostics_json, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            (json.dumps("q"), json.dumps({}), 0, json.dumps(diagnostics), ts),
        )


def _signal_rows(conn, source: str) -> list[dict]:
    rows = conn.execute(
        "SELECT kind, summary, status, occurrence_count FROM signal_events WHERE source = ?",
        (source,),
    ).fetchall()
    return [dict(r) for r in rows]


def test_healthy_state_yields_no_warning_and_no_signal(temp_db):
    conn = get_connection()
    try:
        _seed_many(conn, 20, degraded=False, qe_expansions=["x"])
        conn.commit()

        text = session_start_hook._build_search_health_section(conn)
        assert text == ""
        assert _signal_rows(conn, "hook:search_health") == []
    finally:
        conn.close()


def test_degraded_over_threshold_yields_one_line_warning_and_signal(temp_db):
    conn = get_connection()
    try:
        _seed_many(conn, 5, degraded=True, qe_expansions=["x"], start_days_ago=0.1)
        _seed_many(conn, 15, degraded=False, qe_expansions=["x"], start_days_ago=0.2)
        conn.commit()

        text = session_start_hook._build_search_health_section(conn)

        assert text.count("\n") == 1  # 1行の注意（末尾改行のみ）
        assert "検索品質の劣化" in text
        assert "縮退率" in text

        rows = _signal_rows(conn, "hook:search_health")
        assert len(rows) == 1
        assert rows[0]["kind"] == "machine_error"
        assert rows[0]["status"] == "new"
        assert rows[0]["occurrence_count"] == 1
    finally:
        conn.close()


def test_qe_dead_yields_warning_and_signal(temp_db):
    conn = get_connection()
    try:
        _seed_many(conn, 25, degraded=False, qe_expansions=[])
        conn.commit()

        text = session_start_hook._build_search_health_section(conn)

        assert "クエリ拡張" in text
        rows = _signal_rows(conn, "hook:search_health")
        assert len(rows) == 1
    finally:
        conn.close()


def test_insufficient_sample_count_yields_no_warning(temp_db):
    conn = get_connection()
    try:
        _seed_many(conn, 10, degraded=True, qe_expansions=[])
        conn.commit()

        text = session_start_hook._build_search_health_section(conn)
        assert text == ""
        assert _signal_rows(conn, "hook:search_health") == []
    finally:
        conn.close()


def test_repeated_builds_in_same_degraded_state_do_not_duplicate_signal(temp_db):
    """同じ劣化状態が続く間に何度hookを起動しても、signal_eventsの行は増えずoccurrence_countが伸びる"""
    conn = get_connection()
    try:
        _seed_many(conn, 5, degraded=True, qe_expansions=["x"], start_days_ago=0.1)
        _seed_many(conn, 15, degraded=False, qe_expansions=["x"], start_days_ago=0.2)
        conn.commit()

        session_start_hook._build_search_health_section(conn)
        session_start_hook._build_search_health_section(conn)
        session_start_hook._build_search_health_section(conn)

        rows = _signal_rows(conn, "hook:search_health")
        assert len(rows) == 1
        assert rows[0]["occurrence_count"] == 3
    finally:
        conn.close()
