"""src/services/vessel_rules.py の依存の軽さを別プロセスで確かめる番人テスト。

vessel_rules は hook から直接importされるため、numpy・yoyo・sqlite_vec・src.db
のような重い依存を引き込むと hook の起動コストが上がる。同一プロセスだと
pytest自体が既にこれらのモジュールを読み込んでしまうため、別プロセスでの
importでなければ検証できない。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_FORBIDDEN_PREFIXES = ("numpy", "yoyo", "sqlite_vec", "src.db")

_CHECK_SCRIPT = (
    "import sys\n"
    "import src.services.vessel_rules\n"
    f"prefixes = {_FORBIDDEN_PREFIXES!r}\n"
    "hits = sorted(m for m in sys.modules if any(m == p or m.startswith(p + '.') for p in prefixes))\n"
    "print(','.join(hits))\n"
)


def test_importing_vessel_rules_does_not_pull_heavy_dependencies():
    result = subprocess.run(
        [sys.executable, "-c", _CHECK_SCRIPT],
        cwd=_PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    hits = [h for h in result.stdout.strip().split(",") if h]
    assert hits == [], f"vessel_rules の import が重い依存を引き込んだ: {hits}"
