"""`import src.main` が子プロセスで成功することを確かめる。

src.mainが起動時にトップレベルimportするモジュール群のどこかにPOSIX専用の
依存（例: fcntl）が紛れ込むと、Windowsでは`import src.main`の時点で
ModuleNotFoundErrorになり、HTTPサーバーが起動前に即死する。

このテストは import 成功だけを確認する（サーバーは起動しない、DB にも触れない）。
"""
from __future__ import annotations

import subprocess
import sys

from tests.windows.support import REPO_ROOT, isolated_env


def test_import_src_main_succeeds(tmp_path):
    env = isolated_env(tmp_path)
    result = subprocess.run(
        [sys.executable, "-c", "import src.main"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        "`import src.main` failed:\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
