"""tests/windows/ 配下のテスト共通ユーティリティ。

stdlib のみに依存する。このディレクトリのテストは、hooks.json・.mcp.json に
書かれたコマンドをそのまま読んで実行し、本番の起動経路（別シェル・別
インタプリタからの子プロセス起動）を忠実に再現する。そのため本ファイルは
src.* / tests.helpers を import せず、対象コードは常に子プロセスとして起動する。
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]


# tests/conftest.py の autouse fixture が既定で分離している対象と同じ環境変数群。
# HOME/USERPROFILE を一時ディレクトリへ向けても Path.home() を経由しない設定は
# 個別に上書きが要るため、既知の分離ポイントをここに集約する。
def isolated_env(tmp_home: Path, *, base_env: dict[str, str] | None = None) -> dict[str, str]:
    tmp_home.mkdir(parents=True, exist_ok=True)
    env = dict(base_env if base_env is not None else os.environ)
    env["HOME"] = str(tmp_home)
    env["USERPROFILE"] = str(tmp_home)
    env["CALM_DB_PATH"] = str(tmp_home / "discussion-test.db")
    env["CALM_HABITS_RULES_PATH"] = str(tmp_home / "cc-memory-habits.md")
    env["CALM_SESSION_REGISTRY_PATH"] = str(tmp_home / "session_aliases.json")
    env["CALM_CLAUDE_SESSIONS_DIR"] = str(tmp_home / "claude-sessions")
    env["RELAY_STATE_DIR"] = str(tmp_home / "relay-state")
    return env


def free_tcp_port() -> int:
    """127.0.0.1 上で空いているTCPポートを1つ確保して返す(bindしてすぐ閉じる)。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def load_mcp_launcher_command(repo_root: Path = REPO_ROOT) -> tuple[list[str], dict[str, str]]:
    """.mcp.jsonのcalmエントリからlauncher起動コマンドと追加envを取り出す。

    ${CLAUDE_PLUGIN_ROOT} はrepo_rootへ文字列置換する(Claude Code本体のhook実行と
    異なりシェルを経由しないため、区切り文字の変換は不要)。
    """
    mcp_config = json.loads((repo_root / ".mcp.json").read_text(encoding="utf-8"))
    entry = mcp_config["calm"]

    def sub(value: str) -> str:
        return value.replace("${CLAUDE_PLUGIN_ROOT}", str(repo_root))

    argv = [sub(entry["command"]), *[sub(a) for a in entry.get("args", [])]]
    env_overrides = {k: sub(v) for k, v in entry.get("env", {}).items()}
    return argv, env_overrides


def load_hooks_json(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    return json.loads((repo_root / "hooks" / "hooks.json").read_text(encoding="utf-8"))


def iter_hook_entries(hooks_json: dict[str, Any]):
    """hooks.json内の全フックエントリを (event_name, matcher, hook_dict, index) で列挙する。"""
    for event_name, matcher_blocks in hooks_json["hooks"].items():
        for matcher_block in matcher_blocks:
            matcher = matcher_block.get("matcher", "*")
            for idx, hook in enumerate(matcher_block["hooks"]):
                yield event_name, matcher, hook, idx


def resolve_git_bash() -> Path | None:
    """Git for WindowsのGit Bash(bash.exe)を、gitの実体から辿って解決する。

    shutil.which("bash")はWSLのbash.exe(System32配下)を拾うことがあるため使わない。
    """
    git_exe = shutil.which("git")
    if not git_exe:
        return None
    git_path = Path(git_exe).resolve()
    for candidate in (
        git_path.parent.parent / "bin" / "bash.exe",
        git_path.parent.parent / "usr" / "bin" / "bash.exe",
    ):
        if candidate.is_file():
            return candidate
    return None


def resolve_posix_bash() -> Path | None:
    exe = shutil.which("bash")
    return Path(exe) if exe else None


def resolve_powershell5() -> Path | None:
    exe = shutil.which("powershell.exe") or shutil.which("powershell")
    if exe:
        return Path(exe)
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    candidate = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return candidate if candidate.is_file() else None


def resolve_pwsh() -> Path | None:
    exe = shutil.which("pwsh.exe") or shutil.which("pwsh")
    return Path(exe) if exe else None


class HookRunResult:
    """バイナリ入出力の実行結果。stdout/stderrは生bytesで保持する。

    呼び出し側のOS既定ロケールに引きずられて意図しないencodingで文字化けする
    ことを避けるため(text=Trueだと、送信側でも受信側でもPopenが
    locale.getpreferredencoding()を暗黙に使ってしまう)、encoding判定は
    常に呼び出し側が明示的に行う。
    """

    def __init__(self, returncode: int | None, stdout: bytes, stderr: bytes, timed_out: bool) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out

    def stderr_text(self) -> str:
        return self.stderr.decode("utf-8", errors="replace")


def _kill_process_tree(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except OSError:
        pass


def run_with_timeout(
    argv: list[str],
    *,
    input_bytes: bytes,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
) -> HookRunResult:
    """`subprocess.run(timeout=)`は中間シェル/uvだけを殺しpython孫プロセスが
    パイプを握ったままcommunicate()がハングしうるため、Popen + プロセスツリー
    強制終了で timeout を実装する。

    stdin/stdoutはバイナリで扱う(text=Trueにすると、送信側であるこの関数自身の
    プロセスのロケールに応じてinput_bytesの再encodeやstdoutの暗黙decodeが
    発生し、呼び出し側が意図した正確なバイト列を送受信できなくなるため)。
    """
    popen_kwargs: dict[str, Any] = dict(
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(cwd),
        env=env,
    )
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(argv, **popen_kwargs)
    try:
        stdout, stderr = proc.communicate(input=input_bytes, timeout=timeout)
        return HookRunResult(proc.returncode, stdout, stderr, False)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc.pid)
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
        return HookRunResult(None, stdout, stderr, True)


def read_lock_file(tmp_home: Path) -> dict[str, Any] | None:
    """一時HOME配下の~/.cc-memory/server.lockを読む。無ければNone。"""
    lock_path = tmp_home / ".cc-memory" / "server.lock"
    if not lock_path.is_file():
        return None
    try:
        return json.loads(lock_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def kill_pid(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
