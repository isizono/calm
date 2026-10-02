"""hooks/signal_capture.py のユニットテスト

try_capture_signal / try_capture_guard_block が (1) 正常系で signal_events に記録し、
(2) DB接続不能・import失敗を含むいかなる異常でも例外を外に漏らさないことを検証する。
"""
import sys

import pytest

from hooks.signal_capture import try_capture_guard_block, try_capture_signal
from src.db import get_connection


def test_records_signal_on_valid_db(temp_db):
    try_capture_signal(
        kind="machine_error",
        summary="boom",
        source="hook:stop",
        detail="traceback here",
    )

    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM signal_events").fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row["kind"] == "machine_error"
    assert row["summary"] == "boom"
    assert row["source"] == "hook:stop"
    assert row["detail"] == "traceback here"
    assert row["status"] == "new"


def test_never_raises_when_db_path_unwritable(monkeypatch, tmp_path):
    """DBを開けない状況でも例外を送出しない（フェイルオープンの最終防衛ライン）。"""
    unreachable = tmp_path / "nonexistent_dir" / "db.sqlite"
    monkeypatch.setenv("DISCUSSION_DB_PATH", str(unreachable))

    # 例外が外に漏れないことそのものがテスト対象
    try_capture_signal(kind="machine_error", summary="boom", source="hook:stop")


def test_never_raises_when_signal_service_import_fails(monkeypatch, temp_db, capsys):
    """signal_service の import 自体が失敗しても例外を送出しない。"""
    monkeypatch.setitem(sys.modules, "src.services.signal_service", None)

    try_capture_signal(kind="machine_error", summary="boom", source="hook:stop")

    captured = capsys.readouterr()
    assert "signal_capture.py failed" in captured.err


def test_invalid_kind_does_not_raise(temp_db):
    """kind が KNOWN_KINDS 外でも record_signal の ValueError が握りつぶされる。"""
    try_capture_signal(kind="not_a_real_kind", summary="boom", source="hook:stop")

    conn = get_connection()
    try:
        count = conn.execute("SELECT COUNT(*) AS c FROM signal_events").fetchone()["c"]
    finally:
        conn.close()
    assert count == 0


def test_guard_block_records_signal_on_valid_db(temp_db):
    try_capture_guard_block(
        source="hook:preblock",
        summary="internal ID literal blocked (code)",
        detail="tool=Bash fields=['command']",
    )

    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM signal_events").fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row["kind"] == "guard_block"
    assert row["source"] == "hook:preblock"
    assert row["summary"] == "internal ID literal blocked (code)"
    assert row["status"] == "new"


def test_guard_block_dedupes_same_rule_type(temp_db):
    """同じ(source, summary)の再発はoccurrence_countに畳まれ、新規行は作らない。"""
    try_capture_guard_block(source="hook:preblock", summary="internal ID literal blocked (code)")
    try_capture_guard_block(source="hook:preblock", summary="internal ID literal blocked (code)")

    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM signal_events WHERE kind = 'guard_block'").fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    assert rows[0]["occurrence_count"] == 2


def test_guard_block_never_raises_when_db_path_unwritable(monkeypatch, tmp_path):
    """DBを開けない状況でも例外を送出しない（deny判定自体を壊さないための保証）。"""
    unreachable = tmp_path / "nonexistent_dir" / "db.sqlite"
    monkeypatch.setenv("DISCUSSION_DB_PATH", str(unreachable))

    # 例外が外に漏れないことそのものがテスト対象
    try_capture_guard_block(source="hook:preblock", summary="internal ID literal blocked (code)")


def test_guard_block_never_raises_when_signal_service_import_fails(monkeypatch, temp_db, capsys):
    monkeypatch.setitem(sys.modules, "src.services.signal_service", None)

    try_capture_guard_block(source="hook:preblock", summary="internal ID literal blocked (code)")

    captured = capsys.readouterr()
    assert "signal_capture.py (guard_block) failed" in captured.err
