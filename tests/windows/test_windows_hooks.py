"""hooks/hooks.json の全エントリを、Claude Codeが実行に使うシェルと同じもので
起動できることを確かめる。

hooks.jsonの14エントリは全て`"command": "cd ${CLAUDE_PLUGIN_ROOT} && exec uv run
python ..."`というPOSIXシェル専用の書き方になっている。Windows PowerShell 5.1は
`&&`をパースエラーにし、PowerShell 7には`exec`が無いため、Git for Windowsが
無ければフックは毎回失敗する。Git BashがあってもGit Bashで動く保証は無い
(PowerShellツールが既定で有効な場合がある)。

このテストは各エントリの`command`(将来execフォームに直った場合は`args`)を、
POSIXではbashで、WindowsではGit Bashのbash.exe・PowerShell 5.1・pwshの
それぞれで直接実行し、終了コード0・出力があればJSONとして解析できることを
確かめる。利用できないシェルはskipする(fail扱いにしない)。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.windows.support import (
    REPO_ROOT,
    HookRunResult,
    isolated_env,
    iter_hook_entries,
    load_hooks_json,
    resolve_git_bash,
    resolve_posix_bash,
    resolve_powershell5,
    resolve_pwsh,
    run_with_timeout,
)

_HOOKS_JSON = load_hooks_json()


def _build_payload(event_name: str, tmp_path: Path) -> dict:
    """イベント種別ごとの最小のClaude Code hook入力を組み立てる。

    深い機能検証はこのテストの目的ではない(hooks.jsonの実行形が壊れていない
    ことの確認)。実際に発火しうる分岐へ安全に倒すため、PreToolUse/PostToolUse系
    はtool_name="Read"で統一する: deny_nested_bg_hook.py(Bashのみ処理)や
    ask_answer_rewake_hook.py(add_ask呼び出しのみ処理)は早期returnで無害に
    終わり、86400秒ポーリングのような長時間パスへ入り込まない。
    """
    base = {
        "session_id": "win-hooks-repro-session",
        "transcript_path": str(tmp_path / "transcript.jsonl"),
        "cwd": str(REPO_ROOT),
        "hook_event_name": event_name,
    }
    extra_by_event = {
        "SessionStart": {"source": "startup"},
        "Stop": {"stop_hook_active": False},
        "UserPromptSubmit": {"prompt": "windows hooks repro check"},
        "PreToolUse": {"tool_name": "Read", "tool_input": {"file_path": "dummy.txt"}},
        "PostToolUseFailure": {
            "tool_name": "Read",
            "tool_input": {"file_path": "dummy.txt"},
            "error": "boom",
        },
        "PostToolUse": {
            "tool_name": "Read",
            "tool_input": {"file_path": "dummy.txt"},
            "tool_response": {"content": "ok"},
        },
        "MessageDisplay": {"delta": "hello world", "index": 0, "final": True, "message_id": "m1"},
    }
    return {**base, **extra_by_event.get(event_name, {})}


def _entry_id(event_name: str, matcher: str, hook: dict, idx: int) -> str:
    script = hook.get("command") or " ".join(hook.get("args", []))
    return f"{event_name}[{matcher}]#{idx}:{script.split()[-1] if script else '?'}"


_ENTRIES = list(iter_hook_entries(_HOOKS_JSON))
_ENTRY_IDS = [_entry_id(*e) for e in _ENTRIES]


def _run_via_exec_form(hook: dict, env: dict, input_bytes: bytes, timeout: float) -> HookRunResult:
    # スラッシュ区切りへの変換はshell-form(_run_via_shell)ほど必須ではないが
    # (execフォームはargv直接受け渡しでシェルのエスケープを経由しない)、
    # Windowsはフォワードスラッシュのパスも問題なく受け付けるため同じ変換に揃える。
    plugin_root = str(REPO_ROOT).replace("\\", "/")
    argv = [hook["command"], *hook.get("args", [])]
    argv = [a.replace("${CLAUDE_PLUGIN_ROOT}", plugin_root) for a in argv]
    return run_with_timeout(argv, input_bytes=input_bytes, cwd=REPO_ROOT, env=env, timeout=timeout)


def _run_via_shell(
    command: str, shell_exe: Path, shell_kind: str, env: dict, input_bytes: bytes, timeout: float
) -> HookRunResult:
    # Claude Codeの公式ドキュメントは、shell経由で実行するhookコマンド文字列内の
    # ${CLAUDE_PLUGIN_ROOT}をWindowsでもスラッシュ区切りで置換すると明記している。
    # バックスラッシュのままだとbashの-c文字列内でエスケープシーケンスとして
    # 誤解釈されうるため、ここでも同じ変換を行う(execフォームのargsはシェルを
    # 経由しないため対象外)。
    plugin_root = str(REPO_ROOT).replace("\\", "/")
    command = command.replace("${CLAUDE_PLUGIN_ROOT}", plugin_root)
    if shell_kind == "bash":
        argv = [str(shell_exe), "-c", command]
    else:  # powershell5 / pwsh
        argv = [str(shell_exe), "-NoProfile", "-Command", command]
    return run_with_timeout(argv, input_bytes=input_bytes, cwd=REPO_ROOT, env=env, timeout=timeout)


def _run_entry(hook: dict, shell_kind: str, shell_exe: Path | None, env: dict, input_bytes: bytes) -> HookRunResult:
    timeout = 90.0
    if "args" in hook:
        return _run_via_exec_form(hook, env, input_bytes, timeout)
    assert shell_exe is not None
    return _run_via_shell(hook["command"], shell_exe, shell_kind, env, input_bytes, timeout)


def _assert_hook_ran_cleanly(result: HookRunResult, label: str) -> None:
    assert not result.timed_out, f"{label} timed out\nstderr={result.stderr_text()}"
    assert result.returncode == 0, (
        f"{label} exited with code={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr_text()}"
    )
    stdout = result.stdout.strip()
    if not stdout:
        return
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError as e:
        raise AssertionError(f"{label} stdout is not valid UTF-8: {stdout!r} ({e})")
    try:
        json.loads(text)
    except json.JSONDecodeError as e:
        raise AssertionError(f"{label} stdout is not valid JSON: {text!r} ({e})")


@pytest.mark.skipif(resolve_posix_bash() is None, reason="bash not found")
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only shell check; see test_hook_entry_runs_under_windows_shell for Windows",
)
@pytest.mark.parametrize("entry", _ENTRIES, ids=_ENTRY_IDS)
def test_hook_entry_runs_under_posix_bash(entry, tmp_path):
    event_name, matcher, hook, idx = entry
    bash = resolve_posix_bash()
    env = isolated_env(tmp_path)
    payload = _build_payload(event_name, tmp_path)
    (tmp_path / "transcript.jsonl").touch()
    input_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    result = _run_entry(hook, "bash", bash, env, input_bytes)
    _assert_hook_ran_cleanly(result, _entry_id(*entry))


_WINDOWS_SHELLS = [
    ("git-bash", resolve_git_bash, "bash"),
    ("powershell5", resolve_powershell5, "powershell5"),
    ("pwsh", resolve_pwsh, "pwsh"),
]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only shell check")
@pytest.mark.parametrize("shell_name,resolver,shell_kind", _WINDOWS_SHELLS)
@pytest.mark.parametrize("entry", _ENTRIES, ids=_ENTRY_IDS)
def test_hook_entry_runs_under_windows_shell(entry, shell_name, resolver, shell_kind, tmp_path):
    shell_exe = resolver()
    if shell_exe is None:
        pytest.skip(f"{shell_name} not found on this runner")
    event_name, matcher, hook, idx = entry
    env = isolated_env(tmp_path)
    payload = _build_payload(event_name, tmp_path)
    (tmp_path / "transcript.jsonl").touch()
    input_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    result = _run_entry(hook, shell_kind, shell_exe, env, input_bytes)
    _assert_hook_ran_cleanly(result, f"{_entry_id(*entry)} via {shell_name}")
