"""scripts/orch_liveness.py の単体テスト。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.orch_liveness import (  # noqa: E402
    TURN_END,
    find_transcript,
    judge,
    transcript_tail,
)

NOW = 10_000.0


def _row(**kw):
    return {"sessionId": "s1", "pid": 100, "status": "idle", "startedAt": 1, **kw}


def _msg(role, block):
    return {"type": role, "timestamp": "2026-10-09T01:20:16.742Z",
            "message": {"role": role, "content": [block]}}


# ターンの終わり: Stop hookの結果 → その要約 → turn_duration
_TURN_END_ROWS = [
    {"type": "attachment", "timestamp": "t", "attachment": {"type": "hook_success", "hookName": "Stop"}},
    {"type": "system", "subtype": "stop_hook_summary", "timestamp": "t", "hookCount": 2},
    {"type": "system", "subtype": "turn_duration", "timestamp": "t", "durationMs": 18161},
]


def _jsonl(path, *rows):
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    return path


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

    def test_turn_ended_but_busy_is_waiting_not_stuck(self):
        # 裏のshellがstatusをbusyに保っていても、ターンが終わっていれば待機中
        r = judge([_row(status="busy")], "s1", NOW - 60 * 60, NOW, 25, tail=TURN_END)
        assert r["verdict"] == "OK"

    def test_mid_turn_tail_keeps_stuck(self):
        r = judge([_row(status="busy")], "s1", NOW - 26 * 60, NOW, 25, tail="assistant/tool_use")
        assert r["verdict"] == "STUCK"


class TestTranscriptTail:
    def test_lab76_turn_ended_then_bookkeeping_lines(self, tmp_path):
        # 教訓化役lab-76(10/09 01:46 UTC、26.1分無更新): 文で終え、timestampの無い帳簿行が続く
        p = _jsonl(tmp_path / "s.jsonl",
                   _msg("assistant", {"type": "text", "text": "今回の手番では、新しく返すべきものも無かった"}),
                   *_TURN_END_ROWS,
                   {"type": "bridge-session", "sessionId": "s1"},
                   {"type": "cost-state", "totalCostUSD": 12.87},
                   {"type": "last-prompt", "lastPrompt": "あなたは教訓化役の後継セッションです"},
                   {"type": "cost-state", "totalCostUSD": 12.87})
        tail = transcript_tail(p)
        assert tail == TURN_END
        assert judge([_row(status="busy")], "s1", NOW - 26.1 * 60, NOW, 25, tail=tail)["verdict"] == "OK"

    def test_workspace_d8_turn_ended_after_thinking(self, tmp_path):
        # 教訓化役workspace-d8(10/09 02:39 UTC、25.7分無更新): thinking→文→turn_durationで締まる
        p = _jsonl(tmp_path / "s.jsonl",
                   _msg("assistant", {"type": "thinking", "thinking": "…"}),
                   _msg("assistant", {"type": "text", "text": "報告した"}),
                   *_TURN_END_ROWS)
        tail = transcript_tail(p)
        assert tail == TURN_END
        assert judge([_row(status="busy")], "s1", NOW - 25.7 * 60, NOW, 25, tail=tail)["verdict"] == "OK"

    def test_idle_notices_after_turn_end_are_not_progress(self, tmp_path):
        # 待機中に届くidle購読の知らせ(system/informational)やqueue-operationはターンの進みではない
        p = _jsonl(tmp_path / "s.jsonl",
                   _msg("assistant", {"type": "text", "text": "x"}),
                   *_TURN_END_ROWS,
                   {"type": "system", "subtype": "informational", "timestamp": "t",
                    "content": "A process asked to be told when this session is next idle"},
                   {"type": "queue-operation", "operation": "enqueue", "timestamp": "t"})
        assert transcript_tail(p) == TURN_END

    def test_hung_tool_call_is_mid_turn(self, tmp_path):
        p = _jsonl(tmp_path / "s.jsonl",
                   _msg("assistant", {"type": "tool_use", "name": "Bash", "input": {}}),
                   {"type": "attachment", "timestamp": "t",
                    "attachment": {"type": "hook_success", "hookName": "PreToolUse:Bash"}})
        tail = transcript_tail(p)
        assert tail == "assistant/tool_use"
        assert judge([_row(status="busy")], "s1", NOW - 26 * 60, NOW, 25, tail=tail)["verdict"] == "STUCK"

    def test_blocked_stop_hook_is_not_turn_end(self, tmp_path):
        # Stop hookがblockするとstop_hook_summaryの後にturn_durationが無いまま応答が続く
        p = _jsonl(tmp_path / "s.jsonl",
                   _msg("assistant", {"type": "text", "text": "x"}),
                   {"type": "system", "subtype": "stop_hook_summary", "timestamp": "t", "hookCount": 1})
        assert transcript_tail(p) == "assistant/text"

    def test_last_line_longer_than_first_read_chunk(self, tmp_path):
        p = _jsonl(tmp_path / "s.jsonl",
                   _msg("assistant", {"type": "tool_use", "name": "Read", "input": {}}),
                   _msg("user", {"type": "tool_result", "content": "x" * 200_000}))
        assert transcript_tail(p) == "user/tool_result"

    def test_empty_transcript_has_no_tail(self, tmp_path):
        p = tmp_path / "s.jsonl"
        p.write_text("")
        assert transcript_tail(p) is None


def test_find_transcript_picks_newest_across_projects(tmp_path):
    old = tmp_path / "a" / "s1.jsonl"
    new = tmp_path / "b" / "s1.jsonl"
    for i, p in enumerate((old, new)):
        p.parent.mkdir()
        p.write_text("")
        os.utime(p, (i, i))
    assert find_transcript("s1", tmp_path) == new
    assert find_transcript("s2", tmp_path) is None
