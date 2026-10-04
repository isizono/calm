"""fcntlが無い環境でも起動経路のモジュールがimportできることを確かめる。

このMac上のCPythonにはfcntlが入っているため、素の`import <module>`だけでは
「トップレベルimport fcntlの復活」という退行を検知できない（Windowsで初めて
ModuleNotFoundErrorになる）。`sys.modules["fcntl"] = None`で塞いだ子プロセスで
importし直すことで、Windowsで実際に起きる失敗をこのMacから再現する。

対象はWindowsでも起動する必要がある経路（サーバー本体・launcher・hooks）と、
それらがトップレベルimportで辿る依存モジュール一式。tmux/fcntl前提で
Windowsに移植しない`hooks.recorder_watch`もここに含める。`_acquire_lock`内へ
遅延importしたfcntlが再びトップレベルへ戻ると、それをトップレベルimportする
`src.services.recorder_launcher_service`ごとimportできなくなるため。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_TARGET_MODULES = [
    "src.main",
    "src.launcher",
    "src.services.session_registry_service",
    "src.services.recorder_launcher_service",
    "src.infra.lock_file",
    "src.infra.session_identity",
    "src.infra.process_signature",
    "hooks.recorder_watch",
    "hooks.recorder_autostart_hook",
    "hooks.ask_answer_rewake_hook",
]


@pytest.mark.parametrize("module_name", _TARGET_MODULES)
def test_import_succeeds_with_fcntl_blocked(module_name, tmp_path):
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        "CALM_DB_PATH": str(tmp_path / "discussion-test.db"),
    }
    code = f"import sys; sys.modules['fcntl'] = None; import {module_name}"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"`import {module_name}` failed with fcntl blocked:\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
