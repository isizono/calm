"""src/services/recorder_launcher_service.py の単体テスト。

subprocess呼び出し(tmux・ps)を外部境界としてmonkeypatchする。ps呼び出しは
write_marker/process_start_signature経由でも呼ばれるため、コマンド種別で
振り分けて両方を成立させる（tests/unit/test_recorder_watch.pyと同じ方針）。
ファイルシステム状態（settings.json・mcp.json・run.json・cursor.json）は
実際に書かれうる形で検証する。
"""
import ast
import json
import os
import subprocess
from pathlib import Path

import pytest

from hooks import recorder_watch as watch_hook
from hooks.hook_state import HookState
from hooks.recorder_marker import is_recorder_attached, marker_path, remove_marker, write_marker
from src.services import recorder_launcher_service as svc

_FAKE_PS_STARTED_AT = "Thu Jul 24 09:32:04 2026"
_MAIN_SID = "main-session-abcdefgh"
_MAIN_PID = 12345
_PANE_PID = 54321
_MAIN_PY_PATH = Path(__file__).resolve().parents[2] / "src" / "main.py"


def _registered_get_tool_names(main_py_path: Path) -> set[str]:
    """src/main.pyの@mcp.tool()デコレータ付き関数のうち、get_で始まる名前を返す。"""
    tree = ast.parse(main_py_path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("get_"):
            continue
        for deco in node.decorator_list:
            if ast.unparse(deco) == "mcp.tool()":
                names.add(node.name)
                break
    return names


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    monkeypatch.setattr(HookState, "BASE_DIR", tmp_path / "state")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_PID", raising=False)


@pytest.fixture(autouse=True)
def _mock_subprocess(monkeypatch):
    """tmux/psをコマンド種別で振り分ける。呼び出し履歴はtmuxの分だけ記録する。"""
    tmux_calls: list[list[str]] = []

    def _fake_run(cmd, **kwargs):
        if cmd and cmd[0] == "tmux":
            tmux_calls.append(cmd)
            if cmd[1] == "display-message":
                return subprocess.CompletedProcess(cmd, 0, stdout=f"{_PANE_PID}\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd and cmd[0] == "ps":
            return subprocess.CompletedProcess(cmd, 0, stdout=_FAKE_PS_STARTED_AT + "\n", stderr="")
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    return tmux_calls


@pytest.fixture
def calm_root(tmp_path):
    root = tmp_path / "calm"
    (root / "hooks").mkdir(parents=True)
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
    (root / "hooks" / "recorder_watch.py").write_text("", encoding="utf-8")
    (root / "hooks" / "recorder_instructions.md").write_text("# guide", encoding="utf-8")
    (root / ".mcp.json").write_text(
        json.dumps(
            {
                "calm": {
                    "command": "uv",
                    "args": ["run", "--directory", "${CLAUDE_PLUGIN_ROOT}", "python", "-m", "src.launcher"],
                }
            }
        ),
        encoding="utf-8",
    )
    return root


def _write_jsonl(path, entries: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def _entry(uuid: str) -> dict:
    return {"type": "assistant", "uuid": uuid, "message": {"content": [{"type": "text", "text": "hi"}]}}


# ===================================================================
# 識別情報の解決
# ===================================================================


class TestResolveMainSid:
    def test_from_env_when_not_explicit(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-from-env")
        assert svc.resolve_main_sid(None) == "sess-from-env"

    def test_explicit_overrides_env(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-from-env")
        assert svc.resolve_main_sid("sess-explicit") == "sess-explicit"

    def test_raises_when_neither_set(self):
        with pytest.raises(svc.RecorderLaunchError):
            svc.resolve_main_sid(None)


class TestResolveMainPid:
    def test_from_env_when_not_explicit(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PID", "4242")
        assert svc.resolve_main_pid(None) == 4242

    def test_explicit_overrides_env(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PID", "4242")
        assert svc.resolve_main_pid(999) == 999

    def test_raises_when_neither_set(self):
        with pytest.raises(svc.RecorderLaunchError):
            svc.resolve_main_pid(None)

    def test_raises_when_env_not_int(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PID", "not-an-int")
        with pytest.raises(svc.RecorderLaunchError):
            svc.resolve_main_pid(None)


class TestResolveMainTranscript:
    def test_explicit_path_used_verbatim(self, tmp_path):
        explicit = tmp_path / "somewhere.jsonl"
        assert svc.resolve_main_transcript("sid", str(explicit)) == explicit

    def test_single_glob_match_found(self, tmp_path):
        proj = tmp_path / "projects" / "proj-a"
        proj.mkdir(parents=True)
        target = proj / "sid-1.jsonl"
        target.write_text("", encoding="utf-8")

        result = svc.resolve_main_transcript("sid-1", None, projects_root=tmp_path / "projects")

        assert result == target

    def test_zero_matches_raises(self, tmp_path):
        with pytest.raises(svc.RecorderLaunchError):
            svc.resolve_main_transcript("sid-none", None, projects_root=tmp_path / "projects")

    def test_multiple_matches_raises(self, tmp_path):
        proj_a = tmp_path / "projects" / "proj-a"
        proj_b = tmp_path / "projects" / "proj-b"
        proj_a.mkdir(parents=True)
        proj_b.mkdir(parents=True)
        (proj_a / "sid-dup.jsonl").write_text("", encoding="utf-8")
        (proj_b / "sid-dup.jsonl").write_text("", encoding="utf-8")

        with pytest.raises(svc.RecorderLaunchError):
            svc.resolve_main_transcript("sid-dup", None, projects_root=tmp_path / "projects")


class TestTmuxSessionName:
    def test_reexports_watchers_implementation(self):
        """独自に再実装せず、見張り(hooks.recorder_watch)の実装をそのまま
        importして使っていることを確かめる。別々に持つと式が食い違いうる
        ため、起動側(このモジュール)と停止側(hooks.recorder_watch._terminate
        が使う実装)を単一の関数に一本化している。"""
        assert svc.tmux_session_name is watch_hook.tmux_session_name

    def test_uses_full_sid_not_truncated(self):
        assert svc.tmux_session_name("abcdefgh-ijklmnop") == "calm-rec-abcdefgh-ijklmnop"

    def test_replaces_colon_and_dot_with_underscore(self):
        assert svc.tmux_session_name("sid.with:colons") == "calm-rec-sid_with_colons"


# ===================================================================
# 生成物
# ===================================================================


class TestBuildSettings:
    def test_stop_hook_execs_venv_python_directly(self, calm_root, tmp_path):
        run_dir = tmp_path / "run"
        settings = svc.build_settings(calm_root, run_dir)

        hook_cmd = settings["hooks"]["Stop"][0]["hooks"][0]
        assert hook_cmd["command"] == (
            f"{calm_root}/.venv/bin/python {calm_root}/hooks/recorder_watch.py {run_dir}"
        )
        assert hook_cmd["asyncRewake"] is True
        assert hook_cmd["timeout"] == 86400
        assert "uv run" not in hook_cmd["command"]

    def test_permissions_allow_scoped_read_to_run_dir(self, calm_root, tmp_path):
        run_dir = tmp_path / "run"
        settings = svc.build_settings(calm_root, run_dir)

        allow = settings["permissions"]["allow"]
        assert f"Read(/{run_dir}/**)" in allow
        assert f"Read(/{run_dir}/**)".startswith("Read(//")
        assert f"{svc.MCP_TOOL_PREFIX}add_logs" in allow
        assert f"{svc.MCP_TOOL_PREFIX}add_material" in allow
        assert f"{svc.MCP_TOOL_PREFIX}add_relation" in allow
        assert not any("add_decisions" in a for a in allow)
        assert not any("check_in" in a for a in allow)

    def test_get_tools_are_enumerated_not_wildcarded(self, calm_root, tmp_path):
        """`mcp__calm__get_*`のような部分一致ワイルドカードが実際に解釈される
        かは未確認のため、get_系ツールは名前を1つずつ列挙する。"""
        run_dir = tmp_path / "run"
        settings = svc.build_settings(calm_root, run_dir)

        allow = settings["permissions"]["allow"]
        assert not any(a.endswith("get_*") for a in allow)
        for name in svc._ALLOWED_GET_TOOLS:
            assert f"{svc.MCP_TOOL_PREFIX}{name}" in allow

    def test_write_settings_json_writes_file(self, calm_root, tmp_path):
        run_dir = tmp_path / "run"
        path = svc.write_settings_json(run_dir, calm_root)

        assert path == run_dir / "settings.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["hooks"]["Stop"][0]["hooks"][0]["asyncRewake"] is True


class TestAllowedGetToolsMatchesMainPy:
    """settings.jsonが許可するget_系ツール名が、src/main.pyの実際の登録から
    ズレていないことを確かめる(get_系ツールが増減しても列挙が古くならない
    ように、実装から導出した期待値と突き合わせる)。"""

    def test_matches_registered_get_tools_in_main_py(self):
        expected = _registered_get_tool_names(_MAIN_PY_PATH)
        assert set(svc._ALLOWED_GET_TOOLS) == expected


class TestMcpConfig:
    def test_wraps_in_mcp_servers_key(self, calm_root):
        config = svc.build_mcp_config(calm_root)
        assert set(config.keys()) == {"mcpServers"}
        assert "calm" in config["mcpServers"]

    def test_substitutes_plugin_root_placeholder(self, calm_root):
        config = svc.build_mcp_config(calm_root)
        args = config["mcpServers"]["calm"]["args"]
        assert str(calm_root) in args
        assert not any("${CLAUDE_PLUGIN_ROOT}" in a for a in args)

    def test_write_mcp_json_writes_file(self, calm_root, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        path = svc.write_mcp_json(run_dir, calm_root)

        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["mcpServers"]["calm"]["command"] == "uv"


class TestRunJson:
    def test_first_write_has_single_recorder_sid(self, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        data = svc.update_run_json(
            run_dir,
            main_sid=_MAIN_SID,
            main_pid=_MAIN_PID,
            main_pid_started_at=_FAKE_PS_STARTED_AT,
            main_transcript=tmp_path / "t.jsonl",
            recorder_sid="rec-1",
        )
        assert data["main_sid"] == _MAIN_SID
        assert data["main_pid"] == _MAIN_PID
        assert data["main_pid_started_at"] == _FAKE_PS_STARTED_AT
        assert data["main_transcript"] == str(tmp_path / "t.jsonl")
        assert data["recorder_sids"] == ["rec-1"]

    def test_second_write_appends_without_dropping_first(self, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        svc.update_run_json(
            run_dir, main_sid=_MAIN_SID, main_pid=_MAIN_PID,
            main_pid_started_at=_FAKE_PS_STARTED_AT, main_transcript=tmp_path / "t.jsonl",
            recorder_sid="rec-1",
        )
        data = svc.update_run_json(
            run_dir, main_sid=_MAIN_SID, main_pid=_MAIN_PID,
            main_pid_started_at=_FAKE_PS_STARTED_AT, main_transcript=tmp_path / "t.jsonl",
            recorder_sid="rec-2",
        )
        assert data["recorder_sids"] == ["rec-1", "rec-2"]


class TestAtomicWrites:
    """settings.json・mcp.json・run.jsonの書き込みが、cursor.jsonと同じ
    tempfile→os.replaceのアトミックパターンを通ることを確かめる（見張り等の
    並行読み取りが書きかけの中身を掴まないようにするため）。"""

    def _track_replace(self, monkeypatch):
        calls: list[tuple[str, str]] = []
        real_replace = os.replace

        def _tracking(src, dst):
            calls.append((str(src), str(dst)))
            real_replace(src, dst)

        monkeypatch.setattr(watch_hook.os, "replace", _tracking)
        return calls

    def test_write_settings_json_goes_through_tmpfile_replace(self, calm_root, tmp_path, monkeypatch):
        calls = self._track_replace(monkeypatch)
        run_dir = tmp_path / "run"

        path = svc.write_settings_json(run_dir, calm_root)

        assert len(calls) == 1
        tmp_src, dst = calls[0]
        assert dst == str(path)
        assert tmp_src != dst
        assert Path(tmp_src).parent == path.parent

    def test_write_mcp_json_goes_through_tmpfile_replace(self, calm_root, tmp_path, monkeypatch):
        calls = self._track_replace(monkeypatch)
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        path = svc.write_mcp_json(run_dir, calm_root)

        assert len(calls) == 1
        tmp_src, dst = calls[0]
        assert dst == str(path)
        assert tmp_src != dst

    def test_update_run_json_goes_through_tmpfile_replace(self, tmp_path, monkeypatch):
        calls = self._track_replace(monkeypatch)
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        svc.update_run_json(
            run_dir, main_sid=_MAIN_SID, main_pid=_MAIN_PID,
            main_pid_started_at=_FAKE_PS_STARTED_AT, main_transcript=tmp_path / "t.jsonl",
            recorder_sid="rec-1",
        )

        assert len(calls) == 1
        tmp_src, dst = calls[0]
        assert dst == str(run_dir / "run.json")
        assert tmp_src != dst


class TestEnsureCursor:
    def test_default_starts_from_tail(self, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        transcript = tmp_path / "t.jsonl"
        _write_jsonl(transcript, [_entry("u1"), _entry("u2")])
        complete_size = transcript.stat().st_size
        # 書きかけの末尾行(改行未到達)を追加する。tailはこれを含めてはならない。
        with open(transcript, "a", encoding="utf-8") as f:
            f.write(json.dumps(_entry("u3"), ensure_ascii=False))

        svc.ensure_cursor(run_dir, transcript, from_start=False)

        cursor = json.loads((run_dir / "cursor.json").read_text(encoding="utf-8"))
        assert cursor["byte_offset"] == complete_size
        assert cursor["last_uuid"] == "u2"

    def test_from_start_flag_starts_at_zero(self, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        transcript = tmp_path / "t.jsonl"
        _write_jsonl(transcript, [_entry("u1"), _entry("u2")])

        svc.ensure_cursor(run_dir, transcript, from_start=True)

        cursor = json.loads((run_dir / "cursor.json").read_text(encoding="utf-8"))
        assert cursor["byte_offset"] == 0
        assert cursor["last_uuid"] is None

    def test_does_not_touch_existing_cursor(self, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        transcript = tmp_path / "t.jsonl"
        _write_jsonl(transcript, [_entry("u1")])
        existing = {"last_uuid": "custom", "byte_offset": 999, "next_no": 7}
        (run_dir / "cursor.json").write_text(json.dumps(existing), encoding="utf-8")

        svc.ensure_cursor(run_dir, transcript, from_start=False)

        cursor = json.loads((run_dir / "cursor.json").read_text(encoding="utf-8"))
        assert cursor == existing


# ===================================================================
# start / stop / status
# ===================================================================


def _fixed_sid_factory(value: str):
    return lambda: value


class TestStart:
    def test_happy_path_creates_run_dir_contents_and_marker(self, calm_root, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _MAIN_SID)
        monkeypatch.setenv("CLAUDE_PID", str(_MAIN_PID))
        transcript = tmp_path / "t.jsonl"
        _write_jsonl(transcript, [_entry("u1")])

        result = svc.start(
            calm_root=calm_root, transcript=str(transcript), sid_factory=_fixed_sid_factory("rec-1"),
        )

        assert result["started"] is True
        assert result["recorder_sid"] == "rec-1"
        assert result["tmux_session"] == svc.tmux_session_name(_MAIN_SID)
        assert result["pane_pid"] == _PANE_PID

        run_dir = watch_hook.run_dir_for(_MAIN_SID)
        assert (run_dir / "settings.json").exists()
        assert (run_dir / "mcp.json").exists()
        run_data = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        assert run_data["recorder_sids"] == ["rec-1"]
        assert (run_dir / "cursor.json").exists()

        assert is_recorder_attached(_MAIN_SID) is True
        marker = json.loads(marker_path(_MAIN_SID).read_text(encoding="utf-8"))
        assert marker["pid"] == _PANE_PID

    def test_double_start_is_noop_when_already_attached(self, calm_root, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _MAIN_SID)
        monkeypatch.setenv("CLAUDE_PID", str(_MAIN_PID))
        write_marker(_MAIN_SID, _PANE_PID)
        assert is_recorder_attached(_MAIN_SID) is True

        result = svc.start(calm_root=calm_root, sid_factory=_fixed_sid_factory("rec-should-not-run"))

        assert result == {"started": False, "reason": "already attached", "main_sid": _MAIN_SID}

    def test_double_start_does_not_touch_tmux(self, calm_root, tmp_path, monkeypatch, _mock_subprocess):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _MAIN_SID)
        monkeypatch.setenv("CLAUDE_PID", str(_MAIN_PID))
        write_marker(_MAIN_SID, _PANE_PID)
        # transcriptを渡し、ガードが無ければtmux起動まで到達する状態にする
        # (未指定だと、ガードを外してもresolve_main_transcriptのエラーで先に
        # 落ちてしまい、tmux呼び出しを見るassertの検出力を確かめられない)。
        transcript = tmp_path / "t.jsonl"
        _write_jsonl(transcript, [_entry("u1")])

        svc.start(calm_root=calm_root, transcript=str(transcript), sid_factory=_fixed_sid_factory("rec-x"))
        # 二重起動ガードはis_recorder_attached判定の時点で即returnするため、
        # run_dirにもtmuxにも一切触れない。
        run_dir = watch_hook.run_dir_for(_MAIN_SID)
        assert not (run_dir / "run.json").exists()
        assert _mock_subprocess == []

    def test_restart_appends_new_recorder_sid_to_existing_run_json(self, calm_root, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _MAIN_SID)
        monkeypatch.setenv("CLAUDE_PID", str(_MAIN_PID))
        transcript = tmp_path / "t.jsonl"
        _write_jsonl(transcript, [_entry("u1")])

        svc.start(calm_root=calm_root, transcript=str(transcript), sid_factory=_fixed_sid_factory("rec-1"))
        # 1回目のstartでmarkerが付くため、2回目を素通りさせるにはmarkerを消す
        # (停止して次のstartを行う運用を模す)。
        remove_marker(_MAIN_SID)

        svc.start(calm_root=calm_root, transcript=str(transcript), sid_factory=_fixed_sid_factory("rec-2"))

        run_dir = watch_hook.run_dir_for(_MAIN_SID)
        run_data = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        assert run_data["recorder_sids"] == ["rec-1", "rec-2"]

    def test_pane_pid_failure_cleans_up_tmux_and_does_not_record_recorder_sid(
        self, calm_root, tmp_path, monkeypatch
    ):
        """tmuxセッションの起動自体は成功したが、pane_pid取得が失敗した場合。
        孤児セッションを残さずkill-sessionで後始末し、起動していない
        recorder_sidをrun.jsonに残さないことを確かめる。既存のrun.json
        （前の記録役のrecorder_sids）がある状態から始め、失敗後もそれが
        増えていないことを直接確かめる。"""
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _MAIN_SID)
        monkeypatch.setenv("CLAUDE_PID", str(_MAIN_PID))
        transcript = tmp_path / "t.jsonl"
        _write_jsonl(transcript, [_entry("u1")])
        run_dir = watch_hook.run_dir_for(_MAIN_SID)
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(
            json.dumps({"recorder_sids": ["rec-old"]}), encoding="utf-8"
        )

        tmux_calls: list[list[str]] = []

        def _fake_run(cmd, **kwargs):
            if cmd[0] == "tmux":
                tmux_calls.append(cmd)
                if cmd[1] == "display-message":
                    raise subprocess.CalledProcessError(1, cmd, stderr="no such session")
                return subprocess.CompletedProcess(cmd, 0)
            return subprocess.CompletedProcess(cmd, 0, stdout=_FAKE_PS_STARTED_AT + "\n")

        monkeypatch.setattr(subprocess, "run", _fake_run)

        with pytest.raises(svc.RecorderLaunchError):
            svc.start(
                calm_root=calm_root, transcript=str(transcript),
                sid_factory=_fixed_sid_factory("rec-fail"),
            )

        kill_calls = [c for c in tmux_calls if c[1] == "kill-session"]
        assert kill_calls == [["tmux", "kill-session", "-t", svc.tmux_session_name(_MAIN_SID)]]

        run_data = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        assert run_data["recorder_sids"] == ["rec-old"]
        assert is_recorder_attached(_MAIN_SID) is False


class TestStop:
    def test_removes_marker_and_kills_tmux_session(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _MAIN_SID)
        write_marker(_MAIN_SID, _PANE_PID)
        assert is_recorder_attached(_MAIN_SID) is True

        tmux_calls: list[list[str]] = []

        def _fake_run(cmd, **kwargs):
            if cmd[0] == "tmux":
                tmux_calls.append(cmd)
                return subprocess.CompletedProcess(cmd, 0)
            return subprocess.CompletedProcess(cmd, 0, stdout=_FAKE_PS_STARTED_AT + "\n")

        monkeypatch.setattr(subprocess, "run", _fake_run)

        result = svc.stop()

        assert result == {"main_sid": _MAIN_SID, "was_attached": True}
        assert is_recorder_attached(_MAIN_SID) is False
        assert tmux_calls == [["tmux", "kill-session", "-t", svc.tmux_session_name(_MAIN_SID)]]

    def test_noop_when_not_attached(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _MAIN_SID)
        result = svc.stop()
        assert result == {"main_sid": _MAIN_SID, "was_attached": False}


class TestRestart:
    """restart()は見張りが切り離しプロセスとして呼ぶ経路専用で、
    $CLAUDE_CODE_SESSION_ID等のセッション環境変数には頼れない。テストでは
    それらを未設定のまま明示引数だけでstop→startが動くことを確かめる。"""

    def test_restarts_using_explicit_values_not_env(self, calm_root, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_PID", raising=False)
        transcript = tmp_path / "t.jsonl"
        _write_jsonl(transcript, [_entry("u1")])
        write_marker(_MAIN_SID, _PANE_PID)  # 既存の記録役が付いている状態を模す

        result = svc.restart(
            calm_root=calm_root, main_sid=_MAIN_SID, main_pid=_MAIN_PID,
            main_transcript=str(transcript), sid_factory=_fixed_sid_factory("rec-restarted"),
        )

        assert result["started"] is True
        assert result["recorder_sid"] == "rec-restarted"
        assert is_recorder_attached(_MAIN_SID) is True

    def test_kills_old_session_before_launching_new_one(
        self, calm_root, tmp_path, monkeypatch, _mock_subprocess
    ):
        transcript = tmp_path / "t.jsonl"
        _write_jsonl(transcript, [_entry("u1")])
        write_marker(_MAIN_SID, _PANE_PID)

        svc.restart(
            calm_root=calm_root, main_sid=_MAIN_SID, main_pid=_MAIN_PID,
            main_transcript=str(transcript), sid_factory=_fixed_sid_factory("rec-2"),
        )

        kill_idx = next(i for i, c in enumerate(_mock_subprocess) if c[1] == "kill-session")
        new_session_idx = next(i for i, c in enumerate(_mock_subprocess) if c[1] == "new-session")
        assert kill_idx < new_session_idx

    def test_preserves_recorder_sids_and_cursor_across_restart(self, calm_root, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _MAIN_SID)
        monkeypatch.setenv("CLAUDE_PID", str(_MAIN_PID))
        transcript = tmp_path / "t.jsonl"
        _write_jsonl(transcript, [_entry("u1")])
        svc.start(calm_root=calm_root, transcript=str(transcript), sid_factory=_fixed_sid_factory("rec-1"))
        run_dir = watch_hook.run_dir_for(_MAIN_SID)
        # 記録役が処理を進めた体で、cursor.jsonを書き換えておく(立て直しで
        # 失われないことを確かめるため)。
        cursor_path = run_dir / "cursor.json"
        cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
        cursor["next_no"] = 42
        cursor_path.write_text(json.dumps(cursor), encoding="utf-8")

        svc.restart(
            calm_root=calm_root, main_sid=_MAIN_SID, main_pid=_MAIN_PID,
            main_transcript=str(transcript), sid_factory=_fixed_sid_factory("rec-2"),
        )

        run_data = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        assert run_data["recorder_sids"] == ["rec-1", "rec-2"]
        cursor2 = json.loads(cursor_path.read_text(encoding="utf-8"))
        assert cursor2["next_no"] == 42


class TestStatus:
    def test_reports_attached_and_cursor(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _MAIN_SID)
        write_marker(_MAIN_SID, _PANE_PID)
        run_dir = watch_hook.run_dir_for(_MAIN_SID)
        run_dir.mkdir(parents=True)
        (run_dir / "cursor.json").write_text(json.dumps({"next_no": 3}), encoding="utf-8")

        result = svc.status()

        assert result["attached"] is True
        assert result["run_dir_exists"] is True
        assert result["cursor"] == {"next_no": 3}

    def test_reports_not_attached_when_no_marker(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _MAIN_SID)
        result = svc.status()
        assert result["attached"] is False
        assert result["cursor"] is None


class TestMainCli:
    def test_success_prints_json_to_stdout(self, monkeypatch, capsys):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _MAIN_SID)

        svc.main(["status"])

        captured = capsys.readouterr()
        assert captured.err == ""
        data = json.loads(captured.out)
        assert data == {
            "main_sid": _MAIN_SID,
            "attached": False,
            "run_dir": str(watch_hook.run_dir_for(_MAIN_SID)),
            "run_dir_exists": False,
            "cursor": None,
        }

    def test_recorder_launch_error_exits_1_with_stderr(self, monkeypatch, capsys):
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

        with pytest.raises(SystemExit) as exc_info:
            svc.main(["status"])

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "エラー" in captured.err
        # 「エラー」を含むだけでなく、session_idを解決できなかった具体的な
        # メッセージであることまで確かめる(他のRecorderLaunchErrorと混同しない)。
        assert "session_idを解決できない" in captured.err

    def test_restart_requires_explicit_args(self):
        """restartは$CLAUDE_CODE_SESSION_ID等に頼れないため、引数は必須にする。"""
        with pytest.raises(SystemExit):
            svc.main(["restart"])

    def test_restart_calls_restart_with_explicit_cli_args(self, calm_root, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(svc, "_calm_root", lambda: calm_root)
        transcript = tmp_path / "t.jsonl"
        _write_jsonl(transcript, [_entry("u1")])

        svc.main([
            "restart", "--session-id", _MAIN_SID, "--pid", str(_MAIN_PID),
            "--transcript", str(transcript),
        ])

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["started"] is True
        assert data["main_sid"] == _MAIN_SID


# ===================================================================
# 起動フォルダの固定と信頼確認
# ===================================================================


class TestEnsureTrusted:
    def test_registers_resolved_path_and_keeps_other_keys(self, tmp_path):
        config = tmp_path / "claude.json"
        config.write_text(
            json.dumps({"numStartups": 3, "projects": {"/other": {"allowedTools": ["x"]}}}),
            encoding="utf-8",
        )
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)

        assert svc.ensure_trusted(link, config) is True

        data = json.loads(config.read_text(encoding="utf-8"))
        assert data["numStartups"] == 3
        assert data["projects"]["/other"] == {"allowedTools": ["x"]}
        assert data["projects"][str(real.resolve())] == {"hasTrustDialogAccepted": True}
        assert str(link) not in data["projects"]

    def test_does_not_rewrite_when_already_trusted(self, tmp_path):
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        config = tmp_path / "claude.json"
        original = json.dumps({"projects": {str(cwd.resolve()): {"hasTrustDialogAccepted": True}}})
        config.write_text(original, encoding="utf-8")

        assert svc.ensure_trusted(cwd, config) is False
        assert config.read_text(encoding="utf-8") == original

    def test_keeps_existing_entry_fields_when_adding_trust(self, tmp_path):
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        config = tmp_path / "claude.json"
        config.write_text(
            json.dumps({"projects": {str(cwd.resolve()): {"allowedTools": ["y"]}}}), encoding="utf-8"
        )

        svc.ensure_trusted(cwd, config)

        entry = json.loads(config.read_text(encoding="utf-8"))["projects"][str(cwd.resolve())]
        assert entry == {"allowedTools": ["y"], "hasTrustDialogAccepted": True}

    def test_unreadable_config_is_not_overwritten(self, tmp_path):
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        config = tmp_path / "claude.json"
        config.write_text("{broken", encoding="utf-8")

        with pytest.raises(svc.RecorderLaunchError):
            svc.ensure_trusted(cwd, config)
        assert config.read_text(encoding="utf-8") == "{broken"


class TestStartUsesSharedCwd:
    def _start(self, calm_root, tmp_path, monkeypatch, main_sid, recorder_sid):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", main_sid)
        monkeypatch.setenv("CLAUDE_PID", str(_MAIN_PID))
        transcript = tmp_path / f"{main_sid}.jsonl"
        _write_jsonl(transcript, [_entry("u1")])
        svc.start(calm_root=calm_root, transcript=str(transcript), sid_factory=_fixed_sid_factory(recorder_sid))

    def test_every_session_launches_in_the_same_trusted_cwd(
        self, calm_root, tmp_path, monkeypatch, _mock_subprocess
    ):
        self._start(calm_root, tmp_path, monkeypatch, "main-a", "rec-a")
        self._start(calm_root, tmp_path, monkeypatch, "main-b", "rec-b")

        new_sessions = [c for c in _mock_subprocess if c[1] == "new-session"]
        cwds = {c[c.index("-c") + 1] for c in new_sessions}
        assert cwds == {str(svc.recorder_cwd())}

        projects = json.loads(svc.CLAUDE_CONFIG_PATH.read_text(encoding="utf-8"))["projects"]
        assert projects == {str(svc.recorder_cwd().resolve()): {"hasTrustDialogAccepted": True}}

    def test_settings_and_mcp_config_are_passed_by_absolute_path(
        self, calm_root, tmp_path, monkeypatch, _mock_subprocess
    ):
        self._start(calm_root, tmp_path, monkeypatch, "main-a", "rec-a")

        pane_command = next(c for c in _mock_subprocess if c[1] == "new-session")[-1]
        run_dir = watch_hook.run_dir_for("main-a")
        assert f"--settings {run_dir / 'settings.json'}" in pane_command
        assert f"--mcp-config {run_dir / 'mcp.json'}" in pane_command
