"""src/infra/detached_process.py の中継スクリプトを実プロセスとして動かすE2Eテスト

中継(`sys.executable -c` で起動する`_RELAY_SCRIPT`)はJSON経由で渡された本命を
起動し、pidを1行出力してすぐ終了する。ここでは実際にOSプロセスとして起動し、
「中継は本命の完了を待たずに終了する」「本命は実際に起動し指定した入出力へ
書き込む」ことを確認する。Windows専用のcreationflagsは`_RELAY_SCRIPT`内の
`sys.platform == "win32"`分岐にあり、Mac上では素のPopenにフォールバックする
ため、ここで検証できるのは中継の制御フローそのもの(fire-and-forgetの構造)。
"""
import json
import subprocess
import sys
import time

import psutil

from src.infra.detached_process import _RELAY_SCRIPT


def _run_relay(payload: dict, timeout: float = 10.0):
    relay = subprocess.Popen(
        [sys.executable, "-c", _RELAY_SCRIPT],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    out, err = relay.communicate(json.dumps(payload).encode("utf-8"), timeout=timeout)
    return relay.returncode, out.decode("utf-8"), err.decode("utf-8")


def test_relay_exits_immediately_without_waiting_for_target():
    """中継は本命の終了を待たず、本命を起動したらすぐ終了すること"""
    start = time.monotonic()
    returncode, out, err = _run_relay({
        "argv": [sys.executable, "-c", "import time; time.sleep(5)"],
        "cwd": None,
        "stdout": {"kind": "devnull"},
        "stderr": {"kind": "devnull"},
    })
    elapsed = time.monotonic() - start

    assert returncode == 0, err
    assert elapsed < 3.0, "中継が本命(5秒sleep)の終了を待ってしまっている"

    pid = int(out.strip())
    try:
        assert psutil.Process(pid).is_running()
    finally:
        try:
            psutil.Process(pid).kill()
        except psutil.NoSuchProcess:
            pass


def test_relay_launched_target_writes_to_the_requested_files(tmp_path):
    """stdout/stderrにpath指定した場合、本命が実際にそのファイルへ書き込むこと。
    stderr=same_as_stdoutのときはstdoutと同じファイルに書き込まれること"""
    log_path = tmp_path / "out.log"
    returncode, out, err = _run_relay({
        "argv": [
            sys.executable, "-c",
            "import sys; sys.stdout.write('stdout-line\\n'); "
            "sys.stderr.write('stderr-line\\n')",
        ],
        "cwd": None,
        "stdout": {"kind": "path", "path": str(log_path)},
        "stderr": {"kind": "same_as_stdout"},
    })

    assert returncode == 0, err
    pid = int(out.strip())

    deadline = time.monotonic() + 5.0
    while psutil.pid_exists(pid) and time.monotonic() < deadline:
        time.sleep(0.05)

    content = log_path.read_text()
    assert "stdout-line" in content
    assert "stderr-line" in content
