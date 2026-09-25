"""hooks/recorder_autostart_hook.py のユニットテスト。

subprocess.Popen（記録役の起動・古い記録役の停止はいずれも切り離した
`scripts/recorder.py`プロセスとして起動される）とps（`process_start_
signature`が内部で呼ぶ）を外部境界としてmonkeypatchし、呼び出しの
有無・引数・順序を検証する。実際のtmux/claudeプロセスは一切起動しない。
"""
import io
import json
import subprocess
from unittest.mock import patch

import pytest

import hooks.recorder_autostart_hook as hook
from hooks.hook_state import HookState
from hooks.recorder_watch import run_dir_for
from src.infra import process_signature

_SID = "main-session-current"
_PID = 11111
_TRANSCRIPT = "/Users/x/.claude/projects/proj/main-session-current.jsonl"  # 実在しないパス

# process_start_signature（ps -o lstart=）の既定の戻り値。current_pidと
# 一致するrun.jsonのmain_pid_started_atにもこの値を使うことで、「同一
# プロセス」として一致させる。
_STARTED_AT = "Thu Jul 24 09:32:04 2026"


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    monkeypatch.setattr(HookState, "BASE_DIR", tmp_path / "state")
    monkeypatch.setenv("CALM_RECORDER", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ATTENDED", "1")
    monkeypatch.setenv("CLAUDE_PID", str(_PID))

    def _fake_ps(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=f"{_STARTED_AT}\n", stderr="")

    monkeypatch.setattr(process_signature.subprocess, "run", _fake_ps)


@pytest.fixture
def calls(monkeypatch):
    """recorder.pyへの起動(start)・古い記録役の停止(stop)のPopen呼び出しを
    時系列で記録する（順序を検証するため）。どちらも切り離しプロセスとして
    Popen経由で起動されるため、Popen自体を外部境界としてmonkeypatchする。
    """
    log: list[tuple[list[str], dict]] = []

    def _fake_popen(cmd, **kwargs):
        log.append((cmd, kwargs))
        return None

    monkeypatch.setattr(hook.subprocess, "Popen", _fake_popen)
    return log


def _stop_calls(log: list[tuple]) -> list[str]:
    """"stop"を呼んだ対象session_idの一覧を、呼ばれた順に返す。"""
    return [cmd[cmd.index("--session-id") + 1] for cmd, _ in log if cmd[2] == "stop"]


def _start_calls(log: list[tuple]) -> list[tuple]:
    """"start"呼び出しの(cmd, kwargs)一覧を、呼ばれた順に返す。"""
    return [(cmd, kwargs) for cmd, kwargs in log if cmd[2] == "start"]


def _write_run_json(
    sid_for_dir: str, *, main_sid: str, main_pid: int, main_pid_started_at: str = _STARTED_AT
) -> None:
    run_dir = run_dir_for(sid_for_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "main_sid": main_sid,
                "main_pid": main_pid,
                "main_pid_started_at": main_pid_started_at,
            }
        ),
        encoding="utf-8",
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
        starts = _start_calls(calls)
        assert len(starts) == 1
        cmd, kwargs = starts[0]
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

        cmd, _ = _start_calls(calls)[0]
        assert "--from-start" not in cmd
        assert cmd[cmd.index("--transcript") + 1] == str(real_transcript)

    def test_calm_recorder_is_stripped_from_spawned_env(self, calls):
        """spec 4: 記録役自身に記録役が付かないよう、起動env自体からは取り除く。"""
        _run_hook()

        _, kwargs = _start_calls(calls)[0]
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
        assert [cmd[2] for cmd, _ in calls] == ["stop", "start"]  # 停止してから起動する順序
        assert _stop_calls(calls) == [old_sid]
        cmd, _ = _start_calls(calls)[0]
        assert cmd[cmd.index("--session-id") + 1] == _SID  # 新しいsidで起動する（古いsidではない）

    def test_resume_same_sid_different_pid_stops_then_restarts_same_sid(self, calls):
        """resume: session_idは維持され、main_pidだけ新しいプロセスのものに変わる。"""
        _write_run_json(_SID, main_sid=_SID, main_pid=99999)  # 旧プロセスのpid

        code = _run_hook(session_id=_SID)

        assert code == 0
        assert [cmd[2] for cmd, _ in calls] == ["stop", "start"]  # 停止してから起動する順序
        assert _stop_calls(calls) == [_SID]
        cmd, _ = _start_calls(calls)[0]
        assert cmd[cmd.index("--session-id") + 1] == _SID
        assert cmd[cmd.index("--pid") + 1] == str(_PID)  # 新しいpidで起動する

    def test_compact_same_pid_same_sid_does_not_stop_anything(self, calls):
        """compact: main_pid・session_idともに不変。stale判定に一致しないため
        stopは呼ばれず、start()自体の二重起動防止（別テストで担保済み）に委ねる。"""
        _write_run_json(_SID, main_sid=_SID, main_pid=_PID)  # 現在と完全一致 = staleではない

        code = _run_hook(session_id=_SID)

        assert code == 0
        assert _stop_calls(calls) == []
        assert len(_start_calls(calls)) == 1

    def test_unrelated_run_json_for_other_pid_and_sid_is_left_alone(self, calls):
        """自分と無関係な(別pid・別sid)の記録役には触れない。"""
        _write_run_json("someone-elses-session", main_sid="someone-elses-session", main_pid=22222)

        _run_hook(session_id=_SID)

        assert _stop_calls(calls) == []
        assert len(_start_calls(calls)) == 1

    def test_pid_match_with_different_start_signature_is_not_treated_as_stale(self, calls):
        """同pidだが起動時刻(process_start_signature)が食い違う場合はOSのpid
        再利用とみなし、無関係な古いsession_idをstale扱いしない
        （`is_recorder_attached`と同じ照合方式）。"""
        old_sid = "main-session-before-clear"
        _write_run_json(
            old_sid, main_sid=old_sid, main_pid=_PID,
            main_pid_started_at="Mon Jan 01 00:00:00 2020",  # 現在のcurrent_pidの起動時刻と不一致
        )

        code = _run_hook(session_id=_SID)

        assert code == 0
        assert _stop_calls(calls) == []
        assert len(_start_calls(calls)) == 1

    def test_stop_is_spawned_as_detached_process_not_blocking(self, calls):
        """古い記録役の停止も、起動と同じくPopen経由の切り離しプロセスで行う
        （tmux kill-sessionの完了をSessionStart hook自身が同期的に待たない）。"""
        old_sid = "main-session-before-clear"
        _write_run_json(old_sid, main_sid=old_sid, main_pid=_PID)

        _run_hook(session_id=_SID)

        stop_calls = [(cmd, kwargs) for cmd, kwargs in calls if cmd[2] == "stop"]
        assert len(stop_calls) == 1
        cmd, kwargs = stop_calls[0]
        assert cmd[0].endswith(".venv/bin/python")
        assert cmd[1].endswith("scripts/recorder.py")
        assert cmd[2:] == ["stop", "--session-id", old_sid]
        assert kwargs["start_new_session"] is True
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["stdout"] is kwargs["stderr"]
        assert kwargs["stdout"].name == str(run_dir_for(old_sid) / "autostart_stop.log")

    def test_stop_failure_does_not_block_start(self, monkeypatch):
        """古い記録役の停止(Popen自体の失敗)が起きても、新しい記録役の起動は
        妨げない。"""
        old_sid = "main-session-before-clear"
        _write_run_json(old_sid, main_sid=old_sid, main_pid=_PID)

        log: list[tuple[list[str], dict]] = []

        def _raising_popen(cmd, **kwargs):
            if cmd[2] == "stop":
                raise OSError("boom")
            log.append((cmd, kwargs))
            return None

        monkeypatch.setattr(hook.subprocess, "Popen", _raising_popen)

        code = _run_hook(session_id=_SID)

        assert code == 0
        assert len(log) == 1
        assert log[0][0][2] == "start"


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

        assert len(_start_calls(calls)) == 1


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
        assert len(_start_calls(calls)) == 1

    def test_emits_nothing_to_stdout(self, calls):
        fake_stdout = io.StringIO()
        with patch.object(hook.sys, "stdout", fake_stdout):
            _run_hook()
        assert fake_stdout.getvalue() == ""
