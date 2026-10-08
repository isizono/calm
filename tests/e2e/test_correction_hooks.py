"""人の訂正の印・記録役の起こし直し・未教訓化の注意とblockのE2Eテスト。

hookはサブプロセスで起動し、DBはtemp_db（実際の書き込み経路で仮データを作る）を使う。
"""
import json
from pathlib import Path

import pytest

from hooks.correction_marks import MARKS_FILE, marks_from_transcript, read_mark_ids
from hooks.hook_state import HookState
from hooks.recorder_watch import _Line, _render_chunk
from src.harness.claude_code import ClaudeCodeHarness
from src.services.activity_service import add_activity
from src.services.correction_service import UNLEARNED_CORRECTION_TAG
from src.services.material_service import add_material
from tests.helpers import run_hook_subprocess

SID = "corr-test-session"
TAGS = ["domain:test"]


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(HookState, "BASE_DIR", tmp_path)
    return tmp_path


def _ups(state_dir: Path, prompt: str, prompt_id: str = "p-1") -> dict:
    payload = {"session_id": SID, "prompt": prompt, "prompt_id": prompt_id, "transcript_path": "/nonexistent"}
    result = run_hook_subprocess(
        "hooks/user_prompt_submit_hook.py", json.dumps(payload),
        extra_env={"HOOK_STATE_DIR": str(state_dir), "CALM_RECORDER": "0"},
    )
    return json.loads(result.stdout.strip() or "{}")


def _context(out: dict) -> str:
    return (out.get("hookSpecificOutput") or {}).get("additionalContext", "")


def _activity_with_correction(title="未教訓化: 予告で止まる") -> tuple[int, int]:
    aid = add_activity(title="[作業] x", description="d", tags=TAGS, check_in=False)["activity_id"]
    mid = add_material(title, "本文", TAGS + [UNLEARNED_CORRECTION_TAG], "recorder",
                       related=[{"type": "activity", "ids": [aid]}])["material_id"]
    return aid, mid


class TestMarks:
    def test_human_prompt_is_marked_in_recorder_run_dir(self, state_dir, temp_db):
        _ups(state_dir, "え、でなんで止まってるの", "p-human")
        assert read_mark_ids(state_dir / "recorder_runs" / SID / MARKS_FILE) == {"p-human"}

    def test_relayed_message_is_not_marked(self, state_dir, temp_db):
        _ups(state_dir, '<cross-session-message from="x">周知</cross-session-message>', "p-relay")
        assert not (state_dir / "recorder_runs" / SID / MARKS_FILE).exists()

    def test_marks_from_transcript_take_only_human_prompts(self, tmp_path):
        rows = [
            {"type": "user", "promptId": "a", "message": {"role": "user", "content": "再発防止策は立てた？"}},
            {"type": "user", "promptId": "a", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t", "content": "ok"}]}},
            {"type": "user", "promptId": "b", "isMeta": True, "message": {"role": "user", "content": "meta"}},
            {"type": "user", "promptId": "c", "message": {"role": "user", "content": [
                {"type": "text", "text": "<task-notification>done</task-notification>"}]}},
        ]
        path = tmp_path / "t.jsonl"
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")

        assert [m["prompt_id"] for m in marks_from_transcript(path)] == ["a"]


class TestChunkLabel:
    def test_only_the_marked_human_prompt_line_gets_the_label(self):
        human = {"type": "user", "promptId": "a", "timestamp": "2026-10-08T16:14:09Z",
                 "message": {"role": "user", "content": "なんで止まってるの"}}
        result = {"type": "user", "promptId": "a",
                  "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "x"}]}}
        unmarked = {"type": "user", "promptId": "z", "message": {"role": "user", "content": "続けて"}}
        lines = [_Line(raw=r, entry=ClaudeCodeHarness.to_entry(r), end_offset=0) for r in (human, result, unmarked)]

        text = _render_chunk(1, 5, [], None, None, lines, mark_ids={"a"})

        assert text.count("[訂正候補 prompt_id=a at=2026-10-08T16:14:09Z]") == 1
        assert "[訂正候補 prompt_id=a at=2026-10-08T16:14:09Z]\n[user] なんで止まってるの" in text
        assert "prompt_id=z" not in text


class TestWindowNotice:
    def test_new_unresolved_correction_is_noticed_once(self, state_dir, temp_db):
        aid, _ = _activity_with_correction()
        HookState(SID).set_checked_in_activity(aid)

        first = _context(_ups(state_dir, "次やって", "p-1"))
        second = _context(_ups(state_dir, "その次", "p-2"))

        assert "未教訓化として1件" in first and "予告で止まる" in first
        assert "未教訓化" not in second

    def test_no_notice_without_checked_in_activity(self, state_dir, temp_db):
        _activity_with_correction()
        assert "未教訓化" not in _context(_ups(state_dir, "次やって"))


class TestDelegateStop:
    def _stop(self, state_dir: Path, activity_id: int, last_tools: list[str]) -> dict:
        transcript = state_dir / "transcript.jsonl"
        entries = [
            {"type": "user", "message": {"role": "user", "content": "hi"}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "c1", "name": "mcp__plugin_calm_calm__check_in",
                 "input": {"activity_id": activity_id}},
                {"type": "tool_use", "id": "c2", "name": "mcp__plugin_calm_calm__add_logs", "input": {"items": []}},
            ]}},
            {"type": "user", "message": {"role": "user", "content": "next"}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                *({"type": "tool_use", "id": f"t{i}", "name": n, "input": {"to": "orch", "message": "m"}}
                  for i, n in enumerate(last_tools)),
                {"type": "text", "text": "終えた"},
            ]}},
        ]
        transcript.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n", encoding="utf-8")
        (state_dir / "delegate").mkdir(exist_ok=True)
        (state_dir / "delegate" / str(activity_id)).write_text("1")
        result = run_hook_subprocess(
            "hooks/stop_hook.py", json.dumps({"transcript_path": str(transcript), "session_id": SID}),
            extra_env={"HOOK_STATE_DIR": str(state_dir)},
        )
        return json.loads(result.stdout.strip())

    def test_delegate_holding_unresolved_is_blocked_once_per_item(self, state_dir, temp_db):
        aid, _ = _activity_with_correction()

        first = self._stop(state_dir, aid, ["SendMessage"])
        assert first["decision"] == "block" and "予告で止まる" in first["reason"]
        assert self._stop(state_dir, aid, ["SendMessage"]) == {}
        assert self._stop(state_dir, aid, ["SendMessage"]) == {}

        add_material("未教訓化: 二件目", "本文", TAGS + [UNLEARNED_CORRECTION_TAG], "recorder",
                     related=[{"type": "activity", "ids": [aid]}])
        again = self._stop(state_dir, aid, ["SendMessage"])
        assert again["decision"] == "block" and "二件目" in again["reason"]

    def test_combined_with_no_wake_in_one_block(self, state_dir, temp_db):
        aid, _ = _activity_with_correction()

        result = self._stop(state_dir, aid, [])

        assert result["decision"] == "block"
        assert "予告で止まる" in result["reason"] and "ScheduleWakeup" in result["reason"]

