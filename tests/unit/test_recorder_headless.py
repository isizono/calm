"""hooks/recorder_headless.py の単体テスト。

外部境界として差し替えるのは、`subprocess.run`（claude -p と tmux）・
メインの生死判定（`recorder_watch.process_start_signature`）・`run()`に渡す
`sleep`・HOME（記録役のtranscriptの置き場所）だけ。片の切り出し・DONEの受理・
cursorの更新は実装をそのまま通す。
"""
import json
import subprocess

import pytest

pytest.importorskip("fcntl")

from hooks import recorder_headless as headless  # noqa: E402
from hooks import recorder_watch as watch  # noqa: E402
from hooks.hook_state import HookState  # noqa: E402

_STARTED_AT = "Thu Jul 24 09:32:04 2026"
_MAIN_SID = "main-h"


def _entry(kind: str, uuid: str, *, text: str = "", tool=None, tool_input=None, tool_id=None) -> dict:
    content = []
    if text:
        content.append({"type": "text", "text": text})
    if tool:
        content.append({"type": "tool_use", "name": tool, "input": tool_input or {}, "id": tool_id or f"tu-{uuid}"})
    return {"type": kind, "uuid": uuid, "message": {"content": content}}


def _tool_result(uuid: str, tool_use_id: str, text: str) -> dict:
    content = [{"type": "tool_result", "tool_use_id": tool_use_id, "content": [{"type": "text", "text": text}]}]
    return {"type": "user", "uuid": uuid, "message": {"content": content}}


def _write_jsonl(path, entries) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


class _World:
    """メインの生死・claude -pの応答・tmux呼び出しをまとめて持つ。"""

    def __init__(self, home):
        self.home = home
        self.main_alive = True
        self.responses: list = []
        self.claude_calls: list[dict] = []
        self.tmux_calls: list[list[str]] = []
        self.sleeps: list[float] = []
        self.on_claude = None

    def run(self, cmd, **kwargs):
        if cmd[0] == "tmux":
            self.tmux_calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)
        assert cmd[0] == "claude", cmd
        sid = cmd[cmd.index("--session-id") + 1]
        self.claude_calls.append({"cmd": cmd, "input": kwargs["input"], "sid": sid, "env": kwargs["env"]})
        if self.on_claude:
            self.on_claude(sid)
        resp = self.responses.pop(0)
        if not self.responses:
            # 最後の応答を返したら、メインを死なせてループを終わらせる
            self.main_alive = False
        if isinstance(resp, Exception):
            raise resp
        return resp

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        assert len(self.sleeps) < 200, "ループが終わらない"


def _ok(text: str, denials=None) -> subprocess.CompletedProcess:
    body = {"type": "result", "is_error": False, "result": text,
            "permission_denials": denials or [], "total_cost_usd": 0.01}
    return subprocess.CompletedProcess(["claude"], 0, stdout=json.dumps(body), stderr="")


def _failed(code: int = 1) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["claude"], code, stdout="", stderr="boom")


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(HookState, "BASE_DIR", tmp_path / "state")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CALM_DB_PATH", str(tmp_path / "no-such-db.sqlite"))
    monkeypatch.setenv("CALM_RECORDER", "headless")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _MAIN_SID)
    monkeypatch.setattr(watch, "CHAR_THRESHOLD", 3)
    w = _World(tmp_path / "home")
    monkeypatch.setattr(subprocess, "run", w.run)
    monkeypatch.setattr(watch, "process_start_signature", lambda pid: _STARTED_AT if w.main_alive else None)
    return w


def _setup_run(tmp_path, lines):
    run_dir = watch.run_dir_for(_MAIN_SID)
    run_dir.mkdir(parents=True, exist_ok=True)
    transcript = tmp_path / "main.jsonl"
    _write_jsonl(transcript, lines)
    (run_dir / "run.json").write_text(json.dumps({
        "main_sid": _MAIN_SID, "main_pid": 999, "main_pid_started_at": _STARTED_AT,
        "main_transcript": str(transcript), "recorder_sids": [],
    }), encoding="utf-8")
    (run_dir / "mcp.json").write_text("{}", encoding="utf-8")
    return run_dir


def _cursor(run_dir) -> dict:
    return json.loads((run_dir / "cursor.json").read_text(encoding="utf-8"))


