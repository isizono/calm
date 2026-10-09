"""scripts/signal_wait.py の単体テスト。

DBは実SQLite（temp_db、全migration適用）で、signalはsignal_serviceの
record_signalで作る。main()のsleep引数を差し替え、sleepの副作用として
signalを起票することで「待機中に起票された」を再現する。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts import signal_wait  # noqa: E402
from src.db import get_connection  # noqa: E402
from src.services.signal_service import record_signal  # noqa: E402


@pytest.fixture(autouse=True)
def _env(temp_db, monkeypatch):
    monkeypatch.delenv("CLAUDE_PID", raising=False)
    # immutableで読む前提を本番と揃える: 書き込み側の接続を閉じた後にだけ読む
    with get_connection(load_vec=False) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _record(kind, summary):
    sig = record_signal(kind, summary)
    with get_connection(load_vec=False) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return sig["id"]


def test_wakes_only_on_contradiction_or_friction_after_the_given_id(temp_db, capsys):
    old = _record("friction", "仕掛ける前の摩擦")
    pending = [
        lambda: _record("machine_error", "対象外の種類"),
        lambda: _record("contradiction", "新しい矛盾"),
    ]
    created = []

    def sleep(_):
        created.append(pending.pop(0)())

    assert signal_wait.main(["--after", str(old), "--db", temp_db], sleep=sleep) == 0
    out = capsys.readouterr().out
    contradiction_id = created[1]
    assert f"#{contradiction_id} contradiction: 新しい矛盾" in out
    assert "仕掛ける前の摩擦" not in out
    assert "対象外の種類" not in out
    assert out.rstrip().endswith(f"--after {contradiction_id}")


def test_timeout_prints_rearm_with_same_after(temp_db, capsys):
    clock = iter([0.0, signal_wait.WAIT_LIMIT_SECONDS + 1])
    assert signal_wait.main(["--after", "5", "--db", temp_db],
                            sleep=lambda _: None, now=lambda: next(clock)) == 0
    out = capsys.readouterr().out
    assert "時間切れ" in out
    assert out.rstrip().endswith("--after 5")


def test_exits_silently_when_claude_pid_is_gone(temp_db, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_PID", "99999999")
    assert signal_wait.main(["--after", "0", "--db", temp_db], sleep=lambda _: None) == 0
    assert capsys.readouterr().out == ""
