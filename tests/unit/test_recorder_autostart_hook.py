"""hooks/recorder_autostart_hook.py のユニットテスト。

subprocess.Popen（記録役の起動）とrecorder_launcher_service.stop（古い
記録役の停止）を外部境界としてmonkeypatchし、呼び出しの有無・引数・順序を
検証する。実際のtmux/claudeプロセスは一切起動しない。
"""
import io
import json
import subprocess
from unittest.mock import patch

import pytest

import hooks.recorder_autostart_hook as hook
from hooks.hook_state import HookState
from hooks.recorder_watch import run_dir_for

_SID = "main-session-current"
_PID = 11111
_TRANSCRIPT = "/Users/x/.claude/projects/proj/main-session-current.jsonl"  # 実在しないパス


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    monkeypatch.setattr(HookState, "BASE_DIR", tmp_path / "state")
    monkeypatch.setenv("CALM_RECORDER", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ATTENDED", "1")
    monkeypatch.setenv("CLAUDE_PID", str(_PID))


@pytest.fixture
def calls(monkeypatch):
    """stop・Popenの呼び出しを1本のログに時系列で記録する（順序を検証するため）。"""
    log: list[tuple] = []

    def _fake_stop(*, session_id):
        log.append(("stop", session_id))
        return {"main_sid": session_id, "was_attached": True}

    def _fake_popen(cmd, **kwargs):
        log.append(("popen", cmd, kwargs))
        return None

    monkeypatch.setattr(hook, "recorder_stop", _fake_stop)
    monkeypatch.setattr(hook.subprocess, "Popen", _fake_popen)
    return log


def _stop_calls(log: list[tuple]) -> list[str]:
    return [c[1] for c in log if c[0] == "stop"]


def _popen_calls(log: list[tuple]) -> list[tuple]:
    return [(c[1], c[2]) for c in log if c[0] == "popen"]


def _write_run_json(sid_for_dir: str, *, main_sid: str, main_pid: int) -> None:
    run_dir = run_dir_for(sid_for_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(
        json.dumps({"main_sid": main_sid, "main_pid": main_pid}), encoding="utf-8"
    )


def _run_hook(*, session_id: str = _SID, transcript_path: str = _TRANSCRIPT, cwd: str = "/some/cwd"):
    payload = {
        "session_id": session_id,
        "transcript_path": transcript_path,
        "cwd": cwd,
        "hook_event_name": "SessionStart",
        "source": "startup",
    }
    fake_stdin = io.StringIO(json.dumps(payload))
    with patch.object(hook.sys, "stdin", fake_stdin):
        return hook.main()


class TestGating:
    """spec 1・5: CALM_RECORDER・対話判定のゲート。"""

    def test_noop_when_calm_recorder_unset(self, monkeypatch, calls):
        monkeypatch.delenv("CALM_RECORDER", raising=False)
        code = _run_hook()
        assert code == 0
        assert calls == []

    def test_noop_when_calm_recorder_not_one(self, monkeypatch, calls):
        monkeypatch.setenv("CALM_RECORDER", "true")
        code = _run_hook()
        assert code == 0
        assert calls == []

    def test_noop_when_unattended(self, monkeypatch, calls):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ATTENDED", "0")
        code = _run_hook()
        assert code == 0
        assert calls == []

    def test_noop_when_attended_flag_missing(self, monkeypatch, calls):
        """claude -p / --bgはCLAUDE_CODE_SESSION_ATTENDED="0"を実機確認済みだが、
        欠落時も安全側（何もしない）に倒す。"""
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ATTENDED", raising=False)
        code = _run_hook()
        assert code == 0
        assert calls == []

    def test_noop_when_claude_pid_missing(self, monkeypatch, calls):
        monkeypatch.delenv("CLAUDE_PID", raising=False)
        code = _run_hook()
        assert code == 0
        assert calls == []

    def test_noop_when_transcript_path_missing(self, calls):
        code = _run_hook(transcript_path="")
        assert code == 0
        assert calls == []


class TestSpawnsStart:
    """spec 2: 通常起動時、切り離しプロセスとしてrecorder.py startを呼ぶ。"""

    def test_fresh_session_spawns_detached_start_with_expected_args(self, calls):
        code = _run_hook()

        assert code == 0
        assert _stop_calls(calls) == []  # 一致するstale run.jsonが無いので停止は起きない
        popen = _popen_calls(calls)
        assert len(popen) == 1
        cmd, kwargs = popen[0]
        assert cmd[0].endswith(".venv/bin/python")
        assert cmd[1].endswith("scripts/recorder.py")
        assert cmd[2:] == [
            "start",
            "--session-id", _SID,
            "--pid", str(_PID),
            "--transcript", _TRANSCRIPT,
            "--from-start",  # _TRANSCRIPTは実在しないパスなので付く
        ]
        assert kwargs["start_new_session"] is True
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["stdout"] is kwargs["stderr"]
        assert kwargs["stdout"].name == str(run_dir_for(_SID) / "autostart.log")

    def test_existing_transcript_omits_from_start(self, tmp_path, calls):
        """resumeのように既存のtranscriptがある場合、過去の会話全体を読み直さない
        よう`--from-start`を付けない（既定の「起動時点の末尾から」に任せる）。"""
        real_transcript = tmp_path / "resumed.jsonl"
        real_transcript.write_text('{"type": "user"}\n', encoding="utf-8")

        _run_hook(transcript_path=str(real_transcript))

        cmd, _ = _popen_calls(calls)[0]
        assert "--from-start" not in cmd
        assert cmd[cmd.index("--transcript") + 1] == str(real_transcript)

    def test_calm_recorder_is_stripped_from_spawned_env(self, calls):
        """spec 4: 記録役自身に記録役が付かないよう、起動env自体からは取り除く。"""
        _run_hook()

        _, kwargs = _popen_calls(calls)[0]
        assert "CALM_RECORDER" not in kwargs["env"]
        # 他のenvはそのまま引き継ぐ（stripが過剰でないことの確認）
        assert kwargs["env"]["CLAUDE_PID"] == str(_PID)


class TestStaleDetection:
    """spec 3: /clear・resumeで古い記録役を止めてから付け直す。"""

    def test_clear_same_pid_different_sid_stops_old_then_starts_new(self, calls):
        """/clear: main_pidは同じプロセスのまま、session_idだけ変わる。"""
        old_sid = "main-session-before-clear"
        _write_run_json(old_sid, main_sid=old_sid, main_pid=_PID)

        code = _run_hook(session_id=_SID)

        assert code == 0
        assert [c[0] for c in calls] == ["stop", "popen"]  # 停止してから起動する順序
        assert _stop_calls(calls) == [old_sid]
        cmd, _ = _popen_calls(calls)[0]
        assert cmd[cmd.index("--session-id") + 1] == _SID  # 新しいsidで起動する（古いsidではない）

    def test_resume_same_sid_different_pid_stops_then_restarts_same_sid(self, calls):
        """resume: session_idは維持され、main_pidだけ新しいプロセスのものに変わる。"""
        _write_run_json(_SID, main_sid=_SID, main_pid=99999)  # 旧プロセスのpid

        code = _run_hook(session_id=_SID)

        assert code == 0
        assert [c[0] for c in calls] == ["stop", "popen"]  # 停止してから起動する順序
        assert _stop_calls(calls) == [_SID]
        cmd, _ = _popen_calls(calls)[0]
        assert cmd[cmd.index("--session-id") + 1] == _SID
        assert cmd[cmd.index("--pid") + 1] == str(_PID)  # 新しいpidで起動する

    def test_compact_same_pid_same_sid_does_not_stop_anything(self, calls):
        """compact: main_pid・session_idともに不変。stale判定に一致しないため
        stopは呼ばれず、start()自体の二重起動防止（別テストで担保済み）に委ねる。"""
        _write_run_json(_SID, main_sid=_SID, main_pid=_PID)  # 現在と完全一致 = staleではない

        code = _run_hook(session_id=_SID)

        assert code == 0
        assert _stop_calls(calls) == []
        assert len(_popen_calls(calls)) == 1

    def test_unrelated_run_json_for_other_pid_and_sid_is_left_alone(self, calls):
        """自分と無関係な(別pid・別sid)の記録役には触れない。"""
        _write_run_json("someone-elses-session", main_sid="someone-elses-session", main_pid=22222)

        _run_hook(session_id=_SID)

        assert _stop_calls(calls) == []
        assert len(_popen_calls(calls)) == 1

    def test_stop_failure_does_not_block_start(self, monkeypatch, calls):
        """古い記録役の停止が失敗しても、新しい記録役の起動は妨げない。"""
        old_sid = "main-session-before-clear"
        _write_run_json(old_sid, main_sid=old_sid, main_pid=_PID)

        def _raising_stop(*, session_id):
            raise RuntimeError("tmux kill-session failed")

        monkeypatch.setattr(hook, "recorder_stop", _raising_stop)

        code = _run_hook(session_id=_SID)

        assert code == 0
        assert len(_popen_calls(calls)) == 1


class TestRecorderOwnDirGuard:
    """spec 4補強: 記録役自身のrun_dir配下で起動された場合は無条件でスキップする。

    tmuxサーバーが既存の場合、CALM_RECORDERのenv除去だけでは記録役自身の
    セッションへの伝播を防げないことを実機確認済み（モジュールdocstring参照）
    ため、cwdが記録役のrun_dir配下かで機械的に見分ける二重の防御。run.json
    の有無では判定しない（tmuxセッション起動後・run.json書き込み前の窓で
    run_dir自体は既に存在するため）。
    """

    def test_skips_when_cwd_is_under_a_recorder_run_dir(self, calls):
        recorder_run_dir = run_dir_for("some-recorder-sid")
        recorder_run_dir.mkdir(parents=True, exist_ok=True)
        # run.jsonはまだ無い（tmux起動直後・書き込み前の窓を模す）が、
        # run_dir配下というだけでスキップされるべき。

        code = _run_hook(cwd=str(recorder_run_dir))

        assert code == 0
        assert calls == []

    def test_does_not_skip_for_an_unrelated_cwd(self, tmp_path, calls):
        plain_dir = tmp_path / "some_project_dir"
        plain_dir.mkdir(parents=True)

        _run_hook(cwd=str(plain_dir))

        assert len(_popen_calls(calls)) == 1


class TestErrorHandling:
    """spec 2: 何が起きてもexit 0にし、stdoutを汚さない。"""

    def test_malformed_hook_input_exits_zero(self):
        with patch.object(hook.sys, "stdin", io.StringIO("not json")):
            code = hook.main()
        assert code == 0

    def test_corrupt_existing_run_json_is_skipped_not_raised(self, calls):
        run_dir = run_dir_for("corrupt-sid")
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run.json").write_text("{not valid json", encoding="utf-8")

        code = _run_hook()

        assert code == 0
        assert _stop_calls(calls) == []
        assert len(_popen_calls(calls)) == 1

    def test_emits_nothing_to_stdout(self, calls):
        fake_stdout = io.StringIO()
        with patch.object(hook.sys, "stdout", fake_stdout):
            _run_hook()
        assert fake_stdout.getvalue() == ""