def _log(run_dir) -> list[dict]:
    return [json.loads(line) for line in (run_dir / "headless.log").read_text(encoding="utf-8").splitlines()]


class TestOneCallPerChunk:
    def test_done_confirms_chunk_and_main_death_terminates(self, tmp_path, world):
        run_dir = _setup_run(tmp_path, [_entry("assistant", "e1", text="hello world")])
        world.responses = [_ok("記録した\nDONE 0001")]

        assert headless.run(run_dir, sleep=world.sleep) == 0

        assert len(world.claude_calls) == 1
        call = world.claude_calls[0]
        assert call["input"].startswith("片0001を処理せよ")
        assert "hello world" in call["input"].split(headless.HEADLESS_PROMPT_TEMPLATE.format(no=1))[1]
        assert "CALM_RECORDER" not in call["env"]
        assert "CLAUDE_CODE_SESSION_ID" not in call["env"]

        cursor = _cursor(run_dir)
        assert cursor["pending"] is None
        assert cursor["last_uuid"] == "e1"
        assert cursor["unacked"] == []
        run_data = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        assert run_data["recorder_sids"] == [call["sid"]]
        assert [r["outcome"] for r in _log(run_dir)] == ["done"]
        assert world.tmux_calls == [["tmux", "kill-session", "-t", watch.tmux_session_name(_MAIN_SID)]]

    def test_permission_denial_refuses_done_and_resends(self, tmp_path, world):
        run_dir = _setup_run(tmp_path, [_entry("assistant", "e1", text="hello world")])
        denial = {"tool_name": "mcp__calm__check_in", "tool_use_id": "x", "tool_input": {}}
        world.responses = [_ok("DONE 0001", denials=[denial]), _ok("DONE 0001")]

        headless.run(run_dir, sleep=world.sleep)

        assert len(world.claude_calls) == 2
        assert world.claude_calls[0]["sid"] != world.claude_calls[1]["sid"]
        assert _cursor(run_dir)["unacked"] == []
        log = _log(run_dir)
        assert [r["outcome"] for r in log] == ["retry", "done"]
        assert log[0]["denials"] == [denial]

    def test_missing_done_twice_goes_unacked_and_advances(self, tmp_path, world):
        run_dir = _setup_run(tmp_path, [_entry("assistant", "e1", text="hello world")])
        world.responses = [_ok("まだ"), _ok("まだ2")]

        headless.run(run_dir, sleep=world.sleep)

        cursor = _cursor(run_dir)
        assert cursor["unacked"] == [1]
        assert cursor["last_uuid"] == "e1"


