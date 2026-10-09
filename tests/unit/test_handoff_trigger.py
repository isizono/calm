"""scripts/handoff_trigger.py の単体テスト。"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.handoff_trigger import judge  # noqa: E402

_SESSION_START = "2026-10-09T00:00:00.000Z"
_SESSION_START_EPOCH = 1791504000.0  # 2026-10-09T00:00:00Z


def _assistant(usage: dict, ts: str = _SESSION_START) -> dict:
    return {"type": "assistant", "timestamp": ts, "message": {"usage": usage}}


def _usage(total: int) -> dict:
    # input_tokensにすべて載せ、cache系は0にして合計を厳密に制御する
    return {"input_tokens": total, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}


class TestJudgeContext:
    def test_not_triggered_at_exact_threshold(self):
        lines = [_assistant(_usage(300_000))]
        r = judge(lines, role="holder", threshold=300_000, skill_mtime=None)
        assert r["trigger"] is False
        assert r["reasons"] == []
        assert r["context_tokens"] == 300_000

    def test_triggered_just_above_threshold(self):
        lines = [_assistant(_usage(300_001))]
        r = judge(lines, role="holder", threshold=300_000, skill_mtime=None)
        assert r["trigger"] is True
        assert r["reasons"] == ["context"]

    def test_uses_last_usage_and_skips_rows_without_usage(self):
        lines = [
            _assistant(_usage(400_000)),
            {"type": "assistant", "timestamp": _SESSION_START, "message": {}},  # usage無し
            {"type": "user", "timestamp": _SESSION_START},  # usageキー自体無し
            _assistant(_usage(100)),
        ]
        r = judge(lines, role="holder", threshold=300_000, skill_mtime=None)
        # usage無しの行はcontext_tokensを更新しない。最後に有効なusageだった100が残る
        assert r["context_tokens"] == 100
        assert r["trigger"] is False

    def test_advisor_iteration_sum_does_not_double_count(self):
        # advisorを含む呼び出しは、トップレベルのusageがiterationsの合計になり
        # 実際のコンテキストサイズの約2倍に見える(資材「引き継ぎ区間の読み込み
        # 内訳(10/09)」で実測: トップレベル316,186、実際158,867)。最後の
        # type="message"のiterationの値を使うべきで、トップレベルの合計では
        # 300,000を超えて誤ってtriggerしてはいけない
        usage = {
            "input_tokens": 300_002,  # トップレベル(iterationsの合計、2倍)
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "iterations": [
                {"type": "message", "input_tokens": 150_001,
                 "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                {"type": "message", "input_tokens": 150_001,
                 "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            ],
        }
        r = judge([_assistant(usage)], role="holder", threshold=300_000, skill_mtime=None)
        assert r["context_tokens"] == 150_001
        assert r["trigger"] is False

    def test_iterations_without_type_message_falls_back_to_last_entry(self):
        usage = {
            "input_tokens": 999,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "iterations": [
                {"type": "tool_use", "input_tokens": 1,
                 "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            ],
        }
        r = judge([_assistant(usage)], role="holder", threshold=300_000, skill_mtime=None)
        assert r["context_tokens"] == 1


class TestJudgeCompact:
    def test_triggered_when_compact_summary_present(self):
        lines = [
            {"type": "user", "timestamp": _SESSION_START, "isCompactSummary": True},
            _assistant(_usage(1)),
        ]
        r = judge(lines, role="holder", threshold=300_000, skill_mtime=None)
        assert r["compacted"] is True
        assert r["trigger"] is True
        assert "compact" in r["reasons"]

    def test_not_triggered_when_no_compact_summary(self):
        lines = [_assistant(_usage(1))]
        r = judge(lines, role="holder", threshold=300_000, skill_mtime=None)
        assert r["compacted"] is False


class TestJudgeSkillVersion:
    def test_holder_triggered_when_skill_changed_after_session_start(self):
        lines = [_assistant(_usage(1), ts=_SESSION_START)]
        r = judge(lines, role="holder", threshold=300_000,
                   skill_mtime=_SESSION_START_EPOCH + 3600)
        assert r["skill_changed"] is True
        assert r["trigger"] is True
        assert "skill_version" in r["reasons"]

    def test_holder_not_triggered_when_skill_changed_before_session_start(self):
        lines = [_assistant(_usage(1), ts=_SESSION_START)]
        r = judge(lines, role="holder", threshold=300_000,
                   skill_mtime=_SESSION_START_EPOCH - 3600)
        assert r["skill_changed"] is False
        assert r["trigger"] is False

    def test_consultant_ignores_skill_version_entirely(self):
        lines = [_assistant(_usage(1), ts=_SESSION_START)]
        r = judge(lines, role="consultant", threshold=300_000,
                   skill_mtime=_SESSION_START_EPOCH + 3600)
        assert r["skill_changed"] is None
        assert "skill_version" not in r["reasons"]
        assert r["trigger"] is False

    def test_skill_changed_none_when_mtime_missing(self):
        lines = [_assistant(_usage(1), ts=_SESSION_START)]
        r = judge(lines, role="holder", threshold=300_000, skill_mtime=None)
        assert r["skill_changed"] is False
