"""launcherプロセスの終了契機を、実プロセスで検証する。

HTTPサーバーを自動起動しないよう、リモートモード（CALM_URL指定、接続先は
閉じたポート）で起こす。起こしたプロセスは必ずfixtureで後始末する。
"""
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import psutil
import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIXのシグナル・pipe継承を前提とする"
)

# launcher本体を起こす子スクリプト。argv[1]は見張り対象として差し替えるpid。
_LAUNCHER_SCRIPT = textwrap.dedent(
    """
    import sys, psutil
    from src import launcher
    launcher.PARENT_WATCH_INTERVAL_SEC = 0.2
    watched = int(sys.argv[1]) if len(sys.argv) > 1 else None
    if watched:
        launcher._parent_watch_targets = lambda: [(watched, psutil.Process(watched).create_time())]
    launcher.main()
    """
)

# 中間プロセス: launcherを子として起こし、自身は生き続ける（uvの役）。
# stdinは継承する。テスト側が書き込み端を握っておくので、中間が死んでも
# launcherにstdin EOFは届かない（EOF以外の経路で終了することを確かめるため）。
_MIDDLE_SCRIPT = textwrap.dedent(
    """
    import subprocess, sys, time
    p = subprocess.Popen([sys.executable, "-c", sys.argv[1], sys.argv[2]])
    print(p.pid, flush=True)
    time.sleep(120)
    """
)


@pytest.fixture
def spawned():
    procs: list[subprocess.Popen] = []
    yield procs
    for p in procs:
        if p.poll() is None:
            p.kill()
        p.wait()


@pytest.fixture
def launcher_env(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CALM_", "CC_MEMORY_"))}
    env.update(CALM_URL="http://127.0.0.1:9", RELAY_STATE_DIR=str(tmp_path))
    return env


@pytest.fixture
def held_stdin():
    r, w = os.pipe()
    yield r
    os.close(r)
    os.close(w)


def _spawn_launcher(env, spawned, watched_pid=None):
    args = [sys.executable, "-c", _LAUNCHER_SCRIPT]
    if watched_pid:
        args.append(str(watched_pid))
    p = subprocess.Popen(args, env=env, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    spawned.append(p)
    _wait_registered(env, p.pid)
    return p


def _wait_registered(env, pid: int) -> None:
    """launcherがシグナル登録を終え、登録ファイルを書くまで待つ（その直後にwatchdogが始まる）。"""
    path = Path(env["RELAY_STATE_DIR"]) / "sessions" / f"launcher-{pid}.json"
    deadline = time.time() + 20
    while not path.exists():
        assert time.time() < deadline, "launcherが起動しなかった"
        time.sleep(0.05)
    time.sleep(0.5)


def _wait_gone(pid: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
            return True
        time.sleep(0.1)
    return False


def test_sigterm_terminates_launcher(launcher_env, spawned):
    p = _spawn_launcher(launcher_env, spawned)
    p.send_signal(signal.SIGTERM)
    assert p.wait(timeout=15) == 0  # 強制終了(1)ではなく通常終了


def test_launcher_exits_when_watched_cli_dies_even_if_direct_parent_lives(launcher_env, spawned, held_stdin):
    cli = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    spawned.append(cli)
    middle = subprocess.Popen(
        [sys.executable, "-c", _MIDDLE_SCRIPT, _LAUNCHER_SCRIPT, str(cli.pid)],
        env=launcher_env, stdin=held_stdin, stdout=subprocess.PIPE, text=True,
    )
    spawned.append(middle)
    launcher_pid = int(middle.stdout.readline())
    _wait_registered(launcher_env, launcher_pid)
    assert psutil.pid_exists(launcher_pid)

    cli.kill()
    cli.wait()

    assert _wait_gone(launcher_pid, 15), "見張り対象のCLIが死んだのにlauncherが残った"
    assert middle.poll() is None  # 直接の親は生きたまま


def test_launcher_exits_when_direct_parent_dies(launcher_env, spawned, held_stdin):
    middle = subprocess.Popen(
        [sys.executable, "-c", _MIDDLE_SCRIPT, _LAUNCHER_SCRIPT, "0"],
        env=launcher_env, stdin=held_stdin, stdout=subprocess.PIPE, text=True,
    )
    spawned.append(middle)
    launcher_pid = int(middle.stdout.readline())
    _wait_registered(launcher_env, launcher_pid)
    assert psutil.pid_exists(launcher_pid)

    middle.kill()
    middle.wait()

    assert _wait_gone(launcher_pid, 15), "直接の親が死んだのにlauncherが残った"