class TestLaunchFailure:
    def test_launch_failures_back_off_without_using_the_done_retry(self, tmp_path, world):
        run_dir = _setup_run(tmp_path, [_entry("assistant", "e1", text="hello world")])
        world.responses = [_failed(), subprocess.TimeoutExpired("claude", 600), _ok("DONE 0001")]

        headless.run(run_dir, sleep=world.sleep)

        assert len(world.claude_calls) == 3
        cursor = _cursor(run_dir)
        assert cursor["unacked"] == []
        assert cursor["pending"] is None
        assert [r["outcome"] for r in _log(run_dir)] == ["launch_failed", "launch_failed", "done"]
        # 1回目の失敗のあと30秒、2回目のあと60秒待つ（POLL_INTERVAL刻み）
        waited = sum(world.sleeps[: (30 + 60) // watch.POLL_INTERVAL_SECONDS])
        assert waited == 90

    def test_gives_up_after_limit_and_marks_unacked(self, tmp_path, world):
        run_dir = _setup_run(tmp_path, [_entry("assistant", "e1", text="hello world")])
        world.responses = [_failed() for _ in range(headless.LAUNCH_FAILURE_LIMIT)]

        headless.run(run_dir, sleep=world.sleep)

        assert len(world.claude_calls) == headless.LAUNCH_FAILURE_LIMIT
        cursor = _cursor(run_dir)
        assert cursor["unacked"] == [1]
        assert cursor["last_uuid"] == "e1"

    def test_is_error_result_counts_as_launch_failure(self, tmp_path, world):
        run_dir = _setup_run(tmp_path, [_entry("assistant", "e1", text="hello world")])
        body = {"type": "result", "is_error": True, "result": "API Error: overloaded DONE 0001"}
        world.responses = [subprocess.CompletedProcess(["claude"], 0, stdout=json.dumps(body), stderr=""),
                           _ok("DONE 0001")]

        headless.run(run_dir, sleep=world.sleep)

        assert [r["outcome"] for r in _log(run_dir)] == ["launch_failed", "done"]


class TestResumeAndUnlinked:
    def test_pending_left_by_previous_process_is_sent_first(self, tmp_path, world):
        run_dir = _setup_run(tmp_path, [_entry("assistant", "e1", text="hello world")])
        (run_dir / "chunks").mkdir()
        (run_dir / "chunks" / "0007.md").write_text("# 片 0007\n\n前回の続き\n", encoding="utf-8")
        size = (tmp_path / "main.jsonl").stat().st_size
        cursor = dict(watch._DEFAULT_CURSOR, next_no=8, pending={
            "no": 7, "end_uuid": "e1", "end_offset": size, "end_activity_id": 5, "retries": 0,
        })
        (run_dir / "cursor.json").write_text(json.dumps(cursor), encoding="utf-8")
        world.responses = [_ok("DONE 0007")]

        headless.run(run_dir, sleep=world.sleep)

        assert "前回の続き" in world.claude_calls[0]["input"]
        assert _cursor(run_dir)["last_uuid"] == "e1"

    def test_material_without_related_in_orphan_chunk_is_collected(self, tmp_path, world):
        run_dir = _setup_run(tmp_path, [_entry("assistant", "e1", text="orphan work")])

        def _write_own_transcript(sid):
            _write_jsonl(world.home / ".claude" / "projects" / "rec" / f"{sid}.jsonl", [
                _entry("assistant", "r1", tool="mcp__calm__add_material", tool_id="m1",
                       tool_input={"title": "t", "related": [{"type": "topic", "ids": [1]}]}),
                _tool_result("r1r", "m1", json.dumps({"material_id": 10})),
                _entry("assistant", "r2", tool="mcp__calm__add_material", tool_id="m2", tool_input={"title": "t2"}),
                _tool_result("r2r", "m2", json.dumps({"material_id": 55})),
            ])

        world.on_claude = _write_own_transcript
        world.responses = [_ok("DONE 0001")]

        headless.run(run_dir, sleep=world.sleep)

        assert _cursor(run_dir)["unlinked_materials"] == [55]


class TestEvidenceGate:
    def test_rejected_done_is_resent_with_missing_evidence_note(self, tmp_path, world):
        run_dir = _setup_run(tmp_path, [_entry("assistant", "e1", text="hello world")])
        attempts = []

        def _write_own_transcript(sid):
            attempts.append(sid)
            entries = [_entry("assistant", "r0", text="読んだ")]
            if len(attempts) == 2:
                entries += [
                    _entry("assistant", "r1", tool="mcp__calm__add_logs", tool_id="w1", tool_input={"items": []}),
                    _tool_result("r1r", "w1", json.dumps({"created": [{"log_id": 7}], "errors": []})),
                ]
            _write_jsonl(world.home / ".claude" / "projects" / "rec" / f"{sid}.jsonl", entries)

        world.on_claude = _write_own_transcript
        world.responses = [_ok("DONE 0001"), _ok("DONE 0001")]

        headless.run(run_dir, sleep=world.sleep)

        first, second = (c["input"] for c in world.claude_calls)
        assert first.startswith("片0001を処理せよ")
        note, rest = second.split("\n", 1)
        assert note.startswith("片0001のDONEは受け付けていない（不足: ")
        assert rest.startswith("片0001を処理せよ")
        cursor = _cursor(run_dir)
        assert cursor["unacked"] == []
        assert cursor["pending"] is None
        assert [r["outcome"] for r in _log(run_dir)] == ["retry", "done"]


class TestMain:
    def test_missing_run_json_exits_1(self, tmp_path, world):
        assert headless.main([str(tmp_path / "nowhere")]) == 1

    def test_lock_already_held_exits_0_without_calling_claude(self, tmp_path, world):
        run_dir = _setup_run(tmp_path, [_entry("assistant", "e1", text="hello world")])
        held = watch._acquire_lock(run_dir)
        try:
            assert headless.main([str(run_dir)]) == 0
        finally:
            held.close()
        assert world.claude_calls == []
