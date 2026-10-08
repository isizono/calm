"""scripts/orch_liveness.py の単体テスト。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.orch_liveness import find_transcript, judge  # noqa: E402

NOW = 10_000.0


def _row(**kw):
    return {"sessionId": "s1", "pid": 100, "status": "idle", "startedAt": 1, **kw}


class TestJudge:
    def test_dead_when_no_row_has_pid(self):
        rows = [_row(pid=None), _row(sessionId="other")]
        assert judge(rows, "s1", NOW, NOW, 25)["verdict"] == "DEAD"

    def test_stuck_when_busy_and_transcript_older_than_threshold(self):
        r = judge([_row(status="busy")], "s1", NOW - 25 * 60, NOW, 25)
        assert r["verdict"] == "STUCK"
        assert r["transcript_age_min"] == 25.0

    def test_ok_when_busy_but_transcript_recent(self):
        assert judge([_row(status="busy")], "s1", NOW - 24 * 60, NOW, 25)["verdict"] == "OK"

    def test_ok_when_idle_even_if_transcript_old(self):
        assert judge([_row(status="idle")], "s1", NOW - 600 * 60, NOW, 25)["verdict"] == "OK"

    def test_ok_when_transcript_missing(self):
        assert judge([_row(status="busy")], "s1", None, NOW, 25)["verdict"] == "OK"

    def test_newest_started_row_is_judged(self):
        # /resume後は同じsessionIdの行が並ぶ。新しく開いた側が今の窓口
        rows = [_row(pid=1, status="busy", startedAt=1), _row(pid=2, status="idle", startedAt=2)]
        r = judge(rows, "s1", NOW - 600 * 60, NOW, 25)
        assert (r["verdict"], r["pid"]) == ("OK", 2)


def test_find_transcript_picks_newest_across_projects(tmp_path):
    old = tmp_path / "a" / "s1.jsonl"
    new = tmp_path / "b" / "s1.jsonl"
    for i, p in enumerate((old, new)):
        p.parent.mkdir()
        p.write_text("")
        os.utime(p, (i, i))
    assert find_transcript("s1", tmp_path) == new
    assert find_transcript("s2", tmp_path) is None
